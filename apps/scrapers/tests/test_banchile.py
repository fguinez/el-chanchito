"""Tests for the BanChile scraper movement conversion."""

import asyncio
import os
import pytest
from datetime import datetime, date
from decimal import Decimal
from unittest.mock import MagicMock

os.environ.setdefault("BANCHILE_RUT", "test")
os.environ.setdefault("BANCHILE_PASSWORD", "test")

from product_model import CheckingMetrics

from scrapers.backends.banchile_web import BalanceFetchResult
from scrapers.base import ScrapedProduct
from scrapers.institutions import banchile as banchile_mod
from scrapers.institutions.banchile import BanChileScraper


def _make_movement(
    dt=None, description="TEST", amount=-10000, acct_type="corriente", acct_id="1234"
):
    """Create a mock fintself MovementModel."""
    mock = MagicMock()
    mock.date = dt or datetime(2026, 4, 10, 12, 0)
    mock.description = description
    mock.amount = Decimal(str(amount))
    mock.account_type = MagicMock(value=acct_type)
    mock.account_id = acct_id
    # A plain string, as in fintself — the pydantic envelope validates it.
    mock.transaction_type = "compra"
    return mock


class TestMovementConversion:
    def setup_method(self):
        self.scraper = BanChileScraper()

    def test_basic_conversion(self):
        mov = _make_movement()
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.amount == -10000
        assert txn.institution == "banchile"
        assert txn.description == "TEST"

    def test_checking_account_type(self):
        mov = _make_movement(acct_type="corriente")
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.product_kind == "checking"

    def test_credit_card_type(self):
        mov = _make_movement(acct_type="credito")
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.product_kind == "credit_card"

    def test_datetime_to_date_conversion(self):
        mov = _make_movement(dt=datetime(2026, 4, 10, 15, 30))
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.transaction_date == date(2026, 4, 10)

    def test_date_passthrough(self):
        """If date is already a date object, don't crash."""
        mov = _make_movement(dt=date(2026, 4, 10))
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.transaction_date == date(2026, 4, 10)

    def test_scheduled_month_derived(self):
        mov = _make_movement(dt=datetime(2026, 4, 15, 10, 0))
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.scheduled_month == date(2026, 4, 1)

    def test_external_id_stable(self):
        """Same movement data should produce the same external_id."""
        mov1 = _make_movement()
        mov2 = _make_movement()
        txn1 = self.scraper._movement_to_transaction(mov1)
        txn2 = self.scraper._movement_to_transaction(mov2)
        assert txn1.external_id == txn2.external_id

    def test_different_movements_different_ids(self):
        mov1 = _make_movement(description="COMPRA A")
        mov2 = _make_movement(description="COMPRA B")
        txn1 = self.scraper._movement_to_transaction(mov1)
        txn2 = self.scraper._movement_to_transaction(mov2)
        assert txn1.external_id != txn2.external_id

    def test_positive_amount_income(self):
        mov = _make_movement(amount=500000)
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.amount == 500000

    def test_none_account_type_defaults_checking(self):
        mov = _make_movement()
        mov.account_type = None
        txn = self.scraper._movement_to_transaction(mov)
        assert txn.product_kind == "checking"


class TestScrapeProducts:
    """scrape_products() delegates to the banchile_web backend and shields the
    run from a flaky second login."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def test_returns_backend_products(self, monkeypatch):
        expected = [
            ScrapedProduct(
                institution="banchile",
                kind="checking",
                metrics=CheckingMetrics(balance=1234567),
            )
        ]

        async def fake_fetch(rut, password):
            assert (rut, password) == (self.scraper.rut, self.scraper.password)
            return BalanceFetchResult(products=expected, failed_surfaces=())

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == expected
        assert result.warnings == []

    def test_failed_surfaces_become_warnings(self, monkeypatch):
        """Surfaces the backend exhausted are reported, one warning each."""
        async def fake_fetch(rut, password):
            return BalanceFetchResult(products=[], failed_surfaces=("card", "línea"))

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == []
        assert result.warnings == [
            "BanChile: card surface failed after 3 attempts",
            "BanChile: línea surface failed after 3 attempts",
        ]

    def test_per_holding_surface_labels_reach_warnings(self, monkeypatch):
        """The issue #36 surfaces warn as depósitos/fondos (not 'inversiones')."""
        async def fake_fetch(rut, password):
            return BalanceFetchResult(
                products=[], failed_surfaces=("depósitos", "fondos")
            )

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.warnings == [
            "BanChile: depósitos surface failed after 3 attempts",
            "BanChile: fondos surface failed after 3 attempts",
        ]

    def test_backend_failure_is_swallowed(self, monkeypatch):
        """A heavy, flaky second login must not fail the whole run."""
        async def boom(rut, password):
            raise RuntimeError("login failed")

        monkeypatch.setattr(banchile_mod, "fetch_balances", boom)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == []
        assert result.warnings == ["BanChile: product scrape crashed: login failed"]
