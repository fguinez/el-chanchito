"""The sealed-credentials SQL (V048-V050) against a real PostgreSQL: the key
ring migration, one key per database, rotation (crash recovery and concurrent
dashboard saves included) and the reset command.

Skipped unless TEST_DATABASE_URL is set (see conftest.py). Values and keys
are synthetic and generated per test.
"""

import os
import threading
import time
from datetime import timedelta

import psycopg
import pytest
from credential_manager import (
    CredentialManager,
    DatabaseKeyRing,
    RotationError,
    reset_key_file,
    rotate_key_file,
)

from db import credentials as db_credentials
from db.credentials import KeyRingConflict, fetch_sealed_credentials, sync_key
from scrapers.credentials import (
    CredentialKey,
    KeyFile,
    binding_digest,
    generate_key,
    open_rows,
    reseal_rows,
)
from tests.conftest import MIGRATIONS_DIR, apply_migrations
from tests.fakes_credentials import make_v1_key

HOST, USER = "imap.gmail.com", "user@example.com"


def _insert(conn, key: CredentialKey, slug: str, field: str, value: str, binding=None) -> None:
    conn.execute(
        "INSERT INTO institution_credentials (institution_slug, field_key, scheme, key_id, sealed)"
        " VALUES (%s, %s, %s, %s, %s)",
        (slug, field, key.scheme, key.key_id, key.seal(slug, field, value, binding)),
    )


def _web_save(conn, key: CredentialKey, slug: str, field: str, value: str) -> bool:
    """What the dashboard's upsertSealedFields does: write only while the key
    it sealed to is still the active one, holding that row FOR SHARE."""
    with conn.transaction():
        row = conn.execute(
            "SELECT key_id FROM credential_keys WHERE key_id = %s AND status = 'active' FOR SHARE",
            (key.key_id,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "INSERT INTO institution_credentials"
            " (institution_slug, field_key, scheme, key_id, sealed)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (institution_slug, field_key) DO UPDATE"
            " SET key_id = excluded.key_id, sealed = excluded.sealed, updated_at = now()",
            (slug, field, key.scheme, key.key_id, key.seal(slug, field, value)),
        )
    return True


def _active(conn) -> str | None:
    row = conn.execute("SELECT key_id FROM credential_keys WHERE status = 'active'").fetchone()
    return row["key_id"] if row else None


def _wait_for_lock_waiter(conn, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ).fetchone()
        if row["n"]:
            return
        time.sleep(0.05)
    raise AssertionError("nobody waited on a lock")


def _manager(tmp_path, name="credentials_key", environ=None, **kwargs) -> CredentialManager:
    manager = CredentialManager(
        key_path=tmp_path / name, environ={} if environ is None else environ, **kwargs
    )
    manager.start()
    return manager


# --- Migration ------------------------------------------------------------------


def test_v049_turns_the_singleton_into_the_active_key(unmigrated_db):
    apply_migrations(unmigrated_db, before="V049")
    key = make_v1_key()  # V048 only takes 32-byte public keys
    with psycopg.connect(unmigrated_db) as conn:
        conn.execute(
            "INSERT INTO credential_keys (id, key_id, algorithm, public_key, updated_at)"
            " VALUES (1, %s, %s, %s, '2026-01-02T03:04:05Z')",
            (key.key_id, key.algorithm, key.public_raw),
        )
        _insert(conn, key, "skipo", "api_key", "synthetic-key")
    apply_migrations(unmigrated_db)
    migration = next(MIGRATIONS_DIR.glob("V049__*.sql"))
    with psycopg.connect(unmigrated_db) as conn:
        # Idempotent: a second run changes nothing.
        conn.execute(migration.read_text(encoding="utf-8"))
        rows = conn.execute(
            "SELECT key_id, status, created_at::text, retired_at FROM credential_keys"
        ).fetchall()
        assert rows == [(key.key_id, "active", "2026-01-02 03:04:05+00", None)]
        columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'credential_keys'"
            )
        }
        assert "id" not in columns and "updated_at" not in columns
        (n,) = conn.execute("SELECT count(*) FROM institution_credentials").fetchone()
        assert n == 1


def test_v050_ties_the_public_key_length_to_the_algorithm(unmigrated_db):
    """A v1 key published before V050 survives it; afterwards each algorithm
    takes only its own public key length, and no other algorithm is taken."""
    apply_migrations(unmigrated_db, before="V050")
    v1, v2 = make_v1_key(), generate_key()
    with psycopg.connect(unmigrated_db) as conn:
        conn.execute(
            "INSERT INTO credential_keys (key_id, algorithm, public_key, status)"
            " VALUES (%s, %s, %s, 'active')",
            (v1.key_id, v1.algorithm, v1.public_raw),
        )
    apply_migrations(unmigrated_db)
    migration = next(MIGRATIONS_DIR.glob("V050__*.sql"))
    insert = (
        "INSERT INTO credential_keys (key_id, algorithm, public_key, status, retired_at)"
        " VALUES (%s, %s, %s, 'retired', now())"
    )
    with psycopg.connect(unmigrated_db, autocommit=True) as conn:
        conn.execute(migration.read_text(encoding="utf-8"))  # idempotent
        conn.execute(insert, (v2.key_id, v2.algorithm, v2.public_raw))
        for algorithm, public in [
            (v2.algorithm, v1.public_raw),
            (v1.algorithm, v2.public_raw),
            ("HPKE-Base(something-else)", v1.public_raw),
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, ("e" * 64, algorithm, public))
        (n,) = conn.execute("SELECT count(*) FROM credential_keys").fetchone()
        assert n == 2


def test_at_most_one_key_is_active(db_conn):
    first, second = generate_key(), generate_key()
    sync_key(first, allow_takeover=False)
    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute(
            "INSERT INTO credential_keys (key_id, algorithm, public_key, status)"
            " VALUES (%s, %s, %s, 'active')",
            (second.key_id, second.algorithm, second.public_raw),
        )


def test_rows_are_written_only_under_the_active_key(db_conn):
    first, second = generate_key(), generate_key()
    sync_key(first, allow_takeover=False)
    _insert(db_conn, first, "skipo", "api_key", "synthetic-key")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(db_conn, second, "buda", "api_key", "synthetic-key")
    db_credentials.reset_active_key(second)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(db_conn, first, "buda", "api_key", "synthetic-key")
    _insert(db_conn, second, "buda", "api_key", "synthetic-key")


@pytest.mark.parametrize(
    ("slug", "key_id", "sealed"),
    [
        ("Bad Slug", None, b"\x00" * 64),
        ("banchile", "not-hex", b"\x00" * 64),
        ("banchile", None, b"\x00" * 8),
    ],
)
def test_table_rejects_malformed_rows(db_conn, slug, key_id, sealed):
    key = generate_key()
    sync_key(key, allow_takeover=False)
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO institution_credentials"
            " (institution_slug, field_key, scheme, key_id, sealed)"
            " VALUES (%s, 'password', %s, %s, %s)",
            (slug, key.scheme, key_id or key.key_id, sealed),
        )


def test_rows_round_trip_through_the_table(db_conn):
    key = generate_key()
    sync_key(key, allow_takeover=False)
    _insert(db_conn, key, "banchile", "password", "synthetic-password")
    opened = open_rows(key, fetch_sealed_credentials())
    assert opened.values == {"BANCHILE_PASSWORD": "synthetic-password"}


# --- One key per database ---------------------------------------------------------


def test_sync_publishes_then_never_replaces_a_key_that_owns_rows(db_conn):
    first, second = generate_key(), generate_key()
    assert sync_key(first, allow_takeover=True).outcome == "published"
    assert sync_key(first, allow_takeover=True).outcome == "current"
    _insert(db_conn, first, "skipo", "api_key", "synthetic-key")
    result = sync_key(second, allow_takeover=True)
    assert result.outcome == "mismatch"
    assert result.active.key_id == first.key_id
    assert _active(db_conn) == first.key_id


def test_sync_takes_over_a_key_no_row_is_sealed_to(db_conn):
    first, second = generate_key(), generate_key()
    sync_key(first, allow_takeover=True)
    assert sync_key(second, allow_takeover=False).outcome == "mismatch"
    assert sync_key(second, allow_takeover=True).outcome == "took_over"
    assert _active(db_conn) == second.key_id
    status = db_conn.execute(
        "SELECT status FROM credential_keys WHERE key_id = %s", (first.key_id,)
    ).fetchone()
    assert status["status"] == "retired"


def test_two_services_on_one_database_do_not_replace_each_other(db_conn, tmp_path):
    """`make dev` and `make up` with their own key files: the second one
    reports key_mismatch instead of orphaning the first one's credentials."""
    dev = _manager(tmp_path, "dev_key")
    assert dev.reload() is True
    _insert(db_conn, dev.key, "skipo", "api_key", "synthetic-key")
    assert dev.reload() is True
    assert dev.overlay.status()["SKIPO_API_KEY"]["stored"] is True

    up = _manager(tmp_path, "up_key")
    assert up.reload() is False
    status = up.status()
    assert status["state"] == "key_mismatch"
    assert status["key_mismatch"] is True
    assert status["active_key_id"] == dev.key.key_id
    assert up.settled.is_set()
    assert _active(db_conn) == dev.key.key_id

    dev.tick()
    assert dev.status()["state"] == "ok"


def test_reset_key_retires_the_lost_key_and_its_rows_need_reentry(db_conn, tmp_path):
    dev = _manager(tmp_path, "dev_key")
    dev.reload()
    _insert(db_conn, dev.key, "skipo", "api_key", "synthetic-key")

    key, retired, orphaned = reset_key_file(KeyFile(tmp_path / "up_key"), DatabaseKeyRing())
    assert (retired, orphaned) == (dev.key.key_id, 1)
    assert _active(db_conn) == key.key_id

    up = _manager(tmp_path, "up_key")
    assert up.reload() is True
    assert up.overlay.status()["SKIPO_API_KEY"]["needs_reentry"] is True
    # The displaced service stops opening and saving; it never takes back over.
    dev.tick()
    assert dev.status()["state"] == "key_mismatch"
    assert _active(db_conn) == key.key_id


# --- Rotation ---------------------------------------------------------------------


def test_rotation_reseals_every_row_and_shreds_the_old_key(db_conn, tmp_path):
    manager = _manager(tmp_path)
    manager.reload()
    old = manager.key
    _insert(db_conn, old, "banchile", "password", "synthetic-password")
    _insert(db_conn, old, "email", "imap_host", HOST)
    _insert(db_conn, old, "email", "imap_user", USER)
    _insert(
        db_conn,
        old,
        "email",
        "imap_password",
        "synthetic-app-password",
        binding_digest([HOST, USER]),
    )
    before = {
        r["field_key"]: bytes(r["sealed"])
        for r in db_conn.execute("SELECT field_key, sealed FROM institution_credentials")
    }

    result = rotate_key_file(manager.key_file, DatabaseKeyRing())

    assert (result.resealed, result.skipped) == (4, 0)
    new = result.new_key
    assert KeyFile(tmp_path / "credentials_key").load().key_id == new.key_id
    assert not manager.key_file.next_path.exists()
    assert _active(db_conn) == new.key_id
    rows = fetch_sealed_credentials()
    assert {r.key_id for r in rows} == {new.key_id}
    assert all(bytes(r.sealed) != before[r.field_key] for r in rows)
    opened = open_rows(new, rows)
    assert opened.values == {
        "BANCHILE_PASSWORD": "synthetic-password",
        "EMAIL_IMAP_HOST": HOST,
        "EMAIL_IMAP_USER": USER,
        "EMAIL_IMAP_PASSWORD": "synthetic-app-password",
    }
    assert not open_rows(old, rows).values
    # The running service picks the rotated key file up on its next sync.
    manager.tick()
    assert manager.key.key_id == new.key_id
    assert manager.status()["state"] == "ok"


def test_rotating_a_v1_key_moves_the_ring_and_rows_to_v2(db_conn, tmp_path):
    """An install from before v2: its v1 key is published and owns rows. The
    rotation publishes a v2 key under the v2 algorithm and stores every row
    under the v2 scheme."""
    key_file = KeyFile(tmp_path / "credentials_key")
    old = make_v1_key()
    key_file.replace(old)
    assert sync_key(old, allow_takeover=False).outcome == "published"
    _insert(db_conn, old, "banchile", "password", "synthetic-password")
    _insert(db_conn, old, "email", "imap_host", HOST)
    _insert(db_conn, old, "email", "imap_user", USER)
    _insert(
        db_conn,
        old,
        "email",
        "imap_password",
        "synthetic-app-password",
        binding_digest([HOST, USER]),
    )

    result = rotate_key_file(key_file, DatabaseKeyRing())

    new = result.new_key
    assert (result.resealed, result.skipped) == (4, 0)
    assert new.scheme == "hpke-mlkem768x25519-sha256-aes256gcm-v2"
    assert key_file.load().key_id == new.key_id
    ring = db_conn.execute(
        "SELECT key_id, status, algorithm, octet_length(public_key) AS n FROM credential_keys"
        " ORDER BY status"
    ).fetchall()
    assert [(r["key_id"], r["status"], r["algorithm"], r["n"]) for r in ring] == [
        (new.key_id, "active", new.algorithm, 1216),
        (old.key_id, "retired", old.algorithm, 32),
    ]
    schemes = db_conn.execute("SELECT DISTINCT key_id, scheme FROM institution_credentials")
    assert [(r["key_id"], r["scheme"]) for r in schemes] == [(new.key_id, new.scheme)]
    assert open_rows(new, fetch_sealed_credentials()).values == {
        "BANCHILE_PASSWORD": "synthetic-password",
        "EMAIL_IMAP_HOST": HOST,
        "EMAIL_IMAP_USER": USER,
        "EMAIL_IMAP_PASSWORD": "synthetic-app-password",
    }


def test_rotation_refuses_a_key_file_that_is_not_active(db_conn, tmp_path):
    other = generate_key()
    sync_key(other, allow_takeover=False)
    key_file = KeyFile(tmp_path / "credentials_key")
    key_file.load_or_create()
    with pytest.raises(RotationError):
        rotate_key_file(key_file, DatabaseKeyRing())
    assert not key_file.next_path.exists()
    assert _active(db_conn) == other.key_id


def test_crash_before_the_commit_rolls_back(db_conn, tmp_path):
    manager = _manager(tmp_path)
    manager.reload()
    _insert(db_conn, manager.key, "skipo", "api_key", "synthetic-key")
    old_id = manager.key.key_id
    # The process died after writing .next, before the transaction.
    manager.key_file.write_pending(generate_key())

    restarted = _manager(tmp_path, environ={})
    assert restarted.reload() is True
    assert not restarted.key_file.next_path.exists()
    assert restarted.key.key_id == old_id == _active(db_conn)
    assert restarted.overlay.status()["SKIPO_API_KEY"]["stored"] is True


def test_crash_after_the_commit_finishes_the_rotation(db_conn, tmp_path):
    manager = _manager(tmp_path)
    manager.reload()
    old = manager.key
    _insert(db_conn, old, "skipo", "api_key", "synthetic-key")
    new = generate_key()
    manager.key_file.write_pending(new)
    db_credentials.rotate_key(old, new, lambda rows: reseal_rows(old, new, rows))
    # The process died before renaming .next over the key file: the file
    # still holds the old key, every row is sealed to the new one.

    restarted = _manager(tmp_path, environ={})
    assert restarted.key.key_id == old.key_id
    assert restarted.reload() is True
    assert restarted.key.key_id == new.key_id
    assert KeyFile(tmp_path / "credentials_key").load().key_id == new.key_id
    assert not restarted.key_file.next_path.exists()
    assert restarted.overlay.status()["SKIPO_API_KEY"]["stored"] is True


def test_an_unconfirmed_commit_is_decided_by_the_ring(db_conn, tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.reload()
    _insert(db_conn, manager.key, "skipo", "api_key", "synthetic-key")
    real = db_credentials.rotate_key

    def commit_then_lose_the_connection(*args):
        real(*args)
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(db_credentials, "rotate_key", commit_then_lose_the_connection)
    result = rotate_key_file(manager.key_file, DatabaseKeyRing())
    assert result.resealed is None
    assert manager.key_file.load().key_id == result.new_key.key_id == _active(db_conn)


def test_a_save_in_flight_is_resealed_by_the_rotation(db_conn, tmp_path):
    """The dashboard's save holds the active key FOR SHARE: the rotation waits
    for it, then re-seals the row it wrote."""
    manager = _manager(tmp_path)
    manager.reload()
    old = manager.key
    web = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        web.execute(
            "SELECT 1 FROM credential_keys WHERE key_id = %s AND status = 'active' FOR SHARE",
            (old.key_id,),
        )
        _insert(web, old, "skipo", "api_key", "synthetic-key")
        results = []
        rotation = threading.Thread(
            target=lambda: results.append(rotate_key_file(manager.key_file, DatabaseKeyRing()))
        )
        rotation.start()
        _wait_for_lock_waiter(db_conn)
        web.commit()
        rotation.join(10)
    finally:
        web.close()
    assert results and results[0].resealed == 1
    rows = fetch_sealed_credentials()
    assert open_rows(results[0].new_key, rows).values == {"SKIPO_API_KEY": "synthetic-key"}


def test_a_save_during_a_rotation_never_lands_under_the_retired_key(db_conn, tmp_path):
    """A save that waits on the rotation finds its key retired and stores
    nothing; the dashboard then re-seals to the new key."""
    manager = _manager(tmp_path)
    manager.reload()
    old = manager.key
    inside, release = threading.Event(), threading.Event()

    def slow_reseal(rows, new):
        inside.set()
        release.wait(10)
        return reseal_rows(old, new, rows)

    new = generate_key()
    rotation = threading.Thread(
        target=lambda: db_credentials.rotate_key(old, new, lambda rows: slow_reseal(rows, new))
    )
    rotation.start()
    assert inside.wait(5)
    saved = []
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as web:
        saver = threading.Thread(
            target=lambda: saved.append(_web_save(web, old, "skipo", "api_key", "synthetic-key"))
        )
        saver.start()
        _wait_for_lock_waiter(db_conn)
        release.set()
        rotation.join(10)
        saver.join(10)
        assert saved == [False]
        assert _web_save(web, new, "skipo", "api_key", "synthetic-key") is True
    rows = fetch_sealed_credentials()
    assert [r.key_id for r in rows] == [new.key_id]


def test_rotating_a_key_that_is_no_longer_active_conflicts(db_conn):
    first, second = generate_key(), generate_key()
    sync_key(first, allow_takeover=False)
    db_credentials.reset_active_key(second)
    with pytest.raises(KeyRingConflict):
        db_credentials.rotate_key(first, generate_key(), lambda rows: ([], []))


def test_the_service_rotates_an_old_key_and_records_a_failure(db_conn, tmp_path, monkeypatch):
    """CREDENTIALS_KEY_MAX_AGE_DAYS: a failed attempt is recorded for the
    dashboard and retried later; a successful one clears it."""
    manager = _manager(tmp_path, max_age_days=90)
    manager.reload()
    _insert(db_conn, manager.key, "skipo", "api_key", "synthetic-key")
    manager.reload()
    old_id = manager.key.key_id
    assert manager.tick() is False  # young key: nothing to do

    db_conn.execute(
        "UPDATE credential_keys SET created_at = now() - interval '91 days' WHERE key_id = %s",
        (old_id,),
    )

    def db_went_away(*args):
        raise psycopg.OperationalError("db went away")

    with monkeypatch.context() as patched:
        patched.setattr(db_credentials, "rotate_key", db_went_away)
        manager.tick()
    row = db_conn.execute(
        "SELECT rotation_failed_at, rotation_error FROM credential_keys WHERE key_id = %s",
        (old_id,),
    ).fetchone()
    assert row["rotation_failed_at"] is not None
    assert "OperationalError" in row["rotation_error"]
    assert manager.status()["rotation_error"]
    assert not manager.key_file.next_path.exists()

    manager._next_rotation_attempt = 0.0
    manager.tick()
    assert (
        manager.key.key_id
        != old_id
        == db_conn.execute(
            "SELECT key_id FROM credential_keys WHERE status = 'retired'"
        ).fetchone()["key_id"]
    )
    assert manager.status()["rotation_error"] is None
    assert manager.overlay.status()["SKIPO_API_KEY"]["stored"] is True
    created = db_conn.execute(
        "SELECT created_at, rotation_error FROM credential_keys WHERE status = 'active'"
    ).fetchone()
    assert created["rotation_error"] is None
    assert manager.status()["key_created_at"] is not None
    assert (
        timedelta(0)
        <= (db_conn.execute("SELECT now() AS n").fetchone()["n"] - created["created_at"])
        < timedelta(minutes=5)
    )
