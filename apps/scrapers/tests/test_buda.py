"""Tests for the Buda.com scraper.

These never hit the network: `httpx.AsyncClient` is swapped for a real client
over an `httpx.MockTransport` serving canned Buda payloads. Every figure and id
is synthetic.
"""

import asyncio
import hashlib
import hmac
import logging
import os
from datetime import date

import httpx
import pytest

os.environ.setdefault("BUDA_API_KEY", "test_key")
os.environ.setdefault("BUDA_API_SECRET", "test_secret")

from scrapers.institutions import buda as buda_mod
from scrapers.institutions.buda import BudaScraper, _parse_movement


class TestBudaHmacSigning:
    def setup_method(self):
        self.scraper = BudaScraper()

    def test_sign_produces_required_headers(self):
        headers = self.scraper._sign("GET", "/api/v2/balances")
        assert "X-SBTC-APIKEY" in headers
        assert "X-SBTC-NONCE" in headers
        assert "X-SBTC-SIGNATURE" in headers

    def test_sign_api_key_matches(self):
        headers = self.scraper._sign("GET", "/api/v2/balances")
        assert headers["X-SBTC-APIKEY"] == "test_key"

    def test_signature_is_sha384_hex(self):
        headers = self.scraper._sign("GET", "/api/v2/balances")
        sig = headers["X-SBTC-SIGNATURE"]
        assert len(sig) == 96  # SHA384 hex = 96 chars
        assert all(c in "0123456789abcdef" for c in sig)

    def test_nonce_is_monotonically_increasing(self):
        h1 = self.scraper._sign("GET", "/path1")
        h2 = self.scraper._sign("GET", "/path2")
        assert int(h2["X-SBTC-NONCE"]) > int(h1["X-SBTC-NONCE"])

    def test_different_paths_produce_different_signatures(self):
        h1 = self.scraper._sign("GET", "/api/v2/balances")
        h2 = self.scraper._sign("GET", "/api/v2/orders")
        assert h1["X-SBTC-SIGNATURE"] != h2["X-SBTC-SIGNATURE"]

    def test_sign_with_body(self):
        headers = self.scraper._sign("POST", "/api/v2/orders", '{"amount": 100}')
        assert len(headers["X-SBTC-SIGNATURE"]) == 96


def _movement(item_id, amount, currency="CLP", created_at="2026-01-15T12:00:00.000Z"):
    """One deposit/withdrawal resource as Buda returns it."""
    return {
        "id": item_id,
        "state": "confirmed",
        "currency": currency,
        "created_at": created_at,
        "amount": [amount, currency],
        "fee": ["0.0", currency],
    }


class TestParseMovement:
    def test_clp_deposit(self):
        """A CLP deposit becomes a positive CLP transaction on the crypto product."""
        txn = _parse_movement(_movement(1001, "2500000.0"), "deposits")

        assert txn.amount == 2_500_000
        assert txn.currency == "CLP"
        assert txn.product_kind == "crypto"
        assert txn.external_id == "buda_1001"
        assert txn.description == "Buda deposit CLP"
        assert txn.transaction_date == date(2026, 1, 15)
        assert txn.scheduled_month == date(2026, 1, 1)

    def test_clp_withdrawal_is_negative(self):
        """A CLP withdrawal is recorded as an outflow."""
        txn = _parse_movement(_movement(1002, "1000000.0"), "withdrawals")

        assert txn.amount == -1_000_000
        assert txn.description == "Buda withdrawal CLP"

    @pytest.mark.parametrize(
        ("amount", "currency"),
        [("0.5", "BTC"), ("1.7", "BTC"), ("12.34", "ETH"), ("1234.56", "USDC")],
    )
    def test_non_clp_amount_is_skipped_with_a_warning(self, caplog, amount, currency):
        """A crypto amount is skipped with a warning, never truncated into pesos."""
        with caplog.at_level(logging.WARNING, logger=buda_mod.__name__):
            txn = _parse_movement(_movement(1003, amount, currency), "deposits")

        assert txn is None
        assert f"is in {currency}, not CLP" in caplog.text

    def test_fractional_clp_rounds_instead_of_truncating(self):
        """A sub-peso fraction rounds half up rather than being cut off."""
        txn = _parse_movement(_movement(1004, "999999.5"), "deposits")

        assert txn.amount == 1_000_000

    def test_missing_currency_marker_reads_as_clp(self):
        """An amount without a currency marker takes the CLP endpoint's currency."""
        item = {
            "id": 1005,
            "created_at": "2026-01-15T12:00:00Z",
            "amount": ["999999.0"],
        }

        txn = _parse_movement(item, "deposits")

        assert txn.amount == 999_999
        assert txn.currency == "CLP"

    def test_missing_marker_falls_back_to_the_item_currency(self):
        """With no marker on the amount, the item's own currency still rules."""
        item = {"id": 1010, "currency": "BTC", "amount": ["0.5"]}

        assert _parse_movement(item, "deposits") is None

    @pytest.mark.parametrize("state", ["anulled", "annulled", "rejected", "Canceled"])
    def test_failed_movement_is_skipped(self, state):
        """An annulled or rejected movement never moved money, so it is not imported."""
        item = {**_movement(1011, "1000000.0"), "state": state}

        assert _parse_movement(item, "withdrawals") is None

    @pytest.mark.parametrize("state", ["confirmed", "pending", None])
    def test_other_states_are_imported(self, state):
        """Confirmed, pending and unmarked movements keep being imported."""
        item = {**_movement(1012, "1000000.0"), "state": state}

        assert _parse_movement(item, "deposits").amount == 1_000_000

    @pytest.mark.parametrize(
        "item",
        [
            {"amount": ["2500000.0", "CLP"]},
            {"id": 1006, "amount": ["0.0", "CLP"]},
            {"id": 1007, "amount": ["not-a-number", "CLP"]},
            {"id": 1008, "amount": ["NaN", "CLP"]},
            {"id": 1009},
        ],
        ids=["missing-id", "zero", "unreadable", "nan", "missing-amount"],
    )
    def test_unusable_movement_is_skipped(self, item):
        """No id, no amount, or a zero/unreadable amount yields nothing."""
        assert _parse_movement(item, "deposits") is None


class _Buda:
    """Canned Buda API behind an httpx.MockTransport.

    `pages` maps (tx_type, page) to the movements on that page; `meta` toggles
    Buda's pagination block; `fail` lists (tx_type, page) pairs answering 500.
    Like Buda, it answers 401 unless the signature covers the exact path sent.
    """

    def __init__(self, pages=None, meta=True, fail=(), balances=()):
        self.pages = pages or {}
        self.meta = meta
        self.fail = set(fail)
        self.balances = list(balances)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nonce = request.headers["X-SBTC-NONCE"]
        signed = f"GET {request.url.raw_path.decode()} {nonce}"
        expected = hmac.new(b"test_secret", signed.encode(), hashlib.sha384).hexdigest()
        if request.headers["X-SBTC-SIGNATURE"] != expected:
            return httpx.Response(401, json={"message": "invalid signature"})
        path = request.url.path
        if path == "/api/v2/balances.json":
            return httpx.Response(200, json={"balances": self.balances})
        tx_type = path.rsplit("/", 1)[-1].removesuffix(".json")
        page = int(request.url.params["page"])
        if (tx_type, page) in self.fail:
            return httpx.Response(500, json={"message": "synthetic failure"})
        body = {tx_type: self.pages.get((tx_type, page), [])}
        if self.meta:
            total = max([p for (t, p) in self.pages if t == tx_type], default=1)
            body["meta"] = {"current_page": page, "total_pages": total}
        return httpx.Response(200, json=body)


def _install(monkeypatch, fake: _Buda) -> None:
    """Route the scraper's httpx.AsyncClient through the fake."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        buda_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(fake.handler), **kwargs
        ),
    )


def _clp_page(start, count):
    """`count` synthetic CLP deposits with consecutive ids from `start`."""
    return [_movement(start + i, "1000000.0") for i in range(count)]


class TestScrapeTransactions:
    def test_only_clp_endpoints_are_queried(self, monkeypatch):
        """Crypto movement endpoints are never fetched."""
        fake = _Buda({("deposits", 1): _clp_page(1, 1)})
        _install(monkeypatch, fake)

        txns = asyncio.run(BudaScraper().scrape_transactions())

        paths = {r.url.path for r in fake.requests}
        assert paths == {
            "/api/v2/currencies/clp/deposits.json",
            "/api/v2/currencies/clp/withdrawals.json",
        }
        assert len(txns) == 1

    def test_signature_must_cover_the_query_string(self, monkeypatch):
        """Signing the bare path, without `?per=..&page=..`, is rejected."""
        original = BudaScraper._sign
        monkeypatch.setattr(
            BudaScraper,
            "_sign",
            lambda self, method, path, body="": original(
                self, method, path.split("?")[0], body
            ),
        )
        _install(monkeypatch, _Buda({("deposits", 1): _clp_page(1, 1)}))

        txns = asyncio.run(BudaScraper().scrape_transactions())

        assert txns == []

    def test_follows_meta_total_pages(self, monkeypatch):
        """Every page Buda reports is fetched, deposits and withdrawals alike."""
        fake = _Buda(
            {
                ("deposits", 1): _clp_page(1, 50),
                ("deposits", 2): _clp_page(51, 50),
                ("deposits", 3): _clp_page(101, 3),
                ("withdrawals", 1): [_movement(9001, "999999.0")],
            }
        )
        _install(monkeypatch, fake)

        txns = asyncio.run(BudaScraper().scrape_transactions())

        assert len(txns) == 104
        assert len({t.external_id for t in txns}) == 104
        assert sum(t.amount < 0 for t in txns) == 1

    def test_without_meta_stops_at_the_first_short_page(self, monkeypatch):
        """With no `meta` block, a page shorter than PAGE_SIZE is the last one."""
        fake = _Buda(
            {("deposits", 1): _clp_page(1, 50), ("deposits", 2): _clp_page(51, 7)},
            meta=False,
        )
        _install(monkeypatch, fake)

        txns = asyncio.run(BudaScraper().scrape_transactions())

        deposit_pages = [
            r.url.params["page"] for r in fake.requests if "deposits" in r.url.path
        ]
        assert deposit_pages == ["1", "2"]
        assert len(txns) == 57

    def test_page_cap_stops_and_warns(self, monkeypatch, caplog):
        """A history longer than MAX_PAGES stops at the cap with a warning."""
        monkeypatch.setattr(buda_mod, "MAX_PAGES", 2)
        fake = _Buda({("deposits", p): _clp_page(p * 100, 50) for p in (1, 2, 3)})
        _install(monkeypatch, fake)

        with caplog.at_level(logging.WARNING, logger=buda_mod.__name__):
            txns = asyncio.run(BudaScraper().scrape_transactions())

        assert len(txns) == 100
        assert "stopped after 2 pages" in caplog.text

    def test_failed_page_keeps_earlier_pages(self, monkeypatch):
        """A page that errors keeps what was already read and lets the other leg run."""
        fake = _Buda(
            {
                ("deposits", 1): _clp_page(1, 50),
                ("deposits", 2): _clp_page(51, 50),
                ("withdrawals", 1): [_movement(9001, "999999.0")],
            },
            fail={("deposits", 2)},
        )
        _install(monkeypatch, fake)

        txns = asyncio.run(BudaScraper().scrape_transactions())

        assert len(txns) == 51

    def test_non_clp_item_on_the_clp_endpoint_is_dropped(self, monkeypatch):
        """A crypto amount on the CLP endpoint still never becomes a transaction."""
        fake = _Buda(
            {("deposits", 1): [_movement(1, "2500000.0"), _movement(2, "0.5", "BTC")]}
        )
        _install(monkeypatch, fake)

        txns = asyncio.run(BudaScraper().scrape_transactions())

        assert [t.external_id for t in txns] == ["buda_1"]


class TestScrapeProducts:
    def test_clp_movements_attach_to_the_clp_balance_product(self, monkeypatch):
        """CLP balance and movements share (kind, currency); BTC has its own product."""
        fake = _Buda(
            {("deposits", 1): _clp_page(1, 1)},
            balances=[
                {"id": "CLP", "available_amount": ["2500000.0", "CLP"]},
                {"id": "BTC", "available_amount": ["0.5", "BTC"]},
                {"id": "ETH", "available_amount": ["0.0", "ETH"]},
            ],
        )
        _install(monkeypatch, fake)
        scraper = BudaScraper()

        products = asyncio.run(scraper.scrape_products()).products
        txn = asyncio.run(scraper.scrape_transactions())[0]

        identities = {(p.kind, p.currency): p.metrics.units for p in products}
        assert identities == {("crypto", "CLP"): 2_500_000.0, ("crypto", "BTC"): 0.5}
        assert (txn.product_kind, txn.currency) == ("crypto", "CLP")
