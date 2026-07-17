"""Tests for the Fintual scraper's per-goal product emission.

These never hit the network: `_load_session` is stubbed to a cache hit and
`httpx.AsyncClient` is swapped for a fake whose GET returns a canned
/api/goals JSON:API payload.
"""

import asyncio
import os
from unittest.mock import MagicMock

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

        products = self._scrape(monkeypatch, goals)

        assert len(products) == 2
        first, second = products
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

        products = self._scrape(monkeypatch, goals)

        assert len(products) == 1
        product = products[0]
        assert product.external_ref == "777"  # non-string ids are coerced
        assert product.metrics.nav == 100000
        assert product.metrics.deposited is None
        assert product.metrics.profit is None

    def test_null_nav_emits_zero(self, monkeypatch):
        """An explicit `"nav": null` emits the goal valued at 0, not an error."""
        goals = [_goal("901", name="Fresh Goal", nav=None)]

        products = self._scrape(monkeypatch, goals)

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

        products = self._scrape(monkeypatch, goals)

        assert len(products) == 1
        assert products[0].external_ref == "34567"
        assert products[0].metrics.nav == 12345678

    def test_empty_goals_list(self, monkeypatch):
        """No goals means no products, not an error."""
        assert self._scrape(monkeypatch, []) == []
