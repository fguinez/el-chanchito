"""Tests for the Skipo scraper.

These never hit the network: `httpx.AsyncClient` is swapped for a real client
over an `httpx.MockTransport` serving canned Skipo payloads written from
scratch. Every figure, symbol balance and id is synthetic.
"""

import asyncio
import logging
import os
from datetime import date
from decimal import Decimal

import httpx
import pytest

os.environ.setdefault("SKIPO_API_KEY", "skp_live_synthetic-key")

from scrapers import retry
from scrapers.institutions import skipo as skipo_mod
from scrapers.institutions.skipo import (
    SkipoApiError,
    SkipoAuthError,
    SkipoScraper,
    _asset_classes,
    _local_date,
    _parse_balances,
    _parse_ledger_entry,
    _price_rate,
    _rate,
    _to_clp,
)

KEY = "skp_live_synthetic-key"

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


def _price(market: str, rate: str, side: str = "SELL") -> dict:
    """A synthetic `/v2/markets/{market}/price` answer for one base unit."""
    base = market.split("-")[0]
    return {
        "market": market,
        "side": side,
        "rate": rate,
        "baseAmount": "1",
        "quoteAmount": rate,
        "indicative": True,
        "pricedAt": "2026-01-16T02:30:00Z",
        "assetFormat": "rebased",
        "multiplier": "1",
        "base": {"asset": base, "amount": "1"},
    }


_DEFAULT_BALANCES = [
    _balance("CLP", balance="2500000"),
    _balance("BTC", balance="0.12345678", pref="999999"),
    _balance("ETH"),
]


class _Skipo:
    """Canned Skipo API behind an httpx.MockTransport.

    `ledger` maps a page number to its entries; `fail` lists ledger pages
    answering 500; `prices` maps a market ("BTC-CLP") to its SELL rate, or
    to an int status to answer with; a market not in it answers 404.
    `status` forces every answer (as problem+json with `code`). Like Skipo,
    it answers 401 unless the bearer key matches, and 400 for a price
    without `side`.
    """

    def __init__(
        self,
        balances=None,
        assets=None,
        ledger=None,
        prices=None,
        fail=(),
        status=None,
        code="unauthorized",
    ):
        self.balances = _DEFAULT_BALANCES if balances is None else balances
        self.assets = assets if assets is not None else [_asset(s, c) for s, c in _CLASSES.items()]
        self.ledger = ledger or {}
        self.prices = {"BTC-CLP": "65000000.5"} if prices is None else prices
        self.fail = set(fail)
        self.status = status
        self.code = code
        self.requests: list[httpx.Request] = []

    @staticmethod
    def _problem(status: int, code: str) -> httpx.Response:
        return httpx.Response(
            status,
            json={"type": "about:blank", "title": "Synthetic problem", "status": status, "code": code},
            headers={"Content-Type": "application/problem+json"},
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            return self._problem(self.status, self.code)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return self._problem(401, "unauthorized")
        path = request.url.path
        if path.startswith("/v2/markets/") and path.endswith("/price"):
            if "side" not in request.url.params:
                return self._problem(400, "validation_error")
            market = path.split("/")[3]
            price = self.prices.get(market, 404)
            if isinstance(price, int):
                return self._problem(price, "not_found" if price == 404 else "internal_error")
            return httpx.Response(200, json=_price(market, price, request.url.params["side"]))
        if path == "/v2/balances":
            return httpx.Response(200, json=self.balances)
        if path == "/v2/assets":
            return httpx.Response(
                200,
                json={
                    "data": self.assets,
                    "pagination": {"page": 1, "limit": 100, "totalItems": len(self.assets), "totalPages": 1},
                },
            )
        if path == "/v2/ledger":
            if "assetSymbol" not in request.url.params:
                return self._problem(400, "validation_error")
            page = int(request.url.params["page"])
            if page in self.fail:
                return self._problem(500, "internal_error")
            entries = self.ledger.get(page, [])
            return httpx.Response(
                200,
                json={
                    "startDate": "2026-01-01T00:00:00Z",
                    "endDate": "2026-01-31T23:59:59Z",
                    "data": entries,
                    "pagination": {
                        "page": page,
                        "limit": 100,
                        "totalItems": sum(len(v) for v in self.ledger.values()),
                        "totalPages": max(self.ledger, default=1),
                    },
                },
            )
        return self._problem(404, "not_found")


def _install(monkeypatch, fake: _Skipo) -> None:
    """Route the scraper's httpx.AsyncClient through the fake, with no retry delays."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(retry, "BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(
        skipo_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fake.handler), **kwargs),
    )


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
            pytest.param("1" + "0" * 40, None, id="more-digits-than-the-context"),
            pytest.param("1e100", None, id="huge-exponent"),
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

    def test_naive_timestamp_is_read_as_utc(self):
        """A timestamp without an offset is UTC, then converted to Santiago."""
        assert _local_date("2026-01-16T02:30:00") == date(2026, 1, 15)

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

    def test_sold_out_coin_reports_zero_units(self):
        """A coin at zero is still reported, so a sold-out holding stops counting."""
        products, warnings = _parse_balances([_balance("BTC")], _CLASSES)

        assert [(p.kind, p.currency, p.metrics.units) for p in products] == [("crypto", "BTC", 0.0)]
        assert (products[0].metrics.frozen, products[0].metrics.pending) == (None, None)
        assert warnings == []

    @pytest.mark.parametrize(
        "symbol",
        [
            pytest.param("NVDAX", id="stock"),
            pytest.param("USD", id="other-fiat"),
            pytest.param("ZZZ", id="unknown-class"),
        ],
    )
    def test_zero_non_coin_balances_are_dropped_silently(self, symbol):
        """Holdings that aren't imported anyway yield nothing at zero, not even a warning."""
        products, warnings = _parse_balances([_balance(symbol)], _CLASSES)

        assert products == []
        assert warnings == []

    @pytest.mark.parametrize(
        ("row", "fragment"),
        [
            pytest.param(_balance("USD", balance="1234.56"), "non-CLP fiat", id="other-fiat"),
            pytest.param(_balance("NVDAX", balance="0.12345678"), "tokenized stock", id="stock"),
            pytest.param(_balance("ZZZ", balance="0.12345678"), "unknown asset class", id="unknown-class"),
            pytest.param(_balance("BTC", balance="lots"), "unreadable", id="unreadable-balance"),
            pytest.param(_balance("CLP", balance="1e100"), "unreadable", id="clp-past-whole-pesos"),
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


class TestPriceRate:
    def test_sell_price_becomes_an_institution_rate(self):
        """The price's rate is CLP per unit, institution-wide, dated in Santiago."""
        rate = _price_rate("BTC", _price("BTC-CLP", "65000000.123456789"))

        assert rate is not None
        assert (rate.institution, rate.base_currency, rate.quote_currency) == ("skipo", "BTC", "CLP")
        assert rate.rate == Decimal("65000000.12345679")
        assert rate.product is None
        assert rate.reported_as_of == date(2026, 1, 15)
        assert "venta" in rate.source

    def test_unreadable_priced_at_leaves_no_date(self):
        """A missing pricedAt keeps the rate, undated."""
        rate = _price_rate("BTC", {**_price("BTC-CLP", "999999"), "pricedAt": None})

        assert rate is not None and rate.reported_as_of is None

    @pytest.mark.parametrize(
        ("symbol", "body"),
        [
            pytest.param("BTC", _price("ETH-CLP", "999999"), id="other-market"),
            pytest.param("BTC", _price("BTC-CLP", "0"), id="zero-rate"),
            pytest.param("BTC", _price("BTC-CLP", "abc"), id="unreadable-rate"),
            pytest.param("BTC", _price("BTC-CLP", "1" + "0" * 40), id="huge-rate"),
            pytest.param("BTC", ["not", "an", "object"], id="not-an-object"),
            pytest.param("1INCH", _price("1INCH-CLP", "999999"), id="symbol-outside-pattern"),
        ],
    )
    def test_unusable_answers_yield_no_rate(self, symbol, body):
        """No rate rather than a wrong one, and nothing raises."""
        assert _price_rate(symbol, body) is None


class TestRate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("65000000.123456789", Decimal("65000000.12345679"), id="rounds-half-up"),
            pytest.param("0.000000004", None, id="rounds-to-zero"),
            pytest.param("1000000000000", None, id="past-numeric-20-8"),
            pytest.param("1" + "0" * 40, None, id="more-digits-than-the-context"),
            pytest.param(Decimal(1) / Decimal(3) * Decimal("1e30"), None, id="huge-quotient"),
            pytest.param("0", None, id="zero"),
            pytest.param("-1", None, id="negative"),
            pytest.param("abc", None, id="unreadable"),
        ],
    )
    def test_bounds_before_rounding(self, raw, expected):
        """Only rates `exchange_rates.rate` can hold come back, and nothing raises."""
        assert _rate(raw) == expected


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
            pytest.param({"amount": "1" + "0" * 20, "total": None}, id="amount-past-the-column"),
            pytest.param({"amount": "1" + "0" * 40, "total": None}, id="amount-past-the-context"),
        ],
    )
    def test_skips_unusable_rows_with_a_warning(self, overrides, caplog):
        """Unusable entries are skipped and logged, never raised."""
        with caplog.at_level(logging.WARNING):
            txn = _parse_ledger_entry(_entry(1, **overrides))

        assert txn is None
        assert "Skipo ledger entry" in caplog.text


class TestScrapeProducts:
    def setup_method(self):
        self.scraper = SkipoScraper()

    def test_emits_wallet_coins_and_rates(self, monkeypatch):
        """Held balances become products and the held coin gets a CLP rate."""
        _install(monkeypatch, _Skipo())

        result = asyncio.run(self.scraper.scrape_products())

        assert sorted((p.kind, p.currency) for p in result.products) == [
            ("crypto", "BTC"),
            ("crypto", "ETH"),
            ("wallet", "CLP"),
        ]
        assert [(r.base_currency, r.rate) for r in result.exchange_rates] == [
            ("BTC", Decimal("65000000.5"))
        ]
        assert result.warnings == []

    def test_stock_holdings_make_the_run_partial(self, monkeypatch):
        """A held tokenized stock is skipped with a counted warning."""
        balances = [*_DEFAULT_BALANCES, _balance("NVDAX", balance="0.12345678", pref="999999")]
        _install(monkeypatch, _Skipo(balances=balances))

        result = asyncio.run(self.scraper.scrape_products())

        assert "NVDAX" not in {p.currency for p in result.products}
        assert result.warnings == ["Skipo: 1 tokenized stock holding(s) not imported"]

    def test_prices_are_asked_for_the_sell_side_of_held_coins(self, monkeypatch):
        """Only held coins are priced, against CLP, on the SELL side."""
        fake = _Skipo()
        _install(monkeypatch, fake)

        asyncio.run(self.scraper.scrape_products())

        priced = [
            (r.url.path, dict(r.url.params))
            for r in fake.requests
            if r.url.path.startswith("/v2/markets/")
        ]
        assert priced == [("/v2/markets/BTC-CLP/price", {"side": "SELL"})]

    @pytest.mark.parametrize(
        "price",
        [
            pytest.param(404, id="no-clp-market"),
            pytest.param(422, id="unprocessable"),
            pytest.param(500, id="server-error"),
            pytest.param("abc", id="unreadable-rate"),
        ],
    )
    def test_a_missing_price_is_a_warning_not_a_failure(self, monkeypatch, price):
        """A coin without a usable price keeps its product, gets no rate and is counted."""
        _install(monkeypatch, _Skipo(prices={"BTC-CLP": price}))

        result = asyncio.run(self.scraper.scrape_products())

        assert "BTC" in {p.currency for p in result.products}
        assert result.exchange_rates == []
        assert result.warnings == ["Skipo: no CLP price for 1 coin(s)"]

    def test_a_price_timeout_is_a_warning(self, monkeypatch):
        """A timeout on one price costs only that rate."""
        fake = _Skipo()
        handler = fake.handler

        def timeout_on_price(request):
            if request.url.path.startswith("/v2/markets/"):
                raise httpx.ReadTimeout("")
            return handler(request)

        fake.handler = timeout_on_price
        _install(monkeypatch, fake)

        result = asyncio.run(self.scraper.scrape_products())

        assert len(result.products) == 3
        assert result.warnings == ["Skipo: no CLP price for 1 coin(s)"]

    def test_a_rejected_key_on_a_price_still_raises(self, monkeypatch):
        """A 401 or 403 on a price is an auth failure, not a missing rate."""
        _install(monkeypatch, _Skipo(prices={"BTC-CLP": 403}))

        with pytest.raises(SkipoAuthError) as excinfo:
            asyncio.run(self.scraper.scrape_products())

        assert "403" in str(excinfo.value)

    def test_only_get_requests_are_sent(self, monkeypatch):
        """The scraper never writes to Skipo."""
        fake = _Skipo()
        _install(monkeypatch, fake)

        asyncio.run(self.scraper.scrape_products())

        assert {r.method for r in fake.requests} == {"GET"}
        assert {r.url.path for r in fake.requests} == {
            "/v2/assets",
            "/v2/balances",
            "/v2/markets/BTC-CLP/price",
        }

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            pytest.param(401, "key_expired", id="expired"),
            pytest.param(401, "key_revoked", id="revoked"),
            pytest.param(403, "insufficient_scope", id="scope"),
            pytest.param(403, "ip_not_allowed", id="ip"),
        ],
    )
    def test_rejected_key_raises_with_the_code_only(self, monkeypatch, status, code):
        """An auth failure names its status, code and fix, never the key or a URL."""
        _install(monkeypatch, _Skipo(status=status, code=code))

        with pytest.raises(SkipoAuthError) as excinfo:
            asyncio.run(self.scraper.scrape_products())

        message = str(excinfo.value)
        assert code in message and str(status) in message
        assert "make secret-set KEY=SKIPO_API_KEY" in message
        assert KEY not in message and "synthetic-key" not in message
        assert "api.skipo.com" not in message

    def test_problem_code_that_is_not_a_token_is_dropped(self, monkeypatch):
        """A free-text `code` never reaches the error message."""
        _install(monkeypatch, _Skipo(status=401, code="Key skp_live_synthetic-key expired"))

        with pytest.raises(SkipoAuthError) as excinfo:
            asyncio.run(self.scraper.scrape_products())

        assert "skp_live" not in str(excinfo.value)

    def test_unexpected_balances_payload_fails_the_leg(self, monkeypatch):
        """A balances answer that is not a list raises instead of yielding nothing."""
        _install(monkeypatch, _Skipo(balances={"data": []}))

        with pytest.raises(SkipoApiError):
            asyncio.run(self.scraper.scrape_products())

    def test_rate_limit_is_retried(self, monkeypatch):
        """A 429 on balances is retried and the leg succeeds."""
        fake = _Skipo()
        handler = fake.handler
        calls = {"balances": 0}

        def limited_once(request):
            if request.url.path == "/v2/balances" and calls["balances"] == 0:
                calls["balances"] += 1
                return _Skipo._problem(429, "rate_limited")
            return handler(request)

        fake.handler = limited_once
        _install(monkeypatch, fake)

        result = asyncio.run(self.scraper.scrape_products())

        assert len(result.products) == 3


class TestScrapeTransactions:
    def setup_method(self):
        self.scraper = SkipoScraper()

    def test_reads_only_the_clp_ledger(self, monkeypatch):
        """The ledger is requested for CLP, with the page size Skipo allows."""
        fake = _Skipo(ledger={1: [_entry(1)]})
        _install(monkeypatch, fake)

        asyncio.run(self.scraper.scrape_transactions())

        params = [dict(r.url.params) for r in fake.requests]
        assert params == [{"assetSymbol": "CLP", "page": "1", "limit": "100"}]

    def test_follows_pagination(self, monkeypatch):
        """Every page Skipo reports is fetched, in order."""
        ledger = {1: [_entry(n) for n in range(1, 101)], 2: [_entry(101)]}
        fake = _Skipo(ledger=ledger)
        _install(monkeypatch, fake)

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert len(transactions) == 101
        assert len({t.external_id for t in transactions}) == 101
        assert [r.url.params["page"] for r in fake.requests] == ["1", "2"]

    def test_stops_at_the_page_cap_with_a_warning(self, monkeypatch):
        """Past MAX_PAGES the rest is left out, and the run is flagged partial."""
        monkeypatch.setattr(skipo_mod, "MAX_PAGES", 2)
        ledger = {p: [_entry(p * 1000 + i) for i in range(100)] for p in range(1, 4)}
        _install(monkeypatch, _Skipo(ledger=ledger))

        transactions = asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert len(transactions) == 200
        assert any("capped" in w for w in result.warnings)

    def test_later_page_failure_keeps_earlier_pages(self, monkeypatch):
        """A 500 on page 2 keeps page 1 and surfaces as a products warning."""
        ledger = {1: [_entry(1)], 2: [_entry(2)]}
        _install(monkeypatch, _Skipo(ledger=ledger, fail={2}))

        transactions = asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert [t.external_id for t in transactions] == ["skipo_ldg_synthetic_0001"]
        assert any("stopped early" in w for w in result.warnings)

    def test_warnings_reset_between_cycles(self, monkeypatch):
        """A warning from one cycle doesn't leak into the next."""
        ledger = {1: [_entry(1)], 2: [_entry(2)]}
        _install(monkeypatch, _Skipo(ledger=ledger, fail={2}))
        asyncio.run(self.scraper.scrape_transactions())
        asyncio.run(self.scraper.scrape_products())

        result = asyncio.run(self.scraper.scrape_products())

        assert result.warnings == []

    def test_later_page_timeout_names_the_error(self, monkeypatch):
        """An exception with no message still leaves a readable warning."""
        fake = _Skipo(ledger={1: [_entry(1)], 2: [_entry(2)]})
        handler = fake.handler

        def timeout_on_page_two(request):
            if request.url.params.get("page") == "2":
                raise httpx.ReadTimeout("")
            return handler(request)

        fake.handler = timeout_on_page_two
        _install(monkeypatch, fake)

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert "Skipo: CLP ledger stopped early (ReadTimeout)" in result.warnings

    def test_first_page_failure_fails_the_leg(self, monkeypatch):
        """With nothing read the leg raises instead of returning []."""
        _install(monkeypatch, _Skipo(ledger={1: [_entry(1)]}, fail={1}))

        with pytest.raises(SkipoApiError) as excinfo:
            asyncio.run(self.scraper.scrape_transactions())

        assert "CLP ledger" in str(excinfo.value)

    def test_rejected_key_fails_the_leg(self, monkeypatch):
        """An auth failure is never swallowed."""
        _install(monkeypatch, _Skipo(status=403, code="insufficient_scope"))

        with pytest.raises(SkipoAuthError):
            asyncio.run(self.scraper.scrape_transactions())

    def test_bad_rows_are_skipped_not_fatal(self, monkeypatch):
        """One unusable entry is dropped and the rest are kept."""
        ledger = {1: [_entry(1), _entry(2, type="TRANSFER"), _entry(3, "SELL")]}
        _install(monkeypatch, _Skipo(ledger=ledger))

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert [t.amount for t in transactions] == [1000000, -1000000]

    def test_transactions_land_on_the_wallet_product(self, monkeypatch):
        """Transactions share the (kind, currency) of the singleton CLP wallet."""
        _install(monkeypatch, _Skipo(ledger={1: [_entry(1)]}))

        transactions = asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products()).products

        assert {(t.product_kind, t.currency) for t in transactions} <= {
            (p.kind, p.currency) for p in products if p.external_ref is None
        }
