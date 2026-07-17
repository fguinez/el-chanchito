"""Tests for run_scraper's final status decision (success / partial / error).

The DB writer functions are patched out (no database): what's asserted is the
status + error_message handed to `finish_scraper_run` for each combination of
leg outcomes, driven through stub scrapers with fabricated payloads.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from product_model import CheckingMetrics

import main as main_mod
from main import run_scraper
from scrapers.base import BaseScraper, ProductScrapeResult, ScrapedProduct


class _StubScraper(BaseScraper):
    """Configurable double: each leg returns its canned payload or raises."""

    method = "http_api"
    institution = "stub"

    def __init__(self, products=None, warnings=None, tx_exc=None, prod_exc=None):
        self._products = products or []
        self._warnings = warnings or []
        self._tx_exc = tx_exc
        self._prod_exc = prod_exc

    async def scrape_transactions(self):
        if self._tx_exc is not None:
            raise self._tx_exc
        return []

    async def scrape_products(self):
        if self._prod_exc is not None:
            raise self._prod_exc
        return ProductScrapeResult(self._products, list(self._warnings))


def _product():
    return ScrapedProduct(
        institution="banchile",
        kind="checking",
        metrics=CheckingMetrics(balance=1000000),
    )


@pytest.fixture
def finish(monkeypatch):
    """Patch the DB writer seam; yields the finish_scraper_run mock."""
    mock = MagicMock()
    monkeypatch.setattr(main_mod, "start_scraper_run", MagicMock(return_value="run-1"))
    monkeypatch.setattr(main_mod, "upsert_transactions", MagicMock(return_value=0))
    monkeypatch.setattr(main_mod, "upsert_product", MagicMock())
    monkeypatch.setattr(main_mod, "finish_scraper_run", mock)
    return mock


class TestRunScraperStatus:
    def test_clean_run_records_success(self, finish):
        scraper = _StubScraper(products=[_product()])

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with("run-1", "success", transactions_imported=0)

    def test_product_warnings_record_partial(self, finish):
        scraper = _StubScraper(
            products=[_product()],
            warnings=["BanChile: card surface failed after 3 attempts"],
        )

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="BanChile: card surface failed after 3 attempts",
        )

    def test_leg_exception_records_error(self, finish):
        scraper = _StubScraper(prod_exc=RuntimeError("login failed"))

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "error",
            transactions_imported=0,
            error_message="products: login failed",
        )

    def test_error_message_carries_warnings_too(self, finish):
        scraper = _StubScraper(
            warnings=["BanChile: línea surface failed after 3 attempts"],
            tx_exc=RuntimeError("imap down"),
        )

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "error",
            transactions_imported=0,
            error_message=(
                "transactions: imap down; "
                "BanChile: línea surface failed after 3 attempts"
            ),
        )
