"""The service side of the credentials key ring, on an in-memory ring: one key
per database, rotation and its crash recovery, the reset command, and a
start that never waits on the database. The SQL is covered by
test_credentials_db.py.

Every value is synthetic and every key is generated per test.
"""

import json
from datetime import timedelta

import credentials_admin
import pytest
from credential_manager import (
    CredentialManager,
    RotationError,
    RotationPending,
    max_age_days_from_env,
    rotate_key_file,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from scrapers.credentials import (
    CredentialKey,
    KeyFile,
    load_key,
    open_rows,
)
from tests.fakes_credentials import FakeRing
from tests.test_credentials import SECRET, _row


@pytest.fixture
def store():
    return FakeRing()


def _manager(tmp_path, store, environ, **kwargs):
    manager = CredentialManager(
        key_path=tmp_path / "credentials_key", environ=environ, ring=store, **kwargs
    )
    manager.start()
    manager.reload()
    return manager


def _other_key() -> CredentialKey:
    return CredentialKey.from_private(X25519PrivateKey.generate())


def test_a_service_never_replaces_a_key_that_owns_rows(tmp_path, store):
    other = _other_key()
    store.activate(other)
    store.rows = [_row(other, "skipo", "api_key", SECRET)]
    environ = {"SKIPO_API_KEY": "synthetic-env"}
    manager = _manager(tmp_path, store, environ)
    assert store.activated == [other.key_id]
    status = manager.status()
    assert status["state"] == "key_mismatch"
    assert status["key_mismatch"] is True
    assert status["active_key_id"] == other.key_id
    assert manager.settled.is_set()
    assert environ == {"SKIPO_API_KEY": "synthetic-env"}


def test_a_service_takes_over_a_key_with_no_rows_only_before_publishing(tmp_path, store):
    other = _other_key()
    store.activate(other)
    manager = _manager(tmp_path, store, {})
    assert store.active.key_id == manager.key.key_id
    assert manager.status()["state"] == "ok"
    # Displaced later (another service reset the key): no taking it back.
    store.activate(other)
    manager.tick()
    assert manager.status()["state"] == "key_mismatch"
    assert store.active.key_id == other.key_id


def test_a_displaced_service_drops_the_values_it_opened(tmp_path, store):
    environ: dict[str, str] = {}
    manager = _manager(tmp_path, store, environ)
    store.rows = [_row(manager.key, "skipo", "api_key", SECRET)]
    manager.reload()
    assert environ["SKIPO_API_KEY"] == SECRET
    store.reset(_other_key())
    assert manager.tick() is True
    assert "SKIPO_API_KEY" not in environ


def test_rotation_reseals_and_swaps_the_key_file(tmp_path, store):
    manager = _manager(tmp_path, store, {})
    old = manager.key
    store.rows = [_row(old, "skipo", "api_key", SECRET)]
    manager.reload()
    result = rotate_key_file(manager.key_file, store)
    assert (result.resealed, result.skipped) == (1, 0)
    assert load_key(tmp_path / "credentials_key").key_id == result.new_key.key_id
    assert not manager.key_file.next_path.exists()
    assert open_rows(result.new_key, store.rows).values == {"SKIPO_API_KEY": SECRET}
    # The running service adopts the rotated key file on its next sync.
    manager.tick()
    assert manager.key.key_id == result.new_key.key_id
    assert manager.status()["state"] == "ok"


def test_rotation_with_the_database_down_leaves_everything_as_it_was(tmp_path, store):
    manager = _manager(tmp_path, store, {})
    old_id = manager.key.key_id
    store.fail = True
    with pytest.raises(RotationError):
        rotate_key_file(manager.key_file, store)
    assert load_key(tmp_path / "credentials_key").key_id == old_id
    assert not manager.key_file.next_path.exists()


def test_an_unconfirmed_commit_stays_pending_until_the_database_answers(tmp_path, store):
    manager = _manager(tmp_path, store, {})
    old = manager.key
    store.rows = [_row(old, "skipo", "api_key", SECRET)]
    manager.reload()
    real_rotate = store.rotate

    def rotate_then_drop(*args):
        real_rotate(*args)
        store.fail = True
        raise ConnectionError("connection lost during commit")

    store.rotate = rotate_then_drop
    with pytest.raises(RotationPending):
        rotate_key_file(manager.key_file, store)
    # Both keys are still on disk: nothing is unopenable.
    assert manager.key_file.next_path.exists()
    assert load_key(tmp_path / "credentials_key").key_id == old.key_id
    store.fail = False
    manager.tick()
    assert manager.key.key_id == store.active.key_id != old.key_id
    assert not manager.key_file.next_path.exists()
    assert manager.overlay.status()["SKIPO_API_KEY"]["stored"] is True


def test_rotation_is_due_by_age_and_retried_after_a_failure(tmp_path, store):
    now = [store.now]
    manager = _manager(tmp_path, store, {}, max_age_days=90, now=lambda: now[0])
    store.rows = [_row(manager.key, "skipo", "api_key", SECRET)]
    manager.reload()
    old_id = manager.key.key_id
    manager.tick()
    assert manager.key.key_id == old_id

    def db_down(*args):
        raise ConnectionError("db down")

    now[0] = store.now + timedelta(days=91)
    store.rotate = db_down
    manager.tick()
    assert manager.key.key_id == old_id
    assert store.failures and store.failures[0][0] == old_id
    status = manager.status()
    assert status["rotation_error"] and status["rotation_failed_at"]
    assert SECRET not in json.dumps(status)

    del store.rotate  # back to the working one
    manager.tick()
    assert manager.key.key_id == old_id  # waits before retrying
    manager._next_rotation_attempt = 0.0
    manager.tick()
    assert manager.key.key_id != old_id
    assert manager.status()["rotation_error"] is None


def test_zero_max_age_never_rotates(tmp_path, store):
    manager = _manager(
        tmp_path, store, {}, max_age_days=0, now=lambda: store.now + timedelta(days=3650)
    )
    old_id = manager.key.key_id
    manager.tick()
    assert manager.key.key_id == old_id


@pytest.mark.parametrize(
    ("raw", "days"), [(None, 90), ("", 90), ("30", 30), ("0", 0), ("-1", 90), ("x", 90)]
)
def test_max_age_from_env(raw, days):
    environ = {} if raw is None else {"CREDENTIALS_KEY_MAX_AGE_DAYS": raw}
    assert max_age_days_from_env(environ) == days


def test_reset_needs_the_typed_confirmation(tmp_path, store, monkeypatch):
    other = _other_key()
    store.activate(other)
    key_file = KeyFile(tmp_path / "credentials_key")
    monkeypatch.setattr(credentials_admin, "_poke_service", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert credentials_admin._reset(key_file, store, assume_yes=False) == 1
    assert store.active.key_id == other.key_id
    monkeypatch.setattr("builtins.input", lambda prompt: "RESET")
    assert credentials_admin._reset(key_file, store, assume_yes=False) == 0
    assert store.active.key_id == key_file.load().key_id


def test_reset_refuses_without_a_terminal(tmp_path, store, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert credentials_admin._reset(KeyFile(tmp_path / "k"), store, assume_yes=False) == 2
    assert store.activated == []
