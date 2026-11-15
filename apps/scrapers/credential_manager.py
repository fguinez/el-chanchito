"""Wire the sealed credentials into the scraper service.

`CredentialManager` owns the service's key and the env overlay: on start it
loads (or creates) the key file, publishes the public key to the database for
the dashboard, and lays every stored credential it can open over
`os.environ`; `reload()` does the last step again after the dashboard saves
or deletes one. See scrapers/credentials.py for the scheme.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, MutableMapping
from pathlib import Path

from db.connection import close_pool
from db.credentials import fetch_sealed_credentials, publish_public_key
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
    ) -> None:
        self.key_path = key_path or key_file_path()
        # Snapshots the environment as it is now: the fallback for every field.
        self.overlay = CredentialOverlay(environ)
        self.key: CredentialKey | None = None
        self._fetch = fetch
        self._publish = publish
        self._published = False
        self._loaded = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Load the key, publish it and apply the stored credentials. Never
        raises: without a key or a database the env fallback still works."""
        try:
            self.key = load_or_create_key(self.key_path)
        except (CredentialKeyError, OSError) as exc:
            logger.error(
                "Stored credentials are disabled (%s); only the secret store and env are used",
                exc,
            )
            return
        self.reload()

    def _publish_key(self) -> None:
        assert self.key is not None
        try:
            replaced, orphaned = self._publish(self.key)
        except Exception as exc:
            logger.warning("Could not publish the credentials key yet: %s", type(exc).__name__)
            return
        self._published = True
        if replaced:
            logger.warning(
                "The credentials key changed (was %s, now %s): the dashboard now seals to %s's key",
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

    def reload(self) -> bool:
        """Read the rows again and re-apply the overlay; False when the rows
        could not be read (the previous overlay stays)."""
        with self._lock:
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
            opened = open_rows(self.key, rows)
            self.overlay.apply(opened)
            self._loaded = True
            if opened.values:
                logger.info("Applied %d stored credential field(s)", len(opened.values))
            return True

    def status(self) -> dict:
        """Booleans and the public key id only; never a value."""
        with self._lock:
            return {
                "key_available": self.key is not None,
                "key_id": self.key.key_id if self.key else None,
                "loaded": self._loaded,
                "fields": self.overlay.status(),
            }


def apply_stored_credentials(environ: MutableMapping[str, str] | None = None) -> None:
    """For the manual login CLIs: lay the stored credentials over the env when
    a key file and a database are there. Best effort, never creates a key."""
    if not os.environ.get("DATABASE_URL"):
        return
    try:
        key = load_key(key_file_path())
    except FileNotFoundError:
        return
    except CredentialKeyError as exc:
        logger.warning("Stored credentials not used: %s", exc)
        return
    try:
        rows = fetch_sealed_credentials()
    except Exception as exc:
        logger.warning("Could not read stored credentials: %s", type(exc).__name__)
        return
    finally:
        close_pool()
    overlay = CredentialOverlay(environ)
    overlay.apply(open_rows(key, rows))
