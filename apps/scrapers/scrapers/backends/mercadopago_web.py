"""Mercado Pago (Chile) web session: a real Chrome over CDP, read by fetch only.

Why a real Chrome: the login cannot be automated (a reCAPTCHA guards the
e-mail step and a step-up the password step), so a human signs in once, in a
genuine Chrome on a dedicated persistent profile (`make mercadopago-login`).
That profile keeps the session across relaunches, and every run only reuses
it. Playwright attaches over CDP; it never launches the browser and never
touches the login form.

Hard contract (each line is a way the session was or could be lost):

  * the tab opens on `/banking/balance` and is never navigated again;
  * data is read ONLY with same-origin `fetch()` calls made from inside that
    tab, always with `credentials: 'include'` and `redirect: 'manual'`;
  * the in-page script refuses to run off `www.mercadopago.cl` and can only
    fetch the three paths below: Python picks a page by name, never by URL;
  * `/home` is never fetched or opened (it redirects through the auth
    gateway);
  * a redirect is never followed: it means the session is gone, the run
    stops with `MercadoPagoSessionError` and nothing is retried. A human
    signs in again. In managed mode, a session lost before or at the
    balance page also leaves a marker next to the profile
    (`<profile>.needs-sign-in`), and later runs fail at once without opening
    Chrome until a successful `make mercadopago-login` clears it (or the
    file is deleted).

Two modes (`scrape_account`):

  * managed (default): launch Chrome off-screen on the profile, connect, read,
    terminate it. Needs a machine with a display, so not the headless Docker
    container.
  * reuse: when `MERCADOPAGO_CDP_URL` names a Chrome that is already running
    with a signed-in Mercado Pago tab, attach to it and leave it running.

Finding the tab (verified live): a CDP connection made while the tab is still
loading holds it with an empty URL and never learns the real one, for as long
as that connection lives, while Chrome's own target list (`/json/list`, plain
HTTP on the debug port) shows the page with its real URL from the start. So
the target list is the authority on where the tab is, and Playwright attaches
only when it shows a tab on the portal:

  * no tab on the portal but one on another site, on two looks in a row (or
    on the last look): the session is gone. No Playwright connection is made
    at all, so automation never attaches to the sign-in pages;
  * a tab on the portal: attach with a fresh connection. If that connection
    only shows the empty URL it is dropped and, after a growing pause,
    another is made, until one shows the tab; the read runs on that one;
  * a tab on the portal that no connection ever shows, or no tab at all, by
    the end of the wait: a plain error that says which.

`make mercadopago-login` looks the same way.

The pages are server-rendered HTML. Each one carries its data in an inline
script as `_n.ctx.r={...JSON...}` followed by more JavaScript in the same
script, and under that JSON everything lives at `appProps.pageProps`. Three
pages are read (shapes only; nothing here is a real value):

  * `/banking/balance`: `pageProps.initialResult.balance`, whose
    `result.available.amount.raw` is the wallet balance in whole CLP. The
    same `result` has `invested.data`: `title.middle.raw` is the gain of the
    last 12 months (whole CLP; the title reads "Los últimos 12 meses
    obtuviste") and `subtitle` a sentence that
    carries the yearly rate ("... al 1,0% anual", a decimal comma). Both are
    optional: a missing or unreadable one is None and never fails the read.
  * `/savings/hub`: `pageProps.hub.result.potsDataHub.result.pots`, one entry
    per savings pot (`id` a uuid, `name`, `amount.raw` in CLP). The list can
    be empty. Beside it, `hub.result.investmentData.data` holds one `rate`
    (a string like "1.0", a percent) for every pot and an `is_invested`
    flag; the rate is applied to each pot only while that flag is true. No
    per-pot gain is read: `/savings/detail/<uuid>` has a
    `potDetail.investment.profit` that stays 0 while the page shows a gain
    computed in the browser, so it is not the gain.
  * `/banking/balance/movements?limit=100&page=N` (N from 1):
    `pageProps.listData = {total, pages, groups: [{items: [...]}]}`, the
    wallet ledger, newest first. An item carries `movement_id` (unique per
    row), `datetime` (UTC), `amount.fraction` (dotted thousands, a leading
    minus for money out), `amount.cents`, `title` and `description`. The
    page size of 100 is honored and `pages` is recomputed for it.

This ledger is the only one used. `/activities` also lists purchases paid
with other cards where Mercado Pago was only the gateway, and
`/banking/movements` is a seller-style report that splits one operation into
several rows.

Failure policy:

  * the balance page is the session probe, and nothing else is fetched when
    it fails. A redirect, a 401/403 or a tab that is not on the portal raises
    `MercadoPagoSessionError`. A balance page that answers 200 without a
    readable balance (a changed page, a widget that did not load) is a plain
    `MercadoPagoWebError`: it asks nobody to sign in again;
  * once the balance is read, a pots or ledger page that fails (HTTP error,
    timeout, changed shape) is a warning on the result, so the run ends
    `partial` with what was read; a redirect still raises;
  * a missing top-level node raises `MercadoPagoPageError`; one malformed pot
    or ledger row is skipped and counted, but a ledger page whose rows all
    fail raises, since that is a changed page and not a bad row.

Money is parsed with `Decimal` and rounded half up to whole pesos, and
`datetime` is converted to America/Santiago before its date is taken, so an
evening movement stays on its own day.

Bounds: every Playwright call that takes a timeout gets one (the connection
and each in-page fetch), and the reads share one time budget. Copying a
fetch's result out of the page (`json_value`) takes none, so two outer bounds
cover it: a watchdog ends any call still waiting if the drive overruns (in
managed mode it terminates the Chrome it launched; in reuse mode it kills the
Playwright driver, leaving the user's Chrome running), and in both modes the
run gives up on a drive that does not finish in time. A call that fails
because the tab or its connection is gone (the driver crashed, Chrome or the
tab closed, the watchdog fired) stops the read there instead of moving on to
the next page.

Nothing in an exception message comes from a page: they name fields, counts
and HTTP codes only, because they end up in `scraper_runs.error_message`.
"""

import asyncio
import contextlib
import datetime
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from scrapers import sign_in_marker
from scrapers.backends import real_chrome
from scrapers.backends.percent import parse_percent
from scrapers.base import SignInRequired

logger = logging.getLogger(__name__)


LOCAL_TZ = ZoneInfo("America/Santiago")

# --- The portal: one host, three pages ------------------------------------------
PORTAL_HOST = "www.mercadopago.cl"
BALANCE_PATH = "/banking/balance"
POTS_PATH = "/savings/hub"
MOVEMENTS_PATH = "/banking/balance/movements"
# Where the tab opens. Nothing navigates it afterwards.
START_URL = f"https://{PORTAL_HOST}{BALANCE_PATH}"
# Every page the in-page script can fetch, by the name Python asks for it.
ALLOWED_PATHS = {
    "balance": BALANCE_PATH,
    "pots": POTS_PATH,
    "movements": MOVEMENTS_PATH,
}
MOVEMENTS_PAGE_SIZE = 100
DEFAULT_MOVEMENT_PAGES = 1
# Hard cap on the ledger pager, also when asked for every page.
MAX_MOVEMENT_PAGES = 50

# --- Configuration ------------------------------------------------------------
CDP_URL_ENV = "MERCADOPAGO_CDP_URL"
CDP_PORT_ENV = "MERCADOPAGO_CDP_PORT"
CHROME_PATH_ENV = "MERCADOPAGO_CHROME_PATH"
CHROME_PROFILE_ENV = "MERCADOPAGO_CHROME_PROFILE"
# BCI Lider's Chrome owns 9222.
DEFAULT_CDP_PORT = 9223
# apps/scrapers/, resolved from this file so cwd doesn't matter.
_APP_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_DIR = _APP_DIR / ".mercadopago_chrome_profile"

# --- Time bounds --------------------------------------------------------------
# One in-page fetch aborts itself after this long...
FETCH_TIMEOUT_MS = 20_000
# ...and the Playwright call around it gives up a little later.
EVALUATE_TIMEOUT_MS = 30_000
# Chrome gets this long for its debug port to answer (its target list), and
# the first attach may take as long: right after a launch it is slow. Reuse
# mode attaches to a Chrome that is already up, so its port gets a short wait.
CONNECT_WAIT_S = 30
REUSE_CONNECT_WAIT_S = 5
_PORT_POLL_S = 0.5
# Finding the tab. Chrome's target list says where the tab is; Playwright is
# attached only when it is on the portal. A connection made while the tab is
# still loading holds it with an empty URL and never learns the real one
# (verified live), so a connection that does not show the tab within
# `ATTACH_POLL_S` is dropped and a FRESH one is made after a pause (verified
# live to resolve it). The pauses grow, so a slow load costs few drivers.
ATTACH_POLL_S = 3
RECONNECT_WAIT_S = 15
_REATTACH_PAUSES_S = (2.0, 4.0, 6.0, 8.0)
_TAB_POLL_S = 0.5
# Between two looks at the target list when nothing was attached.
_LOOK_PAUSE_S = 2.0
# How long the tab gets to show on the portal and be attached to, counted
# from the moment Chrome's debug port answers.
MANAGED_TAB_WAIT_S = 90
REUSE_TAB_WAIT_S = 20
# An attach begun just before the tab wait ends still gets `_MIN_ATTACH_S` to
# connect; with the driver's start and stop that stays under this overrun.
_MIN_ATTACH_S = 5
ATTACH_OVERRUN_S = 15
# A tab seen on another site, and none on the portal, on this many looks in a
# row means the session is gone; one sighting could be a redirect on its way
# back.
_ELSEWHERE_CONFIRMATIONS = 2
# The target list not answering this many looks in a row, after having
# answered, means Chrome is gone.
_MAX_UNANSWERED_LOOKS = 3
# All reads of one drive share this budget; once spent, the ledger pages not
# yet read are left for the next run.
READ_BUDGET_S = 5 * 60
# Managed mode: the launched Chrome is terminated this long after its launch
# whatever the drive is doing, which ends any Playwright call still waiting.
# It covers the worst case of each phase plus two minutes of margin.
MANAGED_HARD_LIMIT_S = CONNECT_WAIT_S + MANAGED_TAB_WAIT_S + ATTACH_OVERRUN_S + READ_BUDGET_S + 120
# Reuse mode: the same bound on its own waits; the watchdog kills the
# Playwright driver (never the user's Chrome), which ends any call still waiting.
REUSE_HARD_LIMIT_S = (
    REUSE_CONNECT_WAIT_S + REUSE_TAB_WAIT_S + ATTACH_OVERRUN_S + READ_BUDGET_S + 120
)
# Either mode: the run gives up on a drive after this long, so a call stuck on
# a frozen browser cannot hold the scraper service. Later than both watchdogs,
# which end it cleanly. The 60 s past the managed watchdog must cover a whole
# teardown (`real_chrome.TERMINATE_WORST_CASE_S`); a test pins that.
DRIVE_TIMEOUT_S = MANAGED_HARD_LIMIT_S + 60
_TIMED_OUT = "Mercado Pago read did not finish in time."
# Pause between ledger pages.
_PAGE_PAUSE_S = 0.5
# A page far larger than any real one is not parsed.
MAX_HTML_CHARS = 8_000_000
# `make mercadopago-login`: how long the human has, how often Chrome's target
# list is looked at, and how often the balance page is probed while the
# portal tab stays on the same URL.
LOGIN_TIMEOUT_S = 10 * 60
_LOGIN_POLL_S = 2.0
_LOGIN_PROBE_INTERVAL_S = 15.0
# The login cookie that must be persistent before the login closes Chrome.
# Found live: a sign-in whose balance read already passed was closed before
# `ssid` / `orguserid` were set, and the next launch was signed out; another
# sign-in, whose `ssid` was already persistent, survived a SIGTERM close and a
# relaunch. Live, `ssid` sat on `.mercadopago.cl` (also on `.mercadolibre.cl`
# and `.mercadolibre.com`, which this check does not read).
LASTING_SESSION_COOKIE = "ssid"
# Cookie-jar reads that may fail in a row before the wait gives up on Chrome.
_MAX_JAR_READ_FAILURES = 5
# Where Chrome keeps the cookie store inside the profile (newer builds use
# `Network/`), and the portal hosts whose persistent `ssid` row must be there.
# Found live: the jar held a persistent `ssid` and a SIGTERM close seconds
# later left none on disk, so the next launch was signed out. Chromium
# writes the store in batches (its interval is about 30 s; not timed live).
_COOKIE_DB_PATHS = ("Default/Network/Cookies", "Default/Cookies")
_PORTAL_COOKIE_HOSTS = (".mercadopago.cl", "www.mercadopago.cl")
# Chrome stores cookie times as microseconds since 1601-01-01 UTC.
_CHROME_EPOCH_OFFSET_S = 11_644_473_600
# The disk wait gets at least this long after the jar check passes, even past
# the sign-in deadline: a sign-in finished near the deadline still needs one
# batch write.
_DISK_WAIT_FLOOR_S = 60.0
# After Chrome closes, its network service may still hold the store for a
# moment (a lock or a hot journal): the post-close check retries this long.
_POST_CLOSE_DISK_WAIT_S = 10.0
_POST_CLOSE_DISK_POLL_S = 0.5

_SIGN_IN_HINT = (
    "Run `make mercadopago-login` and sign in again (with MERCADOPAGO_CDP_URL "
    "set, sign in in that Chrome and leave its balance page open)."
)
# A Chrome already open on the profile swallows the launch, so the debug port
# never opens: the same symptom as a machine without a display.
_CHROME_START_HINT = (
    "Chrome did not start (a machine with a display is required, and no other "
    "Chrome may be using the Mercado Pago profile)."
)
_REUSE_START_HINT = (
    f"No Chrome answers there. Start it again, or unset {CDP_URL_ENV} to use the managed one."
)
# The port answered and then stopped answering.
_CHROME_GONE_HINT = "Chrome answered before and stopped (was it closed?)."
# The port answers but a CDP connection could not be made.
_CHROME_NO_ATTACH_HINT = "Chrome is up, but Playwright could not attach to it."
# The process this run launched has ended, so its port will never answer.
_CHROME_EXITED_HINT = (
    "It exits at once when another Chrome already uses the Mercado Pago profile "
    "(close that one), and when its window is closed."
)
# No tab at all on the portal, and none on another site either.
_MANAGED_NO_TAB = "Chrome opened no Mercado Pago tab"
_REUSE_NO_TAB = (
    f"No Mercado Pago tab is open in the Chrome at {CDP_URL_ENV}: open the "
    "balance page in that Chrome"
)

# The inline script assigns the page data right after this marker.
CONTEXT_MARKER = "_n.ctx.r="

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Observed as 17 uppercase alphanumerics. Kept looser than that: the id is
# only a dedup key, so any plain token is usable, while a blank or structured
# value is not.
_MOVEMENT_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
# "12.345" or "-12.345": dotted thousands (or plain digits), an optional
# leading minus that is an ASCII hyphen or U+2212 (MINUS SIGN).
_MINUS_SIGNS = "-" + chr(0x2212)
_FRACTION_RE = re.compile(r"^([" + re.escape(_MINUS_SIGNS) + r"]?)\s*(\d{1,3}(?:\.\d{3})+|\d+)$")
_CENTS_RE = re.compile(r"^\d{2}$")
# The rate in "Generando ganancias al 1,0% anual". The whole number must sit
# between "al" and the percent sign, so "1.234,5%" never yields a partial match.
_RATE_IN_TEXT_RE = re.compile(
    r"\bal\s+([" + re.escape(_MINUS_SIGNS) + r"]?\d+(?:[.,]\d+)?)\s*%\s*anual\b",
    re.IGNORECASE,
)


class MercadoPagoWebError(RuntimeError):
    """The Mercado Pago web read failed for a reason other than the session."""


class MercadoPagoPageError(MercadoPagoWebError):
    """One page did not have the shape this module reads."""


class MercadoPagoFetchError(MercadoPagoWebError):
    """One page could not be fetched (HTTP error, timeout, network)."""


class MercadoPagoConnectError(MercadoPagoWebError):
    """The Chrome debug port did not accept a connection in time."""


class MercadoPagoConnectionLostError(MercadoPagoWebError):
    """The connection to the tab ended mid-read; nothing more can be fetched."""


class MercadoPagoSessionError(SignInRequired):
    """The signed-in browser session is missing or gone; a human must sign in.

    Never retried: a second attempt against a lost session is what gets an
    account flagged. Every message names `make mercadopago-login`.

    `after_balance` is set when the balance page had already read, so the
    loss came from the pots or ledger page. That one leaves no needs-sign-in
    marker: the login checks the balance page only, so it could never clear
    a marker those pages keep writing.
    """

    after_balance: bool = False


class MercadoPagoSignInPending(MercadoPagoSessionError):
    """A managed run skipped because an earlier run found the session lost."""


def _session_lost(reason: str) -> MercadoPagoSessionError:
    return MercadoPagoSessionError(f"Mercado Pago session lost ({reason}). {_SIGN_IN_HINT}")


@dataclass(frozen=True)
class Pot:
    """One savings pot: its uuid (lower-case), its name and its CLP balance.

    `rate_pct` is the yearly yield rate as a percent number (1.0 is 1%), None
    when the page did not say. Per-pot gain is not read: no server-side
    field carries the gain the pot page shows.
    """

    id: str
    name: str
    amount: int
    rate_pct: float | None = None


@dataclass(frozen=True)
class WalletYield:
    """What the balance page says the wallet balance earns.

    `rate_pct` is the yearly rate as a percent number (1.0 is 1%) and
    `profit_12m` the gain of the last 12 months in whole CLP; either is None
    when the page did not carry it in a readable form.
    """

    rate_pct: float | None = None
    profit_12m: int | None = None


@dataclass(frozen=True)
class Movement:
    """One wallet ledger row.

    `amount` is signed whole CLP (negative is money out) and `date` is the
    America/Santiago date of the movement.
    """

    movement_id: str
    date: datetime.date
    amount: int
    title: str
    description: str


@dataclass
class MercadoPagoAccount:
    """What one drive read: the wallet balance, the pots and the ledger rows.

    `warnings` flag partial coverage (the pots page or a ledger page failed
    after the balance was read), so the run records `partial`. A failed pots
    page leaves `pots` empty, like an account without pots; `pots_complete`
    tells them apart. It is True only when the pots page answered with a
    success status and no pot was skipped, so `pots` lists every pot the
    account has (an empty list included).
    """

    balance: int
    wallet_yield: WalletYield = field(default_factory=WalletYield)
    pots: list[Pot] = field(default_factory=list)
    pots_complete: bool = False
    movements: list[Movement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- Pure parsers (unit-tested; no browser) -------------------------------------


def extract_context(html: str) -> dict:
    """The page's `_n.ctx.r` object, decoded from its inline script.

    More JavaScript follows the object in the same script, so exactly one JSON
    value is decoded from the marker on. The marker can also appear where no
    data follows (an earlier `_n.ctx.r=_n.ctx.r||{}`, a string, a comment), so
    every occurrence is tried until one decodes to an object carrying
    `appProps`. Raises `MercadoPagoPageError` when there is none.
    """
    if not isinstance(html, str):
        raise MercadoPagoPageError("page is not text")
    decoder = json.JSONDecoder()
    index = html.find(CONTEXT_MARKER)
    if index < 0:
        raise MercadoPagoPageError("page data marker missing")
    decoded_something = False
    while index >= 0:
        start = index + len(CONTEXT_MARKER)
        while start < len(html) and html[start].isspace():
            start += 1
        try:
            value, _ = decoder.raw_decode(html, start)
        except ValueError:
            # Not chained on purpose: the decoder's error keeps the whole page.
            value = None
        else:
            decoded_something = True
        if isinstance(value, dict) and "appProps" in value:
            return value
        index = html.find(CONTEXT_MARKER, index + 1)
    if decoded_something:
        raise MercadoPagoPageError("page data has no appProps")
    raise MercadoPagoPageError("page data is not JSON")


def page_props(context: dict) -> dict:
    """`appProps.pageProps` of a decoded context; raises when it is missing."""
    return _dig(context, "appProps", "pageProps", what="page")


def page_props_from_html(html: str) -> dict:
    """`extract_context` and `page_props` in one step."""
    return page_props(extract_context(html))


def _dig(node: Any, *keys: str, what: str) -> Any:
    """Walk `keys` down nested objects; raise naming the first missing one."""
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise MercadoPagoPageError(f"{what}: {key} missing")
        node = node[key]
    return node


def _require_success(node: Any, what: str) -> None:
    status = node.get("status") if isinstance(node, dict) else None
    if not isinstance(status, dict) or status.get("type") != "success":
        raise MercadoPagoPageError(f"{what}: status is not success")


def _whole_pesos(raw: Any) -> int | None:
    """A JSON number as whole CLP, rounded half up; None when it is not one."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = Decimal(str(raw))
    if not value.is_finite():
        return None
    try:
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:  # too large to be an amount
        return None


def parse_clp_amount(amount: Any) -> int | None:
    """A ledger `amount` (`fraction` plus `cents`) as signed whole CLP.

    `fraction` is the peso part with dotted thousands and the sign ("12.345",
    "-12.345", the minus an ASCII hyphen or U+2212); `cents` is two digits or
    absent. The sum is rounded half up to whole pesos, away from zero for a
    negative. None when either part is not in that form.
    """
    if not isinstance(amount, dict):
        return None
    fraction = amount.get("fraction")
    if not isinstance(fraction, str):
        return None
    match = _FRACTION_RE.match(fraction.strip())
    if match is None:
        return None
    cents = amount.get("cents")
    if cents is None or cents == "":
        cents_value = Decimal(0)
    elif isinstance(cents, str) and _CENTS_RE.match(cents):
        cents_value = Decimal(cents) / 100
    else:
        return None
    value = Decimal(match.group(2).replace(".", "")) + cents_value
    if match.group(1):
        value = -value
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def santiago_date(raw: Any) -> datetime.date | None:
    """The America/Santiago date of an ISO timestamp that carries its offset.

    The ledger stamps movements in UTC ("...Z"); a timestamp without an
    offset is None, since guessing its zone could move it a day.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(LOCAL_TZ).date()


def parse_balance(props: dict) -> int:
    """The wallet balance in whole CLP from the `/banking/balance` page."""
    balance = _dig(props, "initialResult", "balance", what="balance")
    _require_success(balance, "balance")
    raw = _dig(balance, "result", "available", "amount", "raw", what="balance")
    pesos = _whole_pesos(raw)
    if pesos is None:
        raise MercadoPagoPageError("balance: amount is not a number")
    return pesos


def _rate_number(raw: Any) -> float | None:
    """A signed rate ("1,0", "4.50", "-0,5") as a float, or None."""
    value = parse_percent(raw, signed=True)
    return None if value is None else float(value)


def parse_wallet_yield(props: Any) -> WalletYield:
    """The wallet's yearly rate and gain from the `/banking/balance` page.

    Never raises: these are extras next to the balance, so a missing node, a
    gain that is not a number or a subtitle without a rate each leave that
    value None.

    The rate is read only from the earning phrasing "al X% anual": any other
    percent in the sentence ("100% disponible") is skipped, and a promo such
    as "gana hasta 1,0% anual" yields None, since "hasta" is an offer, not
    the rate being earned. Only the invested-wallet subtitle has been seen; the
    wording for a wallet that is not invested is unverified, which is why
    the rule rejects rather than guesses.
    """
    data = props
    for key in ("initialResult", "balance", "result", "invested", "data"):
        data = data.get(key) if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return WalletYield()
    middle = data.get("title")
    for key in ("middle", "raw"):
        middle = middle.get(key) if isinstance(middle, dict) else None
    rate = None
    subtitle = data.get("subtitle")
    if isinstance(subtitle, str):
        match = _RATE_IN_TEXT_RE.search(subtitle)
        if match is not None:
            rate = _rate_number(match.group(1))
    return WalletYield(rate_pct=rate, profit_12m=_whole_pesos(middle))


def parse_pots_rate(props: Any) -> float | None:
    """The savings hub's yearly rate for every pot, or None.

    The hub prints one rate for all pots (`investmentData.data.rate`, a
    string like "1.0") and an `is_invested` flag; the rate counts only while
    that flag is true. Never raises: a missing or malformed node is None.
    """
    data = props
    for key in ("hub", "result", "investmentData", "data"):
        data = data.get(key) if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("is_invested") is not True:
        return None
    raw = data.get("rate")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        raw = str(raw)
    return _rate_number(raw)


def _parse_pot(entry: Any, rate_pct: float | None = None) -> Pot | None:
    if not isinstance(entry, dict):
        return None
    pot_id = entry.get("id")
    if not isinstance(pot_id, str) or not _UUID_RE.match(pot_id.strip()):
        return None
    amount = entry.get("amount")
    if not isinstance(amount, dict) or amount.get("currency_id") != "CLP":
        return None
    pesos = _whole_pesos(amount.get("raw"))
    if pesos is None:
        pesos = parse_clp_amount(amount)
    if pesos is None:
        return None
    name = entry.get("name")
    return Pot(
        id=pot_id.strip().lower(),
        name=name.strip() if isinstance(name, str) else "",
        amount=pesos,
        rate_pct=rate_pct,
    )


def parse_pots(props: dict) -> tuple[list[Pot], int]:
    """The savings pots of the `/savings/hub` page, and how many were skipped.

    An empty list is a real reading (no pots). An entry that is not a CLP pot
    with a uuid and an amount, or that repeats an earlier id, is skipped and
    counted: the caller reports it, because a pot left out is money left out.
    Every pot carries the hub's shared rate (`parse_pots_rate`) and no gain.
    """
    node = _dig(props, "hub", "result", "potsDataHub", what="pots")
    _require_success(node, "pots")
    entries = _dig(node, "result", "pots", what="pots")
    if not isinstance(entries, list):
        raise MercadoPagoPageError("pots: pots is not a list")
    rate_pct = parse_pots_rate(props)
    pots: dict[str, Pot] = {}
    skipped = 0
    for entry in entries:
        pot = _parse_pot(entry, rate_pct)
        if pot is None or pot.id in pots:
            skipped += 1
            continue
        pots[pot.id] = pot
    if skipped:
        logger.warning("MercadoPago: %d of %d pot(s) skipped", skipped, len(entries))
    return list(pots.values()), skipped


# Why a ledger row was left out. All but the last mean the row was not in the
# expected form; the last that it was fine and simply carries no money.
_SKIP_SHAPE = "shape"
_SKIP_ID = "id"
_SKIP_DATE = "date"
_SKIP_AMOUNT = "amount"
_SKIP_CURRENCY = "currency"
_SKIP_TITLE = "title"
_SKIP_ZERO = "zero"


def _parse_movement(item: Any) -> Movement | str:
    """A ledger item as a `Movement`, or the reason it is skipped."""
    if not isinstance(item, dict):
        return _SKIP_SHAPE
    movement_id = item.get("movement_id")
    if not isinstance(movement_id, str) or not _MOVEMENT_ID_RE.match(movement_id):
        return _SKIP_ID
    day = santiago_date(item.get("datetime"))
    if day is None:
        return _SKIP_DATE
    amount = item.get("amount")
    if not isinstance(amount, dict):
        return _SKIP_AMOUNT
    if amount.get("currency_id") != "CLP":
        return _SKIP_CURRENCY
    pesos = parse_clp_amount(amount)
    if pesos is None:
        return _SKIP_AMOUNT
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return _SKIP_TITLE
    if pesos == 0:
        return _SKIP_ZERO
    description = item.get("description")
    return Movement(
        movement_id=movement_id,
        date=day,
        amount=pesos,
        title=title.strip(),
        description=description.strip() if isinstance(description, str) else "",
    )


def parse_movements(props: dict) -> tuple[list[Movement], int, int]:
    """One ledger page: its rows (newest first), the page count, the skips.

    The page count is the one the page reports for the page size it was asked
    with. A row that is not in the expected form (id, date, CLP amount,
    title) is skipped and counted in the third value: the caller reports it,
    because a movement left out is money left out. A row that rounds to zero
    pesos is dropped without counting. The log line counts the skips by
    reason and never quotes a row. A page that lists rows of which none can
    be read raises instead: that is a changed page.
    """
    data = _dig(props, "listData", what="movements")
    pages = data.get("pages") if isinstance(data, dict) else None
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
        raise MercadoPagoPageError("movements: pages is not a count")
    groups = data.get("groups")
    if not isinstance(groups, list):
        raise MercadoPagoPageError("movements: groups is not a list")

    rows: list[Movement] = []
    skipped: Counter[str] = Counter()
    for group in groups:
        items = group.get("items") if isinstance(group, dict) else None
        if not isinstance(items, list):
            skipped[_SKIP_SHAPE] += 1
            continue
        for item in items:
            parsed = _parse_movement(item)
            if isinstance(parsed, Movement):
                rows.append(parsed)
            else:
                skipped[parsed] += 1

    malformed = sum(count for reason, count in skipped.items() if reason != _SKIP_ZERO)
    if malformed and not rows:
        raise MercadoPagoPageError("movements: no row could be read")
    if skipped:
        # Rows that round to zero pesos are routine (a small daily yield).
        logger.log(
            logging.WARNING if malformed else logging.DEBUG,
            "MercadoPago: %d movement row(s) skipped (%s)",
            sum(skipped.values()),
            ", ".join(f"{reason}: {count}" for reason, count in sorted(skipped.items())),
        )
    return rows, pages, malformed


# --- Reading pages from inside the tab ------------------------------------------

# Runs in the signed-in tab. It refuses any other origin, builds the URL itself
# from the fixed table below (Python only names a page), never follows a
# redirect, and returns a `status` plus, on success, the page text. Safe to run
# twice (a plain GET): Playwright may re-run it if the tab's context is swapped.
_FETCH_JS = """
async ({surface, pageNumber, timeoutMs}) => {
  if (location.protocol !== 'https:' || location.host !== %(host)s) {
    return {status: 'off_portal'};
  }
  const paths = %(paths)s;
  if (!Object.prototype.hasOwnProperty.call(paths, surface)) return {status: 'not_allowed'};
  let path = paths[surface];
  if (surface === 'movements') {
    if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > %(max_page)d) {
      return {status: 'not_allowed'};
    }
    path += '?limit=%(limit)d&page=' + pageNumber;
  }
  const signal = typeof AbortSignal.timeout === 'function'
    ? AbortSignal.timeout(timeoutMs) : undefined;
  try {
    const response = await fetch(location.origin + path, {
      method: 'GET', credentials: 'include', redirect: 'manual', signal,
    });
    if (response.type !== 'basic') return {status: 'redirect'};
    if (response.status !== 200) return {status: 'http', code: response.status};
    const html = await response.text();
    if (html.length > %(max_chars)d) return {status: 'too_large'};
    return {status: 'ok', html};
  } catch (error) {
    return {status: error && error.name === 'TimeoutError' ? 'timeout' : 'error'};
  }
}
""" % {
    "host": json.dumps(PORTAL_HOST),
    "paths": json.dumps(ALLOWED_PATHS),
    "max_page": MAX_MOVEMENT_PAGES,
    "limit": MOVEMENTS_PAGE_SIZE,
    "max_chars": MAX_HTML_CHARS,
}

_FETCH_FAILURES = {
    "timeout": "timed out",
    "error": "network error",
    "too_large": "page too large",
    "not_allowed": "page not allowed",
}


def html_from_fetch(result: Any, surface: str) -> str:
    """The page text out of the in-page script's result, or the right error.

    A redirect, a tab that left the portal and a 401/403 mean the session is
    gone (`MercadoPagoSessionError`). Anything else that is not a 200 with
    text is a `MercadoPagoFetchError` naming the page and a status only.
    """
    status = result.get("status") if isinstance(result, dict) else None
    if status == "ok":
        html = result.get("html")
        if isinstance(html, str):
            return html
        raise MercadoPagoFetchError(f"{surface}: no page text")
    if status == "redirect":
        raise _session_lost("the portal redirected to sign in")
    if status == "off_portal":
        raise _session_lost("the tab left the portal")
    if status == "http":
        code = result.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise MercadoPagoFetchError(f"{surface}: HTTP error")
        if code in (401, 403):
            raise _session_lost(f"HTTP {code}")
        raise MercadoPagoFetchError(f"{surface}: HTTP {code}")
    reason = _FETCH_FAILURES.get(status) if isinstance(status, str) else None
    raise MercadoPagoFetchError(f"{surface}: {reason or 'unexpected fetch result'}")


def _check_killed(kill_switch) -> None:
    """Raise `DriverKilledError` once the drive's watchdog has killed the driver.

    After that the first pending Playwright call fails, and before 1.64 any
    later one spun forever, so none may follow.
    """
    if kill_switch is not None:
        kill_switch.raise_if_fired()


def _evaluate_bounded(page, script: str, arg: dict, timeout_ms: int, kill_switch=None) -> Any:
    """Run an async in-page `script` and return its JSON result, bounded; raises.

    `page.evaluate` takes no timeout. `wait_for_function` runs its predicate
    once right away, and when that returns a promise (always truthy) it waits
    for the promise inside its timeout, so the script is bounded. `json_value`
    (copying the settled result out) and `dispose` have no timeout of their
    own; the drive's watchdog and the drive timeout cover that window. A
    failed `json_value` is not followed by `dispose`: the connection may be
    dead, and the handle goes with it.
    """
    _check_killed(kill_switch)
    handle = page.wait_for_function(script, arg=arg, timeout=timeout_ms)
    value = handle.json_value()
    if kill_switch is None or not kill_switch.fired:
        try:
            handle.dispose()
        except Exception:
            pass
    return value


def _fetch_props(page, surface: str, page_number: int = 1, kill_switch=None) -> dict:
    """Fetch one allowed page from inside the tab and return its `pageProps`.

    The in-page script turns every fetch failure into a status, so a call that
    raises is either a page failure to carry on from (`MercadoPagoFetchError`:
    the script outlived its timeout, the tab navigated) or a lost tab or
    connection (`MercadoPagoConnectionLostError`, see `real_chrome.connection_lost`),
    which ends the read: further calls would only fail the same way.
    """
    failure: str | None = None
    lost = False
    result = None
    try:
        result = _evaluate_bounded(
            page,
            _FETCH_JS,
            {"surface": surface, "pageNumber": page_number, "timeoutMs": FETCH_TIMEOUT_MS},
            EVALUATE_TIMEOUT_MS,
            kill_switch,
        )
    except Exception as exc:
        failure = type(exc).__name__
        lost = real_chrome.connection_lost(exc)
    if failure is not None:
        # A killed driver is not a page failure to carry on from.
        _check_killed(kill_switch)
        # Raised outside the handler: the Playwright error (its text carries a
        # call log) is neither the cause nor the context of this one.
        if lost:
            raise MercadoPagoConnectionLostError(
                f"Mercado Pago read lost its connection to the tab ({surface}: {failure})."
            )
        raise MercadoPagoFetchError(f"{surface}: in-page fetch failed ({failure})")
    return page_props_from_html(html_from_fetch(result, surface))


def _read_movements(
    page, account: MercadoPagoAccount, max_pages: int, deadline: float, kill_switch=None
) -> None:
    """Read the ledger into `account`, newest page first.

    `max_pages` pages of `MOVEMENTS_PAGE_SIZE` rows; 0 (or less) reads every
    page the ledger reports, up to `MAX_MOVEMENT_PAGES`. A page that fails
    keeps the pages before it and adds a warning; so does hitting the cap or
    the read budget when every page was asked for. A row seen on two pages
    (the ledger moved between the two fetches) is kept once. Rows a page
    listed but that could not be read add one warning with their count.
    """
    every_page = max_pages <= 0
    limit = MAX_MOVEMENT_PAGES if every_page else min(max_pages, MAX_MOVEMENT_PAGES)
    seen: set[str] = set()
    skipped = 0
    number = 1
    while True:
        if time.monotonic() >= deadline:
            account.warnings.append(
                f"MercadoPago: movements stopped before page {number} (read budget spent)"
            )
            break
        try:
            rows, pages, unread = parse_movements(
                _fetch_props(page, "movements", number, kill_switch)
            )
        except (MercadoPagoPageError, MercadoPagoFetchError) as exc:
            logger.warning("MercadoPago: movements page %d not read: %s", number, exc)
            account.warnings.append(f"MercadoPago: movements page {number} not read ({exc})")
            break
        skipped += unread
        for row in rows:
            if row.movement_id not in seen:
                seen.add(row.movement_id)
                account.movements.append(row)
        if number >= pages:
            break
        if number >= limit:
            if every_page:
                account.warnings.append(
                    f"MercadoPago: movements capped at {limit} of {pages} pages"
                )
            break
        number += 1
        time.sleep(_PAGE_PAUSE_S)
    if skipped:
        account.warnings.append(f"MercadoPago: {skipped} movement row(s) not read")


def read_account(
    page, movement_pages: int = DEFAULT_MOVEMENT_PAGES, kill_switch=None
) -> MercadoPagoAccount:
    """Read balance, pots and ledger from a tab on the portal. Never navigates.

    The balance page goes first and doubles as the session probe. Whatever
    goes wrong with it, nothing else is fetched: a redirect, a 401/403 or a
    tab off the portal is a lost session (`MercadoPagoSessionError`, raised
    by the fetch); a page that answers 200 without a readable balance (a
    changed page, a balance widget that did not load) or that cannot be
    fetched is a plain `MercadoPagoWebError`, which asks nobody to sign in.
    A lost connection (`MercadoPagoConnectionLostError`) ends the read at any
    page; once `kill_switch` has fired, `DriverKilledError` does instead.
    """
    deadline = time.monotonic() + READ_BUDGET_S
    try:
        balance_props = _fetch_props(page, "balance", kill_switch=kill_switch)
        balance = parse_balance(balance_props)
    except MercadoPagoPageError as exc:
        raise MercadoPagoWebError(
            f"Mercado Pago balance page not readable ({exc}): the page changed "
            "or its balance did not load."
        ) from exc
    except MercadoPagoFetchError as exc:
        raise MercadoPagoWebError(f"Mercado Pago balance page not fetched ({exc}).") from exc
    account = MercadoPagoAccount(balance=balance, wallet_yield=parse_wallet_yield(balance_props))

    try:
        try:
            account.pots, skipped = parse_pots(_fetch_props(page, "pots", kill_switch=kill_switch))
        except (MercadoPagoPageError, MercadoPagoFetchError) as exc:
            logger.warning("MercadoPago: savings pots not read: %s", exc)
            account.warnings.append(f"MercadoPago: savings pots not read ({exc})")
        else:
            if skipped:
                account.warnings.append(f"MercadoPago: {skipped} savings pot(s) not read")
            else:
                account.pots_complete = True

        _read_movements(page, account, movement_pages, deadline, kill_switch)
    except MercadoPagoSessionError as exc:
        exc.after_balance = True
        raise
    logger.info(
        "MercadoPago: balance read, %d pot(s), %d movement(s), %d warning(s)",
        len(account.pots),
        len(account.movements),
        len(account.warnings),
    )
    return account


# --- Chrome plumbing ------------------------------------------------------------


def cdp_port() -> int:
    """The debug port of the managed Chrome (`MERCADOPAGO_CDP_PORT`)."""
    raw = os.environ.get(CDP_PORT_ENV)
    if not raw:
        return DEFAULT_CDP_PORT
    if not raw.strip().isdigit():
        raise MercadoPagoWebError(f"{CDP_PORT_ENV} is not a port number")
    return int(raw)


def profile_dir() -> Path:
    """The dedicated Chrome profile that holds the signed-in session."""
    override = os.environ.get(CHROME_PROFILE_ENV)
    return Path(override).expanduser() if override else DEFAULT_PROFILE_DIR


def sign_in_marker_path() -> Path:
    """The marker a lost managed session leaves, next to the profile."""
    profile = profile_dir()
    return profile.with_name(profile.name + ".needs-sign-in")


def on_portal(url: str) -> bool:
    """Whether `url` is an https page of the portal host, exactly."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == PORTAL_HOST


def _watch_for_portal_tab(browser, wait_s: float):
    """Watch one connection for a tab on the portal, for up to `wait_s`.

    Returns the page as soon as one shows, else None: a connection that
    attached while the tab was loading holds it with an empty URL and never
    learns the real one. Local reads only: it never waits on the browser.
    """
    deadline = time.monotonic() + wait_s
    while True:
        page = real_chrome.find_page(browser, on_portal)
        if page is not None or time.monotonic() >= deadline:
            return page
        time.sleep(_TAB_POLL_S)


def _connect(playwright, cdp_url: str, wait_s: float, process=None):
    """One connection attempt with the whole of `wait_s` as its timeout.

    Callers attach only once Chrome's debug port has answered, so there is no
    closed port to retry on, and the first attach after a launch can take
    longer than a short per-attempt timeout would allow. A timeout is
    therefore "Chrome is up but did not attach" (`MercadoPagoConnectError`:
    a later attempt may work), never "Chrome did not start". `process` is the
    Chrome this run launched: once it has ended nothing will ever answer, a
    plain `MercadoPagoWebError`.
    """
    error: MercadoPagoWebError
    try:
        return real_chrome.connect_over_cdp(
            playwright,
            cdp_url,
            deadline=time.monotonic(),
            attempt_timeout_ms=max(1000, int(wait_s * 1000)),
            process=process,
        )
    except real_chrome.ChromeExitedError as exc:
        error = MercadoPagoWebError(f"{exc} {_CHROME_EXITED_HINT}")
    except real_chrome.ChromeUnreachableError as exc:
        error = MercadoPagoConnectError(f"{exc} {_CHROME_NO_ATTACH_HINT}")
    raise error


@contextlib.contextmanager
def _cdp_connection(cdp_url: str, wait_s: float, process=None, kill_switch=None):
    """A fresh Playwright driver attached to the Chrome at `cdp_url`.

    Leaving the block stops the driver, which drops the connection, also when
    the connection fails or the block raises. Nothing is closed in the
    browser: Chrome and its tabs keep running, so the next connection finds
    them as they are. The driver is published on `kill_switch` while it runs.
    """
    from playwright.sync_api import sync_playwright  # lazy: keeps tests browser-free

    manager = sync_playwright()
    if kill_switch is None:
        with manager as playwright:
            yield _connect(playwright, cdp_url, wait_s, process)
        return
    kill_switch.raise_if_fired()
    try:
        with manager as playwright:
            kill_switch.attach(manager)
            yield _connect(playwright, cdp_url, wait_s, process)
    finally:
        # After the driver stopped, so the watchdog can still end a stuck stop.
        kill_switch.detach()


def _look(cdp_url: str) -> real_chrome.TargetSummary | None:
    """What Chrome itself lists as open (one bounded local HTTP request)."""
    return real_chrome.summarize_targets(cdp_url, on_portal)


def _chrome_exited() -> MercadoPagoWebError:
    return MercadoPagoWebError(f"The launched Chrome is no longer running. {_CHROME_EXITED_HINT}")


def _await_chrome(cdp_url: str, wait_s: float, start_hint: str, process=None):
    """Wait for Chrome's debug port to answer; return its first target list.

    A launched Chrome that ends before answering is final (`process`); a port
    that never answers is `MercadoPagoConnectError` with `start_hint`.
    """
    deadline = time.monotonic() + wait_s
    while True:
        summary = _look(cdp_url)
        if summary is not None:
            return summary
        if real_chrome.has_exited(process):
            raise _chrome_exited()
        if time.monotonic() >= deadline:
            raise MercadoPagoConnectError(f"The Chrome debug port did not answer. {start_hint}")
        time.sleep(_PORT_POLL_S)


def _unanswered(unanswered: int, process) -> None:
    """A look that got no answer after Chrome had answered: raise when final."""
    if real_chrome.has_exited(process):
        raise _chrome_exited()
    if unanswered >= _MAX_UNANSWERED_LOOKS:
        raise MercadoPagoConnectError(
            f"Chrome's target list stopped answering. {_CHROME_GONE_HINT}"
        )


def _targets_text(summary: real_chrome.TargetSummary) -> str:
    """Chrome's target types and their counts, for an error message."""
    listed = ", ".join(f"{kind}: {count}" for kind, count in sorted(summary.types.items()))
    return f"Chrome targets: {listed or 'none'}"


def _with_portal_tab[T](
    cdp_url: str,
    action: Callable[[Any], T],
    *,
    connect_wait_s: float,
    tab_wait_s: float,
    start_hint: str,
    no_tab: str,
    process=None,
    kill_switch=None,
) -> T:
    """Find the portal tab and run `action` on it; never navigates.

    Chrome's own target list decides where the tab is (see the module
    docstring), one look every `_LOOK_PAUSE_S`:

      * a tab on another site and none on the portal, on
        `_ELSEWHERE_CONFIRMATIONS` looks in a row or on the last look: a lost
        session. Playwright is never attached in this case;
      * a tab on the portal: attach a fresh connection and watch it for
        `ATTACH_POLL_S`. If it shows the tab, `action(page)` runs on it and
        its result is returned. If not (a connection made during the load
        never learns the URL), or if attaching fails, the connection is
        dropped and the loop looks again after a growing pause;
      * neither: keep looking.

    When `tab_wait_s` (counted from Chrome's first answer) is spent, the last
    look decides the error: the page was there but never attached to, or
    there was no tab (`no_tab` says what to do about it). A target list that
    stops answering, or a launched Chrome (`process`) that ends, ends the
    wait at once. Each connection's driver is published on `kill_switch`.
    """
    summary = _await_chrome(cdp_url, connect_wait_s, start_hint, process)
    deadline = time.monotonic() + tab_wait_s
    attach_wait: float = CONNECT_WAIT_S
    connections = 0
    elsewhere = 0
    unanswered = 0
    while True:
        pause = _LOOK_PAUSE_S
        if summary is None:
            unanswered += 1
            _unanswered(unanswered, process)
        elif summary.matching_pages:
            unanswered = 0
            elsewhere = 0
            remaining = deadline - time.monotonic()
            wait = min(attach_wait, max(_MIN_ATTACH_S, remaining))
            try:
                with _cdp_connection(cdp_url, wait, process, kill_switch) as browser:
                    watch = min(ATTACH_POLL_S, max(0.0, deadline - time.monotonic()))
                    page = _watch_for_portal_tab(browser, watch)
                    if page is not None:
                        if connections:
                            logger.info(
                                "MercadoPago: portal tab attached on connection %d",
                                connections + 1,
                            )
                        return action(page)
            except MercadoPagoConnectError as exc:
                logger.info("MercadoPago: attach %d failed: %s", connections + 1, exc)
                _check_killed(kill_switch)
            pause = _REATTACH_PAUSES_S[min(connections, len(_REATTACH_PAUSES_S) - 1)]
            connections += 1
            attach_wait = RECONNECT_WAIT_S
        elif summary.other_web_pages:
            unanswered = 0
            elsewhere += 1
            if elsewhere >= _ELSEWHERE_CONFIRMATIONS:
                raise _session_lost("the tab is on another site, not on the portal")
        else:
            unanswered = 0
            elsewhere = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(pause, remaining))
        summary = _look(cdp_url)

    if summary is None:
        raise MercadoPagoConnectError(
            f"Chrome's target list stopped answering. {_CHROME_GONE_HINT}"
        )
    if summary.matching_pages:
        raise MercadoPagoWebError(
            "Chrome has the Mercado Pago page open, but Playwright never attached to it "
            f"({connections} connection(s); {_targets_text(summary)})."
        )
    if summary.other_web_pages:
        raise _session_lost("the tab is on another site, not on the portal")
    raise MercadoPagoWebError(f"{no_tab} ({_targets_text(summary)}).")


def _drive_over_cdp(
    cdp_url: str,
    movement_pages: int,
    *,
    connect_wait_s: float,
    tab_wait_s: float,
    start_hint: str,
    no_tab: str,
    process=None,
    kill_switch=None,
) -> MercadoPagoAccount:
    """Find the portal tab of the Chrome at `cdp_url` and read it.

    Never navigates and never closes the browser: reuse mode attaches to a
    Chrome someone else owns, and managed mode terminates its own process
    (`process`, passed along only so a Chrome that has ended fails fast).
    """
    return _with_portal_tab(
        cdp_url,
        lambda page: read_account(page, movement_pages, kill_switch),
        connect_wait_s=connect_wait_s,
        tab_wait_s=tab_wait_s,
        start_hint=start_hint,
        no_tab=no_tab,
        process=process,
        kill_switch=kill_switch,
    )


def _require_playwright() -> None:
    """Fail before opening a Chrome window on a broken deploy."""
    if importlib.util.find_spec("playwright") is None:
        raise ImportError("playwright is not installed. Run: pip install -r requirements.txt")


def _launch(port: int, *, offscreen: bool):
    """Launch the dedicated Chrome on `port`, opening the balance page.

    First reclaims a Chrome a killed run orphaned on the profile, which can
    take up to about 20 s.
    """
    profile = profile_dir()
    real_chrome.reclaim_orphaned_chrome(port=port, profile_dir=profile)
    # A Chrome already on this port would make the launch hand over to it and
    # exit, so the run would read (and then fail to close) someone else's.
    if real_chrome.port_in_use(port):
        raise MercadoPagoWebError(
            f"Port {port} is already in use. Close the Chrome on it, set "
            f"{CDP_PORT_ENV} to a free port, or set {CDP_URL_ENV} to reuse that Chrome."
        )
    try:
        executable = real_chrome.find_chrome(CHROME_PATH_ENV)
        proc = real_chrome.launch_chrome(
            executable, port=port, profile_dir=profile, url=START_URL, offscreen=offscreen
        )
    except real_chrome.ChromeNotFoundError as exc:
        raise MercadoPagoWebError(str(exc)) from exc
    except real_chrome.ChromeLaunchError as exc:
        # The helper's message has no path; name the setting that holds one.
        raise MercadoPagoWebError(f"{exc} Check {CHROME_PATH_ENV}.") from exc
    # Never raises, so the caller's launch-to-`try` gap stays raise-free.
    real_chrome.record_managed_launch(proc, port=port, profile_dir=profile)
    return proc


def _drive_managed(movement_pages: int) -> MercadoPagoAccount:
    """Launch Chrome off-screen on the profile, read, terminate it.

    One attempt, never a retry: a failure here is either a lost session or a
    machine problem, and neither improves by launching again. The watchdog
    terminates the Chrome if the drive overruns, so a Playwright call stuck on
    a frozen browser cannot hold the scraper service.
    """
    _require_playwright()
    if not profile_dir().is_dir():
        raise MercadoPagoSessionError(
            "No signed-in Chrome profile for Mercado Pago yet. "
            "Run `make mercadopago-login` and sign in."
        )
    marker = sign_in_marker_path()
    since = sign_in_marker.marked_since(marker)
    if since is not None:
        # The session was lost earlier: opening Chrome again only re-learns
        # that (and a lost session must never be retried).
        # The hint leads: the dashboard shows only a run message's first 120
        # characters, and the time is cut to the minute.
        when = since[:16].replace("T", " ") + " UTC" if since else "an earlier run"
        raise MercadoPagoSignInPending(
            "Mercado Pago needs sign-in: run `make mercadopago-login` "
            f"(skipped without opening Chrome; lost since {when})."
        )
    port = cdp_port()
    proc = _launch(port, offscreen=True)
    # Nothing may raise between the launch and this `try`: the `finally` is
    # the only thing that closes the Chrome just started.
    watchdog: threading.Timer | None = None
    try:
        watchdog = threading.Timer(MANAGED_HARD_LIMIT_S, real_chrome.terminate_chrome, args=(proc,))
        watchdog.daemon = True
        watchdog.start()
        return _drive_over_cdp(
            f"http://127.0.0.1:{port}",
            movement_pages,
            connect_wait_s=CONNECT_WAIT_S,
            tab_wait_s=MANAGED_TAB_WAIT_S,
            start_hint=_CHROME_START_HINT,
            no_tab=_MANAGED_NO_TAB,
            process=proc,
        )
    except MercadoPagoSessionError as exc:
        if not exc.after_balance:
            sign_in_marker.mark(marker)
        raise
    except MercadoPagoConnectionLostError:
        # The watchdog terminating Chrome is what cut the connection.
        if watchdog is None or not watchdog.finished.is_set():
            raise
    finally:
        if watchdog is not None:
            watchdog.cancel()
        # Through CDP first, so cookies the portal rotated during the read are
        # written to the profile.
        real_chrome.terminate_chrome(proc, cdp_url=f"http://127.0.0.1:{port}")
        if real_chrome.has_exited(proc):
            real_chrome.forget_managed_launch(profile_dir(), proc.pid)
    # Raised outside the handler: the lost connection is the timeout itself.
    raise MercadoPagoWebError(_TIMED_OUT)


def _drive_reuse(cdp_url: str, movement_pages: int) -> MercadoPagoAccount:
    """Attach to a Chrome that is already running; leave it running.

    The watchdog kills the Playwright driver if the drive overruns, so a call
    stuck on a frozen browser fails and this thread ends; no Playwright call
    follows, and no partial account is returned. The driver error that ends
    the drive is reported as the timeout it is; a scraper error is not.
    """
    _require_playwright()
    switch = real_chrome.DriverKillSwitch()
    watchdog = threading.Timer(REUSE_HARD_LIMIT_S, switch.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        return _drive_over_cdp(
            cdp_url,
            movement_pages,
            connect_wait_s=REUSE_CONNECT_WAIT_S,
            tab_wait_s=REUSE_TAB_WAIT_S,
            start_hint=_REUSE_START_HINT,
            no_tab=_REUSE_NO_TAB,
            kill_switch=switch,
        )
    except real_chrome.DriverKilledError:
        pass
    except Exception as exc:
        if not switch.fired or isinstance(exc, (MercadoPagoWebError, MercadoPagoSessionError)):
            raise
    finally:
        watchdog.cancel()
    # Raised outside the handler: the driver error is neither cause nor context.
    raise MercadoPagoWebError(_TIMED_OUT)


async def scrape_account(
    cdp_url: str | None = None, movement_pages: int = DEFAULT_MOVEMENT_PAGES
) -> MercadoPagoAccount:
    """Read the wallet balance, the savings pots and the wallet ledger.

    Reuse mode when a CDP URL is given (`cdp_url` or `MERCADOPAGO_CDP_URL`),
    managed mode otherwise (see the module docstring). Raises
    `MercadoPagoSessionError` when a human must sign in again,
    `MercadoPagoWebError` when Chrome or the balance page could not be
    reached, and `ImportError` when Playwright is not installed. A pots or
    ledger page that fails after the balance was read is a warning on the
    result instead, unless the tab or its connection was lost.
    """
    cdp = cdp_url if cdp_url is not None else os.environ.get(CDP_URL_ENV)
    drive = (
        asyncio.to_thread(_drive_reuse, cdp, movement_pages)
        if cdp
        else asyncio.to_thread(_drive_managed, movement_pages)
    )
    failure: str | None = None
    try:
        return await asyncio.wait_for(drive, timeout=DRIVE_TIMEOUT_S)
    except TimeoutError:
        # Only if the drive's watchdog did not end it first (both modes have
        # one, firing earlier); the worker thread itself cannot be stopped.
        raise MercadoPagoWebError(_TIMED_OUT) from None
    except (MercadoPagoWebError, MercadoPagoSessionError, ImportError):
        raise
    except Exception as exc:
        failure = type(exc).__name__
    # Raised outside the handler: a Playwright error's text (CDP URL, call log)
    # must not reach `scraper_runs.error_message`, not even as the cause.
    raise MercadoPagoWebError(f"Mercado Pago read failed ({failure}).")


# --- Manual sign-in (`make mercadopago-login`) ----------------------------------


def _balance_readable(page) -> bool:
    """Whether the balance page reads from `page` right now (one fetch)."""
    try:
        parse_balance(_fetch_props(page, "balance"))
    except (MercadoPagoWebError, MercadoPagoSessionError):
        return False
    return True


def _portal_cookies(page) -> list:
    """Chrome's cookies for the portal, read from its own jar (raises on failure).

    A local read over the debug connection: nothing is sent to Mercado Pago.
    """
    return page.context.cookies(f"https://{PORTAL_HOST}/")


def _has_lasting_session(page) -> bool:
    """Whether Chrome holds a persistent `LASTING_SESSION_COOKIE` for the portal.

    A session cookie (no expiry) dies with the window, so it does not count.
    A jar that cannot be read raises.
    """
    return any(
        c.get("name") == LASTING_SESSION_COOKIE and (c.get("expires") or -1) > 0
        for c in _portal_cookies(page)
    )


def _await_lasting_session(page, deadline: float, process=None) -> None:
    """After the balance reads, wait until the session would survive a restart.

    Local cookie-jar reads only, one every `_LOGIN_POLL_S` seconds; no further
    balance probe. Bounded by the sign-in `deadline`. A launched Chrome that
    has ended stops the wait at once; `_MAX_JAR_READ_FAILURES` failed reads in
    a row end it as a Chrome problem, not a session one.
    """
    told = False
    failures = 0
    while True:
        try:
            lasting = _has_lasting_session(page)
        except Exception as exc:
            failures += 1
            lasting = False
            if real_chrome.has_exited(process):
                raise _chrome_exited() from None
            if failures >= _MAX_JAR_READ_FAILURES:
                # Not a MercadoPagoConnectError: the caller retries those as a
                # tab still loading.
                raise MercadoPagoWebError(
                    f"Chrome's cookies could not be read {failures} times in a row "
                    f"({type(exc).__name__}). {_CHROME_GONE_HINT}"
                ) from None
        else:
            failures = 0
        if lasting:
            return
        if not told:
            print(
                "Signed in; waiting for Mercado Pago to save the session before closing Chrome...",
                flush=True,
            )
            told = True
        if real_chrome.has_exited(process):
            raise _chrome_exited()
        if time.monotonic() >= deadline:
            _log_portal_cookie_names(page)
            raise MercadoPagoSessionError(
                "Signed in, but Mercado Pago did not save a lasting session in time, so "
                "it would not survive Chrome closing. Re-run `make mercadopago-login` "
                "and stay on the balance page until the login closes Chrome itself (if "
                'the sign-in page offers a "keep me signed in" option, tick it).'
            )
        time.sleep(_LOGIN_POLL_S)


def chrome_time_to_unix(chrome_us: int) -> float:
    """Chrome's cookie time (microseconds since 1601-01-01 UTC) as Unix seconds."""
    return chrome_us / 1_000_000 - _CHROME_EPOCH_OFFSET_S


def _lasting_session_in_db(db: Path, since: float, now: float) -> bool:
    """Whether the cookie DB at `db` holds a fresh persistent portal `ssid`.

    Fresh: written at or after `since` (Unix seconds, the login's start) and
    expiring after `now`, so a cookie left untouched from an earlier sign-in
    does not count. Written means `last_update_utc` where the store has it:
    Chrome keeps the old `creation_utc` when a cookie is set again with the
    same value (seen live), so a re-run of the login on a profile still
    signed in could otherwise never pass. Opened read-only, and only
    `host_key`, `name`, `is_persistent` and the time columns are selected:
    never a value column. Raises on a sqlite error.
    """
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(cookies)")}
        written = (
            "MAX(creation_utc, last_update_utc)" if "last_update_utc" in columns else "creation_utc"
        )
        rows = conn.execute(
            f"SELECT host_key, name, is_persistent, {written}, expires_utc FROM cookies "
            "WHERE name = ? AND host_key IN (?, ?)",
            (LASTING_SESSION_COOKIE, *_PORTAL_COOKIE_HOSTS),
        ).fetchall()
    finally:
        conn.close()
    return any(
        is_persistent == 1
        and chrome_time_to_unix(written_at) >= since
        and chrome_time_to_unix(expires) > now
        for _host, _name, is_persistent, written_at, expires in rows
    )


def _file_stamp(path: Path) -> tuple[int, int] | None:
    """Size and mtime (ns) of `path`, or None when it is not there."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


def _session_on_disk(profile: Path, since: float) -> bool:
    """Whether the profile's cookie store on disk holds a fresh persistent portal `ssid`.

    Chrome's own file is never opened: it is copied, with its `-journal` /
    `-wal` if present, to a temp dir, the copy is opened read-only, and the
    copy is deleted. The copy is not atomic, so the files are stamped (size,
    mtime) before and after it, and any change means "not yet"; so do a
    missing file, a sqlite error (a copied hot journal is one) and an OS
    error. `since` is the login's start (Unix seconds).
    """
    for relative in _COOKIE_DB_PATHS:
        db = profile / relative
        if not db.is_file():
            continue
        sides = [db.with_name(db.name + suffix) for suffix in ("-journal", "-wal")]
        try:
            with tempfile.TemporaryDirectory(prefix="mp-cookies-") as tmp:
                before = [_file_stamp(path) for path in (db, *sides)]
                snapshot = Path(tmp) / db.name
                shutil.copyfile(db, snapshot)
                for side in sides:
                    if side.is_file():
                        shutil.copyfile(side, Path(tmp) / side.name)
                if [_file_stamp(path) for path in (db, *sides)] != before:
                    logger.info("MercadoPago: cookie store changed while copied; not yet")
                    continue
                found = _lasting_session_in_db(snapshot, since, time.time())
        except (sqlite3.Error, OSError) as exc:
            logger.info("MercadoPago: cookie store not readable yet: %s", type(exc).__name__)
            continue
        if found:
            return True
    return False


def _await_session_on_disk(deadline: float, since: float, process=None) -> None:
    """After the jar holds the lasting cookie, wait until it reaches the disk.

    Chrome stays open meanwhile; one read of a copy of the cookie store every
    `_LOGIN_POLL_S` seconds, until the sign-in `deadline` or
    `_DISK_WAIT_FLOOR_S` from now, whichever is later. Nothing is sent to
    Mercado Pago.
    """
    deadline = max(deadline, time.monotonic() + _DISK_WAIT_FLOOR_S)
    told = False
    while not _session_on_disk(profile_dir(), since):
        if not told:
            print(
                "Session saved in Chrome; waiting for it to reach the profile on disk...",
                flush=True,
            )
            told = True
        if real_chrome.has_exited(process):
            raise _chrome_exited()
        if time.monotonic() >= deadline:
            raise MercadoPagoSessionError(
                "Signed in, but Chrome did not write the session to the profile on disk "
                "in time, so it would not survive Chrome closing. Re-run `make "
                "mercadopago-login` and stay on the balance page until the login closes "
                "Chrome itself."
            )
        time.sleep(_LOGIN_POLL_S)


def _log_portal_cookie_names(page) -> None:
    """Log which portal cookies Chrome holds: names and persistence, never values."""
    try:
        cookies = _portal_cookies(page)
    except Exception:
        return
    names = sorted(
        f"{c.get('name')}({'persistent' if (c.get('expires') or -1) > 0 else 'session'})"
        for c in cookies
    )
    logger.info("MercadoPago: portal cookies at the sign-in deadline: %s", ", ".join(names))


def _await_sign_in(
    cdp_url: str, timeout_s: float, process=None, since: float | None = None
) -> None:
    """Wait until a portal tab reads the balance page and the session lasts.

    Only looks while the human signs in: it never types, clicks or navigates.

    What it does locally: one look every `_LOGIN_POLL_S` seconds at Chrome's
    own target list (a plain HTTP request to the debug port on this machine),
    which says whether a tab is on the portal. While the human is on the
    sign-in site that is all that happens: Playwright is NOT attached, so no
    automation touches the sign-in or its verification pages.

    What it sends to Mercado Pago: only the probe, which is one GET of
    `/banking/balance` made from inside the portal tab, with the session
    cookie and without following a redirect (the same fetch a run starts
    with). A probe needs a fresh Playwright connection (watched for up to
    `ATTACH_POLL_S`, then dropped, unless the balance reads) and is made when a look finds a tab on
    the portal at a URL that was not probed yet, and otherwise at most once
    every `_LOGIN_PROBE_INTERVAL_S` seconds while that tab stays put. So a
    human who sits on a portal page for the whole `timeout_s` without being
    signed in costs at most `timeout_s / _LOGIN_PROBE_INTERVAL_S` probes, and
    the usual sign-in costs one or two.

    A balance read is not enough on its own: it can pass while only session
    cookies carry the session, and closing Chrome then loses it. So once it
    reads, that connection stays open, for local cookie-jar reads only
    (`_await_lasting_session`), until the persistent login cookie is in
    Chrome's jar or `timeout_s` runs out. A cookie in the jar is still not on
    disk (Chrome writes the store in batches), so the wait then reads copies
    of the profile's cookie store (`_await_session_on_disk`) until the cookie
    is there, still within `timeout_s`.

    `process` is the Chrome `save_login_session` launched: once it has ended
    (the window was closed) the wait ends with it. `since` is when the login
    started (Unix seconds; default now): only a cookie written after it
    counts on disk. The jar check has no such rule: Playwright reports no
    write time.
    """
    if since is None:
        since = time.time()
    summary = _await_chrome(cdp_url, CONNECT_WAIT_S, _CHROME_START_HINT, process)
    deadline = time.monotonic() + timeout_s
    probed_key: str | None = None
    probed_at = float("-inf")
    unanswered = 0
    while True:
        if summary is None:
            unanswered += 1
            _unanswered(unanswered, process)
        else:
            unanswered = 0
            due = (
                summary.matching_key != probed_key
                or time.monotonic() - probed_at >= _LOGIN_PROBE_INTERVAL_S
            )
            if summary.matching_pages and due:
                try:
                    with _cdp_connection(cdp_url, RECONNECT_WAIT_S, process) as browser:
                        watch = min(ATTACH_POLL_S, max(0.0, deadline - time.monotonic()))
                        page = _watch_for_portal_tab(browser, watch)
                        if page is not None:
                            probed_key, probed_at = summary.matching_key, time.monotonic()
                            if _balance_readable(page):
                                _await_lasting_session(page, deadline, process)
                                _await_session_on_disk(deadline, since, process)
                                return
                except MercadoPagoConnectError as exc:
                    # The tab may be mid-load; the next look tries again.
                    logger.info("MercadoPago: attach for the sign-in probe failed: %s", exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(_LOGIN_POLL_S)
        summary = _look(cdp_url)
    raise MercadoPagoSessionError("Sign-in not detected in time. Re-run `make mercadopago-login`.")


def _session_on_disk_after_close(profile: Path, since: float) -> bool:
    """The post-close check: read a copy, retrying briefly while Chrome lets go.

    Chrome's network service can still hold the store (an exclusive lock, a
    hot journal) a moment after the browser process exits.
    """
    deadline = time.monotonic() + _POST_CLOSE_DISK_WAIT_S
    while True:
        if _session_on_disk(profile, since):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POST_CLOSE_DISK_POLL_S)


def save_login_session(timeout_s: float = LOGIN_TIMEOUT_S) -> None:
    """Open a visible Chrome on the dedicated profile for a human to sign in.

    The code never types a credential and never touches the login form: it
    opens the balance page, waits (bounded) until that page reads and the
    persistent login cookie is in Chrome's jar and then in the profile's
    cookie store on disk, closes Chrome through CDP (falling back to signals),
    and checks the disk once more. The session stays in the profile, which
    every later run reuses.
    """
    _require_playwright()
    port = cdp_port()
    cdp_url = f"http://127.0.0.1:{port}"
    started = time.time()
    proc = _launch(port, offscreen=False)
    # Nothing may raise between the launch and this `try` (see `_drive_managed`).
    try:
        print(
            "\nA Chrome window opened on Mercado Pago. Sign in there (e-mail, password "
            "and any verification it asks for); nothing is typed for you. Waiting up "
            f"to {int(timeout_s // 60)} min for the balance page to load.",
            flush=True,
        )
        _await_sign_in(cdp_url, timeout_s, process=proc, since=started)
    finally:
        real_chrome.terminate_chrome(proc, cdp_url=cdp_url)
        if real_chrome.has_exited(proc):
            real_chrome.forget_managed_launch(profile_dir(), proc.pid)
    if not _session_on_disk_after_close(profile_dir(), started):
        raise MercadoPagoSessionError(
            "Signed in, but after Chrome closed the session was not in the profile on "
            "disk, so later runs would be signed out. Re-run `make mercadopago-login`."
        )
    sign_in_marker.clear(sign_in_marker_path())
