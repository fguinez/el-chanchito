#!/usr/bin/env python3
"""Cross-platform store for El Chanchito's scraper secrets.

Usage (from the repo root; the make targets wrap the first three):

    python scripts/secret_store.py init           # prompt for each missing secret
    python scripts/secret_store.py set KEY        # store or overwrite one secret
    python scripts/secret_store.py status         # which secrets are stored
    python scripts/secret_store.py export [KEY..] # `export VAR='value'` lines for eval

Only `export` ever prints a value, as shell-quoted lines meant for `eval`
(scripts/load-secrets.sh). Every item is named `chanchito.<VAR>` with the
current user as the account, the same names the macOS Keychain helpers have
always used, so existing items keep working with no migration.

Backends, picked in this order unless CHANCHITO_SECRETS_BACKEND forces one
(`keychain`, `keyring` or `file`):

* keychain: macOS only, the login Keychain through the `security` CLI. It is
  preferred over the keyring package on macOS because items created by
  `security` trust only that binary: reading them through Python's Security
  API would raise a Keychain access prompt, which blocks unattended runs.
* keyring: the `keyring` package with a usable system backend: the Secret
  Service on a Linux desktop (only while its collection is unlocked), or the
  Windows Credential Manager if this ever runs natively there.
* file: a dotenv file, `local/secrets.env` (gitignored) or
  CHANCHITO_SECRETS_FILE, written owner-only (0600). For headless hosts such
  as a Raspberry Pi, where no system keyring is usable.

Stdlib only, plus `keyring` when it is installed.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Optional

SERVICE_PREFIX = "chanchito."

# Prompted by `init` and listed by `status`.
SECRET_KEYS: tuple[str, ...] = (
    "BANCHILE_PASSWORD",
    "LIDER_BCI_PASSWORD",
    "SANTANDER_PASSWORD",
    "BANCO_FALABELLA_PASSWORD",
    "FINTUAL_PASSWORD",
    "BUDA_API_KEY",
    "BUDA_API_SECRET",
    "SKIPO_API_KEY",
    "EMAIL_IMAP_PASSWORD",
    "DASHBOARD_PASSWORD",
)

# Exported by a bare `export` (what load-secrets.sh puts in the environment).
# DASHBOARD_PASSWORD is left out on purpose: exporting it would turn the login
# on under `make dev` (see USAGE.md > Authentication); ask for it by name
# (`export DASHBOARD_PASSWORD`) when deploying. FINTUAL_TOKEN is the legacy
# name for FINTUAL_PASSWORD: still loaded, never prompted for.
NOT_EXPORTED_BY_DEFAULT = frozenset({"DASHBOARD_PASSWORD"})
LEGACY_KEYS: tuple[str, ...] = ("FINTUAL_TOKEN",)
EXPORT_KEYS: tuple[str, ...] = tuple(
    key for key in SECRET_KEYS if key not in NOT_EXPORTED_BY_DEFAULT
) + LEGACY_KEYS
KNOWN_KEYS = frozenset(SECRET_KEYS + LEGACY_KEYS)

BACKEND_ENV = "CHANCHITO_SECRETS_BACKEND"
FILE_ENV = "CHANCHITO_SECRETS_FILE"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS_FILE = REPO_ROOT / "local" / "secrets.env"

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretStoreError(Exception):
    """A backend could not be used or a value could not be stored."""


def _warn(message: str) -> None:
    print(f"!!  {message}", file=sys.stderr)


def current_user(env: Mapping[str, str] = os.environ) -> str:
    """The Keychain account the items use: $USER, else the login name."""
    user = env.get("USER")
    if user:
        return user
    try:
        return getpass.getuser()
    except Exception as exc:  # no USER/LOGNAME/USERNAME and no pwd entry
        raise SecretStoreError("Could not determine the current user; set USER.") from exc


# --- Backends -----------------------------------------------------------------


class Backend:
    name = "backend"
    # Whether an empty answer at the prompt skips the key.
    empty_skips = True

    def describe(self) -> str:
        return self.name

    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def has(self, key: str) -> bool:
        return bool(self.get(key))

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError

    def store_interactive(self, key: str, prompt: Callable[[str], Optional[str]]) -> bool:
        """Ask for `key` with hidden input and store it; False when skipped."""
        value = prompt(key)
        if not value:
            return False
        self.set(key, value)
        return True


class KeychainBackend(Backend):
    """macOS login Keychain via the `security` CLI (the historical store)."""

    name = "keychain"
    empty_skips = False

    def __init__(self, user: str, runner=subprocess.run) -> None:
        self.user = user
        self._run = runner

    def describe(self) -> str:
        return "macOS Keychain (security CLI)"

    def _find(self, key: str, with_value: bool):
        args = ["security", "find-generic-password", "-a", self.user, "-s", SERVICE_PREFIX + key]
        if with_value:
            args.append("-w")
        return self._run(args, capture_output=True, text=True)

    def get(self, key: str) -> Optional[str]:
        result = self._find(key, with_value=True)
        if result.returncode != 0:
            return None
        # `security -w` ends the value with a newline; `$(...)` used to strip it.
        return result.stdout.rstrip("\n") or None

    def has(self, key: str) -> bool:
        # Without -w only the attributes are read: no access prompt.
        return self._find(key, with_value=False).returncode == 0

    def store_interactive(self, key: str, prompt: Callable[[str], Optional[str]]) -> bool:
        # A trailing bare -w makes `security` prompt (hidden, twice) itself, so
        # the value never sits in this process or on a command line.
        result = self._run(
            ["security", "add-generic-password", "-U", "-a", self.user,
             "-s", SERVICE_PREFIX + key, "-w"],
        )
        if result.returncode != 0:
            raise SecretStoreError(f"security could not store {key} (exit {result.returncode}).")
        return True


class KeyringBackend(Backend):
    """The `keyring` package: the Secret Service on Linux, among others."""

    name = "keyring"

    def __init__(self, module, user: str) -> None:
        self._keyring = module
        self.user = user

    def describe(self) -> str:
        try:
            backend = self._keyring.get_keyring()
            return f"system keyring ({type(backend).__module__}.{type(backend).__name__})"
        except Exception:
            return "system keyring"

    def get(self, key: str) -> Optional[str]:
        try:
            return self._keyring.get_password(SERVICE_PREFIX + key, self.user) or None
        except Exception as exc:
            raise SecretStoreError(f"keyring could not read {key}: {type(exc).__name__}") from exc

    def set(self, key: str, value: str) -> None:
        try:
            self._keyring.set_password(SERVICE_PREFIX + key, self.user, value)
        except Exception as exc:
            raise SecretStoreError(f"keyring could not store {key}: {type(exc).__name__}") from exc


class FileBackend(Backend):
    """An owner-only dotenv file (KEY=value lines), for hosts with no keyring."""

    name = "file"

    def __init__(self, path: Path, is_windows: bool = os.name == "nt") -> None:
        self.path = Path(path)
        # Set by select_backend when it fell back here on its own.
        self.fallback_reason: Optional[str] = None
        self._is_windows = is_windows
        self._warned = False

    def describe(self) -> str:
        mode = self._loose_mode()
        if mode is not None:
            return f"file {self.path} (mode {mode:04o}, readable by others)"
        return f"file {self.path} (owner-only)"

    def _loose_mode(self) -> Optional[int]:
        """The file's mode when other users have access to it, else None."""
        if self._is_windows:
            return None
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            return None  # written 0600 when it is created
        return mode if mode & 0o077 else None

    def _check_permissions(self) -> None:
        if self._warned:
            return
        mode = self._loose_mode()
        if mode is not None:
            self._warned = True
            _warn(
                f"{self.path} is readable by other users (mode {mode:04o}); "
                f"run: chmod 600 {shlex.quote(str(self.path))}"
            )

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        self._check_permissions()
        # Only "\n" ends a line (newline="" keeps a stray "\r" inside its line,
        # where splitlines() would start a new KEY=value there); a CRLF file
        # written by hand on Windows still parses.
        with open(self.path, encoding="utf-8", newline="") as handle:
            lines = handle.read().split("\n")
        return [line[:-1] if line.endswith("\r") else line for line in lines]

    def read_all(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self._lines():
            parsed = parse_dotenv_line(line)
            if parsed:
                values[parsed[0]] = parsed[1]
        return values

    def get(self, key: str) -> Optional[str]:
        return self.read_all().get(key) or None

    def set(self, key: str, value: str) -> None:
        new_line = f"{key}={format_dotenv_value(value)}"
        lines = self._lines()
        while lines and not lines[-1].strip():
            lines.pop()  # the final newline's empty tail; don't grow blank lines
        replaced = False
        out: list[str] = []
        for line in lines:
            parsed = parse_dotenv_line(line)
            if parsed and parsed[0] == key:
                if not replaced:
                    out.append(new_line)
                    replaced = True
                continue  # drop duplicates of the key
            out.append(line)
        if not replaced:
            if not out:
                out.append("# El Chanchito secrets (scripts/secret_store.py). Owner-only; never commit.")
            out.append(new_line)
        self._write("\n".join(out) + "\n")

    def _write(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write a 0600 temp file next to the target and swap it in, so the file
        # is never briefly readable by others and a crash can't truncate it.
        fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(self.path, 0o600)


# --- dotenv format --------------------------------------------------------------

_SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")


def format_dotenv_value(value: str) -> str:
    """A value as dotenv text: bare when safe, else double-quoted and escaped."""
    if _SAFE_UNQUOTED.match(value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def parse_dotenv_line(line: str) -> Optional[tuple[str, str]]:
    """(key, value) for a `KEY=value` line, None for blanks and comments.

    Accepts an `export ` prefix, single quotes (literal), double quotes (with
    \\\\, \\", \\n and \\r escapes) and bare values (trimmed; ` #` starts a comment).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    key, sep, raw = stripped.partition("=")
    key = key.strip()
    if not sep or not _KEY_RE.match(key):
        return None
    raw = raw.strip()
    if raw.startswith('"'):
        out = []
        i = 1
        while i < len(raw):
            ch = raw[i]
            if ch == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                out.append({"n": "\n", "r": "\r", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        return key, "".join(out)
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return key, raw[1:end] if end != -1 else raw[1:]
    comment = raw.find(" #")
    if comment != -1:
        raw = raw[:comment].rstrip()
    return key, raw


# --- Backend selection ------------------------------------------------------------


def import_keyring():
    """The keyring module, or None when it isn't installed or fails to import."""
    try:
        import keyring  # noqa: PLC0415 (optional dependency)
    except Exception:
        return None
    return keyring


def secret_service_unlocked(backend) -> bool:
    """True when the Secret Service collection keyring would use is unlocked.

    Asks `is_locked()` only, which never prompts (keyring's own
    get_preferred_collection() would pop an unlock dialog). A missing collection
    raises instead of being created, which would prompt too.
    """
    import secretstorage  # noqa: PLC0415 (installed with keyring on Linux)

    bus = secretstorage.dbus_init()
    try:
        name = getattr(backend, "preferred_collection", None)
        collection = secretstorage.Collection(bus, name) if name else secretstorage.Collection(bus)
        return not collection.is_locked()
    finally:
        close = getattr(bus, "close", None)
        if close:
            close()


def _is_secret_service(backend) -> bool:
    return type(backend).__module__ == "keyring.backends.SecretService"


def keyring_unusable_reason(module, probe=secret_service_unlocked) -> Optional[str]:
    """Why keyring can't be used here, or None when it can."""
    if module is None:
        return "the keyring package is not installed"
    try:
        backend = module.get_keyring()
    except Exception:
        return "keyring failed to load a backend"
    backend_module = type(backend).__module__
    if backend_module.startswith(("keyring.backends.fail", "keyring.backends.null")):
        return "keyring found no system keyring"
    members = [backend]
    if type(backend).__name__ == "ChainerBackend":
        members = list(getattr(backend, "backends", None) or [])
        if not members:
            return "keyring found no system keyring"
    for member in members:
        if _is_secret_service(member):
            try:
                unlocked = probe(member)
            except Exception:
                return "the Secret Service could not be checked"
            if not unlocked:
                return "the Secret Service collection is locked"
    return None


def select_backend(
    env: Mapping[str, str] = os.environ,
    platform: str = sys.platform,
    which: Callable[[str], Optional[str]] = shutil.which,
    keyring_loader: Callable[[], object] = import_keyring,
    probe: Callable[[object], bool] = secret_service_unlocked,
) -> Backend:
    forced = env.get(BACKEND_ENV, "").strip().lower()
    file_path = Path(env.get(FILE_ENV) or DEFAULT_SECRETS_FILE).expanduser()

    def keychain() -> Optional[Backend]:
        if platform == "darwin" and which("security"):
            return KeychainBackend(current_user(env))
        return None

    keyring_reason: list[str] = []

    def keyring_backend() -> Optional[Backend]:
        module = keyring_loader()
        reason = keyring_unusable_reason(module, probe)
        if reason is None:
            return KeyringBackend(module, current_user(env))
        keyring_reason.append(reason)
        return None

    if forced == "file":
        return FileBackend(file_path)
    if forced == "keychain":
        backend = keychain()
        if backend is None:
            raise SecretStoreError(
                f"{BACKEND_ENV}=keychain needs macOS and the `security` CLI."
            )
        return backend
    if forced == "keyring":
        backend = keyring_backend()
        if backend is None:
            raise SecretStoreError(
                f"{BACKEND_ENV}=keyring, but {keyring_reason[0]} (run `make install-py`; "
                "on Linux a Secret Service such as GNOME Keyring must be running and "
                "unlocked)."
            )
        return backend
    if forced:
        raise SecretStoreError(
            f"Unknown {BACKEND_ENV}={forced!r}; use keychain, keyring or file."
        )
    backend = keychain() or keyring_backend()
    if backend is not None:
        return backend
    fallback = FileBackend(file_path)
    fallback.fallback_reason = keyring_reason[0] if keyring_reason else "no system keyring"
    return fallback


def backend_banner(backend: Backend) -> str:
    line = f"Secret store: {backend.describe()}"
    reason = getattr(backend, "fallback_reason", None)
    if reason:
        line += f" [fallback: {reason}; {BACKEND_ENV} overrides]"
    return line


# --- Commands -------------------------------------------------------------------


def prompt_hidden(key: str) -> Optional[str]:
    """Hidden prompt with confirmation; None (skip) on an empty first answer."""
    value = getpass.getpass(f"   {key}: ")
    if not value:
        return None
    if getpass.getpass(f"   {key} (again): ") != value:
        raise SecretStoreError(f"The two values for {key} differ; nothing stored.")
    return value


def cmd_init(backend: Backend, prompt=prompt_hidden, out=print) -> int:
    out(backend_banner(backend))
    failed: list[str] = []
    for key in SECRET_KEYS:
        # One failing key (a mistyped confirmation, an unreadable item) must
        # not leave the rest unasked.
        try:
            if backend.has(key):
                out(f"= {key} already stored (overwrite with: make secret-set KEY={key})")
                continue
            hint = " (empty to skip)" if backend.empty_skips else ""
            out(f"-> Enter value for {key}{hint}")
            stored = backend.store_interactive(key, prompt)
        except SecretStoreError as exc:
            _warn(str(exc))
            failed.append(key)
            continue
        out(f"+ {key} stored" if stored else f"- {key} skipped")
    if failed:
        _warn(f"Not stored: {', '.join(failed)}; retry with `make secret-set KEY=...`.")
        return 1
    return 0


def cmd_set(backend: Backend, key: str, prompt=prompt_hidden, out=print) -> int:
    if key not in KNOWN_KEYS:
        raise SecretStoreError(
            f"Unknown secret {key!r}; known: {', '.join(SECRET_KEYS + LEGACY_KEYS)}."
        )
    out(backend_banner(backend))
    if backend.store_interactive(key, prompt):
        out(f"+ {SERVICE_PREFIX}{key} stored")
        return 0
    out(f"- {key}: empty value, nothing stored")
    return 1


def cmd_status(backend: Backend, env: Mapping[str, str] = os.environ, out=print) -> int:
    out(backend_banner(backend))
    code = 0
    for key in SECRET_KEYS:
        try:
            stored = backend.has(key)
        except SecretStoreError as exc:
            out(f"? {key} (unreadable)")
            _warn(str(exc))
            code = 1
            continue
        if stored:
            out(f"+ {key}")
        elif env.get(key):
            out(f"x {key} (missing; set in the environment)")
        else:
            out(f"x {key} (missing)")
    return code


def cmd_export(
    backend: Backend, keys=None, env: Mapping[str, str] = os.environ, out=print
) -> int:
    """`export VAR='value'` for each stored key not already set in `env`.

    Notes go to stderr and never carry a value. With explicit `keys`, a key
    that is neither in `env` nor in the store fails the command (exit 1).
    """
    explicit = bool(keys)
    wanted = tuple(keys) if keys else EXPORT_KEYS
    for key in wanted:
        if key not in KNOWN_KEYS:
            raise SecretStoreError(f"Unknown secret {key!r}.")
    reason = getattr(backend, "fallback_reason", None)
    if reason:
        _warn(f"Secret store: {backend.describe()} ({reason}; {BACKEND_ENV} overrides)")
    looked_up = loaded = 0
    missing: list[str] = []
    for key in wanted:
        if env.get(key):
            continue  # an exported value wins over the store
        looked_up += 1
        try:
            value = backend.get(key)
        except SecretStoreError as exc:
            _warn(str(exc))
            value = None
        if value:
            out(f"export {key}={shlex.quote(value)}")
            loaded += 1
        else:
            missing.append(key)
    if explicit and missing:
        _warn(f"No value for {', '.join(missing)} in {backend.describe()}.")
        return 1
    if looked_up and not loaded:
        _warn(f"No secrets found in {backend.describe()}; store them with `make secrets-init`.")
    return 0


USAGE = "usage: secret_store.py {init | set KEY | status | export [KEY ...]}"


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    if args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    command, rest = args[0], args[1:]
    valid = (
        (command in ("init", "status") and not rest)
        or (command == "set" and len(rest) == 1)
        or command == "export"
    )
    if not valid:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        backend = select_backend()
        if command == "init":
            return cmd_init(backend)
        if command == "set":
            return cmd_set(backend, rest[0])
        if command == "status":
            return cmd_status(backend)
        # LF and UTF-8 whatever the platform: the output is eval'd by sh, and a
        # console code page would raise mid-value on a non-ASCII character.
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", newline="\n")
        return cmd_export(backend, rest)
    except SecretStoreError as exc:
        _warn(str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):  # Ctrl-C, or Ctrl-D at a prompt
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
