"""Tests for the internal scraper control server (/refresh, /scrapers, /health)."""

import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from main import _HEARTBEAT_STALE_SECONDS, Heartbeat, _make_control_handler


class FakeJob:
    def __init__(self):
        self.modified_with = None

    def modify(self, **changes):
        self.modified_with = changes


class FakeScheduler:
    """Minimal stand-in exposing just the get_job() the handler uses."""

    def __init__(self, job_ids):
        self.jobs = {jid: FakeJob() for jid in job_ids}

    def get_job(self, job_id):
        return self.jobs.get(job_id)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _serve(scheduler, scraper_keys, heartbeat):
    handler = _make_control_handler(scheduler, scraper_keys, heartbeat)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def server():
    scheduler = FakeScheduler(["buda", "fintual", "tenpo"])
    httpd, base = _serve(scheduler, {"buda", "fintual", "tenpo"}, Heartbeat())
    yield scheduler, base
    httpd.shutdown()


@pytest.fixture
def server_with_clock():
    """Like `server`, with the heartbeat on a clock the test controls."""
    clock = FakeClock()
    heartbeat = Heartbeat(clock=clock)
    httpd, base = _serve(FakeScheduler(["buda"]), {"buda"}, heartbeat)
    yield clock, heartbeat, base
    httpd.shutdown()


@pytest.fixture
def server_with_jobless_key():
    """Like `server`, but "mach" is a scraper key with no scheduler job
    (as happens in main_scheduled() when a slug has no _SCHEDULES entry)."""
    scheduler = FakeScheduler(["buda", "fintual"])
    httpd, base = _serve(scheduler, {"buda", "fintual", "mach"}, Heartbeat())
    yield scheduler, base
    httpd.shutdown()


def _request(url, method):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestControlServer:
    def test_health_ok(self, server):
        _, base = server
        status, body = _request(f"{base}/health", "GET")
        assert status == 200
        assert body["status"] == "ok"

    def test_health_is_503_once_the_heartbeat_goes_stale(self, server_with_clock):
        """A scheduler that stopped running its heartbeat job reads as wedged."""
        clock, _, base = server_with_clock
        clock.now += _HEARTBEAT_STALE_SECONDS

        status, body = _request(f"{base}/health", "GET")

        assert status == 503
        assert body == {
            "status": "stale",
            "heartbeat_age_seconds": _HEARTBEAT_STALE_SECONDS,
        }

    def test_a_beat_makes_health_ok_again(self, server_with_clock):
        clock, heartbeat, base = server_with_clock
        clock.now += _HEARTBEAT_STALE_SECONDS
        heartbeat.beat()

        status, body = _request(f"{base}/health", "GET")

        assert status == 200
        assert body == {"status": "ok", "heartbeat_age_seconds": 0}

    def test_scrapers_lists_enabled_slugs_sorted(self, server):
        _, base = server
        status, body = _request(f"{base}/scrapers", "GET")
        assert status == 200
        assert body == {"scrapers": ["buda", "fintual", "tenpo"]}

    def test_scrapers_excludes_keys_without_a_scheduler_job(
        self, server_with_jobless_key
    ):
        # /scrapers must never advertise a slug POST /refresh/{slug} would
        # 404 on, so a key without a scheduler job stays out of the list.
        _, base = server_with_jobless_key
        status, body = _request(f"{base}/scrapers", "GET")
        assert status == 200
        assert body == {"scrapers": ["buda", "fintual"]}

    def test_refresh_all_triggers_every_configured_scraper(self, server):
        scheduler, base = server
        status, body = _request(f"{base}/refresh", "POST")
        assert status == 202
        assert sorted(body["triggered"]) == ["buda", "fintual", "tenpo"]
        assert scheduler.jobs["buda"].modified_with is not None
        assert "next_run_time" in scheduler.jobs["buda"].modified_with

    def test_refresh_one_triggers_only_that_scraper(self, server):
        scheduler, base = server
        status, body = _request(f"{base}/refresh/buda", "POST")
        assert status == 202
        assert body["triggered"] == ["buda"]
        assert scheduler.jobs["buda"].modified_with is not None
        assert scheduler.jobs["fintual"].modified_with is None

    def test_refresh_unknown_slug_404(self, server):
        _, base = server
        status, body = _request(f"{base}/refresh/nope", "POST")
        assert status == 404
        assert "nope" in body["error"]

    def test_unknown_path_404(self, server):
        _, base = server
        status, _ = _request(f"{base}/whatever", "GET")
        assert status == 404
