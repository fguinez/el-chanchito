"""Tests for the internal scraper control server (/refresh, /scrapers)."""

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from main import _REFRESH_COOLDOWNS, _make_control_handler
from scrapers.backends import banchile_web


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


@pytest.fixture
def server():
    scheduler = FakeScheduler(["buda", "fintual", "tenpo"])
    handler = _make_control_handler(scheduler, {"buda", "fintual", "tenpo"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    yield scheduler, f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def server_with_jobless_key():
    """Like `server`, but "mach" is a scraper key with no scheduler job
    (as happens in main_scheduled() when a slug has no _SCHEDULES entry)."""
    scheduler = FakeScheduler(["buda", "fintual"])
    handler = _make_control_handler(scheduler, {"buda", "fintual", "mach"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    yield scheduler, f"http://127.0.0.1:{port}"
    httpd.shutdown()


def _request_with_headers(url, method):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def _request(url, method):
    status, body, _ = _request_with_headers(url, method)
    return status, body


class TestControlServer:
    def test_health_ok(self, server):
        _, base = server
        status, body = _request(f"{base}/health", "GET")
        assert status == 200
        assert body == {"status": "ok"}

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


class FakeClock:
    """Monotonic stand-in the tests move by hand (no real sleeps)."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeBanChile:
    """Sets `banchile_web`'s login-cooldown state, on the fake clock."""

    def __init__(self, clock, session_file, monkeypatch):
        self._clock = clock
        self._session_file = session_file
        self._monkeypatch = monkeypatch

    def login_attempted(self):
        self._monkeypatch.setattr(banchile_web, "_last_login_attempt", self._clock())

    def session_cached(self):
        self._session_file.write_text("{}")


@contextmanager
def _serving(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


@pytest.fixture
def cooldown_server(tmp_path, monkeypatch):
    """banchile is gated by the real `login_cooldown_retry_after` on a fake
    clock (default cooldown, no login attempt, no session file); buda/fintual
    have no cooldown."""
    clock = FakeClock()
    session_file = tmp_path / ".banchile_session.json"
    monkeypatch.setenv("BANCHILE_SESSION_FILE", str(session_file))
    monkeypatch.delenv("BANCHILE_LOGIN_COOLDOWN_MINUTES", raising=False)
    monkeypatch.setattr(banchile_web, "_last_login_attempt", None)
    keys = {"banchile", "buda", "fintual"}
    scheduler = FakeScheduler(sorted(keys))
    cooldowns = {"banchile": lambda: banchile_web.login_cooldown_retry_after(now=clock())}
    handler = _make_control_handler(scheduler, keys, cooldowns)
    with _serving(handler) as base:
        yield scheduler, base, clock, FakeBanChile(clock, session_file, monkeypatch)


class TestRefreshCooldown:
    def test_a_certain_login_inside_the_window_is_429(self, cooldown_server):
        scheduler, base, clock, banchile = cooldown_server
        banchile.login_attempted()
        clock.advance(180)

        status, body, headers = _request_with_headers(f"{base}/refresh/banchile", "POST")

        assert status == 429
        assert headers["Retry-After"] == "420"
        assert body["skipped"] == [{"slug": "banchile", "retry_after_seconds": 420}]
        assert scheduler.jobs["banchile"].modified_with is None

    def test_a_cached_session_is_let_through_inside_the_window(self, cooldown_server):
        """The burst the cache serves: a refresh minutes after a run that logged
        in. If the session turns out dead, the backend gate skips the login."""
        scheduler, base, clock, banchile = cooldown_server
        banchile.login_attempted()
        banchile.session_cached()
        clock.advance(180)

        status, body = _request(f"{base}/refresh/banchile", "POST")

        assert status == 202
        assert body["triggered"] == ["banchile"]
        assert scheduler.jobs["banchile"].modified_with is not None

    @pytest.mark.parametrize("elapsed", [600, 3600], ids=["exactly-over", "long-after"])
    def test_trigger_after_the_window_is_accepted(self, cooldown_server, elapsed):
        scheduler, base, clock, banchile = cooldown_server
        banchile.login_attempted()
        clock.advance(elapsed)

        status, body = _request(f"{base}/refresh/banchile", "POST")

        assert status == 202
        assert body["triggered"] == ["banchile"]
        assert "next_run_time" in scheduler.jobs["banchile"].modified_with

    def test_no_login_attempt_yet_is_not_refused(self, cooldown_server):
        scheduler, base, _, _ = cooldown_server
        status, _ = _request(f"{base}/refresh/banchile", "POST")
        assert status == 202
        assert scheduler.jobs["banchile"].modified_with is not None

    def test_scraper_without_cooldown_is_never_refused(self, cooldown_server):
        scheduler, base, _, banchile = cooldown_server
        banchile.login_attempted()

        status, _ = _request(f"{base}/refresh/buda", "POST")

        assert status == 202
        assert scheduler.jobs["buda"].modified_with is not None

    def test_refresh_all_skips_the_refused_scraper(self, cooldown_server):
        scheduler, base, clock, banchile = cooldown_server
        banchile.login_attempted()
        clock.advance(59.5)

        status, body = _request(f"{base}/refresh", "POST")

        assert status == 202
        assert body["triggered"] == ["buda", "fintual"]
        # Rounded up, so a client never retries a moment too early.
        assert body["skipped"] == [{"slug": "banchile", "retry_after_seconds": 541}]
        assert scheduler.jobs["banchile"].modified_with is None
        assert scheduler.jobs["buda"].modified_with is not None

    def test_refresh_all_reports_an_empty_skip_list(self, cooldown_server):
        _, base, _, _ = cooldown_server
        status, body = _request(f"{base}/refresh", "POST")
        assert status == 202
        assert body == {"triggered": ["banchile", "buda", "fintual"], "skipped": []}

    def test_zero_cooldown_disables_it(self, cooldown_server, monkeypatch):
        scheduler, base, _, banchile = cooldown_server
        monkeypatch.setenv("BANCHILE_LOGIN_COOLDOWN_MINUTES", "0")
        banchile.login_attempted()

        status, _ = _request(f"{base}/refresh/banchile", "POST")

        assert status == 202
        assert scheduler.jobs["banchile"].modified_with is not None


def test_banchile_is_gated_by_the_backend():
    assert _REFRESH_COOLDOWNS == {"banchile": banchile_web.login_cooldown_retry_after}
