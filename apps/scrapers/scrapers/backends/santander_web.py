"""Banco Santander Chile: one headless login, data read from the SPA's own JSON.

The private site (`mibanco.santander.cl`) is an Angular SPA whose data comes
from two JSON APIs, both called by the SPA itself with a bearer token minted at
login:

  * products and balances: `api-dsk.santander.cl/perdsk/datosCliente/
    cruceProductosOnline`, fired automatically by the dashboard right after
    login. Its `MATRIZCAPTACIONES.e1` rows are the deposit products (cuentas
    corrientes, vistas, ...), every field a string.
  * movements: `openbanking.santander.cl/account_balances_transactions_and_
    withholdings_retail/v1/current-accounts/transactions` (the same endpoint
    serves a cuenta vista), fired when the user opens an account's movements
    view from the dashboard.

This module only drives the browser and hands those raw payloads to the pure
parsers below, which are unit-tested with synthetic dicts and never touch a
browser. Nothing is read from the DOM except the controls clicked to log in
and to open the movements view.

The browser half, in order:

  1. Login is always the real (headless) browser: it POSTs to Santander's
     OAuth endpoint together with an Akamai sensor and BioCatch behavioural
     telemetry, so it is never reproduced over HTTP. The RUT and password are
     typed key by key into the masked inputs of the login iframe. Exactly one
     attempt per run; a block page, a step-up prompt or an error banner raises
     a typed error and is never retried.
  2. The dashboard fires `cruceProductosOnline` on its own; its response is
     captured by a `page.on("response")` listener.
  3. Clicking the vista's card makes the SPA call the transactions API, also
     captured. If that yields nothing, the call is replayed once from inside
     the page with the headers the SPA itself sent to the same API host in
     this session (never hardcoded); without them the leg reports partial
     coverage instead.
  4. A best-effort "Cerrar sesión", then the browser closes.

Every wait polls for a specific response or element with a bounded timeout.
`networkidle` is never used: BioCatch telemetry polls continuously, so it
never fires.

Known risk: BioCatch's configuration advertises detection of automated
("agentic") browsers. We do not try to evade it beyond BanChile's context
(no stealth plugin, no other fingerprint change); if Santander starts
blocking or stepping up the headless login, the run fails with the typed error and the user decides.
"""

import asyncio
import datetime as dt
import hashlib
import logging
import re
import secrets
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from scrapers.backends.user_agent import user_agent_for

logger = logging.getLogger(__name__)


# --- Errors -------------------------------------------------------------------
#
# Every message is safe for `scraper_runs.error_message` (shown on the
# dashboard): no RUT, password, token, account number, URL with ids or body.


class SantanderError(RuntimeError):
    """A Santander session failed in a way that cost the whole leg."""


class SantanderBlockedError(SantanderError):
    """Santander's edge served its block page; never retried."""


class SantanderLoginError(SantanderError):
    """The login was rejected or never reached the private site."""


class SantanderStepUpError(SantanderError):
    """The login asked for a second factor; the scraper cannot answer it."""


class SantanderPayloadError(SantanderError):
    """A response the scraper depends on had an unrecognisable shape."""


# --- What a session yields ----------------------------------------------------


@dataclass(frozen=True)
class SantanderAccount:
    """One deposit product row of `MATRIZCAPTACIONES`.

    `contract` and `office` are the raw zero-padded strings; together they form
    the movements API's `accountId`. `balance` is the available balance
    (`MONTODISPONIBLE`) in the product's currency.
    """

    contract: str
    office: str
    glosa: str
    currency: str
    balance: Decimal

    @property
    def account_id(self) -> str:
        return f"{self.office}{self.contract}"

    @property
    def display_number(self) -> str:
        """The contract number without its zero padding, for display only."""
        return self.contract.lstrip("0") or "0"


@dataclass(frozen=True)
class ParsedProducts:
    """The CLP cuentas vista of a products payload, plus what was left out."""

    accounts: list[SantanderAccount] = field(default_factory=list)
    ignored: int = 0
    invalid: int = 0


@dataclass(frozen=True)
class SantanderMovement:
    """One movement of the transactions API, normalised.

    `raw_amount`, `raw_balance` and `flag` are kept verbatim because the
    fallback `external_id` hashes them (see `external_id_for`). `new_balance`
    is the running balance after the movement, when reported.
    """

    transaction_date: date
    accounting_date: Optional[date]
    amount: Decimal
    currency: str
    description: str
    movement_number: Optional[str]
    raw_amount: str
    raw_balance: str
    flag: str
    new_balance: Optional[Decimal] = None


@dataclass(frozen=True)
class ParsedMovements:
    """The usable movements of a transactions payload, plus skip counts.

    `zero` counts zero-amount rows (routine, never a warning), `skipped` the
    unusable ones (no date, unparseable amount, a CLP fraction) and
    `sign_mismatches` the rows whose sign and debit/credit flag disagree.
    """

    movements: list[SantanderMovement] = field(default_factory=list)
    skipped: int = 0
    sign_mismatches: int = 0
    zero: int = 0


# --- Pure helpers (unit-tested; no browser) -----------------------------------

_CENTS_RE = re.compile(r"^\s*([+-]?)\s*(\d+)\s*([+-]?)\s*$")
_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_DIGITS_RE = re.compile(r"^\d+$")
_SPACES_RE = re.compile(r"\s+")
_CENT = Decimal("0.01")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_cents(raw: Any) -> Optional[Decimal]:
    """A zero-padded string of cents ("000000000250000000") as a Decimal.

    Santander pads every amount with leading zeros and keeps two implied
    decimals: the last two digits are cents. Money out carries a "-" after the
    digits ("000000099999900-"); a leading sign is accepted too. None for
    anything else (blank, letters, a decimal point, two signs).
    """
    if not isinstance(raw, str):
        return None
    match = _CENTS_RE.match(raw)
    if match is None:
        return None
    leading, digits, trailing = match.groups()
    if leading and trailing:
        return None
    value = (Decimal(int(digits)) * _CENT).quantize(_CENT)
    return -value if "-" in (leading, trailing) else value


def parse_iso_date(raw: Any) -> Optional[date]:
    """The calendar date at the start of an ISO string, as printed.

    The API reports dates as "YYYY-MM-DD" optionally followed by a time; only
    the printed date is taken, never shifted by a time zone.
    """
    if not isinstance(raw, str):
        return None
    match = _ISO_DATE_RE.match(raw)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _is_whole(value: Decimal) -> bool:
    return value == value.to_integral_value()


def is_clp_vista(row: dict) -> bool:
    """True for a `MATRIZCAPTACIONES` row that is a cuenta vista in CLP.

    The short label is the only product-type field whose meaning is known
    ("CTA VISTA ..."); `PRODUCTO` / `SUBPRODUCTO` are opaque codes.
    """
    glosa = _text(row.get("GLOSACORTA")).upper()
    currency = _text(row.get("CODIGOMONEDA")).upper()
    return currency == "CLP" and "VISTA" in glosa


def _rows(matrix: Any) -> list:
    """`e1` as a list: SOAP-ish backends send one item as a bare dict."""
    if not isinstance(matrix, dict):
        return []
    rows = matrix.get("e1")
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def parse_products(payload: Any) -> ParsedProducts:
    """The CLP cuentas vista of a `cruceProductosOnline` payload.

    Rows of any other currency or label are counted in `ignored` and dropped:
    the scraper only models a CLP cuenta vista. A vista row whose contract or
    balance is unusable is counted in `invalid`. The personal fields under
    `ESCALARES` are never read.

    The balance is `MONTODISPONIBLE` read as cents (last two digits). That
    interpretation is shared with the movements API's amounts and was checked
    live only against a zero balance, so `fetch_session` cross-checks it with
    the latest movement's running balance and logs a warning when they
    disagree. A CLP balance with a fraction is treated as invalid rather than
    rounded, since it would mean the interpretation is wrong.

    Raises `SantanderPayloadError` when `DATA.OUTPUT` is missing altogether; a
    payload without `MATRIZCAPTACIONES` simply has no accounts.
    """
    output = None
    if isinstance(payload, dict):
        data = payload.get("DATA")
        if isinstance(data, dict):
            output = data.get("OUTPUT")
    if not isinstance(output, dict):
        raise SantanderPayloadError("Santander product list had an unexpected shape")
    matrices = output.get("MATRICES")
    rows = _rows(matrices.get("MATRIZCAPTACIONES")) if isinstance(matrices, dict) else []

    accounts: list[SantanderAccount] = []
    ignored = invalid = 0
    for row in rows:
        if not is_clp_vista(row):
            ignored += 1
            continue
        contract = _text(row.get("NUMEROCONTRATO"))
        office = _text(row.get("OFICINACONTRATO"))
        balance = parse_cents(row.get("MONTODISPONIBLE"))
        if (
            not _DIGITS_RE.match(contract)
            or not _DIGITS_RE.match(office)
            or balance is None
            or not _is_whole(balance)
        ):
            invalid += 1
            continue
        accounts.append(
            SantanderAccount(
                contract=contract,
                office=office,
                glosa=_text(row.get("GLOSACORTA")),
                currency="CLP",
                balance=balance.quantize(Decimal(1)),
            )
        )
    # A stable order (lowest contract first), not the API's, so a holder of
    # several vistas always gets the same one read.
    accounts.sort(key=lambda account: int(account.contract))
    return ParsedProducts(accounts=accounts, ignored=ignored, invalid=invalid)


def _description(row: dict) -> str:
    """The bank's own text for a movement, whitespace collapsed."""
    for key in ("observation", "expandedCode"):
        text = _SPACES_RE.sub(" ", _text(row.get(key)))
        if text:
            return text
    return "Movimiento Santander"


def _movement_number(raw: Any) -> Optional[str]:
    """`movementNumber` without its zero padding; None when absent or all zeros."""
    text = _text(raw)
    if not _DIGITS_RE.match(text):
        return None
    return text.lstrip("0") or None


SKIP_INVALID = "invalid"
SKIP_ZERO = "zero"
SKIP_SIGN_MISMATCH = "sign_mismatch"


def parse_movement(
    row: Any, currency: str = "CLP"
) -> tuple[Optional[SantanderMovement], Optional[str]]:
    """One raw movement, or None and the reason it must be skipped.

    Returns `(movement, None)` or `(None, reason)`. The sign comes from the
    trailing "-" of `movementAmount` and is cross-checked with
    `chargePaymentFlag` ("D" is money out, "H" money in): a row where the two
    disagree is `SKIP_SIGN_MISMATCH`, since either reading could be wrong. A
    zero amount is `SKIP_ZERO`; no date, an unparseable amount or a CLP amount
    with a fraction is `SKIP_INVALID`.
    """
    if not isinstance(row, dict):
        return None, SKIP_INVALID
    transaction_date = parse_iso_date(row.get("transactionDate"))
    accounting_date = parse_iso_date(row.get("accountingDate"))
    if transaction_date is None:
        transaction_date = accounting_date
    raw_amount = _text(row.get("movementAmount"))
    amount = parse_cents(raw_amount)
    if transaction_date is None or amount is None:
        return None, SKIP_INVALID
    if amount == 0:
        return None, SKIP_ZERO
    if currency == "CLP":
        if not _is_whole(amount):
            return None, SKIP_INVALID
        amount = amount.quantize(Decimal(1))
    flag = _text(row.get("chargePaymentFlag")).upper()
    if (flag == "D" and amount > 0) or (flag == "H" and amount < 0):
        return None, SKIP_SIGN_MISMATCH
    raw_balance = _text(row.get("newBalance"))
    return (
        SantanderMovement(
            transaction_date=transaction_date,
            accounting_date=accounting_date,
            amount=amount,
            currency=currency,
            description=_description(row),
            movement_number=_movement_number(row.get("movementNumber")),
            raw_amount=raw_amount,
            raw_balance=raw_balance,
            flag=flag,
            new_balance=parse_cents(raw_balance),
        ),
        None,
    )


def parse_movements(payload: Any, currency: str = "CLP") -> ParsedMovements:
    """Every usable movement of a transactions payload.

    Raises `SantanderPayloadError` when `movements` is not a list; an empty
    list is a valid, quiet account. Skipped rows are counted, not logged one
    by one (see `parse_movement` for the rules).
    """
    rows = payload.get("movements") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SantanderPayloadError("Santander movements had an unexpected shape")
    movements: list[SantanderMovement] = []
    counts = {SKIP_INVALID: 0, SKIP_ZERO: 0, SKIP_SIGN_MISMATCH: 0}
    for row in rows:
        movement, reason = parse_movement(row, currency)
        if movement is not None:
            movements.append(movement)
        else:
            counts[reason] += 1
    return ParsedMovements(
        movements=movements,
        skipped=counts[SKIP_INVALID],
        sign_mismatches=counts[SKIP_SIGN_MISMATCH],
        zero=counts[SKIP_ZERO],
    )


def external_id_for(movement: SantanderMovement) -> str:
    """The `external_id` of one Santander movement.

    ``san_mov_<accountingDate>_<movementNumber>``
        The bank's own movement number, without its zero padding, scoped by
        the posting date: the number may be a per-posting-day sequence, so the
        date it is scoped by must be the one the bank numbers by. It was
        identical across two separate logins.

    ``san_fp_<md5[:16]>``
        The fallback when a movement has no usable number or no posting date
        (the occurrence date is never substituted for it): a hash of the
        description-free fields only (currency, both dates, the raw amount,
        the raw running balance and the debit/credit flag). The running
        balance keeps two same-day movements of the same amount apart. The
        description (`observation`, `expandedCode`) is never hashed: banks
        reword it.
    """
    if movement.movement_number and movement.accounting_date:
        return f"san_mov_{movement.accounting_date.isoformat()}_{movement.movement_number}"
    parts = (
        movement.currency,
        movement.transaction_date.isoformat(),
        movement.accounting_date.isoformat() if movement.accounting_date else "",
        movement.raw_amount,
        movement.raw_balance,
        movement.flag,
    )
    digest = hashlib.md5("|".join(parts).encode()).hexdigest()[:16]
    return f"san_fp_{digest}"


def latest_balance(movements: list[SantanderMovement]) -> Optional[Decimal]:
    """The running balance after the most recent movement, when reported.

    "Most recent" is the latest posting (else occurrence) date, then the
    highest movement number; movements without a balance are ignored.
    """
    def key(movement: SantanderMovement) -> tuple:
        day = movement.accounting_date or movement.transaction_date
        number = int(movement.movement_number) if movement.movement_number else -1
        return (day, number)

    candidates = [m for m in movements if m.new_balance is not None]
    if not candidates:
        return None
    return max(candidates, key=key).new_balance


def transactions_request_body(account: SantanderAccount, opening: date, closing: date) -> dict:
    """The body the SPA sends to the transactions API for one account."""
    return {
        "accountId": account.account_id,
        "currency": account.currency,
        "commercialGroup": "",
        "openingDate": opening.isoformat(),
        "closingDate": closing.isoformat(),
    }


# --- Browser half: one headless login per run ---------------------------------

LOGIN_URL = "https://banco.santander.cl/personas"
MOVEMENTS_URL = (
    "https://openbanking.santander.cl/account_balances_transactions_and_"
    "withholdings_retail/v1/current-accounts/transactions"
)
_PUBLIC_HOST = "banco.santander.cl"
_PRIVATE_HOST = "mibanco.santander.cl"
_OPENBANKING_HOST = "openbanking.santander.cl"
_PRODUCTS_FRAGMENT = "/perdsk/datosCliente/cruceProductosOnline"
_MOVEMENTS_FRAGMENT = "/current-accounts/transactions"
# Santander's "Revisa tu conexión a internet" page, which its edge serves to a
# client it refuses, loads its assets from here.
_BLOCK_FRAGMENT = "/fueradelinea/"

# A realistic desktop context, BanChile's ceiling and no further: locale,
# timezone, viewport, a user agent that drops headless Chromium's
# "HeadlessChrome" marker but keeps the real engine version and host OS (a
# fixed, stale UA contradicts the client hints the browser also sends), and
# the `navigator.webdriver` mask. The live headless login only passed with
# that context. No stealth plugin, no other fingerprint change.
_WEBDRIVER_MASK_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
VIEWPORT = {"width": 1366, "height": 768}
LOCALE = "es-CL"
TIMEZONE_ID = "America/Santiago"
_SANTIAGO = ZoneInfo(TIMEZONE_ID)

# Timeouts (ms). Every wait is bounded and polls for a specific response or
# element: never `networkidle`, which BioCatch's continuous telemetry keeps
# from ever firing.
_GOTO_TIMEOUT_MS = 90_000
_LOGIN_FORM_TIMEOUT_MS = 30_000
_LOGIN_RESULT_TIMEOUT_MS = 120_000
_PRODUCTS_TIMEOUT_MS = 45_000
_MOVEMENTS_TIMEOUT_MS = 30_000
_CARD_TIMEOUT_MS = 15_000
_CLICK_TIMEOUT_MS = 10_000
_LOGOUT_TIMEOUT_MS = 5_000
_POLL_MS = 250
# The masked Angular inputs drop characters typed faster than this.
_TYPE_DELAY_MS = 120
# The window the SPA itself was seen requesting, reused by the replay fallback.
_REPLAY_WINDOW_DAYS = 69

# Headers the replay fallback needs from a request the SPA made itself in this
# session; without all of the required ones it does not try.
_REPLAY_HEADERS_REQUIRED = ("authorization", "x-client-code", "x-organization-code")
_REPLAY_HEADERS_OPTIONAL = ("x-santander-client-id", "x-schema-id")

# Matched against accent-free, lower-cased text. In the login iframe only text
# that appeared after submitting counts (see `new_markers`): its idle form
# already says "¿Olvidaste tu RUT o clave?" and may mention Santander Pass.
_STEP_UP_MARKERS = (
    "santander pass",
    "clave dinamica",
    "superclave",
    "segundo factor",
    "codigo de verificacion",
)
# On a private-site page before the product list arrives, Santander Pass is
# left out: the dashboard may advertise it.
_PAGE_STEP_UP_MARKERS = tuple(m for m in _STEP_UP_MARKERS if m != "santander pass")
# Never observed live (every login so far succeeded); kept conservative so a
# promotional text cannot trip it.
_LOGIN_ERROR_MARKERS = (
    "clave incorrecta",
    "rut o clave",
    "rut y/o clave",
    "datos ingresados no son correctos",
    "clave bloqueada",
    "clave se encuentra bloqueada",
)

_POST_JSON_JS = """
async ({url, body, headers}) => {
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await response.json(); } catch (e) { payload = null; }
  return {status: response.status, payload};
}
"""

_INNER_TEXT_JS = "() => (document.body ? document.body.innerText : '')"


@dataclass(frozen=True)
class SantanderSessionResult:
    """What one Santander login produced.

    `account` is the CLP cuenta vista (None when the product list has none),
    `movements` its parsed movements and `warnings` the partial-coverage notes
    the scraper hands to `ProductScrapeResult.warnings`.
    """

    account: Optional[SantanderAccount] = None
    movements: list[SantanderMovement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _normalized_text(text: str) -> str:
    """Lower-cased and accent-free, for marker matching."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def find_marker(text: str, markers: tuple[str, ...]) -> Optional[str]:
    """The first of `markers` present in `text`, matched accent-insensitively."""
    normalized = _normalized_text(text)
    return next((marker for marker in markers if marker in normalized), None)


def new_markers(before: str, after: str, markers: tuple[str, ...]) -> list[str]:
    """The `markers` present in `after` but not in `before`, accent-insensitively.

    Used on the login iframe, whose idle form already carries some marker
    words: only what appeared after submitting is a reaction to the login.
    """
    old, new = _normalized_text(before), _normalized_text(after)
    return [marker for marker in markers if marker in new and marker not in old]


def is_block_document(url: str, is_main_frame: bool) -> bool:
    """Whether a 403 document response is Santander's edge refusing us.

    Only the main frame or a Santander host counts, so a third-party iframe
    (an ad, a tracker) answering 403 cannot fail the run.
    """
    host = (urlparse(url).hostname or "").lower()
    return is_main_frame or host == "santander.cl" or host.endswith(".santander.cl")


class SessionCapture:
    """Everything the page's own traffic tells us, collected by listeners.

    Holds the products payloads, the movements payloads paired with the
    `accountId` their request asked for, the headers of the SPA's latest
    authenticated open-banking request (for the replay fallback) and whether
    the edge's block page showed up. Every callback is defensive: a handler
    that raises would surface as an unrelated Playwright error much later.
    """

    def __init__(self) -> None:
        self.products: list[dict] = []
        self.movements: list[tuple[Optional[str], dict]] = []
        self.headers: dict[str, str] = {}
        self.blocked = False
        # The page's main frame, set once the page exists (block detection).
        self.main_frame = None

    def on_request(self, request) -> None:
        try:
            url = request.url or ""
            if _BLOCK_FRAGMENT in url:
                self.blocked = True
            if (urlparse(url).hostname or "").lower() != _OPENBANKING_HOST:
                return
            headers = {k.lower(): v for k, v in (request.headers or {}).items()}
            if headers.get("authorization"):
                self.headers = headers
        except Exception:
            pass

    def on_response(self, response) -> None:
        try:
            url = response.url or ""
            if _BLOCK_FRAGMENT in url:
                self.blocked = True
                return
            if response.status == 403 and response.request.resource_type == "document":
                frame = getattr(response, "frame", None)
                main = self.main_frame is not None and frame == self.main_frame
                if is_block_document(url, main):
                    self.blocked = True
                return
            if response.status != 200:
                return
            if _PRODUCTS_FRAGMENT in url:
                payload = response.json()
                if isinstance(payload, dict):
                    self.products.append(payload)
            elif _MOVEMENTS_FRAGMENT in url:
                payload = response.json()
                if isinstance(payload, dict):
                    body = response.request.post_data_json
                    account_id = body.get("accountId") if isinstance(body, dict) else None
                    self.movements.append((account_id, payload))
        except Exception:
            pass

    def movements_for(self, account_id: str) -> Optional[dict]:
        """The latest movements payload whose request asked for `account_id`.

        Strict on purpose: a payload whose request body was unreadable, or
        that asked for another account (a fallback click can open one), is
        never taken for the vista's.
        """
        for requested, payload in reversed(self.movements):
            if requested == account_id:
                return payload
        return None

    def replay_headers(self) -> Optional[dict[str, str]]:
        """The captured headers a replay needs, or None if any is missing."""
        if not all(self.headers.get(name) for name in _REPLAY_HEADERS_REQUIRED):
            return None
        names = _REPLAY_HEADERS_REQUIRED + _REPLAY_HEADERS_OPTIONAL
        return {name: self.headers[name] for name in names if self.headers.get(name)}


def _raise_if_blocked(page, capture: SessionCapture) -> None:
    if capture.blocked or _BLOCK_FRAGMENT in (page.url or ""):
        raise SantanderBlockedError(
            "Santander's edge served its block page; not retrying this run"
        )


def _launch_browser(playwright, headless: bool):
    """Chromium via the full-binary "chromium" channel, else the default build."""
    try:
        return playwright.chromium.launch(headless=headless, channel="chromium")
    except Exception:
        logger.warning("Santander: chromium channel unavailable; using default build")
        return playwright.chromium.launch(headless=headless)


def _first_visible(locators, timeout_ms: int):
    """The first locator that becomes visible (the first one waits longest), or None."""
    for index, locator in enumerate(locators):
        try:
            locator.wait_for(state="visible", timeout=timeout_ms if index == 0 else 2_000)
            return locator
        except Exception:
            continue
    return None


def _login_frame(page):
    """The login iframe's Frame, or None while it isn't attached."""
    try:
        handle = page.locator("iframe#login-frame").element_handle(timeout=1_000)
        return handle.content_frame() if handle else None
    except Exception:
        return None


def _frame_text(frame) -> str:
    try:
        text = frame.evaluate(_INNER_TEXT_JS)
        return text if isinstance(text, str) else ""
    except Exception:
        return ""


def _login_iframe_text(page) -> str:
    frame = _login_frame(page)
    return _frame_text(frame) if frame is not None else ""


def _raise_step_up() -> None:
    raise SantanderStepUpError(
        "Santander asked for a second factor; the scraper cannot answer it"
    )


def _check_login_texts(page, baseline: str) -> None:
    """Raise on a step-up prompt or an error banner shown after submitting.

    * The login iframe: only markers absent from `baseline`, its text right
      before submit, count.
    * A `mibanco` page reached before the product list arrived: step-up
      markers other than Santander Pass (which the dashboard may advertise).
    * Any other non-public page: every marker.

    The public home itself is never read: it advertises Santander Pass.
    """
    after = _login_iframe_text(page)
    if after:
        if new_markers(baseline, after, _STEP_UP_MARKERS):
            _raise_step_up()
        if new_markers(baseline, after, _LOGIN_ERROR_MARKERS):
            raise SantanderLoginError("Santander rejected the RUT or password")
    url = page.url or ""
    if _PRIVATE_HOST in url:
        if find_marker(_frame_text(page), _PAGE_STEP_UP_MARKERS):
            _raise_step_up()
    elif _PUBLIC_HOST not in url:
        text = _frame_text(page)
        if find_marker(text, _STEP_UP_MARKERS):
            _raise_step_up()
        if find_marker(text, _LOGIN_ERROR_MARKERS):
            raise SantanderLoginError("Santander rejected the RUT or password")


def _login(page, capture: SessionCapture, rut: str, password: str) -> None:
    """The one login attempt of a run: public site, login iframe, private site."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
    _raise_if_blocked(page, capture)

    enter = _first_visible(
        (
            page.get_by_role("button", name="Ingresar al sitio privado").first,
            page.locator("#btnIngresar").first,
        ),
        _LOGIN_FORM_TIMEOUT_MS,
    )
    if enter is None:
        _raise_if_blocked(page, capture)
        raise SantanderLoginError("Santander login button did not appear")
    enter.click(timeout=_CLICK_TIMEOUT_MS)

    frame = page.frame_locator("iframe#login-frame")
    rut_box = frame.get_by_role("textbox", name="RUT")
    try:
        rut_box.wait_for(state="visible", timeout=_LOGIN_FORM_TIMEOUT_MS)
    except Exception:
        _raise_if_blocked(page, capture)
        raise SantanderLoginError("Santander login form did not load") from None
    try:
        rut_box.press_sequentially(rut, delay=_TYPE_DELAY_MS)
        frame.get_by_role("textbox", name="Clave").press_sequentially(
            password, delay=_TYPE_DELAY_MS
        )
        submit = frame.get_by_role("button", name="Ingresar", exact=True)
        if submit.count() == 0:
            submit = frame.get_by_role("button", name="Ingresar")
        baseline = _login_iframe_text(page)
        submit.first.click(timeout=_CLICK_TIMEOUT_MS)
    except Exception:
        # Playwright's message can quote the text being typed: drop it.
        raise SantanderLoginError("Santander login form could not be filled in") from None

    # Success is the dashboard's product list arriving (it is fired on its own
    # right after login), not a URL: a step-up screen can live on the private
    # host too.
    deadline = time.monotonic() + _LOGIN_RESULT_TIMEOUT_MS / 1000
    next_text_check = 0.0
    while True:
        _raise_if_blocked(page, capture)
        if capture.products:
            return
        now = time.monotonic()
        if now >= next_text_check:
            _check_login_texts(page, baseline)
            next_text_check = now + 1.0
        if now >= deadline:
            logger.warning(
                "Santander: login timed out on host %s (login form attached: %s)",
                urlparse(page.url or "").hostname,
                _login_frame(page) is not None,
            )
            if _PRIVATE_HOST in (page.url or ""):
                raise SantanderPayloadError(
                    "Santander dashboard did not load the product list"
                )
            raise SantanderLoginError("Santander login did not reach the private site")
        page.wait_for_timeout(_POLL_MS)


def _wait_for(page, capture: SessionCapture, ready, timeout_ms: int):
    """Poll `ready()` until it returns something, the budget ends, or a block.

    Playwright's sync event handlers only run while the caller is inside a
    wait, so the polling `wait_for_timeout` is what lets the capture fill.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        _raise_if_blocked(page, capture)
        value = ready()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(_POLL_MS)


def _open_movements_view(page) -> bool:
    """Click the vista's card on the dashboard, which makes the SPA load movements."""
    vista = re.compile("vista", re.I)
    card = _first_visible(
        (
            page.locator("#cuentas div.box-product").filter(has_text=vista).first,
            page.locator("lib-carousel .swiper-slide").filter(has_text=vista).first,
            page.get_by_role("region", name=re.compile(r"^\s*Cuentas"))
            .get_by_role("emphasis")
            .first,
            page.locator("#cuentas div.box-product").first,
        ),
        _CARD_TIMEOUT_MS,
    )
    if card is None:
        return False
    try:
        card.click(timeout=_CLICK_TIMEOUT_MS)
    except Exception:
        logger.debug("Santander: clicking the account card failed", exc_info=True)
        return False
    return True


def _replay_movements(
    page, capture: SessionCapture, account: SantanderAccount, today: date
) -> tuple[int, Optional[dict]]:
    """POST the transactions call from inside the page, as the SPA would.

    Only with headers the SPA itself sent in this session (nothing is ever
    hardcoded). Returns (status, payload); (0, None) when it could not try.
    """
    headers = capture.replay_headers()
    if headers is None:
        return 0, None
    headers = {
        **headers,
        "content-type": "application/json",
        "x-b3-spanid": secrets.token_hex(8),
    }
    opening = today - dt.timedelta(days=_REPLAY_WINDOW_DAYS)
    body = transactions_request_body(account, opening, today)
    try:
        result = page.evaluate(
            _POST_JSON_JS, {"url": MOVEMENTS_URL, "body": body, "headers": headers}
        )
    except Exception:
        logger.debug("Santander: movements replay failed", exc_info=True)
        return 0, None
    if not isinstance(result, dict):
        return 0, None
    payload = result.get("payload")
    return int(result.get("status") or 0), payload if isinstance(payload, dict) else None


def _read_movements(
    page, capture: SessionCapture, account: SantanderAccount, today: date
) -> Optional[dict]:
    """The vista's movements payload: the SPA's own call, else one replay."""
    if _open_movements_view(page):
        payload = _wait_for(
            page,
            capture,
            lambda: capture.movements_for(account.account_id),
            _MOVEMENTS_TIMEOUT_MS,
        )
        if payload is not None:
            return payload
        logger.warning("Santander: the movements view loaded no movements; replaying")
    else:
        logger.warning("Santander: account card not found; replaying the movements call")
    status, payload = _replay_movements(page, capture, account, today)
    if payload is None or status != 200:
        logger.warning(
            "Santander: movements replay unavailable (status %s, headers %s)",
            status or "none",
            "captured" if capture.replay_headers() else "missing",
        )
        return None
    return payload


def read_session(page, capture: SessionCapture, today: date) -> SantanderSessionResult:
    """Everything one authenticated page yields: the vista and its movements."""
    products_payload = _wait_for(
        page,
        capture,
        lambda: capture.products[-1] if capture.products else None,
        _PRODUCTS_TIMEOUT_MS,
    )
    if products_payload is None:
        raise SantanderPayloadError("Santander dashboard did not load the product list")
    parsed = parse_products(products_payload)
    if parsed.ignored:
        logger.info("Santander: ignoring %d non-vista or non-CLP products", parsed.ignored)
    warnings: list[str] = []
    if parsed.invalid:
        logger.warning("Santander: %d vista rows had no usable balance", parsed.invalid)
        warnings.append(f"Santander: {parsed.invalid} cuenta vista rows were unreadable")
    if not parsed.accounts:
        warnings.append("Santander: no CLP cuenta vista in the product list")
        return SantanderSessionResult(warnings=warnings)
    if len(parsed.accounts) > 1:
        warnings.append(
            f"Santander: {len(parsed.accounts)} CLP cuentas vista found; only the "
            "lowest-numbered one is read"
        )
    account = parsed.accounts[0]

    payload = _read_movements(page, capture, account, today)
    if payload is None:
        warnings.append("Santander: movements did not load")
        return SantanderSessionResult(account=account, warnings=warnings)
    movements = parse_movements(payload, account.currency)
    if movements.zero:
        logger.info("Santander: skipped %d zero-amount movements", movements.zero)
    if movements.skipped:
        logger.warning("Santander: skipped %d unusable movements", movements.skipped)
        warnings.append(
            f"Santander: {movements.skipped} movements skipped (no date or unreadable amount)"
        )
    if movements.sign_mismatches:
        logger.warning(
            "Santander: skipped %d movements whose sign disagreed with their flag",
            movements.sign_mismatches,
        )
        warnings.append(
            f"Santander: {movements.sign_mismatches} movements skipped (sign and "
            "debit/credit flag disagree)"
        )
    warnings.extend(
        _balance_unit_warnings(account.balance, latest_balance(movements.movements))
    )
    return SantanderSessionResult(
        account=account, movements=movements.movements, warnings=warnings
    )


def _balance_unit_warnings(balance: Decimal, running: Optional[Decimal]) -> list[str]:
    """Cross-check the implied-cents balance with the latest running balance.

    A gap of exactly a factor of 100 means one of the two cents readings is
    wrong: a run warning, so the run is partial. Any other gap (holds, a
    movement posted after the list was read) is only logged. Never a figure.
    """
    if running is None or running == balance:
        return []
    if balance != 0 and running in (balance * 100, balance / 100):
        logger.warning("Santander: balance and running balance differ by a factor of 100")
        return [
            "Santander balance unit looks inconsistent with the running balance; "
            "check the implied-cents reading"
        ]
    logger.warning(
        "Santander: available balance and latest running balance disagree; "
        "keeping the available balance"
    )
    return []


def _logout(page) -> None:
    """Best-effort "Cerrar sesión" so the bank's session doesn't linger.

    Never observed live; when the control isn't found or the click fails, the
    browser is closed anyway, which ends the session on our side.
    """
    pattern = re.compile(r"cerrar sesi", re.I)
    try:
        button = page.get_by_role("button", name=pattern).first
        if button.count() == 0:
            button = page.get_by_text(pattern).first
        if button.count() == 0:
            return
        button.click(timeout=_LOGOUT_TIMEOUT_MS)
        page.wait_for_timeout(1_500)
    except Exception:
        logger.debug("Santander: logout click failed; closing the browser", exc_info=True)


def _session_sync(rut: str, password: str, headless: bool) -> SantanderSessionResult:
    from playwright.sync_api import sync_playwright  # lazy: keeps tests browser-free

    capture = SessionCapture()
    with sync_playwright() as playwright:
        browser = None
        try:
            browser = _launch_browser(playwright, headless)
            context = browser.new_context(
                user_agent=user_agent_for(browser.version, sys.platform),
                viewport=VIEWPORT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
            )
            context.add_init_script(_WEBDRIVER_MASK_JS)
            page = context.new_page()
            capture.main_frame = page.main_frame
            page.on("request", capture.on_request)
            page.on("response", capture.on_response)
            _login(page, capture, rut, password)
            try:
                return read_session(page, capture, dt.datetime.now(_SANTIAGO).date())
            finally:
                _logout(page)
        except SantanderError:
            raise
        except Exception as exc:
            # A raw Playwright error can carry URLs and call logs; the run
            # records only its type. The traceback stays in the local log.
            logger.debug("Santander: session crashed", exc_info=True)
            raise SantanderError(
                f"Santander session failed ({type(exc).__name__})"
            ) from None
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.debug("Santander: closing the browser failed", exc_info=True)


async def fetch_session(
    rut: str, password: str, *, headless: bool = True
) -> SantanderSessionResult:
    """Log into Santander once and read the CLP cuenta vista and its movements.

    Runs the synchronous Playwright flow in a worker thread so it doesn't block
    the scheduler's event loop. Exactly one login attempt: any failure raises
    and is never retried here.
    """
    return await asyncio.to_thread(_session_sync, rut, password, headless)
