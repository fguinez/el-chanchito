"""Review follow-ups for the credentials key ring: a leftover pending key is
deleted only when the database positively rules it out, stale key temp files
are swept, rotation needs a real file lock, rotation failures never carry a
path, and no scraper is built before the stored credentials are applied.

The DB cases run against the throwaway PostgreSQL (TEST_DATABASE_URL). Every
key is generated per test and every value is synthetic.
"""

import os
import threading
import time

import credentials_admin
import psycopg
import pytest
from credential_manager import (
    CredentialManager,
    DatabaseKeyRing,
    PendingUnresolved,
    RotationError,
    recover_pending,
    rotate_key_file,
)

from db.credentials import sync_key
from main import CredentialControl, ScraperSet
from scrapers.credentials import ALGORITHM, KeyFile, OpenedCredentials, generate_key, load_key
from tests.fakes_credentials import FakeRing
from tests.test_credentials import SECRET, FakeScheduler, _row


@pytest.fixture
def store():
    return FakeRing()


def _manager(tmp_path, store, environ=None, **kwargs):
    manager = CredentialManager(
        key_path=tmp_path / "credentials_key",
        environ={} if environ is None else environ,
        ring=store,
        **kwargs,
    )
    manager.start()
    return manager


# --- Pending keys are deleted only when positively ruled out --------------------


def test_a_pending_key_is_discarded_only_when_the_current_key_is_active(tmp_path, store):
    key_file = KeyFile(tmp_path / "credentials_key")
    current = key_file.load_or_create()
    store.activate(current)
    key_file.write_pending(generate_key())
    assert recover_pending(key_file, store) == "discarded"
    assert not key_file.next_path.exists()


def test_a_pending_key_another_database_cannot_judge_is_kept(tmp_path, store):
    """Wrong DATABASE_URL: the pending key is not in this ring, but neither is
    the key file's key active here, so nothing proves the rotation failed."""
    key_file = KeyFile(tmp_path / "credentials_key")
    key_file.load_or_create()
    pending = generate_key()
    key_file.write_pending(pending)
    store.activate(generate_key())
    with pytest.raises(PendingUnresolved):
        recover_pending(key_file, store)
    assert load_key(key_file.next_path).key_id == pending.key_id


def test_the_service_keeps_an_unresolved_pending_key_and_settles(tmp_path, store):
    key_file = KeyFile(tmp_path / "credentials_key")
    key_file.load_or_create()
    key_file.write_pending(generate_key())
    store.activate(generate_key())
    manager = _manager(tmp_path, store)
    assert manager.reload() is False
    assert key_file.next_path.exists()
    # Env-only scrapes may go on; nothing was deleted.
    assert manager.settled.is_set()


def test_stale_key_temp_files_are_swept(tmp_path, store):
    key_file = KeyFile(tmp_path / "credentials_key")
    store.activate(key_file.load_or_create())
    next_tmp = tmp_path / "credentials_key.next.0123456789abcdef.tmp"
    old_tmp = tmp_path / "credentials_key.fedcba9876543210.tmp"
    fresh_tmp = tmp_path / "credentials_key.aaaaaaaaaaaaaaaa.tmp"
    for path in (next_tmp, old_tmp, fresh_tmp):
        path.write_text("synthetic")
    long_ago = time.time() - 3600
    os.utime(old_tmp, (long_ago, long_ago))
    recover_pending(key_file, store)
    assert not next_tmp.exists()
    assert not old_tmp.exists()
    # A first start may be writing this one right now.
    assert fresh_tmp.exists()


# --- File locking ---------------------------------------------------------------


def test_rotation_and_reset_refuse_without_a_real_lock(tmp_path, store, monkeypatch):
    monkeypatch.setattr(KeyFile, "locking_available", property(lambda self: False))
    manager = _manager(tmp_path, store, max_age_days=1)
    manager.reload()
    with pytest.raises(RotationError, match="file locking"):
        rotate_key_file(manager.key_file, store)
    from credential_manager import reset_key_file

    with pytest.raises(RotationError, match="file locking"):
        reset_key_file(manager.key_file, store)
    assert manager._rotation_due() is False


# --- Rotation failures never carry the key file path ---------------------------


def test_rotation_failure_reasons_never_name_the_key_file(tmp_path, store):
    manager = _manager(tmp_path, store)
    manager.reload()
    store.activate(generate_key())  # the key file is no longer the active key
    with pytest.raises(RotationError) as excinfo:
        rotate_key_file(manager.key_file, store)
    assert str(tmp_path) not in str(excinfo.value)
    manager.key_file.path.unlink()
    with pytest.raises(RotationError) as excinfo:
        rotate_key_file(manager.key_file, store)
    assert str(tmp_path) not in str(excinfo.value)


# --- Reset prompt -------------------------------------------------------------------


def test_reset_prompt_shows_both_keys_and_the_target(tmp_path, store, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:synthetic-pw@db.example:6543/finance")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    active = generate_key()
    store.activate(active)
    key_file = KeyFile(tmp_path / "credentials_key")
    local = key_file.load_or_create()
    assert credentials_admin._reset(key_file, store, assume_yes=False) == 1
    out = capsys.readouterr().out
    assert active.key_id[:12] in out
    assert local.key_id[:12] in out
    assert "db.example:6543/finance" in out
    assert "synthetic-pw" not in out


# --- No scraper before the stored credentials are applied -----------------------


def test_reload_with_the_database_down_builds_no_scraper(tmp_path, store, monkeypatch):
    """POST /credentials/reload before the first sync must not build env-only
    scrapers that /refresh could then run with replaced credentials."""
    monkeypatch.setenv("SKIPO_API_KEY", "synthetic-env-key")
    store.fail = True
    manager = _manager(tmp_path, store, environ=os.environ)
    scheduler = FakeScheduler()
    scrapers = ScraperSet(
        scheduler,
        None,
        build=lambda: {"skipo": object()} if os.environ.get("SKIPO_API_KEY") else {},
    )
    control = CredentialControl(manager, scrapers)
    result = control.reload()
    assert result["applied"] is False
    assert result["added"] == [] and result["scrapers"] == []
    assert scheduler.jobs == {}
    control.tick()
    assert scheduler.jobs == {}

    store.fail = False
    store.rows = [_row(manager.key, "skipo", "api_key", SECRET)]
    try:
        assert control.reload()["added"] == ["skipo"]
        assert os.environ["SKIPO_API_KEY"] == SECRET
    finally:
        manager.overlay.apply(OpenedCredentials({}, frozenset()))


# --- Against PostgreSQL ------------------------------------------------------------


def test_recovery_waits_for_a_rotation_commit_in_flight(db_conn, tmp_path):
    """The ambiguous-commit case: recovery takes the ring's advisory lock, so
    it reads only after a rotation's transaction ends, sees the new key and
    promotes it instead of deleting it."""
    key_file = KeyFile(tmp_path / "credentials_key")
    old = key_file.load_or_create()
    sync_key(old, allow_takeover=False)
    new = generate_key()
    key_file.write_pending(new)
    rotation = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        rotation.execute("SELECT pg_advisory_xact_lock(hashtext('el-chanchito/credential_keys'))")
        rotation.execute(
            "UPDATE credential_keys SET status = 'retired', retired_at = now() WHERE key_id = %s",
            (old.key_id,),
        )
        rotation.execute(
            "INSERT INTO credential_keys (key_id, algorithm, public_key, status)"
            " VALUES (%s, %s, %s, 'active')",
            (new.key_id, ALGORITHM, new.public_raw),
        )
        outcome = []
        recovery = threading.Thread(
            target=lambda: outcome.append(recover_pending(key_file, DatabaseKeyRing()))
        )
        recovery.start()
        time.sleep(0.5)
        assert recovery.is_alive(), "recovery did not wait for the ring lock"
        rotation.commit()
        recovery.join(10)
    finally:
        rotation.close()
    assert outcome == ["promoted"]
    assert key_file.load().key_id == new.key_id


def test_recovery_against_another_database_keeps_the_pending_key(db_conn, tmp_path):
    """A crash-after-commit `.next` checked against the wrong database (where
    neither key is known) is kept, never deleted."""
    key_file = KeyFile(tmp_path / "credentials_key")
    key_file.load_or_create()
    pending = generate_key()
    key_file.write_pending(pending)
    sync_key(generate_key(), allow_takeover=False)  # some other key is active here
    with pytest.raises(PendingUnresolved):
        recover_pending(key_file, DatabaseKeyRing())
    assert load_key(key_file.next_path).key_id == pending.key_id

    # An empty ring cannot rule it out either.
    db_conn.execute("DELETE FROM credential_keys")
    with pytest.raises(PendingUnresolved):
        recover_pending(key_file, DatabaseKeyRing())
    assert key_file.next_path.exists()
