"""Abstract base class for all scrapers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class ScrapedTransaction:
    """A transaction extracted by a scraper."""

    account_institution: str  # e.g. 'banchile', 'fintual'
    account_type: str  # e.g. 'checking', 'credit_card'
    description: str
    amount: int  # CLP, negative=expense, positive=income
    transaction_date: date
    external_id: str  # unique identifier for dedup
    category_hint: Optional[str] = None  # scraper's best guess
    scheduled_month: Optional[date] = None


@dataclass
class ScrapedBalance:
    """A balance snapshot from a scraper."""

    account_institution: str
    account_type: str
    balance: int  # CLP
    as_of: date


class BaseScraper(ABC):
    """Interface that all scrapers must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this scraper (used in scraper_runs table)."""
        ...

    @abstractmethod
    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """Fetch recent transactions from the source."""
        ...

    @abstractmethod
    async def scrape_balances(self) -> list[ScrapedBalance]:
        """Fetch current account balances."""
        ...
