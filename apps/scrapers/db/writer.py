"""Write scraped data to the database."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from db.connection import get_pool
from scrapers.base import ScrapedTransaction, ScrapedBalance

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


def _resolve_product_id(
    conn, institution_slug: str, kind: str, currency: str = "CLP"
) -> str:
    """Get or create the product for (institution, kind, currency).

    Walks the chain institution -> account -> product, creating missing links.
    Single-user deployment: everything attaches to the oldest user.
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

    product_row = conn.execute(
        """
        SELECT id FROM products
        WHERE account_id = %s AND kind = %s AND currency = %s
        LIMIT 1
        """,
        (account_id, kind, currency),
    ).fetchone()
    if product_row:
        return product_row[0]

    product_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO products (id, account_id, name, kind, currency)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            product_id,
            account_id,
            f"{institution_name} - {kind}"
            + (f" ({currency})" if currency != "CLP" else ""),
            kind,
            currency,
        ),
    )
    return product_id


def upsert_transactions(transactions: list[ScrapedTransaction]) -> int:
    """Insert transactions, skipping duplicates. Returns count of new rows."""
    if not transactions:
        return 0

    pool = get_pool()
    inserted = 0

    with pool.connection() as conn:
        for txn in transactions:
            product_id = _resolve_product_id(
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


def upsert_balance(balance: ScrapedBalance) -> None:
    """Record a scraped balance.

    Always refreshes products.current_balance/balance_as_of ("last checked"),
    but appends a product_balances history row only when the value changed —
    both in the same transaction so the denormalized column can't drift.
    """
    new_value = Decimal(str(balance.balance))
    pool = get_pool()
    with pool.connection() as conn:
        product_id = _resolve_product_id(
            conn, balance.institution, balance.product_kind, balance.currency
        )

        row = conn.execute(
            "SELECT current_balance FROM products WHERE id = %s", (product_id,)
        ).fetchone()
        current = row[0] if row else None

        now = datetime.now(timezone.utc)
        changed = current is None or Decimal(current) != new_value
        if changed:
            conn.execute(
                """
                INSERT INTO product_balances (id, product_id, balance, as_of, source)
                VALUES (%s, %s, %s, %s, 'scraper')
                ON CONFLICT (product_id, as_of) DO NOTHING
                """,
                (str(uuid4()), product_id, new_value, now),
            )

        conn.execute(
            """
            UPDATE products
            SET current_balance = %s, balance_as_of = %s, updated_at = %s
            WHERE id = %s
            """,
            (new_value, now, now, product_id),
        )

        logger.info(
            "Balance %s: %s/%s = %s %s (as of %s)",
            "update" if changed else "confirmed",
            balance.institution,
            balance.product_kind,
            f"{balance.balance:,}",
            balance.currency,
            balance.as_of,
        )
