"""Sealed scraper credentials (V048/V049): the key ring and the rows.

The rows hold only HPKE ciphertext (see scrapers/credentials.py); nothing
here sees a plaintext value, except `rotate_key`, which hands the rows to a
re-seal callback inside its transaction.

Every write to the ring runs under one transaction-scoped advisory lock, so
two processes never publish, take over, rotate or reset at once. A rotation
also locks the active key row FOR UPDATE, which the dashboard's writes (and
V049's trigger) lock FOR SHARE: a save either lands before the rotation reads
the rows, and is re-sealed, or waits and then finds its key retired.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from db.connection import get_pool, with_db_retry
from scrapers.credentials import (
    ALGORITHM,
    CredentialKey,
    ResealedCredential,
    SealedCredential,
)

_RING_LOCK = "SELECT pg_advisory_xact_lock(hashtext('el-chanchito/credential_keys'))"


@dataclass(frozen=True)
class ActiveKey:
    key_id: str
    created_at: datetime


@dataclass(frozen=True)
class KeySync:
    """What `sync_key` found or did. `outcome` is `current` (the key was
    already active), `published` (there was none), `took_over` (the active
    key had no rows sealed to it) or `mismatch` (another key is active and
    owns rows; nothing changed)."""

    outcome: str
    active: ActiveKey
    orphaned: int = 0


class KeyRingConflict(Exception):
    """The ring is not in the state the operation expected."""


def _active(conn, lock: bool = False) -> ActiveKey | None:
    row = conn.execute(
        "SELECT key_id, created_at FROM credential_keys WHERE status = 'active'"
        + (" FOR UPDATE" if lock else "")
    ).fetchone()
    return ActiveKey(row[0], row[1]) if row else None


def _activate(conn, key: CredentialKey) -> ActiveKey:
    row = conn.execute(
        """
        INSERT INTO credential_keys (key_id, algorithm, public_key, status, created_at)
        VALUES (%s, %s, %s, 'active', now())
        ON CONFLICT (key_id) DO UPDATE
          SET status = 'active', retired_at = NULL, created_at = now(),
              rotation_failed_at = NULL, rotation_error = NULL
        RETURNING key_id, created_at
        """,
        (key.key_id, ALGORITHM, key.public_raw),
    ).fetchone()
    return ActiveKey(row[0], row[1])


def _retire(conn, key_id: str) -> None:
    conn.execute(
        "UPDATE credential_keys SET status = 'retired', retired_at = now()"
        " WHERE key_id = %s AND status = 'active'",
        (key_id,),
    )


def _count_sealed_to(conn, key_id: str) -> int:
    (n,) = conn.execute(
        "SELECT count(*) FROM institution_credentials WHERE key_id = %s", (key_id,)
    ).fetchone()
    return n


def _count_orphaned(conn, key_id: str) -> int:
    (n,) = conn.execute(
        "SELECT count(*) FROM institution_credentials WHERE key_id <> %s", (key_id,)
    ).fetchone()
    return n


@with_db_retry
def sync_key(key: CredentialKey, allow_takeover: bool) -> KeySync:
    """Make sure the dashboard seals to `key`, without ever replacing an
    active key that owns rows. With `allow_takeover`, an active key no row
    is sealed to is retired in favour of `key` (nothing is lost)."""
    with get_pool().connection() as conn, conn.transaction():
        conn.execute(_RING_LOCK)
        active = _active(conn, lock=True)
        if active is not None and active.key_id == key.key_id:
            return KeySync("current", active, _count_orphaned(conn, key.key_id))
        if active is None:
            outcome = "published"
        elif allow_takeover and _count_sealed_to(conn, active.key_id) == 0:
            _retire(conn, active.key_id)
            outcome = "took_over"
        else:
            return KeySync("mismatch", active)
        return KeySync(outcome, _activate(conn, key), _count_orphaned(conn, key.key_id))


@with_db_retry
def fetch_active_key() -> ActiveKey | None:
    """The key the dashboard currently seals to, if any."""
    with get_pool().connection() as conn:
        return _active(conn)


@with_db_retry
def pending_outcome(pending_key_id: str, current_key_id: str | None) -> str:
    """Whether a rotation from `current_key_id` to `pending_key_id` committed.

    Takes the ring's advisory lock first, so a rotation whose COMMIT is still
    in flight finishes (and becomes visible) before this reads. Returns
    `committed` (the pending key is in the ring), `not_committed` (it is not,
    and the current key is still the active one: the rotation never landed
    here) or `unknown` (anything else, e.g. another database): only
    `not_committed` lets the caller delete the pending key."""
    with get_pool().connection() as conn, conn.transaction():
        conn.execute(_RING_LOCK)
        pending = conn.execute(
            "SELECT 1 FROM credential_keys WHERE key_id = %s", (pending_key_id,)
        ).fetchone()
        if pending is not None:
            return "committed"
        active = _active(conn)
    if current_key_id is not None and active is not None and active.key_id == current_key_id:
        return "not_committed"
    return "unknown"


def _rows(conn, where: str = "", params: tuple = (), lock: bool = False) -> list[SealedCredential]:
    rows = conn.execute(
        "SELECT institution_slug, field_key, scheme, key_id, sealed FROM institution_credentials "
        + where
        + " ORDER BY institution_slug, field_key"
        + (" FOR UPDATE" if lock else ""),
        params,
    ).fetchall()
    return [
        SealedCredential(
            institution_slug=slug,
            field_key=field_key,
            scheme=scheme,
            key_id=key_id,
            sealed=bytes(sealed),
        )
        for slug, field_key, scheme, key_id, sealed in rows
    ]


@with_db_retry
def fetch_sealed_credentials() -> list[SealedCredential]:
    with get_pool().connection() as conn:
        return _rows(conn)


def rotate_key(
    old: CredentialKey,
    new: CredentialKey,
    reseal: Callable[
        [list[SealedCredential]], tuple[list[ResealedCredential], list[SealedCredential]]
    ],
) -> tuple[int, int]:
    """In one transaction: re-seal every row sealed to `old` to `new`, retire
    `old` and make `new` the active key. Returns (re-sealed, left as they
    were). Raises KeyRingConflict when `old` is not the active key. Must
    stay un-retried: the caller decides an unknown outcome from the ring, and
    discards `.next` on KeyRingConflict, which a retried commit would make
    unsafe."""
    with get_pool().connection() as conn, conn.transaction():
        conn.execute(_RING_LOCK)
        active = _active(conn, lock=True)
        if active is None or active.key_id != old.key_id:
            raise KeyRingConflict("the key being rotated is not the active key")
        rows = _rows(conn, "WHERE key_id = %s", (old.key_id,), lock=True)
        resealed, skipped = reseal(rows)
        _retire(conn, old.key_id)
        _activate(conn, new)
        for row in resealed:
            conn.execute(
                "UPDATE institution_credentials SET key_id = %s, sealed = %s"
                " WHERE institution_slug = %s AND field_key = %s AND key_id = %s",
                (new.key_id, row.sealed, row.institution_slug, row.field_key, old.key_id),
            )
    return len(resealed), len(skipped)


@with_db_retry
def reset_active_key(key: CredentialKey) -> tuple[str | None, int]:
    """Retire whatever key is active and make `key` the active one, whether
    or not rows are sealed to the old one. Returns the retired key id (None
    when `key` already was active, or none was) and how many rows can no
    longer be opened."""
    with get_pool().connection() as conn, conn.transaction():
        conn.execute(_RING_LOCK)
        active = _active(conn, lock=True)
        retired = None
        if active is None or active.key_id != key.key_id:
            if active is not None:
                _retire(conn, active.key_id)
                retired = active.key_id
            _activate(conn, key)
        return retired, _count_orphaned(conn, key.key_id)


@with_db_retry
def record_rotation_failure(key_id: str, reason: str) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE credential_keys SET rotation_failed_at = now(), rotation_error = %s"
            " WHERE key_id = %s AND status = 'active'",
            (reason[:500], key_id),
        )
