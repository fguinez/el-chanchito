"""Tests for the dashboard-started sign-ins (interactive_login) and their
/login routes on the control server."""

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import main
from interactive_login import (
    InteractiveLogins,
    LoginError,
    LoginProvider,
    LoginSession,
    RunGuard,
    build_providers,
)
from main import Heartbeat, _make_control_handler
from scrapers.base import SignInRequired


class Spawner:
    """Runs each sign-in on a thread the test can join."""

    def __init__(self):
        self.threads: list[threading.Thread] = []

    def __call__(self, target):
        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()

    def join(self):
        for thread in self.threads:
            thread.join(timeout=5)


class Trigger:
    def __init__(self, jobs=("fintual", "mercadopago", "bci_lider")):
        self.jobs = set(jobs)
        self.calls: list[str] = []

    def __call__(self, slug):
        self.calls.append(slug)
        return slug in self.jobs


def _code_provider(received: list[str]):
    def run(await_code):
        received.append(await_code())

    return LoginProvider("code", run)


def _browser_provider(error: Exception | None = None, started: threading.Event | None = None):
    release = threading.Event()

    def run(_):
        if started:
            started.set()
        release.wait(5)
        if error:
            raise error

    provider = LoginProvider("browser", run)
    provider.release = release  # type: ignore[attr-defined]
    return provider


@pytest.fixture
def spawner():
    return Spawner()


@pytest.fixture
def trigger():
    return Trigger()


def _wait_state(logins, slug, state):
    for _ in range(500):
        if logins.status(slug)[1]["state"] == state:
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"{slug} never reached {state}: {logins.status(slug)}")


def test_code_sign_in_waits_for_the_code_then_refreshes(spawner, trigger):
    """A code sign-in hands the posted code to the provider and refreshes on success."""
    received: list[str] = []
    guard = RunGuard()
    logins = InteractiveLogins({"fintual": _code_provider(received)}, trigger, guard, spawn=spawner)

    logins.start("fintual")
    _wait_state(logins, "fintual", "awaiting_code")
    code_status, _ = logins.submit_code("fintual", " 123456 ")
    spawner.join()

    assert (code_status, received, logins.status("fintual")[1]["state"], trigger.calls) == (
        202,
        ["123456"],
        "succeeded",
        ["fintual"],
    )
    assert guard.holder("fintual") is None


def test_a_code_posted_as_the_wait_times_out_still_counts(trigger):
    """The timeout re-checks under the lock, so an accepted code is never dropped."""
    logins = InteractiveLogins({"fintual": _code_provider([])}, trigger, RunGuard(), code_timeout=0)
    session = LoginSession("fintual", "code")
    session._code = "123456"

    assert logins._await_code(session) == "123456"


def test_code_sign_in_times_out(spawner, trigger):
    """No code within the timeout fails the sign-in with a message to show."""
    logins = InteractiveLogins(
        {"fintual": _code_provider([])}, trigger, RunGuard(), code_timeout=0, spawn=spawner
    )

    logins.start("fintual")
    spawner.join()

    status = logins.status("fintual")[1]
    assert (status["state"], status["message"], trigger.calls) == (
        "failed",
        "No code arrived in time; start the sign-in again.",
        [],
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (LoginError("No code arrived."), "No code arrived."),
        (SignInRequired("Signed in, but not saved."), "Signed in, but not saved."),
        (RuntimeError("https://example.test/secret?token=x"), "Sign-in failed (RuntimeError)."),
    ],
)
def test_failed_sign_in_shows_only_safe_messages(spawner, trigger, error, message):
    """Our own sign-in errors are shown; anything else is reduced to its type."""
    provider = _browser_provider(error)
    provider.release.set()
    logins = InteractiveLogins({"mercadopago": provider}, trigger, RunGuard(), spawn=spawner)

    logins.start("mercadopago")
    spawner.join()

    assert logins.status("mercadopago")[1]["message"] == message


def test_second_start_while_running_is_refused(spawner, trigger):
    """Only one sign-in per institution at a time."""
    started = threading.Event()
    provider = _browser_provider(started=started)
    logins = InteractiveLogins({"mercadopago": provider}, trigger, RunGuard(), spawn=spawner)
    logins.start("mercadopago")
    started.wait(5)

    status, _ = logins.start("mercadopago")
    provider.release.set()
    spawner.join()

    assert status == 409


def test_sign_in_is_refused_while_a_run_holds_the_institution(spawner, trigger):
    """A sign-in never overlaps a scraper run of the same institution."""
    guard = RunGuard()
    guard.claim("mercadopago", "run")
    logins = InteractiveLogins({"mercadopago": _browser_provider()}, trigger, guard, spawn=spawner)

    status, body = logins.start("mercadopago")

    assert (status, spawner.threads) == (409, [])
    assert "running" in body["error"]


def test_refresh_sign_in_triggers_the_run(spawner, trigger):
    """A refresh sign-in is the scraper run itself, started now."""
    logins = InteractiveLogins(
        {"bci_lider": LoginProvider("refresh")}, trigger, RunGuard(), spawn=spawner
    )

    status, body = logins.start("bci_lider")

    assert (status, body["state"], trigger.calls, spawner.threads) == (
        202,
        "succeeded",
        ["bci_lider"],
        [],
    )


@pytest.mark.parametrize(
    ("code", "status"),
    [("12345", 400), ("abcdef", 400), (123456, 400), ("123456", 409)],
)
def test_submit_code_validates_and_needs_a_waiting_sign_in(trigger, code, status):
    """A code must be 6 digits and only goes to a sign-in that is waiting for one."""
    logins = InteractiveLogins({"fintual": _code_provider([])}, trigger, RunGuard())

    assert logins.submit_code("fintual", code)[0] == status


def test_unknown_institution_is_404(trigger):
    """Institutions without an interactive sign-in answer 404 everywhere."""
    logins = InteractiveLogins({}, trigger, RunGuard())

    assert [logins.start("banchile")[0], logins.status("banchile")[0]] == [404, 404]


@pytest.mark.parametrize(
    ("env", "keys", "expected"),
    [
        ({"FINTUAL_PASSWORD": "synthetic"}, {"fintual"}, {"fintual": "code"}),
        ({"FINTUAL_TOKEN": "synthetic"}, {"fintual"}, {"fintual": "code"}),
        ({}, {"fintual"}, {}),
        ({}, {"mercadopago", "bci_lider"}, {"mercadopago": "browser", "bci_lider": "refresh"}),
        ({"MERCADOPAGO_CDP_URL": "http://localhost:9333"}, {"mercadopago"}, {}),
        ({"LIDER_BCI_CDP_URL": "http://localhost:9333"}, {"bci_lider"}, {}),
        ({"FINTUAL_PASSWORD": "synthetic"}, {"buda"}, {}),
    ],
)
def test_build_providers(monkeypatch, env, keys, expected):
    """Only enabled scrapers whose sign-in can run unattended by a terminal."""
    for name in ("FINTUAL_PASSWORD", "FINTUAL_TOKEN", "LIDER_BCI_CDP_URL", "MERCADOPAGO_CDP_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    providers = build_providers(keys)

    assert {slug: p.method for slug, p in providers.items()} == expected


@pytest.fixture
def login_server(trigger):
    received: list[str] = []
    logins = InteractiveLogins(
        {"fintual": _code_provider(received), "bci_lider": LoginProvider("refresh")},
        trigger,
        RunGuard(),
    )
    handler = _make_control_handler(None, set(), Heartbeat(), logins=logins)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield logins, received, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _request(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


JSON = {"Content-Type": "application/json"}


def test_login_routes_drive_a_code_sign_in(login_server):
    """List, start, poll and post the code through the control server."""
    logins, received, base = login_server

    listed = _request(f"{base}/login")
    started = _request(f"{base}/login/fintual", "POST", {}, JSON)
    _wait_state(logins, "fintual", "awaiting_code")
    coded = _request(f"{base}/login/fintual/code", "POST", {"code": "000000"}, JSON)
    _wait_state(logins, "fintual", "succeeded")

    assert (listed[1]["logins"], started[0], coded[0], received) == (
        [
            {"institution": "bci_lider", "method": "refresh"},
            {"institution": "fintual", "method": "code"},
        ],
        202,
        202,
        ["000000"],
    )


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("POST", "/login/fintual", {}, 415),
        ("POST", "/login/fintual", {"Content-Type": "text/plain"}, 415),
        ("POST", "/login/fintual", {**JSON, "Origin": "http://evil.test"}, 403),
        ("GET", "/login", {"Sec-Fetch-Site": "cross-site"}, 403),
        ("GET", "/login/fintual", {"Origin": "http://localhost:3000"}, 403),
        ("POST", "/login/fintual/other", JSON, 404),
    ],
)
def test_login_routes_refuse_browsers_and_unlabelled_posts(
    login_server, method, path, headers, status
):
    """A browser reaching the port directly, or a form-style post, starts nothing."""
    logins, _, base = login_server

    code, _ = _request(f"{base}{path}", method, {} if method == "POST" else None, headers)

    assert (code, logins.status("fintual")[1]["state"]) == (status, "idle")


def test_login_routes_are_404_without_logins():
    """A control server built without sign-ins has no /login routes."""
    handler = _make_control_handler(None, set(), Heartbeat())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    status, _ = _request(f"http://127.0.0.1:{httpd.server_address[1]}/login")
    httpd.shutdown()

    assert status == 404


def test_run_is_skipped_while_a_sign_in_holds_the_institution(monkeypatch):
    """A scheduled run of an institution being signed in to records nothing."""
    started: list[str] = []
    monkeypatch.setattr(main, "start_scraper_run", lambda *a: started.append(a) or 1)
    main._RUN_GUARD.claim("fintual", "login")

    class Scraper:
        name = "Fintual"
        institution = "fintual"
        method = "api"

    try:
        asyncio.run(main.run_scraper(Scraper()))
    finally:
        main._RUN_GUARD.release("fintual")

    assert started == []


def test_cancelled_sign_in_does_not_stay_running(trigger):
    """A BaseException out of the provider still ends the sign-in as failed."""

    def run(_):
        raise KeyboardInterrupt

    logins = InteractiveLogins({"mercadopago": LoginProvider("browser", run)}, trigger, RunGuard())
    session_started = []
    logins._spawn = lambda target: session_started.append(target)
    logins.start("mercadopago")

    with pytest.raises(KeyboardInterrupt):
        session_started[0]()

    assert logins.status("mercadopago")[1]["state"] == "failed"
