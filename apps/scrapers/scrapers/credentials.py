"""Open the scraper credentials the dashboard seals, and lay them over the env.

The scraper service owns an X25519 key pair. The private key lives in an
owner-only file (CREDENTIALS_KEY_FILE, default apps/scrapers/.credentials_key)
and never leaves this process; the public key is published to the database,
where the dashboard reads it to seal each credential field with HPKE
(RFC 9180, base mode): DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, AES-256-GCM.
The dashboard can seal but never open, and the database alone opens nothing.

Each ciphertext is bound to its context through the HPKE `info` string
(scheme, institution slug, field key, key id), so a row copied onto another
field or institution, or sealed to another key, fails to open.

Stored values are laid over `os.environ` under each field's env var, and win
over what the process started with (the secret store, .env, the shell); a
field with no stored value falls back to that original environment, which is
snapshotted once so deleting a stored field restores it.

Nothing here logs, returns or raises with a credential value: messages name
the institution slug and field key only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import stat
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hpke, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from product_model import CREDENTIAL_SPECS, CREDENTIALS_BY_SLUG

logger = logging.getLogger(__name__)

# The version of the whole construction below (suite, info layout, plaintext
# encoding). Stored on every row; the dashboard writes the same value.
SCHEME = "hpke-x25519-sha256-aes256gcm-v1"
# Published next to the public key so the dashboard can refuse a key it
# would seal to with the wrong suite.
ALGORITHM = "HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)"
_INFO_LABEL = "el-chanchito/credential"
_SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)

KEY_FILE_ENV = "CREDENTIALS_KEY_FILE"
# apps/scrapers/, resolved from this file so cwd doesn't matter.
_APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_KEY_FILE = _APP_DIR / ".credentials_key"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_KEY_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class CredentialKeyError(Exception):
    """The key file is missing, unreadable, malformed or not owner-only."""


class CredentialOpenError(Exception):
    """A sealed credential could not be opened (wrong key, context or data)."""


def key_id_for(public_raw: bytes) -> str:
    """The id of a public key: the hex SHA-256 of its 32 raw bytes."""
    return hashlib.sha256(public_raw).hexdigest()


def sealing_info(slug: str, field_key: str, key_id: str) -> bytes:
    """The HPKE `info` that binds a ciphertext to where it belongs.

    The dashboard builds the same bytes (apps/web/src/lib/credentials/seal.ts).
    The parts are NUL-separated; slugs and keys are `[a-z][a-z0-9_]*` and the
    key id is hex, so the layout is unambiguous.
    """
    if not _NAME_RE.match(slug) or not _NAME_RE.match(field_key):
        raise CredentialOpenError("malformed credential slug or field key")
    if not _KEY_ID_RE.match(key_id):
        raise CredentialOpenError("malformed credential key id")
    return "\x00".join((_INFO_LABEL, SCHEME, slug, field_key, key_id)).encode("ascii")


@dataclass(frozen=True)
class CredentialKey:
    """The service's key pair. `repr` never shows the private half."""

    private_key: X25519PrivateKey
    public_raw: bytes
    key_id: str

    def __repr__(self) -> str:
        return f"CredentialKey(key_id={self.key_id!r})"

    @classmethod
    def from_private(cls, private_key: X25519PrivateKey) -> CredentialKey:
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return cls(private_key, public_raw, key_id_for(public_raw))

    def seal(self, slug: str, field_key: str, value: str) -> bytes:
        """Seal like the dashboard does (tests and tooling only)."""
        info = sealing_info(slug, field_key, self.key_id)
        return _SUITE.encrypt(value.encode("utf-8"), self.private_key.public_key(), info=info)

    def open(self, slug: str, field_key: str, key_id: str, sealed: bytes) -> str:
        """The plaintext of one stored field; CredentialOpenError on any failure."""
        if key_id != self.key_id:
            raise CredentialOpenError("sealed to another key")
        info = sealing_info(slug, field_key, key_id)
        try:
            plaintext = _SUITE.decrypt(bytes(sealed), self.private_key, info=info)
            return plaintext.decode("utf-8")
        except Exception as exc:  # InvalidTag, short input, bad UTF-8
            raise CredentialOpenError(type(exc).__name__) from None


# --- Key file -------------------------------------------------------------------


def key_file_path(environ: Mapping[str, str] = os.environ) -> Path:
    override = (environ.get(KEY_FILE_ENV) or "").strip()
    return Path(override) if override else DEFAULT_KEY_FILE


def _check_owner_only(st: os.stat_result, path: Path) -> None:
    if os.name == "nt":  # no POSIX modes; the file inherits the folder's ACL
        return
    if not stat.S_ISREG(st.st_mode):
        raise CredentialKeyError(f"{path} is not a regular file")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise CredentialKeyError(
            f"{path} is readable by other users (mode {mode:04o}); refusing to use it. "
            f"Run: chmod 600 {path}"
        )
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise CredentialKeyError(f"{path} is owned by another user; refusing to use it")


def load_key(path: Path) -> CredentialKey:
    """Read an existing key file; FileNotFoundError when there is none."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP on a symlink, EACCES
        raise CredentialKeyError(f"cannot open {path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as handle:
        _check_owner_only(os.fstat(handle.fileno()), path)
        data = handle.read(64 * 1024)
    try:
        private_key = serialization.load_pem_private_key(data, password=None)
    except Exception:
        raise CredentialKeyError(f"{path} is not a PEM private key") from None
    if not isinstance(private_key, X25519PrivateKey):
        raise CredentialKeyError(f"{path} does not hold an X25519 key")
    return CredentialKey.from_private(private_key)


def _create_key(path: Path) -> CredentialKey:
    """Write a new key, owner-only from the first byte, without replacing one
    another process created meanwhile (that one is loaded instead)."""
    private_key = X25519PrivateKey.generate()
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # link() fails if the target exists: an atomic create-if-absent.
            os.link(tmp, path)
        except FileExistsError:
            return load_key(path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    logger.info("Created a new credentials key at %s", path)
    return CredentialKey.from_private(private_key)


def load_or_create_key(path: Path) -> CredentialKey:
    """The service's key, generated on first use. Raises CredentialKeyError
    for a key file that exists but must not be used."""
    try:
        return load_key(path)
    except FileNotFoundError:
        return _create_key(path)


# --- Stored rows ----------------------------------------------------------------


@dataclass(frozen=True)
class SealedCredential:
    """One `institution_credentials` row."""

    institution_slug: str
    field_key: str
    scheme: str
    key_id: str
    sealed: bytes

    def __repr__(self) -> str:
        return f"SealedCredential({self.institution_slug}.{self.field_key})"


@dataclass(frozen=True)
class OpenedCredentials:
    """What a set of rows yields: values by env var, and the env vars whose
    row exists but cannot be opened (to be entered again)."""

    values: dict[str, str]
    needs_reentry: frozenset[str]

    def __repr__(self) -> str:
        return (
            f"OpenedCredentials(stored={sorted(self.values)}, "
            f"needs_reentry={sorted(self.needs_reentry)})"
        )


def open_rows(key: CredentialKey | None, rows: Iterable[SealedCredential]) -> OpenedCredentials:
    """Open every row the registry knows. Without a usable key nothing is
    opened, and nothing is flagged either: the rows may be fine once the key
    file is fixed."""
    values: dict[str, str] = {}
    reentry: set[str] = set()
    if key is None:
        return OpenedCredentials({}, frozenset())
    for row in rows:
        spec = CREDENTIALS_BY_SLUG.get(row.institution_slug)
        field = next((f for f in spec.fields if f.key == row.field_key), None) if spec else None
        if field is None:
            logger.warning(
                "Stored credential %s.%s is not in the registry; ignored",
                row.institution_slug,
                row.field_key,
            )
            continue
        if row.scheme != SCHEME or row.key_id != key.key_id:
            logger.warning(
                "Stored credential %s.%s was sealed to another key; enter it again",
                row.institution_slug,
                row.field_key,
            )
            reentry.add(field.env_var)
            continue
        try:
            values[field.env_var] = key.open(
                row.institution_slug, row.field_key, row.key_id, row.sealed
            )
        except CredentialOpenError:
            logger.warning(
                "Stored credential %s.%s could not be opened; enter it again",
                row.institution_slug,
                row.field_key,
            )
            reentry.add(field.env_var)
    return OpenedCredentials(values, frozenset(reentry))


# --- Environment overlay --------------------------------------------------------


def _registry_env() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """(env var, legacy fallback names) for every registry field."""
    return tuple((f.env_var, f.fallback_env_vars) for spec in CREDENTIAL_SPECS for f in spec.fields)


class CredentialOverlay:
    """Lays stored values over an environment and can take them back off.

    The environment as it was at construction is the fallback: a field with
    no stored value gets its original value back (or is removed if it had
    none), so deleting a stored credential restores the secret store's.
    """

    def __init__(
        self,
        environ: MutableMapping[str, str] | None = None,
        fields: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._fields = _registry_env() if fields is None else fields
        names = {name for env, legacy in self._fields for name in (env, *legacy)}
        self._baseline = {name: self._environ.get(name) for name in names}
        self._opened = OpenedCredentials({}, frozenset())

    def apply(self, opened: OpenedCredentials) -> None:
        self._opened = opened
        for env, _legacy in self._fields:
            value = opened.values.get(env, self._baseline[env])
            if value is None:
                self._environ.pop(env, None)
            else:
                self._environ[env] = value

    def status(self) -> dict[str, dict[str, bool]]:
        """Per env var, booleans only: a fallback value exists, a stored one
        was opened, a stored one needs re-entry."""
        return {
            env: {
                "fallback": any(self._baseline[name] for name in (env, *legacy)),
                "stored": env in self._opened.values,
                "needs_reentry": env in self._opened.needs_reentry,
            }
            for env, legacy in self._fields
        }


# Per process, so a fingerprint is useless outside it.
_FINGERPRINT_KEY = secrets.token_bytes(32)


def env_fingerprint(env_vars: Iterable[str], environ: Mapping[str, str] = os.environ) -> str:
    """A keyed digest of the current values of `env_vars`, to tell whether
    they changed without keeping the values around."""
    mac = hmac.new(_FINGERPRINT_KEY, digestmod=hashlib.sha256)
    for name in env_vars:
        value = environ.get(name)
        encoded = b"" if value is None else value.encode("utf-8")
        mac.update(b"-" if value is None else b"+")
        mac.update(len(encoded).to_bytes(4, "big") + encoded)
    return mac.hexdigest()
