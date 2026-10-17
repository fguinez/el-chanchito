"""Tests for the Skipo scraper.

These never hit the network: `httpx.AsyncClient` is swapped for a real client
over an `httpx.MockTransport` serving canned Skipo payloads written from
scratch. Every figure, symbol balance and id is synthetic.
"""

import logging
from datetime import date
from decimal import Decimal

import pytest

from scrapers.institutions.skipo import (
    _asset_classes,
    _exchange_rates,
    _local_date,
    _parse_balances,
    _parse_ledger_entry,
    _to_clp,
)

_CLASSES = {
    "CLP": "FIAT",
    "USD": "FIAT",
    "BTC": "CRYPTO",
    "ETH": "CRYPTO",
    "USDC": "STABLECOIN",
    "NVDAX": "STOCK",
}


def _asset(symbol: str, asset_class: str) -> dict:
    """A synthetic catalog asset."""
    return {"assetSymbol": symbol, "assetName": f"Synthetic {symbol}", "assetClass": asset_class}


def _balance(
    symbol: str,
    balance="0",
    frozen="0",
    pending="0",
    pref="0",
    **overrides,
) -> dict:
    """A synthetic `/v2/balances` row; amounts are decimal strings, as Skipo sends them."""
    row = {
        "assetSymbol": symbol,
        "balance": balance,
        "balanceFrozen": frozen,
        "balancePending": pending,
        "balancePrefCurrency": pref,
        "balanceUSD": "0",
        "multiplier": "1",
        "assetFormat": "rebased",
        "base": {
            "asset": symbol,
            "balance": balance,
            "balanceFrozen": frozen,
            "balancePending": pending,
        },
    }
    row.update(overrides)
    return row


def _entry(n: int, entry_type="DEPOSIT", amount="1000000", **overrides) -> dict:
    """A synthetic CLP ledger entry with a consecutive id."""
    row = {
        "id": f"ldg_synthetic_{n:04d}",
        "source": {"type": "deposit", "id": f"src_synthetic_{n:04d}"},
        "type": entry_type,
        "subType": None,
        "assetSymbol": "CLP",
        "detail": "Movimiento sintetico",
        "amount": amount,
        "fee": "0",
        "total": amount,
        "balance": "2500000",
        "createdAt": "2026-01-15T12:00:00Z",
    }
    row.update(overrides)
    return row


class TestToClp:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("2500000", 2500000, id="decimal-string"),
            pytest.param("999999.00000000", 999999, id="trailing-zeros"),
            pytest.param("1000000.5", 1000001, id="half-rounds-up"),
            pytest.param("1000000.49", 1000000, id="below-half-rounds-down"),
            pytest.param(1000000, 1000000, id="json-number"),
            pytest.param("$ 1.000.000", None, id="display-format-rejected"),
            pytest.param(None, None, id="none"),
            pytest.param(True, None, id="bool"),
            pytest.param("NaN", None, id="not-finite"),
        ],
    )
    def test_parses_decimal_strings(self, raw, expected):
        """Decimal strings and numbers become whole CLP, half up."""
        assert _to_clp(raw) == expected


class TestLocalDate:
    def test_converts_utc_evening_to_santiago(self):
        """A UTC timestamp past midnight is still the previous day in Santiago."""
        assert _local_date("2026-01-16T02:30:00Z") == date(2026, 1, 15)

    def test_keeps_an_explicit_offset(self):
        """A timestamp with an offset is converted, not read as UTC."""
        assert _local_date("2026-01-15T23:30:00-03:00") == date(2026, 1, 15)

    def test_unreadable_is_none(self):
        """Garbage yields None rather than today's date."""
        assert _local_date("not a date") is None


class TestAssetClasses:
    def test_maps_symbols_upper_case(self):
        """The catalog becomes a symbol to class map, case-insensitive on input."""
        classes = _asset_classes([_asset("btc", "crypto"), {"assetSymbol": "X"}])

        assert classes == {"BTC": "CRYPTO"}


class TestParseBalances:
    def test_clp_is_the_singleton_wallet(self):
        """The CLP row becomes one CLP wallet with no external_ref, in whole pesos."""
        products, warnings = _parse_balances([_balance("CLP", balance="2500000.5")], _CLASSES)

        assert [(p.kind, p.currency, p.external_ref) for p in products] == [("wallet", "CLP", None)]
        assert products[0].metrics.balance == 2500001
        assert warnings == []

    def test_empty_clp_wallet_is_still_reported(self):
        """A zero CLP balance is an observation, so a drained wallet stops showing its old balance."""
        products, _ = _parse_balances([_balance("CLP", balance="0")], _CLASSES)

        assert [(p.kind, p.metrics.balance) for p in products] == [("wallet", 0)]

    @pytest.mark.parametrize(
        ("symbol",),
        [pytest.param("BTC", id="crypto"), pytest.param("USDC", id="stablecoin")],
    )
    def test_coins_become_crypto_products(self, symbol):
        """Crypto and stablecoins become one crypto product per symbol, in units."""
        products, warnings = _parse_balances(
            [_balance(symbol, balance="0.12345678", frozen="0.5", pending="0")], _CLASSES
        )

        assert [(p.kind, p.currency, p.external_ref) for p in products] == [("crypto", symbol, None)]
        assert products[0].metrics.units == pytest.approx(0.12345678)
        assert products[0].metrics.frozen == pytest.approx(0.5)
        assert products[0].metrics.pending is None
        assert warnings == []

    def test_frozen_only_coin_is_held(self):
        """A coin with nothing available but units frozen in an order is still a holding."""
        products, _ = _parse_balances([_balance("ETH", frozen="0.12345678")], _CLASSES)

        assert [(p.currency, p.metrics.units) for p in products] == [("ETH", 0.0)]

    def test_zero_coin_balances_are_dropped_silently(self):
        """Coins held nowhere yield no product and no warning."""
        products, warnings = _parse_balances([_balance("BTC"), _balance("NVDAX")], _CLASSES)

        assert products == []
        assert warnings == []

    @pytest.mark.parametrize(
        ("row", "fragment"),
        [
            pytest.param(_balance("USD", balance="1234.56"), "non-CLP fiat", id="other-fiat"),
            pytest.param(_balance("NVDAX", balance="0.12345678"), "tokenized stock", id="stock"),
            pytest.param(_balance("ZZZ", balance="0.12345678"), "unknown asset class", id="unknown-class"),
            pytest.param(_balance("BTC", balance="lots"), "unreadable", id="unreadable-balance"),
            pytest.param(_balance("", balance="1"), "unreadable", id="no-symbol"),
        ],
    )
    def test_skipped_holdings_become_warnings(self, row, fragment, caplog):
        """Holdings that can't be modelled are left out, counted in a warning without amounts."""
        with caplog.at_level(logging.WARNING):
            products, warnings = _parse_balances([row], _CLASSES)

        assert products == []
        assert len(warnings) == 1 and fragment in warnings[0]
        assert "1234" not in warnings[0] and "0.123" not in warnings[0]

    def test_stock_warning_counts_holdings(self):
        """Several stocks yield one warning naming the count."""
        classes = {**_CLASSES, "AAPLX": "STOCK"}
        rows = [_balance("NVDAX", balance="1"), _balance("AAPLX", balance="2")]

        _, warnings = _parse_balances(rows, classes)

        assert warnings == ["Skipo: 2 tokenized stock holding(s) not imported"]


class TestExchangeRates:
    def test_rate_is_clp_value_over_units(self):
        """A held coin's rate is its CLP value divided by its units, institution-wide."""
        rates = _exchange_rates([_balance("BTC", balance="0.5", pref="1000000")], _CLASSES)

        assert len(rates) == 1
        rate = rates[0]
        assert (rate.institution, rate.base_currency, rate.quote_currency) == ("skipo", "BTC", "CLP")
        assert rate.rate == Decimal("2000000")
        assert rate.product is None
        assert rate.source

    def test_rate_rounds_half_up_to_eight_decimals(self):
        """The quotient is rounded half up to what `exchange_rates.rate` holds."""
        rates = _exchange_rates([_balance("USDC", balance="3", pref="2")], _CLASSES)

        assert rates[0].rate == Decimal("0.66666667")

    @pytest.mark.parametrize(
        "row",
        [
            pytest.param(_balance("CLP", balance="2500000", pref="2500000"), id="clp"),
            pytest.param(_balance("NVDAX", balance="1", pref="999999"), id="stock"),
            pytest.param(_balance("USD", balance="1", pref="999999"), id="other-fiat"),
            pytest.param(_balance("BTC", balance="0", pref="999999"), id="zero-units"),
            pytest.param(_balance("BTC", balance="0.5", pref="0"), id="zero-value"),
            pytest.param(_balance("BTC", balance="0.5", pref="n/a"), id="unreadable-value"),
            pytest.param(_balance("BTC", balance="0.5", frozen="0.1", pref="999999"), id="frozen-units"),
            pytest.param(_balance("BTC", balance="0.5", pending="0.1", pref="999999"), id="pending-units"),
            pytest.param(_balance("BTC", balance="0.00000001", pref="999999999999"), id="too-large"),
        ],
    )
    def test_no_rate_when_it_would_be_wrong_or_meaningless(self, row):
        """No rate for CLP, non-coins, empty or ambiguous holdings, or values that don't fit."""
        assert _exchange_rates([row], _CLASSES) == []

    def test_symbol_outside_the_currency_pattern_is_skipped(self):
        """A coin whose symbol can't be a rate currency is skipped, not raised."""
        classes = {**_CLASSES, "1INCH": "CRYPTO"}

        assert _exchange_rates([_balance("1INCH", balance="1", pref="999999")], classes) == []


class TestParseLedgerEntry:
    @pytest.mark.parametrize(
        ("entry_type", "sign", "description"),
        [
            pytest.param("DEPOSIT", 1, "Skipo depósito", id="deposit-in"),
            pytest.param("BUY", 1, "Skipo venta cripto", id="buy-in"),
            pytest.param("WITHDRAWAL", -1, "Skipo retiro", id="withdrawal-out"),
            pytest.param("SELL", -1, "Skipo compra cripto", id="sell-out"),
        ],
    )
    def test_type_sets_sign_and_description(self, entry_type, sign, description):
        """The sign comes from the type, since amounts are positive magnitudes."""
        txn = _parse_ledger_entry(_entry(1, entry_type))

        assert txn is not None
        assert txn.amount == sign * 1000000
        assert txn.description == description

    def test_earn_distribution_reads_as_yield(self):
        """An earn distribution is described as yield whatever its type."""
        txn = _parse_ledger_entry(_entry(1, subType="INTERNAL_EARN_DISTRIBUTION"))

        assert txn is not None
        assert txn.description == "Skipo rendimiento"
        assert txn.amount == 1000000

    def test_envelope_fields(self):
        """A CLP wallet transaction with a prefixed id, Santiago date and scheduled month."""
        txn = _parse_ledger_entry(_entry(7, createdAt="2026-02-01T01:00:00Z"))

        assert txn is not None
        assert (txn.institution, txn.product_kind, txn.currency) == ("skipo", "wallet", "CLP")
        assert txn.external_id == "skipo_ldg_synthetic_0007"
        assert txn.transaction_date == date(2026, 1, 31)
        assert txn.scheduled_month == date(2026, 1, 1)

    def test_total_wins_over_amount(self):
        """When both are readable, `total` (thought to include the fee) is used."""
        txn = _parse_ledger_entry(_entry(1, "SELL", amount="999999", fee="1", total="1000000"))

        assert txn is not None
        assert txn.amount == -1000000

    def test_unreadable_total_falls_back_to_amount(self):
        """Without a readable `total`, `amount` is used."""
        txn = _parse_ledger_entry(_entry(1, amount="999999", total=None))

        assert txn is not None
        assert txn.amount == 999999

    def test_description_does_not_change_the_id(self):
        """A rewritten detail or subtype keeps the same external_id."""
        first = _parse_ledger_entry(_entry(1))
        rewritten = _parse_ledger_entry(_entry(1, detail="Otro texto", subType="EXTERNAL_FIAT_BANK"))

        assert first is not None and rewritten is not None
        assert first.external_id == rewritten.external_id

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"id": None}, id="no-id"),
            pytest.param({"type": "TRANSFER"}, id="unknown-type"),
            pytest.param({"type": None}, id="no-type"),
            pytest.param({"amount": "0", "total": "0"}, id="zero-amount"),
            pytest.param({"amount": "abc", "total": None}, id="unreadable-amount"),
            pytest.param({"createdAt": ""}, id="no-date"),
            pytest.param({"assetSymbol": "BTC"}, id="not-clp"),
        ],
    )
    def test_skips_unusable_rows_with_a_warning(self, overrides, caplog):
        """Unusable entries are skipped and logged, never raised."""
        with caplog.at_level(logging.WARNING):
            txn = _parse_ledger_entry(_entry(1, **overrides))

        assert txn is None
        assert "Skipo ledger entry" in caplog.text
