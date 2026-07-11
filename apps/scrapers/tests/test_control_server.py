"""Tests for the internal scraper control server (POST /refresh)."""

import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from main import _make_control_handler


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
    scheduler = FakeScheduler(["buda", "fintual"])
    handler = _make_control_handler(scheduler, {"buda", "fintual"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    yield scheduler, f"http://127.0.0.1:{port}"
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
        assert body == {"status": "ok"}

    def test_refresh_all_triggers_every_configured_scraper(self, server):
        scheduler, base = server
        status, body = _request(f"{base}/refresh", "POST")
        assert status == 202
        assert sorted(body["triggered"]) == ["buda", "fintual"]
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
