"""Tests for the BanChile scraper movement conversion."""

import asyncio
import os
import pytest
from datetime import datetime, date
from decimal import Decimal
from unittest.mock import MagicMock

os.environ.setdefault("BANCHILE_RUT", "test")
os.environ.setdefault("BANCHILE_PASSWORD", "test")

from scrapers.base import ScrapedBalance
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


class TestScrapeBalances:
    """scrape_balances() delegates to the banchile_web backend and shields the
    run from a flaky second login."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def test_returns_backend_balances(self, monkeypatch):
        expected = [
            ScrapedBalance(
                institution="banchile",
                product_kind="checking",
                balance=1234567,
                as_of=date.today(),
            )
        ]

        async def fake_fetch(rut, password):
            assert (rut, password) == (self.scraper.rut, self.scraper.password)
            return expected

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)
        assert asyncio.run(self.scraper.scrape_balances()) == expected

    def test_backend_failure_is_swallowed(self, monkeypatch):
        async def boom(rut, password):
            raise RuntimeError("login failed")

        monkeypatch.setattr(banchile_mod, "fetch_balances", boom)
        # A heavy, flaky second login must not fail the whole run.
        assert asyncio.run(self.scraper.scrape_balances()) == []
