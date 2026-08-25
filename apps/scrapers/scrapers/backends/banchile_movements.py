"""Banco de Chile movements: the checking cartola plus the card's two legs.

Issue #57. BanChile transactions used to come from `fintself`, which parses the
rendered movements table and therefore never sees an operation id, so every
`external_id` was a hash of date + description + amount + account and every
failure mode of that hash was a data-integrity bug (#55, #56). Four read-only
live sessions established where the bank's own ids actually live, and none of
them is in the movements list itself:

  * checking (`movimientos/getCartola`): each movement carries a composite `id`
    the bank builds from producto + cuenta + fecha + monto + tipo. It is stable
    across logins but NOT unique (batch credits posted in the same second for
    the same amount collide: 37 distinct values for 42 movements), and an opaque
    `infoDataGlosaAdicional` token that is session-scoped (zero overlap between
    two logins), so neither can be an identity on its own. The operation id (the
    UI's "ID Transacción", behind the "+" expander) is either inline in
    `detalleGlosa` as an "Id transaccion: ..." line, or one extra POST away
    (`movimientos/cartola/detalle-glosa` -> `transaccionId`). Measured over a
    42-movement window: coverage 39/42, all 39 distinct, identical across two
    separate logins, and every same-second collision resolved into distinct ids.
  * card, unbilled (`tarjeta-credito-digital/movimientos-no-facturados`): no id
    at all, and its `detalle-glosa` sibling answered 501 "Glosa aun no
    implementada" for all 44 rows, so there is nothing deeper to fetch. A
    description-free fingerprint is all this leg offers.
  * card, billed (`tarjetas/estadocuenta/nacional/resumen-por-fecha`): every row
    carries `numReferencia` ("DDMM NNNNNNNN"), unique across the 65 real rows of
    a statement and byte-identical across two logins. Rows flagged
    `totales: true` are summary lines and are dropped before anything else, and
    an all-zero 8-digit suffix means "no reference", not a value.

Why this is its own module: `banchile_web.py` is 1.6k lines about *balances* and
its docstring is a map of that surface. Movements are a different read, with
their own payload shapes and their own hazards, sharing only the session. So
this module owns the movements surfaces and reuses banchile_web's primitives
(browser launch, login, popup dismissal, portal fencing, the bounded-retry
budgets, the amount/date parsers) instead of re-implementing them. The shared
session entry point (`fetch_session`, which folds in issue #28's "one login per
run") lives here too, because it depends on both modules.

Same discipline as banchile_web: the Playwright code only navigates, fires the
calls and hands *raw payloads* to the pure helpers below, which are unit-tested
with synthetic dicts and never touch a browser. Every surface goes through
banchile_web's bounded-retry machinery and stays non-fatal, so one flaky surface
cannot cost the others.

Two deliberate limitations, both grounded in what the live sessions did and did
not establish:

  * We consume the SPA's *own* responses rather than composing requests, because
    the card endpoints take a card descriptor (`idTarjeta`, `codigoProducto`,
    `mascara`, `nombreTitular`, ...) whose derivation was never observed. The
    only calls we compose are ones whose body is fully known: the per-movement
    `detalle-glosa` (built from the movement itself) and the billed statement
    replay (the SPA's own captured body with just `fechaFacturacion` swapped).
  * `getCartola` paging is therefore NOT implemented: its request body was never
    observed, so a second page cannot be composed, and no "ver más" control was
    observed either. `pagina[0].masPaginas` is logged as a warning when it is
    set, so a truncated window is visible in the logs instead of silent. The
    window the portal returns by default covers far more than the daily cadence
    the scraper runs at.
"""

import asyncio
import datetime
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from scrapers.backends.banchile_web import (
    DEFAULT_TIMEOUT,
    _CARD_LINK_SELECTORS,
    _CARD_LINK_TIMEOUTS_MS,
    _PORTAL_HOST,
    _RENDER_TIMEOUTS_MS,
    _budget,
    _click_first,
    _dismiss_popup,
    _ensure_on_portal,
    _launch_browser,
    _login,
    _new_context,
    _on_portal,
    _parse_date_ddmmyyyy,
    _read_all_surfaces,
    _read_surface_with_retries,
    _recover_to_home,
    parse_clp,
)
from scrapers.base import ScrapedProduct

logger = logging.getLogger(__name__)


# --- What a movements read yields ---------------------------------------------


@dataclass(frozen=True)
class BanChileMovement:
    """One movement, normalised out of a raw payload row.

    `operation_id` is the bank's own id when this leg has one (`transaccionId`
    for checking, `numReferencia` for a billed card row) and None otherwise;
    `fingerprint` is the leg's description-free identity fields, in a fixed
    order, for the movements the bank gives no id for. The `external_id` scheme
    built on top of the two lives in `institutions/banchile.py`, which owns the
    key format; this module only decides *which* fields are identity.

    `source` is for logging only. It is deliberately NOT part of any key: a
    charge crossing from the unbilled to the billed leg must not re-key itself,
    which is exactly the bug issue #56 tracks.
    """

    source: str
    product_kind: str
    description: str
    amount: int
    transaction_date: datetime.date
    operation_id: Optional[str] = None
    fingerprint: tuple[str, ...] = ()


@dataclass(frozen=True)
class BanChileSessionResult:
    """Everything one BanChile login produced: products, movements, failures.

    `failed_surfaces` merges the product surfaces (banchile_web's dashboard /
    card / línea / depósitos / fondos) with the movement ones, so the scraper
    can report partial coverage from either half of the shared session.
    """

    products: list[ScrapedProduct] = field(default_factory=list)
    movements: list[BanChileMovement] = field(default_factory=list)
    failed_surfaces: tuple[str, ...] = ()


# --- Pure helpers (unit-tested; no browser) -----------------------------------

# The inline glosa is a list of "Label: value" lines. Only the id line is read;
# every other label observed (Rut/Cuenta/Nombre Origen and Destinatario, Banco,
# Comentario, ...) is personal data we neither need nor want. The SPA also
# renders a `detalleGlosa[0] == "Fija"` shape whose lines are free-form rather
# than label/value: those simply never match, which is the desired outcome.
# The accent on "transacción" drifts between rows, hence the optional "ó".
_GLOSA_ID_RE = re.compile(r"^\s*id\s+transacci[oó]n\s*[:\-]\s*(.+?)\s*$", re.I)

# `fecha` is "YYYYMMDD HH:MM:SS"; only the date half reaches a transaction.
_CARTOLA_FECHA_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")

# A billed reference is "DDMM NNNNNNNN": four digits of posting day/month, a
# space, then eight digits. An all-zero suffix is the bank's "no reference"
# (observed on a `grupo: "pagos"` row), not a value.
_ALL_ZEROS_RE = re.compile(r"^0+$")


def _text(value) -> str:
    """A payload field as a stripped string; "" for None and non-strings."""
    return value.strip() if isinstance(value, str) else ""


def _number(value) -> Optional[float]:
    """A payload field as a float, or None when it isn't a JSON number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fingerprint_value(value) -> str:
    """Render one identity field for a fingerprint, stably across runs.

    A JSON number that happens to be whole must render the same whether the
    payload spelled it `1234` or `1234.0`, or the same movement would re-key on
    a cosmetic change in the bank's serialisation.
    """
    number = _number(value)
    if number is not None:
        return str(int(number)) if number.is_integer() else repr(number)
    return _text(value)


def operation_id_from_glosa(detalle_glosa) -> Optional[str]:
    """The "Id transaccion: ..." value from a movement's inline glosa.

    Returns None when the glosa is absent, empty, or carries no id line (two of
    the 42 movements observed were exactly that: a non-empty glosa with no id).
    """
    if not isinstance(detalle_glosa, (list, tuple)):
        return None
    for line in detalle_glosa:
        if not isinstance(line, str):
            continue
        match = _GLOSA_ID_RE.match(line)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def operation_id_from_detail(payload) -> Optional[str]:
    """The `transaccionId` from a `cartola/detalle-glosa` response.

    Only a 200 body carries one. The 501 ("Glosa aun no implementada", which is
    what a movement with a non-empty inline glosa answers) and the transient 503
    both mean "no id this run", never an error: the caller passes their bodies
    here and gets None.
    """
    if not isinstance(payload, dict):
        return None
    value = _text(payload.get("transaccionId"))
    return value or None


def needs_detail_glosa(raw) -> bool:
    """True when this movement's id has to be fetched with an extra request.

    Movements whose `detalleGlosa` is non-empty already carry the id inline and
    answer 501 to the endpoint, so asking for them is a wasted round trip (it
    skipped roughly 26 of 42 calls in the observed window). The token and the
    account number are the request body, so a movement missing either can't be
    asked at all.
    """
    if not isinstance(raw, dict):
        return False
    if raw.get("detalleGlosa"):
        return False
    return bool(_text(raw.get("infoDataGlosaAdicional"))) and bool(
        _text(raw.get("numeroCuenta"))
    )


def detail_glosa_body(raw) -> dict:
    """The `cartola/detalle-glosa` request body for one movement."""
    return {
        "infoDataGlosaAdicional": _text(raw.get("infoDataGlosaAdicional")),
        "numeroCuenta": _text(raw.get("numeroCuenta")),
    }


def cartola_has_more_pages(payload) -> bool:
    """True when `getCartola` says its window continues past what it returned.

    We cannot follow it (see the module docstring), so the caller logs a warning
    rather than silently truncating.
    """
    if not isinstance(payload, dict):
        return False
    pagina = payload.get("pagina")
    if not isinstance(pagina, list) or not pagina:
        return False
    first = pagina[0]
    return isinstance(first, dict) and bool(first.get("masPaginas"))


def _cartola_date(raw) -> Optional[datetime.date]:
    """The movement's date: `fecha`'s date half, else `fechaContable`."""
    match = _CARTOLA_FECHA_RE.match(_text(raw.get("fecha")))
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime.date(year, month, day)
        except ValueError:
            pass
    return _parse_date_ddmmyyyy(_text(raw.get("fechaContable")))


def parse_cartola_movement(raw, operation_id: Optional[str] = None):
    """One `getCartola` row -> a BanChileMovement (None when unusable).

    `monto` is unsigned and `tipo` carries the direction, so an unrecognised
    `tipo` is skipped rather than guessed: importing a credit as a charge is
    worse than importing nothing. The fallback identity is the bank's composite
    `id` plus `saldo` (the running ledger balance, verified unique and stable
    across logins: 41 of 41 consecutive deltas equalled the signed `monto`).
    """
    if not isinstance(raw, dict):
        return None
    monto = parse_clp(_text(raw.get("monto")))
    if monto is None:
        return None
    tipo = _text(raw.get("tipo")).lower()
    if tipo == "cargo":
        amount = -abs(monto)
    elif tipo == "abono":
        amount = abs(monto)
    else:
        logger.warning("BanChile: skipping cartola movement with tipo %r", tipo)
        return None

    tx_date = _cartola_date(raw)
    if tx_date is None:
        return None

    bank_id = _text(raw.get("id"))
    saldo = _text(raw.get("saldo"))
    if operation_id is None and not bank_id and not saldo:
        # Nothing stable left to key on; a positional id would re-import the
        # movement on the next scrape.
        return None

    return BanChileMovement(
        source="checking",
        product_kind="checking",
        description=_text(raw.get("descripcion")) or "Movimiento",
        amount=int(amount),
        transaction_date=tx_date,
        operation_id=operation_id,
        fingerprint=(bank_id, saldo),
    )


def parse_unbilled_movement(raw):
    """One `movimientos-no-facturados` row -> a BanChileMovement (or None).

    This leg has no id of any kind, so the key is the description-free
    fingerprint below: posting date, authorisation date and time, amount, the
    card's last four and the Transbank merchant code. `montoCompra` is positive
    for a charge (spending, so a negative amount for us) and negative for a
    payment or refund, which the SPA renders as a `montoPago`.
    """
    if not isinstance(raw, dict):
        return None
    monto = _number(raw.get("montoCompra"))
    if monto is None:
        return None
    tx_date = _parse_date_ddmmyyyy(_text(raw.get("fechaTransaccionString")))
    if tx_date is None:
        return None

    description = (
        _text(raw.get("nombreComercio"))
        or _text(raw.get("glosaTransaccion"))
        or _text(raw.get("descripcionTransaccion"))
        or "Movimiento"
    )
    return BanChileMovement(
        source="card_unbilled",
        product_kind="credit_card",
        description=description,
        amount=-int(round(monto)),
        transaction_date=tx_date,
        operation_id=None,
        fingerprint=tuple(
            _fingerprint_value(raw.get(key))
            for key in (
                "fechaTransaccionString",
                "fechaAutorizacionString",
                "horaAutorizacion",
                "montoCompra",
                "numeroTarjeta",
                "codigoComercioTBK",
            )
        ),
    )


def parse_unbilled_movements(payload) -> list[BanChileMovement]:
    """Every usable row of a `movimientos-no-facturados` response."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("listaMovNoFactur")
    if not isinstance(rows, list):
        return []
    movements = []
    for raw in rows:
        movement = parse_unbilled_movement(raw)
        if movement is not None:
            movements.append(movement)
    return movements


def billed_reference(raw_reference) -> Optional[str]:
    """A billed row's `numReferencia`, or None when it isn't one.

    The format is "DDMM NNNNNNNN". A reference whose 8-digit suffix is all
    zeros is the bank's way of saying there is none (observed on a payment row),
    so it must never become a key: every such row would share it.
    """
    reference = _text(raw_reference)
    if not reference:
        return None
    parts = reference.split()
    suffix = parts[-1] if len(parts) > 1 else reference
    if _ALL_ZEROS_RE.match(suffix.replace(" ", "")):
        return None
    return reference


def parse_billed_movement(raw, fecha_facturacion: str = ""):
    """One `transaccionesTarjetas` row -> a BanChileMovement (or None).

    `totales: true` rows are the section summary lines the SPA prints under
    each group and are dropped by `parse_billed_movements` before this is even
    called. Signs mirror the unbilled leg: a positive `montoTransaccion` is
    spending. The fallback identity (for a row whose reference is absent or
    all zeros) is date + amount + card + statement date; the merchant and the
    `grupo` section name are deliberately excluded, since keying on either is
    the issue #56 bug.
    """
    if not isinstance(raw, dict):
        return None
    monto = _number(raw.get("montoTransaccion"))
    if monto is None:
        return None
    tx_date = _parse_date_ddmmyyyy(_text(raw.get("fechaTransaccionString")))
    if tx_date is None:
        return None

    description = (
        _text(raw.get("descripcion")) or _text(raw.get("comercio")) or "Movimiento"
    )
    return BanChileMovement(
        source="card_billed",
        product_kind="credit_card",
        description=description,
        amount=-int(round(monto)),
        transaction_date=tx_date,
        operation_id=billed_reference(raw.get("numReferencia")),
        fingerprint=(
            _fingerprint_value(raw.get("fechaTransaccionString")),
            _fingerprint_value(raw.get("montoTransaccion")),
            _fingerprint_value(raw.get("nombreTarjeta")),
            _text(fecha_facturacion),
        ),
    )


def parse_billed_movements(payload, fecha_facturacion: str = "") -> list[BanChileMovement]:
    """Every real row of a `resumen-por-fecha` response, summaries dropped."""
    if not isinstance(payload, dict):
        return []
    seccion = payload.get("seccionOperaciones")
    if not isinstance(seccion, dict):
        return []
    rows = seccion.get("transaccionesTarjetas")
    if not isinstance(rows, list):
        return []
    movements = []
    for raw in rows:
        if isinstance(raw, dict) and raw.get("totales"):
            continue  # a group summary line, not a movement
        movement = parse_billed_movement(raw, fecha_facturacion)
        if movement is not None:
            movements.append(movement)
    return movements


def statement_dates(payload, limit: int) -> list[str]:
    """The newest `limit` national statement dates from `fechas-facturacion`.

    The response lists them newest first; only periods that actually have a
    national statement are worth asking for.
    """
    if not isinstance(payload, dict) or limit <= 0:
        return []
    lista = payload.get("listaNacional")
    if not isinstance(lista, list):
        return []
    dates = [
        _text(entry.get("fechaFacturacion"))
        for entry in lista
        if isinstance(entry, dict)
        and entry.get("existeEstadoCuentaNacional")
        and _text(entry.get("fechaFacturacion"))
    ]
    return dates[:limit]


def dedupe_movements(movements: list[BanChileMovement]) -> list[BanChileMovement]:
    """Drop movements repeated within one read, keeping the first of each.

    A checking account read twice (the dialog re-drive below) or a statement
    replayed for a period already captured would otherwise yield the same rows
    again. Identity here is the movement's own identity fields, never its
    position.
    """
    seen: set[tuple] = set()
    unique: list[BanChileMovement] = []
    for movement in movements:
        key = (movement.source, movement.operation_id, movement.fingerprint)
        if key in seen:
            continue
        seen.add(key)
        unique.append(movement)
    return unique


# --- Browser plumbing ---------------------------------------------------------

# Every endpoint is a POST under this base on the portal host, authenticated by
# the session cookie alone: no bearer token, no extra header.
_CARTOLA_FRAGMENT = "movimientos/getCartola"
_DETALLE_GLOSA_PATH = (
    "/mibancochile/rest/persona/bff-pper-prd-cta-movimientos/movimientos/"
    "cartola/detalle-glosa"
)
_UNBILLED_FRAGMENT = "movimientos-no-facturados"
_FECHAS_FACTURACION_FRAGMENT = "estadocuenta/fechas-facturacion"
_BILLED_FRAGMENT = "estadocuenta/nacional/resumen-por-fecha"

_MOVIMIENTOS_ROUTE = "#/movimientos/cuenta/saldos-movimientos"
_BILLED_ROUTE = "#/tarjeta-credito/consultar/facturados"

# The movements route opens a Material dialog ("Seleccione una cuenta") that
# defaults to the USD account, and `getCartola` does not fire until it is
# cleared: pick the currency, pick the account radio, press Aceptar.
_CURRENCY_SELECT_SELECTOR = "mat-select[name=monedas]"
_CURRENCY_OPTION_SELECTOR = "mat-option"
_ACCOUNT_RADIO_SELECTOR = "mat-radio-button"
_MODAL_ACCEPT_SELECTOR = "#modalPrimaryBtn button"
_CLP_OPTION_RE = re.compile(r"\bCLP\b", re.I)

# Movement surfaces are slower than a balance read (the cartola arrives after a
# dialog round trip), so they reuse banchile_web's escalating render budgets.
_DIALOG_SETTLE_MS = 600
# The per-movement id call is one extra request each. Space them so a scrape
# never looks like a burst, and bound the total: the observed window needed ~16
# of them, and a run that wanted far more is a signal, not a licence to hammer.
_GLOSA_PAUSE_MS = 300
_MAX_GLOSA_CALLS = 80
# One radio per account of the chosen currency. The dialog has to be re-driven
# per account (see `_read_checking_movements`), so this bounds the re-drives.
_MAX_CHECKING_ACCOUNTS = 4
# How far back the billed leg reads. The unbilled leg only covers the current
# cycle, so reading the newest two statements covers a charge that crossed the
# billing boundary since the last run (the crossing is what issue #56 is about)
# without walking the whole history on every scrape.
_BILLED_STATEMENTS = 2

_POST_JSON_JS = """
async ({url, body}) => {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await response.json(); } catch (error) { payload = null; }
  return {status: response.status, payload: payload};
}
"""


class _JsonCapture:
    """Collect the SPA's own JSON calls whose URL contains `fragment`.

    We read the portal's responses rather than composing requests because the
    card endpoints take a descriptor we never observed being derived (see the
    module docstring). Both halves are kept: the response body is the data, and
    the request body is what lets the billed leg replay an older statement.
    Every callback is defensive: an event handler that raises would surface as
    an unrelated Playwright error much later.
    """

    def __init__(self, page, fragment: str) -> None:
        self.page = page
        self.fragment = fragment
        self.payloads: list[dict] = []
        self.requests: list[dict] = []

    def _on_request(self, request) -> None:
        try:
            if self.fragment not in (request.url or ""):
                return
            data = request.post_data_json
            if isinstance(data, dict):
                self.requests.append({"url": request.url, "body": data})
        except Exception:
            pass

    def _on_response(self, response) -> None:
        try:
            if self.fragment not in (response.url or "") or response.status != 200:
                return
            payload = response.json()
            if isinstance(payload, dict):
                self.payloads.append(payload)
        except Exception:
            pass

    def __enter__(self) -> "_JsonCapture":
        try:
            self.page.on("request", self._on_request)
            self.page.on("response", self._on_response)
        except Exception:
            logger.debug("BanChile: could not attach a %s listener", self.fragment)
        return self

    def __exit__(self, *exc) -> bool:
        for event, handler in (
            ("request", self._on_request),
            ("response", self._on_response),
        ):
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass
        return False


def _wait_for_payload(page, capture: _JsonCapture, timeout_ms: int) -> Optional[dict]:
    """Poll until `capture` has a payload, the budget runs out, or we go astray.

    Playwright's sync event handlers only run while the caller is inside a wait,
    so the polling `wait_for_timeout` is what lets the capture fill.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if capture.payloads:
            return capture.payloads[-1]
        if not _on_portal(page):
            return None
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(500)


def _post_json(page, url: str, body: dict) -> tuple[int, Optional[dict]]:
    """POST `body` to `url` from inside the authenticated page.

    Returns (status, payload); (0, None) when the call could not be made at all.
    Callers treat every non-200 as "no data", never as fatal.
    """
    try:
        result = page.evaluate(_POST_JSON_JS, {"url": url, "body": body})
    except Exception:
        logger.debug("BanChile: POST %s failed", url, exc_info=True)
        return 0, None
    if not isinstance(result, dict):
        return 0, None
    payload = result.get("payload")
    return int(result.get("status") or 0), payload if isinstance(payload, dict) else None


# --- Checking: the cartola behind the account dialog --------------------------


def _select_clp_currency(page, timeout_ms: int) -> bool:
    """Open the dialog's currency select and pick CLP (it defaults to USD)."""
    try:
        select = page.locator(_CURRENCY_SELECT_SELECTOR).first
        select.wait_for(state="visible", timeout=timeout_ms)
        select.click()
        page.wait_for_timeout(_DIALOG_SETTLE_MS)
        options = page.locator(_CURRENCY_OPTION_SELECTOR)
        for index in range(options.count()):
            option = options.nth(index)
            if _CLP_OPTION_RE.search(option.inner_text() or ""):
                option.click()
                page.wait_for_timeout(_DIALOG_SETTLE_MS)
                return True
    except Exception:
        logger.debug("BanChile: currency select not usable", exc_info=True)
    return False


def _click_account_radio(page, index: int) -> int:
    """Select the index-th account radio; returns how many the dialog offers.

    Returns 0 when the radios aren't there or the index is out of range, which
    the caller reads as "no account to open".
    """
    try:
        radios = page.locator(_ACCOUNT_RADIO_SELECTOR)
        count = radios.count()
        if index >= count:
            return count
        radio = radios.nth(index)
        radio_id = radio.get_attribute("id")
        # The radio's own input is covered by its label, which is what the SPA
        # binds the click to.
        target = page.locator(f"#{radio_id} label").first if radio_id else radio
        target.click()
        page.wait_for_timeout(_DIALOG_SETTLE_MS)
        return count
    except Exception:
        logger.debug("BanChile: account radio %d not usable", index, exc_info=True)
        return 0


def _open_cartola(page, index: int, timeout_ms: int) -> tuple[Optional[dict], int]:
    """Drive the account dialog for account `index` and capture its cartola.

    Returns (payload, account_count). The whole route is re-driven per account
    because no control for re-opening the selector in place was observed; a
    re-drive that fails simply ends the loop with whatever was already read.
    """
    _recover_to_home(page)
    if not _ensure_on_portal(page):
        return None, 0
    with _JsonCapture(page, _CARTOLA_FRAGMENT) as capture:
        try:
            page.evaluate(
                "route => { window.location.hash = route; }", _MOVIMIENTOS_ROUTE
            )
        except Exception:
            logger.debug("BanChile: could not route to the movements view", exc_info=True)
            return None, 0
        if not _select_clp_currency(page, timeout_ms):
            return None, 0
        count = _click_account_radio(page, index)
        if not count or index >= count:
            return None, count
        if not _click_first(page, [_MODAL_ACCEPT_SELECTOR], timeout=timeout_ms):
            return None, count
        return _wait_for_payload(page, capture, timeout_ms), count


def _operation_ids_for(
    page, raw_movements: list, budget: int
) -> tuple[list[Optional[str]], int]:
    """The operation id of each raw movement: inline first, then the XHR.

    The extra call only fires for movements whose `detalleGlosa` is empty (the
    others answer 501), the calls are spaced, and the total is bounded. A 501, a
    503 or an unparseable body all mean "no id this run": the movement falls
    back to its fingerprint key and is adopted onto its operation id by the
    writer once one appears. Returns the ids alongside what is left of the
    budget, so reading a second account cannot restart it.
    """
    ids: list[Optional[str]] = []
    remaining = budget
    for raw in raw_movements:
        if not isinstance(raw, dict):
            ids.append(None)
            continue
        inline = operation_id_from_glosa(raw.get("detalleGlosa"))
        if inline is not None:
            ids.append(inline)
            continue
        if remaining <= 0 or not needs_detail_glosa(raw):
            ids.append(None)
            continue
        remaining -= 1
        url = f"https://{_PORTAL_HOST}{_DETALLE_GLOSA_PATH}"
        status, payload = _post_json(page, url, detail_glosa_body(raw))
        ids.append(operation_id_from_detail(payload) if status == 200 else None)
        try:
            page.wait_for_timeout(_GLOSA_PAUSE_MS)
        except Exception:
            pass
    return ids, remaining


def _movements_from_cartola(
    page, payload: dict, budget: int
) -> tuple[list[BanChileMovement], int]:
    """Shape one `getCartola` payload into movements, ids fetched as needed."""
    raw_movements = payload.get("movimientos")
    if not isinstance(raw_movements, list) or not raw_movements:
        return [], budget
    if cartola_has_more_pages(payload):
        logger.warning(
            "BanChile: the cartola reports more pages than we can read "
            "(getCartola paging is not implemented; issue #57)"
        )
    ids, remaining = _operation_ids_for(page, raw_movements, budget)
    movements = []
    for raw, operation_id in zip(raw_movements, ids):
        movement = parse_cartola_movement(raw, operation_id)
        if movement is not None:
            movements.append(movement)
    with_id = sum(1 for m in movements if m.operation_id)
    logger.info(
        "BanChile: %d checking movements (%d with an operation id)",
        len(movements),
        with_id,
    )
    return movements, remaining


def _read_checking_movements(page, attempt: int) -> list[BanChileMovement]:
    """Read every CLP checking account's cartola (one attempt).

    The dialog lists one radio per account of the chosen currency, and this
    account also holds a USD one, so the currency is picked explicitly first.
    Accounts are read one dialog drive each: the SPA offers no observed way to
    switch accounts without re-opening the selector, so the route is re-driven
    per index and the results deduped. A re-drive that yields nothing ends the
    loop with whatever was already read, which keeps a single-account setup
    (the common case) to exactly one drive.
    """
    budget = _budget(_RENDER_TIMEOUTS_MS, attempt)
    collected: list[BanChileMovement] = []
    glosa_calls_left = _MAX_GLOSA_CALLS
    accounts = 1
    index = 0
    while index < min(accounts, _MAX_CHECKING_ACCOUNTS):
        payload, count = _open_cartola(page, index, budget)
        if count:
            accounts = count
        if payload is None:
            break
        movements, glosa_calls_left = _movements_from_cartola(
            page, payload, glosa_calls_left
        )
        collected.extend(movements)
        index += 1
    return dedupe_movements(collected)


# --- Credit card: the unbilled and billed legs --------------------------------


def _open_card_area(page, attempt: int) -> bool:
    """Click the dashboard's card shortcut, fenced against leaving the portal.

    Reuses banchile_web's `_CARD_LINK_SELECTORS`: the dashboard is littered with
    marketing "Tarjeta de Crédito" links pointing at the public site, so only
    the "SALDOS Y MOV.TARJETAS" shortcut is safe to click.
    """
    if not _ensure_on_portal(page):
        return False
    if not _click_first(
        page, _CARD_LINK_SELECTORS, timeout=_budget(_CARD_LINK_TIMEOUTS_MS, attempt)
    ):
        logger.info("BanChile: card shortcut not found (attempt %d)", attempt + 1)
        return False
    if not _on_portal(page):
        logger.warning("BanChile: card shortcut left the portal; recovering")
        _ensure_on_portal(page)
        return False
    return True


def _read_card_unbilled(page, attempt: int) -> list[BanChileMovement]:
    """Capture the unbilled movements the card page loads (one attempt).

    Nothing is composed here: opening the card page makes the SPA fetch
    `movimientos-no-facturados` itself, and its sibling `detalle-glosa` answered
    501 for every row observed, so there is no per-row call worth making.
    """
    budget = _budget(_RENDER_TIMEOUTS_MS, attempt)
    with _JsonCapture(page, _UNBILLED_FRAGMENT) as capture:
        if not _open_card_area(page, attempt):
            return []
        payload = _wait_for_payload(page, capture, budget)
    if payload is None:
        logger.info("BanChile: unbilled card movements did not load (attempt %d)", attempt + 1)
        return []
    movements = parse_unbilled_movements(payload)
    logger.info("BanChile: %d unbilled card movements", len(movements))
    return movements


def _replay_statement(page, request: dict, fecha: str) -> Optional[dict]:
    """Re-issue the SPA's own `resumen-por-fecha` call for another period."""
    body = dict(request.get("body") or {})
    if not body:
        return None
    body["fechaFacturacion"] = fecha
    status, payload = _post_json(page, request["url"], body)
    if status != 200:
        logger.info("BanChile: statement %s answered %s; skipped", fecha, status)
        return None
    return payload


def _read_card_billed(page, attempt: int) -> list[BanChileMovement]:
    """Read the newest statements' billed movements (one attempt).

    The SPA loads the most recent statement itself when the facturados tab
    opens; older periods are read by replaying that same request with a
    different `fechaFacturacion`, taken from `fechas-facturacion`. Only the
    national (CLP) statement is read: our transaction envelope carries whole
    CLP amounts, so the international one has nowhere to go (it returned zero
    rows in every session observed anyway).
    """
    budget = _budget(_RENDER_TIMEOUTS_MS, attempt)
    with _JsonCapture(page, _FECHAS_FACTURACION_FRAGMENT) as fechas_capture:
        with _JsonCapture(page, _BILLED_FRAGMENT) as billed_capture:
            if not _open_card_area(page, attempt):
                return []
            try:
                page.evaluate(
                    "route => { window.location.hash = route; }", _BILLED_ROUTE
                )
            except Exception:
                logger.debug("BanChile: could not route to the billed tab", exc_info=True)
                return []
            payload = _wait_for_payload(page, billed_capture, budget)
            if payload is None:
                logger.info(
                    "BanChile: billed card movements did not load (attempt %d)",
                    attempt + 1,
                )
                return []
            requests = list(billed_capture.requests)
            fechas_payloads = list(fechas_capture.payloads)

        loaded = _text((requests[-1]["body"] if requests else {}).get("fechaFacturacion"))
        movements = parse_billed_movements(payload, loaded)

        dates = statement_dates(
            fechas_payloads[-1] if fechas_payloads else None, _BILLED_STATEMENTS
        )
        for fecha in dates:
            if not fecha or fecha == loaded or not requests:
                continue
            older = _replay_statement(page, requests[-1], fecha)
            if older is not None:
                movements.extend(parse_billed_movements(older, fecha))

    movements = dedupe_movements(movements)
    logger.info("BanChile: %d billed card movements", len(movements))
    return movements


# Surface label -> per-attempt reader, in read order. The labels feed
# `BanChileSessionResult.failed_surfaces` and become run warnings.
_MOVEMENT_READERS: list[tuple[str, Callable]] = [
    ("movimientos", _read_checking_movements),
    ("tarjeta no facturados", _read_card_unbilled),
    ("tarjeta facturados", _read_card_billed),
]


def read_movement_surfaces(page) -> tuple[list[BanChileMovement], tuple[str, ...]]:
    """Read every movements surface off an authenticated page, with retries.

    This is the seam the tests drive with a fake page. Each surface goes through
    banchile_web's bounded-retry machinery and is non-fatal: one that never
    yields a movement is reported, not raised, so a drift in the card markup
    cannot cost the checking movements.
    """
    movements: list[BanChileMovement] = []
    failed: list[str] = []
    for surface, read in _MOVEMENT_READERS:
        found = _read_surface_with_retries(page, surface, read)
        if found:
            movements.extend(found)
        else:
            failed.append(surface)
    return dedupe_movements(movements), tuple(failed)


# --- The shared session -------------------------------------------------------

def _session_sync(rut: str, password: str, headless: bool) -> BanChileSessionResult:
    """One login, both reads (issue #28): products first, then movements.

    Products run first so the heavier movements read (a dialog drive plus one
    call per movement without an inline glosa) can never cost the balances,
    which is the leg the dashboard depends on every day.
    """
    from playwright.sync_api import sync_playwright  # lazy: keeps tests browser-free

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, headless)
        try:
            context = _new_context(browser)
            page = context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT)

            _login(page, rut, password)
            _dismiss_popup(page)

            balances = _read_all_surfaces(page)
            movements, failed = read_movement_surfaces(page)
            return BanChileSessionResult(
                products=balances.products,
                movements=movements,
                failed_surfaces=balances.failed_surfaces + failed,
            )
        finally:
            browser.close()


async def fetch_session(
    rut: str, password: str, *, headless: bool = True
) -> BanChileSessionResult:
    """Log into Banco de Chile once and read both products and movements.

    Runs the synchronous Playwright flow in a thread executor so it doesn't
    block the scheduler's event loop, mirroring `banchile_web.fetch_balances`.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _session_sync, rut, password, headless)
