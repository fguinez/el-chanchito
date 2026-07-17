"""Self-contained Banco de Chile web session (login + balance read).

Banco de Chile exposes no public/open-banking API for individuals, and the
`fintself` library we use for *transactions* only returns `MovementModel`s —
never an account balance (fintself#… — see issue #27). So to give `banchile`
real, refreshable balances we log in ourselves with Playwright and read the
figures off the post-login "Mis Productos" dashboard — CLP/USD checking and the
credit-card cupo — plus the card total cupo/límite/utilizado (and masked last4),
the línea de crédito, and one product per depósito a plazo / fondo mutuo read
off their listing pages and detail asides (issue #36; see the scope note before
`_PRODUCT_ROWS` for what's covered and how).

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
import datetime
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from product_model import (
    CheckingMetrics,
    CreditCardAttributes,
    CreditCardMetrics,
    InvestmentAttributes,
    InvestmentMetrics,
    LineOfCreditMetrics,
    TermDepositAttributes,
    TermDepositMetrics,
)

from scrapers.base import ScrapedProduct

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
# Retry/backoff for the product surfaces (dashboard + the three detail routes).
# The portal intermittently serves slow pages (issue #35), so each surface gets
# _SURFACE_ATTEMPTS tries with escalating budgets: the figures arrive via late
# XHRs, and each render budget bounds a 1s poll of the page text. Between
# attempts we pause, then recover to the portal home so every retry starts from
# a known state.
_SURFACE_ATTEMPTS = 3
_RENDER_TIMEOUTS_MS = (30_000, 45_000, 60_000)   # per-attempt render/poll budget
_CARD_LINK_TIMEOUTS_MS = (6_000, 9_000, 12_000)  # per-attempt card shortcut budget
_ASIDE_CLICK_TIMEOUTS_MS = (6_000, 9_000, 12_000)    # per-attempt VER DETALLE budget
_ASIDE_RENDER_TIMEOUTS_MS = (10_000, 15_000, 20_000)  # per-attempt aside render/poll
_RETRY_PAUSES_MS = (2_000, 4_000)                # pause before attempt 2 / attempt 3

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
    "[aria-label='Cerrar']",
    "[aria-label='cerrar']",
    ".modal-close",
    ".close-modal",
    "button[data-dismiss='modal']",
    ".modal button:has-text('×')",
]


class BanChileWebError(RuntimeError):
    """Raised when the BdC web session can't log in or reach the balance."""


@dataclass(frozen=True)
class BalanceFetchResult:
    """What a BdC session scraped, plus which surfaces never yielded a product.

    Every surface on this account always carries ≥1 product, so a surface that
    stays empty after all its retries lands in `failed_surfaces` — a subset of
    ("dashboard", "card", "línea", "depósitos", "fondos") — and the institution
    scraper turns those into run warnings instead of silently losing figures.
    """

    products: list[ScrapedProduct]
    failed_surfaces: tuple[str, ...]


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
# Scope (see `scrape_products` in institutions/banchile.py; issues #8, #30):
# Read straight off the dashboard, all CLP unless noted:
#   • checking (CLP + USD) — asset; CLP also feeds the planning "real balance"
#                            drift. USD is summed separately (`_usd_checking...`).
#   • credit_card (CLP)  — stores the *available cupo* (the planning drift relies
#                          on that); the total límite comes from the card detail
#                          page (below), which supersedes this entry so net-worth
#                          debt = límite − available.
#
# Read off their own detail pages (best-effort, non-fatal — see the navigation
# section), once #30 made net worth currency- and cupo-aware:
#   • credit_card (CLP + USD) — available cupo + total cupo/límite + the
#     bank-reported "Utilizado" per currency (`card_saldos_from_text`), plus the
#     masked card number's last4, so a card contributes real debt.
#   • line_of_credit (CLP)    — available + authorized cupo + "Monto utilizado"
#     (`linea_saldo...`), stored like a card so net-worth debt prefers the
#     reported utilizado and falls back to autorizado − disponible. Emitted
#     only with the cupo, else the available would be miscounted as debt.
#   • term_deposit (CLP) — depósitos a plazo, asset. One product per deposit
#     (issue #36): the identity (N° Depósito) and every figure come from each
#     deposit's detail aside on the "Mis Depósitos" listing; they aren't on the
#     "Mis Productos" dashboard at all.
#   • investment (CLP)   — fondos mutuos, asset. One product per fund, read off
#     the "Mis Fondos" listing cards (the detail aside only enriches).
# USD balances convert to CLP via lib/rates' multi-currency FX (api/planning,
# api/wealth, api/institutions).

# A Chilean-formatted amount: 1.234.567 with optional ,dd decimals.
_AMT = r"([\d.]{1,15}(?:,\d{1,2})?)"

# family -> compiled "header … [label] $amount" row. Order is stable (drives the
# emitted/logged order). "Disponible" is coupled tightly to "$" for the account
# families so a USD figure ("Disponible\nUSD 0,00") is skipped. Depósitos a plazo
# and fondos mutuos aren't on this dashboard; they're read per holding from their
# own listing routes instead (see the inversiones per-holding section below).
_PRODUCT_ROWS: list[tuple[str, "re.Pattern[str]"]] = [
    ("checking", re.compile(r"cuenta\s+corriente.{0,60}?disponible\s*\$\s?" + _AMT, re.I | re.S)),
    ("credit_card", re.compile(r"tarjetas?\s+de\s+cr[eé]dito.{0,120}?disponible\s*\$\s?" + _AMT, re.I | re.S)),
]

# Fallback for the dedicated "Saldos y Movimientos" checking view, which labels
# the figure "Saldo Disponible" (spendable) / "Saldo Contable" (accounting).
# Tried in order so the spendable figure wins when both are present.
_SALDO_PATTERNS = [
    re.compile(r"saldo\s+disponible.{0,40}?\$\s?" + _AMT, re.I | re.S),
    re.compile(r"saldo\s+contable.{0,40}?\$\s?" + _AMT, re.I | re.S),
]

# --- Credit-card detail page ("Saldos y movimientos no facturados") -----------
# The "Mis Productos" dashboard only shows an unreliable placeholder for the
# card, so the real figures come from the per-card route (reached via the
# "Saldos y Mov. Tarjetas Crédito" shortcut → #/tarjeta-credito/consultar/saldos;
# label/route confirmed by live QA). That page has a CLP "Nacional" and a USD
# "Internacional" section, each with "Utilizado", "Disponible" and "Cupo total":
#     Nacional, al 12/07/2026
#     Utilizado      $ 400.000
#     Disponible     $ 3.600.000
#     Cupo total     $ 4.000.000
# We read "Disponible" (-> metrics.available), "Cupo total" (-> metrics.limit)
# and "Utilizado" (-> metrics.owed). The reported Utilizado is the bank's own
# debt figure and net worth prefers it; it sits slightly below límite −
# disponible because pending holds consume cupo without being owed yet, and
# límite − disponible stays the fallback when it doesn't parse (issues #30/#34).
# The two currencies are read from separate slices of the page (split on the
# "Internacional" header) so a figure is never paired with the other cupo.
_CARD_INTERNACIONAL_RE = re.compile(r"internacional", re.I)
_CLP_CUPO_RE = re.compile(r"cupo\s+total\s*\$\s?" + _AMT, re.I)
_CLP_DISPONIBLE_RE = re.compile(r"disponible\s*\$\s?" + _AMT, re.I)
_CLP_UTILIZADO_RE = re.compile(r"utilizado\s*\$\s?" + _AMT, re.I)
_USD_CUPO_RE = re.compile(r"cupo\s+total\s*USD\s?" + _AMT, re.I)
_USD_DISPONIBLE_RE = re.compile(r"disponible\s*USD\s?" + _AMT, re.I)
_USD_UTILIZADO_RE = re.compile(r"utilizado\s*USD\s?" + _AMT, re.I)
# The card header prints the masked card number ("Titular Visa Signature
# ****0000"); its tail becomes attributes.last4 — one value for the whole card.
_MASKED_NUMBER_RE = re.compile(r"\*{4}(\d{4})")

# USD cuenta corriente on the dashboard: same block as the CLP one but the
# figure is "Disponible USD …". Coupled to "USD" so it never grabs a CLP "$".
_USD_CHECKING_RE = re.compile(
    r"cuenta\s+corriente.{0,60}?disponible\s*USD\s?" + _AMT, re.I | re.S
)

# --- Línea de crédito detail page ("Saldos y movimientos de la línea") ---------
# Labels "Monto autorizado" (total cupo -> metrics.limit), "Saldo disponible"
# (-> metrics.available) and "Monto utilizado" (-> metrics.owed, the bank's own
# debt figure, preferred by net worth with autorizado − disponible as the
# fallback). Stored like a card; only emitted when the cupo is present, or the
# available would be miscounted as debt (issue #30).
_LINEA_AUTORIZADO_RE = re.compile(r"monto\s+autorizado.*?\$\s?" + _AMT, re.I | re.S)
_LINEA_DISPONIBLE_RE = re.compile(r"saldo\s+disponible.*?\$\s?" + _AMT, re.I | re.S)
_LINEA_UTILIZADO_RE = re.compile(r"monto\s+utilizado.*?\$\s?" + _AMT, re.I | re.S)

# --- Inversiones per-holding pages (issue #36) ---------------------------------
# Depósitos a plazo and fondos mutuos aren't on the "Mis Productos" dashboard;
# they live under the inversiones SPA. Until issue #36 we read only the "Resumen
# de Inversión" summary totals (one summed product per kind) because clicking
# through every holding was judged too fragile; issue #35's bounded retries and
# recovery reversed that call, so each listing route is now parsed per holding:
#
#   • depósitos ("mis-depositos" route): one card per deposit. The card lacks
#     the deposit number and the Monto Inicial, so each card's "VER DETALLE"
#     aside is REQUIRED; it carries the identity ("N° Depósito") and the figures.
#   • fondos ("mis-fondos" route): one card per fund with the name, "Serie
#     <token>", "Lo que he invertido:", "Variación histórica:" (signed) and
#     "Mi saldo" (the nav). The "Acerca del fondo" aside only enriches the
#     variation percentages and is best-effort: a failure never costs the fund.
#
# The listing headers keep the old summed totals ("Saldo Depósitos en Pesos" /
# "SALDO TOTAL EN FONDOS MUTUOS", equal to the resumen totals the retired code
# scraped); they cross-check the cards and, on the final attempt only, feed a
# roll-up fallback shaped exactly like the retired summed products (see V013;
# the DB writer keeps the two representations mutually exclusive).
#
# Hazards (from the redacted capture behind issue #36): "$" noise sits above
# both listings (DISPONIBLE PARA INVERTIR, INDICADORES ECONÓMICOS, and on the
# fondos page a FONDO RECOMENDADO marketing block with its own fund name and a
# near-miss "VER DETALLES" button), so cards are anchored strictly after the
# "MIS DEPÓSITOS A PLAZO" / "MIS FONDOS MUTUOS" section headers and stop at the
# cross-sell/legal blocks. The fondos page also prints the customer's full name
# on its "Cuentas de Inversión:" line; no parser below ever captures, persists,
# or logs it.
_DEPOSITOS_TOTAL_RE = re.compile(r"saldo\s+dep[oó]sitos\s+en\s+pesos\s*\$\s?" + _AMT, re.I)
_DEPOSITOS_CANTIDAD_RE = re.compile(r"\bcantidad\s+(\d+)\b", re.I)
_DEPOSITOS_CARDS_START_RE = re.compile(r"mis\s+dep[oó]sitos\s+a\s+plazo", re.I)
_DEPOSITOS_CARDS_END_RE = re.compile(r"te\s+puede\s+interesar", re.I)
# One listing card: status chip, "Depósito a Plazo" title, then a label/value
# stack. Only "Monto a recibir" is required; the stack tolerates missing labels.
_DEPOSITO_CARD_RE = re.compile(
    r"dep[oó]sito\s+a\s+plazo"
    r"(?:\s+tipo\s+de\s+dep[oó]sito\s+(?P<tipo>[^\n]*\S))?"
    r"(?:\s+vencimiento\s+(?P<venc>\d{1,2}/\d{1,2}/\d{4}))?"
    r"\s+monto\s+a\s+recibir\s+\$\s?(?P<monto>[\d.]{1,15})",
    re.I,
)
# The deposit detail aside, anchored on its title. The "N° Depósito" line keeps
# label and value together (an unbroken digit run, 17 digits observed).
_DEPOSITO_ASIDE_ANCHOR_RE = re.compile(r"detalle\s+del\s+dep[oó]sito\s+a\s+plazo", re.I)
_DEPOSITO_NUMERO_RE = re.compile(r"n\s*[°º]\s*dep[oó]sito\s*:?\s*(\d{6,})", re.I)
_DEPOSITO_RECIBIR_RE = re.compile(r"monto\s+a\s+recibir\s*\$\s?" + _AMT, re.I)
_DEPOSITO_INICIAL_RE = re.compile(r"monto\s+inicial\s*\$\s?" + _AMT, re.I)
_DEPOSITO_VENCIMIENTO_RE = re.compile(r"fecha\s+de\s+vencimiento\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
_DEPOSITO_EMISION_RE = re.compile(r"fecha\s+emisi[oó]n\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
_DEPOSITO_TIPO_RE = re.compile(r"tipo\s+de\s+dep[oó]sito\s+([^\n]*\S)", re.I)
_DEPOSITO_MONEDA_RE = re.compile(r"tipo\s+de\s+moneda\s+([A-Za-z]{2,10})", re.I)
_DEPOSITO_PLAZO_RE = re.compile(r"\bplazo\s+(\d+\s*d[ií]as)", re.I)
_DEPOSITO_TASA_PERIODO_RE = re.compile(r"tasa\s+per[ií]odo\s+([^\n%]*%)", re.I)

_FONDOS_TOTAL_RE = re.compile(
    r"saldo\s+total\s+en\s+fondos\s+mutuos.{0,60}?\$\s?" + _AMT, re.I | re.S
)
_FONDOS_CARDS_START_RE = re.compile(r"mis\s+fondos\s+mutuos", re.I)
_FONDOS_CARDS_END_RE = re.compile(r"revisa\s+nuestra\s+completa\s+oferta", re.I)
# One listing card: a free-form fund-name line immediately above "Serie <token>"
# (no colon; the aside variant "Serie: <token>" can't match), then the amounts.
# "Variación histórica:" is signed; the page prints a leading space or "$ -".
_FONDO_CARD_RE = re.compile(
    r"^[ \t]*(?P<name>[^\n]*\S)[ \t]*\n"
    r"\s*serie[ \t]+(?P<serie>\S+)[ \t]*\n"
    r"(?:\s*lo\s+que\s+he\s+invertido:\s+\$\s?(?P<invertido>[\d.]{1,15})[ \t]*\n)?"
    r"(?:\s*variaci[oó]n\s+hist[oó]rica:\s+(?P<variacion>-?[ \t]*\$[ \t]*-?[\d.]{1,15})[ \t]*\n)?"
    r"\s*mi\s+saldo\s+\$\s?(?P<saldo>[\d.]{1,15})",
    re.I | re.M,
)
# The fund detail aside. Its anchor is deliberately case-SENSITIVE: the fondos
# marketing block renders an all-caps "ACERCA DEL FONDO" button, while the real
# aside title is "Acerca del fondo"; losing the (optional) enrichment on a case
# drift is safer than reading the recommended-fund marketing as a holding.
_FONDO_ASIDE_ANCHOR_RE = re.compile(r"Acerca del fondo")
_FONDO_ASIDE_NAME_RE = re.compile(r"Acerca del fondo\s+([^\n]*\S)")
_FONDO_ASIDE_SERIE_RE = re.compile(r"\bserie:\s*([^\n]*\S)", re.I)
_FONDO_VAR_DIARIA_RE = re.compile(r"var\.?\s*diaria\s+([+-]?[\d.,]+\s*%)", re.I)
_FONDO_VAR_30D_RE = re.compile(r"var\.?\s*30\s*d[ií]as\s+([+-]?[\d.,]+\s*%)", re.I)
_FONDO_VAR_YTD_RE = re.compile(r"acumulada\s+a[ñn]o\s+([+-]?[\d.,]+\s*%)", re.I)

# Percent values arrive as "0,45%", "+1,2345%" (signed, 4 decimals) or
# "12,34 %" (space before the sign); comma is always the decimal separator.
_PCT_VALUE_RE = re.compile(r"([+-]?\d{1,3}(?:\.\d{3})*(?:,\d+)?)\s*%")
_TERM_DAYS_RE = re.compile(r"(\d+)\s*d[ií]as", re.I)
_DDMMYYYY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


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


def _parse_pct(raw: Optional[str]) -> Optional[float]:
    """Parse a Chilean-formatted percent ("0,45%", "+1,2345%", "12,34 %").

    Comma is the decimal separator and a sign, when printed, precedes the
    number. Returns the percent number (4.2 for 4.2%), or None when no percent
    is present; a missing label must yield None, never an exception.
    """
    if not raw:
        return None
    match = _PCT_VALUE_RE.search(raw)
    if match is None:
        return None
    return float(match.group(1).replace(".", "").replace(",", "."))


def _parse_date_ddmmyyyy(raw: Optional[str]) -> Optional[datetime.date]:
    """Parse a "DD/MM/YYYY" date; None when absent or not a real date."""
    if not raw:
        return None
    match = _DDMMYYYY_RE.search(raw)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _parse_term_days(raw: Optional[str]) -> Optional[int]:
    """Parse a "NN días" term into its day count; None when absent."""
    if not raw:
        return None
    match = _TERM_DAYS_RE.search(raw)
    return int(match.group(1)) if match else None


def _section(text: str, start_re: "re.Pattern[str]", end_re: "re.Pattern[str]") -> str:
    """Slice `text` between the first `start_re` match and the next `end_re`.

    The anchor that turns a whole listing page into just its holdings region:
    marketing/indicator "$" noise above the section header and cross-sell,
    glossary and appended aside text below the end marker never reach the card
    regexes. Empty string when the start anchor is missing.
    """
    start = start_re.search(text)
    if start is None:
        return ""
    tail = text[start.end():]
    end = end_re.search(tail)
    return tail[: end.start()] if end else tail


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
    """Read a card's cupos per currency off the detail page.

    Returns e.g. ``{"CLP": {"available": 3600000.0, "limit": 4000000.0,
    "owed": 400000.0}, "USD": {"available": 1950.0, "limit": 2000.0,
    "owed": 50.0}}`` — a currency appears only when its "Disponible" figure
    parses; ``limit`` (the "Cupo total") and ``owed`` (the bank-reported
    "Utilizado") are attached when they parse too. Returns ``{}`` when nothing
    parses.
    """
    if not text:
        return {}

    intl = _CARD_INTERNACIONAL_RE.search(text)
    clp_section = text[: intl.start()] if intl else text
    usd_section = text[intl.start() :] if intl else ""

    result: dict[str, dict[str, float]] = {}
    for currency, section, cupo_re, disponible_re, utilizado_re in (
        ("CLP", clp_section, _CLP_CUPO_RE, _CLP_DISPONIBLE_RE, _CLP_UTILIZADO_RE),
        ("USD", usd_section, _USD_CUPO_RE, _USD_DISPONIBLE_RE, _USD_UTILIZADO_RE),
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
        utilizado = utilizado_re.search(section)
        if utilizado is not None:
            owed = parse_amount(utilizado.group(1))
            if owed is not None:
                entry["owed"] = owed
        result[currency] = entry
    return result


def card_last4_from_text(text: Optional[str]) -> Optional[str]:
    """The card's last four digits from the masked number ("****0000"), if shown.

    One shared value for the whole card — the CLP "Nacional" and USD
    "Internacional" slices belong to the same plastic. Returns None when no
    masked number is on the page (recording nothing beats guessing).
    """
    if not text:
        return None
    match = _MASKED_NUMBER_RE.search(text)
    return match.group(1) if match else None


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
    """Read a línea de crédito's cupos off its detail page.

    Returns ``{"available": 80000.0, "limit": 100000.0, "owed": 20000.0}``
    (``limit``/``owed`` only when "Monto autorizado" / "Monto utilizado" parse),
    or None when the "Saldo disponible" isn't found.
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
    utilizado = _LINEA_UTILIZADO_RE.search(text)
    if utilizado is not None:
        owed = parse_amount(utilizado.group(1))
        if owed is not None:
            entry["owed"] = owed
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


def dashboard_balances_from_text(text: Optional[str]) -> list[ScrapedProduct]:
    """Shape the "Mis Productos" dashboard text into ScrapedProducts.

    Emits the CLP checking figure plus the USD cuenta corriente when present. The
    card's *total* cupo/límite, the línea, and the per-holding depósitos/fondos
    live on their own detail pages, read separately (see `card_balances_from_text`
    / `linea_balances_from_text` / `build_depositos_products` /
    `build_fondos_products`).
    """
    by_kind = balances_by_kind(text)
    usd_checking = _usd_checking_from_text(text)
    if not by_kind and usd_checking is None:
        logger.warning("BanChile: no balances found on page")
        return []

    balances: list[ScrapedProduct] = []
    for kind, _pattern in _PRODUCT_ROWS:  # stable, source-ordered
        # The dashboard's card "Disponible" is a static placeholder ($999.999 /
        # USD 1.234,00 — live QA confirmed it never matches the real available
        # cupo on the card detail page). Sourcing the card only from its detail
        # page (`card_balances_from_text`) avoids writing that placeholder — or,
        # worse, feeding it into the planning cupo drift.
        if kind == "credit_card":
            continue
        if kind in by_kind:
            amount = by_kind[kind]
            logger.info("BanChile %s balance: $%s CLP", kind, f"{amount:,}")
            balances.append(
                ScrapedProduct(
                    institution="banchile",
                    kind=kind,
                    currency="CLP",
                    metrics=CheckingMetrics(balance=amount),
                )
            )
    if usd_checking is not None:
        logger.info("BanChile checking balance: USD %s", f"{usd_checking:,.2f}")
        balances.append(
            ScrapedProduct(
                institution="banchile",
                kind="checking",
                currency="USD",
                metrics=CheckingMetrics(balance=usd_checking),
            )
        )
    return balances


def balances_from_page(page) -> list[ScrapedProduct]:
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


def card_balances_from_text(text: Optional[str]) -> list[ScrapedProduct]:
    """Card ScrapedProducts (CLP + USD) from the card detail page text.

    Available cupo -> `metrics.available`, total cupo -> `metrics.limit`, and
    the bank-reported "Utilizado" -> `metrics.owed` (net worth prefers it,
    falling back to límite − available). The masked number's last4 rides along
    as `attributes.last4`, one shared value for both currencies. Every field
    beyond `available` is optional — a currency missing its límite/utilizado
    still emits (debt 0, like a dashboard-only scrape). Returns [] when nothing
    parses.
    """
    last4 = card_last4_from_text(text)
    attributes = CreditCardAttributes(last4=last4) if last4 is not None else None
    balances: list[ScrapedProduct] = []
    for currency, data in card_saldos_from_text(text).items():
        limit = data.get("limit")
        owed = data.get("owed")
        logger.info(
            "BanChile credit_card balance: %s %s (limit %s, owed %s)",
            currency,
            f"{data['available']:,.2f}",
            f"{limit:,.2f}" if limit is not None else "—",
            f"{owed:,.2f}" if owed is not None else "—",
        )
        balances.append(
            ScrapedProduct(
                institution="banchile",
                kind="credit_card",
                currency=currency,
                attributes=attributes,
                metrics=CreditCardMetrics(
                    available=data["available"], limit=limit, owed=owed
                ),
            )
        )
    return balances


def linea_balances_from_text(text: Optional[str]) -> list[ScrapedProduct]:
    """Línea de crédito ScrapedProduct from its detail page text.

    The reported "Monto utilizado" rides along as `metrics.owed` when it parses
    (optional — net worth falls back to límite − available without it). Only
    emitted when the authorized cupo is present: without it, net worth would
    treat the *available* balance as the amount owed (issue #30). Returns []
    when the cupo or the available figure is missing.
    """
    entry = linea_saldo_from_text(text)
    if entry is None or "limit" not in entry:
        return []
    owed = entry.get("owed")
    logger.info(
        "BanChile line_of_credit balance: $%s CLP (limit $%s, owed %s)",
        f"{entry['available']:,.0f}",
        f"{entry['limit']:,.0f}",
        f"${owed:,.0f}" if owed is not None else "—",
    )
    return [
        ScrapedProduct(
            institution="banchile",
            kind="line_of_credit",
            currency="CLP",
            metrics=LineOfCreditMetrics(
                available=entry["available"], limit=entry["limit"], owed=owed
            ),
        )
    ]


def depositos_header_from_text(text: Optional[str]) -> dict:
    """The depósitos listing header: CLP total and deposit count.

    Returns ``{"total": <Saldo Depósitos en Pesos>, "cantidad": <Cantidad>}``
    with None for whichever didn't parse. The total equals the resumen's "En
    depósitos y ahorros" (the figure the retired summed product carried) and
    `cantidad` cross-checks the parsed cards.
    """
    result: dict = {"total": None, "cantidad": None}
    if not text:
        return result
    total = _DEPOSITOS_TOTAL_RE.search(text)
    if total is not None:
        result["total"] = parse_clp(total.group(1))
    cantidad = _DEPOSITOS_CANTIDAD_RE.search(text)
    if cantidad is not None:
        result["cantidad"] = int(cantidad.group(1))
    return result


def deposito_cards_from_text(text: Optional[str]) -> list[dict]:
    """The depósitos listing cards, anchored after "MIS DEPÓSITOS A PLAZO".

    Each card is ``{"tipo_deposito", "vencimiento", "monto_a_recibir"}`` (None
    for missing labels). Cards size the aside click loop and cross-check the
    header's Cantidad; the identity and the balance are aside-only, so nothing
    here is enough to emit a product. Stops at "Te puede interesar:" so the
    cross-sell teasers, the glossary ("Monto a Recibir: ..."), and any appended
    aside text are never read as cards.
    """
    if not text:
        return []
    region = _section(text, _DEPOSITOS_CARDS_START_RE, _DEPOSITOS_CARDS_END_RE)
    return [
        {
            "tipo_deposito": match.group("tipo"),
            "vencimiento": _parse_date_ddmmyyyy(match.group("venc")),
            "monto_a_recibir": parse_clp(match.group("monto")),
        }
        for match in _DEPOSITO_CARD_RE.finditer(region)
    ]


def deposito_aside_from_text(text: Optional[str]) -> Optional[dict]:
    """Parse the LAST "Detalle del Depósito a Plazo" aside block in `text`.

    Opening an aside appends its text to the end of body.innerText, so the
    last anchored block is the freshest whether the SPA accumulates asides or
    replaces them. Every field is optional (None when its label is missing);
    the caller decides what an aside without `numero`/`monto_inicial` means.
    Returns None when no aside block is present at all.
    """
    if not text:
        return None
    anchors = list(_DEPOSITO_ASIDE_ANCHOR_RE.finditer(text))
    if not anchors:
        return None
    block = text[anchors[-1].start():]

    def value(pattern: "re.Pattern[str]") -> Optional[str]:
        match = pattern.search(block)
        return match.group(1) if match else None

    return {
        "numero": value(_DEPOSITO_NUMERO_RE),
        "monto_inicial": parse_clp(value(_DEPOSITO_INICIAL_RE)),
        "monto_a_recibir": parse_clp(value(_DEPOSITO_RECIBIR_RE)),
        "fecha_vencimiento": _parse_date_ddmmyyyy(value(_DEPOSITO_VENCIMIENTO_RE)),
        "fecha_emision": _parse_date_ddmmyyyy(value(_DEPOSITO_EMISION_RE)),
        "tipo_deposito": value(_DEPOSITO_TIPO_RE),
        "tipo_moneda": value(_DEPOSITO_MONEDA_RE),
        "plazo_dias": _parse_term_days(value(_DEPOSITO_PLAZO_RE)),
        "tasa_periodo_pct": _parse_pct(value(_DEPOSITO_TASA_PERIODO_RE)),
    }


def fondos_header_from_text(text: Optional[str]) -> Optional[int]:
    """The fondos listing header total (SALDO TOTAL EN FONDOS MUTUOS), CLP int.

    Equals the sum of the cards' "Mi saldo" and the resumen's "En activos
    financieros" (the figure the retired summed product carried). None when it
    doesn't parse.
    """
    if not text:
        return None
    match = _FONDOS_TOTAL_RE.search(text)
    return parse_clp(match.group(1)) if match else None


def fondo_cards_from_text(text: Optional[str]) -> list[dict]:
    """The fondos listing cards, anchored after "MIS FONDOS MUTUOS".

    Each card is ``{"name", "serie", "invertido", "variacion", "saldo"}``;
    "Mi saldo" (the nav) is required, the other figures may be None. Anchoring
    matters here: the FONDO RECOMENDADO marketing block above the section
    header carries a fund name that must never become a holding, and the page
    ends in cross-sell/legal noise (stopped at "Revisa nuestra completa
    oferta"). The "Cuentas de Inversión:" line above the header carries the
    customer's full name; it is outside the region and never captured.
    """
    if not text:
        return []
    region = _section(text, _FONDOS_CARDS_START_RE, _FONDOS_CARDS_END_RE)
    cards: list[dict] = []
    for match in _FONDO_CARD_RE.finditer(region):
        saldo = parse_clp(match.group("saldo"))
        if saldo is None:
            continue
        cards.append(
            {
                "name": match.group("name").strip(),
                "serie": match.group("serie"),
                "invertido": parse_clp(match.group("invertido")),
                "variacion": parse_clp(match.group("variacion")),
                "saldo": saldo,
            }
        )
    return cards


def fondo_aside_from_text(text: Optional[str]) -> Optional[dict]:
    """Parse the LAST "Acerca del fondo" aside block in `text` (best-effort).

    Yields ``{"fund_name", "serie", "var_daily_pct", "var_30d_pct",
    "var_ytd_pct"}`` with None for missing fields; the variations are signed
    percent numbers ("+1,2345%" -> 1.2345). The caller matches the block back
    to its card by fund name + serie. The anchor is case-sensitive so the
    marketing block's all-caps "ACERCA DEL FONDO" button never reads as an
    aside. Returns None when no aside block is present.
    """
    if not text:
        return None
    anchors = list(_FONDO_ASIDE_ANCHOR_RE.finditer(text))
    if not anchors:
        return None
    block = text[anchors[-1].start():]

    def value(pattern: "re.Pattern[str]") -> Optional[str]:
        match = pattern.search(block)
        return match.group(1) if match else None

    name = value(_FONDO_ASIDE_NAME_RE)
    return {
        "fund_name": name.strip() if name else None,
        "serie": value(_FONDO_ASIDE_SERIE_RE),
        "var_daily_pct": _parse_pct(value(_FONDO_VAR_DIARIA_RE)),
        "var_30d_pct": _parse_pct(value(_FONDO_VAR_30D_RE)),
        "var_ytd_pct": _parse_pct(value(_FONDO_VAR_YTD_RE)),
    }


def _deposito_product(aside: dict) -> ScrapedProduct:
    """One typed term_deposit from a complete aside (numero + Monto Inicial)."""
    numero = aside["numero"]
    return ScrapedProduct(
        institution="banchile",
        kind="term_deposit",
        currency=aside.get("tipo_moneda") or "CLP",
        external_ref=numero,
        name=f"Depósito a Plazo {numero[-4:]}",
        attributes=TermDepositAttributes(
            issue_date=aside.get("fecha_emision"),
            maturity_date=aside.get("fecha_vencimiento"),
            term_days=aside.get("plazo_dias"),
            interest_rate_pct=aside.get("tasa_periodo_pct"),
            deposit_type=aside.get("tipo_deposito"),
            principal=aside["monto_inicial"],
            maturity_value=aside.get("monto_a_recibir"),
        ),
        metrics=TermDepositMetrics(balance=aside["monto_inicial"]),
    )


def build_depositos_products(
    text: Optional[str], asides: list[dict], attempt: int
) -> list[ScrapedProduct]:
    """Shape the depósitos listing + its per-deposit asides into ScrapedProducts.

    Identity is each aside's "N° Depósito" (external_ref) and the balance
    convention is ``balance = Monto Inicial``: the portal shows no per-deposit
    current value anywhere, and its own aggregate ("Saldo Depósitos en Pesos",
    which also equals the resumen's "En depósitos y ahorros" total the retired
    summed product used) is consistent with the sum of Monto Inicial, so this
    preserves continuity with the retired roll-up. "Monto a recibir" is the
    FUTURE value at vencimiento and is deliberately NOT the balance (it rides
    along as the `maturity_value` attribute); nothing is prorated.

    Degradation ladder (`attempt` is 0-based; the last attempt relaxes):
      1. Non-final attempts demand a fully consistent page: cards == the
         header's Cantidad, one complete aside (id + Monto Inicial) per card,
         all ids unique. Anything less returns [] so the surface retries.
      2. The final attempt emits whatever per-holding products were parsed,
         provided every collected aside is complete and unique (an incomplete
         aside means money we can't identify, which the roll-up covers better).
      3. Failing that, it falls back to ONE summed product shaped exactly like
         the retired legacy roll-up (no external_ref/name, header total as the
         balance); the writer keeps it mutually exclusive with per-holding rows.
      4. Else []: the surface lands in failed_surfaces (issue #35 machinery).
    Per-holding and roll-up shapes are never mixed in one result.
    """
    header = depositos_header_from_text(text)
    cards = deposito_cards_from_text(text)
    final = attempt >= _SURFACE_ATTEMPTS - 1
    complete = [
        a for a in asides if a.get("numero") and a.get("monto_inicial") is not None
    ]
    numeros = {a["numero"] for a in complete}
    all_complete = len(complete) == len(asides) and len(numeros) == len(complete)

    strict = (
        bool(cards)
        and header["cantidad"] is not None
        and len(cards) == header["cantidad"]
        and len(asides) == len(cards)
        and all_complete
    )
    # The final attempt relaxes the header's Cantidad cross-check, but per-holding
    # products still have to cover EVERY card: a subset would drop deposits whose
    # aside never rendered, and (once the writer retires the roll-up) silently
    # undercount net worth. Partial coverage falls through to the roll-up, which
    # preserves the exact total.
    covers_all = bool(cards) and len(complete) == len(cards) and all_complete
    if strict or (final and covers_all):
        products = [_deposito_product(aside) for aside in complete]
        for product in products:
            logger.info(
                "BanChile term_deposit …%s balance: $%s CLP",
                product.external_ref[-4:],
                f"{product.metrics.balance:,}",
            )
        return products
    if not final:
        logger.info(
            "BanChile: depósitos parse incomplete (%d cards, %d asides, "
            "cantidad %s); will retry",
            len(cards),
            len(asides),
            header["cantidad"],
        )
        return []
    if header["total"] is not None:
        logger.warning(
            "BanChile: per-deposit parse unusable; falling back to the summed "
            "term_deposit roll-up ($%s CLP)",
            f"{header['total']:,}",
        )
        return [
            ScrapedProduct(
                institution="banchile",
                kind="term_deposit",
                currency="CLP",
                metrics=TermDepositMetrics(balance=header["total"]),
            )
        ]
    return []


def build_fondos_products(
    text: Optional[str], asides: list[dict], attempt: int
) -> list[ScrapedProduct]:
    """Shape the fondos listing cards (+ best-effort aside vars) into products.

    external_ref is ``"<fund name>|<serie>"`` (no fund registry id appears
    anywhere on the portal); nav/deposited/profit come from the CARD ("Mi
    saldo" / "Lo que he invertido:" / signed "Variación histórica:") so every
    fund shares one freshness basis even when an aside was read. Asides only
    enrich `var_daily_pct`/`var_30d_pct`/`var_ytd_pct` and are matched back by
    fund name + serie; a missing or mismatched aside just leaves those None.

    Degradation ladder: non-final attempts require the cards' "Mi saldo" sum
    to equal the header's SALDO TOTAL EN FONDOS MUTUOS exactly (else [] and
    the surface retries); the final attempt emits the parsed cards anyway,
    falls back to ONE summed roll-up (header total as nav, no external_ref or
    name: the retired legacy shape) when no card parsed, and returns [] when
    even the header is gone. Per-holding and roll-up shapes are never mixed.
    """
    cards = fondo_cards_from_text(text)
    total = fondos_header_from_text(text)
    final = attempt >= _SURFACE_ATTEMPTS - 1
    if cards:
        if not final and (
            total is None or sum(card["saldo"] for card in cards) != total
        ):
            logger.info(
                "BanChile: fondos cards (%d) do not add up to the header total; "
                "will retry",
                len(cards),
            )
            return []
        vars_by_key = {(a.get("fund_name"), a.get("serie")): a for a in asides}
        products: list[ScrapedProduct] = []
        for card in cards:
            enrich = vars_by_key.get((card["name"], card["serie"]), {})
            logger.info(
                "BanChile investment %s (serie %s) nav: $%s CLP",
                card["name"],
                card["serie"],
                f"{card['saldo']:,}",
            )
            products.append(
                ScrapedProduct(
                    institution="banchile",
                    kind="investment",
                    currency="CLP",
                    external_ref=f"{card['name']}|{card['serie']}",
                    name=card["name"],
                    attributes=InvestmentAttributes(fund_name=card["name"]),
                    metrics=InvestmentMetrics(
                        nav=card["saldo"],
                        deposited=card["invertido"],
                        profit=card["variacion"],
                        var_daily_pct=enrich.get("var_daily_pct"),
                        var_30d_pct=enrich.get("var_30d_pct"),
                        var_ytd_pct=enrich.get("var_ytd_pct"),
                    ),
                )
            )
        return products
    if not final:
        return []
    if total is not None:
        logger.warning(
            "BanChile: per-fund parse unusable; falling back to the summed "
            "investment roll-up ($%s CLP)",
            f"{total:,}",
        )
        return [
            ScrapedProduct(
                institution="banchile",
                kind="investment",
                currency="CLP",
                metrics=InvestmentMetrics(nav=total),
            )
        ]
    return []


def _merge_balances(
    base: list[ScrapedProduct], extra: list[ScrapedProduct]
) -> list[ScrapedProduct]:
    """Overlay `extra` onto `base`, keyed by (kind, currency, external_ref).

    A detail-page reading (card cupo, línea) supersedes the dashboard's entry for
    the same product, so a card gets its `limit` and we never write the same
    product twice; per-holding products (each with its own external_ref) never
    clobber each other. Order is preserved: surviving base entries, then `extra`.
    """
    if not extra:
        return base
    replaced = {(b.kind, b.currency, b.external_ref) for b in extra}
    merged = [b for b in base if (b.kind, b.currency, b.external_ref) not in replaced]
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


def _nth_visible(page, selectors: list[str], index: int, timeout: int = 5000):
    """Wait for the index-th match of the first workable selector; None if none.

    The per-holding listings repeat the same control once per card (the
    deposits listing even reuses one id for every VER DETALLE button), so the
    i-th card's button is addressed positionally. Splits the budget across
    selectors like `_first_visible`.
    """
    per_selector = max(1000, timeout // len(selectors)) if selectors else timeout
    for selector in selectors:
        try:
            element = page.locator(selector).nth(index)
            element.wait_for(state="visible", timeout=per_selector)
            return element
        except Exception:
            continue
    return None


def _click_nth(page, selectors: list[str], index: int, timeout: int = 5000) -> bool:
    element = _nth_visible(page, selectors, index, timeout)
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


def _wait_for_balances(page, timeout_ms: int) -> list[ScrapedProduct]:
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
# the "Mis Productos" dashboard. Each surface below is read through
# `_read_surface_with_retries` — bounded attempts with escalating budgets,
# recovering to the portal home in between — and stays *non-fatal*: a surface
# that never parses is reported in `failed_surfaces` and leaves the other
# surfaces untouched, so a drift in this markup can't cost us the
# checking/depósito/fondos figures.
#
# Hard-won from live QA: the dashboard is littered with *marketing* "Tarjeta de
# Crédito" links pointing at the public site (sitiospublicos.bancochile.cl), so a
# broad `tarjeta-credito` selector clicks one of those and navigates the tab off
# the authenticated portal — losing the card AND the línea (its hash then lands
# on the wrong origin). The card's own "Ver saldos" button only toggles a dashboard
# placeholder open; the figures we need are behind the "Saldos y Mov. Tarjetas
# Crédito" shortcut, which routes in-app to #/tarjeta-credito/consultar/saldos.
# So we use that shortcut, fence every read with `_ensure_on_portal`, and treat a
# click that escapes the portal as a miss and recover from it.
_PORTAL_HOST = "portalpersonas.bancochile.cl"
_PORTAL_HOME = (
    "https://portalpersonas.bancochile.cl/mibancochile-web/front/persona/index.html#/home"
)
_LINEA_ROUTE = "#/movimientos/linea/saldos-movimientos/"
_CARD_LINK_SELECTORS = [
    'button:has-text("SALDOS Y MOV.TARJETAS")',
    'a:has-text("SALDOS Y MOV.TARJETAS")',
    'a[href*="tarjeta-credito/consultar/saldos"]',
]
# The card detail page prints "Cupo total" for each of the Nacional/Internacional
# sections — a reliable "the figures have rendered" signal.
_CARD_READY_RE = re.compile(r"cupo\s+total", re.I)
_LINEA_READY_RE = re.compile(r"monto\s+autorizado", re.I)
# The two inversiones listings are static hash routes, driven like the línea
# (confirmed live: the SPA routes to them on hash assignment, no clicks needed).
# Each has rendered once its section header or header total is on the page.
_DEPOSITOS_ROUTE = "#/inversion/depositos-a-plazo/consultar/mis-depositos"
_DEPOSITOS_READY_RE = re.compile(
    r"mis\s+dep[oó]sitos\s+a\s+plazo|saldo\s+total\s+de\s+mis\s+dep[oó]sitos", re.I
)
_FONDOS_ROUTE = "#/inversion/fondos-mutuos/consultar/mis-fondos"
_FONDOS_READY_RE = re.compile(
    r"mis\s+fondos\s+mutuos|saldo\s+total\s+en\s+fondos\s+mutuos", re.I
)
# Every holding card pairs its safe "VER DETALLE" with transactional controls
# (OPERAR; the deposits page adds SIMULAR and the fund aside ends in REINVERTIR/
# RESCATAR/APORTAR), and the fondos marketing block has a near-miss "VER
# DETALLES" button. So the click targets are exact: the ids observed in the
# captured DOM first (the deposits listing reuses one id for every card; the
# fondos ids embed a per-fund token), then an exact-text fallback (`:text-is`,
# never `:has-text`) that cannot match "Ver detalles".
_DEPOSITO_DETALLE_SELECTORS = [
    "button#id-ver-detalle-dap",
    'button:text-is("Ver detalle")',
]
_FONDO_DETALLE_SELECTORS = [
    'button[id^="id-card"][id*="-ver-detalle-"]',
    'button:text-is("Ver detalle")',
]
# The aside is an Angular Material dialog with a dedicated close button (from
# the captured DOM); Escape is the standard mat-dialog fallback.
_ASIDE_CLOSE_SELECTORS = [
    "button[bch-modal-close]",
    "button.bch-modal-header-buttons-close",
    '[aria-label="Cerrar cuadro de diálogo"]',
]


def _on_portal(page) -> bool:
    """True while the tab is still on the authenticated portal origin."""
    try:
        return _PORTAL_HOST in (page.url or "")
    except Exception:
        return False


def _ensure_on_portal(page) -> bool:
    """Guarantee the tab is back on the portal home, reloading it if it escaped.

    Cookies live on the browser context, so re-navigating restores the
    authenticated dashboard. Returns True once the portal is loaded.
    """
    if _on_portal(page):
        return True
    logger.warning("BanChile: off portal (%s); returning to portal home", page.url)
    try:
        page.goto(_PORTAL_HOME, timeout=LOGIN_TIMEOUT, wait_until="domcontentloaded")
        return _on_portal(page)
    except Exception:
        logger.exception("BanChile: could not return to portal home")
        return False


def _wait_for_text(page, ready_re: "re.Pattern[str]", timeout_ms: int) -> Optional[str]:
    """Poll the page's visible text until `ready_re` matches; return it or None.

    SPA route changes swap content in via XHR, so the target figures aren't there
    the instant the route changes — poll like `_wait_for_balances` does. Bails
    early if the tab wanders off the portal (a stray navigation).
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not _on_portal(page):
            return None
        try:
            text = page.evaluate(_INNER_TEXT_JS)
        except Exception:
            text = ""
        if text and ready_re.search(text):
            return text
        page.wait_for_timeout(1000)
    return None


def _recover_to_home(page) -> None:
    """Reset to the portal home so the next attempt starts from a known state.

    Re-assigning an unchanged `window.location.hash` is a no-op, so the
    hash-routed surfaces need the route reset before they can be retried — and
    the card's dashboard shortcut needs the dashboard back. Cookies live on the
    browser context, so the navigation stays authenticated.
    """
    try:
        page.goto(_PORTAL_HOME, timeout=LOGIN_TIMEOUT, wait_until="domcontentloaded")
    except Exception:
        logger.exception("BanChile: could not return to portal home")


def _budget(values: tuple[int, ...], index: int) -> int:
    """`values[index]`, clamped so attempts beyond the tuple reuse its last budget."""
    return values[min(index, len(values) - 1)]


def _read_surface_with_retries(page, surface: str, read: Callable) -> list[ScrapedProduct]:
    """Run one surface's reader with bounded retries and escalating budgets.

    An attempt succeeds iff it parses ≥1 product — every surface on this
    account always has one, so a rendered-but-empty parse is portal slowness
    worth retrying (issue #35). Between attempts: a pause (through the page
    clock, which keeps the tests' MagicMock seam), then a recovery to the
    portal home. Exceptions never escape a surface; a mid-attempt crash just
    consumes that attempt.
    """
    for attempt in range(_SURFACE_ATTEMPTS):
        if attempt:
            page.wait_for_timeout(_budget(_RETRY_PAUSES_MS, attempt - 1))
            _recover_to_home(page)
        try:
            products = read(page, attempt)
        except Exception:
            logger.exception(
                "BanChile: %s surface read failed (attempt %d/%d)",
                surface,
                attempt + 1,
                _SURFACE_ATTEMPTS,
            )
            continue
        if products:
            return products
    logger.warning(
        "BanChile: %s surface failed after %d attempts", surface, _SURFACE_ATTEMPTS
    )
    return []


def _read_dashboard(page, attempt: int) -> list[ScrapedProduct]:
    """Poll the "Mis Productos" dashboard and parse its balances (one attempt)."""
    return _wait_for_balances(page, _budget(_RENDER_TIMEOUTS_MS, attempt))


def _read_card_detail(page, attempt: int) -> list[ScrapedProduct]:
    """Click through to the card detail page and read its cupos (one attempt).

    Fenced so a stray click can't strand us off-portal: if the click escapes the
    authenticated origin we recover to the portal home and fail the attempt.
    """
    if not _ensure_on_portal(page):
        return []
    if not _click_first(page, _CARD_LINK_SELECTORS, timeout=_budget(_CARD_LINK_TIMEOUTS_MS, attempt)):
        logger.info(
            "BanChile: card saldos link not found (attempt %d/%d)",
            attempt + 1,
            _SURFACE_ATTEMPTS,
        )
        return []
    if not _on_portal(page):
        logger.warning(
            "BanChile: card link left the portal; recovering (attempt %d/%d)",
            attempt + 1,
            _SURFACE_ATTEMPTS,
        )
        _ensure_on_portal(page)
        return []
    text = _wait_for_text(page, _CARD_READY_RE, _budget(_RENDER_TIMEOUTS_MS, attempt))
    if text is None:
        logger.info(
            "BanChile: card detail page did not render (attempt %d/%d)",
            attempt + 1,
            _SURFACE_ATTEMPTS,
        )
        return []
    return card_balances_from_text(text)


def _read_linea_detail(page, attempt: int) -> list[ScrapedProduct]:
    """Open the línea detail route and read its authorized cupo (one attempt).

    The route is static, so we drive the SPA straight to it via a hash change (a
    reload/`page.goto` to a same-document fragment wouldn't re-route Angular). We
    first make sure we're on the portal so the hash lands on the right origin.
    """
    if not _ensure_on_portal(page):
        return []
    page.evaluate("route => { window.location.hash = route; }", _LINEA_ROUTE)
    text = _wait_for_text(page, _LINEA_READY_RE, _budget(_RENDER_TIMEOUTS_MS, attempt))
    if text is None:
        logger.info(
            "BanChile: línea detail page did not render (attempt %d/%d)",
            attempt + 1,
            _SURFACE_ATTEMPTS,
        )
        return []
    return linea_balances_from_text(text)


def _close_aside(page) -> None:
    """Best-effort close of an open detail aside (close button, else Escape).

    The aside must be gone before the next card's VER DETALLE is clicked, or
    the next read could parse the previous holding's block; a close that
    silently fails is caught by the caller's freshness check instead.
    """
    if _click_first(page, _ASIDE_CLOSE_SELECTORS, timeout=2000):
        return
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _wait_for_aside(page, extract: Callable, timeout_ms: int) -> Optional[dict]:
    """Poll the page's visible text until `extract(text)` yields a value.

    Opening an aside never changes the URL; its text is APPENDED to the end of
    body.innerText once its XHR lands, so this polls like `_wait_for_text`.
    Always reads at least once, even on a zero budget, so a synchronously
    rendered aside is never missed. None on timeout or if the tab escapes the
    portal.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if not _on_portal(page):
            return None
        try:
            text = page.evaluate(_INNER_TEXT_JS)
        except Exception:
            text = ""
        if text:
            result = extract(text)
            if result is not None:
                return result
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(1000)


def _collect_asides(
    page,
    count: int,
    selectors: list[str],
    parse: Callable,
    accept: Callable,
    attempt: int,
) -> list[dict]:
    """Open, parse and close the VER DETALLE aside of each of `count` cards.

    `parse(text)` extracts the LAST aside block in the page text (correct
    whether the SPA accumulates aside text or replaces it) and `accept(index,
    aside, collected)` decides whether that block belongs to card `index`
    (deposits: an id not collected yet; fondos: the card's own name + serie).
    A stale/duplicate aside is closed and re-clicked ONCE, then that holding
    is given up on. Failures only skip holdings; the caller's sanity checks
    decide what an incomplete collection means.
    """
    collected: list[dict] = []
    for index in range(count):

        def fresh(text: str, _index: int = index) -> Optional[dict]:
            aside = parse(text)
            if aside is not None and accept(_index, aside, collected):
                return aside
            return None

        for round_ in range(2):
            if not _click_nth(
                page, selectors, index, _budget(_ASIDE_CLICK_TIMEOUTS_MS, attempt)
            ):
                break
            aside = _wait_for_aside(
                page, fresh, _budget(_ASIDE_RENDER_TIMEOUTS_MS, attempt)
            )
            _close_aside(page)
            if aside is not None:
                collected.append(aside)
                break
            if round_ == 0:
                logger.debug(
                    "BanChile: aside %d stale or unreadable; re-clicking once", index
                )
    return collected


def _read_depositos_detail(page, attempt: int) -> list[ScrapedProduct]:
    """Open the depósitos listing and read one product per deposit (one attempt).

    The listing is a static hash route (driven like the línea). Each deposit's
    identity and figures live only in its VER DETALLE aside, so the cards just
    size the click loop and cross-check the header's Cantidad; the decision of
    what to emit is `build_depositos_products`'s (pure, unit-tested).
    """
    if not _ensure_on_portal(page):
        return []
    page.evaluate("route => { window.location.hash = route; }", _DEPOSITOS_ROUTE)
    text = _wait_for_text(page, _DEPOSITOS_READY_RE, _budget(_RENDER_TIMEOUTS_MS, attempt))
    if text is None:
        logger.info(
            "BanChile: depósitos listing did not render (attempt %d/%d)",
            attempt + 1,
            _SURFACE_ATTEMPTS,
        )
        return []
    cards = deposito_cards_from_text(text)

    def accept(index: int, aside: dict, collected: list[dict]) -> bool:
        numero = aside.get("numero")
        return bool(numero) and numero not in {a.get("numero") for a in collected}

    asides = _collect_asides(
        page, len(cards), _DEPOSITO_DETALLE_SELECTORS,
        deposito_aside_from_text, accept, attempt,
    )
    return build_depositos_products(text, asides, attempt)


def _read_fondos_detail(page, attempt: int) -> list[ScrapedProduct]:
    """Open the fondos mutuos listing and read one product per fund (one attempt).

    The cards alone carry everything a fund product needs; each card's aside
    is opened best-effort to enrich the variation percentages and is matched
    back by fund name + serie, so an aside failure never costs the fund. The
    decision of what to emit is `build_fondos_products`'s (pure, unit-tested).
    """
    if not _ensure_on_portal(page):
        return []
    page.evaluate("route => { window.location.hash = route; }", _FONDOS_ROUTE)
    text = _wait_for_text(page, _FONDOS_READY_RE, _budget(_RENDER_TIMEOUTS_MS, attempt))
    if text is None:
        logger.info(
            "BanChile: fondos listing did not render (attempt %d/%d)",
            attempt + 1,
            _SURFACE_ATTEMPTS,
        )
        return []
    cards = fondo_cards_from_text(text)

    def accept(index: int, aside: dict, collected: list[dict]) -> bool:
        card = cards[index]
        if aside.get("fund_name") != card["name"]:
            return False
        serie = aside.get("serie")
        return serie is None or serie == card["serie"]

    asides = _collect_asides(
        page, len(cards), _FONDO_DETALLE_SELECTORS,
        fondo_aside_from_text, accept, attempt,
    )
    return build_fondos_products(text, asides, attempt)


# Surface label -> per-attempt reader, in scrape order. The labels feed
# `BalanceFetchResult.failed_surfaces`.
_SURFACE_READERS: list[tuple[str, Callable]] = [
    ("dashboard", _read_dashboard),
    ("card", _read_card_detail),
    ("línea", _read_linea_detail),
    ("depósitos", _read_depositos_detail),
    ("fondos", _read_fondos_detail),
]


def _read_all_surfaces(page) -> BalanceFetchResult:
    """Read every product surface off an authenticated page, with retries.

    This is the seam the retry tests drive with a fake page. A detail reading
    supersedes the dashboard's entry for the same product (a card picks up its
    límite; the depósitos/fondos listings supply the per-holding term_deposit
    and investment products), and a surface with no products after all its
    attempts lands in `failed_surfaces`.
    """
    balances: list[ScrapedProduct] = []
    failed: list[str] = []
    for surface, read in _SURFACE_READERS:
        products = _read_surface_with_retries(page, surface, read)
        if products:
            balances = _merge_balances(balances, products)
        else:
            failed.append(surface)
    return BalanceFetchResult(products=balances, failed_surfaces=tuple(failed))


def _scrape_sync(rut: str, password: str, headless: bool) -> BalanceFetchResult:
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
            return _read_all_surfaces(page)
        finally:
            browser.close()


async def fetch_balances(
    rut: str, password: str, *, headless: bool = True
) -> BalanceFetchResult:
    """Log into Banco de Chile and return its scraped balances.

    Covers the dashboard checking (CLP/USD) plus the card total cupo/límite, the
    línea, and one product per depósito a plazo / fondo mutuo read from the
    inversiones listing pages; surfaces that never yielded a product are reported
    in the result's `failed_surfaces`. Runs the synchronous Playwright flow in a
    thread executor so it doesn't block the scheduler's event loop, mirroring
    backends/fintself.py.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scrape_sync, rut, password, headless)
