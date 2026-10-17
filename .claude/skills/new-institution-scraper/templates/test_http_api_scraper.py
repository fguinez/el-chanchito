"""Tests for the Acme scraper.

TEMPLATE: copy to `apps/scrapers/tests/test_<slug>.py` next to the module made
from `http_api_scraper.py`, and rename Acme/acme/ACME.

These never hit the network: `httpx.AsyncClient` is swapped for a real client
over an `httpx.MockTransport` serving canned Acme payloads. Every figure and id
is synthetic.
"""

import asyncio
import logging
import os
from datetime import date

import httpx
import pytest

os.environ.setdefault("ACME_API_TOKEN", "synthetic-token")

from scrapers.institutions import acme as acme_mod
from scrapers.institutions.acme import (
    AcmeAuthError,
    AcmeScraper,
    _local_date,
    _parse_movement,
    _to_clp,
)

TOKEN = "synthetic-token"


def _movement(n: int, amount="1000000", direction="debit", **overrides) -> dict:
    """A synthetic Acme movement with consecutive ids."""
    row = {
        "id": f"{n:012d}",
        "amount": amount,
        "currency": "CLP",
        "direction": direction,
        "description": f"COMERCIO SINTETICO {n}",
        "booked_at": "2026-01-15T12:00:00Z",
    }
    row.update(overrides)
    return row


def _account(balance="2500000", currency="CLP") -> dict:
    """A synthetic Acme account."""
    return {"id": "000000000001", "currency": currency, "available_balance": balance}


class _Acme:
    """Canned Acme API behind an httpx.MockTransport.

    `pages` maps a page number to its movements; `fail` lists pages answering
    500; `status` forces every answer. Like Acme, it answers 401 unless the
    bearer token matches.
    """

    def __init__(self, account=None, pages=None, fail=(), status=None):
        self.account = account or _account()
        self.pages = pages or {}
        self.fail = set(fail)
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            return httpx.Response(self.status, json={"error": "synthetic failure"})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "invalid token"})
        if request.url.path == "/v1/account":
            return httpx.Response(200, json=self.account)
        page = int(request.url.params["page"])
        if page in self.fail:
            return httpx.Response(500, json={"error": "synthetic failure"})
        return httpx.Response(
            200,
            json={
                "movements": self.pages.get(page, []),
                "meta": {"page": page, "total_pages": max(self.pages, default=1)},
            },
        )


def _install(monkeypatch, fake: _Acme) -> None:
    """Route the scraper's httpx.AsyncClient through the fake."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        acme_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(fake.handler), **kwargs
        ),
    )


class TestToClp:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(2500000, 2500000, id="int"),
            pytest.param("999999", 999999, id="numeric-string"),
            pytest.param("1000000.5", 1000001, id="half-rounds-up"),
            pytest.param(1000000.4, 1000000, id="float-rounds-down"),
            pytest.param("-1000000", -1000000, id="negative"),
            pytest.param("$ 1.000.000", None, id="display-format-rejected"),
            pytest.param(None, None, id="none"),
            pytest.param(True, None, id="bool"),
            pytest.param("nan", None, id="not-finite"),
        ],
    )
    def test_parses_machine_amounts(self, raw, expected):
        """JSON numbers and numeric strings become whole CLP, half up."""
        assert _to_clp(raw) == expected


class TestLocalDate:
    def test_converts_utc_evening_to_santiago(self):
        """A UTC timestamp past midnight is still the previous day in Santiago."""
        assert _local_date("2026-01-16T02:30:00Z") == date(2026, 1, 15)

    def test_unreadable_is_none(self):
        """Garbage yields None rather than today's date."""
        assert _local_date("not a date") is None


class TestParseMovement:
    def test_debit_is_negative(self):
        """A debit becomes a negative CLP transaction with a prefixed id."""
        txn = _parse_movement(_movement(1))

        assert txn is not None
        assert txn.amount == -1000000
        assert txn.external_id == "acme_000000000001"

    def test_sets_the_scheduled_month(self):
        """Every transaction carries the first of its month, which the Expenses page filters on."""
        txn = _parse_movement(_movement(1))

        assert txn is not None
        assert txn.scheduled_month == date(2026, 1, 1)

    def test_credit_is_positive(self):
        """A credit becomes a positive transaction."""
        txn = _parse_movement(_movement(1, direction="credit"))

        assert txn is not None
        assert txn.amount == 1000000

    def test_description_does_not_change_the_id(self):
        """A rewritten description keeps the same external_id."""
        first = _parse_movement(_movement(1))
        rewritten = _parse_movement(_movement(1, description="SYNTHETIC STORE 1 SANTIAGO"))

        assert first is not None and rewritten is not None
        assert first.external_id == rewritten.external_id

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"id": None}, id="no-id"),
            pytest.param({"amount": "0"}, id="zero-amount"),
            pytest.param({"amount": None}, id="no-amount"),
            pytest.param({"booked_at": ""}, id="no-date"),
            pytest.param({"direction": None}, id="no-direction"),
            pytest.param({"direction": "reversal"}, id="unknown-direction"),
        ],
    )
    def test_skips_unusable_rows(self, overrides):
        """Rows without an id, amount or date are skipped."""
        assert _parse_movement(_movement(1, **overrides)) is None

    def test_skips_other_currencies_with_a_warning(self, caplog):
        """A non-CLP movement is skipped, not coerced, and logged."""
        with caplog.at_level(logging.WARNING):
            txn = _parse_movement(_movement(1, amount="1234.56", currency="USD"))

        assert txn is None
        assert "not CLP" in caplog.text


class TestScrapeProducts:
    def setup_method(self):
        self.scraper = AcmeScraper()

    def test_emits_the_singleton_checking_product(self, monkeypatch):
        """The account becomes one CLP checking observation with no external_ref."""
        _install(monkeypatch, _Acme())

        result = asyncio.run(self.scraper.scrape_products())

        assert [(p.kind, p.currency, p.external_ref) for p in result.products] == [
            ("checking", "CLP", None)
        ]
        assert result.products[0].metrics.balance == 2500000

    def test_unreadable_balance_is_a_warning(self, monkeypatch):
        """An account without a balance yields no product and a warning."""
        _install(monkeypatch, _Acme(account=_account(balance=None)))

        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == []
        assert result.warnings

    def test_rejected_token_raises_without_leaking_it(self, monkeypatch):
        """A 401 raises AcmeAuthError whose message names the fix, not the token."""
        _install(monkeypatch, _Acme(status=401))

        with pytest.raises(AcmeAuthError) as excinfo:
            asyncio.run(self.scraper.scrape_products())

        assert "make secret-set" in str(excinfo.value)
        assert TOKEN not in str(excinfo.value)


class TestScrapeTransactions:
    def setup_method(self):
        self.scraper = AcmeScraper()

    def test_follows_pagination(self, monkeypatch):
        """Every page Acme reports is fetched, in order."""
        pages = {1: [_movement(n) for n in range(1, 51)], 2: [_movement(51)]}
        _install(monkeypatch, _Acme(pages=pages))

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert len(transactions) == 51
        assert len({t.external_id for t in transactions}) == 51

    def test_later_page_failure_keeps_earlier_pages(self, monkeypatch):
        """A 500 on page 2 keeps page 1's movements."""
        pages = {1: [_movement(1)], 2: [_movement(2)]}
        _install(monkeypatch, _Acme(pages=pages, fail={2}))

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert [t.external_id for t in transactions] == ["acme_000000000001"]

    def test_early_stop_surfaces_as_a_products_warning(self, monkeypatch):
        """A partial movements leg leaves a warning the products leg returns."""
        pages = {1: [_movement(1)], 2: [_movement(2)]}
        _install(monkeypatch, _Acme(pages=pages, fail={2}))

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert any("stopped early" in w for w in result.warnings)

    def test_stops_at_the_page_cap_with_a_warning(self, monkeypatch):
        """Past MAX_PAGES the rest is left out, and the run is flagged partial."""
        monkeypatch.setattr(acme_mod, "MAX_PAGES", 2)
        pages = {n: [_movement(n * 100 + i) for i in range(50)] for n in range(1, 4)}
        fake = _Acme(pages=pages)
        _install(monkeypatch, fake)

        transactions = asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert len(transactions) == 100
        assert any("capped" in w for w in result.warnings)

    def test_non_json_page_keeps_earlier_pages(self, monkeypatch):
        """An HTML error page mid-pagination is a stop, not a crash of the whole leg."""
        fake = _Acme(pages={1: [_movement(1)], 2: [_movement(2)]})
        handler = fake.handler

        def html_on_page_two(request):
            if request.url.params.get("page") == "2":
                return httpx.Response(200, text="<html>maintenance</html>")
            return handler(request)

        fake.handler = html_on_page_two
        _install(monkeypatch, fake)

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert [t.external_id for t in transactions] == ["acme_000000000001"]

    def test_first_page_failure_fails_the_leg(self, monkeypatch):
        """With nothing read the leg raises instead of returning []."""
        _install(monkeypatch, _Acme(pages={1: [_movement(1)]}, fail={1}))

        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(self.scraper.scrape_transactions())

        assert "movements" in str(excinfo.value)

    def test_rejected_token_fails_the_leg(self, monkeypatch):
        """An auth failure is never swallowed, even mid-pagination."""
        _install(monkeypatch, _Acme(status=403))

        with pytest.raises(AcmeAuthError):
            asyncio.run(self.scraper.scrape_transactions())

    def test_transactions_land_on_the_balance_product(self, monkeypatch):
        """Transactions share the (kind, currency) of the singleton product."""
        _install(monkeypatch, _Acme(pages={1: [_movement(1)]}))

        transactions = asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products()).products

        assert {(t.product_kind, t.currency) for t in transactions} <= {
            (p.kind, p.currency) for p in products if p.external_ref is None
        }
