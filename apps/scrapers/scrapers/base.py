"""Abstract base class for all scrapers.

The scraped-data contract (the `ScrapedProduct` / `ScrapedTransaction`
envelopes) lives in the shared `product_model` package — the same pydantic
models that will validate the future REST ingest API — and is re-exported
here so scrapers keep a single import point.
"""

from abc import ABC, abstractmethod

from product_model import ScrapedProduct, ScrapedTransaction

__all__ = ["BaseScraper", "ScrapedProduct", "ScrapedTransaction"]


class BaseScraper(ABC):
    """Interface that all scrapers must implement.

    `method` = how we scrape (email, fintself, http_api, open_banking)
    `institution` = what we scrape (mach, banchile, buda, ...)
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
    async def scrape_products(self) -> list[ScrapedProduct]:
        """Fetch current products as typed observations (attributes/metrics)."""
        ...
