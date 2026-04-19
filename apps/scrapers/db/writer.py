"""Write scraped data to the database."""

import logging
from datetime import datetime, timezone
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


def _resolve_account_id(conn, institution: str, account_type: str) -> str:
    """Get or create an account ID for the given institution and type."""
    row = conn.execute(
        "SELECT id FROM accounts WHERE institution = %s AND account_type = %s",
        (institution, account_type),
    ).fetchone()

    if row:
        return row[0]

    account_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO accounts (id, name, institution, account_type)
        VALUES (%s, %s, %s, %s)
        """,
        (
            account_id,
            f"{institution} - {account_type}",
            institution,
            account_type,
        ),
    )
    return account_id


def upsert_transactions(transactions: list[ScrapedTransaction]) -> int:
    """Insert transactions, skipping duplicates. Returns count of new rows."""
    if not transactions:
        return 0

    pool = get_pool()
    inserted = 0

    with pool.connection() as conn:
        for txn in transactions:
            account_id = _resolve_account_id(
                conn, txn.account_institution, txn.account_type
            )
            try:
                cur = conn.execute(
                    """
                    INSERT INTO transactions
                        (id, account_id, description, amount, transaction_date,
                         scheduled_month, source, external_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (account_id, external_id) DO NOTHING
                    """,
                    (
                        str(uuid4()),
                        account_id,
                        txn.description,
                        txn.amount,
                        txn.transaction_date,
                        txn.scheduled_month,
                        f"scraper_{txn.account_institution}",
                        txn.external_id,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
            except Exception:
                logger.exception("Failed to insert transaction: %s", txn.external_id)

    return inserted


def upsert_balance(balance: ScrapedBalance) -> None:
    """Store the latest balance for an account (upsert)."""
    pool = get_pool()
    with pool.connection() as conn:
        account_id = _resolve_account_id(
            conn, balance.account_institution, balance.account_type
        )
        conn.execute(
            """
            INSERT INTO account_balances (id, account_id, balance, as_of, source)
            VALUES (%s, %s, %s, now(), 'scraper')
            ON CONFLICT (account_id) DO UPDATE
            SET balance = EXCLUDED.balance, as_of = now(), source = 'scraper'
            """,
            (str(uuid4()), account_id, balance.balance),
        )
        logger.info(
            "Balance update: %s/%s = $%s (as of %s)",
            balance.account_institution,
            balance.account_type,
            f"{balance.balance:,}",
            balance.as_of,
        )
