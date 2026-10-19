"""Banco Falabella: one headless login, data read from the DOM.

Banco Falabella's private site (`web2.bancofalabella.cl`) is a single-page app
whose balances show on the post-login dashboard product cards and whose card
movements render inside a `<credit-card-movements>` web component (shadow DOM).
The checking account and línea de crédito details are legacy server-rendered
views injected into the light DOM.
This module only drives the browser and hands the rendered text and table rows
to the pure parsers below, which are unit-tested with synthetic strings and
never touch a browser. `fetch_session` is the single mockable seam.

The data BFF (`/movement-cc/*`) encrypts its request bodies client-side, so the
figures come from the rendered page; the decision record is in
`institutions/banco_falabella.py`.

The browser half, in order:

  1. Open the public home and click "Mi Cuenta". A slide-over panel exposes the
     RUT and "Clave internet" (a six-digit PIN) inputs and an "Ingresar"
     button; both are typed key by key. Login is RUT + clave, no second factor.
  2. Success is the redirect to `web2.bancofalabella.cl` with a "MIS PRODUCTOS"
     marker, detected with a `wait_for_function`; `networkidle` is never used.
  3. A marketing campaign modal (whose backdrop intercepts clicks) is dismissed,
     then the dashboard product card is given time to populate its cupo figures.
     Balances come from that card's innerText (`balance_from_text`). Movements
     come from the card detail's shadow-DOM table (`movements_from_rows`): the
     unbilled "Ultimos movimientos" tab and the current billed "Movimientos
     facturados" statement. Iterating the month select for older statements is a
     deliberate follow-up (see `_read_billed`).
  4. The Cuenta Corriente and Línea de Crédito cards on the same dashboard give
     their balances (`checking_from_text` / `line_from_text`). Then, one
     dashboard reload each, the checking detail's movements table
     (`checking_movements_from_rows`) and the línea detail's "Tasa diaria"
     (`daily_rate_from_text`) are read. These run after the card so a failure
     there can never cost the card's surfaces.

Gotchas handled here:

  * Idle-timeout modal: after a short idle the site shows a "Tu sesion
    expirara" modal with a "Continuar" button and, if ignored, logs out.
    `_keep_alive` dismisses it (light and shadow DOM) before each surface read
    and before closing any other overlay.
  * Flaky movements BFF: the `/movement-cc/*` calls often return 500 and the
    UI shows a "Reintentar" button. A failed load is retried through it at
    once, a hung one after an escalating render budget, keeping the session
    alive between tries; a surface that never loads becomes a warning (partial
    coverage), not a failed run.
  * A failed or in-flight load looks empty: the component draws the same
    header-only table (with its "sin movimientos" art while reloading) as a
    tab that truly has no movements. `_MovementsFeed` watches those calls on
    the wire, and an empty read only counts after a `/movements` call answered
    2xx.

Every message raised from here is safe for `scraper_runs.error_message`: no
RUT, clave, cookies, URLs with ids, or figures.

Set `BANCO_FALABELLA_HEADLESS=0` to watch the browser while debugging.
"""

import asyncio
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


# --- Errors -------------------------------------------------------------------
#
# Every message is safe for `scraper_runs.error_message` (shown on the
# dashboard): no RUT, clave, token, card number, URL with ids or figures.


class BancoFalabellaError(RuntimeError):
    """A Banco Falabella session failed in a way that cost the whole leg."""


class BancoFalabellaLoginError(BancoFalabellaError):
    """The login was rejected or never reached the private site."""


# --- What a session yields ----------------------------------------------------


@dataclass(frozen=True)
class RawBalance:
    """The CMR Mastercard figures read off the dashboard product card.

    `limit` is the "Cupo de compras", `owed` the "Cupo utilizado" and
    `available` the "Cupo disponible" (the headline). Amounts are whole-peso
    Decimals; any field is None when its label was not found. `statement_day`
    and `due_day` are reserved for the card-detail billed header and not read
    yet, so they stay None.
    """

    name: Optional[str] = None
    last4: Optional[str] = None
    brand: Optional[str] = None
    limit: Optional[Decimal] = None
    owed: Optional[Decimal] = None
    available: Optional[Decimal] = None
    statement_day: Optional[int] = None
    due_day: Optional[int] = None


@dataclass(frozen=True)
class RawMovement:
    """One card movement, normalised from a rendered table row.

    `amount` is already signed: negative for a charge, positive for a payment,
    credit or refund (see `movements_from_rows`). It is the full "Monto total",
    not the monthly "Cuota a pagar", so a multi-installment purchase has the
    same amount in every month it shows up. `cuotas_current` / `cuotas_total`
    are the installment counter ("03/12"), kept for display only and never part
    of the identity (the portal changes the counter as a purchase is billed).
    """

    transaction_date: date
    description: str
    persona: str
    amount: Decimal
    currency: str
    cuotas_current: Optional[int] = None
    cuotas_total: Optional[int] = None


@dataclass(frozen=True)
class RawCheckingBalance:
    """The Cuenta Corriente figures read off its dashboard product card.

    `available` is the "Saldo disponible" (the headline) and `accounting` the
    "Saldo contable"; `account_number` is the number printed under the product
    name, kept for display only. Any field is None when it was not found.
    """

    account_number: Optional[str] = None
    available: Optional[Decimal] = None
    accounting: Optional[Decimal] = None


@dataclass(frozen=True)
class RawLineBalance:
    """The Línea de Crédito figures: its dashboard card plus its detail rate.

    `limit` is the "Cupo autorizado", `owed` the "Saldo utilizado" and
    `available` the "Saldo disponible" (the headline). `daily_rate_pct` is the
    detail page's "Tasa diaria" as a percent number (1.23 for "1.23 %"), None
    when the detail was not read.
    """

    limit: Optional[Decimal] = None
    owed: Optional[Decimal] = None
    available: Optional[Decimal] = None
    daily_rate_pct: Optional[Decimal] = None


@dataclass(frozen=True)
class RawCheckingMovement:
    """One Cuenta Corriente movement, normalised from a rendered table row.

    `amount` is signed: the "Abono" column is money in (positive), the "Cargo"
    column money out (negative). `balance` is the running "Saldo" after the
    movement, None when the cell was blank.
    """

    transaction_date: date
    description: str
    amount: Decimal
    currency: str
    balance: Optional[Decimal] = None


@dataclass(frozen=True)
class FalabellaSession:
    """What one Banco Falabella login produced.

    `balance` is the CMR Mastercard card (None when its available cupo did not
    parse) and `movements` its parsed movements across the unbilled and billed
    tabs. `checking` and `line` are the Cuenta Corriente and Línea de Crédito
    (None when the dashboard shows no such product or its available balance did
    not parse), and `checking_movements` the checking account's movements.
    `warnings` are the partial-coverage notes the scraper hands to
    `ProductScrapeResult.warnings`.
    """

    balance: Optional[RawBalance] = None
    movements: list[RawMovement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checking: Optional[RawCheckingBalance] = None
    line: Optional[RawLineBalance] = None
    checking_movements: list[RawCheckingMovement] = field(default_factory=list)


# --- Pure helpers (unit-tested; no browser) -----------------------------------

_CLP_SHAPE_RE = re.compile(r"^\d+(?:\.\d+)?$")
_DMY_RE = re.compile(r"(\d{1,2})([/-])(\d{1,2})\2(\d{4})")
_CUOTAS_RE = re.compile(r"^\s*(\d{1,3})\s*/\s*(\d{1,3})\s*$")
_SPACES_RE = re.compile(r"\s+")
_WHOLE = Decimal(1)

# The CMR card network words that become `attributes.brand`.
_BRANDS = ("Mastercard", "Visa", "American Express", "Amex")

# A movement whose description carries one of these WORDS is money in (a
# payment, abono, refund or reversal), so its amount flips positive. Matched on
# word boundaries against the accent-free, lower-cased description, so a
# merchant like "MERCADOPAGO*..." or "GRANULADOS ..." stays a charge. Seen
# live: a card payment ("PAGO TARJETA ...") is printed with a "-" ("-$..."),
# so the printed sign already marks it; the word set is the fallback for a
# credit printed unsigned, and only "pago" has been seen on a real row.
_PAYMENT_RE = re.compile(r"\b(?:pagos?|abonos?|devoluc\w*|revers\w*|anula\w*)\b")

# Dashboard product-card labels, matched accent- and case-insensitively. The
# amount is always the line BEFORE the label; the Avance cash-advance line
# ("Monto disponible") is deliberately not among them, so its figure is never
# read as a card balance.
_LABEL_LIMIT = "cupo de compras"
_LABEL_OWED = "cupo utilizado"
_LABEL_AVAILABLE = "cupo disponible"

# The Cuenta Corriente and Línea de Crédito dashboard cards follow the same
# amount-then-label layout. Their product names pick the right card among the
# accounts (`app-accounts`) and agreements (`app-agreements`) the page lists.
_CHECKING_NAME = "cuenta corriente"
_LINE_NAME = "linea de credito"
_LABEL_SALDO_DISPONIBLE = "saldo disponible"
_LABEL_SALDO_CONTABLE = "saldo contable"
_LABEL_CUPO_AUTORIZADO = "cupo autorizado"
_LABEL_SALDO_UTILIZADO = "saldo utilizado"

# The línea detail's "Tasa diaria" field, read off the accent-free, lower-cased
# page text (label and value render with no space between them).
_DAILY_RATE_RE = re.compile(r"tasa diaria\s*:?\s*(\d+(?:[.,]\d+)?)\s*%")
_PERCENT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*%?\s*$")


def _normalize(text: str) -> str:
    """Lower-cased, accent-free and single-spaced, for label matching."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACES_RE.sub(" ", stripped).strip().lower()


def parse_clp(raw: Any) -> Optional[Decimal]:
    """Parse a Chilean CLP amount into whole pesos, rounding half-up.

    Accepts both the portal's spaceless form ("$1.000.000") and the spaced one
    ("$ 2.500.000"), "$ 0", a "-" sign on either side of the "$", dot
    thousands separators and an optional "," decimal tail (rounded half-up,
    since CLP is whole and the envelope rejects a fraction). Uses `Decimal`
    throughout, never `float` or Python's banker's-rounding `round`. Returns
    None when no digit can be read.
    """
    if not isinstance(raw, str):
        return None
    compact = re.sub(r"[$\s]", "", raw)
    negative = compact.startswith("-")
    cleaned = compact.lstrip("-")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(".", "")
    if not _CLP_SHAPE_RE.fullmatch(cleaned):
        return None
    value = Decimal(cleaned).quantize(_WHOLE, rounding=ROUND_HALF_UP)
    return -value if negative else value


def parse_dmy(raw: Any) -> Optional[date]:
    """Parse a "DD/MM/YYYY" or "DD-MM-YYYY" date ("15/03/2025" -> 2025-03-15).

    The card tables print slashes and the checking table dashes; a mix of the
    two in one date is rejected. None otherwise.
    """
    if not isinstance(raw, str):
        return None
    match = _DMY_RE.search(raw)
    if match is None:
        return None
    day, month, year = (int(match.group(index)) for index in (1, 3, 4))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_cuotas(raw: Any) -> Optional[tuple[int, int]]:
    """Parse an installment counter ("01/01" -> (1, 1); "03/12" -> (3, 12))."""
    if not isinstance(raw, str):
        return None
    match = _CUOTAS_RE.match(raw)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def last4_from_masked(raw: Any) -> Optional[str]:
    """The last four digits of a masked card number ("* * * * 1234" / "**** 1234")."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"\D", "", raw)
    return digits[-4:] if len(digits) >= 4 else None


def _brand_from_name(name: Optional[str]) -> Optional[str]:
    """The card network word within a product name ("CMR Mastercard" -> "Mastercard")."""
    if not name:
        return None
    low = _normalize(name)
    for brand in _BRANDS:
        if _normalize(brand) in low:
            return brand
    return None


def _is_payment(description: str) -> bool:
    """Whether a description reads as money in rather than a charge."""
    return _PAYMENT_RE.search(_normalize(description)) is not None


def _lines(text: Optional[str]) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _amount_before(lines: list[str], label: str) -> Optional[Decimal]:
    """The CLP amount on the line before the first line equal to `label`."""
    for index, line in enumerate(lines):
        if index > 0 and _normalize(line) == label:
            return parse_clp(lines[index - 1])
    return None


def balance_from_text(text: Optional[str]) -> RawBalance:
    """Shape the dashboard product-card innerText into a `RawBalance`.

    The card renders a line stack (card name, masked number, then amount/label
    pairs). Each cupo figure is the line BEFORE its label, so the Avance
    "Monto disponible" line is never mistaken for a card balance. A label that
    is absent leaves its field None (a missing figure is tolerated).
    """
    lines = _lines(text)
    name: Optional[str] = None
    last4: Optional[str] = None
    for index, line in enumerate(lines):
        candidate = last4_from_masked(line)
        if candidate is not None and re.search(r"[•*]", line):
            last4 = candidate
            if index > 0:
                name = lines[index - 1]
            break
    return RawBalance(
        name=name,
        last4=last4,
        brand=_brand_from_name(name),
        limit=_amount_before(lines, _LABEL_LIMIT),
        owed=_amount_before(lines, _LABEL_OWED),
        available=_amount_before(lines, _LABEL_AVAILABLE),
    )


def movements_from_rows(
    rows: list[list[str]], section: str = ""
) -> list[RawMovement]:
    """Turn rendered body rows into `RawMovement`s; skip and log a bad row.

    Each row is the table's cells in order: Fecha de compras, Descripcion,
    Persona, Monto total, Cuotas, Cuota a pagar. The amount is the full
    "Monto total" (not the monthly "Cuota a pagar") and is signed here:
    negative for a charge, positive for a credit. A credit is a row the portal
    prints with a "-" (as card portals mark an abono, e.g. "$-999.999") or one
    whose description reads as a payment; a printed sign is never flipped back.
    A row without a usable date or amount is logged once (no figures) and
    dropped; the rest of the leg continues.
    """
    movements: list[RawMovement] = []
    skipped = 0
    for row in rows:
        if len(row) < 4:
            skipped += 1
            logger.warning("Banco Falabella: skipping a %s row with too few cells", section or "movement")
            continue
        transaction_date = parse_dmy(row[0])
        monto_total = parse_clp(row[3])
        if transaction_date is None or monto_total is None:
            skipped += 1
            logger.warning(
                "Banco Falabella: skipping an unparseable %s row", section or "movement"
            )
            continue
        description = _SPACES_RE.sub(" ", row[1].strip()) if len(row) > 1 else ""
        persona = row[2].strip() if len(row) > 2 else ""
        cuotas = parse_cuotas(row[4]) if len(row) > 4 else None
        is_credit = monto_total < 0 or _is_payment(description)
        amount = abs(monto_total) if is_credit else -monto_total
        movements.append(
            RawMovement(
                transaction_date=transaction_date,
                description=description,
                persona=persona,
                amount=amount,
                currency="CLP",
                cuotas_current=cuotas[0] if cuotas else None,
                cuotas_total=cuotas[1] if cuotas else None,
            )
        )
    if skipped:
        logger.warning("Banco Falabella: skipped %d unusable %s rows", skipped, section or "movement")
    return movements


def checking_from_text(text: Optional[str]) -> RawCheckingBalance:
    """Shape the Cuenta Corriente dashboard card's innerText.

    The card renders the product name, the account number, then amount/label
    pairs ("Saldo contable", "Saldo disponible"); each figure is the line
    BEFORE its label. The account number is the line after the name, kept
    only when it carries digits and is not an amount.
    """
    lines = _lines(text)
    account_number: Optional[str] = None
    for index, line in enumerate(lines[:-1]):
        if _normalize(line) == _CHECKING_NAME:
            candidate = lines[index + 1]
            if re.search(r"\d", candidate) and "$" not in candidate:
                account_number = _SPACES_RE.sub(" ", candidate)
            break
    return RawCheckingBalance(
        account_number=account_number,
        available=_amount_before(lines, _LABEL_SALDO_DISPONIBLE),
        accounting=_amount_before(lines, _LABEL_SALDO_CONTABLE),
    )


def line_from_text(text: Optional[str]) -> RawLineBalance:
    """Shape the Línea de Crédito dashboard card's innerText.

    Same amount-then-label layout as the other cards: "Cupo autorizado" is the
    limit, "Saldo utilizado" the amount owed and "Saldo disponible" the
    headline. The rate is not on the card (see `daily_rate_from_text`).
    """
    lines = _lines(text)
    return RawLineBalance(
        limit=_amount_before(lines, _LABEL_CUPO_AUTORIZADO),
        owed=_amount_before(lines, _LABEL_SALDO_UTILIZADO),
        available=_amount_before(lines, _LABEL_SALDO_DISPONIBLE),
    )


def parse_percent(raw: Any) -> Optional[Decimal]:
    """Parse a percent figure ("1.23 %", "1,23 %", "1.23") into a Decimal."""
    if not isinstance(raw, str):
        return None
    match = _PERCENT_RE.match(raw)
    if match is None:
        return None
    return Decimal(match.group(1).replace(",", "."))


def daily_rate_from_text(text: Optional[str]) -> Optional[Decimal]:
    """The "Tasa diaria" percent in the línea detail's text, or None.

    Only a figure followed by "%" right after the label counts, so the
    movements table's "Tasa diaria" column header is never read as the rate.
    """
    match = _DAILY_RATE_RE.search(_normalize(text or ""))
    if match is None:
        return None
    return Decimal(match.group(1).replace(",", "."))


def checking_movements_from_rows(rows: list[list[str]]) -> list[RawCheckingMovement]:
    """Turn the checking table's body rows into `RawCheckingMovement`s.

    Each row is the cells Fecha, Descripcion, Cargo, Abono, Saldo. Exactly one
    of Cargo (money out, made negative) and Abono (money in, positive) carries
    the amount; the other is blank. A single-cell row is the table's "no
    records" or filler row and is dropped silently. A row without a usable
    date, or with no amount or an amount in both columns, is logged once (no
    figures) and dropped; the rest of the leg continues.
    """
    movements: list[RawCheckingMovement] = []
    skipped = 0
    for row in rows:
        if len(row) < 4:
            if any(cell.strip() for cell in row) and len(row) > 1:
                skipped += 1
            continue
        transaction_date = parse_dmy(row[0])
        cargo = parse_clp(row[2])
        abono = parse_clp(row[3])
        if transaction_date is None or (cargo is None) == (abono is None):
            skipped += 1
            continue
        amount = abs(abono) if abono is not None else -abs(cargo)
        movements.append(
            RawCheckingMovement(
                transaction_date=transaction_date,
                description=_SPACES_RE.sub(" ", row[1].strip()),
                amount=amount,
                currency="CLP",
                balance=parse_clp(row[4]) if len(row) > 4 else None,
            )
        )
    if skipped:
        logger.warning("Banco Falabella: skipped %d unusable checking rows", skipped)
    return movements


# --- Browser half: one headless login per run ---------------------------------

HOME_URL = "https://www.bancofalabella.cl"
_PRIVATE_HOST = "web2.bancofalabella.cl"
_PRODUCTS_MARKER = "mis productos"

VIEWPORT = {"width": 1366, "height": 768}
LOCALE = "es-CL"
TIMEZONE_ID = "America/Santiago"

# A realistic desktop context (BanChile/Santander's ceiling, no further): a UA
# that drops headless Chromium's "HeadlessChrome" marker but keeps the real
# engine version and host OS, plus the `navigator.webdriver` mask.
_UA_PLATFORMS = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "linux": "X11; Linux x86_64",
}
_WEBDRIVER_MASK_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"

# Timeouts (ms). Every wait is bounded; `networkidle` is never used.
_GOTO_TIMEOUT_MS = 90_000
_LOGIN_FORM_TIMEOUT_MS = 30_000
_LOGIN_RESULT_TIMEOUT_MS = 90_000
_CARD_TIMEOUT_MS = 20_000
_CLICK_TIMEOUT_MS = 10_000
_TABLE_POLL_MS = 500
# The masked clave input drops characters typed faster than this.
_TYPE_DELAY_MS = 120

# Flaky movements BFF (see module docstring). A load the feed sees fail is
# retried at once and cheaply (about half of them fail live), so those retries
# are counted apart from the escalating budgets, which run out only when a load
# neither shows rows nor answers (hung, or failing outside `/movement-cc/`).
_RENDER_TIMEOUTS_MS = (12_000, 20_000, 30_000)
_FAILED_LOAD_RETRIES = 10
# Polls spent looking for the "Reintentar" button after a load stopped.
_RETRY_CLICK_POLLS = 4
# Hard cap on one surface, whatever mix of failed and hung loads it meets.
_SURFACE_TIMEOUT_MS = 120_000
# An empty read must hold this many polls, after its load answered, before a
# tab counts as empty (the table renders a beat after the response lands).
_EMPTY_CONFIRM_POLLS = 3

# The movements BFF calls (see `_MovementsFeed`): each tab load is a chain of
# `/movement-cc/*` calls that ends in `.../credit-card/movements`.
_MOVEMENT_CC_PATH = "/movement-cc/"
_MOVEMENTS_PATH_SUFFIX = "/movements"
_LOAD_STEP = "step"
_LOAD_DONE = "done"
_LOAD_FAILED = "failed"

# Tab labels (rendered as <label> elements inside the web component).
_UNBILLED_TAB = "Ultimos movimientos"
_BILLED_TAB = "Movimientos facturados"

# Matched against accent-free, lower-cased text. Kept conservative so marketing
# copy cannot trip them. UNVERIFIED: no rejection was seen live.
_LOGIN_ERROR_MARKERS = (
    "clave incorrecta",
    "rut o clave",
    "datos incorrectos",
    "clave bloqueada",
)

# The product card's innerText (light DOM). The cupo figures sit in sibling
# divs of `a.div-product` inside the credit-cards grid, so we read the whole
# grid container (not the anchor alone, which holds only the name and last4).
# Falls back to the body so a markup drift still yields something to anchor on.
_CARD_TEXT_JS = """
() => {
  const grid = document.querySelector('div.grid-container.grid-credit-cards');
  if (grid) return grid.innerText || '';
  const card = document.querySelector('a.div-product');
  if (card) {
    const container = card.closest('div.grid-container') || card.parentElement;
    return (container || card).innerText || '';
  }
  return document.body ? document.body.innerText : '';
}
"""

# Shared JS prelude: every element inside the <credit-card-movements> component
# (its light children and nested shadow roots), plus a visibility test. Movement
# reads stay inside the component, so marketing copy or other widgets elsewhere
# on the page can never pass for movements or for an empty state, and a hidden
# (inactive) tab panel is ignored. `document.querySelector` cannot see into
# shadow DOM, so the walk is required.
_MOVEMENTS_NODES_JS = """
  const movementsNodes = () => {
    const hosts = [];
    const findHosts = (root) => {
      try {
        root.querySelectorAll('credit-card-movements').forEach((h) => hosts.push(h));
        root.querySelectorAll('*').forEach((el) => { if (el.shadowRoot) findHosts(el.shadowRoot); });
      } catch (e) {}
    };
    findHosts(document);
    const nodes = [];
    const walk = (root) => {
      try {
        root.querySelectorAll('*').forEach((el) => {
          nodes.push(el);
          if (el.shadowRoot) walk(el.shadowRoot);
        });
      } catch (e) {}
    };
    hosts.forEach((h) => { if (h.shadowRoot) walk(h.shadowRoot); walk(h); });
    return nodes;
  };
  const isVisible = (el) => el.getClientRects().length > 0;
"""

# The visible movements table inside the component, each body row as an array
# of cell strings.
_MOVEMENTS_ROWS_JS = (
    "() => {"
    + _MOVEMENTS_NODES_JS
    + """
  const header = (t) => (t.innerText || '').toLowerCase();
  const table = movementsNodes().find((el) => el.tagName === 'TABLE' && isVisible(el)
    && /monto total|descripci/.test(header(el)));
  if (!table) return {found: false, rows: []};
  const rows = [];
  table.querySelectorAll('tr').forEach((tr) => {
    const cells = Array.from(tr.querySelectorAll('td')).map((td) => (td.innerText || '').trim());
    if (cells.length) rows.push(cells);
  });
  return {found: true, rows};
}"""
)

# Click a button/label whose trimmed text matches `needle`, deep-walked (the
# idle modal's "Continuar" and the "Reintentar" button can live in shadow DOM).
_CLICK_TEXT_JS = """
(needle) => {
  const want = needle.toLowerCase();
  const hit = (el) => (el.textContent || '').trim().toLowerCase() === want;
  const walk = (root) => {
    try {
      for (const el of root.querySelectorAll('button, [role=button], a, label')) {
        if (hit(el)) { el.click(); return true; }
      }
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot && walk(el.shadowRoot)) return true;
      }
    } catch (e) {}
    return false;
  };
  return walk(document);
}
"""

# Whether the idle-timeout modal is showing: the rendered light DOM first, then
# every nested shadow root.
_IDLE_MODAL_JS = """
() => {
  const needle = 'tu sesion expirar';
  const strip = (s) => (s || '').normalize('NFKD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
  if (strip(document.body ? document.body.innerText : '').includes(needle)) return true;
  const walk = (root) => {
    try {
      for (const el of root.querySelectorAll('*')) {
        if (!el.shadowRoot) continue;
        if (strip(el.shadowRoot.textContent).includes(needle) || walk(el.shadowRoot)) return true;
      }
    } catch (e) {}
    return false;
  };
  return walk(document);
}
"""

# After login a marketing campaign modal (app-marketing) drops a ".backdrop
# visible" shade over the dashboard that intercepts clicks on the card; clicking
# the shade closes it (dismisses an ad, accepts nothing). Only a shade inside
# app-marketing is clicked, never one nested in a link, button or form (a click
# bubbles to its ancestors). Deep-walked.
_DISMISS_OVERLAY_JS = """
() => {
  const within = (el, selector) => {
    for (let n = el; n; n = n.parentNode || n.host) {
      if (n.nodeType === 1 && n.matches(selector)) return true;
    }
    return false;
  };
  let acted = false;
  const walk = (root) => {
    try {
      for (const el of root.querySelectorAll('*')) {
        const cls = typeof el.className === 'string' ? el.className : '';
        if (/backdrop/i.test(cls) && /\\bvisible\\b/i.test(cls)
            && within(el, 'app-marketing') && !within(el, 'a, button, form')) {
          el.click();
          acted = true;
        }
      }
      for (const el of root.querySelectorAll('*')) { if (el.shadowRoot) walk(el.shadowRoot); }
    } catch (e) {}
  };
  walk(document);
  return acted;
}
"""

# Whether the card detail (its <credit-card-movements> component) has rendered.
_CARD_DETAIL_READY_JS = """
() => {
  const walk = (root) => {
    try {
      if (root.querySelector('credit-card-movements')) return true;
      for (const el of root.querySelectorAll('*')) { if (el.shadowRoot && walk(el.shadowRoot)) return true; }
    } catch (e) {}
    return false;
  };
  return walk(document);
}
"""

# Whether the movements component shows a visible "no movements" message. It
# also shows while a load is running, so it only means empty once the load
# answered (see `_read_surface`). Scoped to the component and to short, visible
# elements, so page copy cannot match.
_EMPTY_STATE_JS = (
    "() => {"
    + _MOVEMENTS_NODES_JS
    + """
  const needles = ['no tienes movimientos', 'sin movimientos', 'no hay movimientos',
                   'no registra movimientos'];
  const strip = (s) => (s || '').normalize('NFKD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
  return movementsNodes().some((el) => (el.textContent || '').length < 200 && isVisible(el)
    && needles.some((n) => strip(el.textContent).includes(n)));
}"""
)

# The movements component's visible "Reintentar" control (its load-error
# view): whether one shows, and clicking it. Never a hidden one in an inactive
# tab panel, nor one elsewhere on the page, whose reload would not be this tab's.
_RETRY_CONTROL_JS = (
    _MOVEMENTS_NODES_JS
    + """
  const retry = movementsNodes().find((el) => el.matches('button, [role=button], a, label')
    && isVisible(el) && (el.textContent || '').trim().toLowerCase() === 'reintentar');
"""
)
_RETRY_SHOWN_JS = "() => {" + _RETRY_CONTROL_JS + "  return Boolean(retry);\n}"
_CLICK_RETRY_JS = (
    "() => {" + _RETRY_CONTROL_JS + "  if (!retry) return false;\n  retry.click();\n  return true;\n}"
)


# The Cuenta Corriente and Línea de Crédito dashboard cards (light DOM): the
# innerText of each product grid inside the accounts and agreements components.
# The name in each text picks the product (see `_pick_grid`).
_PRODUCT_GRIDS_JS = """
() => {
  const texts = (selector) => Array.from(document.querySelectorAll(selector))
    .map((grid) => grid.innerText || '');
  return {
    accounts: texts('app-accounts div.grid-container'),
    agreements: texts('app-agreements div.grid-container'),
    hasAccounts: Boolean(document.querySelector('app-accounts')),
    hasAgreements: Boolean(document.querySelector('app-agreements')),
  };
}
"""

# The checking and línea details are legacy server-rendered views
# (`viewIntegration/getViewContent/...`) injected into the light DOM: no shadow
# DOM, and their responses are HTML, so the decrypted-JSON hook sees nothing.
# Their load-failure view prints this message.
_LEGACY_ERROR_MARKER = "en estos momentos no lo podemos atender"

# The checking movements table (`ctbListMovbfch`), else the table whose OWN
# header row reads Fecha / Cargo / Abono / Saldo (the page nests its layout in
# outer tables, so a descendant header must not count). Each body row as an
# array of cell strings; `failed` when the legacy error view shows.
_CHECKING_TABLE_JS = """
(errorMarker) => {
  const strip = (s) => (s || '').normalize('NFKD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
  const failed = strip(document.body ? document.body.innerText : '').includes(errorMarker);
  const ownHeader = (t) => (t.tHead ? strip(t.tHead.innerText) : '');
  const table = document.querySelector('table#ctbListMovbfch')
    || Array.from(document.querySelectorAll('table')).find((t) => {
      const h = ownHeader(t);
      return h.includes('fecha') && h.includes('cargo') && h.includes('abono') && h.includes('saldo');
    });
  if (!table) return {found: false, failed, rows: []};
  const rows = [];
  Array.from(table.tBodies).forEach((body) => Array.from(body.rows).forEach((tr) => {
    const cells = Array.from(tr.cells).filter((td) => td.tagName === 'TD').map((td) => (td.innerText || '').trim());
    if (cells.length) rows.push(cells);
  }));
  return {found: true, failed, rows};
}
"""

# The línea detail's "Tasa diaria" field (`textFieldTasaDiariaLinea`), plus the
# page text to fall back on when the field id drifts.
_LINE_RATE_JS = """
() => {
  const field = document.querySelector('#textFieldTasaDiariaLinea');
  return {
    rate: field ? (field.innerText || field.textContent || '').trim() : null,
    text: document.body ? document.body.innerText : '',
  };
}
"""

# How long a legacy detail view gets to render before it counts as not loaded.
_LEGACY_VIEW_TIMEOUT_MS = 30_000
# How long the dashboard gets to render the checking and línea cards once the
# credit card's figures are in (they come from an earlier call, so this is a
# backstop, spent in full only by a client without those products).
_OTHER_PRODUCTS_TIMEOUT_MS = 10_000
# Once one of the two cards is complete, how long the other gets to turn up.
_OTHER_PRODUCTS_SETTLE_MS = 2_000
# The dashboard links' product names (accents optional).
_CHECKING_LINK_RE = r"cuenta\s+corriente"
_LINE_LINK_RE = r"l[ií]nea\s+de\s+cr[eé]dito"


def _find_marker(text: str, markers: tuple[str, ...]) -> Optional[str]:
    normalized = _normalize(text)
    return next((marker for marker in markers if marker in normalized), None)


def user_agent_for(browser_version: str, platform: str) -> str:
    """A desktop Chrome UA for the engine actually running, minus "Headless"."""
    major = browser_version.split(".")[0]
    os_token = _UA_PLATFORMS.get(platform, _UA_PLATFORMS["linux"])
    return (
        f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _launch_browser(playwright, headless: bool):
    """Chromium via the full-binary "chromium" channel, else the default build."""
    try:
        return playwright.chromium.launch(headless=headless, channel="chromium")
    except Exception:
        logger.warning("Banco Falabella: chromium channel unavailable; using default build")
        return playwright.chromium.launch(headless=headless)


def _first_visible(locators, timeout_ms: int):
    """The first locator that becomes visible (the first waits longest), or None."""
    for index, locator in enumerate(locators):
        try:
            locator.wait_for(state="visible", timeout=timeout_ms if index == 0 else 2_000)
            return locator
        except Exception:
            continue
    return None


def _keep_alive(page) -> None:
    """Dismiss the idle-timeout modal ("Tu sesion expirara" -> "Continuar")."""
    try:
        if not page.evaluate(_IDLE_MODAL_JS):
            return
    except Exception:
        return
    try:
        button = page.get_by_role("button", name=re.compile(r"continuar", re.I)).first
        if button.count() > 0:
            button.click(timeout=_CLICK_TIMEOUT_MS)
            return
    except Exception:
        logger.debug("Banco Falabella: keep-alive locator click failed", exc_info=True)
    try:
        page.evaluate(_CLICK_TEXT_JS, "Continuar")
    except Exception:
        logger.debug("Banco Falabella: keep-alive JS click failed", exc_info=True)


def _dismiss_overlays(page) -> None:
    """Close post-login marketing overlays whose backdrop intercepts clicks.

    The idle modal is answered first ("Continuar"), so it is never closed by its
    backdrop instead. Retried a few times since a fresh context can stack more
    than one.
    """
    _keep_alive(page)
    for _ in range(3):
        try:
            if not page.evaluate(_DISMISS_OVERLAY_JS):
                return
        except Exception:
            return
        page.wait_for_timeout(500)


def _wait_for_card_text(page) -> str:
    """Poll the product card until its cupo figures have loaded, or time out.

    The card renders name and last4 first; the cupo amounts (in sibling divs)
    arrive a moment later, so reading too early misses the balances.
    """
    deadline = time.monotonic() + _CARD_TIMEOUT_MS / 1000
    text = ""
    while time.monotonic() < deadline:
        text = _read_card_text(page)
        if _LABEL_AVAILABLE in _normalize(text):
            return text
        page.wait_for_timeout(_TABLE_POLL_MS)
    return text


def _open_login_panel(page) -> None:
    """Click the "Mi Cuenta" button that opens the login slide-over."""
    button = _first_visible(
        (
            page.get_by_role("button", name=re.compile(r"mi cuenta", re.I)).first,
            page.get_by_role("link", name=re.compile(r"mi cuenta", re.I)).first,
            page.get_by_text(re.compile(r"mi cuenta", re.I)).first,
        ),
        _LOGIN_FORM_TIMEOUT_MS,
    )
    if button is None:
        raise BancoFalabellaLoginError("Banco Falabella login button did not appear")
    button.click(timeout=_CLICK_TIMEOUT_MS)


def _login(page, rut: str, password: str) -> None:
    """The one login attempt of a run: public home, slide-over, private site."""
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
    _open_login_panel(page)

    rut_box = _first_visible(
        (
            page.get_by_role("textbox", name=re.compile(r"rut", re.I)).first,
            page.locator('input[placeholder*="RUT" i]').first,
            page.locator('input[name*="rut" i]').first,
        ),
        _LOGIN_FORM_TIMEOUT_MS,
    )
    clave_box = _first_visible(
        (
            page.get_by_role("textbox", name=re.compile(r"clave", re.I)).first,
            page.locator('input[type="password"]').first,
            page.locator('input[placeholder*="dígitos" i]').first,
        ),
        _LOGIN_FORM_TIMEOUT_MS,
    )
    if rut_box is None or clave_box is None:
        raise BancoFalabellaLoginError("Banco Falabella login form did not load")

    try:
        rut_box.press_sequentially(rut, delay=_TYPE_DELAY_MS)
        clave_box.press_sequentially(password, delay=_TYPE_DELAY_MS)
        submit = page.get_by_role("button", name=re.compile(r"ingresar", re.I)).first
        submit.click(timeout=_CLICK_TIMEOUT_MS)
    except Exception:
        # Playwright's message can quote the text being typed: drop it.
        raise BancoFalabellaLoginError(
            "Banco Falabella login form could not be filled in"
        ) from None

    try:
        page.wait_for_function(
            "() => location.host.includes('%s') && /%s/i.test(document.body ? document.body.innerText : '')"
            % (_PRIVATE_HOST, _PRODUCTS_MARKER),
            timeout=_LOGIN_RESULT_TIMEOUT_MS,
        )
    except Exception:
        marker = _find_marker(_body_text(page), _LOGIN_ERROR_MARKERS)
        if marker is not None:
            raise BancoFalabellaLoginError(
                "Banco Falabella rejected the RUT or clave"
            ) from None
        raise BancoFalabellaLoginError(
            "Banco Falabella login did not reach the private site"
        ) from None
    logger.info("Banco Falabella login successful")


def _body_text(page) -> str:
    try:
        text = page.evaluate("() => (document.body ? document.body.innerText : '')")
        return text if isinstance(text, str) else ""
    except Exception:
        return ""


def _read_card_text(page) -> str:
    try:
        text = page.evaluate(_CARD_TEXT_JS)
        return text if isinstance(text, str) else ""
    except Exception:
        logger.debug("Banco Falabella: reading the product card failed", exc_info=True)
        return ""


def _click_text(page, needle: str) -> bool:
    """Click a control by its exact visible text (pierces open shadow DOM)."""
    try:
        locator = page.get_by_text(re.compile(r"^\s*%s\s*$" % re.escape(needle), re.I)).first
        if locator.count() > 0:
            locator.click(timeout=_CLICK_TIMEOUT_MS)
            return True
    except Exception:
        logger.debug("Banco Falabella: locator click for %s failed", needle, exc_info=True)
    try:
        return bool(page.evaluate(_CLICK_TEXT_JS, needle))
    except Exception:
        return False


def _open_card_detail(page) -> bool:
    """Open the CMR card detail (its movements component) from the dashboard."""
    _dismiss_overlays(page)
    try:
        # The card in the credit-cards grid the balances came from, so another
        # product listed first is never opened instead.
        card = page.locator("div.grid-container.grid-credit-cards a.div-product").first
        if card.count() == 0:
            card = page.locator("a.div-product").first
        card.wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
    except Exception:
        logger.debug("Banco Falabella: the card never became visible", exc_info=True)
        return False
    for _attempt in range(2):
        try:
            card.click(timeout=_CLICK_TIMEOUT_MS)
        except Exception:
            logger.debug("Banco Falabella: the card click was blocked; dismissing overlays", exc_info=True)
            _dismiss_overlays(page)
            continue
        deadline = time.monotonic() + _CARD_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            try:
                if page.evaluate(_CARD_DETAIL_READY_JS):
                    return True
            except Exception:
                pass
            page.wait_for_timeout(_TABLE_POLL_MS)
        _dismiss_overlays(page)
    return False


def _read_table_rows(page) -> Optional[list[list[str]]]:
    """Read the movements table once; None when it is not present yet."""
    try:
        result = page.evaluate(_MOVEMENTS_ROWS_JS)
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("found"):
        return None
    rows = result.get("rows")
    return rows if isinstance(rows, list) else None


def _shows_empty_state(page) -> bool:
    try:
        return bool(page.evaluate(_EMPTY_STATE_JS))
    except Exception:
        return False


def _shows_retry(page) -> bool:
    try:
        return bool(page.evaluate(_RETRY_SHOWN_JS))
    except Exception:
        return False


class _MovementsFeed:
    """The movements component's BFF calls, as seen on the wire.

    A call settles when it finishes, not at its status line (a body can still
    fail after a 2xx). `state(since)` reads the calls started after a `mark()`:
    `_LOAD_DONE` when the last one settled was a 2xx `/movements` call,
    `_LOAD_FAILED` when it failed, and None while one is still pending or only
    an earlier step of the chain has settled. "Started" is the order the events
    reach Python, which is why every mark is taken before the click that starts
    a load.
    """

    def __init__(self) -> None:
        self._started = 0
        # request -> [start sequence, response status once known]
        self._pending: dict[Any, list] = {}
        self._settled: list[tuple[int, str]] = []

    def attach(self, page) -> None:
        page.on("request", self.on_request)
        page.on("response", self.on_response)
        page.on("requestfinished", self.on_request_finished)
        page.on("requestfailed", self.on_request_failed)

    def on_request(self, request) -> None:
        if request.method == "OPTIONS" or _MOVEMENT_CC_PATH not in urlsplit(request.url).path:
            return
        self._pending[request] = [self._started, None]
        self._started += 1

    def on_response(self, response) -> None:
        entry = self._pending.get(response.request)
        if entry is not None:
            entry[1] = response.status

    def on_request_finished(self, request) -> None:
        entry = self._pending.pop(request, None)
        if entry is None:
            return
        sequence, status = entry
        if status is None or status >= 400:
            outcome = _LOAD_FAILED
        elif 200 <= status < 300 and urlsplit(request.url).path.rstrip("/").endswith(
            _MOVEMENTS_PATH_SUFFIX
        ):
            outcome = _LOAD_DONE
        else:
            outcome = _LOAD_STEP
        self._settled.append((sequence, outcome))

    def on_request_failed(self, request) -> None:
        entry = self._pending.pop(request, None)
        if entry is not None:
            self._settled.append((entry[0], _LOAD_FAILED))

    def mark(self) -> int:
        """A point for `state`: only calls started after it count."""
        return self._started

    def state(self, since: int) -> Optional[str]:
        if any(sequence >= since for sequence, _ in self._pending.values()):
            return None
        settled = [outcome for sequence, outcome in self._settled if sequence >= since]
        if not settled or settled[-1] == _LOAD_STEP:
            return None
        return settled[-1]


def _click_retry(page) -> bool:
    """Click the component's "Reintentar", giving the error view a few polls to render."""
    for _ in range(_RETRY_CLICK_POLLS):
        try:
            clicked = bool(page.evaluate(_CLICK_RETRY_JS))
        except Exception:
            clicked = False
        if clicked:
            _keep_alive(page)
            return True
        page.wait_for_timeout(_TABLE_POLL_MS)
    logger.debug("Banco Falabella: no Reintentar control to click")
    return False


def _read_surface(
    page,
    label: str,
    feed: _MovementsFeed,
    previous: Optional[list[list[str]]] = None,
    since: Optional[int] = None,
) -> Optional[list[list[str]]]:
    """Open a movements tab and read its rows, retrying failed or hung loads.

    Returns the body rows, `[]` for a confirmed-empty tab, or None when the
    surface never loaded (the caller turns a None into a partial-coverage
    warning). An empty read (a header-only table or the component's "no
    movements" message) only counts once the tab's load answered (`feed`, from
    the caller's `since` mark, else from the tab click), no "Reintentar" shows,
    and the read holds for `_EMPTY_CONFIRM_POLLS` polls. A load that failed and
    shows the error view is retried through its "Reintentar" at once, up to
    `_FAILED_LOAD_RETRIES` times; a load that shows nothing within its budget is
    retried with the next, longer one; the whole surface gives up after
    `_SURFACE_TIMEOUT_MS`. Rows equal to `previous` (the other tab's, still on
    screen right after the switch) are ignored, so a slow surface is filed
    neither as empty nor as the wrong tab.
    """
    if since is None:
        since = feed.mark()
    _click_text(page, label)
    surface_deadline = time.monotonic() + _SURFACE_TIMEOUT_MS / 1000
    budget = 0
    failures = 0
    while True:
        _keep_alive(page)
        deadline = min(time.monotonic() + _RENDER_TIMEOUTS_MS[budget] / 1000, surface_deadline)
        empty_polls = 0
        failed = False
        while time.monotonic() < deadline:
            rows = _read_table_rows(page)
            if rows and rows != previous:
                logger.info("Banco Falabella: %s surface read, %d rows", label, len(rows))
                return rows
            state = feed.state(since)
            looks_empty = state == _LOAD_DONE and (
                rows == [] or (rows is None and _shows_empty_state(page))
            )
            # A failure (or an error view behind a 2xx) counts once the error
            # view shows: the component rides out some failed steps (a failed
            # projected-billing call) and goes on to call `/movements`.
            if (state == _LOAD_FAILED or looks_empty) and _shows_retry(page):
                failed = True
                break
            empty_polls = empty_polls + 1 if looks_empty else 0
            if empty_polls >= _EMPTY_CONFIRM_POLLS:
                logger.info("Banco Falabella: %s surface is empty", label)
                return []
            page.wait_for_timeout(_TABLE_POLL_MS)
        if time.monotonic() >= surface_deadline:
            return None
        if failed:
            failures += 1
            if failures > _FAILED_LOAD_RETRIES:
                return None
            logger.debug("Banco Falabella: %s load failed; retrying", label)
        else:
            budget += 1
            if budget >= len(_RENDER_TIMEOUTS_MS):
                return None
        retry_since = feed.mark()
        # Without a click no new load starts, so the current one still counts.
        if _click_retry(page):
            since = retry_since


def _read_billed(
    page,
    feed: _MovementsFeed,
    warnings: list[str],
    previous: Optional[list[list[str]]] = None,
) -> list[RawMovement]:
    """Read the current billed statement (the "Movimientos facturados" tab).

    Only the statement showing by default (the most recent) is read. Iterating
    the month <select> for older statements is a deliberate follow-up: on this
    flaky component it was unreliable (options load progressively, values are ISO
    timestamps, and a switch can read off the previous month's still-visible
    table), and a daily run already captures every movement while it is unbilled
    and again while it is the current statement. `previous` is the unbilled
    tab's rows, ignored if still on screen right after the switch.
    """
    rows = _read_surface(page, _BILLED_TAB, feed, previous)
    if rows is None:
        warnings.append("Banco Falabella: billed movements surface never loaded")
        return []
    return movements_from_rows(rows, "billed")


def read_session(page) -> FalabellaSession:
    """Everything one authenticated page yields: every product and its movements.

    The CMR card is read first, exactly as when it was the only product: its
    dashboard figures, then its detail's movements. Only then does the session
    reload the dashboard for the checking and línea cards and open their
    details. Each of those reads is guarded: any error there (Playwright's
    included) becomes a figure-free warning, so it can cost those surfaces but
    never the card's.
    """
    warnings: list[str] = []
    home_url = page.url
    feed = _MovementsFeed()
    feed.attach(page)
    _keep_alive(page)
    _dismiss_overlays(page)

    balance = balance_from_text(_wait_for_card_text(page))
    if balance.available is None:
        logger.warning("Banco Falabella: the card available cupo did not parse")
        warnings.append("Banco Falabella: the card available cupo did not parse")
        balance = None
    movements = _read_card_movements(page, feed, warnings)

    checking, line = _guarded(
        lambda: _read_dashboard_products(page, home_url, warnings),
        warnings,
        "the checking and line of credit cards",
        (None, None),
    )
    checking_movements: list[RawCheckingMovement] = []
    if checking is not None:
        # The dashboard is still showing: the checking detail needs no reload.
        checking_movements = _guarded(
            lambda: _read_checking(page, warnings),
            warnings,
            "the checking account detail",
            [],
        )
    if line is not None:
        line = _guarded(
            lambda: _read_line(page, home_url, line, warnings),
            warnings,
            "the line of credit detail",
            line,
        )
    return FalabellaSession(
        balance=balance,
        movements=movements,
        warnings=warnings,
        checking=checking,
        line=line,
        checking_movements=checking_movements,
    )


def _guarded(read, warnings: list[str], what: str, fallback):
    """Run one non-card read; any error becomes a warning and `fallback`.

    The error itself (which can carry URLs or page text) goes to the debug log
    only; the warning names the surface and the error type.
    """
    try:
        return read()
    except Exception as exc:
        logger.debug("Banco Falabella: reading %s failed", what, exc_info=True)
        warnings.append(f"Banco Falabella: reading {what} failed ({type(exc).__name__})")
        return fallback


def _read_dashboard_products(
    page, home_url: str, warnings: list[str]
) -> tuple[Optional[RawCheckingBalance], Optional[RawLineBalance]]:
    """Reload the dashboard and read the checking and línea cards off it."""
    if not _return_home(page, home_url):
        warnings.append("Banco Falabella: the dashboard did not come back for the other products")
        return None, None
    checking, line = _read_other_products(page, warnings)
    if checking is not None and checking.available is None:
        warnings.append("Banco Falabella: the checking available balance did not parse")
        checking = None
    if line is not None and line.available is None:
        warnings.append("Banco Falabella: the line of credit available balance did not parse")
        line = None
    return checking, line


def _read_card_movements(page, feed: _MovementsFeed, warnings: list[str]) -> list[RawMovement]:
    """The CMR card detail's unbilled and current billed movements."""
    # The unbilled tab loads as the card detail opens, before it is clicked.
    since = feed.mark()
    if not _open_card_detail(page):
        warnings.append("Banco Falabella: could not open the card detail")
        return []

    movements: list[RawMovement] = []
    unbilled = _read_surface(page, _UNBILLED_TAB, feed, since=since)
    if unbilled is None:
        warnings.append("Banco Falabella: unbilled movements surface never loaded")
    else:
        movements.extend(movements_from_rows(unbilled, "unbilled"))

    movements.extend(_read_billed(page, feed, warnings, previous=unbilled))
    return movements


def _pick_grid(texts: Any, name: str) -> Optional[str]:
    """The first product-grid text whose product name is `name`, else None."""
    if not isinstance(texts, list):
        return None
    for text in texts:
        if isinstance(text, str) and any(_normalize(line) == name for line in _lines(text)):
            return text
    return None


def _read_other_products(
    page, warnings: list[str]
) -> tuple[Optional[RawCheckingBalance], Optional[RawLineBalance]]:
    """The Cuenta Corriente and Línea de Crédito dashboard cards, when listed.

    Polls until both cards have their available balance, or, when only one
    does, until it has held for `_OTHER_PRODUCTS_SETTLE_MS` with no second card
    turning up (the two components render independently, so the first one
    complete is no sign the other is absent). Gives up at the backstop. A
    product the page never lists is None; when its component is on the page but
    never rendered a card, that is a warning, not a silent absence.
    """
    deadline = time.monotonic() + _OTHER_PRODUCTS_TIMEOUT_MS / 1000
    settle_until: Optional[float] = None
    while True:
        try:
            grids = page.evaluate(_PRODUCT_GRIDS_JS)
        except Exception:
            grids = {}
        if not isinstance(grids, dict):
            grids = {}
        checking_text = _pick_grid(grids.get("accounts"), _CHECKING_NAME)
        line_text = _pick_grid(grids.get("agreements"), _LINE_NAME)
        checking = checking_from_text(checking_text) if checking_text else None
        line = line_from_text(line_text) if line_text else None
        listed = [product for product in (checking, line) if product is not None]
        complete = bool(listed) and all(product.available is not None for product in listed)
        now = time.monotonic()
        if complete and len(listed) == 2:
            return checking, line
        if complete:
            settle_until = settle_until or now + _OTHER_PRODUCTS_SETTLE_MS / 1000
            if now >= settle_until:
                break
        else:
            settle_until = None
        if now >= deadline:
            break
        page.wait_for_timeout(_TABLE_POLL_MS)
    for product, present, cards, label in (
        (checking, "hasAccounts", "accounts", "checking account"),
        (line, "hasAgreements", "agreements", "line of credit"),
    ):
        if product is None and grids.get(present) and not grids.get(cards):
            warnings.append(f"Banco Falabella: the {label} card never rendered")
    return checking, line


def _return_home(page, home_url: str) -> bool:
    """Reload the private dashboard (the SPA keeps one URL for every view)."""
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        page.wait_for_function(
            "() => /%s/i.test(document.body ? document.body.innerText : '')" % _PRODUCTS_MARKER,
            timeout=_LOGIN_RESULT_TIMEOUT_MS,
        )
    except Exception:
        logger.debug("Banco Falabella: the dashboard did not come back", exc_info=True)
        return False
    _keep_alive(page)
    _dismiss_overlays(page)
    return True


def _open_product(page, component: str, name: str) -> bool:
    """Click the dashboard product link named `name` inside `component`."""
    _dismiss_overlays(page)
    try:
        link = (
            page.locator(f"{component} a.div-product")
            .filter(has_text=re.compile(name, re.I))
            .first
        )
        link.wait_for(state="visible", timeout=_CARD_TIMEOUT_MS)
    except Exception:
        logger.debug("Banco Falabella: the %s link never became visible", component, exc_info=True)
        return False
    for _attempt in range(2):
        try:
            link.click(timeout=_CLICK_TIMEOUT_MS)
            return True
        except Exception:
            logger.debug("Banco Falabella: the %s click was blocked; dismissing overlays", component, exc_info=True)
            _dismiss_overlays(page)
    return False


def _read_checking_rows(page) -> Optional[list[list[str]]]:
    """Wait for the checking detail's movements table; None if it never loads.

    The legacy view arrives as one server-rendered response, so the table has
    its rows as soon as it exists. `[]` is a table without movement rows. The
    error view counts only when no table rendered: the page can show both.
    """
    deadline = time.monotonic() + _LEGACY_VIEW_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        _keep_alive(page)
        try:
            result = page.evaluate(_CHECKING_TABLE_JS, _LEGACY_ERROR_MARKER)
        except Exception:
            result = None
        if isinstance(result, dict):
            rows = result.get("rows")
            if result.get("found") and isinstance(rows, list):
                logger.info("Banco Falabella: checking surface read, %d rows", len(rows))
                return rows
            if result.get("failed"):
                logger.info("Banco Falabella: the checking view answered with its error page")
                return None
        page.wait_for_timeout(_TABLE_POLL_MS)
    return None


def _read_line_rate(page) -> Optional[Decimal]:
    """Wait for the línea detail's "Tasa diaria"; None if it never shows."""
    deadline = time.monotonic() + _LEGACY_VIEW_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        _keep_alive(page)
        try:
            result = page.evaluate(_LINE_RATE_JS)
        except Exception:
            result = None
        if isinstance(result, dict):
            rate = parse_percent(result.get("rate"))
            if rate is None:
                rate = daily_rate_from_text(result.get("text"))
            if rate is not None:
                return rate
        page.wait_for_timeout(_TABLE_POLL_MS)
    return None


def _read_checking(page, warnings: list[str]) -> list[RawCheckingMovement]:
    """The checking account's latest movements (the detail's default view).

    Only the default "Últimos movimientos" view is read; the month select for
    older periods is not iterated (a daily run sees each movement there).
    Expects the dashboard to be showing.
    """
    if not _open_product(page, "app-accounts", _CHECKING_LINK_RE):
        warnings.append("Banco Falabella: could not open the checking account detail")
        return []
    rows = _read_checking_rows(page)
    if rows is None:
        warnings.append("Banco Falabella: checking movements surface never loaded")
        return []
    return checking_movements_from_rows(rows)


def _read_line(page, home_url: str, line: RawLineBalance, warnings: list[str]) -> RawLineBalance:
    """The línea card enriched with its detail's daily rate, when it loads.

    The línea movements table is not read: its view errored live and it lists
    draws and repayments that also show on the checking account (a draw is a
    "traspaso" into it), so the checking leg already carries that money.
    """
    if not (_return_home(page, home_url) and _open_product(page, "app-agreements", _LINE_LINK_RE)):
        warnings.append("Banco Falabella: could not open the line of credit detail")
        return line
    rate = _read_line_rate(page)
    if rate is None:
        warnings.append("Banco Falabella: the line of credit daily rate did not parse")
        return line
    return replace(line, daily_rate_pct=rate)


def _logout(page) -> None:
    """Best-effort "Cerrar sesion" so the bank's session does not linger."""
    try:
        _click_text(page, "Cerrar sesión")
        page.wait_for_timeout(1_000)
    except Exception:
        logger.debug("Banco Falabella: logout click failed; closing the browser", exc_info=True)


def _session_sync(rut: str, password: str, headless: bool) -> FalabellaSession:
    from playwright.sync_api import sync_playwright  # lazy: keeps tests browser-free

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
            _login(page, rut, password)
            try:
                return read_session(page)
            finally:
                _logout(page)
        except BancoFalabellaError:
            raise
        except Exception as exc:
            # A raw Playwright error can carry URLs and call logs; the run
            # records only its type. The traceback stays in the local log.
            logger.debug("Banco Falabella: session crashed", exc_info=True)
            raise BancoFalabellaError(
                f"Banco Falabella session failed ({type(exc).__name__})"
            ) from None
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.debug("Banco Falabella: closing the browser failed", exc_info=True)


async def fetch_session(
    rut: str, password: str, *, headless: bool = True
) -> FalabellaSession:
    """Log into Banco Falabella once and read the CMR card and its movements.

    Runs the synchronous Playwright flow in a worker thread so it does not block
    the scheduler's event loop. Exactly one login attempt: any failure raises
    and is never retried here.
    """
    return await asyncio.to_thread(_session_sync, rut, password, headless)
