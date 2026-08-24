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

    def _scrape(self, monkeypatch, movements):
        """Run scrape_transactions() over a canned fintself movement list."""
        async def fake_scraper(bank_key, user, password):
            return movements

        monkeypatch.setattr(banchile_mod, "run_fintself_scraper", fake_scraper)
        return asyncio.run(self.scraper.scrape_transactions())

    def test_identical_same_day_movements_get_distinct_ids(self, monkeypatch):
        """Five genuinely distinct transfers that look alike must not collapse.

        Same date, description, amount and account: before the occurrence
        index they hashed to one id and the writer's ON CONFLICT DO NOTHING
        kept only the first.
        """
        movements = [
            _make_movement(description="TRANSFERENCIA DE TERCERO", amount=1000000)
            for _ in range(5)
        ]

        txns = self._scrape(monkeypatch, movements)

        assert len(txns) == 5
        assert len({t.external_id for t in txns}) == 5

    def test_first_occurrence_keeps_legacy_id(self, monkeypatch):
        """Backwards compatible: rows already imported keep matching.

        The literal is the id the pre-fix scheme emitted for this synthetic
        movement, md5 of `2026-04-10|TRANSFERENCIA DE TERCERO|1000000|1234`.
        If it ever changes, every BdC row already stored needs a migration or
        the next scrape re-imports the whole history as duplicates.
        """
        movements = [
            _make_movement(description="TRANSFERENCIA DE TERCERO", amount=1000000)
            for _ in range(5)
        ]

        txns = self._scrape(monkeypatch, movements)

        assert txns[0].external_id == "bch_f8d33350bb56255e"

    def test_distinct_movements_stay_distinct_across_scrapes(self, monkeypatch):
        """Different movements keep different ids; a repeat-free scrape is stable."""
        movements = [
            _make_movement(description="COMPRA A", amount=-999999),
            _make_movement(description="COMPRA B", amount=-999999),
            _make_movement(description="COMPRA A", amount=-2500000),
        ]

        first_run = self._scrape(monkeypatch, movements)
        second_run = self._scrape(monkeypatch, movements)

        assert len({t.external_id for t in first_run}) == 3
        assert [t.external_id for t in first_run] == [t.external_id for t in second_run]

    def test_interleaved_movements_dont_shift_duplicate_ids(self, monkeypatch):
        """The counter is per identity key, not per position in the list."""
        def duplicate():
            return _make_movement(description="TRANSFERENCIA DE TERCERO", amount=1000000)

        contiguous = [duplicate(), duplicate(), duplicate()]
        interleaved = [
            duplicate(),
            _make_movement(description="COMPRA A", amount=-999999),
            duplicate(),
            _make_movement(description="COMPRA B", amount=-2500000),
            duplicate(),
        ]

        contiguous_ids = [t.external_id for t in self._scrape(monkeypatch, contiguous)]
        interleaved_txns = self._scrape(monkeypatch, interleaved)

        dup_ids = [
            t.external_id
            for t in interleaved_txns
            if t.description == "TRANSFERENCIA DE TERCERO"
        ]
        assert dup_ids == contiguous_ids

    def test_unconvertible_movement_doesnt_consume_an_occurrence(self, monkeypatch):
        """A skipped movement must not shift the surviving repeats' indices."""
        # It must fail *after* its key is computed, so the test can tell
        # whether the counter is bumped before or after a successful convert:
        # a non-str category_hint is rejected by the ScrapedTransaction envelope.
        broken = _make_movement(description="TRANSFERENCIA DE TERCERO", amount=1000000)
        broken.transaction_type = object()
        movements = [
            _make_movement(description="TRANSFERENCIA DE TERCERO", amount=1000000),
            broken,
            _make_movement(description="TRANSFERENCIA DE TERCERO", amount=1000000),
        ]
        healthy = [
            _make_movement(description="TRANSFERENCIA DE TERCERO", amount=1000000)
            for _ in range(2)
        ]

        txns = self._scrape(monkeypatch, movements)
        expected = [t.external_id for t in self._scrape(monkeypatch, healthy)]

        assert [t.external_id for t in txns] == expected


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
