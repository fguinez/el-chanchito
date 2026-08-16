"""Self-contained Tarjeta Lider Bci web session (real Chrome over CDP).

Tarjeta Lider Bci (the retailcard.cl credit card co-branded by BCI) exposes no
public/open-banking API for individuals, and `fintself` doesn't cover it, so the
figures are read off the post-login SPA (https://www.liderbciserviciosfinancieros.cl):

  * the "Mi Tarjeta -> Saldos" page: the CLP "Nacional" and USD "Internacional"
    cupos (Autorizado/Utilizado/Disponible), plus the card product name and the
    masked number's last4;
  * the "Mi Tarjeta -> Movimientos" page: the Nacionales (CLP) charges, read
    per page across the pager.

Auth (why a real Chrome driven over CDP): the login sits behind a Cloudflare
Turnstile that passes invisibly ONLY for a genuine browser. A Playwright-launched
Chromium (headless or headed) gets an unsolvable interactive "Verifique que es un
ser humano" check, and a captured session doesn't survive headless reuse (the auth
token lives in tab-scoped sessionStorage and Cloudflare rebinds on a fresh browser).
So both sign-in and scraping drive a *genuine* Chrome: an ordinary OS process on a
debug port (`_launch_real_chrome`), not a Playwright-launched browser. Playwright
connects over CDP only to autofill the login (Turnstile then clears invisibly, or
after the human ticks the check, which we never do) and to read pages.

`scrape_card` runs in one of two modes:
  * managed (default, fully unattended): it launches a headed Chrome, signs in via
    autofill, scrapes, and closes it. Needs a machine with a display (Cloudflare
    blocks headless), so not the headless Docker container.
  * reuse: when `LIDER_BCI_CDP_URL` points at an already-running debuggable Chrome
    (`make bci-lider-login`, or one you manage), it drives that and leaves it open.
Either way it reuses an already-signed-in tab or re-logs-in via autofill, and
raises `BciLiderSessionError` when Chrome is unreachable or sign-in can't complete.

Design note (why DOM scraping, not the XHR API): the balances are bootstrapped
into the Angular app state at login and never re-fetched on a route change, and
the movements endpoint (POST api-ssff.retailcard.cl/.../movfacturarpesos) needs
its request payload and session token reverse-engineered. Reading the rendered
text keeps every figure anchored on its own `$`/`US$` label (a drift records
nothing rather than a wrong number) and mirrors `banchile_web.py`, with all the
interpretation isolated in pure, unit-tested helpers so the source can be swapped
for the XHR endpoint later without touching the mapping.
"""

import datetime
import importlib.util
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from product_model import CreditCardAttributes, CreditCardMetrics

from scrapers.base import ScrapedProduct

logger = logging.getLogger(__name__)

# --- Portal entry points / routes ---------------------------------------------
LOGIN_URL = "https://www.liderbciserviciosfinancieros.cl/login"
BALANCES_URL = "https://www.liderbciserviciosfinancieros.cl/private-home/my-card/balances"
MOVEMENTS_URL = "https://www.liderbciserviciosfinancieros.cl/private-home/my-card/movements"

INSTITUTION = "bci_lider"

# apps/scrapers/, resolved from this file so cwd doesn't matter.
_APP_DIR = Path(__file__).resolve().parents[2]

# Real-Chrome / CDP flow. The portal's login is behind a Cloudflare Turnstile that
# passes invisibly ONLY for a genuine browser: a Playwright-launched Chromium gets
# an unsolvable interactive check, and a captured session doesn't survive headless
# reuse (the auth token lives in tab-scoped sessionStorage and Cloudflare rebinds
# on a fresh browser). So both sign-in and scraping run against a real Chrome (an
# ordinary OS process on a debug port, not driven by Playwright); Playwright only
# connects over CDP to autofill the login and read pages. A dedicated profile keeps
# it isolated from the user's main Chrome.
CDP_URL_ENV = "LIDER_BCI_CDP_URL"
_DEFAULT_CDP_PORT = 9222
_CHROME_PROFILE = _APP_DIR / ".bci_lider_chrome_profile"
# Budget for a scrape that may need to (re-)log in via autofill first.
_LOGIN_WAIT_S = 120


def _cdp_port() -> int:
    return int(os.environ.get("LIDER_BCI_CDP_PORT", _DEFAULT_CDP_PORT))


def _chrome_executable() -> str:
    """Path to a real Chrome/Chromium (override with LIDER_BCI_CHROME_PATH)."""
    override = os.environ.get("LIDER_BCI_CHROME_PATH")
    if override:
        return override
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise BciLiderWebError(
        "Could not find Google Chrome. Set LIDER_BCI_CHROME_PATH to its binary."
    )


def _launch_real_chrome(port: int, url: str, offscreen: bool = False) -> subprocess.Popen:
    """Launch a genuine Chrome with remote debugging on `port`, opening `url`.

    `offscreen` parks the window off-screen and stops Chrome throttling a window it
    thinks is hidden, so a managed scrape doesn't flash a visible window; it stays a
    real headed browser (Cloudflare still passes). Managed mode falls back to a
    visible window if the off-screen launch misbehaves. Interactive sign-in
    (`save_login_session`) always launches visible so a human can see it.
    """
    exe = _chrome_executable()
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if offscreen:
        args += ["--window-position=-32000,-32000", "--disable-backgrounding-occluded-windows"]
    args.append(url)
    logger.info(
        "Launching Chrome: %s (debug port %s, offscreen=%s)", exe, port, offscreen
    )
    # start_new_session makes this Chrome its own process-group leader, so managed
    # mode can tear down the whole tree (renderers/GPU/zygote) in one signal.
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )


def _port_in_use(port: int) -> bool:
    """True if something is already listening on localhost:`port`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# Timeouts (ms). We drive a real Chrome over CDP, so no browser-context tuning
# (user agent, viewport, locale) is needed here: it's the user's genuine Chrome.
LOGIN_TIMEOUT = 60000  # login round-trip / route navigation can be slow
RENDER_TIMEOUT_MS = 30000  # budget to poll a page's async widget into the DOM
MAX_MOVEMENT_PAGES = 20  # hard cap on the pager loop (10 rows/page)

# --- Selectors (each a fallback list; SPA markup drifts) ----------------------
# Sign-in happens in a genuine Chrome (over CDP): we autofill the RUT + clave and
# click Ingresar, but only submit once Cloudflare Turnstile has issued its token
# (invisibly for a real browser, or after the human ticks the check). We never
# tick the check ourselves.
_RUT_SELECTORS = [
    'input[placeholder="Rut"]',
    'input[placeholder*="Rut"]',
    'input[formcontrolname="rut"]',
    'input[type="text"]:visible',
]
_PASSWORD_SELECTORS = [
    'input[type="password"]:visible',
    'input[placeholder*="Clave"]',
    'input[formcontrolname="password"]',
]
_SUBMIT_SELECTORS = [
    'button[type="submit"]:visible',
    'button:has-text("Ingresar")',
]
# Length of the Cloudflare Turnstile token; > 0 means the check has passed.
_TURNSTILE_TOKEN_JS = (
    "() => { const el = document.querySelector("
    "'input[name=\"cf-turnstile-response\"],textarea[name=\"cf-turnstile-response\"]');"
    " return el ? (el.value || '').length : 0; }"
)
_POPUP_CLOSE_SELECTORS = [
    "button[aria-label='Close']",
    "[aria-label='Cerrar']",
    "button.close",
    ".modal-close",
    ".close-modal",
    "button.mat-dialog-close",
    "[mat-dialog-close]",
]

# --- Pure helpers (unit-tested; no browser) -----------------------------------

# A Chilean-formatted amount: 1.234.567 with optional ,dd decimals.
_AMT = r"([\d.]{1,15}(?:,\d{1,2})?)"

# The "Saldos de tu tarjeta" table renders one row per currency, three amounts
# in Autorizado / Utilizado / Disponible order (values below are synthetic):
#     Nacional        $2.500.000   $1.000.000   $1.500.000
#     Internacional   US$1.234,56  US$234,56    US$1.000,00
# CLP is anchored on a bare "$", USD on "US$", so one is never read as the other.
_NACIONAL_RE = re.compile(
    r"nacional\s+\$\s?" + _AMT + r"\s+\$\s?" + _AMT + r"\s+\$\s?" + _AMT, re.I
)
_INTERNACIONAL_RE = re.compile(
    r"internacional\s+US\$\s?" + _AMT + r"\s+US\$\s?" + _AMT + r"\s+US\$\s?" + _AMT,
    re.I,
)
# "Lider Bci Tradicional\nTarjeta N° XXXX XXXX XXXX 1234": the product name is the
# line above, the last4 the trailing digit run after the masked groups.
_CARD_NAME_RE = re.compile(r"([^\n]+?)\s*\n\s*Tarjeta\s+N[°º]", re.I)
_LAST4_RE = re.compile(r"Tarjeta\s+N[°º][^\n]*?(\d{4})\s*(?:\n|$)", re.I)

# One movements row: date, description, an optional "NN/NN" cuotas token, then the
# CLP amount anchored on "$". Charges carry no sign; a credit/abono (e.g. the card
# payment) is marked with a "-" that the portal prints *after* the "$" ("$-999.999"),
# so both placements are accepted. Anchored to a line so the greedy description
# can't swallow rows.
_MOVEMENT_RE = re.compile(
    r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+"           # 1: date
    r"(.+?)"                                     # 2: description (lazy)
    r"(?:\s+(\d{1,2}/\d{1,2}))?"                # 3: cuotas (optional)
    r"\s+(-?\s*\$\s?-?\s?[\d.]{1,15})\s*$",     # 4: monto (sign either side of "$")
    re.M,
)
_MOVEMENTS_START_RE = re.compile(r"tienda\s*/\s*descripci[oó]n.*?monto", re.I | re.S)
_MOVEMENTS_END_RE = re.compile(r"mostrando\s+p[aá]gina", re.I)


def parse_amount(raw: Optional[str]) -> Optional[float]:
    """Parse a Chilean-formatted CLP/USD amount into a float (keeps decimals).

    Handles ``$1.234.567``, ``US$1.234,56``, ``$ 999.999`` and ``1.234,00``
    (comma = decimal separator). Returns None when there's no digit to parse.
    """
    if not raw:
        return None
    cleaned = re.sub(r"(?i)US\$|USD|CLP|\$|\s", "", raw.strip())
    if "," in cleaned:  # comma is the decimal separator
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:  # dots are thousands separators
        cleaned = cleaned.replace(".", "")
    if not re.search(r"\d", cleaned):
        return None
    try:
        return float(cleaned)  # float() carries a leading "-"
    except ValueError:
        logger.warning("Could not parse amount: %r", raw)
        return None


def parse_clp(raw: Optional[str]) -> Optional[int]:
    """Parse a Chilean-formatted CLP amount into whole pesos (rounds decimals)."""
    value = parse_amount(raw)
    return int(round(value)) if value is not None else None


def card_saldos_from_text(text: Optional[str]) -> dict[str, dict[str, float]]:
    """Read the card cupos per currency off the "Saldos" page text.

    Returns e.g. (synthetic) ``{"CLP": {"available": 1500000.0, "limit":
    2500000.0, "owed": 1000000.0}, "USD": {"available": 1000.0, "limit":
    1234.56, "owed": 234.56}}`` (a currency appears only when its whole row parses).
    Autorizado -> ``limit``, Utilizado -> ``owed``, Disponible -> ``available``.
    Returns ``{}`` when nothing parses.
    """
    if not text:
        return {}
    result: dict[str, dict[str, float]] = {}
    for currency, pattern in (("CLP", _NACIONAL_RE), ("USD", _INTERNACIONAL_RE)):
        match = pattern.search(text)
        if match is None:
            continue
        autorizado, utilizado, disponible = (parse_amount(g) for g in match.groups())
        if disponible is None:
            continue
        entry: dict[str, float] = {"available": disponible}
        if autorizado is not None:
            entry["limit"] = autorizado
        if utilizado is not None:
            entry["owed"] = utilizado
        result[currency] = entry
    return result


def card_last4_from_text(text: Optional[str]) -> Optional[str]:
    """The card's last four digits from "Tarjeta N° XXXX XXXX XXXX 1234"."""
    if not text:
        return None
    match = _LAST4_RE.search(text)
    return match.group(1) if match else None


def card_name_from_text(text: Optional[str]) -> Optional[str]:
    """The card product name printed above "Tarjeta N°" (e.g. "Lider Bci Tradicional")."""
    if not text:
        return None
    match = _CARD_NAME_RE.search(text)
    return match.group(1).strip() if match else None


def card_products_from_text(text: Optional[str]) -> list[ScrapedProduct]:
    """Shape the "Saldos" page text into credit_card ScrapedProducts (CLP + USD).

    Disponible -> ``metrics.available``, Autorizado -> ``metrics.limit``,
    Utilizado -> ``metrics.owed`` (net worth prefers it, falling back to
    limit − available). The masked number's last4 and the product name ride
    along as attributes, shared by both currencies. Returns [] when nothing
    parses (recorded as a run warning by the caller).
    """
    saldos = card_saldos_from_text(text)
    if not saldos:
        logger.warning("BciLider: no card cupos found on the Saldos page")
        return []

    last4 = card_last4_from_text(text)
    brand = card_name_from_text(text)
    attributes = (
        CreditCardAttributes(last4=last4, brand=brand)
        if (last4 is not None or brand is not None)
        else None
    )

    products: list[ScrapedProduct] = []
    for currency in ("CLP", "USD"):
        data = saldos.get(currency)
        if not data:
            continue
        limit = data.get("limit")
        owed = data.get("owed")
        logger.info(
            "BciLider credit_card balance: %s available %s (limit %s, owed %s)",
            currency,
            f"{data['available']:,.2f}",
            f"{limit:,.2f}" if limit is not None else "n/a",
            f"{owed:,.2f}" if owed is not None else "n/a",
        )
        products.append(
            ScrapedProduct(
                institution=INSTITUTION,
                kind="credit_card",
                currency=currency,
                name=brand,  # the card product name, e.g. "Lider Bci Tradicional"
                attributes=attributes,
                metrics=CreditCardMetrics(
                    available=data["available"], limit=limit, owed=owed
                ),
            )
        )
    return products


def _parse_date_ddmmyyyy(raw: str) -> Optional[datetime.date]:
    """Parse a "DD/MM/YYYY" date; None when it isn't a real date."""
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def movements_from_text(text: Optional[str]) -> list[dict]:
    """Parse the Nacionales (CLP) movements table into raw movement dicts.

    Each dict is ``{"date": date, "description": str, "cuotas": str|None,
    "amount": int}`` where ``amount`` is negative for a charge (the common case)
    and positive for a credit/abono (a "-" on the Monto). Only the region
    between the table header and the "Mostrando Página" pager is scanned, so the
    surrounding chrome can't be read as rows. Returns [] when nothing parses.
    """
    if not text:
        return []
    start = _MOVEMENTS_START_RE.search(text)
    region = text[start.end():] if start else text
    end = _MOVEMENTS_END_RE.search(region)
    if end:
        region = region[: end.start()]

    movements: list[dict] = []
    for match in _MOVEMENT_RE.finditer(region):
        date_raw, description, cuotas, monto_raw = match.groups()
        tx_date = _parse_date_ddmmyyyy(date_raw)
        pesos = parse_clp(monto_raw)
        if tx_date is None or pesos is None:
            continue
        # The Monto is what the card charged, so flipping its sign gives the
        # movement: a charge becomes an expense, a "-" abono becomes a credit.
        amount = -pesos
        movements.append(
            {
                "date": tx_date,
                "description": description.strip(),
                "cuotas": cuotas or None,
                "amount": amount,
            }
        )
    return movements


# --- Browser plumbing ---------------------------------------------------------


@dataclass
class BciLiderCardResult:
    """What a Lider Bci session scraped in one login.

    `products` are the typed credit_card observations (CLP + USD), `movements`
    the raw Nacionales charge dicts (the institution scraper converts them to
    `ScrapedTransaction`s), and `warnings` flag partial coverage (e.g. the Saldos
    page never rendered) so the run records `partial` instead of failing.
    """

    products: list[ScrapedProduct] = field(default_factory=list)
    movements: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BciLiderWebError(RuntimeError):
    """Raised when the Lider Bci web session hits an unexpected page/layout."""


class BciLiderSessionError(RuntimeError):
    """Raised when the debuggable Chrome is unreachable or sign-in didn't complete."""


def _first_visible(page, selectors: list[str], timeout: int = 5000):
    """Wait for the first of `selectors` to become visible; return it or None."""
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


def _find_page_matching(browser, needle: str):
    """The first open page whose URL contains `needle`, or None."""
    for context in browser.contexts:
        for page in context.pages:
            try:
                if needle in page.url:
                    return page
            except Exception:
                continue
    return None


def _find_authenticated_page(browser, deadline: float):
    """Poll the CDP-connected browser for a page in the private area; None on timeout."""
    while time.monotonic() < deadline:
        page = _find_page_matching(browser, "/private-home")
        if page is not None:
            return page
        time.sleep(2)
    return None


def _fill_by_keystrokes(page, selectors: list[str], value: str, timeout: int) -> None:
    """Type `value` into the first matching field with real keystrokes.

    `.fill()` sets the DOM value without driving Angular's reactive form or the
    RUT input mask, leaving the form model empty and Ingresar inert; typing
    character by character (plus a blur) is what registers the value.
    """
    field = _first_visible(page, selectors, timeout=timeout)
    if field is None:
        return
    field.click()
    field.fill("")  # clear any prior value before typing
    field.press_sequentially(value, delay=60)
    field.press("Tab")  # blur triggers Angular validation


def _submit_enabled(page) -> bool:
    try:
        return bool(page.evaluate(
            "() => { const b = [...document.querySelectorAll('button')]"
            ".find(x => /ingresar/i.test(x.textContent || ''));"
            " return b ? !b.disabled : false; }"
        ))
    except Exception:
        return False


def _autofill_and_submit(page, rut: Optional[str], password: Optional[str], deadline: float) -> None:
    """Type the RUT + clave, then click Ingresar once the login is submittable.

    Submits only when the Cloudflare Turnstile token is present AND Ingresar is
    enabled. The token appears invisibly for a genuine browser or after the human
    ticks the "Verifique que es un ser humano" check; we never tick it ourselves.
    If it never clears, we never submit (the caller then times out).
    """
    try:
        if rut:
            _fill_by_keystrokes(page, _RUT_SELECTORS, rut, timeout=15000)
        if password:
            _fill_by_keystrokes(page, _PASSWORD_SELECTORS, password, timeout=8000)
    except Exception:
        logger.info("BciLider: autofill hit an issue; finish the login in the window",
                    exc_info=True)

    while time.monotonic() < deadline:
        try:
            if page.evaluate(_TURNSTILE_TOKEN_JS) and _submit_enabled(page):
                _click_first(page, _SUBMIT_SELECTORS, timeout=5000)
                logger.info("BciLider: submitted the login (Turnstile token present)")
                return
        except Exception:
            pass
        page.wait_for_timeout(1000)


def save_login_session(
    rut: Optional[str] = None, password: Optional[str] = None, timeout_s: int = 600,
) -> str:
    """Launch a real Chrome, sign in via CDP-driven autofill, and leave it running.

    A Playwright-launched browser can't clear the portal's Cloudflare check, so a
    *genuine* Chrome (an ordinary OS process on a debug port, not a Playwright-driven
    browser) is opened; Playwright connects over CDP and autofills the RUT + clave
    and clicks Ingresar once Turnstile has issued its token. For a genuine browser
    that token appears invisibly, so no human action is needed; if Cloudflare instead
    shows the "Verifique que es un ser humano" check, the human ticks it (we never
    do) and the submit fires automatically. The Chrome is left running so scraping
    (LIDER_BCI_CDP_URL) can drive it. Returns the CDP URL to point the scraper at.
    """
    from playwright.sync_api import sync_playwright

    rut = rut or os.environ.get("LIDER_BCI_RUT")
    password = password or os.environ.get("LIDER_BCI_PASSWORD")
    port = _cdp_port()
    cdp_url = f"http://localhost:{port}"
    _launch_real_chrome(port, LOGIN_URL)
    print(
        "\nA Chrome window opened and the login is being autofilled. If Cloudflare "
        "shows a 'Verifique que es un ser humano' check, tick it (I won't); the "
        f"sign-in then submits automatically. Waiting up to {timeout_s // 60} min. "
        f"Leave this Chrome running so scraping can use it (LIDER_BCI_CDP_URL={cdp_url}).",
        flush=True,
    )

    deadline = time.monotonic() + timeout_s
    with sync_playwright() as playwright:
        browser = None
        while browser is None and time.monotonic() < deadline:
            try:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
            except Exception:
                time.sleep(1)
        if browser is None:
            raise BciLiderWebError(f"Could not reach the Chrome debug port {cdp_url}.")

        login_page = None
        while login_page is None and time.monotonic() < deadline:
            login_page = _find_page_matching(browser, "liderbciserviciosfinancieros")
            if login_page is None:
                time.sleep(1)
        if login_page is not None and "/private-home" not in login_page.url:
            _autofill_and_submit(login_page, rut, password, deadline)

        if _find_authenticated_page(browser, deadline) is None:
            raise BciLiderSessionError(
                "Sign-in not detected in time. Re-run `make bci-lider-login`."
            )
        # Do NOT close `browser`: dropping the CDP connection leaves Chrome running.

    print(f"\nSigned in. Chrome left running on {cdp_url}. "
          f"Point the scraper at it with LIDER_BCI_CDP_URL={cdp_url}.")
    return cdp_url


def _dismiss_popup(page) -> None:
    """Best-effort dismissal of the post-login marketing modal."""
    try:
        _click_first(page, _POPUP_CLOSE_SELECTORS, timeout=4000)
    except Exception:
        pass


def _inner_text(page) -> str:
    """The page's visible text, guarded (body can be null mid-render)."""
    try:
        return page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        return ""


def _read_balances(page) -> list[ScrapedProduct]:
    """Open the Saldos page and read the card cupos, polling for the async render.

    An expired session bounces to /login; that raises `BciLiderSessionError` so the
    caller can re-log-in (or prompt one) instead of recording an empty scrape.
    """
    page.goto(BALANCES_URL, timeout=LOGIN_TIMEOUT, wait_until="domcontentloaded")
    _dismiss_popup(page)
    deadline = time.monotonic() + RENDER_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if "/login" in page.url:
            raise BciLiderSessionError(
                "BciLider not signed in (redirected to /login). "
                "Run `make bci-lider-login` to sign in again."
            )
        text = _inner_text(page)
        if card_saldos_from_text(text):  # quiet check while the widget renders
            return card_products_from_text(text)
        page.wait_for_timeout(1000)
    # Final attempt: build once (logs "no card cupos" only if it's genuinely empty).
    return card_products_from_text(_inner_text(page))


def _read_movements(page) -> list[dict]:
    """Open the Movimientos page and read the Nacionales charges across the pager.

    Stays on the default "Nacionales" tab (CLP) and reads each pager page, then
    switches to "Último periodo facturado" for the prior cycle. Dedup keys make
    the overlap between the two periods harmless. Best-effort: a pager/tab that
    won't click just ends that leg with whatever was collected.
    """
    page.goto(MOVEMENTS_URL, timeout=LOGIN_TIMEOUT, wait_until="domcontentloaded")
    _dismiss_popup(page)

    # Wait for the first page of rows (or the empty-state) to render.
    deadline = time.monotonic() + RENDER_TIMEOUT_MS / 1000
    while time.monotonic() < deadline and not movements_from_text(_inner_text(page)):
        page.wait_for_timeout(1000)

    seen: set[tuple] = set()
    movements: list[dict] = []

    def collect() -> None:
        for mov in movements_from_text(_inner_text(page)):
            key = (mov["date"], mov["description"], mov["amount"])
            if key not in seen:
                seen.add(key)
                movements.append(mov)

    def read_all_pages() -> None:
        collect()
        for page_num in range(2, MAX_MOVEMENT_PAGES + 1):
            before = len(movements)
            try:
                button = page.get_by_text(str(page_num), exact=True).first
                button.wait_for(state="visible", timeout=3000)
                button.click()
            except Exception:
                break
            page.wait_for_timeout(1500)
            collect()
            if len(movements) == before:  # pager didn't advance
                break

    read_all_pages()
    try:  # the prior billed cycle adds history on the first run
        page.get_by_text("Último periodo facturado", exact=False).first.click(
            timeout=3000
        )
        page.wait_for_timeout(1500)
        read_all_pages()
    except Exception:
        logger.info("BciLider: no 'Último periodo facturado' view; skipping")

    logger.info("BciLider: %d Nacionales movements read", len(movements))
    return movements


def _read_card(page, result: BciLiderCardResult) -> None:
    """Read balances + movements into `result` from an authenticated tab.

    The balances leg doubles as a final auth check: a session that bounced to
    /login raises BciLiderSessionError (the caller re-logs-in and retries). The
    movements leg is best-effort and degrades to a warning.
    """
    result.products = _read_balances(page)
    if not result.products:
        result.warnings.append("BciLider: no card balances parsed")
    try:
        result.movements = _read_movements(page)
    except Exception as exc:
        logger.exception("BciLider: movements leg failed")
        result.warnings.append(f"BciLider: movements failed: {exc}")


def _login_via_autofill(page, rut: Optional[str], password: Optional[str], deadline: float) -> None:
    """Drive the login form on `page` and wait for the private area.

    Autofill clears Cloudflare because this is a genuine Chrome. Raises
    BciLiderSessionError if sign-in can't complete (e.g. Cloudflare shows the human
    check with nobody to tick it, or the credentials are wrong).
    """
    if "/login" not in (page.url or ""):
        page.goto(LOGIN_URL, timeout=LOGIN_TIMEOUT, wait_until="domcontentloaded")
    _autofill_and_submit(page, rut, password, deadline)
    while time.monotonic() < deadline:
        if "/private-home" in page.url:
            return
        page.wait_for_timeout(1000)
    raise BciLiderSessionError(
        "Could not sign in via the debuggable Chrome (Cloudflare may need a human "
        "to tick the check, or the credentials are wrong). Run `make bci-lider-login`."
    )


def _ensure_logged_in(page, rut: Optional[str], password: Optional[str], deadline: float) -> None:
    """Make `page` an authenticated portal tab, logging in via autofill if needed.

    Probes by navigating to the balances route, then *polls* for the SPA route
    guard's verdict: an expired session's redirect to /login fires after Angular
    boots, so a single fixed snapshot would misread a slow bounce as
    "authenticated" and skip the re-login. Stops early once /login appears (then
    re-logs-in) or the balances render (then it's authenticated). A stale tab can
    still show a /private-home URL after the session idle-expired, so the render
    check, not the URL, is what confirms auth.
    """
    try:
        page.goto(BALANCES_URL, timeout=LOGIN_TIMEOUT, wait_until="domcontentloaded")
    except Exception:
        pass
    probe_deadline = min(deadline, time.monotonic() + 8)
    while time.monotonic() < probe_deadline:
        if "/login" in page.url:
            _login_via_autofill(page, rut, password, deadline)
            return
        if card_saldos_from_text(_inner_text(page)):  # quiet: authenticated + rendering
            return
        page.wait_for_timeout(500)
    if "/login" in page.url:  # a bounce that landed just after the probe window
        _login_via_autofill(page, rut, password, deadline)


def _connect_cdp(playwright, cdp_url: str, deadline: float):
    """Connect to a Chrome debug port, retrying until it's up or `deadline`.

    Managed mode launches Chrome and connects immediately, so the port needs a few
    seconds to come up; attended mode connects on the first try.
    """
    last_exc: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            return playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    raise BciLiderSessionError(
        f"Could not reach the Chrome debug port {cdp_url}. In managed mode Chrome "
        "failed to start (a machine with a display is required); in reuse mode start "
        "it with `make bci-lider-login`."
    ) from last_exc


def _terminate_chrome(proc: subprocess.Popen) -> None:
    """Stop a Chrome we launched (managed mode), including its child processes.

    Signals the whole process group (Chrome spawns renderer/GPU/zygote children) so
    a SIGKILL fallback can't orphan them, and reaps the process after each signal so
    no zombie lingers and the debug port / profile lock is released for the next run.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None

    def _kill(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass
        except Exception:
            logger.warning("BciLider: error signalling managed Chrome", exc_info=True)

    _kill(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
        return
    except Exception:
        pass
    _kill(signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except Exception:
        logger.warning("BciLider: managed Chrome did not exit after SIGKILL")


def _scrape_over_cdp(cdp_url: str, rut: Optional[str], password: Optional[str]) -> BciLiderCardResult:
    """Read the card by driving a Chrome reachable at `cdp_url`; never closes it.

    Reuses an existing tab (an already-signed-in one keeps its tab-scoped
    sessionStorage auth) and ensures it's logged in, re-logging-in via autofill on a
    genuine-Chrome tab when the session has expired: that is what clears Cloudflare.
    """
    from playwright.sync_api import sync_playwright

    result = BciLiderCardResult()
    deadline = time.monotonic() + _LOGIN_WAIT_S
    with sync_playwright() as playwright:
        browser = _connect_cdp(playwright, cdp_url, min(deadline, time.monotonic() + 30))
        # Prefer an already-authenticated tab (keeps its sessionStorage), else any
        # existing tab, else a new one; then ensure it's signed in before reading.
        page = (
            _find_page_matching(browser, "/private-home")
            or _find_page_matching(browser, "liderbciserviciosfinancieros")
        )
        if page is None:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
        _ensure_logged_in(page, rut, password, deadline)
        try:
            _read_card(page, result)  # navigates the authenticated tab in place
        except BciLiderSessionError:
            # A late route-guard redirect bounced us to /login mid-read: sign in
            # and retry once (belt-and-braces with _ensure_logged_in's probe).
            logger.info("BciLider: session bounced mid-read; re-logging in and retrying")
            _login_via_autofill(page, rut, password, time.monotonic() + _LOGIN_WAIT_S)
            result = BciLiderCardResult()
            _read_card(page, result)
        # Do NOT close the browser here: in reuse mode it's the user's live Chrome;
        # in managed mode _scrape_managed_sync terminates the process it launched.
    return result


def _scrape_managed_sync(rut: Optional[str], password: Optional[str]) -> BciLiderCardResult:
    """Launch a real Chrome, scrape (logging in via autofill), then close it.

    Fully unattended: no long-running Chrome to keep alive. Chrome must be a
    genuine, headed browser (Cloudflare blocks headless), so this needs a machine
    with a display. The dedicated profile persists across runs, so Cloudflare's
    clearance cookie can carry over and speed up the next sign-in.
    """
    # Fail before opening a Chrome window on a broken deploy.
    if importlib.util.find_spec("playwright") is None:
        raise ImportError("playwright is not installed. Run: pip install -r requirements.txt")
    port = _cdp_port()
    # A Chrome already on this port+profile would make our launch forward to it and
    # exit, so we'd scrape (and then fail to close) someone else's Chrome. Refuse
    # instead: the operator can close it or reuse it via LIDER_BCI_CDP_URL.
    if _port_in_use(port):
        raise BciLiderSessionError(
            f"Port {port} is already in use. Close the Chrome on it, or set "
            f"LIDER_BCI_CDP_URL=http://localhost:{port} to reuse that Chrome."
        )
    cdp_url = f"http://localhost:{port}"
    # Prefer an off-screen window (no flash); if that misbehaves, fall back once to a
    # normal visible window, which is the most reliable. `_terminate_chrome` waits
    # for exit, so the port is free again before the fallback launch.
    try:
        return _scrape_managed_once(port, cdp_url, rut, password, offscreen=True)
    except Exception as exc:
        logger.warning(
            "BciLider: off-screen Chrome scrape failed (%s); retrying with a "
            "visible window", exc,
        )
    return _scrape_managed_once(port, cdp_url, rut, password, offscreen=False)


def _scrape_managed_once(
    port: int, cdp_url: str, rut: Optional[str], password: Optional[str], offscreen: bool,
) -> BciLiderCardResult:
    """Launch Chrome, scrape, and terminate it (one managed attempt)."""
    proc = _launch_real_chrome(port, LOGIN_URL, offscreen=offscreen)
    try:
        return _scrape_over_cdp(cdp_url, rut, password)
    finally:
        _terminate_chrome(proc)


async def scrape_card(
    rut: Optional[str] = None, password: Optional[str] = None,
    cdp_url: Optional[str] = None,
) -> BciLiderCardResult:
    """Read the card's balances + movements by driving a real Chrome over CDP.

    Two modes:
      * reuse: when a CDP URL is given (`cdp_url` arg or LIDER_BCI_CDP_URL env), it
        connects to an already-running Chrome and leaves it open (attended, or a
        Chrome you manage separately);
      * managed (default): it launches a headed Chrome, signs in via autofill,
        scrapes, and closes it, so scheduled runs are fully unattended (needs a
        machine with a display; Cloudflare blocks headless).
    Either way the login autofill is the only path past the portal's Cloudflare
    check. Raises `ImportError` if Playwright isn't installed and
    `BciLiderSessionError` when Chrome is unreachable or sign-in can't complete. A
    flaky movements leg after a valid session is a warning on the result instead.
    """
    import asyncio

    cdp = cdp_url if cdp_url is not None else os.environ.get(CDP_URL_ENV)
    rut = rut or os.environ.get("LIDER_BCI_RUT")
    password = password or os.environ.get("LIDER_BCI_PASSWORD")
    loop = asyncio.get_event_loop()
    if cdp:
        return await loop.run_in_executor(None, _scrape_over_cdp, cdp, rut, password)
    return await loop.run_in_executor(None, _scrape_managed_sync, rut, password)
