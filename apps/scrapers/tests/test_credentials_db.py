"""The sealed-credentials SQL (V048) against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL is set (see conftest.py). Values and keys
are synthetic and generated per test.
"""

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from db.credentials import fetch_sealed_credentials, publish_public_key
from scrapers.credentials import ALGORITHM, SCHEME, CredentialKey, open_rows


def _key() -> CredentialKey:
    return CredentialKey.from_private(X25519PrivateKey.generate())


def test_publish_is_idempotent_and_reports_a_replaced_key(db_conn):
    first = _key()
    assert publish_public_key(first) == (None, 0)
    assert publish_public_key(first) == (None, 0)
    row = db_conn.execute("SELECT * FROM credential_keys").fetchone()
    assert row["key_id"] == first.key_id
    assert bytes(row["public_key"]) == first.public_raw
    assert row["algorithm"] == ALGORITHM

    db_conn.execute(
        "INSERT INTO institution_credentials (institution_slug, field_key, scheme, key_id, sealed)"
        " VALUES ('skipo', 'api_key', %s, %s, %s)",
        (SCHEME, first.key_id, first.seal("skipo", "api_key", "synthetic-key")),
    )
    second = _key()
    assert publish_public_key(second) == (first.key_id, 1)
    assert db_conn.execute("SELECT count(*) AS n FROM credential_keys").fetchone()["n"] == 1


def test_rows_round_trip_through_the_table(db_conn):
    key = _key()
    publish_public_key(key)
    db_conn.execute(
        "INSERT INTO institution_credentials (institution_slug, field_key, scheme, key_id, sealed)"
        " VALUES ('banchile', 'password', %s, %s, %s)",
        (SCHEME, key.key_id, key.seal("banchile", "password", "synthetic-password")),
    )
    opened = open_rows(key, fetch_sealed_credentials())
    assert opened.values == {"BANCHILE_PASSWORD": "synthetic-password"}


@pytest.mark.parametrize(
    ("slug", "key_id", "sealed"),
    [
        ("Bad Slug", "0" * 64, b"\x00" * 64),
        ("banchile", "not-hex", b"\x00" * 64),
        ("banchile", "0" * 64, b"\x00" * 8),
    ],
)
def test_table_rejects_malformed_rows(db_conn, slug, key_id, sealed):
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO institution_credentials"
            " (institution_slug, field_key, scheme, key_id, sealed)"
            " VALUES (%s, 'password', %s, %s, %s)",
            (slug, SCHEME, key_id, sealed),
        )
