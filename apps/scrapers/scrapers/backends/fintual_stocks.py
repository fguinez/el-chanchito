"""Fintual's stock brokerage: the USD wallet ledger, as transactions.

Pure functions, no I/O: the `FintualScraper` sends `WALLET_MOVEMENTS_QUERY`
(Fintual's own web-bundle query, `POST /gql/`) a page at a time and hands each
answer's `data` to `parse_wallet_page`.

WalletMovements lists every movement of the USD cash wallet ("US$ X sin usar
en acciones"): deposits from a Chilean bank, buys and sells, dividends, cash
interest, withdrawals, cash mergers, compensations, returns of capital, ACAT
cash and warrant exercises. Every list's shape comes from Fintual's query
text, and not all of them are verified against live data yet, so an
unreadable row is skipped and counted rather than failing the page.
Divestments are counted, not imported.

Each movement becomes one row on the wallet, and the ones that move money in
or out of a stock position (buys, sells, dividends, cash mergers) a mirror
row on that position's product, which they name by `stock_ref`. Signs follow
the product: a buy is money out of the wallet and into the stock. Money that
only changes product (deposits, buys, the wallet side of a dividend) is
flagged an internal transfer; a dividend counts as income once, on the
stock. Every figure in this module's tests is synthetic.
"""

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from product_model import ScrapedTransaction

# The brokerage holds US stocks; the wallet and the positions are in USD.
STOCKS_CURRENCY = "USD"
# Goal refs are bare digits, so the prefix keeps stock refs apart.
STOCK_REF_PREFIX = "stock:"
# A movement's date is the day the wallet changed, in Chile.
LOCAL_TZ = ZoneInfo("America/Santiago")
PAGE_SIZE = 50

_CENT = Decimal("0.01")

WALLET_MOVEMENTS_QUERY = """\
query WalletMovements($limit: Int!, $offset: Int!) {
  stocksWalletDividends(limit: $limit, offset: $offset) {
    id
    date
    createdAt
    netCapital {
      amount
      currency
    }
    asset {
      id
      symbol
    }
  }
  stocksDomesticBankDeposits(limit: $limit, offset: $offset) {
    id
    capital {
      amount
      currency
    }
    declaredAt
    forexOrder {
      id
      executedAt
      foreignCapital {
        amount
        currency
      }
    }
  }
  stocksDomesticBankWithdrawals(limit: $limit, offset: $offset) {
    id
    paidAt
    paymentDate
    declaredAt
    requestedCapital {
      amount
      currency
    }
    forexOrder {
      id
      executedAt
      domesticCapital {
        amount
        currency
      }
      foreignCapital {
        amount
        currency
      }
    }
  }
  stocksDivestments(limit: $limit, offset: $offset) {
    divestmentOperationUuid
    requestedAmount
    requestedCurrency
    convertedAmount
    convertedCurrency
    paymentTime
    declaredAt
    executedAt
    paidAt
  }
  stocksWalletAssetSubscriptionInstructions(limit: $limit, offset: $offset) {
    id
    declaredAt
    requestedCapital {
      amount
      currency
    }
    assetOrder {
      id
      capital {
        amount
        currency
      }
      asset {
        id
        name
        symbol
      }
      fulfilledAt
    }
  }
  stocksWalletAssetRedemptionInstructions(limit: $limit, offset: $offset) {
    id
    declaredAt
    requestedCapital {
      amount
      currency
    }
    requestedShares
    assetOrder {
      id
      capital {
        amount
        currency
      }
      asset {
        id
        name
        symbol
      }
      fulfilledAt
    }
  }
  stocksWalletCashMergers(limit: $limit, offset: $offset) {
    id
    createdAt
    date
    asset {
      id
      name
      symbol
    }
    capital {
      amount
      currency
    }
  }
  stocksWalletCompensations(limit: $limit, offset: $offset) {
    id
    createdAt
    date
    capital {
      amount
      currency
    }
  }
  stocksWalletCashInterests(limit: $limit, offset: $offset) {
    id
    createdAt
    date
    amount
  }
  stocksWalletReturnOfCapitals(limit: $limit, offset: $offset) {
    id
    returnOfCapitals {
      id
      createdAt
      date
      amount {
        amount
        currency
      }
    }
  }
  stocksWalletAcatCashActivities(limit: $limit, offset: $offset) {
    fees {
      id
      date
      createdAt
      amount
    }
    cashInbounds {
      id
      date
      createdAt
      amount
    }
    cashOutbounds {
      id
      date
      createdAt
      amount
    }
  }
  stocksWalletWarrantExerciseCashActivities(limit: $limit, offset: $offset) {
    id
    fees {
      id
      date
      amount
      createdAt
    }
    costs {
      id
      date
      amount
      createdAt
    }
  }
}"""


# -- small JSON helpers --------------------------------------------------------


def stock_ref(asset_id: str) -> str:
    """The `external_ref` of a stock position's product, from its asset id
    (not the ticker: tickers get renamed). Products and the transactions that
    name them both build it here."""
    return f"{STOCK_REF_PREFIX}{asset_id}"


def number(value: Any) -> Decimal | None:
    """A JSON number as an exact Decimal of its repr; None if it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return Decimal(str(value))


def text(value: Any) -> str | None:
    """A non-blank JSON string, stripped; None otherwise."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def cents(value: Decimal) -> Decimal:
    """A USD amount rounded half up to cents."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def usd(value: Any) -> Decimal | None:
    """A JSON number as USD cents (half up); None if it isn't a number."""
    amount = number(value)
    return None if amount is None else cents(amount)


def _moment(value: Any) -> datetime | None:
    """An ISO datetime, read as UTC when it has no offset; None if unreadable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local_date(value: Any) -> date | None:
    """The Chilean calendar day of an ISO date or datetime; None if unreadable.

    A bare `YYYY-MM-DD` is already a day and is kept as is. A datetime is
    converted to America/Santiago first, so a `01:30Z` movement lands on the
    previous local day; one without an offset is read as UTC.
    """
    if not isinstance(value, str):
        return None
    if "T" not in value and " " not in value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    parsed = _moment(value)
    return None if parsed is None else parsed.astimezone(LOCAL_TZ).date()


# -- the ledger -----------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    """The stock a movement concerns, as far as the row says."""

    id: str | None
    symbol: str | None
    name: str | None

    @property
    def label(self) -> str:
        """How a description names it: the ticker, else the name."""
        return self.symbol or self.name or "acciones"


@dataclass(frozen=True)
class MovementType:
    """How one WalletMovements list maps onto transactions.

    `wallet_sign` turns the row's amount into the wallet's signed amount (a
    negative raw amount flips with it). `stock_sign`, relative to the raw
    amount too, is set for the types a stock position mirrors; their
    description gets the stock's label appended.
    """

    tag: str
    label: str
    wallet_sign: int
    wallet_internal: bool
    stock_sign: int | None = None
    stock_internal: bool = True


DEPOSIT = MovementType("dep", "Depósito desde banco", +1, True)
WITHDRAWAL = MovementType("wdr", "Retiro a banco", -1, True)
BUY = MovementType("buy", "Compra", -1, True, stock_sign=+1)
SELL = MovementType("sell", "Venta", +1, True, stock_sign=-1)
# Income once: on the stock, where it was earned; the wallet side is a transfer.
DIVIDEND = MovementType("div", "Dividendo", +1, True, stock_sign=+1, stock_internal=False)
INTEREST = MovementType("int", "Intereses saldo USD", +1, False)
CASH_MERGER = MovementType("cmrg", "Fusión en efectivo", +1, True, stock_sign=-1)
COMPENSATION = MovementType("comp", "Compensación Fintual", +1, False)
RETURN_OF_CAPITAL = MovementType("roc", "Devolución de capital", +1, True)
ACAT_IN = MovementType("acatin", "Transferencia ACAT entrante", +1, True)
ACAT_OUT = MovementType("acatout", "Transferencia ACAT saliente", -1, True)
ACAT_FEE = MovementType("acatfee", "Comisión ACAT", -1, False)
WARRANT_COST = MovementType("wcost", "Ejercicio de warrant", -1, True)
WARRANT_FEE = MovementType("wfee", "Comisión ejercicio de warrant", -1, False)


@dataclass(frozen=True)
class LedgerEntry:
    """One movement of the USD wallet. `amount` is the raw USD figure, in
    cents; the signed amounts each product sees follow from `type`."""

    type: MovementType
    source_id: str
    day: date
    amount: Decimal
    asset: Asset | None = None

    @property
    def wallet_amount(self) -> Decimal:
        return self.type.wallet_sign * self.amount

    @property
    def stock_amount(self) -> Decimal | None:
        """The amount on the stock position, for a type with an identified
        stock; None when there is no mirror row."""
        if self.type.stock_sign is None or self.asset is None or self.asset.id is None:
            return None
        return self.type.stock_sign * self.amount

    @property
    def description(self) -> str:
        if self.type.stock_sign is None:
            return self.type.label
        label = self.asset.label if self.asset else "acciones"
        return f"{self.type.label} {label}"


@dataclass
class WalletPage:
    """What one WalletMovements page held.

    `length` is its longest list, nested ones included: how far the next
    page's offset moves (Fintual may cap a list below the limit asked for), 0
    on the page after the last. `pending` counts movements not executed yet
    (left out without a warning; they come back once done) and `cancelled`
    the ones declared more than `PENDING_WINDOW` ago and still not executed
    (left out too, without holding up the balance check). `divestments`
    counts the rows of a list not imported, `skipped` the unusable rows per
    reason ("not_usd", "unreadable") and `without_asset` the stock movements
    whose stock has no id (their wallet row is kept, the mirror is not).
    """

    entries: list[LedgerEntry] = field(default_factory=list)
    length: int = 0
    pending: int = 0
    cancelled: int = 0
    divestments: int = 0
    skipped: Counter = field(default_factory=Counter)
    without_asset: int = 0


class _Unreadable(ValueError):
    """A row whose id, date or amount can't be read."""


class _NotUsd(ValueError):
    """A row whose amount is in another currency."""


def _source_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise _Unreadable("no id")
    return str(value).strip()


def _day(value: Any) -> date:
    day = local_date(value)
    if day is None:
        raise _Unreadable("no date")
    return day


def _money(value: Any) -> Decimal:
    """A `{amount, currency}` object or a bare number (the lists without a
    currency are USD) as USD cents."""
    if isinstance(value, dict):
        if value.get("currency") != STOCKS_CURRENCY:
            raise _NotUsd(value.get("currency"))
        value = value.get("amount")
    amount = usd(value)
    if amount is None:
        raise _Unreadable("no amount")
    return amount


def _asset(value: Any) -> Asset:
    value = value if isinstance(value, dict) else {}
    asset_id = value.get("id")
    return Asset(
        id=str(asset_id) if isinstance(asset_id, (str, int)) and str(asset_id) else None,
        symbol=text(value.get("symbol")),
        name=text(value.get("name")),
    )


# A row's (source id, day, amount, asset), or None while it is not executed.
_Parsed = tuple[str, date, Decimal, Asset | None] | None

# The query has no state field: a deposit, withdrawal or order declared longer
# ago than this and still not executed was cancelled, not pending.
PENDING_WINDOW = timedelta(days=7)


def _may_still_execute(row: dict, now: datetime) -> bool:
    """Whether a movement not executed yet is pending: declared within
    `PENDING_WINDOW` of `now`. One without a readable declaration date counts
    as pending, the side that only skips the balance check."""
    declared = _moment(row.get("declaredAt")) or _moment(row.get("createdAt"))
    return declared is None or now - declared <= PENDING_WINDOW


def _forex(row: dict) -> _Parsed:
    """A deposit or withdrawal: it moves the wallet once its currency
    exchange has executed, by the USD side of it."""
    order = row.get("forexOrder")
    if not isinstance(order, dict) or not order.get("executedAt"):
        return None
    return (
        _source_id(row.get("id")),
        _day(order["executedAt"]),
        _money(order.get("foreignCapital")),
        None,
    )


def _asset_order(row: dict) -> _Parsed:
    """A buy or sell: it moves the wallet once its order is fulfilled. The
    order id is the purchase's identity (the per-asset views share it)."""
    order = row.get("assetOrder")
    if not isinstance(order, dict) or not order.get("fulfilledAt"):
        return None
    return (
        _source_id(order.get("id")),
        _day(order["fulfilledAt"]),
        _money(order.get("capital")),
        _asset(order.get("asset")),
    )


def _dated(amount_key: str, with_asset: bool = False) -> Callable[[dict], _Parsed]:
    """A movement stamped with the day it happened, its amount under `amount_key`."""

    def parse(row: dict) -> _Parsed:
        return (
            _source_id(row.get("id")),
            _day(row.get("date")),
            _money(row.get(amount_key)),
            _asset(row.get("asset")) if with_asset else None,
        )

    return parse


# Top-level lists: (field, type, parser).
_LISTS: tuple[tuple[str, MovementType, Callable[[dict], _Parsed]], ...] = (
    ("stocksDomesticBankDeposits", DEPOSIT, _forex),
    ("stocksDomesticBankWithdrawals", WITHDRAWAL, _forex),
    ("stocksWalletAssetSubscriptionInstructions", BUY, _asset_order),
    ("stocksWalletAssetRedemptionInstructions", SELL, _asset_order),
    ("stocksWalletDividends", DIVIDEND, _dated("netCapital", with_asset=True)),
    ("stocksWalletCashInterests", INTEREST, _dated("amount")),
    ("stocksWalletCashMergers", CASH_MERGER, _dated("capital", with_asset=True)),
    ("stocksWalletCompensations", COMPENSATION, _dated("capital")),
)
# Lists nested in a container: (container field, list field, type). Each
# container may come as one object or a list of them.
_NESTED_LISTS: tuple[tuple[str, str, MovementType], ...] = (
    ("stocksWalletReturnOfCapitals", "returnOfCapitals", RETURN_OF_CAPITAL),
    ("stocksWalletAcatCashActivities", "cashInbounds", ACAT_IN),
    ("stocksWalletAcatCashActivities", "cashOutbounds", ACAT_OUT),
    ("stocksWalletAcatCashActivities", "fees", ACAT_FEE),
    ("stocksWalletWarrantExerciseCashActivities", "costs", WARRANT_COST),
    ("stocksWalletWarrantExerciseCashActivities", "fees", WARRANT_FEE),
)
_DIVESTMENTS = "stocksDivestments"


def _rows(value: Any, where: str) -> list:
    """A list field's rows; a null one (nothing to list) is no rows."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"WalletMovements {where} is not a list")
    return value


def _containers(value: Any, where: str) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return [row for row in _rows(value, where) if isinstance(row, dict)]


def parse_wallet_page(data: Any, now: datetime) -> WalletPage:
    """One WalletMovements answer's `data` as ledger entries.

    Rows not executed yet are pending while declared within `PENDING_WINDOW`
    of `now` (an aware datetime), and cancelled after that; rows in another
    currency or without a readable id, date or amount are skipped and
    counted; a row whose amount rounds to zero cents moves nothing and is
    dropped. Raises ValueError when the answer isn't shaped like
    WalletMovements at all.
    """
    if not isinstance(data, dict):
        raise ValueError("WalletMovements answered no data")
    page = WalletPage()

    def add(movement_type: MovementType, rows: list, parse: Callable[[dict], _Parsed]) -> None:
        page.length = max(page.length, len(rows))
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise _Unreadable("not an object")
                parsed = parse(row)
            except _NotUsd:
                page.skipped["not_usd"] += 1
                continue
            except _Unreadable:
                page.skipped["unreadable"] += 1
                continue
            if parsed is None:
                if _may_still_execute(row, now):
                    page.pending += 1
                else:
                    page.cancelled += 1
                continue
            source_id, day, amount, asset = parsed
            if not amount:
                continue
            entry = LedgerEntry(movement_type, source_id, day, amount, asset)
            if movement_type.stock_sign is not None and entry.stock_amount is None:
                page.without_asset += 1
            page.entries.append(entry)

    for name, movement_type, parse in _LISTS:
        add(movement_type, _rows(data.get(name), name), parse)
    for container, name, movement_type in _NESTED_LISTS:
        for holder in _containers(data.get(container), container):
            add(movement_type, _rows(holder.get(name), f"{container}.{name}"), _dated("amount"))

    divestments = _rows(data.get(_DIVESTMENTS), _DIVESTMENTS)
    page.length = max(page.length, len(divestments))
    page.divestments = len(divestments)
    return page


def ledger_to_transactions(entries: list[LedgerEntry]) -> list[ScrapedTransaction]:
    """Each entry's wallet row, plus its mirror row on the stock position.

    The two legs carry their own external ids (`fintual_wallet_<tag>_<id>`,
    `fintual_stock_<tag>_<id>`): ids are unique across a whole batch.
    """
    transactions: list[ScrapedTransaction] = []
    for entry in entries:
        common = dict(
            institution="fintual",
            currency=STOCKS_CURRENCY,
            description=entry.description,
            transaction_date=entry.day,
            scheduled_month=date(entry.day.year, entry.day.month, 1),
        )
        transactions.append(
            ScrapedTransaction(
                product_kind="wallet",
                amount=entry.wallet_amount,
                external_id=f"fintual_wallet_{entry.type.tag}_{entry.source_id}",
                internal_transfer=entry.type.wallet_internal,
                **common,
            )
        )
        stock_amount = entry.stock_amount
        if stock_amount is not None:
            transactions.append(
                ScrapedTransaction(
                    product_kind="investment",
                    product_external_ref=stock_ref(entry.asset.id),
                    amount=stock_amount,
                    external_id=f"fintual_stock_{entry.type.tag}_{entry.source_id}",
                    internal_transfer=entry.type.stock_internal,
                    **common,
                )
            )
    return transactions


def ledger_balance(entries: list[LedgerEntry]) -> Decimal:
    """What the entries add up to on the wallet: its balance, when the ledger
    is complete."""
    return sum((entry.wallet_amount for entry in entries), Decimal("0.00"))
