"""Tests for the pure Fintual stocks backend: WalletMovements pages in,
ledger entries and transactions out.

No network and no scraper: each test builds a WalletMovements `data` payload
with the shapes the recon recorded. Every id, ticker, date and figure is
synthetic (asset ids 1001..1003, tickers ACME/GLBX/INITK).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scrapers.backends import fintual_stocks
from scrapers.backends.fintual_stocks import (
    WALLET_MOVEMENTS_QUERY,
    ledger_balance,
    ledger_to_transactions,
    local_date,
    parse_wallet_page,
    shows_unsold_stocks,
    sold_out_products,
    stock_ref,
    usd,
)


def _money(amount, currency="USD"):
    return {"amount": amount, "currency": currency}


def _asset(asset_id="1001", symbol="ACME", name="Acme Corp"):
    return {"id": asset_id, "symbol": symbol, "name": name}


def _deposit(dep_id="2001", amount=1234.56, executed_at="2026-01-02T15:04:05Z", currency="USD"):
    """A stocksDomesticBankDeposits row: CLP sent from a bank, converted to USD."""
    forex = None
    if executed_at is not None:
        forex = {
            "id": f"3{dep_id}",
            "executedAt": executed_at,
            "foreignCapital": _money(amount, currency),
        }
    return {
        "id": dep_id,
        "capital": _money(1000000, "CLP"),
        "declaredAt": "2026-01-01T12:00:00Z",
        "forexOrder": forex,
    }


def _withdrawal(wdr_id="2101", amount=100.0, executed_at="2026-01-20T15:00:00Z"):
    return {
        "id": wdr_id,
        "paidAt": None,
        "paymentDate": None,
        "declaredAt": "2026-01-19T12:00:00Z",
        "requestedCapital": _money(amount),
        "forexOrder": {
            "id": f"3{wdr_id}",
            "executedAt": executed_at,
            "domesticCapital": _money(1000000, "CLP"),
            "foreignCapital": _money(amount),
        },
    }


def _order(order_id="5001", asset=None, capital=999.99, fulfilled_at="2026-01-05T15:00:00Z"):
    """A subscription or redemption instruction around its asset order."""
    return {
        "id": f"4{order_id}",
        "declaredAt": "2026-01-05T14:00:00Z",
        "requestedCapital": _money(capital),
        "requestedShares": None,
        "assetOrder": {
            "id": order_id,
            "capital": _money(capital),
            "asset": _asset() if asset is None else asset,
            "fulfilledAt": fulfilled_at,
        },
    }


def _dividend(div_id="6001", amount=12.34, day="2026-01-10", asset=None):
    asset = {"id": "1001", "symbol": "ACME"} if asset is None else asset
    return {
        "id": div_id,
        "date": day,
        "createdAt": f"{day}T12:00:00Z",
        "netCapital": _money(amount),
        "asset": asset,
    }


def _dated(row_id, amount, day="2026-01-15", key="amount"):
    """A row of the lists stamped with a day: interest, compensations, ACAT..."""
    return {"id": row_id, "date": day, "createdAt": f"{day}T12:00:00Z", key: amount}


def _page(**lists):
    """A WalletMovements `data`: every list empty unless given."""
    data = {
        "stocksWalletDividends": [],
        "stocksDomesticBankDeposits": [],
        "stocksDomesticBankWithdrawals": [],
        "stocksDivestments": [],
        "stocksWalletAssetSubscriptionInstructions": [],
        "stocksWalletAssetRedemptionInstructions": [],
        "stocksWalletCashMergers": [],
        "stocksWalletCompensations": [],
        "stocksWalletCashInterests": [],
        "stocksWalletReturnOfCapitals": {"id": "7000", "returnOfCapitals": []},
        "stocksWalletAcatCashActivities": {"fees": [], "cashInbounds": [], "cashOutbounds": []},
        "stocksWalletWarrantExerciseCashActivities": {"id": "8000", "fees": [], "costs": []},
    }
    data.update(lists)
    return data


# Within a week of the fixtures' deposits and orders being declared: those
# not executed yet are pending.
_NOW = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)


def _parse(data, now=_NOW):
    return parse_wallet_page(data, now)


def _transactions(**lists):
    return ledger_to_transactions(_parse(_page(**lists)).entries)


def _by_id(transactions):
    return {t.external_id: t for t in transactions}


class TestHelpers:
    def test_the_stock_ref_is_keyed_by_asset_id(self):
        assert stock_ref("1001") == "stock:1001"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(1234.56, Decimal("1234.56"), id="cents"),
            pytest.param(0.125, Decimal("0.13"), id="half-up"),
            pytest.param(100, Decimal("100.00"), id="int"),
            pytest.param("12.34", None, id="string"),
            pytest.param(True, None, id="bool"),
            pytest.param(float("nan"), None, id="nan"),
        ],
    )
    def test_usd_amounts(self, raw, expected):
        assert usd(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("2026-01-02", date(2026, 1, 2), id="bare-date"),
            pytest.param("2026-01-02T15:04:05Z", date(2026, 1, 2), id="afternoon-utc"),
            pytest.param("2026-01-02T01:30:00Z", date(2026, 1, 1), id="early-utc-is-yesterday"),
            pytest.param("2026-07-02T03:30:00+00:00", date(2026, 7, 1), id="winter-offset"),
            pytest.param("2026-01-02T01:30:00", date(2026, 1, 1), id="naive-is-utc"),
            pytest.param("2026-01-02T23:30:00-03:00", date(2026, 1, 2), id="local-offset"),
            pytest.param("not a date", None, id="garbage"),
            pytest.param(None, None, id="null"),
        ],
    )
    def test_local_date(self, raw, expected):
        """The day the wallet changed in Chile, not in UTC."""
        assert local_date(raw) == expected

    def test_the_query_asks_for_every_list_the_parser_reads(self):
        assert WALLET_MOVEMENTS_QUERY.startswith(
            "query WalletMovements($limit: Int!, $offset: Int!) {"
        )
        names = [name for name, _, _ in fintual_stocks._LISTS]
        names += [f"{container}(" for container, _, _ in fintual_stocks._NESTED_LISTS]
        names += [fintual_stocks._DIVESTMENTS]
        for name in names:
            assert name in WALLET_MOVEMENTS_QUERY


class TestCoreMovements:
    """Deposits, buys, sells, dividends and cash interest."""

    def test_a_deposit_is_an_internal_credit_on_the_wallet(self):
        (txn,) = _transactions(stocksDomesticBankDeposits=[_deposit()])

        assert txn.external_id == "fintual_wallet_dep_2001"
        assert (txn.product_kind, txn.currency, txn.product_external_ref) == (
            "wallet",
            "USD",
            None,
        )
        assert txn.amount == Decimal("1234.56")
        assert txn.description == "Depósito desde banco"
        assert txn.internal_transfer is True
        assert txn.transaction_date == date(2026, 1, 2)

    def test_a_buy_moves_money_from_the_wallet_to_the_stock(self):
        txns = _by_id(_transactions(stocksWalletAssetSubscriptionInstructions=[_order()]))

        wallet = txns["fintual_wallet_buy_5001"]
        stock = txns["fintual_stock_buy_5001"]
        assert (wallet.product_kind, wallet.amount) == ("wallet", Decimal("-999.99"))
        assert (stock.product_kind, stock.amount) == ("investment", Decimal("999.99"))
        assert stock.product_external_ref == "stock:1001"
        assert wallet.description == stock.description == "Compra ACME"
        assert wallet.internal_transfer and stock.internal_transfer
        assert wallet.transaction_date == stock.transaction_date == date(2026, 1, 5)

    def test_a_sell_moves_money_from_the_stock_to_the_wallet(self):
        glbx = _asset("1002", "GLBX", "Globex Corporation")
        order = _order("5002", asset=glbx, capital=100.0)

        txns = _by_id(_transactions(stocksWalletAssetRedemptionInstructions=[order]))

        assert txns["fintual_wallet_sell_5002"].amount == Decimal("100.00")
        stock = txns["fintual_stock_sell_5002"]
        assert (stock.amount, stock.product_external_ref) == (Decimal("-100.00"), "stock:1002")
        assert stock.description == "Venta GLBX"
        assert stock.internal_transfer is True

    def test_a_dividend_is_income_on_the_stock_and_a_transfer_on_the_wallet(self):
        """Counted once as income: where it was earned."""
        txns = _by_id(_transactions(stocksWalletDividends=[_dividend()]))

        wallet = txns["fintual_wallet_div_6001"]
        stock = txns["fintual_stock_div_6001"]
        assert wallet.amount == stock.amount == Decimal("12.34")
        assert (wallet.internal_transfer, stock.internal_transfer) == (True, False)
        assert stock.product_external_ref == "stock:1001"
        assert stock.description == "Dividendo ACME"
        assert stock.transaction_date == date(2026, 1, 10)

    def test_cash_interest_is_income_on_the_wallet(self):
        (txn,) = _transactions(stocksWalletCashInterests=[_dated("6101", 12.34)])

        assert txn.external_id == "fintual_wallet_int_6101"
        assert (txn.amount, txn.internal_transfer) == (Decimal("12.34"), False)
        assert txn.description == "Intereses saldo USD"

    def test_the_order_id_is_the_identity_not_the_instruction_id(self):
        """The per-asset views share the order id: one purchase, one id."""
        txns = _transactions(stocksWalletAssetSubscriptionInstructions=[_order("5009")])

        assert {t.external_id for t in txns} == {
            "fintual_wallet_buy_5009",
            "fintual_stock_buy_5009",
        }

    def test_every_row_carries_the_dashboard_fields(self):
        txns = _transactions(
            stocksDomesticBankDeposits=[_deposit()],
            stocksWalletAssetSubscriptionInstructions=[_order()],
            stocksWalletDividends=[_dividend(day="2026-02-27")],
        )

        assert all(t.institution == "fintual" and t.currency == "USD" for t in txns)
        assert all(t.scheduled_month == t.transaction_date.replace(day=1) for t in txns)
        assert all(t.accounting_date is None and t.category_hint is None for t in txns)
        assert len({t.external_id for t in txns}) == len(txns)


class TestOtherMovements:
    """One sign test per remaining movement type."""

    @pytest.mark.parametrize(
        ("lists", "wallet_id", "amount", "description", "internal"),
        [
            pytest.param(
                {"stocksDomesticBankWithdrawals": [_withdrawal()]},
                "fintual_wallet_wdr_2101",
                "-100.00",
                "Retiro a banco",
                True,
                id="withdrawal",
            ),
            pytest.param(
                {"stocksWalletCompensations": [_dated("6201", _money(12.34), key="capital")]},
                "fintual_wallet_comp_6201",
                "12.34",
                "Compensación Fintual",
                False,
                id="compensation",
            ),
            pytest.param(
                {
                    "stocksWalletReturnOfCapitals": {
                        "id": "7000",
                        "returnOfCapitals": [_dated("6301", _money(12.34))],
                    }
                },
                "fintual_wallet_roc_6301",
                "12.34",
                "Devolución de capital",
                True,
                id="return-of-capital",
            ),
            pytest.param(
                {
                    "stocksWalletAcatCashActivities": {
                        "fees": [],
                        "cashInbounds": [_dated("6401", 999.99)],
                        "cashOutbounds": [],
                    }
                },
                "fintual_wallet_acatin_6401",
                "999.99",
                "Transferencia ACAT entrante",
                True,
                id="acat-inbound",
            ),
            pytest.param(
                {
                    "stocksWalletAcatCashActivities": {
                        "fees": [],
                        "cashInbounds": [],
                        "cashOutbounds": [_dated("6402", 999.99)],
                    }
                },
                "fintual_wallet_acatout_6402",
                "-999.99",
                "Transferencia ACAT saliente",
                True,
                id="acat-outbound",
            ),
            pytest.param(
                {
                    "stocksWalletAcatCashActivities": {
                        "fees": [_dated("6403", 12.34)],
                        "cashInbounds": [],
                        "cashOutbounds": [],
                    }
                },
                "fintual_wallet_acatfee_6403",
                "-12.34",
                "Comisión ACAT",
                False,
                id="acat-fee",
            ),
            pytest.param(
                {
                    "stocksWalletWarrantExerciseCashActivities": {
                        "id": "8000",
                        "fees": [],
                        "costs": [_dated("6501", 100.0)],
                    }
                },
                "fintual_wallet_wcost_6501",
                "-100.00",
                "Ejercicio de warrant",
                True,
                id="warrant-cost",
            ),
            pytest.param(
                {
                    "stocksWalletWarrantExerciseCashActivities": {
                        "id": "8000",
                        "fees": [_dated("6502", 12.34)],
                        "costs": [],
                    }
                },
                "fintual_wallet_wfee_6502",
                "-12.34",
                "Comisión ejercicio de warrant",
                False,
                id="warrant-fee",
            ),
        ],
    )
    def test_wallet_only_types(self, lists, wallet_id, amount, description, internal):
        (txn,) = _transactions(**lists)

        assert txn.external_id == wallet_id
        assert txn.product_kind == "wallet"
        assert txn.amount == Decimal(amount)
        assert txn.description == description
        assert txn.internal_transfer is internal

    def test_a_cash_merger_moves_money_from_the_stock_to_the_wallet(self):
        merger = {
            **_dated("6601", _money(999.99), key="capital"),
            "asset": _asset("1003", "INITK", "Initech Inc"),
        }

        txns = _by_id(_transactions(stocksWalletCashMergers=[merger]))

        wallet = txns["fintual_wallet_cmrg_6601"]
        stock = txns["fintual_stock_cmrg_6601"]
        assert (wallet.amount, stock.amount) == (Decimal("999.99"), Decimal("-999.99"))
        assert stock.product_external_ref == "stock:1003"
        assert stock.description == "Fusión en efectivo INITK"
        assert wallet.internal_transfer and stock.internal_transfer

    def test_nested_containers_may_come_as_a_list(self):
        page = _page(
            stocksWalletReturnOfCapitals=[
                {"id": "7001", "returnOfCapitals": [_dated("6311", _money(12.34))]},
                {"id": "7002", "returnOfCapitals": [_dated("6312", _money(100.0))]},
            ]
        )

        entries = _parse(page).entries

        assert [e.source_id for e in entries] == ["6311", "6312"]

    def test_a_negative_raw_amount_flips_with_the_sign(self):
        refund = _dated("6202", _money(-12.34), key="capital")

        (txn,) = _transactions(stocksWalletCompensations=[refund])

        assert txn.amount == Decimal("-12.34")


class TestSkippedRows:
    @pytest.mark.parametrize(
        "lists",
        [
            pytest.param(
                {"stocksDomesticBankDeposits": [_deposit(executed_at=None)]}, id="no-forex"
            ),
            pytest.param(
                {
                    "stocksDomesticBankDeposits": [
                        {**_deposit(), "forexOrder": {"id": "1", "executedAt": None}}
                    ]
                },
                id="forex-not-executed",
            ),
            pytest.param(
                {"stocksWalletAssetSubscriptionInstructions": [_order(fulfilled_at=None)]},
                id="buy-not-filled",
            ),
            pytest.param(
                {
                    "stocksWalletAssetRedemptionInstructions": [
                        {**_order(), "assetOrder": None}
                    ]
                },
                id="sell-without-order",
            ),
        ],
    )
    def test_pending_rows_are_left_out_quietly(self, lists):
        """They return once executed; only the count is kept."""
        page = _parse(_page(**lists))

        assert page.entries == []
        assert (page.pending, page.cancelled) == (1, 0)
        assert not page.skipped

    @pytest.mark.parametrize(
        ("age", "pending", "cancelled"),
        [
            pytest.param(timedelta(days=7), 1, 0, id="a-week-old"),
            pytest.param(timedelta(days=7, seconds=1), 0, 1, id="older"),
        ],
    )
    def test_a_movement_never_executed_is_pending_for_a_week(self, age, pending, cancelled):
        """The query has no state field: past the window it was cancelled,
        and it no longer counts as pending."""
        declared = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)  # _order's declaredAt
        order = _order(fulfilled_at=None)

        page = _parse(_page(stocksWalletAssetSubscriptionInstructions=[order]), now=declared + age)

        assert page.entries == []
        assert (page.pending, page.cancelled) == (pending, cancelled)
        assert not page.skipped

    @pytest.mark.parametrize(
        ("dates", "pending", "cancelled"),
        [
            pytest.param(
                {"declaredAt": None, "createdAt": "2025-12-01T12:00:00Z"}, 0, 1, id="created-at"
            ),
            pytest.param({"declaredAt": None}, 1, 0, id="undated"),
            pytest.param({"declaredAt": "someday"}, 1, 0, id="unreadable"),
        ],
    )
    def test_the_age_falls_back_to_the_creation_date(self, dates, pending, cancelled):
        """Without any readable date the row stays pending: that only skips
        the balance check."""
        deposit = {**_deposit(executed_at=None), **dates}

        page = _parse(_page(stocksDomesticBankDeposits=[deposit]))

        assert (page.pending, page.cancelled) == (pending, cancelled)

    @pytest.mark.parametrize(
        ("lists", "reason"),
        [
            pytest.param(
                {"stocksDomesticBankDeposits": [_deposit(currency="CLP")]},
                "not_usd",
                id="deposit-in-clp",
            ),
            pytest.param(
                {"stocksWalletDividends": [{**_dividend(), "netCapital": _money(12.34, "EUR")}]},
                "not_usd",
                id="dividend-in-eur",
            ),
            pytest.param(
                {"stocksWalletCashInterests": [_dated(None, 12.34)]},
                "unreadable",
                id="no-id",
            ),
            pytest.param(
                {"stocksWalletCashInterests": [_dated("6101", 12.34, day="someday")]},
                "unreadable",
                id="bad-date",
            ),
            pytest.param(
                {"stocksWalletCashInterests": [_dated("6101", None)]},
                "unreadable",
                id="no-amount",
            ),
            pytest.param(
                {"stocksWalletCashInterests": ["not an object"]},
                "unreadable",
                id="not-an-object",
            ),
            pytest.param(
                {"stocksWalletAssetSubscriptionInstructions": [_order(capital=None)]},
                "unreadable",
                id="filled-without-capital",
            ),
        ],
    )
    def test_unusable_rows_are_skipped_and_counted(self, lists, reason):
        page = _parse(_page(**lists))

        assert page.entries == []
        assert page.skipped == {reason: 1}

    def test_a_movement_rounding_to_zero_is_dropped(self):
        page = _parse(_page(stocksWalletCashInterests=[_dated("6101", 0.004)]))

        assert page.entries == []
        assert not page.skipped and not page.pending

    def test_a_stock_without_an_id_keeps_only_its_wallet_row(self):
        order = _order(asset={"id": None, "symbol": None, "name": "Acme Corp"})

        page = _parse(_page(stocksWalletAssetSubscriptionInstructions=[order]))
        txns = ledger_to_transactions(page.entries)

        assert page.without_asset == 1
        assert [t.external_id for t in txns] == ["fintual_wallet_buy_5001"]
        assert txns[0].description == "Compra Acme Corp"

    @pytest.mark.parametrize(
        ("asset", "description"),
        [
            pytest.param(_asset(), "Compra ACME", id="ticker"),
            pytest.param({"id": "1001", "name": "Acme Corp"}, "Compra Acme Corp", id="name"),
            pytest.param({"id": "1001"}, "Compra acciones", id="neither"),
        ],
    )
    def test_the_description_names_the_stock_as_well_as_it_can(self, asset, description):
        txns = _transactions(stocksWalletAssetSubscriptionInstructions=[_order(asset=asset)])

        assert {t.description for t in txns} == {description}

    def test_divestments_are_counted_not_imported(self):
        divestment = {
            "divestmentOperationUuid": "00000000-0000-0000-0000-000000000001",
            "requestedAmount": 100.0,
            "requestedCurrency": "USD",
        }

        page = _parse(_page(stocksDivestments=[divestment]))

        assert (page.entries, page.divestments, page.length) == ([], 1, 1)


class TestPageShape:
    def test_the_length_is_the_longest_list_nested_ones_included(self):
        page = _parse(
            _page(
                stocksDomesticBankDeposits=[_deposit("2001"), _deposit("2002")],
                stocksWalletAcatCashActivities={
                    "fees": [_dated(f"64{n:02}", 12.34) for n in range(3)],
                    "cashInbounds": [],
                    "cashOutbounds": [],
                },
            )
        )

        assert page.length == 3

    def test_each_container_of_a_list_counts_toward_the_length(self):
        holders = [{"id": f"70{n:02}", "returnOfCapitals": []} for n in range(3)]

        assert _parse(_page(stocksWalletReturnOfCapitals=holders)).length == 3

    def test_an_empty_page_has_no_length(self):
        assert _parse(_page()).length == 0

    def test_null_lists_are_empty(self):
        page = _parse({key: None for key in _page()})

        assert (page.length, page.entries) == (0, [])

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(None, id="no-data"),
            pytest.param(_page(stocksWalletDividends={"id": "1"}), id="object-for-a-list"),
        ],
    )
    def test_an_unexpected_shape_raises(self, data):
        with pytest.raises(ValueError):
            _parse(data)


class TestLedgerBalance:
    def test_the_ledger_adds_up_to_the_wallet(self):
        """deposits + dividends + interest + sells - buys, to the cent."""
        page = _parse(
            _page(
                stocksDomesticBankDeposits=[_deposit()],
                stocksWalletAssetSubscriptionInstructions=[_order()],
                stocksWalletAssetRedemptionInstructions=[_order("5002", capital=100.0)],
                stocksWalletDividends=[_dividend()],
                stocksWalletCashInterests=[_dated("6101", 12.34)],
            )
        )

        assert ledger_balance(page.entries) == Decimal("359.25")

    def test_an_empty_ledger_is_zero(self):
        assert ledger_balance([]) == Decimal("0.00")


class TestSoldOutProducts:
    def _entries(self, **lists):
        return _parse(_page(**lists)).entries

    def test_a_stock_off_the_list_is_valued_at_zero_from_its_ledger(self):
        """A cash merger pays the shares out in cash: it counts as a sale."""
        initk = _asset("1003", "INITK", "Initech Inc")
        entries = self._entries(
            stocksWalletAssetSubscriptionInstructions=[_order("5001", asset=initk, capital=999.99)],
            stocksWalletCashMergers=[
                {**_dated("6601", _money(1234.56), key="capital"), "asset": initk}
            ],
            stocksWalletDividends=[_dividend(asset={"id": "1003", "symbol": "INITK"})],
        )

        (product,) = sold_out_products(entries, frozenset())

        assert (product.kind, product.currency, product.external_ref) == (
            "investment",
            "USD",
            "stock:1003",
        )
        assert product.name == "Initech Inc"
        assert product.attributes.ticker == "INITK"
        assert (product.metrics.nav, product.metrics.units) == (0.0, 0.0)
        assert (product.metrics.deposited, product.metrics.profit) == (-234.57, 234.57)
        assert product.metrics.dividends == 12.34

    def test_a_sale_values_the_stock_at_zero(self):
        entries = self._entries(
            stocksWalletAssetSubscriptionInstructions=[_order("5001", capital=999.99)],
            stocksWalletAssetRedemptionInstructions=[_order("5002", capital=1234.56)],
        )

        (product,) = sold_out_products(entries, frozenset())

        assert (product.external_ref, product.metrics.nav) == ("stock:1001", 0.0)
        assert (product.metrics.deposited, product.metrics.profit) == (-234.57, 234.57)

    def test_a_listed_stock_is_left_alone(self):
        entries = self._entries(
            stocksWalletAssetSubscriptionInstructions=[_order("5001")],
            stocksWalletAssetRedemptionInstructions=[_order("5002", capital=100.0)],
        )

        assert sold_out_products(entries, frozenset({"1001"})) == []

    @pytest.mark.parametrize(
        "lists",
        [
            pytest.param(
                {"stocksWalletAssetSubscriptionInstructions": [_order()]},
                id="bought-then-merged-spun-off-or-moved-out",
            ),
            pytest.param(
                {"stocksWalletDividends": [_dividend(asset={"id": "1002", "symbol": "GLBX"})]},
                id="moved-in-paid-a-dividend-then-left",
            ),
        ],
    )
    def test_a_stock_that_left_without_a_sale_is_left_alone(self, lists):
        """No cash leg: zeroing it would book its cost as a loss."""
        assert sold_out_products(self._entries(**lists), frozenset()) == []

    def test_wallet_only_movements_make_no_product(self):
        entries = self._entries(
            stocksDomesticBankDeposits=[_deposit()],
            stocksWalletAssetSubscriptionInstructions=[_order(asset={"id": None})],
        )

        assert sold_out_products(entries, frozenset()) == []

    def test_a_stock_known_only_by_its_ticker_is_named_by_it(self):
        glbx = {"id": "1002", "symbol": "GLBX"}
        entries = self._entries(
            stocksWalletAssetRedemptionInstructions=[_order("5002", asset=glbx, capital=100.0)]
        )

        (product,) = sold_out_products(entries, frozenset())

        assert product.name == "GLBX"
        assert (product.metrics.deposited, product.metrics.profit) == (-100.0, 100.0)


class TestShowsUnsoldStocks:
    @pytest.mark.parametrize(
        ("lists", "unsold"),
        [
            pytest.param(
                {"stocksWalletAssetSubscriptionInstructions": [_order()]}, True, id="bought"
            ),
            pytest.param(
                {
                    "stocksWalletAssetSubscriptionInstructions": [_order("5001")],
                    "stocksWalletAssetRedemptionInstructions": [_order("5002", capital=100.0)],
                },
                False,
                id="bought-and-sold",
            ),
            pytest.param(
                {
                    "stocksWalletAssetSubscriptionInstructions": [_order("5001")],
                    "stocksWalletCashMergers": [
                        {**_dated("6601", _money(100.0), key="capital"), "asset": _asset()}
                    ],
                },
                False,
                id="bought-and-merged-for-cash",
            ),
            pytest.param({"stocksWalletDividends": [_dividend()]}, False, id="dividend-only"),
            pytest.param(
                {"stocksDomesticBankDeposits": [_deposit()]}, False, id="wallet-only"
            ),
        ],
    )
    def test_a_stock_bought_and_never_sold(self, lists, unsold):
        assert shows_unsold_stocks(_parse(_page(**lists)).entries) is unsold
