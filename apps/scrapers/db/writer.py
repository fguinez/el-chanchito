"""Write scraped data to the database."""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from db.connection import get_pool
from scrapers.base import ScrapedProduct, ScrapedTransaction

logger = logging.getLogger(__name__)


def start_scraper_run(method: str, institution: str) -> str:
    """Record the start of a scraper run. Returns the run ID.

    `method` is the scraping mechanism ('email', 'fintself', 'http_api',
    'open_banking'); `institution` is the platform being scraped.
    """
    run_id = str(uuid4())
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO scraper_runs (id, method, institution, started_at, status)
            VALUES (%s, %s, %s, %s, 'running')
            """,
            (run_id, method, institution, datetime.now(timezone.utc)),
        )
    return run_id


def finish_scraper_run(
    run_id: str,
    status: str,
    transactions_imported: int = 0,
    error_message: str | None = None,
) -> None:
    """Record the end of a scraper run."""
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE scraper_runs
            SET finished_at = %s, status = %s,
                transactions_imported = %s, error_message = %s
            WHERE id = %s
            """,
            (
                datetime.now(timezone.utc),
                status,
                transactions_imported,
                error_message,
                run_id,
            ),
        )


# --- Pure helpers (no DB) ------------------------------------------------------

# Distinct from any canonical JSON string, so None (no metrics recorded yet /
# none scraped) never compares equal to a real payload — not even to `{}`.
_NO_METRICS = "\x00none"


def _normalize_numbers(value):
    """Recursively convert ints to floats so equal quantities compare equal.

    Comparison-only — never applied to what gets stored. bool is an int
    subclass and stays a bool.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, dict):
        return {k: _normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(v) for v in value]
    return value


def _canonical_metrics(metrics: dict | None) -> str:
    """Canonical form of a metrics payload for change detection.

    Key order must not matter (Postgres jsonb reorders keys), so the dict is
    serialized sorted and compact. Numeric type must not matter either: a
    jsonb integer literal (e.g. the V011-seeded `"limit": 4000000`) loads as
    int while a pydantic dump of the same quantity is the float 4000000.0 —
    ints are normalized to floats before dumping so that doesn't read as a
    change. None maps to a sentinel distinct from `{}`.
    """
    if metrics is None:
        return _NO_METRICS
    return json.dumps(
        _normalize_numbers(metrics), sort_keys=True, separators=(",", ":")
    )


def _headline_decimal(headline: float | int | None) -> Decimal | None:
    """Convert a metrics headline to a Decimal for the NUMERIC balance column.

    Via str() so a float like 0.1 keeps its printed value instead of its
    binary expansion. None (kinds with no headline) passes through.
    """
    if headline is None:
        return None
    return Decimal(str(headline))


def _resolve_product(
    conn,
    institution_slug: str,
    kind: str,
    currency: str = "CLP",
    external_ref: str | None = None,
    name: str | None = None,
) -> str:
    """Get or create the product for (institution, kind, currency, external_ref).

    Walks the chain institution -> account -> product, creating missing links.
    Single-user deployment: everything attaches to the oldest user. The product
    step upserts on the identity index so concurrent writers can't race a
    SELECT-then-INSERT; `name` is only used for the INSERT values (create-only),
    so a user rename is never overwritten.
    """
    row = conn.execute(
        "SELECT id, name FROM institutions WHERE slug = %s", (institution_slug,)
    ).fetchone()
    if row:
        institution_id, institution_name = row
    else:
        institution_id = str(uuid4())
        institution_name = institution_slug
        conn.execute(
            """
            INSERT INTO institutions (id, slug, name, kind)
            VALUES (%s, %s, %s, 'other')
            """,
            (institution_id, institution_slug, institution_slug),
        )

    user_row = conn.execute(
        "SELECT id FROM users ORDER BY created_at LIMIT 1"
    ).fetchone()
    if not user_row:
        raise RuntimeError("No users found — run the V009 migration first")
    user_id = user_row[0]

    account_row = conn.execute(
        """
        SELECT id FROM accounts
        WHERE user_id = %s AND institution_id = %s
        ORDER BY display_order LIMIT 1
        """,
        (user_id, institution_id),
    ).fetchone()
    if account_row:
        account_id = account_row[0]
    else:
        account_id = str(uuid4())
        conn.execute(
            "INSERT INTO accounts (id, user_id, institution_id) VALUES (%s, %s, %s)",
            (account_id, user_id, institution_id),
        )

    default_name = f"{institution_name} - {kind}" + (
        f" ({currency})" if currency != "CLP" else ""
    )
    product_row = conn.execute(
        """
        INSERT INTO products (id, account_id, name, kind, currency, external_ref)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, kind, currency, COALESCE(external_ref, ''))
        DO UPDATE SET updated_at = now()
        RETURNING id
        """,
        (
            str(uuid4()),
            account_id,
            name or default_name,
            kind,
            currency,
            external_ref,
        ),
    ).fetchone()
    return product_row[0]


def upsert_transactions(transactions: list[ScrapedTransaction]) -> int:
    """Insert transactions, skipping duplicates. Returns count of new rows."""
    if not transactions:
        return 0

    pool = get_pool()
    inserted = 0

    with pool.connection() as conn:
        for txn in transactions:
            product_id = _resolve_product(
                conn, txn.institution, txn.product_kind, txn.currency
            )
            try:
                cur = conn.execute(
                    """
                    INSERT INTO transactions
                        (id, product_id, description, amount, transaction_date,
                         scheduled_month, source, external_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (product_id, external_id) DO NOTHING
                    """,
                    (
                        str(uuid4()),
                        product_id,
                        txn.description,
                        txn.amount,
                        txn.transaction_date,
                        txn.scheduled_month,
                        f"scraper_{txn.institution}",
                        txn.external_id,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
            except Exception:
                logger.exception("Failed to insert transaction: %s", txn.external_id)

    return inserted


def upsert_product(sp: ScrapedProduct) -> None:
    """Record one scraped product observation.

    Attributes shallow-merge into products.attributes (a dashboard-only scrape
    can't wipe a last4 read earlier from a detail page). Metrics always refresh
    products.current_balance/metrics/balance_as_of ("last checked"), but append
    a product_snapshots history row only when the payload changed — both in the
    same transaction so the denormalized columns can't drift.
    """
    pool = get_pool()
    with pool.connection() as conn:
        product_id = _resolve_product(
            conn,
            sp.institution,
            sp.kind,
            sp.currency,
            external_ref=sp.external_ref,
            name=sp.name,
        )

        if sp.attributes is not None:
            attrs = sp.attributes.model_dump(mode="json", exclude_none=True)
            conn.execute(
                "UPDATE products SET attributes = attributes || %s WHERE id = %s",
                (Jsonb(attrs), product_id),
            )

        if sp.metrics is None:
            logger.info(
                "Product confirmed (no metrics): %s/%s %s",
                sp.institution,
                sp.kind,
                sp.currency,
            )
            return

        metrics_dict = sp.metrics.model_dump(mode="json", exclude_none=True)
        headline = _headline_decimal(sp.metrics.headline())

        row = conn.execute(
            "SELECT metrics FROM products WHERE id = %s", (product_id,)
        ).fetchone()
        current_metrics = row[0] if row else None

        now = datetime.now(timezone.utc)
        changed = _canonical_metrics(metrics_dict) != _canonical_metrics(
            current_metrics
        )
        # `balance` is NOT NULL: a kind whose headline is None (e.g. debit_card)
        # gets no history row, only the latest-metrics refresh below.
        if changed and headline is not None:
            conn.execute(
                """
                INSERT INTO product_snapshots
                    (id, product_id, balance, metrics, as_of, source)
                VALUES (%s, %s, %s, %s, %s, 'scraper')
                ON CONFLICT (product_id, as_of) DO NOTHING
                """,
                (str(uuid4()), product_id, headline, Jsonb(metrics_dict), now),
            )

        # balance_as_of moves even when nothing changed — it means "last time a
        # scraper confirmed this observation".
        conn.execute(
            """
            UPDATE products
            SET current_balance = %s, metrics = %s, balance_as_of = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (headline, Jsonb(metrics_dict), now, now, product_id),
        )

        logger.info(
            "Product %s: %s/%s %s = %s",
            "update" if changed else "confirmed",
            sp.institution,
            sp.kind,
            sp.currency,
            metrics_dict,
        )
