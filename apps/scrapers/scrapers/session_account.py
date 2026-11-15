"""Tie a cached web session to the account that created it.

A session cache (Fintual cookies, BanChile storage state) belongs to one
account. When the identifier (RUT, e-mail) changes, from the dashboard or in
the env across restarts, the old session must not be reused, and a run still
holding the old one may write it back after it was deleted. So every save
writes `<session>.account` next to it (0600) with a salted SHA-256 of the
identifier, never the identifier itself, and every load checks it.

A session with no marker yet (cached before markers existed) is adopted for
the current account rather than discarded, so upgrading does not force a new
sign-in (a Fintual one needs an e-mailed code).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_LABEL = b"el-chanchito/session-account/v1"


def marker_path(session: Path) -> Path:
    return session.with_name(session.name + ".account")


def _digest(salt: bytes, identifier: str) -> str:
    encoded = identifier.strip().encode("utf-8")
    return hashlib.sha256(_LABEL + salt + len(encoded).to_bytes(4, "big") + encoded).hexdigest()


def write_account(session: Path, identifier: str) -> None:
    """Record which account `session` belongs to; best effort, never raises."""
    marker = marker_path(session)
    salt = secrets.token_bytes(16)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=marker.parent, prefix=marker.name + ".")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{salt.hex()}:{_digest(salt, identifier)}\n")
        os.replace(tmp, marker)
    except OSError as exc:
        logger.warning("Could not record the session's account (%s)", type(exc).__name__)
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)


def belongs_to(session: Path, identifier: str) -> bool:
    """False when `session`'s marker names another account (or is corrupt).
    A missing marker adopts the session for `identifier`."""
    marker = marker_path(session)
    try:
        salt_hex, digest = marker.read_text().strip().split(":", 1)
        salt = bytes.fromhex(salt_hex)
    except FileNotFoundError:
        if session.exists():
            write_account(session, identifier)
        return True
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(digest, _digest(salt, identifier))


def discard(session: Path) -> None:
    """Delete a session and its marker; best effort."""
    for path in (session, marker_path(session)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete %s", path.name)
