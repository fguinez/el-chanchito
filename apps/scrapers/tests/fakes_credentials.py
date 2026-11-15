"""An in-memory key ring with the interface of credential_manager.DatabaseKeyRing."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import psycopg
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from db.credentials import ActiveKey, KeyRingConflict, KeySync
from scrapers.credentials import CredentialKey


def make_v1_key() -> CredentialKey:
    """A v1 (X25519) key, as installs made before v2 still hold."""
    return CredentialKey.from_private(X25519PrivateKey.generate())


class FakeRing:
    def __init__(self) -> None:
        self.rows: list = []
        self.active: ActiveKey | None = None
        self.statuses: dict[str, str] = {}
        self.activated: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.fail = False
        self.fail_fetch = False
        self.fetches = 0
        self.now = datetime.now(UTC)

    def _check(self) -> None:
        if self.fail:
            raise psycopg.OperationalError("db down")

    def activate(self, key) -> ActiveKey:
        if self.active is not None:
            self.statuses[self.active.key_id] = "retired"
        self.statuses[key.key_id] = "active"
        self.active = ActiveKey(key.key_id, self.now)
        self.activated.append(key.key_id)
        return self.active

    def _orphaned(self, key_id: str) -> int:
        return sum(1 for r in self.rows if r.key_id != key_id)

    def sync_key(self, key, allow_takeover):
        self._check()
        if self.active is not None and self.active.key_id == key.key_id:
            return KeySync("current", self.active, self._orphaned(key.key_id))
        if self.active is None:
            outcome = "published"
        elif allow_takeover and not any(r.key_id == self.active.key_id for r in self.rows):
            outcome = "took_over"
        else:
            return KeySync("mismatch", self.active)
        return KeySync(outcome, self.activate(key), self._orphaned(key.key_id))

    def active_key(self):
        self._check()
        return self.active

    def pending_outcome(self, pending_key_id, current_key_id):
        self._check()
        if pending_key_id in self.statuses:
            return "committed"
        if current_key_id and self.active and self.active.key_id == current_key_id:
            return "not_committed"
        return "unknown"

    def fetch_rows(self):
        self._check()
        if self.fail_fetch:
            raise psycopg.OperationalError("db down")
        self.fetches += 1
        return list(self.rows)

    def rotate(self, old, new, reseal):
        self._check()
        if self.active is None or self.active.key_id != old.key_id:
            raise KeyRingConflict("the key being rotated is not the active key")
        resealed, skipped = reseal([r for r in self.rows if r.key_id == old.key_id])
        self.activate(new)
        by_field = {(r.institution_slug, r.field_key): r for r in resealed}
        self.rows = [
            replace(
                r,
                key_id=new.key_id,
                scheme=by_field[(r.institution_slug, r.field_key)].scheme,
                sealed=by_field[(r.institution_slug, r.field_key)].sealed,
            )
            if r.key_id == old.key_id and (r.institution_slug, r.field_key) in by_field
            else r
            for r in self.rows
        ]
        return len(resealed), len(skipped)

    def reset(self, key):
        self._check()
        if self.active is not None and self.active.key_id == key.key_id:
            return None, self._orphaned(key.key_id)
        retired = self.active.key_id if self.active else None
        self.activate(key)
        return retired, self._orphaned(key.key_id)

    def record_rotation_failure(self, key_id, reason):
        self._check()
        self.failures.append((key_id, reason))
