"""Tests for the Fintual scraper's per-goal product emission.

These never hit the network: `_load_session` is stubbed to a cache hit and
`httpx.AsyncClient` is swapped for a fake whose GET returns a canned
/api/goals JSON:API payload.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("FINTUAL_EMAIL", "test@example.com")
os.environ.setdefault("FINTUAL_TOKEN", "test_password")

from scrapers.institutions import fintual as fintual_mod
from scrapers.institutions.fintual import FintualScraper


def _goal(goal_id, **attributes):
    """Build one JSON:API goal resource as returned by GET /api/goals."""
    return {"id": goal_id, "type": "goal", "attributes": attributes}


class _FakeClient:
    """Async-context stand-in for httpx.AsyncClient with a canned GET."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = self._payload
        return response


class TestScrapeProducts:
    def _scrape(self, monkeypatch, goals):
        """Run scrape_products() against a canned goals payload."""
        monkeypatch.setattr(
            FintualScraper, "_load_session", lambda self, client: True
        )
        monkeypatch.setattr(
            fintual_mod.httpx,
            "AsyncClient",
            lambda **kwargs: _FakeClient({"data": goals}),
        )
        return asyncio.run(FintualScraper().scrape_products())

    def test_one_product_per_goal(self, monkeypatch):
        """Two goals become two products keyed by the JSON:API resource id."""
        goals = [
            _goal(
                "34567",
                name="Risky Norris",
                nav=12345678.33,
                deposited=12000000.0,
                profit=345678.33,
            ),
            _goal(
                "34811",
                name="Conservative Clooney",
                nav=2500000.0,
                deposited=2600000.0,
                profit=-100000.0,
            ),
        ]

        result = self._scrape(monkeypatch, goals)

        assert result.warnings == []
        assert len(result.products) == 2
        first, second = result.products
        assert first.institution == "fintual"
        assert first.kind == "investment"
        assert first.currency == "CLP"
        assert first.external_ref == "34567"
        assert first.name == "Risky Norris"
        assert first.metrics.nav == 12345678  # rounded to whole CLP
        assert first.metrics.deposited == 12000000.0
        assert first.metrics.profit == 345678.33
        assert second.external_ref == "34811"
        assert second.name == "Conservative Clooney"
        assert second.metrics.nav == 2500000
        assert second.metrics.profit == -100000.0

    def test_missing_deposited_and_profit_are_omitted(self, monkeypatch):
        """A goal without deposited/profit still emits; the fields stay None."""
        goals = [_goal(777, name="Solo", nav=100000)]

        products = self._scrape(monkeypatch, goals).products

        assert len(products) == 1
        product = products[0]
        assert product.external_ref == "777"  # non-string ids are coerced
        assert product.metrics.nav == 100000
        assert product.metrics.deposited is None
        assert product.metrics.profit is None

    def test_null_nav_emits_zero(self, monkeypatch):
        """An explicit `"nav": null` emits the goal valued at 0, not an error."""
        goals = [_goal("901", name="Fresh Goal", nav=None)]

        products = self._scrape(monkeypatch, goals).products

        assert len(products) == 1
        product = products[0]
        assert product.external_ref == "901"
        assert product.metrics.nav == 0

    def test_goal_without_id_is_skipped(self, monkeypatch):
        """Entries with a missing or null id are skipped; other goals still emit."""
        goals = [
            {"type": "goal", "attributes": {"name": "No Id", "nav": 5000.0}},
            _goal(None, name="Null Id", nav=7000.0),
            _goal("34567", name="Risky Norris", nav=12345678.33),
        ]

        result = self._scrape(monkeypatch, goals)

        assert len(result.products) == 1
        assert result.products[0].external_ref == "34567"
        assert result.products[0].metrics.nav == 12345678
        assert result.warnings == ["Fintual: skipped 2 goal(s) without an id"]

    def test_empty_goals_list(self, monkeypatch):
        """No goals means no products, not an error."""
        result = self._scrape(monkeypatch, [])

        assert result.products == []
        assert result.warnings == []


class _SequencedClient:
    """Async-context httpx stand-in whose GET returns statuses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.gets = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        status, payload = self._responses[min(self.gets, len(self._responses) - 1)]
        self.gets += 1
        response = MagicMock()
        response.status_code = status
        response.json.return_value = payload
        return response


class TestAutoLogin:
    def test_expired_session_relogs_in_and_retries(self, monkeypatch):
        """When FINTUAL_EMAIL == EMAIL_IMAP_USER, a 401 triggers a re-login
        via the e-mailed 2FA code, then the scrape succeeds."""
        monkeypatch.setenv("FINTUAL_EMAIL", "test@example.com")
        monkeypatch.setenv("EMAIL_IMAP_USER", "test@example.com")
        monkeypatch.setattr(FintualScraper, "_load_session", lambda self, client: True)
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        monkeypatch.setattr(
            fintual_mod.httpx,
            "AsyncClient",
            lambda **kwargs: _SequencedClient(
                [
                    (401, {}),
                    (200, {"data": [_goal("34567", name="Risky Norris", nav=12345678.33)]}),
                ]
            ),
        )

        result = asyncio.run(FintualScraper().scrape_products())

        login.assert_awaited_once()
        assert len(result.products) == 1
        assert result.products[0].external_ref == "34567"

    def test_no_imap_match_keeps_manual_error(self, monkeypatch):
        """With a different IMAP user, an expired session surfaces the
        manual-login error instead of attempting an auto sign-in."""
        monkeypatch.delenv("EMAIL_IMAP_USER", raising=False)
        monkeypatch.setattr(FintualScraper, "_load_session", lambda self, client: True)
        monkeypatch.setattr(
            fintual_mod.httpx,
            "AsyncClient",
            lambda **kwargs: _SequencedClient([(401, {})]),
        )

        try:
            asyncio.run(FintualScraper().scrape_products())
        except fintual_mod.FintualSessionError as e:
            assert "fintual-login" in str(e)
        else:
            raise AssertionError("expected FintualSessionError")

    def test_missing_session_with_imap_match_relogs_in(self, monkeypatch):
        """No cached session + matching IMAP user logs in automatically."""
        monkeypatch.setenv("FINTUAL_EMAIL", "test@example.com")
        monkeypatch.setenv("EMAIL_IMAP_USER", "test@example.com")
        monkeypatch.setattr(FintualScraper, "_load_session", lambda self, client: False)
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        monkeypatch.setattr(
            fintual_mod.httpx,
            "AsyncClient",
            lambda **kwargs: _SequencedClient(
                [(200, {"data": [_goal("34567", name="Risky Norris", nav=1.0)]})]
            ),
        )

        asyncio.run(FintualScraper().scrape_products())

        login.assert_awaited_once()
