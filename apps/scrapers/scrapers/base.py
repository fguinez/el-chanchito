"""Abstract base class for all scrapers.

The scraped-data contract (the `ScrapedProduct` / `ScrapedTransaction` /
`ScrapedExchangeRate` envelopes) lives in the shared `product_model` package,
the same pydantic models that will validate the future REST ingest API, and is
re-exported here so scrapers keep a single import point.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from product_model import (
    ProductRef,
    ScrapedExchangeRate,
    ScrapedProduct,
    ScrapedTransaction,
    TransactionLocation,
)

__all__ = [
    "BaseScraper",
    "ProductRef",
    "ProductScrapeResult",
    "ScrapedExchangeRate",
    "ScrapedProduct",
    "ScrapedTransaction",
    "SupersededTransaction",
    "TransactionLocation",
]


@dataclass
class ProductScrapeResult:
    """Outcome of a products leg: the scraped products plus non-fatal warnings.

    Warnings flag partial coverage — e.g. a BanChile surface that stayed empty
    after all its retries — so `run_scraper` can record the run as `partial`
    without failing the leg.

    `exchange_rates` are the rates the institution applies that the leg read
    (e.g. BanChile's USD card rate); `run_scraper` writes them after the
    products, so a product-scoped rate finds the product it names.
    """

    products: list[ScrapedProduct]
    warnings: list[str] = field(default_factory=list)
    exchange_rates: list[ScrapedExchangeRate] = field(default_factory=list)


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
