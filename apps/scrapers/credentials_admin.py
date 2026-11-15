"""Manage the key that seals the dashboard's credentials.

    python credentials_admin.py status      # key file vs the database's key ring
    python credentials_admin.py rotate      # new key, re-seal every row, delete the old key
    python credentials_admin.py reset-key   # lost key file: start over (asks first)

Run it where the key file lives: on the host for `make dev` (`make
credentials-rotate`), inside the scrapers container for `make up` (`make
credentials-rotate STACK=up`). It takes the key file's lock, so it never
races the service's own periodic rotation, and the running service picks the
new key up from disk on its next sync; this script asks it to sync right away
through the loopback control endpoint (best effort, within a minute anyway).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import UTC, datetime

from credential_manager import (
    DatabaseKeyRing,
    PendingUnresolved,
    RotationError,
    max_age_days_from_env,
    reset_key_file,
    rotate_key_file,
)
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

from db.connection import close_pool
from scrapers.credentials import KEY_KINDS, CredentialKeyError, KeyFile, key_file_path

CONFIRM_WORD = "RESET"


def _poke_service() -> None:
    base = os.environ.get("SCRAPER_CONTROL_URL") or (
        f"http://127.0.0.1:{os.environ.get('SCRAPER_CONTROL_PORT') or 8080}"
    )
    request = urllib.request.Request(
        f"{base.rstrip('/')}/credentials/reload",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            applied = json.loads(response.read() or b"{}").get("applied")
    except Exception:
        print(
            "The scraper service did not answer at "
            f"{base}; if it is running it loads the new key within a minute."
        )
        return
    print(f"The scraper service reloaded its credentials (applied: {applied}).")


def _scheme_of(algorithm: str) -> str:
    """The scheme a key's published algorithm seals with (v1 or v2)."""
    for kind in KEY_KINDS:
        if kind.algorithm == algorithm:
            return kind.scheme
    return "unknown scheme"


def _status(key_file: KeyFile, ring: DatabaseKeyRing) -> int:
    try:
        local_key = key_file.load()
    except FileNotFoundError:
        local_key = None
    local = local_key.key_id if local_key else None
    active = ring.active_key()
    print(
        f"Key file:   {key_file.path} "
        f"({f'{local[:12]}, {local_key.scheme}' if local_key else 'missing'})"
    )
    if key_file.next_path.exists():
        print(
            f"Pending:    {key_file.next_path} (an interrupted rotation; the service resolves it)"
        )
    if active is None:
        print("Database:   no active key yet (the scraper service publishes one on start)")
        return 0
    age = datetime.now(UTC) - active.created_at
    print(
        f"Database:   active key {active.key_id[:12]}, created {active.created_at:%Y-%m-%d} ({age.days} days ago)"
    )
    print(f"Algorithm:  {active.algorithm} ({_scheme_of(active.algorithm)})")
    max_age = max_age_days_from_env()
    print(
        f"Rotation:   every {max_age} days (CREDENTIALS_KEY_MAX_AGE_DAYS)"
        if max_age
        else "Rotation:   automatic rotation is off (CREDENTIALS_KEY_MAX_AGE_DAYS=0)"
    )
    if local != active.key_id:
        print(
            "key_mismatch: this key file is not the database's active key; this service "
            "cannot open the stored credentials. Run only the scraper service that saved them "
            "against this database (never a copy of its key file), or run "
            "`make credentials-reset-key` to start over."
        )
        return 1
    return 0


def _rotate(key_file: KeyFile, ring: DatabaseKeyRing) -> int:
    result = rotate_key_file(key_file, ring, dict(os.environ))
    counts = (
        f"{result.resealed} credential(s) re-sealed"
        + (f", {result.skipped} left as they were (they need re-entry)" if result.skipped else "")
        if result.resealed is not None
        else "the rotation was confirmed after a retry"
    )
    print(
        f"Rotated the credentials key {result.old_key_id[:12]} -> {result.new_key.key_id[:12]}: "
        f"{counts}. The old private key file is deleted, so older database backups no longer "
        "open with this key file (filesystem snapshots, volume backups or copies of the key "
        "file still hold the old key)."
    )
    _poke_service()
    return 0


def _target() -> str:
    """Where this runs and which database it changes (never the password)."""
    try:
        info = conninfo_to_dict(os.environ.get("DATABASE_URL", ""))
        database = f"{info.get('host', '?')}:{info.get('port', '5432')}/{info.get('dbname', '?')}"
    except Exception:
        database = "(unparsable DATABASE_URL)"
    where = "the scrapers container (make up)" if os.path.exists("/.dockerenv") else "this host"
    return f"{where}, database {database}"


def _reset(key_file: KeyFile, ring: DatabaseKeyRing, assume_yes: bool) -> int:
    try:
        local_key = key_file.load()
        local = f"{local_key.key_id[:12]}, {local_key.algorithm}"
    except FileNotFoundError:
        local = "none yet; it will be created"
    try:
        active = ring.active_key()
        active_id = f"{active.key_id[:12]}, {active.algorithm}" if active else "none"
    except Exception as exc:
        active_id = f"unknown ({type(exc).__name__})"
    print(
        f"Running on {_target()}.\n"
        f"  Database's active key: {active_id}\n"
        f"  Local key file:        {local} ({key_file.path})\n"
        "This makes the local key the database's active credentials key and retires the\n"
        "current one. Every credential stored in Credenciales becomes unreadable and must be\n"
        "entered again. Use it only when the key file that sealed them is lost; if another\n"
        "scraper service (make dev or make up) still holds it, run that one instead (wrong\n"
        "stack? make dev is the default, STACK=up targets make up)."
    )
    if key_file.next_path.exists():
        print(
            f"A pending key from an unfinished rotation ({key_file.next_path.name}) will be\n"
            "moved aside, not deleted: it still holds a private key."
        )
    if not assume_yes:
        if not sys.stdin.isatty():
            print("Refusing without a terminal to confirm on.", file=sys.stderr)
            return 2
        answer = input(f"Type {CONFIRM_WORD} to continue: ").strip()
        if answer != CONFIRM_WORD:
            print("Nothing changed.")
            return 1
    key, retired, orphaned = reset_key_file(key_file, ring)
    if retired:
        print(
            f"Retired key {retired[:12]}; {key.key_id[:12]} is now active. "
            f"{orphaned} stored credential(s) must be entered again in Credenciales."
        )
    else:
        print(f"Key {key.key_id[:12]} is the active key; no other key was retired.")
        if orphaned:
            # A retry after a commit whose reply was lost lands here too.
            print(f"{orphaned} stored credential(s) must be entered again in Credenciales.")
    _poke_service()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["status", "rotate", "reset-key"])
    parser.add_argument("--yes", action="store_true", help="reset-key without the prompt")
    args = parser.parse_args(argv)
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    key_file = KeyFile(key_file_path())
    ring = DatabaseKeyRing()
    try:
        if args.command == "status":
            return _status(key_file, ring)
        if args.command == "rotate":
            return _rotate(key_file, ring)
        return _reset(key_file, ring, args.yes)
    except PendingUnresolved as exc:
        print(f"Failed: {exc}. {exc.detail()}.", file=sys.stderr)
        return 1
    except (RotationError, CredentialKeyError) as exc:
        print(f"Failed: {exc} (key file: {key_file.path})", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed: {type(exc).__name__} (is the database up?)", file=sys.stderr)
        return 1
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main())
