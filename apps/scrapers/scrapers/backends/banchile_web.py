"""Self-contained Banco de Chile web session (login + balance read).

Banco de Chile exposes no public/open-banking API for individuals, and the
`fintself` library we use for *transactions* only returns `MovementModel`s —
never an account balance (fintself#… — see issue #27). So to give `banchile`
real, refreshable balances we log in ourselves with Playwright and read the
figures off the post-login "Mis Productos" dashboard — CLP/USD checking,
credit-card cupo, depósitos a plazo and fondos mutuos — plus the card total
cupo/límite and the línea de crédito from their own detail pages (see the scope
note before `_PRODUCT_ROWS` for what's covered and how).

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

# Every figure is anchored on the literal CLP "$" so an account number, a date,
# or a USD amount ("USD 0,00") near a label can't be mistaken for a balance —
# recording nothing ("sin dato") beats recording a wrong figure into net worth.
#
# The post-login "Mis Productos" dashboard renders each holding as a uniform
# block — a product-type header, an id, optional label(s), then the amount:
#     Cuenta Corriente
#     00-000-00000-01
#     Disponible
#     $ 2.500.000
# We map each product family to a `product_kind` and sum every CLP "$" figure
# under that family's header into one product (several cuentas corrientes -> one
# `banchile/checking`; three depósitos a plazo -> one `term_deposit`; ...).
#
# Scope (see `scrape_balances` in institutions/banchile.py; issues #8, #30):
# Read straight off the dashboard, all CLP unless noted:
#   • checking (CLP + USD) — asset; CLP also feeds the planning "real balance"
#                            drift. USD is summed separately (`_usd_checking...`).
#   • credit_card (CLP)  — stores the *available cupo* (the planning drift relies
#                          on that); the total límite comes from the card detail
#                          page (below), which supersedes this entry so net-worth
#                          debt = credit_limit − available.
#   • term_deposit (CLP) — depósito a plazo, asset.
#   • investment (CLP)   — fondos mutuos, asset.
# The depósito/fondos row patterns are inferred from the uniform layout (no live
# fixture yet) and validated against synthetic fixtures, so a layout mismatch
# yields *nothing* rather than a wrong number.
#
# Read off their own detail pages (best-effort, non-fatal — see the navigation
# section), once #30 made net worth currency- and cupo-aware:
#   • credit_card (CLP + USD) — available cupo + total cupo/límite, per currency
#     (`card_saldos_from_text`), so a card contributes real debt.
#   • line_of_credit (CLP)    — available + authorized cupo (`linea_saldo...`),
#     stored like a card so debt = autorizado − disponible = utilizado. Emitted
#     only with the cupo, else the available would be miscounted as debt.
# USD balances convert to CLP via lib/rates' multi-currency FX (api/planning,
# api/wealth, api/institutions).

# A Chilean-formatted amount: 1.234.567 with optional ,dd decimals.
_AMT = r"([\d.]{1,15}(?:,\d{1,2})?)"

# family -> compiled "header … [label] $amount" row. Order is stable (drives the
# emitted/logged order). "Disponible" is coupled tightly to "$" for the account
# families so a USD figure ("Disponible\nUSD 0,00") is skipped; depósito/fondos
# have no reliable label, so they anchor on the header + the nearest CLP "$".
_PRODUCT_ROWS: list[tuple[str, "re.Pattern[str]"]] = [
    ("checking", re.compile(r"cuenta\s+corriente.{0,60}?disponible\s*\$\s?" + _AMT, re.I | re.S)),
    ("credit_card", re.compile(r"tarjetas?\s+de\s+cr[eé]dito.{0,120}?disponible\s*\$\s?" + _AMT, re.I | re.S)),
    ("term_deposit", re.compile(r"dep[oó]sitos?\s+a\s+plazo.{0,80}?\$\s?" + _AMT, re.I | re.S)),
    ("investment", re.compile(r"fondos?\s+mutuos?.{0,80}?\$\s?" + _AMT, re.I | re.S)),
]

# Fallback for the dedicated "Saldos y Movimientos" checking view, which labels
# the figure "Saldo Disponible" (spendable) / "Saldo Contable" (accounting).
# Tried in order so the spendable figure wins when both are present.
_SALDO_PATTERNS = [
    re.compile(r"saldo\s+disponible.{0,40}?\$\s?" + _AMT, re.I | re.S),
    re.compile(r"saldo\s+contable.{0,40}?\$\s?" + _AMT, re.I | re.S),
]

# --- Credit-card detail page ("Saldos y movimientos no facturados") -----------
# The "Mis Productos" dashboard only shows a card's *available* cupo; the total
# límite lives on the per-card detail route (#/tarjeta-credito/consultar/...).
# That page lists a CLP "Cupo Nacional" and a USD "Cupo Internacional", each with
# its Disponible and Utilizado/Usado. We read available (-> current_balance) and
# cupo (-> the credit_limit that makes net-worth debt = limit − available). The
# two currencies are read from separate slices of the page (split on "Cupo
# Internacional") so a "Disponible" is never paired with the other cupo.
_CARD_INTERNACIONAL_RE = re.compile(r"cupo\s+internacional", re.I)
_CLP_CUPO_RE = re.compile(r"cupo\s+nacional.*?\$\s?" + _AMT, re.I | re.S)
_CLP_DISPONIBLE_RE = re.compile(r"disponible.*?\$\s?" + _AMT, re.I | re.S)
_USD_CUPO_RE = re.compile(r"cupo\s+internacional.*?USD\s?" + _AMT, re.I | re.S)
_USD_DISPONIBLE_RE = re.compile(r"disponible.*?USD\s?" + _AMT, re.I | re.S)

# USD cuenta corriente on the dashboard: same block as the CLP one but the
# figure is "Disponible USD …". Coupled to "USD" so it never grabs a CLP "$".
_USD_CHECKING_RE = re.compile(
    r"cuenta\s+corriente.{0,60}?disponible\s*USD\s?" + _AMT, re.I | re.S
)

# --- Línea de crédito detail page ("Saldos y movimientos de la línea") ---------
# Labels "Monto autorizado" (total cupo -> credit_limit), "Saldo disponible"
# (available -> current_balance) and "Monto utilizado" (owed). Stored like a
# card so net-worth debt = autorizado − disponible = utilizado; only emitted when
# the cupo is present, or the available would be miscounted as debt (issue #30).
_LINEA_AUTORIZADO_RE = re.compile(r"monto\s+autorizado.*?\$\s?" + _AMT, re.I | re.S)
_LINEA_DISPONIBLE_RE = re.compile(r"saldo\s+disponible.*?\$\s?" + _AMT, re.I | re.S)


def parse_amount(raw: Optional[str]) -> Optional[float]:
    """Parse a Chilean-formatted CLP/USD amount into a float (keeps decimals).

    Handles ``$1.234.567``, ``USD 2.345,67``, ``$ 1.234.567`` and
    ``1.234.567,00`` (comma = decimal separator). Returns None when there's no
    digit to parse. Used where cents matter (USD cupo); `parse_clp` rounds it to
    whole pesos for CLP.
    """
    if not raw:
        return None
    cleaned = re.sub(r"(?i)CLP|USD|\$|\s", "", raw.strip())
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


def _sum_clp_rows(text: str, pattern: "re.Pattern[str]") -> Optional[int]:
    """Sum every CLP "$" figure `pattern` captures; None when none parse."""
    total = 0
    found = False
    for raw in pattern.findall(text):
        value = parse_clp(raw)
        if value is not None:
            total += value
            found = True
    return total if found else None


def balances_by_kind(text: Optional[str]) -> dict[str, int]:
    """Map each BdC `product_kind` present in `text` to its CLP total.

    Returns ``{}`` when nothing parses. Checking falls back to the dedicated
    "Saldo Disponible/Contable" view when the dashboard block is absent.
    """
    if not text:
        return {}

    result: dict[str, int] = {}
    for kind, pattern in _PRODUCT_ROWS:
        total = _sum_clp_rows(text, pattern)
        if total is not None:
            result[kind] = total

    if "checking" not in result:
        for pattern in _SALDO_PATTERNS:
            match = pattern.search(text)
            if match:
                value = parse_clp(match.group(1))
                if value is not None:
                    result["checking"] = value
                    break
    return result


def _balance_from_text(text: Optional[str]) -> Optional[int]:
    """The CLP checking balance only (kept for callers/tests that want a scalar).

    See `balances_by_kind` for the full per-kind map.
    """
    return balances_by_kind(text).get("checking")


def card_saldos_from_text(text: Optional[str]) -> dict[str, dict[str, float]]:
    """Read a card's available cupo + total límite per currency off the detail page.

    Returns e.g. ``{"CLP": {"available": 3550000.0, "limit": 4000000.0},
    "USD": {"available": 2345.67, "limit": 2400.0}}`` — a currency appears only
    when its *available* (Disponible) figure parses; ``limit`` is attached when
    its cupo does too. Returns ``{}`` when nothing parses.
    """
    if not text:
        return {}

    intl = _CARD_INTERNACIONAL_RE.search(text)
    clp_section = text[: intl.start()] if intl else text
    usd_section = text[intl.start() :] if intl else ""

    result: dict[str, dict[str, float]] = {}
    for currency, section, cupo_re, disponible_re in (
        ("CLP", clp_section, _CLP_CUPO_RE, _CLP_DISPONIBLE_RE),
        ("USD", usd_section, _USD_CUPO_RE, _USD_DISPONIBLE_RE),
    ):
        if not section:
            continue
        disponible = disponible_re.search(section)
        if disponible is None:
            continue
        available = parse_amount(disponible.group(1))
        if available is None:
            continue
        entry: dict[str, float] = {"available": available}
        cupo = cupo_re.search(section)
        if cupo is not None:
            limit = parse_amount(cupo.group(1))
            if limit is not None:
                entry["limit"] = limit
        result[currency] = entry
    return result


def _usd_checking_from_text(text: Optional[str]) -> Optional[float]:
    """Sum every USD "Cuenta Corriente … Disponible USD …" on the dashboard.

    Returns None when there's no USD cuenta corriente (the common case). Kept
    fractional — a USD balance carries cents.
    """
    if not text:
        return None
    total = 0.0
    found = False
    for raw in _USD_CHECKING_RE.findall(text):
        value = parse_amount(raw)
        if value is not None:
            total += value
            found = True
    return total if found else None


def linea_saldo_from_text(text: Optional[str]) -> Optional[dict[str, float]]:
    """Read a línea de crédito's available + authorized cupo off its detail page.

    Returns ``{"available": 100000.0, "limit": 100000.0}`` (``limit`` only when
    "Monto autorizado" parses), or None when the "Saldo disponible" isn't found.
    """
    if not text:
        return None
    disponible = _LINEA_DISPONIBLE_RE.search(text)
    if disponible is None:
        return None
    available = parse_amount(disponible.group(1))
    if available is None:
        return None
    entry: dict[str, float] = {"available": available}
    autorizado = _LINEA_AUTORIZADO_RE.search(text)
    if autorizado is not None:
        limit = parse_amount(autorizado.group(1))
        if limit is not None:
            entry["limit"] = limit
    return entry


# Guarded: right after the post-login redirect `document.body` can still be
# null, and the balances load via a later XHR — so this must not throw.
_INNER_TEXT_JS = "() => (document.body && document.body.innerText) || ''"


def _read_checking(page) -> Optional[int]:
    """Read the page's visible text and extract the CLP checking balance.

    Quiet (no logging) so it's safe to call in a polling loop; returns None
    when the balance isn't present/rendered yet — checking is the "dashboard has
    loaded" signal `_wait_for_balances` polls on.
    """
    try:
        text = page.evaluate(_INNER_TEXT_JS)
    except Exception:
        return None
    return _balance_from_text(text)


def dashboard_balances_from_text(text: Optional[str]) -> list[ScrapedBalance]:
    """Shape the "Mis Productos" dashboard text into ScrapedBalances.

    Emits the CLP checking / credit-card cupo / depósito / fondos figures (in
    stable source order) plus the USD cuenta corriente when present. The card's
    *total* cupo/límite and the línea live on their own detail pages, read
    separately (see `card_balances_from_text` / `linea_balances_from_text`).
    """
    by_kind = balances_by_kind(text)
    usd_checking = _usd_checking_from_text(text)
    if not by_kind and usd_checking is None:
        logger.warning("BanChile: no balances found on page")
        return []

    balances: list[ScrapedBalance] = []
    for kind, _pattern in _PRODUCT_ROWS:  # stable, source-ordered
        if kind in by_kind:
            amount = by_kind[kind]
            logger.info("BanChile %s balance: $%s CLP", kind, f"{amount:,}")
            balances.append(
                ScrapedBalance(
                    institution="banchile",
                    product_kind=kind,
                    balance=amount,
                    as_of=date.today(),
                    currency="CLP",
                )
            )
    if usd_checking is not None:
        logger.info("BanChile checking balance: USD %s", f"{usd_checking:,.2f}")
        balances.append(
            ScrapedBalance(
                institution="banchile",
                product_kind="checking",
                balance=usd_checking,
                as_of=date.today(),
                currency="USD",
            )
        )
    return balances


def balances_from_page(page) -> list[ScrapedBalance]:
    """Read the dashboard balances off an already-authenticated page.

    This is the seam the tests mock: it only reads `page`'s visible text (via
    ``page.evaluate``) and delegates the shaping to `dashboard_balances_from_text`,
    so it can be exercised with a fake page and no real bank. Returns [] on error
    or when nothing is found.
    """
    try:
        text = page.evaluate(_INNER_TEXT_JS)
    except Exception:
        logger.exception("BanChile: could not read page text")
        return []
    return dashboard_balances_from_text(text)


def card_balances_from_text(text: Optional[str]) -> list[ScrapedBalance]:
    """Card ScrapedBalances (CLP + USD) from the card detail page text.

    Available cupo -> `balance`, total cupo -> `credit_limit` (so net-worth debt
    = límite − available). Currencies without a límite still emit (debt 0, like a
    dashboard-only scrape). Returns [] when nothing parses.
    """
    balances: list[ScrapedBalance] = []
    for currency, data in card_saldos_from_text(text).items():
        limit = data.get("limit")
        logger.info(
            "BanChile credit_card balance: %s %s (limit %s)",
            currency,
            f"{data['available']:,.2f}",
            f"{limit:,.2f}" if limit is not None else "—",
        )
        balances.append(
            ScrapedBalance(
                institution="banchile",
                product_kind="credit_card",
                balance=data["available"],
                as_of=date.today(),
                currency=currency,
                credit_limit=limit,
            )
        )
    return balances


def linea_balances_from_text(text: Optional[str]) -> list[ScrapedBalance]:
    """Línea de crédito ScrapedBalance from its detail page text.

    Only emitted when the authorized cupo is present: without it, net worth would
    treat the *available* balance as the amount owed (issue #30). Returns [] when
    the cupo or the available figure is missing.
    """
    entry = linea_saldo_from_text(text)
    if entry is None or "limit" not in entry:
        return []
    logger.info(
        "BanChile line_of_credit balance: $%s CLP (limit $%s)",
        f"{entry['available']:,.0f}",
        f"{entry['limit']:,.0f}",
    )
    return [
        ScrapedBalance(
            institution="banchile",
            product_kind="line_of_credit",
            balance=entry["available"],
            as_of=date.today(),
            currency="CLP",
            credit_limit=entry["limit"],
        )
    ]


def _merge_balances(
    base: list[ScrapedBalance], extra: list[ScrapedBalance]
) -> list[ScrapedBalance]:
    """Overlay `extra` onto `base`, keyed by (product_kind, currency).

    A detail-page reading (card cupo, línea) supersedes the dashboard's entry for
    the same product, so a card gets its `credit_limit` and we never write the
    same product twice. Order is preserved: surviving base entries, then `extra`.
    """
    if not extra:
        return base
    replaced = {(b.product_kind, b.currency) for b in extra}
    merged = [b for b in base if (b.product_kind, b.currency) not in replaced]
    merged.extend(extra)
    return merged


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


# --- Detail-page navigation ---------------------------------------------------
# The card's total cupo and the línea live on their own SPA (hash) routes, off
# the "Mis Productos" dashboard. Each read below is *best-effort and non-fatal*:
# any failure returns [] and leaves the dashboard balances untouched, so a drift
# in this markup can't cost us the checking/depósito/fondos figures. The card's
# saldos route carries a per-card token we don't know up front, so it's reached
# by clicking the card; the línea's route is static, so we drive the SPA to it.
_LINEA_ROUTE = "#/movimientos/linea/saldos-movimientos/"
_CARD_LINK_SELECTORS = [
    'a[href*="tarjeta-credito/consultar/saldos"]',
    'a[href*="tarjeta-credito"]',
    'a:has-text("Tarjeta de Crédito")',
    'a:has-text("Tarjetas de Crédito")',
]
_LINEA_LINK_SELECTORS = [
    'a[href*="linea/saldos-movimientos"]',
    'a[href*="movimientos/linea"]',
    'a:has-text("Línea de Crédito")',
]
_CARD_READY_RE = re.compile(r"cupo\s+(?:nacional|internacional)", re.I)
_LINEA_READY_RE = re.compile(r"monto\s+autorizado", re.I)


def _wait_for_text(page, ready_re: "re.Pattern[str]", timeout_ms: int) -> Optional[str]:
    """Poll the page's visible text until `ready_re` matches; return it or None.

    SPA route changes swap content in via XHR, so the target figures aren't there
    the instant the route changes — poll like `_wait_for_balances` does.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            text = page.evaluate(_INNER_TEXT_JS)
        except Exception:
            text = ""
        if text and ready_re.search(text):
            return text
        page.wait_for_timeout(1000)
    return None


def _read_card_detail(page) -> list[ScrapedBalance]:
    """Click through to the card detail page and read its cupos (best-effort)."""
    try:
        if not _click_first(page, _CARD_LINK_SELECTORS, timeout=6000):
            logger.info("BanChile: card detail link not found; skipping cupo/límite")
            return []
        text = _wait_for_text(page, _CARD_READY_RE, BALANCE_WAIT_TIMEOUT)
        if text is None:
            logger.info("BanChile: card detail page did not render; skipping cupo")
            return []
        return card_balances_from_text(text)
    except Exception:
        logger.exception("BanChile: card detail read failed")
        return []


def _read_linea_detail(page) -> list[ScrapedBalance]:
    """Open the línea detail route and read its authorized cupo (best-effort)."""
    try:
        # Static route: drive the SPA straight to it (a hash change re-routes an
        # already-loaded Angular app; `page.goto` to a same-document fragment
        # wouldn't). Fall back to a menu link if the hash change is swallowed.
        try:
            page.evaluate("route => { window.location.hash = route; }", _LINEA_ROUTE)
        except Exception:
            if not _click_first(page, _LINEA_LINK_SELECTORS, timeout=6000):
                logger.info("BanChile: línea detail link not found; skipping")
                return []
        text = _wait_for_text(page, _LINEA_READY_RE, BALANCE_WAIT_TIMEOUT)
        if text is None:
            logger.info("BanChile: línea detail page did not render; skipping")
            return []
        return linea_balances_from_text(text)
    except Exception:
        logger.exception("BanChile: línea detail read failed")
        return []


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
            balances = _wait_for_balances(page, BALANCE_WAIT_TIMEOUT)
            # Enrich with the per-product detail pages (card cupo/límite, línea).
            # Each is non-fatal; a detail reading supersedes the dashboard's entry
            # for the same product so a card picks up its credit_limit.
            balances = _merge_balances(balances, _read_card_detail(page))
            balances = _merge_balances(balances, _read_linea_detail(page))
            return balances
        finally:
            browser.close()


async def fetch_balances(
    rut: str, password: str, *, headless: bool = True
) -> list[ScrapedBalance]:
    """Log into Banco de Chile and return its scraped balances.

    Covers the dashboard products (CLP/USD checking, card cupo, depósitos,
    fondos) plus the card total cupo/límite and the línea read from their detail
    pages. Runs the synchronous Playwright flow in a thread executor so it
    doesn't block the scheduler's event loop, mirroring backends/fintself.py.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scrape_sync, rut, password, headless)
