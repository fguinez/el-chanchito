"""Sealed scraper credentials (V048): publish the public key, read the rows.

The rows hold only HPKE ciphertext (see scrapers/credentials.py); nothing
here sees a plaintext value.
"""

from db.connection import get_pool, with_db_retry
from scrapers.credentials import ALGORITHM, CredentialKey, SealedCredential


@with_db_retry
def publish_public_key(key: CredentialKey) -> tuple[str | None, int]:
    """Make `key` the one the dashboard seals to.

    Returns the key id it replaced (None when it was already current or there
    was none) and how many stored rows were sealed to some other key, which
    this service can no longer open.
    """
    with get_pool().connection() as conn:
        row = conn.execute("SELECT key_id FROM credential_keys WHERE id = 1").fetchone()
        previous = row[0] if row else None
        conn.execute(
            """
            INSERT INTO credential_keys (id, key_id, algorithm, public_key, updated_at)
            VALUES (1, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE
              SET key_id = EXCLUDED.key_id,
                  algorithm = EXCLUDED.algorithm,
                  public_key = EXCLUDED.public_key,
                  updated_at = now()
              WHERE credential_keys.key_id <> EXCLUDED.key_id
                 OR credential_keys.algorithm <> EXCLUDED.algorithm
            """,
            (key.key_id, ALGORITHM, key.public_raw),
        )
        (orphaned,) = conn.execute(
            "SELECT count(*) FROM institution_credentials WHERE key_id <> %s",
            (key.key_id,),
        ).fetchone()
    return (previous if previous and previous != key.key_id else None), orphaned


@with_db_retry
def fetch_published_key_id() -> str | None:
    """The key id the dashboard currently seals to, if any."""
    with get_pool().connection() as conn:
        row = conn.execute("SELECT key_id FROM credential_keys WHERE id = 1").fetchone()
    return row[0] if row else None


@with_db_retry
def fetch_sealed_credentials() -> list[SealedCredential]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT institution_slug, field_key, scheme, key_id, sealed
            FROM institution_credentials
            ORDER BY institution_slug, field_key
            """
        ).fetchall()
    return [
        SealedCredential(
            institution_slug=slug,
            field_key=field_key,
            scheme=scheme,
            key_id=key_id,
            sealed=bytes(sealed),
        )
        for slug, field_key, scheme, key_id, sealed in rows
    ]
