"""Tests for the Buda.com scraper."""

import asyncio
import os

import httpx
import pytest

os.environ.setdefault("BUDA_API_KEY", "test_key")
os.environ.setdefault("BUDA_API_SECRET", "test_secret")

from scrapers import retry
from scrapers.institutions import buda as buda_mod
from scrapers.institutions.buda import BudaScraper


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


class _SequencedClient:
    """Async-context httpx stand-in: GETs answer the scripted responses in
    order, and every request's headers are recorded."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.headers.append(headers)
        return self._responses[len(self.headers) - 1]


class TestBudaRetries:
    def test_a_503_on_balances_is_retried_with_a_fresh_signature(self, monkeypatch):
        """Buda rejects a reused nonce, so each attempt is signed anew."""
        monkeypatch.setattr(retry, "BASE_DELAY_SECONDS", 0.0)
        request = httpx.Request("GET", "https://www.buda.com/api/v2/balances.json")
        balances = {"balances": [{"id": "BTC", "available_amount": ["0.5", "BTC"]}]}
        client = _SequencedClient(
            [
                httpx.Response(503, request=request),
                httpx.Response(200, json=balances, request=request),
            ]
        )
        monkeypatch.setattr(buda_mod.httpx, "AsyncClient", lambda **kwargs: client)

        result = asyncio.run(BudaScraper().scrape_products())

        assert [p.currency for p in result.products] == ["BTC"]
        nonces = [h["X-SBTC-NONCE"] for h in client.headers]
        assert len(nonces) == 2 and nonces[0] != nonces[1]
