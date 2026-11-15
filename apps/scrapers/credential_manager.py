"""Wire the sealed credentials into the scraper service.

`CredentialManager` owns the service's key and the env overlay: on start it
loads (or creates) the key file, publishes the public key to the database for
the dashboard, and lays every stored credential it can open over
`os.environ`; `reload()` does the last step again after the dashboard saves
or deletes one, and `tick()` (a scheduler job) keeps retrying until the key
is published and the rows are read, and republishes the key if another
process replaced it. See scrapers/credentials.py for the scheme.

Only the long-running service creates or publishes a key. One-off runs
(`make scrapers-once`) and the login CLIs load an existing key read-only and
use the stored credentials only when the database's published key is theirs.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, MutableMapping
from pathlib import Path

from db.connection import close_pool
from db.credentials import (
    fetch_published_key_id,
    fetch_sealed_credentials,
    publish_public_key,
)
from scrapers.credentials import (
    CredentialKey,
    CredentialKeyError,
    CredentialOverlay,
    SealedCredential,
    key_file_path,
    load_key,
    load_or_create_key,
    open_rows,
)

logger = logging.getLogger("scraper-service.credentials")


class CredentialManager:
    def __init__(
        self,
        key_path: Path | None = None,
        environ: MutableMapping[str, str] | None = None,
        fetch: Callable[[], list[SealedCredential]] = fetch_sealed_credentials,
        publish: Callable[[CredentialKey], tuple[str | None, int]] = publish_public_key,
        published_key_id: Callable[[], str | None] = fetch_published_key_id,
    ) -> None:
        self.key_path = key_path or key_file_path()
        # Snapshots the environment as it is now: the fallback for every field.
        self.overlay = CredentialOverlay(environ)
        self.key: CredentialKey | None = None
        self._fetch = fetch
        self._publish = publish
        self._published_key_id = published_key_id
        self._published = False
        self._loaded = False
        # `_io_lock` serializes the database work (which may sit through
        # retries for ~30 s); `_lock` guards the state and is only ever held
        # briefly, so status() never waits on the database.
        self._io_lock = threading.Lock()
        self._lock = threading.Lock()
        self._retry_thread: threading.Thread | None = None

    # -- startup ---------------------------------------------------------------

    def start(self) -> None:
        """Service mode: load or create the key, publish it, apply the stored
        credentials. Never raises: without a key or a database the env
        fallback still works, and `tick()` keeps retrying."""
        try:
            self.key = load_or_create_key(self.key_path)
        except (CredentialKeyError, OSError) as exc:
            logger.error(
                "Stored credentials are disabled (%s); only the secret store and env are used",
                exc,
            )
            return
        self.reload()

    def start_readonly(self) -> None:
        """One-off mode: use an existing key only if it is the published one.
        Never creates, publishes or replaces a key."""
        try:
            self.key = load_key(self.key_path)
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
            published = self._published_key_id()
        except Exception as exc:
            logger.warning("Could not read the published key: %s", type(exc).__name__)
            self.key = None
            return
        if published != self.key.key_id:
            logger.warning(
                "The database seals to another key (%s) than %s (%s): stored credentials "
                "are not used by this run",
                (published or "none")[:12],
                self.key_path,
                self.key.key_id[:12],
            )
            self.key = None
            return
        self._published = True
        self.reload()

    # -- key publication -----------------------------------------------------------

    def _publish_key(self) -> None:
        assert self.key is not None
        try:
            replaced, orphaned = self._publish(self.key)
        except Exception as exc:
            logger.warning("Could not publish the credentials key yet: %s", type(exc).__name__)
            return
        with self._lock:
            self._published = True
        if replaced:
            logger.warning(
                "The credentials key in the database was %s; now %s (%s). If another "
                "scraper service shares this database with its own key file, the two "
                "keep replacing each other: give them one key file",
                replaced[:12],
                self.key.key_id[:12],
                self.key_path,
            )
        if orphaned:
            logger.warning(
                "%d stored credential(s) were sealed to another key and cannot be opened; "
                "restore that key file or enter them again in Credenciales",
                orphaned,
            )

    def _check_published(self) -> None:
        """Republish when the database's key is no longer ours."""
        assert self.key is not None
        try:
            published = self._published_key_id()
        except Exception as exc:
            logger.warning("Could not read the published key: %s", type(exc).__name__)
            return
        if published != self.key.key_id:
            if published:
                logger.warning(
                    "Another process published key %s over ours; publishing ours again",
                    published[:12],
                )
            with self._lock:
                self._published = False
            self._publish_key()

    # -- rows ----------------------------------------------------------------------

    def _reload_locked(self) -> bool:
        """Called with `_io_lock` held; takes `_lock` only to apply."""
        if self.key is None:
            return False
        if not self._published:
            self._publish_key()
            if not self._published:
                # The database is down: reading the rows would sit
                # through the same retries for nothing.
                return False
        try:
            rows = self._fetch()
        except Exception as exc:
            logger.warning("Could not read stored credentials: %s", type(exc).__name__)
            return False
        opened = open_rows(self.key, rows, self.overlay.baseline)
        with self._lock:
            self.overlay.apply(opened)
            self._loaded = True
        if opened.values:
            logger.info("Applied %d stored credential field(s)", len(opened.values))
        return True

    def reload(self) -> bool:
        """Read the rows again and re-apply the overlay; False when the rows
        could not be read (the previous overlay stays)."""
        with self._io_lock:
            return self._reload_locked()

    def tick(self) -> bool:
        """The periodic job: publish the key until it sticks, put it back if
        replaced, and read the rows until they load. True when the rows were
        loaded just now (the caller then rebuilds its scrapers)."""
        with self._io_lock:
            if self.key is None:
                return False
            if self._published:
                self._check_published()
            if self._published and self._loaded:
                return False
            return self._reload_locked()

    def retry_in_background(self, on_loaded: Callable[[], object] | None = None) -> None:
        """Run one `tick()` on a thread unless one is already running."""
        if self.key is None or (self._published and self._loaded):
            return
        if self._retry_thread is not None and self._retry_thread.is_alive():
            return

        def run() -> None:
            if self.tick() and on_loaded is not None:
                on_loaded()

        self._retry_thread = threading.Thread(target=run, name="credentials-retry", daemon=True)
        self._retry_thread.start()

    def status(self) -> dict:
        """Booleans and the public key id only; never a value. Never waits on
        the database."""
        with self._lock:
            return {
                "key_available": self.key is not None,
                "key_id": self.key.key_id if self.key else None,
                "published": self._published,
                "loaded": self._loaded,
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
