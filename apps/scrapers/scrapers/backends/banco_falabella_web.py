"""Banco Falabella (CMR Mastercard): one headless login, data read from the DOM.

Banco Falabella's private site (`web2.bancofalabella.cl`) is a single-page app
whose balances show on the post-login dashboard product card and whose card
movements render inside a `<credit-card-movements>` web component (shadow DOM).
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

Gotchas handled here:

  * Idle-timeout modal: after a short idle the site shows a "Tu sesion
    expirara" modal with a "Continuar" button and, if ignored, logs out.
    `_keep_alive` dismisses it (light and shadow DOM) before each surface read
    and before closing any other overlay.
  * Flaky movements BFF: the `/movements` call often returns 500 and the UI
    shows a "Reintentar" button. Each surface is read with `_SURFACE_ATTEMPTS`
    tries and escalating render budgets, clicking Reintentar and keeping the
    session alive between tries; a surface that never loads becomes a warning
    (partial coverage), not a failed run.

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
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

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
class FalabellaSession:
    """What one Banco Falabella login produced.

    `balance` is the CMR Mastercard card (None when its available cupo did not
    parse), `movements` its parsed movements across the unbilled and billed
    tabs, and `warnings` the partial-coverage notes the scraper hands to
    `ProductScrapeResult.warnings`.
    """

    balance: Optional[RawBalance] = None
    movements: list[RawMovement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- Pure helpers (unit-tested; no browser) -----------------------------------

_CLP_SHAPE_RE = re.compile(r"^\d+(?:\.\d+)?$")
_DMY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_CUOTAS_RE = re.compile(r"^\s*(\d{1,3})\s*/\s*(\d{1,3})\s*$")
_SPACES_RE = re.compile(r"\s+")
_WHOLE = Decimal(1)

# The CMR card network words that become `attributes.brand`.
_BRANDS = ("Mastercard", "Visa", "American Express", "Amex")

# A movement whose description carries one of these WORDS is money in (a
# payment, abono, refund or reversal), so its amount flips positive. Matched on
# word boundaries against the accent-free, lower-cased description, so a
# merchant like "MERCADOPAGO*..." or "GRANULADOS ..." stays a charge.
# UNVERIFIED: no payment or credit row was seen live, so the word set is a best
# guess and must be confirmed against a real statement.
_PAYMENT_RE = re.compile(r"\b(?:pagos?|abonos?|devoluc\w*|revers\w*|anula\w*)\b")

# Dashboard product-card labels, matched accent- and case-insensitively. The
# amount is always the line BEFORE the label; the Avance cash-advance line
# ("Monto disponible") is deliberately not among them, so its figure is never
# read as a card balance.
_LABEL_LIMIT = "cupo de compras"
_LABEL_OWED = "cupo utilizado"
_LABEL_AVAILABLE = "cupo disponible"


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
    """Parse a "DD/MM/YYYY" date ("15/03/2025" -> 2025-03-15); None otherwise."""
    if not isinstance(raw, str):
        return None
    match = _DMY_RE.search(raw)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
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
    """Whether a description reads as money in rather than a charge (UNVERIFIED)."""
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

# Flaky movements BFF (see module docstring): tries per surface, escalating budgets.
_SURFACE_ATTEMPTS = 3
_RENDER_TIMEOUTS_MS = (12_000, 20_000, 30_000)
# An empty read must hold this many polls before a tab counts as empty.
_EMPTY_CONFIRM_POLLS = 3
# When the other tab was empty too, its stale empty table looks exactly like
# this one's, so the empty read must hold through most of the first budget.
_STALE_EMPTY_CONFIRM_POLLS = 20

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

# Whether the movements component shows a visible "no movements" message (a
# legitimately empty tab, as opposed to one that never loaded). Scoped to the
# component and to short, visible elements, so page copy cannot match.
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


def _read_surface(
    page, label: str, previous: Optional[list[list[str]]] = None
) -> Optional[list[list[str]]]:
    """Open a movements tab and read its rows with escalating-budget retries.

    Returns the body rows, `[]` for a confirmed-empty tab, or None when the
    surface never rendered after `_SURFACE_ATTEMPTS` tries (the caller turns a
    None into a partial-coverage warning). An empty read (a header-only table or
    the component's "no movements" message) only counts once it holds for
    `_EMPTY_CONFIRM_POLLS` polls (`_STALE_EMPTY_CONFIRM_POLLS` when `previous`
    was empty too), and rows equal to `previous` (the other tab's, still on
    screen right after the switch) are ignored, so a slow surface is filed
    neither as empty nor as the wrong tab.
    """
    confirm_polls = _STALE_EMPTY_CONFIRM_POLLS if previous == [] else _EMPTY_CONFIRM_POLLS
    _click_text(page, label)
    for attempt in range(_SURFACE_ATTEMPTS):
        _keep_alive(page)
        deadline = time.monotonic() + _RENDER_TIMEOUTS_MS[attempt] / 1000
        empty_polls = 0
        while time.monotonic() < deadline:
            rows = _read_table_rows(page)
            if rows and rows != previous:
                logger.info("Banco Falabella: %s surface read, %d rows", label, len(rows))
                return rows
            if rows == [] or (rows is None and _shows_empty_state(page)):
                empty_polls += 1
                if empty_polls >= confirm_polls:
                    logger.info("Banco Falabella: %s surface is empty", label)
                    return []
            else:
                empty_polls = 0
            page.wait_for_timeout(_TABLE_POLL_MS)
        if attempt < _SURFACE_ATTEMPTS - 1:
            _click_text(page, "Reintentar")
            _keep_alive(page)
    return None


def _read_billed(
    page, warnings: list[str], previous: Optional[list[list[str]]] = None
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
    rows = _read_surface(page, _BILLED_TAB, previous)
    if rows is None:
        warnings.append(
            f"Banco Falabella: billed movements surface failed after {_SURFACE_ATTEMPTS} attempts"
        )
        return []
    return movements_from_rows(rows, "billed")


def read_session(page) -> FalabellaSession:
    """Everything one authenticated page yields: the card and its movements."""
    warnings: list[str] = []
    _keep_alive(page)
    _dismiss_overlays(page)

    balance = balance_from_text(_wait_for_card_text(page))
    if balance.available is None:
        logger.warning("Banco Falabella: the card available cupo did not parse")
        warnings.append("Banco Falabella: the card available cupo did not parse")
        balance = None

    if not _open_card_detail(page):
        warnings.append("Banco Falabella: could not open the card detail")
        return FalabellaSession(balance=balance, movements=[], warnings=warnings)

    movements: list[RawMovement] = []
    unbilled = _read_surface(page, _UNBILLED_TAB)
    if unbilled is None:
        warnings.append(
            f"Banco Falabella: unbilled movements surface failed after {_SURFACE_ATTEMPTS} attempts"
        )
    else:
        movements.extend(movements_from_rows(unbilled, "unbilled"))

    movements.extend(_read_billed(page, warnings, previous=unbilled))
    return FalabellaSession(balance=balance, movements=movements, warnings=warnings)


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
