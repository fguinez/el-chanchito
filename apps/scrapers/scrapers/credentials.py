"""Open the scraper credentials the dashboard seals, and lay them over the env.

The scraper service owns an HPKE key pair (RFC 9180, base mode). The private
key lives in an owner-only file (CREDENTIALS_KEY_FILE, default
apps/scrapers/.credentials_key) and never leaves this process; the public key
is published to the database, where the dashboard reads it to seal each
credential field. The dashboard can seal but never open, and the database
alone opens nothing.

The key decides the suite, and the suite's scheme is stored on every row:

- v1, `hpke-x25519-sha256-aes256gcm-v1`: DHKEM(X25519, HKDF-SHA256),
  HKDF-SHA256, AES-256-GCM. Keys made before v2 keep working until they are
  rotated.
- v2, `hpke-mlkem768x25519-sha256-aes256gcm-v2`: the hybrid post-quantum KEM
  ML-KEM-768 + X25519 (X-Wing, HPKE KEM 0x647a), HKDF-SHA256, AES-256-GCM.
  The private key is a 32-byte X-Wing seed. New keys are v2, so rotating a
  v1 key re-seals every row to v2.

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

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import hpke, serialization
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from product_model import CREDENTIAL_SPECS, CREDENTIALS_BY_SLUG

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeyKind:
    """One version of the whole construction (suite, info layout, plaintext
    encoding). `scheme` is stored on every row and `algorithm` is published
    next to the public key; the dashboard has the same pairs."""

    scheme: str
    algorithm: str
    suite: hpke.Suite
    public_key_length: int


V1 = KeyKind(
    scheme="hpke-x25519-sha256-aes256gcm-v1",
    algorithm="HPKE-Base(DHKEM-X25519-HKDF-SHA256, HKDF-SHA256, AES-256-GCM)",
    suite=hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM),
    public_key_length=32,
)
# X-Wing (draft-connolly-cfrg-xwing-kem), HPKE KEM 0x647a
# (draft-ietf-hpke-pq). Public key: ML-KEM-768 (1184 bytes) || X25519 (32).
V2 = KeyKind(
    scheme="hpke-mlkem768x25519-sha256-aes256gcm-v2",
    algorithm="HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)",
    suite=hpke.Suite(hpke.KEM.MLKEM768_X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM),
    public_key_length=1216,
)
KEY_KINDS = (V1, V2)
SCHEMES = frozenset(kind.scheme for kind in KEY_KINDS)
_INFO_LABEL = "el-chanchito/credential"

XWING_SEED_BYTES = 32
# PKCS#8 OneAsymmetricKey { version 0, id-XWing (1.3.6.1.4.1.62253.25722, no
# parameters), privateKey OCTET STRING (the 32-byte seed) }, as DER.
_XWING_OID_DER = bytes.fromhex("060b2b0601040183e62d81c87a")
_XWING_PKCS8_PREFIX = bytes.fromhex("3034020100300d") + _XWING_OID_DER + bytes.fromhex("0420")
_PEM_PRIVATE_KEY_RE = re.compile(
    rb"\A-----BEGIN PRIVATE KEY-----\r?\n"
    rb"([A-Za-z0-9+/=\r\n]+?)\r?\n"
    rb"-----END PRIVATE KEY-----\r?\n?\Z"
)

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
    """The id of a public key: the hex SHA-256 of its raw bytes (32 for v1,
    1216 for v2)."""
    return hashlib.sha256(public_raw).hexdigest()


def binding_digest(values: Iterable[str]) -> str:
    """Hex SHA-256 of the values a secret is sealed to (e.g. IMAP host and
    user), length-prefixed so no two lists collide."""
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big") + encoded)
    return digest.hexdigest()


def sealing_info(
    slug: str, field_key: str, key_id: str, binding: str | None = None, *, scheme: str
) -> bytes:
    """The HPKE `info` that binds a ciphertext to where it belongs.

    The dashboard builds the same bytes (apps/web/src/lib/credentials/seal.ts).
    The parts are NUL-separated; slugs and keys are `[a-z][a-z0-9_]*` and the
    key id and binding are hex, so the layout is unambiguous. `scheme` is the
    scheme of the key sealed to. A secret that binds other fields (registry
    `binds`) adds `binding_digest` of their values, so it only opens next to
    the destination it was saved with.
    """
    if scheme not in SCHEMES:
        raise CredentialOpenError("unknown credential scheme")
    if not _NAME_RE.match(slug) or not _NAME_RE.match(field_key):
        raise CredentialOpenError("malformed credential slug or field key")
    if not _KEY_ID_RE.match(key_id):
        raise CredentialOpenError("malformed credential key id")
    parts = [_INFO_LABEL, scheme, slug, field_key, key_id]
    if binding is not None:
        if not _KEY_ID_RE.match(binding):
            raise CredentialOpenError("malformed credential binding")
        parts.append(f"bind:{binding}")
    return "\x00".join(parts).encode("ascii")


def _xwing_private(seed: bytes) -> tuple[hpke.MLKEM768X25519PrivateKey, bytes]:
    """The HPKE private key and the 1216-byte public key an X-Wing seed
    expands to: SHAKE256(seed, 96) = ML-KEM-768 seed (64) || X25519 key (32)."""
    expanded = hashlib.shake_256(seed).digest(96)
    mlkem = MLKEM768PrivateKey.from_seed_bytes(expanded[:64])
    x25519 = X25519PrivateKey.from_private_bytes(expanded[64:])
    public_raw = mlkem.public_key().public_bytes_raw() + x25519.public_key().public_bytes_raw()
    return hpke.MLKEM768X25519PrivateKey(mlkem, x25519), public_raw


@dataclass(frozen=True)
class CredentialKey:
    """The service's key pair, of either kind. `repr` never shows the
    private half. A v2 key keeps its seed, which is what its file stores."""

    private_key: X25519PrivateKey | hpke.MLKEM768X25519PrivateKey
    public_raw: bytes
    key_id: str
    kind: KeyKind = V1
    seed: bytes | None = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:
        return f"CredentialKey(key_id={self.key_id!r}, scheme={self.scheme!r})"

    @property
    def scheme(self) -> str:
        return self.kind.scheme

    @property
    def algorithm(self) -> str:
        return self.kind.algorithm

    @classmethod
    def from_private(cls, private_key: X25519PrivateKey) -> CredentialKey:
        """A v1 key."""
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return cls(private_key, public_raw, key_id_for(public_raw), V1)

    @classmethod
    def from_seed(cls, seed: bytes) -> CredentialKey:
        """A v2 (X-Wing) key from its 32-byte seed."""
        if len(seed) != XWING_SEED_BYTES:
            raise CredentialKeyError("an X-Wing seed is 32 bytes")
        private_key, public_raw = _xwing_private(bytes(seed))
        return cls(private_key, public_raw, key_id_for(public_raw), V2, bytes(seed))

    def private_pem(self) -> bytes:
        """The PKCS#8 PEM the key file holds."""
        if self.seed is not None:  # v2: the X-Wing PKCS#8 holds the seed
            der = _XWING_PKCS8_PREFIX + self.seed
            body = base64.b64encode(der)
            lines = [body[i : i + 64] for i in range(0, len(body), 64)]
            return (
                b"-----BEGIN PRIVATE KEY-----\n"
                + b"\n".join(lines)
                + b"\n-----END PRIVATE KEY-----\n"
            )
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def seal(self, slug: str, field_key: str, value: str, binding: str | None = None) -> bytes:
        """Seal like the dashboard does (tests and tooling only)."""
        info = sealing_info(slug, field_key, self.key_id, binding, scheme=self.scheme)
        return self.kind.suite.encrypt(
            value.encode("utf-8"),
            self.private_key.public_key(),
            info=info,
        )

    def open(
        self, slug: str, field_key: str, key_id: str, sealed: bytes, binding: str | None = None
    ) -> str:
        """The plaintext of one stored field; CredentialOpenError on any failure."""
        if key_id != self.key_id:
            raise CredentialOpenError("sealed to another key")
        info = sealing_info(slug, field_key, key_id, binding, scheme=self.scheme)
        try:
            plaintext = self.kind.suite.decrypt(
                bytes(sealed),
                self.private_key,
                info=info,
            )
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
    seed = _xwing_seed_from_pem(data, path)
    if seed is not None:
        return CredentialKey.from_seed(seed)
    try:
        private_key = serialization.load_pem_private_key(data, password=None)
    except Exception:
        raise CredentialKeyError(f"{path} is not a PEM private key") from None
    if not isinstance(private_key, X25519PrivateKey):
        raise CredentialKeyError(f"{path} does not hold an X-Wing or X25519 key")
    return CredentialKey.from_private(private_key)


def _xwing_seed_from_pem(data: bytes, path: Path) -> bytes | None:
    """The seed of an X-Wing PKCS#8 PEM; None when the file is some other
    PKCS#8 key (left to `cryptography`). A file that names X-Wing but is not
    exactly the expected encoding raises CredentialKeyError."""
    match = _PEM_PRIVATE_KEY_RE.match(data)
    if match is None:
        return None
    try:
        der = base64.b64decode(re.sub(rb"\r?\n", b"", match.group(1)), validate=True)
    except (binascii.Error, ValueError):
        return None
    if _XWING_OID_DER not in der:
        return None
    if len(der) != len(_XWING_PKCS8_PREFIX) + XWING_SEED_BYTES or not der.startswith(
        _XWING_PKCS8_PREFIX
    ):
        raise CredentialKeyError(f"{path} is a malformed X-Wing key")
    return der[len(_XWING_PKCS8_PREFIX) :]


def generate_key() -> CredentialKey:
    """A new key: always v2 (X-Wing)."""
    return CredentialKey.from_seed(secrets.token_bytes(XWING_SEED_BYTES))


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new_file(path: Path, key: CredentialKey) -> bool:
    """Write `key` to `path` owner-only from the first byte, durably, only if
    nothing is there yet (link() is an atomic create-if-absent). False when
    another file was already in place."""
    pem = key.private_pem()
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
            os.link(tmp, path)
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    _fsync_dir(path.parent)
    return True


def _create_key(path: Path) -> CredentialKey:
    """Write a new key without replacing one another process created
    meanwhile (that one is loaded instead)."""
    key = generate_key()
    if not _write_new_file(path, key):
        return load_key(path)
    logger.info("Created a new credentials key at %s", path)
    return key


def load_or_create_key(path: Path) -> CredentialKey:
    """The service's key, generated on first use. Raises CredentialKeyError
    for a key file that exists but must not be used."""
    try:
        return load_key(path)
    except FileNotFoundError:
        return _create_key(path)


class KeyFile:
    """Where the service's private key lives, and the one seam through which
    it is read or written (a secret manager can replace this class later).

    Rotation writes the new key to `<file>.next` first, then, once the
    database has switched to it, renames it over `<file>`, which deletes the
    old private key. `<file>.lock` serializes that between the service and
    the `make credentials-*` commands.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.next_path = path.with_name(f"{path.name}.next")
        self.lock_path = path.with_name(f"{path.name}.lock")

    def __repr__(self) -> str:
        return f"KeyFile({str(self.path)!r})"

    def load(self) -> CredentialKey:
        return load_key(self.path)

    def load_or_create(self) -> CredentialKey:
        """Under the lock, so the sweep never meets a first-start temp file
        still being written."""
        with self.lock():
            return self.load_or_create_locked()

    def load_or_create_locked(self) -> CredentialKey:
        """`load_or_create` for a caller that already holds the lock (flock
        is per open file, so taking it twice in one process deadlocks)."""
        return load_or_create_key(self.path)

    def set_pending_aside(self) -> Path:
        """Rename `.next` to `.next.aside-<UTC time>` and return that path.
        It still holds a private key; the stale-temp sweep never touches it."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        aside = self.next_path.with_name(
            f"{self.next_path.name}.aside-{stamp}-{secrets.token_hex(4)}"
        )
        os.link(self.next_path, aside)  # never clobbers an earlier aside file
        self.next_path.unlink()
        _fsync_dir(self.path.parent)
        return aside

    def load_pending(self) -> CredentialKey | None:
        """The key a rotation left in `.next`; None when there is none. An
        unusable one raises CredentialKeyError."""
        try:
            return load_key(self.next_path)
        except FileNotFoundError:
            return None

    def write_pending(self, key: CredentialKey) -> None:
        if not _write_new_file(self.next_path, key):
            raise CredentialKeyError(f"{self.next_path} already exists")

    def promote_pending(self) -> None:
        """Make `.next` the key file; the old private key is gone after this."""
        os.replace(self.next_path, self.path)
        _fsync_dir(self.path.parent)

    def discard_pending(self) -> None:
        try:
            os.unlink(self.next_path)
        except FileNotFoundError:
            return
        _fsync_dir(self.path.parent)

    def replace(self, key: CredentialKey) -> None:
        """Make `key` the key file through `.next`, so a crash leaves either
        file whole."""
        self.discard_pending()
        self.write_pending(key)
        self.promote_pending()

    def sweep_stale_tmp(self) -> int:
        """Delete key temp files a killed writer left behind (each holds a
        private key, or is a second link to one that rotation would then
        never delete). Call it with the lock held: every key write happens
        under it, so any temp file found is stale. `.aside-*` files are not
        temps and are left alone."""
        removed = 0
        for tmp in self.path.parent.glob(f"{self.path.name}.*.tmp"):
            try:
                tmp.unlink()
                removed += 1
            except FileNotFoundError:
                continue
        if removed:
            _fsync_dir(self.path.parent)
            logger.warning("Deleted %d stale credentials key temp file(s)", removed)
        return removed

    @property
    def locking_available(self) -> bool:
        """Whether `lock()` really excludes other processes (not on native
        Windows, which has no flock; WSL2 does)."""
        try:
            import fcntl  # noqa: F401
        except ImportError:
            return False
        return True

    @contextmanager
    def lock(self) -> Iterator[None]:
        """An exclusive lock on `<file>.lock` (no-op where flock is missing)."""
        try:
            import fcntl
        except ImportError:  # Windows: one process per key file is assumed
            yield
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


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
    """What a set of rows yields: values by env var, the env vars whose row
    exists but cannot be used (to be entered again), and the env vars that
    must be unset even though the environment has a value: a binding secret
    whose destination came from the store but which was not sealed to it."""

    values: dict[str, str]
    needs_reentry: frozenset[str]
    suppressed: frozenset[str] = frozenset()

    def __repr__(self) -> str:
        return (
            f"OpenedCredentials(stored={sorted(self.values)}, "
            f"needs_reentry={sorted(self.needs_reentry)}, suppressed={sorted(self.suppressed)})"
        )


# C0, DEL and C1 controls never belong in a login (the dashboard refuses them).
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


def _value_problem(field, value: str, fallback: Mapping[str, str | None]) -> str | None:
    if _CONTROL_RE.search(value):
        return "has control characters"
    if (
        field.allowed_values
        and value.lower() not in {v.lower() for v in field.allowed_values}
        and not _truthy(fallback.get(field.allow_custom_env))
    ):
        return f"is not a known value (set {field.allow_custom_env}=1 to allow it)"
    return None


def open_rows(
    key: CredentialKey | None,
    rows: Iterable[SealedCredential],
    fallback: Mapping[str, str | None] | None = None,
) -> OpenedCredentials:
    """Open every row the registry knows.

    `fallback` is the environment the stored values are laid over: it decides
    the custom-value opt-outs and stands in for bound fields with no row.
    Without a usable key nothing is opened and nothing is flagged: the rows
    may be fine once the key file is fixed.
    """
    fallback = fallback or {}
    if key is None:
        return OpenedCredentials({}, frozenset())
    values: dict[str, str] = {}
    reentry: set[str] = set()
    suppressed: set[str] = set()
    by_field: dict[tuple[str, str], SealedCredential] = {}

    def flag(row: SealedCredential, env: str, why: str) -> None:
        logger.warning(
            "Stored credential %s.%s %s; enter it again", row.institution_slug, row.field_key, why
        )
        reentry.add(env)

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
        if row.scheme != key.scheme or row.key_id != key.key_id:
            flag(row, field.env_var, "was sealed to another key")
            continue
        by_field[(row.institution_slug, row.field_key)] = row

    # Plain fields first: binding secrets are opened against their values.
    for spec in CREDENTIAL_SPECS:
        for field in spec.fields:
            row = by_field.get((spec.slug, field.key))
            if row is None or field.binds:
                continue
            try:
                value = key.open(row.institution_slug, row.field_key, row.key_id, row.sealed)
            except CredentialOpenError:
                flag(row, field.env_var, "could not be opened")
                continue
            problem = _value_problem(field, value, fallback)
            if problem:
                flag(row, field.env_var, problem)
                continue
            values[field.env_var] = value

    for spec in CREDENTIAL_SPECS:
        fields = {f.key: f for f in spec.fields}
        for field in spec.fields:
            if not field.binds:
                continue
            bound = [fields[k] for k in field.binds]
            destination_stored = any(b.env_var in values for b in bound)
            effective = [values.get(b.env_var, fallback.get(b.env_var) or "") for b in bound]
            row = by_field.get((spec.slug, field.key))
            opened = False
            if row is not None:
                try:
                    value = key.open(
                        row.institution_slug,
                        row.field_key,
                        row.key_id,
                        row.sealed,
                        binding_digest(effective),
                    )
                    if _CONTROL_RE.search(value):
                        raise CredentialOpenError("control characters")
                    values[field.env_var] = value
                    opened = True
                except CredentialOpenError:
                    flag(
                        row,
                        field.env_var,
                        f"was not sealed to the {'/'.join(field.binds)} it would be used with",
                    )
            if not opened and destination_stored:
                # Never send another secret to a destination the store chose.
                logger.warning(
                    "%s.%s is stored without a matching %s; %s stays unset",
                    spec.slug,
                    "/".join(field.binds),
                    field.key,
                    field.env_var,
                )
                suppressed.add(field.env_var)
                reentry.add(field.env_var)
    return OpenedCredentials(values, frozenset(reentry), frozenset(suppressed))


@dataclass(frozen=True)
class ResealedCredential:
    """A row re-sealed to a new key, under that key's `scheme`."""

    institution_slug: str
    field_key: str
    scheme: str
    sealed: bytes

    def __repr__(self) -> str:
        return f"ResealedCredential({self.institution_slug}.{self.field_key})"


def reseal_rows(
    old: CredentialKey,
    new: CredentialKey,
    rows: Iterable[SealedCredential],
    fallback: Mapping[str, str | None] | None = None,
) -> tuple[list[ResealedCredential], list[SealedCredential]]:
    """Open every row sealed to `old` and seal its value to `new`, in the same
    context (a binding secret keeps its binding) but under `new`'s scheme, so
    a v1 key's rows come out as v2 when `new` is v2. Returns the re-sealed
    rows and the ones left as they were: sealed to another key or scheme, or
    not openable (they already needed re-entry)."""
    fallback = fallback or {}
    rows = list(rows)
    plain: dict[tuple[str, str], str] = {}
    resealed: list[ResealedCredential] = []
    skipped: list[SealedCredential] = []

    def field_of(row: SealedCredential):
        spec = CREDENTIALS_BY_SLUG.get(row.institution_slug)
        return next((f for f in spec.fields if f.key == row.field_key), None) if spec else None

    def reseal(row: SealedCredential, binding: str | None) -> None:
        value = old.open(row.institution_slug, row.field_key, row.key_id, row.sealed, binding)
        plain[(row.institution_slug, row.field_key)] = value
        resealed.append(
            ResealedCredential(
                row.institution_slug,
                row.field_key,
                new.scheme,
                new.seal(row.institution_slug, row.field_key, value, binding),
            )
        )

    ours = [r for r in rows if r.scheme == old.scheme and r.key_id == old.key_id]
    skipped.extend(r for r in rows if r not in ours)
    binding_rows = []
    for row in ours:
        field = field_of(row)
        if field is not None and field.binds:
            binding_rows.append((row, field))
            continue
        try:
            reseal(row, None)
        except CredentialOpenError:
            skipped.append(row)
    for row, field in binding_rows:
        spec = CREDENTIALS_BY_SLUG[row.institution_slug]
        bound = [next(f for f in spec.fields if f.key == k) for k in field.binds]
        values = [
            plain.get((row.institution_slug, b.key), fallback.get(b.env_var) or "") for b in bound
        ]
        try:
            reseal(row, binding_digest(values))
        except CredentialOpenError:
            skipped.append(row)
    for row in skipped:
        logger.warning(
            "Stored credential %s.%s was not re-sealed (it could not be opened); it needs re-entry",
            row.institution_slug,
            row.field_key,
        )
    return resealed, skipped


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
        names |= {
            f.allow_custom_env
            for spec in CREDENTIAL_SPECS
            for f in spec.fields
            if f.allow_custom_env
        }
        self._baseline = {name: self._environ.get(name) for name in names}
        self._opened = OpenedCredentials({}, frozenset())

    def apply(self, opened: OpenedCredentials) -> None:
        self._opened = opened
        for env, _legacy in self._fields:
            value = (
                None if env in opened.suppressed else opened.values.get(env, self._baseline[env])
            )
            if value is None:
                self._environ.pop(env, None)
            else:
                self._environ[env] = value

    @property
    def baseline(self) -> dict[str, str | None]:
        """The environment as it was before any stored value (a copy)."""
        return dict(self._baseline)

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
