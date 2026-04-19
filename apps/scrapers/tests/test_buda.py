"""Tests for the Buda.com scraper."""

import os
import pytest

os.environ.setdefault("BUDA_API_KEY", "test_key")
os.environ.setdefault("BUDA_API_SECRET", "test_secret")

from scrapers.buda import BudaScraper


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
