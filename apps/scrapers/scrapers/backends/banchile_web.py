"""Self-contained Banco de Chile web session (login + balance read).

Banco de Chile exposes no public/open-banking API for individuals, and the
`fintself` library we use for *transactions* only returns `MovementModel`s —
never an account balance (fintself#… — see issue #27). So to give `banchile`
a real, refreshable Saldo we log in ourselves with Playwright and read the
figure off the "Saldos y Movimientos" view.

This module deliberately does **not** import `fintself`; it only borrows its
login flow / page routes as a reference. The one gotcha worth repeating: Banco
de Chile serves a *degraded* post-login page to Playwright's default headless
shell (no "Mis Productos" menu — fintself#28), so we launch Chromium via the
``channel="chromium"`` full binary, which behaves like a headed session.

Design note (why DOM scraping, not XHR interception): issue #27 recommends
intercepting the SPA's balance JSON endpoint as the more robust option. That
requires observing the live endpoint, which needs real credentials + bank
access we don't have here, and importing a wrong figure into someone's net
worth is worse than importing none. So the balance is read from the rendered
page text, with all *interpretation* isolated in pure, unit-tested helpers
(`_balance_from_text` / `parse_clp`) so the source can be swapped for the XHR
endpoint later without touching the mapping.
"""

import asyncio
import logging
import re
import time
from datetime import date
from typing import Optional

from scrapers.base import ScrapedBalance

logger = logging.getLogger(__name__)

# --- BdC entry points / routes (reference: fintself cl/banco_chile.py) --------
LOGIN_URL = "https://sitiospublicos.bancochile.cl/personas"

# --- Browser context tuning (mirrors fintself's anti-detection defaults) ------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1366, "height": 768}
LOCALE = "es-CL"
TIMEZONE_ID = "America/Santiago"

# Timeouts (ms)
DEFAULT_TIMEOUT = 15000
LOGIN_TIMEOUT = 45000
# Post-login, the dashboard "Cuentas" balances arrive via a later XHR; poll the
# rendered page up to this long for them to appear.
BALANCE_WAIT_TIMEOUT = 30000

# --- Selectors, each a fallback list (BdC markup drifts — see issue #27) ------
_LOGIN_ACCESS_SELECTORS = [
    'a:has-text("Banco en Línea")',
    'a:has-text("Ingresar")',
    'button:has-text("Ingresar")',
    'a[href*="login"]',
]
_RUT_SELECTORS = [
    'input[placeholder*="RUT"]',
    'input[placeholder*="rut"]',
    'input[autocomplete="username"]',
    'input[name="username"]',
    'input[name="rut"]',
    "#rut",
    'input[type="text"]:visible',
]
_PASSWORD_SELECTORS = [
    'input[type="password"]:visible',
    'input[autocomplete="current-password"]',
    'input[name="password"]',
    "#password",
]
_SUBMIT_SELECTORS = [
    'button[type="submit"]:visible',
    'button:has-text("Ingresar")',
    'button:has-text("Entrar")',
    'button:has-text("Acceder")',
    "button.btn-primary",
]
# Post-login lands on portalpersonas.bancochile.cl with a "Cerrar Sesión" /
# "Mis Productos" chrome. Detecting via URL + body text is far more robust than
# a specific menu selector (the menu lives in a not-always-visible "Megamenu").
_LOGIN_SUCCESS_JS = (
    "() => location.href.includes('portalpersonas') "
    "|| /cerrar sesi[oó]n|mis productos/i.test(document.body.innerText || '')"
)
_POPUP_CLOSE_SELECTORS = [
    "button.close",
    "[aria-label='Close']",
    ".modal-close",
    "button[data-dismiss='modal']",
]


class BanChileWebError(RuntimeError):
    """Raised when the BdC web session can't log in or reach the balance."""


# --- Pure helpers (unit-tested; no browser) -----------------------------------

# Extraction is anchored on the literal "$" so an account number, a date, or a
# USD figure ("USD 0,00") near a label can never be mistaken for the balance —
# recording nothing ("sin dato") beats recording a wrong figure into net worth.
#
# Primary source: the post-login "Mis Productos" dashboard, where each account
# is rendered as
#     Cuenta Corriente
#     00-000-00000-01
#     Disponible
#     $ 2.500.000
# We take *only* "Cuenta Corriente" blocks — never "Línea de Crédito" or a
# "Tarjeta de Crédito" cupo — and only the CLP "$" figure, coupling "Disponible"
# tightly to "$" so a USD cuenta corriente ("Disponible\nUSD 0,00") is skipped.
# Multiple CLP checking accounts are summed (one `banchile/checking` product).
_CHECKING_ROW = re.compile(
    r"cuenta\s+corriente.{0,60}?disponible\s*\$\s?([\d.]{1,15}(?:,\d{1,2})?)",
    re.I | re.S,
)
# Fallback for the dedicated "Saldos y Movimientos" view, which labels the
# figure "Saldo Disponible" (spendable) / "Saldo Contable" (accounting).
# Tried in order so the spendable figure wins when both are present.
_SALDO_PATTERNS = [
    re.compile(r"saldo\s+disponible.{0,40}?\$\s?([\d.]{1,15}(?:,\d{1,2})?)", re.I | re.S),
    re.compile(r"saldo\s+contable.{0,40}?\$\s?([\d.]{1,15}(?:,\d{1,2})?)", re.I | re.S),
]


def parse_clp(raw: Optional[str]) -> Optional[int]:
    """Parse a Chilean-formatted CLP amount into whole pesos.

    Handles ``$1.234.567``, ``1.234.567``, ``$ 1.234.567`` and the rare
    ``1.234.567,00`` (comma = decimals, rounded). Returns None when there's no
    digit to parse.
    """
    if not raw:
        return None
    cleaned = re.sub(r"(?i)CLP|\$|\s", "", raw.strip())
    if "," in cleaned:  # comma is the decimal separator
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:  # dots are thousands separators
        cleaned = cleaned.replace(".", "")
    if not re.search(r"\d", cleaned):
        return None
    try:
        return int(round(float(cleaned)))  # float() carries a leading "-"
    except ValueError:
        logger.warning("Could not parse CLP amount: %r", raw)
        return None


def _balance_from_text(text: Optional[str]) -> Optional[int]:
    """Extract the CLP checking available balance from a page's visible text.

    Prefers the dashboard product summary (summing every CLP "Cuenta Corriente"
    disponible); falls back to a single "Saldo Disponible/Contable" figure.
    Returns None when neither is present.
    """
    if not text:
        return None

    total = 0
    found = False
    for raw in _CHECKING_ROW.findall(text):
        value = parse_clp(raw)
        if value is not None:
            total += value
            found = True
    if found:
        return total

    for pattern in _SALDO_PATTERNS:
        match = pattern.search(text)
        if match:
            value = parse_clp(match.group(1))
            if value is not None:
                return value
    return None


# Guarded: right after the post-login redirect `document.body` can still be
# null, and the balances load via a later XHR — so this must not throw.
_INNER_TEXT_JS = "() => (document.body && document.body.innerText) || ''"


def _read_checking(page) -> Optional[int]:
    """Read the page's visible text and extract the CLP checking balance.

    Quiet (no logging) so it's safe to call in a polling loop; returns None
    when the balance isn't present/rendered yet.
    """
    try:
        text = page.evaluate(_INNER_TEXT_JS)
    except Exception:
        return None
    return _balance_from_text(text)


def balances_from_page(page) -> list[ScrapedBalance]:
    """Read the checking balance off an already-authenticated BdC page.

    This is the seam the tests mock: it only reads `page`'s visible text (via
    ``page.evaluate``) and shapes the result, so it can be exercised with a
    fake page and no real bank. Returns an empty list when no balance is found.

    Credit cards are intentionally **not** read here (deferred — see
    `scrape_balances` in institutions/banchile.py for the rationale).
    """
    checking = _read_checking(page)
    if checking is None:
        logger.warning("BanChile: no checking balance found on page")
        return []

    logger.info("BanChile checking balance: $%s CLP", f"{checking:,}")
    return [
        ScrapedBalance(
            institution="banchile",
            product_kind="checking",
            balance=checking,
            as_of=date.today(),
            currency="CLP",
        )
    ]


# --- Browser plumbing ---------------------------------------------------------


def _first_visible(page, selectors: list[str], timeout: int = 5000):
    """Wait for the first of `selectors` to become visible; return it or None.

    Splits the budget across selectors and *waits* (rather than a bare
    `is_visible` snapshot) so slow-rendering SPA fields aren't missed.
    """
    per_selector = max(1000, timeout // len(selectors)) if selectors else timeout
    for selector in selectors:
        try:
            element = page.locator(selector).first
            element.wait_for(state="visible", timeout=per_selector)
            return element
        except Exception:
            continue
    return None


def _click_first(page, selectors: list[str], timeout: int = 5000) -> bool:
    element = _first_visible(page, selectors, timeout)
    if element is None:
        return False
    try:
        element.click()
        return True
    except Exception:
        try:  # JS click as a fallback for overlay-covered elements
            page.evaluate("el => el.click()", element)
            return True
        except Exception:
            return False


def _launch_browser(playwright, headless: bool):
    """Launch Chromium via the full-binary "chromium" channel (see module doc).

    Falls back to the default launch on Playwright builds without channel
    support (<1.49), matching backends/fintself.py::_force_new_headless.
    """
    try:
        return playwright.chromium.launch(headless=headless, channel="chromium")
    except Exception:
        logger.warning("chromium channel unavailable; using default headless shell")
        return playwright.chromium.launch(headless=headless)


def _login(page, rut: str, password: str) -> None:
    """Log into Banco de Chile with RUT + password."""
    # "domcontentloaded", not the default "load": the public site pulls heavy
    # marketing/third-party resources whose "load" event can exceed 45s (it
    # timed out in-service right after fintself's session). The form is in the
    # DOM once HTML is parsed, and `_first_visible` waits for the RUT field.
    page.goto(LOGIN_URL, timeout=LOGIN_TIMEOUT, wait_until="domcontentloaded")
    # The public site gates the form behind an "Ingresar" access button.
    if _click_first(page, _LOGIN_ACCESS_SELECTORS, timeout=5000):
        page.wait_for_timeout(3000)

    rut_field = _first_visible(page, _RUT_SELECTORS, timeout=15000)
    if rut_field is None:
        raise BanChileWebError("Could not find the RUT field on the login page")
    rut_field.fill(rut)

    password_field = _first_visible(page, _PASSWORD_SELECTORS, timeout=8000)
    if password_field is None:
        raise BanChileWebError("Could not find the password field on the login page")
    password_field.fill(password)

    if not _click_first(page, _SUBMIT_SELECTORS, timeout=5000):
        page.keyboard.press("Enter")  # some layouts submit on Enter

    try:
        page.wait_for_function(_LOGIN_SUCCESS_JS, timeout=LOGIN_TIMEOUT)
    except Exception as exc:
        raise BanChileWebError(
            "Login did not reach the post-login page (bad credentials, "
            "maintenance, or a changed layout)."
        ) from exc
    logger.info("BanChile login successful")


def _dismiss_popup(page) -> None:
    """Best-effort dismissal of the post-login marketing popup."""
    try:
        _click_first(page, _POPUP_CLOSE_SELECTORS, timeout=4000)
    except Exception:
        pass


def _wait_for_balances(page, timeout_ms: int) -> list[ScrapedBalance]:
    """Poll the dashboard until its async balance widget renders.

    A single read right after the post-login redirect misses the balances (the
    "Cuentas" figures arrive via a later XHR, and `document.body` may not even
    exist yet). Poll the pure extractor until a figure appears or time runs out.
    We stay on the dashboard on purpose: the dedicated "Saldos y Movimientos"
    route opens an account-selection modal that defaults to the USD account.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while _read_checking(page) is None and time.monotonic() < deadline:
        page.wait_for_timeout(1000)
    return balances_from_page(page)  # one canonical build + log


def _scrape_sync(rut: str, password: str, headless: bool) -> list[ScrapedBalance]:
    """Synchronous Playwright flow (runs in a worker thread, no event loop)."""
    from playwright.sync_api import sync_playwright  # lazy: keeps tests browser-free

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, headless)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport=VIEWPORT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT)

            _login(page, rut, password)
            _dismiss_popup(page)
            # Balances render on the post-login dashboard via a later XHR.
            return _wait_for_balances(page, BALANCE_WAIT_TIMEOUT)
        finally:
            browser.close()


async def fetch_balances(
    rut: str, password: str, *, headless: bool = True
) -> list[ScrapedBalance]:
    """Log into Banco de Chile and return its scraped balances (checking only).

    Runs the synchronous Playwright flow in a thread executor so it doesn't
    block the scheduler's event loop, mirroring backends/fintself.py.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scrape_sync, rut, password, headless)
