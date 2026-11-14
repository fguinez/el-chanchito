"""Abstract base class for all scrapers.

The scraped-data contract (the `ScrapedProduct` / `ScrapedTransaction` /
`ScrapedExchangeRate` / `ScrapedPriceHistory` envelopes) lives in the shared
`product_model` package, the same pydantic models that will validate the future
REST ingest API, and is re-exported here so scrapers keep a single import
point.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from product_model import (
    BaseMetrics,
    DailyPrice,
    ProductRef,
    ScrapedExchangeRate,
    ScrapedPriceHistory,
    ScrapedProduct,
    ScrapedTransaction,
    TransactionLocation,
)

__all__ = [
    "BILLING_STATUS_BY_FLAG",
    "BaseScraper",
    "BilledStatement",
    "DailyPrice",
    "ProductRef",
    "ProductScrapeResult",
    "ScrapedExchangeRate",
    "ScrapedPriceHistory",
    "ScrapedProduct",
    "ScrapedTransaction",
    "SupersededTransaction",
    "TransactionLocation",
    "VanishedProductScope",
    "one_per_key_billed_first",
]

# A scraper's "read in the billed view" flag -> `ScrapedTransaction.billing_status`
# (None, an unknown view, maps to no status).
BILLING_STATUS_BY_FLAG = {True: "billed", False: "unbilled"}


def one_per_key_billed_first(
    transactions: list[ScrapedTransaction],
) -> list[ScrapedTransaction]:
    """One transaction per `external_id`, a billed copy beating any other.

    For a scraper that reads a card's unbilled and billed views and keys a
    charge the same in both: each key stays where it was first seen, and a
    later billed copy replaces an earlier one that is not billed, whatever the
    read order. Any other repeat is dropped.
    """
    kept: list[ScrapedTransaction] = []
    position: dict[str, int] = {}
    for transaction in transactions:
        index = position.get(transaction.external_id)
        if index is None:
            position[transaction.external_id] = len(kept)
            kept.append(transaction)
        elif transaction.billing_status == "billed" and kept[index].billing_status != "billed":
            kept[index] = transaction
    return kept


@dataclass(frozen=True)
class VanishedProductScope:
    """A set of per-holding products a products leg listed in full.

    Every product of the institution's (kind, currency) whose `external_ref`
    starts with `external_ref_prefix` that still exists at the source is in
    `present_refs`, so a stored active one missing from it was closed there
    (a savings pot the user emptied and closed, say).
    `db.writer.retire_vanished_products` flags those on their first miss and
    retires them on the second complete listing in a row that lacks them:
    inactive, with a `closed_metrics` snapshot (a zero balance) so its
    history stays and net worth stops counting it. A scraper emits a scope only from a read it
    knows is complete. Never part of the ingest contract.
    """

    institution: str
    product_kind: str
    currency: str
    external_ref_prefix: str
    present_refs: frozenset[str]
    closed_metrics: BaseMetrics

    def __post_init__(self) -> None:
        # A retired product with no balance would vanish from the web, history included.
        if self.closed_metrics.headline() is None:
            raise ValueError("closed_metrics must have a headline value")


@dataclass
class ProductScrapeResult:
    """Outcome of a products leg: the scraped products plus non-fatal warnings.

    Warnings flag partial coverage — e.g. a BanChile surface that stayed empty
    after all its retries — so `run_scraper` can record the run as `partial`
    without failing the leg.

    `exchange_rates` are the rates the institution applies that the leg read
    (e.g. BanChile's USD card rate); `run_scraper` writes them after the
    products, so a product-scoped rate finds the product it names.

    `price_histories` are the daily closing prices the leg read for some of
    its products (e.g. Fintual's stock positions); `run_scraper` writes them
    after the products too, for the same reason.

    `vanished_product_scopes` name the product sets the leg listed in full
    (see `VanishedProductScope`); `run_scraper` flags, then retires, the
    stored products they no longer list, only once every product of the leg
    was written.
    """

    products: list[ScrapedProduct]
    warnings: list[str] = field(default_factory=list)
    exchange_rates: list[ScrapedExchangeRate] = field(default_factory=list)
    price_histories: list[ScrapedPriceHistory] = field(default_factory=list)
    vanished_product_scopes: list[VanishedProductScope] = field(default_factory=list)


@dataclass(frozen=True)
class SupersededTransaction:
    """A stored row an earlier version of a scraper wrote on the wrong product.

    The scraper describes it and `db.writer.delete_superseded_transactions`
    removes at most one stored row per instance: by `external_id` when the old
    key is reproducible, else by date and amount among the rows whose key
    starts with `external_id_prefix`, and only once the row that replaces it,
    `replacement_external_id`, is stored. Only rows created before the
    migration `created_before_migration` was applied qualify: that migration
    ships with the fix, so a later row cannot be the old version's mistake.
    Never part of the ingest contract.
    """

    institution: str
    product_kind: str
    currency: str
    transaction_date: date
    amount: int
    replacement_external_id: str
    created_before_migration: str
    external_id_prefix: str
    external_id: Optional[str] = None


@dataclass(frozen=True)
class VanishedWindow:
    """A stretch of one product's history that a scrape listed in full.

    Every pending row of that stretch is in the scrape, so a stored pending row
    the scrape no longer carries is one the source dropped: a stale card
    authorization, a charge the bank re-dated or re-described, a reversed
    pair. `db.writer.soft_delete_vanished_transactions` soft-deletes those
    rows: on the institution's (kind, currency) product, written by its own
    scraper, keyed under `external_id_prefix`, dated after `after`, with the
    `unbilled` billing status (or none at all, when `include_unreported`), and
    never one the user edited. Never part of the ingest contract.
    """

    institution: str
    product_kind: str
    currency: str
    external_id_prefix: str
    after: date
    include_unreported: bool = False


@dataclass(frozen=True)
class BilledStatement:
    """One card statement and the keys of the movements it billed, for
    `db.writer.mark_billed_statements`. Never part of the ingest contract."""

    institution: str
    product_kind: str
    currency: str
    statement_date: date
    external_ids: frozenset[str]


class BaseScraper(ABC):
    """Interface that all scrapers must implement.

    `method` = how we scrape (email, fintself, web, http_api, open_banking)
    `institution` = what we scrape (mach, banchile, bci_lider, buda, ...)
    Both are stored on each `scraper_runs` row.
    """

    @property
    @abstractmethod
    def method(self) -> str:
        ...

    @property
    @abstractmethod
    def institution(self) -> str:
        ...

    @property
    def name(self) -> str:
        """Log/display label; DB uses method+institution directly."""
        return f"{self.method}_{self.institution}"

    @abstractmethod
    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """Fetch recent transactions from the source."""
        ...

    @abstractmethod
    async def scrape_products(self) -> ProductScrapeResult:
        """Fetch current products as typed observations (attributes/metrics)."""
        ...

    def superseded_transactions(self) -> list[SupersededTransaction]:
        """Stored rows the last `scrape_transactions` replaced; none by default.

        `run_scraper` deletes them after writing that scrape's transactions.
        """
        return []

    def vanished_windows(self) -> list[VanishedWindow]:
        """Windows the last `scrape_transactions` listed in full; none by default.

        `run_scraper` soft-deletes the pending rows in them that scrape no
        longer carried, once every one of its transactions was written.
        """
        return []

    def billed_statements(self) -> list[BilledStatement]:
        """Statements the last `scrape_transactions` read; none by default.

        `run_scraper` marks the stored rows each one lists as billed on it,
        after writing that scrape's transactions.
        """
        return []
