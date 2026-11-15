"""Tie a cached web session to the account that created it.

A session cache (Fintual cookies, BanChile storage state) belongs to one
account. When the identifier (RUT, e-mail) changes, from the dashboard or in
the env across restarts, the old session must not be reused, and a run still
holding the old one may write it back after it was deleted. So every save
writes `<session>.account` next to it (0600) with a salted SHA-256 of the
identifier, never the identifier itself, and every load checks it. The
identifier is normalized first (`normalize`), so a RUT typed with or without
dots, or an e-mail in another case, is still the same account.

A session with no marker (cached before markers existed) is discarded, not
adopted: nothing records which account saved it. Comparing the identifier
with the env or secret-store value would not prove it either, since before
markers that value could change across restarts with the session kept. The
cost is one sign-in after upgrading: BanChile signs in again with its clave,
Fintual mails a code (read from the inbox when IMAP is the same account, else
the run asks for a sign-in).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_LABEL = b"el-chanchito/session-account/v1"


def marker_path(session: Path) -> Path:
    return session.with_name(session.name + ".account")


# A RUT as people type it: digits with optional dots or spaces, an optional
# hyphen, then the check digit (0-9 or K).
_RUT_RE = re.compile(r"^[\d.\s]+-?\s*[\dkK]$")


def normalize(identifier: str) -> str:
    """The canonical form of an account identifier: an e-mail stripped and
    lowercased, a RUT as `<digits>-<check digit>` with an uppercase K (no dots
    or spaces, leading zeros dropped), anything else stripped."""
    value = identifier.strip()
    if "@" in value:
        return value.lower()
    if _RUT_RE.match(value):
        compact = re.sub(r"[.\s-]", "", value).upper()
        body, check = compact[:-1].lstrip("0"), compact[-1]
        if body:
            return f"{body}-{check}"
    return value


def _hash(salt: bytes, value: str) -> str:
    encoded = value.encode("utf-8")
    return hashlib.sha256(_LABEL + salt + len(encoded).to_bytes(4, "big") + encoded).hexdigest()


def _digest(salt: bytes, identifier: str) -> str:
    return _hash(salt, normalize(identifier))


def _legacy_digest(salt: bytes, identifier: str) -> str:
    """The digest markers had before identifiers were normalized."""
    return _hash(salt, identifier.strip())


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
    """False when `session`'s marker names another account, is corrupt, or
    is missing while the session exists (see the module docstring)."""
    marker = marker_path(session)
    try:
        salt_hex, digest = marker.read_text().strip().split(":", 1)
        salt = bytes.fromhex(salt_hex)
    except FileNotFoundError:
        if not session.exists():
            return True
        logger.info("%s has no account marker; it is not reused", session.name)
        return False
    except (OSError, ValueError):
        return False
    if hmac.compare_digest(digest, _digest(salt, identifier)):
        return True
    if hmac.compare_digest(digest, _legacy_digest(salt, identifier)):
        # Written before normalization: same account, so move it to the new form.
        write_account(session, identifier)
        return True
    return False


def discard(session: Path, *, directory: bool = False) -> None:
    """Delete a session and its marker; best effort. `directory` is for a
    session that is a whole directory (a browser profile); otherwise a
    directory found at the session path is left alone."""
    for path in (session, marker_path(session)):
        try:
            if directory and path is session and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete %s", path.name)
