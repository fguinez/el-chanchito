"""Mercado Pago (Chile) web pages: the page contracts and their pure parsers.

The account pages of www.mercadopago.cl are server-rendered HTML. Each one
carries its data in an inline script as `_n.ctx.r={...JSON...}` followed by
more JavaScript in the same script, and under that JSON everything lives at
`appProps.pageProps`. Three pages are read (shapes only; nothing here is a
real value):

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
    minus for money out), `amount.cents`, `title` and `description`.

Parsing rules:

  * the marker can also appear where no data follows, so every occurrence
    is tried until one decodes to an object carrying `appProps`;
  * a missing top-level node raises `MercadoPagoPageError`: the page is not
    the one this module knows;
  * one malformed pot or movement row is skipped and counted for the caller
    to report, never fatal (the log line carries counts only), but a
    movements page whose rows all fail raises, since that is a changed page
    and not a bad row;
  * money is parsed with `Decimal` and rounded half up to whole pesos;
  * `datetime` is converted to America/Santiago before its date is taken, so
    an evening movement stays on its own day.

Nothing in an exception message comes from a page: they name fields and
counts only, because they end up in `scraper_runs.error_message`.
"""

import datetime
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Santiago")

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
