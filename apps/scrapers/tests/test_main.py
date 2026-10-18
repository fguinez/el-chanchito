"""Tests for run_scraper's final status decision (success / partial / error).

The DB writer functions are patched out (no database): what's asserted is the
status + error_message handed to `finish_scraper_run` for each combination of
leg and per-row write outcomes, driven through stub scrapers with fabricated
payloads.
"""

import asyncio
from datetime import date
from unittest.mock import MagicMock

import psycopg
import pytest

from product_model import CheckingMetrics, CreditCardMetrics

import main as main_mod
from db.writer import (
    TransactionWriteResult,
    UnresolvedPriceScope,
    UnresolvedRateScope,
)
from main import run_outcome, run_scraper
from scrapers.base import (
    BaseScraper,
    DailyPrice,
    ProductRef,
    ProductScrapeResult,
    ScrapedExchangeRate,
    ScrapedPriceHistory,
    ScrapedProduct,
    ScrapedTransaction,
    SupersededTransaction,
)


class _StubScraper(BaseScraper):
    """Configurable double: each leg returns its canned payload or raises."""

    method = "http_api"
    institution = "stub"

    def __init__(
        self,
        products=None,
        warnings=None,
        tx_exc=None,
        prod_exc=None,
        transactions=None,
        superseded=(),
        exchange_rates=(),
        price_histories=(),
    ):
        self._products = products or []
        self._warnings = warnings or []
        self._tx_exc = tx_exc
        self._prod_exc = prod_exc
        self._transactions = transactions or []
        self._superseded = list(superseded)
        self._exchange_rates = list(exchange_rates)
        self._price_histories = list(price_histories)

    def superseded_transactions(self):
        return self._superseded

    async def scrape_transactions(self):
        if self._tx_exc is not None:
            raise self._tx_exc
        return list(self._transactions)

    async def scrape_products(self):
        if self._prod_exc is not None:
            raise self._prod_exc
        return ProductScrapeResult(
            self._products,
            list(self._warnings),
            list(self._exchange_rates),
            list(self._price_histories),
        )


def _product():
    return ScrapedProduct(
        institution="banchile",
        kind="checking",
        metrics=CheckingMetrics(balance=1000000),
    )


def _card():
    return ScrapedProduct(
        institution="banchile",
        kind="credit_card",
        metrics=CreditCardMetrics(available=999999, limit=2500000),
    )


def _transaction(n):
    return ScrapedTransaction(
        institution="banchile",
        product_kind="checking",
        description="COMPRA SINTETICA",
        amount=-999999,
        transaction_date=date(2026, 9, 1),
        external_id=f"bch_op_000000000{n}",
    )


@pytest.fixture
def writer(monkeypatch):
    """Patch the DB writer seam; yields its mocks by name."""
    mocks = {
        "write_transactions": MagicMock(return_value=TransactionWriteResult()),
        "upsert_product": MagicMock(),
        "upsert_exchange_rate": MagicMock(),
        "upsert_price_history": MagicMock(return_value=0),
        "delete_superseded_transactions": MagicMock(),
        "finish_scraper_run": MagicMock(),
    }
    monkeypatch.setattr(main_mod, "start_scraper_run", MagicMock(return_value="run-1"))
    for name, mock in mocks.items():
        monkeypatch.setattr(main_mod, name, mock)
    return mocks


@pytest.fixture
def finish(writer):
    """The finish_scraper_run mock of the patched writer seam."""
    return writer["finish_scraper_run"]


class TestRunScraperStatus:
    def test_clean_run_records_success(self, finish):
        scraper = _StubScraper(products=[_product()])

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1", "success", transactions_imported=0, error_message=None
        )

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

    def test_a_failed_leg_with_the_other_written_records_partial(self, finish):
        """Balances that landed make a transactions failure partial, not error."""
        scraper = _StubScraper(
            products=[_product()], tx_exc=RuntimeError("session crashed")
        )

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="transactions: session crashed",
        )

    def test_transactions_landing_make_a_products_failure_partial(
        self, writer, finish
    ):
        writer["write_transactions"].return_value = TransactionWriteResult(inserted=2)
        scraper = _StubScraper(
            transactions=[_transaction(1), _transaction(2)],
            prod_exc=RuntimeError("login failed"),
        )

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=2,
            error_message="products: login failed",
        )


class TestPerRowIsolation:
    def test_a_failing_product_does_not_stop_the_rest(self, writer, finish):
        """Every product is still written, and the run is partial with details."""
        writer["upsert_product"].side_effect = [RuntimeError("bad metrics"), None]
        products = [_card(), _product()]
        scraper = _StubScraper(products=products)

        asyncio.run(run_scraper(scraper))

        assert [c.args[0] for c in writer["upsert_product"].call_args_list] == products
        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message=(
                "products: 1 of 2 not written "
                "(banchile/credit_card CLP: bad metrics)"
            ),
        )

    def test_a_database_outage_stops_the_remaining_product_writes(
        self, writer, finish
    ):
        """Once retries are exhausted, the rest would each block on their own."""
        writer["upsert_product"].side_effect = psycopg.OperationalError("db down")
        scraper = _StubScraper(products=[_card(), _product(), _product()])

        asyncio.run(run_scraper(scraper))

        assert writer["upsert_product"].call_count == 1
        finish.assert_called_once_with(
            "run-1",
            "error",
            transactions_imported=0,
            error_message=(
                "products: 3 of 3 not written (banchile/credit_card CLP: db down)"
            ),
        )

    def test_every_product_failing_records_error(self, writer, finish):
        writer["upsert_product"].side_effect = RuntimeError("db gone")
        scraper = _StubScraper(products=[_product()])

        asyncio.run(run_scraper(scraper))

        assert finish.call_args.args[1] == "error"

    def test_failed_transaction_rows_record_partial(self, writer, finish):
        writer["write_transactions"].return_value = TransactionWriteResult(
            inserted=1, failed=1
        )
        scraper = _StubScraper(transactions=[_transaction(1), _transaction(2)])

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=1,
            error_message="transactions: 1 of 2 not written",
        )

    def test_rows_waiting_for_their_product_are_a_warning(self, writer, finish):
        """They don't count as landed, so with nothing else stored the run is
        still partial, not error: the next run lands them."""
        writer["write_transactions"].return_value = TransactionWriteResult(
            inserted=0, unresolved=2
        )
        scraper = _StubScraper(transactions=[_transaction(1), _transaction(2)])

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message=(
                "transactions: 2 of 2 not written: their product does not exist yet"
            ),
        )

    def test_waiting_rows_are_not_counted_as_landed(self, writer, finish):
        """With the products leg failing too, nothing landed: an error."""
        writer["write_transactions"].return_value = TransactionWriteResult(unresolved=2)
        scraper = _StubScraper(
            transactions=[_transaction(1), _transaction(2)],
            prod_exc=RuntimeError("login failed"),
        )

        asyncio.run(run_scraper(scraper))

        finish.assert_called_once_with(
            "run-1",
            "error",
            transactions_imported=0,
            error_message=(
                "products: login failed; "
                "transactions: 2 of 2 not written: their product does not exist yet"
            ),
        )


class TestRunOutcome:
    @pytest.mark.parametrize(
        ("errors", "warnings", "landed", "expected"),
        [
            ([], [], 0, ("success", None)),
            ([], [], 3, ("success", None)),
            ([], ["w"], 0, ("partial", "w")),
            (["e"], [], 1, ("partial", "e")),
            (["e"], ["w"], 1, ("partial", "e; w")),
            (["e"], [], 0, ("error", "e")),
            (["e"], ["w"], 0, ("error", "e; w")),
        ],
    )
    def test_status_and_message(self, errors, warnings, landed, expected):
        """A failure is `error` only when nothing landed."""
        assert run_outcome(errors, warnings, landed) == expected


_TWIN = SupersededTransaction(
    institution="banchile",
    product_kind="credit_card",
    currency="CLP",
    transaction_date=date(2026, 8, 20),
    amount=-12,
    replacement_external_id="bch_fp_00000000000000ff",
    created_before_migration="V023__transactions_amount_numeric.sql",
    external_id_prefix="bch_fp_",
)


class TestSupersededCleanup:
    """Rows a scrape replaced are deleted once its transactions are written."""

    def test_runs_after_the_write_with_what_it_persisted(self, finish, monkeypatch):
        calls = []
        written = TransactionWriteResult(
            persisted_external_ids=frozenset({"bch_fp_00000000000000ff"})
        )
        monkeypatch.setattr(
            main_mod, "write_transactions", lambda txns: calls.append("write") or written
        )
        monkeypatch.setattr(
            main_mod,
            "delete_superseded_transactions",
            lambda items, persisted, keys: calls.append(("delete", items, persisted, keys)),
        )

        asyncio.run(run_scraper(_StubScraper(superseded=[_TWIN])))

        assert calls == [
            "write",
            ("delete", [_TWIN], written.persisted_external_ids, set()),
        ]

    def test_skipped_when_there_is_nothing_to_delete(self, finish):
        asyncio.run(run_scraper(_StubScraper()))

        main_mod.delete_superseded_transactions.assert_not_called()

    def test_skipped_when_the_transactions_leg_failed(self, finish):
        scraper = _StubScraper(tx_exc=RuntimeError("login failed"), superseded=[_TWIN])

        asyncio.run(run_scraper(scraper))

        main_mod.delete_superseded_transactions.assert_not_called()

    def test_a_cleanup_failure_is_a_warning(self, finish, monkeypatch):
        monkeypatch.setattr(
            main_mod,
            "delete_superseded_transactions",
            MagicMock(side_effect=RuntimeError("db down")),
        )

        asyncio.run(run_scraper(_StubScraper(superseded=[_TWIN])))

        finish.assert_called_once_with(
            "run-1", "partial", transactions_imported=0, error_message="cleanup: db down"
        )


def _rate():
    return ScrapedExchangeRate(
        institution="banchile",
        base_currency="USD",
        rate="950.00",
        product=ProductRef(kind="credit_card", currency="USD"),
        source="Pago internacional de tarjeta",
    )


class TestExchangeRates:
    """The products leg's rates are written after its products, each on its own."""

    def test_written_after_the_products(self, finish, monkeypatch):
        calls = []
        rate = _rate()
        monkeypatch.setattr(
            main_mod, "upsert_product", lambda sp: calls.append("product")
        )
        monkeypatch.setattr(
            main_mod, "upsert_exchange_rate", lambda fx: calls.append(("rate", fx))
        )

        scraper = _StubScraper(products=[_product()], exchange_rates=[rate])

        asyncio.run(run_scraper(scraper))

        assert calls == ["product", ("rate", rate)]
        finish.assert_called_once_with(
            "run-1", "success", transactions_imported=0, error_message=None
        )

    def test_an_unresolvable_scope_is_a_warning(self, finish, monkeypatch):
        monkeypatch.setattr(
            main_mod,
            "upsert_exchange_rate",
            MagicMock(side_effect=UnresolvedRateScope("no credit_card product in USD")),
        )

        asyncio.run(run_scraper(_StubScraper(exchange_rates=[_rate()])))

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="exchange rate USD/CLP: no credit_card product in USD",
        )

    def test_a_write_failure_costs_only_that_rate(self, finish, monkeypatch):
        written = []

        def upsert(fx):
            if not written:
                written.append(None)
                raise RuntimeError("db down")
            written.append(fx)

        monkeypatch.setattr(main_mod, "upsert_exchange_rate", upsert)
        second = _rate()

        asyncio.run(run_scraper(_StubScraper(exchange_rates=[_rate(), second])))

        assert written == [None, second]
        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="exchange rate USD/CLP: db down",
        )

    def test_still_written_when_a_product_write_fails(self, finish, monkeypatch):
        monkeypatch.setattr(
            main_mod, "upsert_product", MagicMock(side_effect=RuntimeError("db down"))
        )

        scraper = _StubScraper(products=[_product()], exchange_rates=[_rate()])

        asyncio.run(run_scraper(scraper))

        main_mod.upsert_exchange_rate.assert_called_once()
        finish.assert_called_once_with(
            "run-1",
            "error",
            transactions_imported=0,
            error_message="products: 1 of 1 not written (banchile/checking CLP: db down)",
        )


def _stock():
    return ScrapedProduct(
        institution="fintual",
        kind="stock",
        currency="USD",
        external_ref="stock:0001",
    )


def _history(external_ref="stock:0001"):
    """A synthetic two-day ACME series."""
    return ScrapedPriceHistory(
        institution="fintual",
        product=ProductRef(
            kind="stock", currency="USD", external_ref=external_ref
        ),
        points=[
            DailyPrice(date=date(2026, 1, 2), price="100.0"),
            DailyPrice(date=date(2026, 1, 5), price="250.5"),
        ],
    )


_UNRESOLVED_STOCK = "no stock product in USD with that external_ref at 'fintual'"


class TestPriceHistories:
    """The products leg's price histories are written after its products,
    each on its own."""

    def test_written_after_the_products(self, finish, monkeypatch):
        calls = []
        history = _history()
        monkeypatch.setattr(
            main_mod, "upsert_product", lambda sp: calls.append("product")
        )
        monkeypatch.setattr(
            main_mod, "upsert_price_history", lambda h: calls.append(("history", h))
        )

        scraper = _StubScraper(products=[_stock()], price_histories=[history])

        asyncio.run(run_scraper(scraper))

        assert calls == ["product", ("history", history)]
        finish.assert_called_once_with(
            "run-1", "success", transactions_imported=0, error_message=None
        )

    def test_an_unresolvable_product_is_a_warning(self, finish, monkeypatch):
        monkeypatch.setattr(
            main_mod,
            "upsert_price_history",
            MagicMock(side_effect=UnresolvedPriceScope(_UNRESOLVED_STOCK)),
        )

        asyncio.run(
            run_scraper(_StubScraper(products=[_stock()], price_histories=[_history()]))
        )

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="price histories: 1 of 1 not written (UnresolvedPriceScope)",
        )

    def test_failed_series_share_one_counted_warning(self, finish, monkeypatch):
        """N unresolved series are one warning, not N identical ones."""
        refs = ("stock:0001", "stock:0002", "stock:0003")

        def upsert(history):
            if history.product.external_ref != "stock:0002":
                raise UnresolvedPriceScope(_UNRESOLVED_STOCK)

        monkeypatch.setattr(main_mod, "upsert_price_history", upsert)

        asyncio.run(
            run_scraper(
                _StubScraper(
                    products=[_stock()], price_histories=[_history(r) for r in refs]
                )
            )
        )

        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="price histories: 2 of 3 not written (UnresolvedPriceScope)",
        )

    def test_the_warning_names_each_failure_type_once(self, finish, monkeypatch):
        upsert = MagicMock(
            side_effect=[
                UnresolvedPriceScope(_UNRESOLVED_STOCK),
                RuntimeError("synthetic failure"),
                UnresolvedPriceScope(_UNRESOLVED_STOCK),
            ]
        )
        monkeypatch.setattr(main_mod, "upsert_price_history", upsert)
        histories = [_history(f"stock:000{n}") for n in (1, 2, 3)]

        asyncio.run(
            run_scraper(_StubScraper(products=[_stock()], price_histories=histories))
        )

        assert upsert.call_count == 3
        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message=(
                "price histories: 3 of 3 not written "
                "(RuntimeError, UnresolvedPriceScope)"
            ),
        )

    def test_a_write_failure_costs_only_that_series(self, finish, monkeypatch):
        written = []

        def upsert(history):
            if not written:
                written.append(None)
                raise RuntimeError("synthetic failure")
            written.append(history)

        monkeypatch.setattr(main_mod, "upsert_price_history", upsert)
        second = _history("stock:0002")

        asyncio.run(
            run_scraper(
                _StubScraper(products=[_stock()], price_histories=[_history(), second])
            )
        )

        assert written == [None, second]
        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="price histories: 1 of 2 not written (RuntimeError)",
        )

    def test_a_connection_error_stops_the_remaining_series(self, finish, monkeypatch):
        """upsert_price_history already retried: the rest would only block,
        and they count as not written."""
        upsert = MagicMock(side_effect=psycopg.OperationalError("db down"))
        monkeypatch.setattr(main_mod, "upsert_price_history", upsert)
        histories = [_history(f"stock:000{n}") for n in (1, 2, 3)]

        asyncio.run(
            run_scraper(_StubScraper(products=[_stock()], price_histories=histories))
        )

        upsert.assert_called_once()
        finish.assert_called_once_with(
            "run-1",
            "partial",
            transactions_imported=0,
            error_message="price histories: 3 of 3 not written (OperationalError)",
        )

    def test_the_summary_counts_the_points_read(self, writer, finish, caplog):
        caplog.set_level("INFO", logger="scraper-service")
        histories = [_history("stock:0001"), _history("stock:0002")]

        asyncio.run(
            run_scraper(_StubScraper(products=[_stock()], price_histories=histories))
        )

        assert "1 products, 4 price points read" in caplog.text

    def test_nothing_is_written_when_the_products_leg_fails(self, writer, finish):
        scraper = _StubScraper(
            prod_exc=RuntimeError("synthetic outage"), price_histories=[_history()]
        )

        asyncio.run(run_scraper(scraper))

        writer["upsert_price_history"].assert_not_called()
        finish.assert_called_once_with(
            "run-1",
            "error",
            transactions_imported=0,
            error_message="products: synthetic outage",
        )
