"""Sealed credentials: the HPKE scheme, the key file, the env overlay, the
service wiring and the control endpoints.

Every value is synthetic (`synthetic-*`, user@example.com, 11.111.111-1), and
every key is generated per test or is the throwaway fixture key, which
protects nothing.
"""

import base64
import json
import os
import stat
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from credential_manager import CredentialManager
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

import main
from main import CredentialControl, Heartbeat, ScraperSet, _make_control_handler
from scrapers.credentials import (
    SCHEME,
    CredentialKey,
    CredentialKeyError,
    CredentialOpenError,
    CredentialOverlay,
    OpenedCredentials,
    SealedCredential,
    env_fingerprint,
    load_key,
    load_or_create_key,
    open_rows,
    sealing_info,
)

FIXTURE = Path(__file__).parent / "fixtures" / "credentials" / "ts_sealed.json"
SECRET = "synthetic-password-1234"


@pytest.fixture
def key() -> CredentialKey:
    return CredentialKey.from_private(X25519PrivateKey.generate())


def _row(key: CredentialKey, slug: str, field: str, value: str) -> SealedCredential:
    return SealedCredential(slug, field, SCHEME, key.key_id, key.seal(slug, field, value))


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
        sealing_info(slug, field, key_id)


def test_dashboard_fixture_opens():
    """A ciphertext sealed by the dashboard's TypeScript code (with a
    throwaway key) opens here, and only in its own context. Regenerate with
    `WRITE_CREDENTIALS_FIXTURE=1 corepack pnpm --filter @chanchito/web test
    credentials-seal`."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scalar = bytes.fromhex(fixture["recipient_scalar_hex"])
    key = CredentialKey.from_private(X25519PrivateKey.from_private_bytes(scalar))
    assert key.key_id == fixture["recipient_fingerprint"]
    assert fixture["scheme"] == SCHEME
    for entry in fixture["entries"]:
        sealed = base64.b64decode(entry["sealed"])
        assert key.open(entry["slug"], entry["field"], key.key_id, sealed) == entry["value"]
        with pytest.raises(CredentialOpenError):
            key.open(entry["slug"], "other_field", key.key_id, sealed)


# --- Key file -------------------------------------------------------------------


def test_creates_an_owner_only_key(tmp_path):
    path = tmp_path / "state" / "credentials_key"
    key = load_or_create_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_key(path).key_id == key.key_id
    assert list(path.parent.iterdir()) == [path]  # no temp file left behind


def test_refuses_a_key_readable_by_others(tmp_path):
    path = tmp_path / "credentials_key"
    load_or_create_key(path)
    path.chmod(0o640)
    with pytest.raises(CredentialKeyError, match="chmod 600"):
        load_or_create_key(path)


def test_refuses_a_symlinked_key(tmp_path):
    real = tmp_path / "real_key"
    load_or_create_key(real)
    link = tmp_path / "credentials_key"
    link.symlink_to(real)
    with pytest.raises(CredentialKeyError):
        load_key(link)


def test_refuses_a_non_x25519_key(tmp_path):
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
        SealedCredential("santander", "rut", SCHEME, key.key_id, swapped),
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


class FakeStore:
    """Rows and published keys in memory, in place of the database."""

    def __init__(self):
        self.rows: list[SealedCredential] = []
        self.published: list[str] = []
        self.fail_fetch = False

    def fetch(self):
        if self.fail_fetch:
            raise ConnectionError("db down")
        return list(self.rows)

    def publish(self, key):
        self.published.append(key.key_id)
        return None, 0


@pytest.fixture
def store():
    return FakeStore()


def _manager(tmp_path, store, environ):
    manager = CredentialManager(
        key_path=tmp_path / "credentials_key",
        environ=environ,
        fetch=store.fetch,
        publish=store.publish,
    )
    manager.start()
    return manager


def test_manager_creates_publishes_and_applies(tmp_path, store):
    environ = {"BANCHILE_PASSWORD": "synthetic-from-keychain"}
    manager = _manager(tmp_path, store, environ)
    assert store.published == [manager.key.key_id]

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


def test_manager_with_a_loose_key_file_uses_env_only(tmp_path, store):
    path = tmp_path / "credentials_key"
    load_or_create_key(path)
    path.chmod(0o644)
    environ = {"SKIPO_API_KEY": "synthetic-env"}
    manager = _manager(tmp_path, store, environ)
    assert manager.key is None
    assert store.published == []
    assert manager.status()["key_available"] is False
    assert environ == {"SKIPO_API_KEY": "synthetic-env"}


# --- Scheduler wiring and control endpoints -------------------------------------


class FakeJob:
    def __init__(self, args):
        self.args = args

    def modify(self, **changes):
        self.args = changes.get("args", self.args)


class FakeScheduler:
    def __init__(self):
        self.jobs: dict[str, FakeJob] = {}

    def add_job(self, func, trigger, args, id, **kwargs):
        self.jobs[id] = FakeJob(args)

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
        scheduler, scrapers.keys, Heartbeat(), credentials=CredentialControl(manager, scrapers)
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    yield manager, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    # The overlay wrote into the real environment; take it back off.
    manager.overlay.apply(OpenedCredentials({}, frozenset()))


def _call(url, method="GET"):
    req = urllib.request.Request(url, method=method)
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
    """A database that refused the key is down: reading the rows would only
    sit through the same retries."""
    fetched = []

    def publish(key):
        raise ConnectionError("db down")

    manager = CredentialManager(
        key_path=tmp_path / "credentials_key",
        environ={},
        fetch=lambda: fetched.append(1) or [],
        publish=publish,
    )
    manager.start()
    assert manager.key is not None
    assert manager.reload() is False
    assert fetched == []
