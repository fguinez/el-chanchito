"""Sealed credentials: the HPKE scheme, the key file, the env overlay, the
service wiring and the control endpoints.

Every value is synthetic (`synthetic-*`, user@example.com, 11.111.111-1), and
every key is generated per test or is the throwaway fixture key, which
protects nothing.
"""

import asyncio
import base64
import json
import os
import stat
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from credential_manager import CredentialManager
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

import main
from main import CredentialControl, Heartbeat, ScraperSet, _make_control_handler
from scrapers.credentials import (
    V1,
    CredentialKey,
    CredentialKeyError,
    CredentialOpenError,
    CredentialOverlay,
    OpenedCredentials,
    SealedCredential,
    binding_digest,
    env_fingerprint,
    generate_key,
    load_key,
    load_or_create_key,
    open_rows,
    sealing_info,
)
from tests.fakes_credentials import FakeRing as FakeStore

FIXTURE = Path(__file__).parent / "fixtures" / "credentials" / "ts_sealed.json"
SECRET = "synthetic-password-1234"


@pytest.fixture(params=["v1", "v2"])
def key(request) -> CredentialKey:
    """Every scheme test runs with a v1 (X25519) key and a v2 (X-Wing) one."""
    if request.param == "v1":
        return CredentialKey.from_private(X25519PrivateKey.generate())
    return generate_key()


def _row(key: CredentialKey, slug: str, field: str, value: str) -> SealedCredential:
    return SealedCredential(slug, field, key.scheme, key.key_id, key.seal(slug, field, value))


# --- Scheme ---------------------------------------------------------------------


def test_round_trip(key):
    sealed = key.seal("banchile", "password", SECRET)
    assert SECRET.encode() not in sealed
    assert key.open("banchile", "password", key.key_id, sealed) == SECRET


def test_each_seal_is_fresh(key):
    assert key.seal("banchile", "password", SECRET) != key.seal("banchile", "password", SECRET)


@pytest.mark.parametrize(
    ("slug", "field"),
    [("santander", "password"), ("banchile", "rut")],
    ids=["other-institution", "other-field"],
)
def test_context_mismatch_fails(key, slug, field):
    sealed = key.seal("banchile", "password", SECRET)
    with pytest.raises(CredentialOpenError):
        key.open(slug, field, key.key_id, sealed)


def test_other_key_fails(key):
    other = CredentialKey.from_private(X25519PrivateKey.generate())
    sealed = other.seal("banchile", "password", SECRET)
    with pytest.raises(CredentialOpenError):
        key.open("banchile", "password", other.key_id, sealed)
    with pytest.raises(CredentialOpenError):
        key.open("banchile", "password", key.key_id, sealed)


def test_tampered_ciphertext_fails(key):
    sealed = bytearray(key.seal("banchile", "password", SECRET))
    sealed[-1] ^= 0x01
    with pytest.raises(CredentialOpenError):
        key.open("banchile", "password", key.key_id, bytes(sealed))


def test_errors_never_carry_the_value(key):
    sealed = key.seal("banchile", "password", SECRET)
    with pytest.raises(CredentialOpenError) as excinfo:
        key.open("santander", "password", key.key_id, sealed)
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(_row(key, "banchile", "password", SECRET))


@pytest.mark.parametrize(
    ("slug", "field", "key_id"),
    [("bad|slug", "password", "0" * 64), ("banchile", "Password", "0" * 64), ("a", "b", "xyz")],
)
def test_info_rejects_malformed_parts(slug, field, key_id):
    with pytest.raises(CredentialOpenError):
        sealing_info(slug, field, key_id, scheme=V1.scheme)


def test_dashboard_fixture_opens():
    """A ciphertext the dashboard's TypeScript code sealed to a throwaway v1
    key opens here, and only in its own context. The fixture is frozen: the
    dashboard no longer seals v1, but v1 rows still have to open until a
    rotation re-seals them."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scalar = bytes.fromhex(fixture["recipient_scalar_hex"])
    key = CredentialKey.from_private(X25519PrivateKey.from_private_bytes(scalar))
    assert key.key_id == fixture["recipient_fingerprint"]
    assert fixture["scheme"] == V1.scheme == key.scheme
    for entry in fixture["entries"]:
        sealed = base64.b64decode(entry["sealed"])
        binding = binding_digest(entry["binding_values"]) if "binding_values" in entry else None
        assert (
            key.open(entry["slug"], entry["field"], key.key_id, sealed, binding) == entry["value"]
        )
        with pytest.raises(CredentialOpenError):
            key.open(entry["slug"], "other_field", key.key_id, sealed, binding)
        if binding is not None:
            with pytest.raises(CredentialOpenError):
                key.open(entry["slug"], entry["field"], key.key_id, sealed)


# --- Key file -------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_creates_an_owner_only_key(tmp_path):
    path = tmp_path / "state" / "credentials_key"
    key = load_or_create_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_key(path).key_id == key.key_id
    assert list(path.parent.iterdir()) == [path]  # no temp file left behind


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_refuses_a_key_readable_by_others(tmp_path):
    path = tmp_path / "credentials_key"
    load_or_create_key(path)
    path.chmod(0o640)
    with pytest.raises(CredentialKeyError, match="chmod 600"):
        load_or_create_key(path)


@pytest.mark.skipif(os.name == "nt", reason="O_NOFOLLOW is POSIX")
def test_refuses_a_symlinked_key(tmp_path):
    real = tmp_path / "real_key"
    load_or_create_key(real)
    link = tmp_path / "credentials_key"
    link.symlink_to(real)
    with pytest.raises(CredentialKeyError):
        load_key(link)


def test_refuses_a_key_of_another_type(tmp_path):
    path = tmp_path / "credentials_key"
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, pem)
    os.close(fd)
    with pytest.raises(CredentialKeyError, match="X25519"):
        load_key(path)


def test_concurrent_creation_keeps_the_first_key(tmp_path, monkeypatch):
    """When another process writes the key between our check and our link,
    its key is loaded and ours is discarded."""
    path = tmp_path / "credentials_key"
    first = load_or_create_key(tmp_path / "first")
    real_link = os.link

    def racing_link(src, dst):
        os.rename(tmp_path / "first", dst)
        return real_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    from scrapers import credentials

    assert credentials._create_key(path).key_id == first.key_id


# --- Rows and overlay -----------------------------------------------------------


def test_open_rows_flags_rows_it_cannot_open(key):
    other = CredentialKey.from_private(X25519PrivateKey.generate())
    swapped = key.seal("santander", "password", "synthetic-other")
    rows = [
        _row(key, "banchile", "password", SECRET),
        _row(key, "banchile", "rut", "11.111.111-1"),
        _row(other, "buda", "api_key", "synthetic-key"),
        SealedCredential("santander", "rut", key.scheme, key.key_id, swapped),
        _row(key, "unknown_bank", "password", "synthetic-x"),
    ]
    opened = open_rows(key, rows)
    assert opened.values == {"BANCHILE_PASSWORD": SECRET, "BANCHILE_RUT": "11.111.111-1"}
    assert opened.needs_reentry == {"BUDA_API_KEY", "SANTANDER_RUT"}
    assert SECRET not in repr(opened)


def test_open_rows_without_a_key_opens_and_flags_nothing(key):
    opened = open_rows(None, [_row(key, "banchile", "password", SECRET)])
    assert opened == OpenedCredentials({}, frozenset())


def test_stored_values_win_and_deleting_restores_the_fallback():
    environ = {"BANCHILE_PASSWORD": "synthetic-from-keychain", "FINTUAL_TOKEN": "synthetic-legacy"}
    overlay = CredentialOverlay(environ)

    overlay.apply(
        OpenedCredentials(
            {"BANCHILE_PASSWORD": "synthetic-from-ui", "BUDA_API_KEY": "synthetic-key"},
            frozenset({"SKIPO_API_KEY"}),
        )
    )
    assert environ["BANCHILE_PASSWORD"] == "synthetic-from-ui"
    assert environ["BUDA_API_KEY"] == "synthetic-key"
    status = overlay.status()
    assert status["BANCHILE_PASSWORD"] == {"fallback": True, "stored": True, "needs_reentry": False}
    assert status["FINTUAL_PASSWORD"]["fallback"] is True  # legacy FINTUAL_TOKEN
    assert status["SKIPO_API_KEY"] == {"fallback": False, "stored": False, "needs_reentry": True}
    assert all(type(v) is bool for field in status.values() for v in field.values())

    overlay.apply(OpenedCredentials({}, frozenset()))
    assert environ == {
        "BANCHILE_PASSWORD": "synthetic-from-keychain",
        "FINTUAL_TOKEN": "synthetic-legacy",
    }


def test_fingerprint_tracks_changes_only():
    env = {"A": "synthetic-1"}
    before = env_fingerprint(["A", "B"], env)
    assert env_fingerprint(["A", "B"], dict(env)) == before
    env["A"] = "synthetic-2"
    assert env_fingerprint(["A", "B"], env) != before
    assert "synthetic" not in before


# --- Manager --------------------------------------------------------------------


@pytest.fixture
def store():
    return FakeStore()


def _manager(tmp_path, store, environ, **kwargs):
    manager = CredentialManager(
        key_path=tmp_path / "credentials_key", environ=environ, ring=store, **kwargs
    )
    manager.start()
    manager.reload()
    return manager


def test_manager_creates_publishes_and_applies(tmp_path, store):
    environ = {"BANCHILE_PASSWORD": "synthetic-from-keychain"}
    manager = _manager(tmp_path, store, environ)
    assert store.activated == [manager.key.key_id]

    store.rows = [_row(manager.key, "banchile", "password", SECRET)]
    assert manager.reload()
    assert environ["BANCHILE_PASSWORD"] == SECRET

    store.rows = []
    assert manager.reload()
    assert environ["BANCHILE_PASSWORD"] == "synthetic-from-keychain"


def test_manager_keeps_the_overlay_when_the_db_is_down(tmp_path, store):
    environ: dict[str, str] = {}
    manager = _manager(tmp_path, store, environ)
    store.rows = [_row(manager.key, "skipo", "api_key", SECRET)]
    manager.reload()
    store.fail_fetch = True
    assert manager.reload() is False
    assert environ["SKIPO_API_KEY"] == SECRET


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_manager_with_a_loose_key_file_uses_env_only(tmp_path, store):
    path = tmp_path / "credentials_key"
    load_or_create_key(path)
    path.chmod(0o644)
    environ = {"SKIPO_API_KEY": "synthetic-env"}
    manager = _manager(tmp_path, store, environ)
    assert manager.key is None
    assert store.activated == []
    assert manager.status()["key_available"] is False
    assert manager.status()["state"] == "key_unavailable"
    assert manager.settled.is_set()
    assert environ == {"SKIPO_API_KEY": "synthetic-env"}


# --- Scheduler wiring and control endpoints -------------------------------------


class FakeJob:
    def __init__(self, args, trigger=None):
        self.args = args
        self.trigger = trigger

    def modify(self, **changes):
        self.args = changes.get("args", self.args)


class FakeScheduler:
    def __init__(self):
        self.jobs: dict[str, FakeJob] = {}

    def add_job(self, func, trigger, args, id, **kwargs):
        self.jobs[id] = FakeJob(args, trigger)

    def remove_job(self, job_id):
        del self.jobs[job_id]

    def get_job(self, job_id):
        return self.jobs.get(job_id)


def test_scraper_set_adds_drops_and_rebuilds(monkeypatch):
    for name in main._credential_env_vars("buda") + main._credential_env_vars("skipo"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SKIPO_API_KEY", "synthetic-key")
    built: dict[str, object] = {}

    def build():
        built.clear()
        if os.environ.get("SKIPO_API_KEY"):
            built["skipo"] = object()
        if os.environ.get("BUDA_API_KEY") and os.environ.get("BUDA_API_SECRET"):
            built["buda"] = object()
        return dict(built)

    scheduler = FakeScheduler()
    scrapers = ScraperSet(scheduler, build(), build=build)
    first_skipo = scheduler.jobs["skipo"].args[0]
    assert scrapers.keys == {"skipo"}

    assert scrapers.rebuild() == {"added": [], "removed": [], "updated": []}
    assert scheduler.jobs["skipo"].args[0] is first_skipo  # unchanged: same instance

    monkeypatch.setenv("BUDA_API_KEY", "synthetic-key")
    monkeypatch.setenv("BUDA_API_SECRET", "synthetic-secret")
    monkeypatch.setenv("SKIPO_API_KEY", "synthetic-new-key")
    assert scrapers.rebuild() == {"added": ["buda"], "removed": [], "updated": ["skipo"]}
    assert scheduler.jobs["skipo"].args[0] is not first_skipo

    monkeypatch.delenv("SKIPO_API_KEY")
    assert scrapers.rebuild() == {"added": [], "removed": ["skipo"], "updated": []}
    assert scrapers.keys == {"buda"} and set(scheduler.jobs) == {"buda"}


@pytest.mark.parametrize("first", [False, True], ids=["later rebuild", "first rebuild"])
def test_a_change_applied_mid_build_is_picked_up_by_the_next_rebuild(monkeypatch, first):
    """A concurrent apply that lands while a rebuild builds must not be
    recorded as already seen, or its own rebuild skips the update."""
    for name in main._credential_env_vars("skipo"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SKIPO_API_KEY", "synthetic-key")
    racing = {"on": False}

    def build():
        instance = object()
        if racing["on"]:
            racing["on"] = False
            monkeypatch.setenv("SKIPO_API_KEY", "synthetic-new-key")
        return {"skipo": instance}

    scheduler = FakeScheduler()
    scrapers = ScraperSet(scheduler, None if first else build(), build=build)
    racing["on"] = True
    scrapers.rebuild()
    stale = scheduler.jobs["skipo"].args[0]

    assert scrapers.rebuild()["updated"] == ["skipo"]
    assert scheduler.jobs["skipo"].args[0] is not stale


def test_tenpo_credential_change_resets_the_imap_session(monkeypatch):
    resets = []
    monkeypatch.setattr(main, "reset_email_session", lambda: resets.append(1))
    monkeypatch.setenv("EMAIL_IMAP_PASSWORD", "synthetic-old")
    scrapers = ScraperSet(FakeScheduler(), {"tenpo": object()}, build=lambda: {"tenpo": object()})
    monkeypatch.setenv("EMAIL_IMAP_PASSWORD", "synthetic-new")
    assert scrapers.rebuild()["updated"] == ["tenpo"]
    assert resets == [1]


@pytest.fixture
def control(tmp_path, store, monkeypatch):
    for name in main._credential_env_vars("skipo"):
        monkeypatch.delenv(name, raising=False)
    manager = _manager(tmp_path, store, os.environ)
    scheduler = FakeScheduler()

    def build():
        return {"skipo": object()} if os.environ.get("SKIPO_API_KEY") else {}

    scrapers = ScraperSet(scheduler, build(), build=build)
    handler = _make_control_handler(
        scheduler, scrapers.snapshot, Heartbeat(), credentials=CredentialControl(manager, scrapers)
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    yield manager, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    # The overlay wrote into the real environment; take it back off.
    manager.overlay.apply(OpenedCredentials({}, frozenset()))


def _call(url, method="GET", headers=None):
    if headers is None:
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_reload_applies_a_new_credential_and_never_echoes_it(control, store):
    manager, base = control
    store.rows = [_row(manager.key, "skipo", "api_key", SECRET)]

    code, body = _call(f"{base}/credentials/reload", "POST")
    assert code == 200
    assert SECRET not in body
    payload = json.loads(body)
    assert payload["applied"] is True
    assert payload["added"] == ["skipo"]
    assert payload["scrapers"] == ["skipo"]

    code, body = _call(f"{base}/scrapers")
    assert json.loads(body) == {"scrapers": ["skipo"]}

    code, body = _call(f"{base}/credentials/status")
    assert code == 200
    assert SECRET not in body
    status = json.loads(body)
    assert status["key_available"] is True
    assert status["key_id"] == manager.key.key_id
    assert status["fields"]["SKIPO_API_KEY"] == {
        "fallback": False,
        "stored": True,
        "needs_reentry": False,
    }

    store.rows = []
    code, body = _call(f"{base}/credentials/reload", "POST")
    assert json.loads(body)["removed"] == ["skipo"]
    assert "SKIPO_API_KEY" not in os.environ


@pytest.fixture
def rut_in_env(monkeypatch):
    monkeypatch.setenv("BANCHILE_RUT", "11.111.111-1")


def test_status_never_contains_fallback_values(rut_in_env, control):
    _, base = control
    _, body = _call(f"{base}/credentials/status")
    assert "11.111.111-1" not in body
    assert json.loads(body)["fields"]["BANCHILE_RUT"]["fallback"] is True


@pytest.mark.parametrize(
    ("method", "path", "headers", "code"),
    [
        ("GET", "/credentials/status", {"Origin": "http://evil.example"}, 403),
        ("GET", "/credentials/status", {"Sec-Fetch-Site": "cross-site"}, 403),
        ("POST", "/credentials/reload", {"Origin": "http://evil.example"}, 403),
        ("POST", "/credentials/reload", {"Content-Type": "text/plain"}, 415),
    ],
)
def test_credentials_endpoints_refuse_browser_requests(control, method, path, headers, code):
    """Like /login, the credentials routes answer only the dashboard's server."""
    _, base = control
    assert _call(f"{base}{path}", method, headers)[0] == code


def test_credentials_endpoints_404_without_a_manager():
    handler = _make_control_handler(FakeScheduler(), set(), Heartbeat())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert _call(f"{base}/credentials/status")[0] == 404
        assert _call(f"{base}/credentials/reload", "POST")[0] == 404
    finally:
        httpd.shutdown()


def test_manager_skips_the_rows_while_the_key_cannot_be_published(tmp_path, store):
    """A database that refused the key sync is down: reading the rows would
    only sit through the same retries."""
    store.fail = True
    manager = CredentialManager(key_path=tmp_path / "credentials_key", environ={}, ring=store)
    manager.start()
    assert manager.key is not None
    assert manager.reload() is False
    assert store.fetches == 0
    assert not manager.settled.is_set()


# --- Bound secrets (IMAP password -> host + user) -------------------------------

HOST, USER, IMAP_PASSWORD = "imap.gmail.com", "user@example.com", "synthetic-app-password"


def _email_rows(key, host=HOST, user=USER, sealed_to=(HOST, USER)):
    return [
        _row(key, "email", "imap_host", host),
        _row(key, "email", "imap_user", user),
        SealedCredential(
            "email",
            "imap_password",
            key.scheme,
            key.key_id,
            key.seal("email", "imap_password", IMAP_PASSWORD, binding_digest(sealed_to)),
        ),
    ]


def test_bound_secret_opens_with_its_destination(key):
    opened = open_rows(key, _email_rows(key))
    assert opened.values == {
        "EMAIL_IMAP_HOST": HOST,
        "EMAIL_IMAP_USER": USER,
        "EMAIL_IMAP_PASSWORD": IMAP_PASSWORD,
    }
    assert not opened.suppressed


def test_a_replaced_host_never_gets_the_password(key):
    """Someone with DB write access re-seals the host to their own server and
    keeps the stored password row: the password must not follow, and the
    secret store's password must not either."""
    rows = _email_rows(key, host="imap.mail.me.com")  # allowed host, not the sealed one
    fallback = {"EMAIL_IMAP_PASSWORD": "synthetic-keychain-password"}
    opened = open_rows(key, rows, fallback)
    assert "EMAIL_IMAP_PASSWORD" not in opened.values
    assert "EMAIL_IMAP_PASSWORD" in opened.suppressed
    environ = dict(fallback)
    overlay = CredentialOverlay(environ)
    overlay.apply(opened)
    assert "EMAIL_IMAP_PASSWORD" not in environ
    assert overlay.status()["EMAIL_IMAP_PASSWORD"]["needs_reentry"] is True


def test_a_stored_host_without_a_password_suppresses_the_fallback(key):
    opened = open_rows(
        key,
        [_row(key, "email", "imap_host", HOST)],
        {"EMAIL_IMAP_PASSWORD": "synthetic-keychain-password", "EMAIL_IMAP_USER": USER},
    )
    assert opened.suppressed == {"EMAIL_IMAP_PASSWORD"}


def test_fallback_destination_keeps_the_fallback_password(key):
    opened = open_rows(key, [], {"EMAIL_IMAP_HOST": HOST, "EMAIL_IMAP_PASSWORD": "synthetic-x"})
    assert not opened.suppressed


def test_unknown_imap_host_is_refused_unless_allowed(key):
    rows = _email_rows(key, host="imap.example.com", sealed_to=("imap.example.com", USER))
    refused = open_rows(key, rows, {"EMAIL_IMAP_HOST": HOST})
    assert "EMAIL_IMAP_HOST" in refused.needs_reentry
    assert "EMAIL_IMAP_HOST" not in refused.values
    # The host fell back, so the password (sealed to the refused host) is not used.
    assert "EMAIL_IMAP_PASSWORD" not in refused.values

    allowed = open_rows(key, rows, {"EMAIL_IMAP_ALLOW_CUSTOM_HOST": "1"})
    assert allowed.values["EMAIL_IMAP_HOST"] == "imap.example.com"
    assert allowed.values["EMAIL_IMAP_PASSWORD"] == IMAP_PASSWORD


@pytest.mark.parametrize("bad", ["synthetic\x85value", "synthetic\x1bvalue"])
def test_control_characters_are_refused(key, bad):
    opened = open_rows(key, [_row(key, "skipo", "api_key", bad)])
    assert opened.values == {}
    assert opened.needs_reentry == {"SKIPO_API_KEY"}


# --- Retries, republishing and read-only runs -------------------------------------


def test_tick_retries_until_published_and_loaded(tmp_path, store):
    store.fail = True
    manager = _manager(tmp_path, store, {})
    assert manager.status()["published"] is False
    assert manager.status()["state"] == "starting"
    store.rows = [_row(manager.key, "skipo", "api_key", SECRET)]
    assert manager.tick() is False  # still down
    store.fail = False
    assert manager.tick() is True  # loaded just now: the caller rebuilds
    assert manager.overlay.status()["SKIPO_API_KEY"]["stored"] is True
    assert manager.tick() is False  # nothing left to do
    assert manager.status()["state"] == "ok"


def test_readonly_never_creates_a_key(tmp_path, store):
    manager = CredentialManager(key_path=tmp_path / "credentials_key", environ={}, ring=store)
    manager.start_readonly()
    assert manager.key is None
    assert not (tmp_path / "credentials_key").exists()
    assert store.activated == []


def test_readonly_skips_rows_when_another_key_is_active(tmp_path, store):
    path = tmp_path / "credentials_key"
    key = load_or_create_key(path)
    other = CredentialKey.from_private(X25519PrivateKey.generate())
    store.activate(other)
    store.rows = [_row(key, "skipo", "api_key", SECRET)]
    environ: dict[str, str] = {}
    manager = CredentialManager(key_path=path, environ=environ, ring=store)
    manager.start_readonly()
    assert environ == {}
    assert store.activated == [other.key_id]

    store.activate(key)
    manager.start_readonly()
    assert environ == {"SKIPO_API_KEY": SECRET}


# --- Scraper state across credential changes -----------------------------------------


class FakeFintual:
    def __init__(self, folder):
        self.session_file = folder / ".fintual_session.json"
        self._login_failed_file = folder / ".fintual_session.json.login-failed"


def test_fintual_change_clears_the_login_failed_marker(tmp_path, monkeypatch):
    for name in main._credential_env_vars("fintual"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FINTUAL_EMAIL", "user@example.com")
    monkeypatch.setenv("FINTUAL_PASSWORD", "synthetic-old")
    scrapers = ScraperSet(
        FakeScheduler(),
        {"fintual": FakeFintual(tmp_path)},
        build=lambda: {"fintual": FakeFintual(tmp_path)},
    )
    fake = FakeFintual(tmp_path)
    fake._login_failed_file.write_text("x")
    monkeypatch.setenv("FINTUAL_PASSWORD", "synthetic-new")
    assert scrapers.rebuild()["updated"] == ["fintual"]
    assert not fake._login_failed_file.exists()


class TestFintualKeepaliveJob:
    @pytest.fixture(autouse=True)
    def _fintual_env(self, monkeypatch):
        for name in main._credential_env_vars("fintual"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("FINTUAL_KEEPALIVE_HOURS", raising=False)
        monkeypatch.setenv("FINTUAL_EMAIL", "user@example.com")
        monkeypatch.setenv("FINTUAL_PASSWORD", "synthetic-password")

    def _scheduler(self, tmp_path):
        scheduler = FakeScheduler()
        ScraperSet(
            scheduler,
            {"fintual": FakeFintual(tmp_path)},
            build=lambda: {"fintual": FakeFintual(tmp_path)},
        )
        return scheduler

    @pytest.mark.parametrize(
        ("env", "hours"), [(None, 2), ("3", 3), ("0.5", 0.5), ("x", 2), ("0.1", 0.25)]
    )
    def test_it_runs_at_the_env_interval(self, monkeypatch, tmp_path, env, hours):
        if env is not None:
            monkeypatch.setenv("FINTUAL_KEEPALIVE_HOURS", env)

        scheduler = self._scheduler(tmp_path)

        job = scheduler.jobs[main.FINTUAL_KEEPALIVE_JOB]
        assert job.trigger.interval == timedelta(hours=hours)
        assert job.args == [scheduler]

    @pytest.mark.parametrize("env", ["x", "inf", "nan"])
    def test_a_non_number_warns_and_uses_the_default(self, monkeypatch, caplog, env):
        monkeypatch.setenv("FINTUAL_KEEPALIVE_HOURS", env)

        hours = main.fintual_keepalive_hours()

        assert hours == 2
        assert "FINTUAL_KEEPALIVE_HOURS is not a number; using 2.0" in caplog.text

    @pytest.mark.parametrize("env", ["0", "-1"])
    def test_zero_or_less_disables_it(self, monkeypatch, tmp_path, env):
        monkeypatch.setenv("FINTUAL_KEEPALIVE_HOURS", env)

        scheduler = self._scheduler(tmp_path)

        assert set(scheduler.jobs) == {"fintual"}

    def test_it_is_not_scheduled_without_fintual(self):
        scheduler = FakeScheduler()

        ScraperSet(scheduler, {"skipo": object()}, build=lambda: {"skipo": object()})

        assert main.FINTUAL_KEEPALIVE_JOB not in scheduler.jobs

    def test_it_leaves_with_fintual(self, monkeypatch, tmp_path):
        scheduler = FakeScheduler()
        scrapers = ScraperSet(
            scheduler,
            {"fintual": FakeFintual(tmp_path)},
            build=lambda: (
                {"fintual": FakeFintual(tmp_path)} if os.environ.get("FINTUAL_EMAIL") else {}
            ),
        )
        monkeypatch.delenv("FINTUAL_EMAIL")

        scrapers.rebuild()

        assert scheduler.jobs == {}

    def test_the_job_keeps_the_scheduled_instance_alive(self):
        scheduler = FakeScheduler()
        scraper = SimpleNamespace(keepalive=AsyncMock())
        scheduler.add_job(None, None, [scraper], "fintual")

        asyncio.run(main._fintual_keepalive(scheduler))

        scraper.keepalive.assert_awaited_once()

    @pytest.mark.parametrize("holder", ["run", "login"])
    def test_the_job_skips_while_fintual_is_busy(self, holder):
        scheduler = FakeScheduler()
        scraper = SimpleNamespace(keepalive=AsyncMock())
        scheduler.add_job(None, None, [scraper], "fintual")
        assert main._RUN_GUARD.claim("fintual", holder)

        try:
            asyncio.run(main._fintual_keepalive(scheduler))
        finally:
            main._RUN_GUARD.release("fintual")

        scraper.keepalive.assert_not_awaited()


def test_status_never_waits_on_the_database(tmp_path, store):
    """A reload stuck in DB retries must not block GET /credentials/status."""
    import threading

    manager = _manager(tmp_path, store, {})
    release = threading.Event()
    entered = threading.Event()

    def slow_fetch():
        entered.set()
        release.wait(5)
        return []

    store.fetch_rows = slow_fetch
    worker = threading.Thread(target=manager.reload)
    worker.start()
    assert entered.wait(2)
    done = []
    reader = threading.Thread(target=lambda: done.append(manager.status()))
    reader.start()
    reader.join(1)
    release.set()
    worker.join(5)
    assert done, "status() blocked behind the database"


def test_refresh_never_triggers_internal_jobs():
    scheduler = FakeScheduler()
    scheduler.add_job(None, None, [], "credentials")
    scheduler.add_job(None, None, [], "heartbeat")
    handler = _make_control_handler(scheduler, frozenset(), Heartbeat())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert _call(f"{base}/refresh/credentials", "POST")[0] == 404
        assert _call(f"{base}/refresh/heartbeat", "POST")[0] == 404
    finally:
        httpd.shutdown()


def test_snapshot_is_immutable_and_swapped(monkeypatch):
    monkeypatch.setenv("SKIPO_API_KEY", "synthetic-key")
    present = {"skipo": object()}
    scrapers = ScraperSet(FakeScheduler(), dict(present), build=lambda: dict(present))
    before = scrapers.snapshot()
    assert isinstance(before, frozenset)
    present.clear()
    scrapers.rebuild()
    assert before == {"skipo"} and scrapers.snapshot() == frozenset()
