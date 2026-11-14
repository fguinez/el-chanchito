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
    signs in again.

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
    `result.available.amount.raw` is the wallet balance in whole CLP. (The
    same node also has the recent gain and the yearly rate of the fund the
    balance is invested in; neither is stored today, a possible later metric.)
  * `/savings/hub`: `pageProps.hub.result.potsDataHub.result.pots`, one entry
    per savings pot (`id` a uuid, `name`, `amount.raw` in CLP). The list can
    be empty.
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
cover it: in managed mode a watchdog terminates the Chrome it launched if the
drive overruns, which ends any call still waiting on it, and in both modes
the run gives up on a drive that does not finish in time.

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
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, TypeVar
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from scrapers.backends import real_chrome

logger = logging.getLogger(__name__)

T = TypeVar("T")

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
MANAGED_HARD_LIMIT_S = (
    CONNECT_WAIT_S + MANAGED_TAB_WAIT_S + ATTACH_OVERRUN_S + READ_BUDGET_S + 120
)
# Either mode: the run gives up on a drive after this long, so a call stuck on
# a frozen browser (reuse mode has no Chrome of its own to terminate) cannot
# hold the scraper service. Later than the watchdog, which ends it cleanly.
DRIVE_TIMEOUT_S = MANAGED_HARD_LIMIT_S + 60
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
    f"No Chrome answers there. Start it again, or unset {CDP_URL_ENV} to use "
    "the managed one."
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

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
# Observed as 17 uppercase alphanumerics. Kept looser than that: the id is
# only a dedup key, so any plain token is usable, while a blank or structured
# value is not.
_MOVEMENT_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
# "12.345" or "-12.345": dotted thousands (or plain digits), an optional
# leading minus that is an ASCII hyphen or U+2212 (MINUS SIGN).
_MINUS_SIGNS = "-" + chr(0x2212)
_FRACTION_RE = re.compile(
    r"^([" + re.escape(_MINUS_SIGNS) + r"]?)\s*(\d{1,3}(?:\.\d{3})+|\d+)$"
)
_CENTS_RE = re.compile(r"^\d{2}$")


class MercadoPagoWebError(RuntimeError):
    """The Mercado Pago web read failed for a reason other than the session."""


class MercadoPagoPageError(MercadoPagoWebError):
    """One page did not have the shape this module reads."""


class MercadoPagoFetchError(MercadoPagoWebError):
    """One page could not be fetched (HTTP error, timeout, network)."""


class MercadoPagoConnectError(MercadoPagoWebError):
    """The Chrome debug port did not accept a connection in time."""


class MercadoPagoSessionError(RuntimeError):
    """The signed-in browser session is missing or gone; a human must sign in.

    Never retried: a second attempt against a lost session is what gets an
    account flagged. Every message names `make mercadopago-login`.
    """


def _session_lost(reason: str) -> MercadoPagoSessionError:
    return MercadoPagoSessionError(f"Mercado Pago session lost ({reason}). {_SIGN_IN_HINT}")


@dataclass(frozen=True)
class Pot:
    """One savings pot: its uuid (lower-case), its name and its CLP balance."""

    id: str
    name: str
    amount: int


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
    page leaves `pots` empty, like an account without pots; only the warning
    tells them apart.
    """

    balance: int
    pots: list[Pot] = field(default_factory=list)
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


def _whole_pesos(raw: Any) -> Optional[int]:
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


def parse_clp_amount(amount: Any) -> Optional[int]:
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


def santiago_date(raw: Any) -> Optional[datetime.date]:
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


def _parse_pot(entry: Any) -> Optional[Pot]:
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
    )


def parse_pots(props: dict) -> tuple[list[Pot], int]:
    """The savings pots of the `/savings/hub` page, and how many were skipped.

    An empty list is a real reading (no pots). An entry that is not a CLP pot
    with a uuid and an amount, or that repeats an earlier id, is skipped and
    counted: the caller reports it, because a pot left out is money left out.
    """
    node = _dig(props, "hub", "result", "potsDataHub", what="pots")
    _require_success(node, "pots")
    entries = _dig(node, "result", "pots", what="pots")
    if not isinstance(entries, list):
        raise MercadoPagoPageError("pots: pots is not a list")
    pots: dict[str, Pot] = {}
    skipped = 0
    for entry in entries:
        pot = _parse_pot(entry)
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


def _evaluate_bounded(page, script: str, arg: dict, timeout_ms: int) -> Any:
    """Run an async in-page `script` and return its JSON result, bounded; raises.

    `page.evaluate` takes no timeout. `wait_for_function` runs its predicate
    once right away, and when that returns a promise (always truthy) it waits
    for the promise inside its timeout, so the script is bounded. `json_value`
    (copying the settled result out) and `dispose` have no timeout of their
    own; the managed-mode watchdog and the drive timeout cover that window.
    """
    handle = page.wait_for_function(script, arg=arg, timeout=timeout_ms)
    try:
        return handle.json_value()
    finally:
        try:
            handle.dispose()
        except Exception:
            pass


def _fetch_props(page, surface: str, page_number: int = 1) -> dict:
    """Fetch one allowed page from inside the tab and return its `pageProps`."""
    failure: Optional[str] = None
    result = None
    try:
        result = _evaluate_bounded(
            page,
            _FETCH_JS,
            {"surface": surface, "pageNumber": page_number, "timeoutMs": FETCH_TIMEOUT_MS},
            EVALUATE_TIMEOUT_MS,
        )
    except Exception as exc:
        failure = type(exc).__name__
    if failure is not None:
        # Raised outside the handler: the Playwright error (its text carries a
        # call log) is neither the cause nor the context of this one.
        raise MercadoPagoFetchError(f"{surface}: in-page fetch failed ({failure})")
    return page_props_from_html(html_from_fetch(result, surface))


def _read_movements(
    page, account: MercadoPagoAccount, max_pages: int, deadline: float
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
            rows, pages, unread = parse_movements(_fetch_props(page, "movements", number))
        except MercadoPagoWebError as exc:
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


def read_account(page, movement_pages: int = DEFAULT_MOVEMENT_PAGES) -> MercadoPagoAccount:
    """Read balance, pots and ledger from a tab on the portal. Never navigates.

    The balance page goes first and doubles as the session probe. Whatever
    goes wrong with it, nothing else is fetched: a redirect, a 401/403 or a
    tab off the portal is a lost session (`MercadoPagoSessionError`, raised
    by the fetch); a page that answers 200 without a readable balance (a
    changed page, a balance widget that did not load) or that cannot be
    fetched is a plain `MercadoPagoWebError`, which asks nobody to sign in.
    """
    deadline = time.monotonic() + READ_BUDGET_S
    try:
        balance = parse_balance(_fetch_props(page, "balance"))
    except MercadoPagoPageError as exc:
        raise MercadoPagoWebError(
            f"Mercado Pago balance page not readable ({exc}): the page changed "
            "or its balance did not load."
        ) from exc
    except MercadoPagoFetchError as exc:
        raise MercadoPagoWebError(f"Mercado Pago balance page not fetched ({exc}).") from exc
    account = MercadoPagoAccount(balance=balance)

    try:
        account.pots, skipped = parse_pots(_fetch_props(page, "pots"))
    except MercadoPagoWebError as exc:
        logger.warning("MercadoPago: savings pots not read: %s", exc)
        account.warnings.append(f"MercadoPago: savings pots not read ({exc})")
    else:
        if skipped:
            account.warnings.append(f"MercadoPago: {skipped} savings pot(s) not read")

    _read_movements(page, account, movement_pages, deadline)
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
def _cdp_connection(cdp_url: str, wait_s: float, process=None):
    """A fresh Playwright driver attached to the Chrome at `cdp_url`.

    Leaving the block stops the driver, which drops the connection, also when
    the connection fails or the block raises. Nothing is closed in the
    browser: Chrome and its tabs keep running, so the next connection finds
    them as they are.
    """
    from playwright.sync_api import sync_playwright  # lazy: keeps tests browser-free

    with sync_playwright() as playwright:
        yield _connect(playwright, cdp_url, wait_s, process)


def _look(cdp_url: str) -> Optional[real_chrome.TargetSummary]:
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
        raise MercadoPagoConnectError(f"Chrome's target list stopped answering. {_CHROME_GONE_HINT}")


def _targets_text(summary: real_chrome.TargetSummary) -> str:
    """Chrome's target types and their counts, for an error message."""
    listed = ", ".join(f"{kind}: {count}" for kind, count in sorted(summary.types.items()))
    return f"Chrome targets: {listed or 'none'}"


def _with_portal_tab(
    cdp_url: str,
    action: Callable[[Any], T],
    *,
    connect_wait_s: float,
    tab_wait_s: float,
    start_hint: str,
    no_tab: str,
    process=None,
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
    wait at once.
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
                with _cdp_connection(cdp_url, wait, process) as browser:
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
        raise MercadoPagoConnectError(f"Chrome's target list stopped answering. {_CHROME_GONE_HINT}")
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
) -> MercadoPagoAccount:
    """Find the portal tab of the Chrome at `cdp_url` and read it.

    Never navigates and never closes the browser: reuse mode attaches to a
    Chrome someone else owns, and managed mode terminates its own process
    (`process`, passed along only so a Chrome that has ended fails fast).
    """
    return _with_portal_tab(
        cdp_url,
        lambda page: read_account(page, movement_pages),
        connect_wait_s=connect_wait_s,
        tab_wait_s=tab_wait_s,
        start_hint=start_hint,
        no_tab=no_tab,
        process=process,
    )


def _require_playwright() -> None:
    """Fail before opening a Chrome window on a broken deploy."""
    if importlib.util.find_spec("playwright") is None:
        raise ImportError("playwright is not installed. Run: pip install -r requirements.txt")


def _launch(port: int, *, offscreen: bool):
    """Launch the dedicated Chrome on `port`, opening the balance page."""
    # A Chrome already on this port would make the launch hand over to it and
    # exit, so the run would read (and then fail to close) someone else's.
    if real_chrome.port_in_use(port):
        raise MercadoPagoWebError(
            f"Port {port} is already in use. Close the Chrome on it, set "
            f"{CDP_PORT_ENV} to a free port, or set {CDP_URL_ENV} to reuse that Chrome."
        )
    try:
        executable = real_chrome.find_chrome(CHROME_PATH_ENV)
        return real_chrome.launch_chrome(
            executable, port=port, profile_dir=profile_dir(), url=START_URL, offscreen=offscreen
        )
    except real_chrome.ChromeNotFoundError as exc:
        raise MercadoPagoWebError(str(exc)) from exc
    except real_chrome.ChromeLaunchError as exc:
        # The helper's message has no path; name the setting that holds one.
        raise MercadoPagoWebError(f"{exc} Check {CHROME_PATH_ENV}.") from exc


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
    port = cdp_port()
    proc = _launch(port, offscreen=True)
    # Nothing may raise between the launch and this `try`: the `finally` is
    # the only thing that closes the Chrome just started.
    watchdog: Optional[threading.Timer] = None
    try:
        watchdog = threading.Timer(
            MANAGED_HARD_LIMIT_S, real_chrome.terminate_chrome, args=(proc,)
        )
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
    finally:
        if watchdog is not None:
            watchdog.cancel()
        real_chrome.terminate_chrome(proc)


def _drive_reuse(cdp_url: str, movement_pages: int) -> MercadoPagoAccount:
    """Attach to a Chrome that is already running; leave it running."""
    _require_playwright()
    return _drive_over_cdp(
        cdp_url,
        movement_pages,
        connect_wait_s=REUSE_CONNECT_WAIT_S,
        tab_wait_s=REUSE_TAB_WAIT_S,
        start_hint=_REUSE_START_HINT,
        no_tab=_REUSE_NO_TAB,
    )


async def scrape_account(
    cdp_url: Optional[str] = None, movement_pages: int = DEFAULT_MOVEMENT_PAGES
) -> MercadoPagoAccount:
    """Read the wallet balance, the savings pots and the wallet ledger.

    Reuse mode when a CDP URL is given (`cdp_url` or `MERCADOPAGO_CDP_URL`),
    managed mode otherwise (see the module docstring). Raises
    `MercadoPagoSessionError` when a human must sign in again,
    `MercadoPagoWebError` when Chrome or the balance page could not be
    reached, and `ImportError` when Playwright is not installed. A pots or
    ledger page that fails after the balance was read is a warning on the
    result instead.
    """
    cdp = cdp_url if cdp_url is not None else os.environ.get(CDP_URL_ENV)
    drive = (
        asyncio.to_thread(_drive_reuse, cdp, movement_pages)
        if cdp
        else asyncio.to_thread(_drive_managed, movement_pages)
    )
    failure: Optional[str] = None
    try:
        return await asyncio.wait_for(drive, timeout=DRIVE_TIMEOUT_S)
    except asyncio.TimeoutError:
        # The worker thread cannot be stopped; in managed mode its watchdog has
        # already terminated the Chrome it was waiting on.
        raise MercadoPagoWebError("Mercado Pago read did not finish in time.") from None
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


def _await_sign_in(cdp_url: str, timeout_s: float, process=None) -> None:
    """Wait until a portal tab can read the balance page, or time out.

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
    `ATTACH_POLL_S`, then dropped) and is made when a look finds a tab on
    the portal at a URL that was not probed yet, and otherwise at most once
    every `_LOGIN_PROBE_INTERVAL_S` seconds while that tab stays put. So a
    human who sits on a portal page for the whole `timeout_s` without being
    signed in costs at most `timeout_s / _LOGIN_PROBE_INTERVAL_S` probes, and
    the usual sign-in costs one or two.

    `process` is the Chrome `save_login_session` launched: once it has ended
    (the window was closed) the wait ends with it.
    """
    summary = _await_chrome(cdp_url, CONNECT_WAIT_S, _CHROME_START_HINT, process)
    deadline = time.monotonic() + timeout_s
    probed_key: Optional[str] = None
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
                                return
                except MercadoPagoConnectError as exc:
                    # The tab may be mid-load; the next look tries again.
                    logger.info("MercadoPago: attach for the sign-in probe failed: %s", exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(_LOGIN_POLL_S)
        summary = _look(cdp_url)
    raise MercadoPagoSessionError("Sign-in not detected in time. Re-run `make mercadopago-login`.")


def save_login_session(timeout_s: float = LOGIN_TIMEOUT_S) -> None:
    """Open a visible Chrome on the dedicated profile for a human to sign in.

    The code never types a credential and never touches the login form: it
    opens the balance page, waits (bounded) until that page reads, and closes
    Chrome. The session stays in the profile, which every later run reuses.
    """
    _require_playwright()
    port = cdp_port()
    proc = _launch(port, offscreen=False)
    # Nothing may raise between the launch and this `try` (see `_drive_managed`).
    try:
        print(
            "\nA Chrome window opened on Mercado Pago. Sign in there (e-mail, password "
            "and any verification it asks for); nothing is typed for you. Waiting up "
            f"to {int(timeout_s // 60)} min for the balance page to load.",
            flush=True,
        )
        _await_sign_in(f"http://127.0.0.1:{port}", timeout_s, process=proc)
    finally:
        real_chrome.terminate_chrome(proc)
