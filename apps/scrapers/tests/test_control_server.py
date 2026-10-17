"""Tests for the internal scraper control server (/refresh, /scrapers, /health)."""

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

import main
from main import (
    _HEARTBEAT_STALE_SECONDS,
    _REFRESH_COOLDOWNS,
    Heartbeat,
    _make_control_handler,
    _start_control_server,
)
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
    handler = _make_control_handler(scheduler, keys, Heartbeat(), cooldowns)
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


@pytest.fixture
def start_server(monkeypatch):
    """Start the real control server on an ephemeral port; yield a launcher."""
    servers = []
    monkeypatch.setenv("SCRAPER_CONTROL_PORT", "0")

    def start():
        server = _start_control_server(FakeScheduler(["buda"]), {"buda"}, Heartbeat())
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


class TestControlServerBindHost:
    """The unauthenticated trigger listens on loopback unless told otherwise."""

    def test_binds_loopback_by_default(self, start_server, monkeypatch):
        """With SCRAPER_CONTROL_HOST unset, no LAN peer can reach the trigger."""
        monkeypatch.delenv("SCRAPER_CONTROL_HOST", raising=False)

        server = start_server()

        assert server.server_address[0] == "127.0.0.1"

    def test_empty_host_falls_back_to_loopback(self, start_server, monkeypatch):
        """An empty value (e.g. `SCRAPER_CONTROL_HOST=` in .env) is not 0.0.0.0."""
        monkeypatch.setenv("SCRAPER_CONTROL_HOST", "")

        server = start_server()

        assert server.server_address[0] == "127.0.0.1"

    def test_binds_the_configured_host(self, monkeypatch):
        """Compose sets 0.0.0.0 so the web container can reach it.

        The server class is faked: really binding a wildcard address would pop
        up the macOS firewall prompt on every test run.
        """
        bound = []

        class FakeServer:
            def __init__(self, address, handler):
                bound.append(address)

            def serve_forever(self):
                pass

        monkeypatch.setattr(main, "ThreadingHTTPServer", FakeServer)
        monkeypatch.setenv("SCRAPER_CONTROL_PORT", "8080")
        monkeypatch.setenv("SCRAPER_CONTROL_HOST", "0.0.0.0")

        _start_control_server(FakeScheduler(["buda"]), {"buda"}, Heartbeat())

        assert bound == [("0.0.0.0", 8080)]

    def test_stays_disabled_without_a_port(self, monkeypatch):
        """No SCRAPER_CONTROL_PORT means no server, whatever the host."""
        monkeypatch.delenv("SCRAPER_CONTROL_PORT", raising=False)
        monkeypatch.setenv("SCRAPER_CONTROL_HOST", "0.0.0.0")

        server = _start_control_server(FakeScheduler(["buda"]), {"buda"}, Heartbeat())

        assert server is None
