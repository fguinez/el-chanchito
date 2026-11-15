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
    RotationError,
    max_age_days_from_env,
    reset_key_file,
    rotate_key_file,
)
from dotenv import load_dotenv

from db.connection import close_pool
from scrapers.credentials import CredentialKeyError, KeyFile, key_file_path

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


def _status(key_file: KeyFile, ring: DatabaseKeyRing) -> int:
    try:
        local = key_file.load().key_id
    except FileNotFoundError:
        local = None
    active = ring.active_key()
    print(f"Key file:   {key_file.path} ({local[:12] if local else 'missing'})")
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
    max_age = max_age_days_from_env()
    print(
        f"Rotation:   every {max_age} days (CREDENTIALS_KEY_MAX_AGE_DAYS)"
        if max_age
        else "Rotation:   automatic rotation is off (CREDENTIALS_KEY_MAX_AGE_DAYS=0)"
    )
    if local != active.key_id:
        print(
            "key_mismatch: this key file is not the database's active key; this service "
            "cannot open the stored credentials. Use the other service's key file, or run "
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
        f"{counts}. The old private key is deleted, so older database backups no longer open."
    )
    _poke_service()
    return 0


def _reset(key_file: KeyFile, ring: DatabaseKeyRing, assume_yes: bool) -> int:
    print(
        "This makes the key file\n"
        f"  {key_file.path}\n"
        "the database's active credentials key and retires the current one. Every credential\n"
        "stored in Credenciales becomes unreadable and must be entered again. Use it only when\n"
        "the key file that sealed them is lost; if another scraper service (make dev or make up)\n"
        "still holds it, use that key file instead."
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
        print(f"Key {key.key_id[:12]} already was the active key; nothing to reset.")
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
    except (RotationError, CredentialKeyError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed: {type(exc).__name__} (is the database up?)", file=sys.stderr)
        return 1
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main())
