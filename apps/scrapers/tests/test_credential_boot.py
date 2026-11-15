"""The scraper service's start: it comes up without the database, and the
first scrape waits until the stored credentials are applied.

Every value is synthetic and every key is generated per test.
"""

import asyncio
import os

import pytest
from credential_manager import CredentialManager

import main
from main import ScraperSet
from scrapers.credentials import OpenedCredentials
from tests.fakes_credentials import FakeRing
from tests.test_credentials import SECRET, FakeScheduler, _row


@pytest.fixture
def store():
    return FakeRing()


def test_start_never_touches_the_database(tmp_path, store):
    """The service comes up and reports status while the database is down."""
    store.fail = True
    manager = CredentialManager(key_path=tmp_path / "credentials_key", environ={}, ring=store)
    manager.start()
    assert manager.key is not None
    assert store.activated == [] and store.fetches == 0
    assert manager.status()["state"] == "starting"
    assert not manager.settled.is_set()


def test_first_scrape_waits_for_the_stored_credentials(tmp_path, store, monkeypatch):
    """The first run must not log in with credentials the dashboard replaced,
    so it waits until they are applied."""
    for name in main._credential_env_vars("skipo"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main, "_SETTLE_POLL_SECONDS", 0.01)
    ran: list[dict] = []

    async def fake_run_all_once(scrapers):
        ran.append({slug: os.environ.get("SKIPO_API_KEY") for slug in scrapers})

    monkeypatch.setattr(main, "run_all_once", fake_run_all_once)
    store.fail = True
    manager = CredentialManager(
        key_path=tmp_path / "credentials_key", environ=os.environ, ring=store
    )
    manager.start()
    scraper_set = ScraperSet(
        FakeScheduler(),
        None,
        build=lambda: {"skipo": object()} if os.environ.get("SKIPO_API_KEY") else {},
    )

    async def scenario():
        task = asyncio.ensure_future(main._first_scrape(manager, scraper_set, "8080"))
        await asyncio.sleep(0.05)
        assert not ran and not task.done()
        store.fail = False
        store.activate(manager.key)
        store.rows = [_row(manager.key, "skipo", "api_key", SECRET)]
        manager.tick()
        await asyncio.wait_for(task, 2)

    try:
        asyncio.run(scenario())
        assert ran == [{"skipo": SECRET}]
    finally:
        manager.overlay.apply(OpenedCredentials({}, frozenset()))


def test_first_rebuild_builds_without_resetting_state(monkeypatch):
    resets = []
    monkeypatch.setattr(main, "_reset_scraper_state", lambda slug, s: resets.append(slug))
    scraper = object()
    scrapers = ScraperSet(FakeScheduler(), None, build=lambda: {"skipo": scraper})
    assert scrapers.snapshot() == frozenset()
    assert scrapers.rebuild()["added"] == ["skipo"]
    assert scrapers.current() == {"skipo": scraper}
    assert resets == []
