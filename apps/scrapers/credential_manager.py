"""Wire the sealed credentials into the scraper service.

`CredentialManager` owns the service's key and the env overlay. `start()`
only loads (or creates) the key file, so the service comes up without the
database; `reload()` and `tick()` (a scheduler job, also kicked in the
background) then sync the key with the database's key ring and lay every
stored credential it can open over `os.environ`.

One key per database: a service never replaces an active key it does not
hold. If the database seals to another key that owns rows, the service
reports `key_mismatch`, opens nothing and logs the fix (run only the service
that saved them, never a copy of its key file, or `make credentials-reset-key`). Before it has ever published in this process
it may take over an active key no row is sealed to, since nothing is lost.

Rotation (`rotate_key_file`, shared by the service's periodic check and
`credentials_admin.py rotate`) writes the new key to `<file>.next`, re-seals
every row and swaps the active key in one transaction, then renames `.next`
over the key file, which deletes the old private key. A crash leaves `.next`
behind; `recover_pending` finishes or rolls it back by whether the database
knows that key. Both run under the key file's lock.

Only the long-running service creates or publishes a key. One-off runs
(`make scrapers-once`) and the login CLIs load an existing key read-only and
use the stored credentials only when the database's active key is theirs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from db import credentials as db_credentials
from db.connection import close_pool, is_transient_db_error
from db.credentials import ActiveKey, KeyRingConflict, KeySync
from scrapers.credentials import (
    V2,
    CredentialKey,
    CredentialKeyError,
    CredentialOverlay,
    KeyFile,
    OpenedCredentials,
    ResealedCredential,
    SealedCredential,
    generate_key,
    key_file_path,
    open_rows,
    reseal_rows,
)

logger = logging.getLogger("scraper-service.credentials")

MAX_AGE_ENV = "CREDENTIALS_KEY_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 90
# After a failed periodic rotation, wait this long before trying again.
ROTATION_RETRY_SECONDS = 60 * 60

RESET_HINT = (
    "run only the scraper service that saved them against this database (stop this one; "
    "one service per database). Do not copy its key file here: after a rotation a copy "
    "keeps the old key, which rotation is meant to delete. If that key file is lost, run "
    "`make credentials-reset-key` to start over (stored credentials must then be entered "
    "again)"
)


class DatabaseKeyRing:
    """The key ring and rows in Postgres (db/credentials.py)."""

    def sync_key(self, key: CredentialKey, allow_takeover: bool) -> KeySync:
        return db_credentials.sync_key(key, allow_takeover)

    def active_key(self) -> ActiveKey | None:
        return db_credentials.fetch_active_key()

    def pending_outcome(self, pending_key_id: str, current_key_id: str | None) -> str:
        return db_credentials.pending_outcome(pending_key_id, current_key_id)

    def fetch_rows(self) -> list[SealedCredential]:
        return db_credentials.fetch_sealed_credentials()

    def rotate(
        self,
        old: CredentialKey,
        new: CredentialKey,
        reseal: Callable[
            [list[SealedCredential]], tuple[list[ResealedCredential], list[SealedCredential]]
        ],
    ) -> tuple[int, int]:
        return db_credentials.rotate_key(old, new, reseal)

    def reset(self, key: CredentialKey) -> tuple[str | None, int]:
        return db_credentials.reset_active_key(key)

    def record_rotation_failure(self, key_id: str, reason: str) -> None:
        db_credentials.record_rotation_failure(key_id, reason)


class RotationError(Exception):
    """A rotation that did not happen; the message never carries a value."""


class RotationPending(RotationError):
    """The outcome is unknown until the database answers; `.next` stays and
    the next sync finishes or rolls it back."""


class PendingUnresolved(RotationPending):
    """A leftover `.next` the database neither confirms nor rules out (for
    example DATABASE_URL points at another database, or it was recreated).
    It is kept, never deleted: a database that can decide resolves it, or
    `make credentials-reset-key` moves it aside."""

    def __init__(self, next_path: Path, pending_key_id: str) -> None:
        super().__init__("an interrupted rotation could not be resolved against this database")
        self.next_path = next_path
        self.pending_key_id = pending_key_id

    def detail(self) -> str:
        return (
            f"{self.next_path} holds the key of an interrupted rotation "
            f"({self.pending_key_id[:12]}) that this database neither confirms nor rules out. "
            "If DATABASE_URL points at the wrong database, fix it and the service resolves it. "
            "If this database was recreated, or the rotation is known to have failed, run "
            "`make credentials-reset-key`: it moves the file aside to "
            f"{self.next_path.name}.aside-<time>, which still holds a private key; delete it "
            "once you are sure nothing was sealed to it"
        )


NO_LOCK_REASON = (
    "rotating or resetting the key needs file locking (macOS, Linux or WSL2); "
    "native Windows is not supported"
)


@dataclass(frozen=True)
class RotationResult:
    old_key_id: str
    new_key: CredentialKey
    resealed: int | None
    skipped: int | None


def recover_pending(key_file: KeyFile, ring) -> str | None:
    """Finish or roll back a rotation a crash interrupted; call it with the
    key file's lock held. Returns "promoted", "discarded" or None (nothing
    pending).

    The pending key is promoted when the ring knows it (the commit landed),
    and deleted only when the ring positively shows the key file's current
    key still active without it. Anything else (another database, no current
    key file) raises PendingUnresolved and keeps it; a database error
    propagates and keeps it too."""
    key_file.sweep_stale_tmp()
    pending = key_file.load_pending()
    if pending is None:
        return None
    try:
        current: str | None = key_file.load().key_id
    except FileNotFoundError:
        current = None
    outcome = ring.pending_outcome(pending.key_id, current)
    if outcome == "committed":
        key_file.promote_pending()
        logger.warning(
            "Finished an interrupted credentials key rotation: %s is now the key file "
            "and the old private key is deleted",
            pending.key_id[:12],
        )
        return "promoted"
    if outcome == "not_committed":
        key_file.discard_pending()
        logger.warning("Rolled back an interrupted credentials key rotation; the key is unchanged")
        return "discarded"
    raise PendingUnresolved(key_file.next_path, pending.key_id)


def rotate_key_file(
    key_file: KeyFile, ring, fallback: dict[str, str | None] | None = None
) -> RotationResult:
    """Rotate the database's active key, which must be `key_file`'s."""
    if not key_file.locking_available:
        raise RotationError(NO_LOCK_REASON)
    with key_file.lock():
        try:
            recover_pending(key_file, ring)
        except (CredentialKeyError, RotationPending):
            raise
        except Exception as exc:
            raise RotationPending(
                f"could not check an interrupted rotation ({type(exc).__name__})"
            ) from None
        try:
            old = key_file.load()
        except FileNotFoundError:
            logger.warning("No credentials key file at %s to rotate", key_file.path)
            raise RotationError("there is no key file to rotate") from None
        try:
            active = ring.active_key()
        except Exception as exc:
            raise RotationError(f"could not read the active key ({type(exc).__name__})") from None
        if active is None or active.key_id != old.key_id:
            logger.warning(
                "%s holds key %s, not the database's active key %s",
                key_file.path,
                old.key_id[:12],
                (active.key_id if active else "none")[:12],
            )
            raise RotationError("the key file does not hold the database's active key")
        new = generate_key()
        key_file.write_pending(new)
        try:
            resealed, skipped = ring.rotate(
                old, new, lambda rows: reseal_rows(old, new, rows, fallback)
            )
        except KeyRingConflict as exc:
            # Raised inside the transaction, which rolled back: nothing used
            # the new key.
            key_file.discard_pending()
            raise RotationError(f"the database refused the rotation ({exc})") from None
        except Exception as exc:
            reason = type(exc).__name__
            try:
                outcome = recover_pending(key_file, ring)
            except Exception:
                raise RotationPending(
                    f"the database did not confirm the rotation ({reason})"
                ) from None
            if outcome != "promoted":
                raise RotationError(f"the database refused the rotation ({reason})") from None
            return RotationResult(old.key_id, new, None, None)
        key_file.promote_pending()
    logger.info(
        "Rotated the credentials key %s -> %s: %d row(s) re-sealed, %d left as they were; "
        "the old private key is deleted",
        old.key_id[:12],
        new.key_id[:12],
        resealed,
        skipped,
    )
    return RotationResult(old.key_id, new, resealed, skipped)


def reset_key_file(key_file: KeyFile, ring) -> tuple[CredentialKey, str | None, int]:
    """Make the local key file's key the database's active one, retiring
    whatever was active. Rows sealed to that one can no longer be opened.
    A pending key the database cannot judge is moved aside, never deleted."""
    if not key_file.locking_available:
        raise RotationError(NO_LOCK_REASON)
    with key_file.lock():
        try:
            recover_pending(key_file, ring)
        except PendingUnresolved as exc:
            aside = key_file.set_pending_aside()
            logger.warning(
                "Moved the unresolved pending key %s aside to %s; it still holds a private "
                "key: delete it once you are sure nothing was sealed to it",
                exc.pending_key_id[:12],
                aside,
            )
        except (CredentialKeyError, RotationPending):
            raise
        except Exception as exc:
            raise RotationPending(
                f"could not check an interrupted rotation ({type(exc).__name__})"
            ) from None
        key = key_file.load_or_create_locked()
        retired, orphaned = ring.reset(key)
    return key, retired, orphaned


def max_age_days_from_env(environ=os.environ) -> int:
    raw = (environ.get(MAX_AGE_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_DAYS
    try:
        days = int(raw)
    except ValueError:
        days = -1
    if days < 0:
        logger.warning(
            "%s=%r is not a number of days; using %d", MAX_AGE_ENV, raw, DEFAULT_MAX_AGE_DAYS
        )
        return DEFAULT_MAX_AGE_DAYS
    return days


def _transient(exc: BaseException) -> bool:
    """A database connection error, worth waiting for; a file error on the
    key or its lock (EACCES, EROFS, ENOLCK) is not."""
    return is_transient_db_error(exc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class CredentialManager:
    def __init__(
        self,
        key_path: Path | None = None,
        environ: MutableMapping[str, str] | None = None,
        ring=None,
        max_age_days: int | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.key_file = KeyFile(key_path or key_file_path())
        self.key_path = self.key_file.path
        # Snapshots the environment as it is now: the fallback for every field.
        self.overlay = CredentialOverlay(environ)
        self.ring = ring or DatabaseKeyRing()
        self.max_age_days = max_age_days_from_env() if max_age_days is None else max_age_days
        self._now = now
        self.key: CredentialKey | None = None
        self._key_error = False
        self._published = False
        self._ever_published = False
        self._loaded = False
        self._mismatch: str | None = None
        self._active: ActiveKey | None = None
        self._orphaned = 0
        self._opened = OpenedCredentials({}, frozenset())
        self._rotation_failure: tuple[datetime, str] | None = None
        self._next_rotation_attempt = 0.0
        self._rows_read = False
        self._warned_no_lock = False
        self._pending_unresolved = False
        self._key_file_error = False
        # Set once the stored credentials are applied or known to be unusable
        # (no key, key mismatch, schema missing): the first scrape waits on it,
        # so it never logs in with credentials the dashboard has replaced.
        self.settled = threading.Event()
        # `_io_lock` serializes the database work (which may sit through
        # retries for ~30 s); `_lock` guards the state and is only ever held
        # briefly, so status() never waits on the database.
        self._io_lock = threading.Lock()
        self._lock = threading.Lock()
        self._retry_thread: threading.Thread | None = None

    # -- startup ---------------------------------------------------------------

    def start(self) -> None:
        """Service mode: load or create the key, nothing else; the database
        work happens in `reload()` / `tick()`. Never raises: without a key the
        env fallback still works."""
        try:
            self.key = self.key_file.load_or_create()
        except (CredentialKeyError, OSError) as exc:
            logger.error(
                "Stored credentials are disabled (%s); only the secret store and env are used",
                exc,
            )
            self._key_error = True
            self.settled.set()

    def start_readonly(self) -> None:
        """One-off mode: use an existing key only if it is the active one.
        Never creates, publishes, rotates or replaces a key."""
        try:
            candidates = [self.key_file.load()]
        except FileNotFoundError:
            logger.warning(
                "No credentials key at %s: stored credentials are not used. Start the "
                "scheduled service (make dev) once to create it",
                self.key_path,
            )
            return
        except CredentialKeyError as exc:
            logger.error("Stored credentials are disabled (%s)", exc)
            return
        try:
            pending = self.key_file.load_pending()
        except CredentialKeyError:
            pending = None
        if pending is not None:
            candidates.append(pending)
        try:
            active = self.ring.active_key()
        except Exception as exc:
            logger.warning("Could not read the active credentials key: %s", type(exc).__name__)
            return
        key = next((k for k in candidates if active and k.key_id == active.key_id), None)
        if key is None:
            logger.warning(
                "The database seals to another key (%s) than %s (%s): stored credentials "
                "are not used by this run",
                (active.key_id if active else "none")[:12],
                self.key_path,
                candidates[0].key_id[:12],
            )
            return
        self.key = key
        self._published = True
        try:
            rows = self.ring.fetch_rows()
        except Exception as exc:
            logger.warning("Could not read stored credentials: %s", type(exc).__name__)
            return
        self._apply(open_rows(key, rows, self.overlay.baseline))

    # -- sync ------------------------------------------------------------------------

    def _adopt_key_file(self) -> bool:
        """With the key file's lock held: finish an interrupted rotation and
        pick up a key `credentials_admin.py` rotated or reset on disk. True
        when the key changed."""
        assert self.key is not None
        recover_pending(self.key_file, self.ring)
        try:
            disk = self.key_file.load()
        except FileNotFoundError:
            logger.warning(
                "The credentials key file %s is gone; keeping the loaded key", self.key_path
            )
            return False
        except CredentialKeyError as exc:
            logger.error("The credentials key file is unusable (%s); keeping the loaded key", exc)
            return False
        if disk.key_id == self.key.key_id:
            return False
        logger.info("Loaded the new credentials key %s from %s", disk.key_id[:12], self.key_path)
        self.key = disk
        return True

    def _apply(self, opened: OpenedCredentials) -> bool:
        with self._lock:
            changed = opened != self._opened or not self._loaded
            self._opened = opened
            self.overlay.apply(opened)
            self._loaded = True
        if changed and opened.values:
            logger.info("Applied %d stored credential field(s)", len(opened.values))
        self.settled.set()
        return changed

    def _db_failed(self, what: str, exc: BaseException) -> None:
        logger.warning("Could not %s: %s", what, type(exc).__name__)
        if not _transient(exc):
            # A missing table or similar: the env fallback is what there is.
            self.settled.set()

    def _sync_locked(self, read_rows: bool) -> bool:
        """Called with `_io_lock` held. Syncs the key with the ring and, when
        published and asked to (or the state changed), re-applies the rows.
        True when the overlay changed."""
        if self.key is None:
            return False
        try:
            with self.key_file.lock():
                key_changed = self._adopt_key_file()
                sync = self.ring.sync_key(self.key, allow_takeover=not self._ever_published)
        except PendingUnresolved as exc:
            if not self._pending_unresolved:
                logger.error("pending_unresolved: %s", exc.detail())
            with self._lock:
                self._pending_unresolved = True
            self.settled.set()
            return False
        except (CredentialKeyError, OSError) as exc:
            # The key file or its lock (EACCES, EROFS, ENOLCK): waiting will
            # not fix it, so run on the env fallback and say so.
            if not self._key_file_error:
                logger.error(
                    "Stored credentials are disabled: the credentials key file cannot be used "
                    "(%s); only the secret store and env are used",
                    exc,
                )
            with self._lock:
                self._key_file_error = True
            self.settled.set()
            return False
        except Exception as exc:
            self._db_failed("sync the credentials key with the database", exc)
            return False
        with self._lock:
            self._pending_unresolved = False
            self._key_file_error = False

        if sync.outcome == "mismatch":
            newly = self._mismatch != sync.active.key_id
            with self._lock:
                self._published = False
                self._mismatch = sync.active.key_id
                self._active = sync.active
            if newly:
                logger.warning(
                    "key_mismatch: the database seals credentials to key %s, which owns "
                    "stored rows, but %s holds %s. Stored credentials are not opened and the "
                    "dashboard cannot save new ones until you %s",
                    sync.active.key_id[:12],
                    self.key_path,
                    self.key.key_id[:12],
                    RESET_HINT,
                )
            stale = self._opened.values or self._opened.suppressed
            changed = self._apply(OpenedCredentials({}, frozenset())) if stale else False
            self.settled.set()
            return changed

        if sync.outcome == "published":
            logger.info("Published credentials key %s", self.key.key_id[:12])
        elif sync.outcome == "took_over":
            logger.warning(
                "Took over the credentials key: no stored credential was sealed to the "
                "previous key, so %s (%s) is now the one the dashboard seals to",
                self.key.key_id[:12],
                self.key_path,
            )
        state_changed = self._mismatch is not None or not self._published or key_changed
        with self._lock:
            self._published = True
            self._ever_published = True
            self._mismatch = None
            self._active = sync.active
        if sync.orphaned and sync.orphaned != self._orphaned:
            logger.warning(
                "%d stored credential(s) were sealed to another key and cannot be opened; "
                "enter them again in Credenciales",
                sync.orphaned,
            )
        self._orphaned = sync.orphaned
        if not (read_rows or state_changed or not self._loaded):
            return False
        try:
            rows = self.ring.fetch_rows()
        except Exception as exc:
            self._db_failed("read stored credentials", exc)
            return False
        self._rows_read = True
        return self._apply(open_rows(self.key, rows, self.overlay.baseline))

    def reload(self) -> bool:
        """Sync the key and re-read the rows. True when they were read and
        applied; False when not (the previous overlay stays, or the env
        fallback alone on a key mismatch)."""
        with self._io_lock:
            self._rows_read = False
            self._sync_locked(read_rows=True)
            return self._rows_read

    # -- rotation ----------------------------------------------------------------------

    def _needs_upgrade(self) -> bool:
        """The active key is not v2 (an older install's v1 key): the dashboard
        no longer seals to it, so it is rotated at once, whatever the age."""
        return self._active is not None and self._active.algorithm != V2.algorithm

    def _rotation_due(self) -> bool:
        if not (self._published and self._loaded):
            return False
        if self.max_age_days <= 0 and not self._needs_upgrade():
            return False
        if not self.key_file.locking_available:
            if not self._warned_no_lock:
                self._warned_no_lock = True
                logger.warning("Automatic key rotation is off: %s", NO_LOCK_REASON)
            return False
        if self._active is None or time.monotonic() < self._next_rotation_attempt:
            return False
        if self._needs_upgrade():
            return True
        return self._now() - self._active.created_at >= timedelta(days=self.max_age_days)

    def _rotate_locked(self) -> bool:
        assert self.key is not None
        if self._needs_upgrade():
            logger.info(
                "The credentials key uses the older v1 scheme, which the dashboard no longer "
                "seals to; rotating it to a v2 (X-Wing) key"
            )
        else:
            logger.info(
                "The credentials key is older than %d days (%s); rotating it",
                self.max_age_days,
                MAX_AGE_ENV,
            )
        try:
            result = rotate_key_file(self.key_file, self.ring, self.overlay.baseline)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RotationError) else type(exc).__name__
            self._next_rotation_attempt = time.monotonic() + ROTATION_RETRY_SECONDS
            with self._lock:
                self._rotation_failure = (self._now(), reason)
            logger.warning(
                "Credentials key rotation failed (%s); retrying in %d minutes",
                reason,
                ROTATION_RETRY_SECONDS // 60,
            )
            try:
                self.ring.record_rotation_failure(self.key.key_id, reason)
            except Exception as record_exc:
                logger.warning(
                    "Could not record the rotation failure: %s", type(record_exc).__name__
                )
            return False
        self.key = result.new_key
        with self._lock:
            self._rotation_failure = None
        return self._sync_locked(read_rows=True)

    def tick(self) -> bool:
        """The periodic job: sync the key (publish, adopt a rotated key file,
        detect a mismatch), read the rows until they load, and rotate a key
        older than the configured age. True when the overlay changed (the
        caller then rebuilds its scrapers)."""
        with self._io_lock:
            if self.key is None:
                return False
            changed = self._sync_locked(read_rows=False)
            if self._rotation_due():
                changed = self._rotate_locked() or changed
            return changed

    def retry_in_background(self, on_loaded: Callable[[], object] | None = None) -> None:
        """Run one `tick()` on a thread unless one is already running or there
        is nothing pending."""
        if self.key is None or (self._published and self._loaded):
            return
        if self._retry_thread is not None and self._retry_thread.is_alive():
            return

        def run() -> None:
            if self.tick() and on_loaded is not None:
                on_loaded()

        self._retry_thread = threading.Thread(target=run, name="credentials-retry", daemon=True)
        self._retry_thread.start()

    # -- status ------------------------------------------------------------------------

    def status(self) -> dict:
        """Booleans, key ids and dates only; never a value. Never waits on
        the database."""
        with self._lock:
            usable = self.key is not None and not self._key_file_error
            if not usable:
                state = "key_unavailable"
            elif self._pending_unresolved:
                state = "pending_unresolved"
            elif self._mismatch:
                state = "key_mismatch"
            elif self._published and self._loaded:
                state = "ok"
            else:
                state = "starting"
            failure = self._rotation_failure
            return {
                "state": state,
                "key_available": usable,
                "key_id": self.key.key_id if self.key else None,
                "published": self._published,
                "loaded": self._loaded,
                "key_mismatch": self._mismatch is not None,
                "active_key_id": self._active.key_id if self._active else None,
                "key_created_at": _iso(self._active.created_at) if self._active else None,
                "max_age_days": self.max_age_days,
                "rotation_failed_at": _iso(failure[0]) if failure else None,
                "rotation_error": failure[1] if failure else None,
                "fields": self.overlay.status(),
            }


def apply_stored_credentials(environ: MutableMapping[str, str] | None = None) -> None:
    """For the manual login CLIs: lay the stored credentials over the env when
    a key file and a database are there and the database seals to that key.
    Best effort; never creates or publishes a key."""
    if not os.environ.get("DATABASE_URL"):
        return
    try:
        CredentialManager(environ=environ).start_readonly()
    finally:
        close_pool()
