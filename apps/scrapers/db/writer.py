"""Write scraped data to the database."""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from db.connection import get_pool
from db.slug import slugify, unique_slug
from scrapers.base import ScrapedProduct, ScrapedTransaction

logger = logging.getLogger(__name__)


def start_scraper_run(method: str, institution: str) -> str:
    """Record the start of a scraper run. Returns the run ID.

    `method` is the scraping mechanism ('email', 'fintself', 'web', 'http_api',
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
    SELECT-then-INSERT; `name` and `slug` are only used for the INSERT values
    (create-only), so a user rename is never overwritten and slugs stay stable.
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
    product_name = name or default_name
    # Slugs are unique per institution (inactive products keep theirs reserved),
    # so the taken set spans every account of the institution.
    taken = {
        row[0]
        for row in conn.execute(
            """
            SELECT p.slug FROM products p
            JOIN accounts a ON p.account_id = a.id
            WHERE a.institution_id = %s
            """,
            (institution_id,),
        ).fetchall()
    }
    slug = unique_slug(slugify(product_name, kind), taken)
    product_row = conn.execute(
        """
        INSERT INTO products (id, account_id, name, slug, kind, currency,
                              external_ref)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, kind, currency, COALESCE(external_ref, ''))
        DO UPDATE SET updated_at = now()
        RETURNING id
        """,
        (
            str(uuid4()),
            account_id,
            product_name,
            slug,
            kind,
            currency,
            external_ref,
        ),
    ).fetchone()
    return product_row[0]


def _resolve_product_row(conn, product_id: str):
    """The (account_id, kind, currency, external_ref, is_active) of a product."""
    return conn.execute(
        "SELECT account_id, kind, currency, external_ref, is_active "
        "FROM products WHERE id = %s",
        (product_id,),
    ).fetchone()


def _has_active_ref_sibling(conn, account_id, kind: str, currency: str) -> bool:
    """True when an active per-holding (external_ref set) product exists for the
    same account/kind/currency, so a NULL-ref roll-up would double-count it."""
    return (
        conn.execute(
            "SELECT 1 FROM products "
            "WHERE account_id = %s AND kind = %s AND currency = %s "
            "AND external_ref IS NOT NULL AND is_active LIMIT 1",
            (account_id, kind, currency),
        ).fetchone()
        is not None
    )


def _retire_rollup_sibling(conn, account_id, kind: str, currency: str) -> str | None:
    """Deactivate the active NULL-ref roll-up sibling of a per-holding product.

    Drops the sibling's snapshots and nulls its balances (mirrors migrations
    V012/V013), so the roll-up and per-holding representations never both count
    toward net worth. Idempotent; returns the retired product id, or None when
    there was nothing to retire.
    """
    row = conn.execute(
        "SELECT id FROM products "
        "WHERE account_id = %s AND kind = %s AND currency = %s "
        "AND external_ref IS NULL AND is_active LIMIT 1",
        (account_id, kind, currency),
    ).fetchone()
    if not row:
        return None
    sibling_id = row[0]
    conn.execute(
        "DELETE FROM product_snapshots WHERE product_id = %s", (sibling_id,)
    )
    conn.execute(
        "UPDATE products SET is_active = false, current_balance = NULL, "
        "metrics = NULL, balance_as_of = NULL, updated_at = now() WHERE id = %s",
        (sibling_id,),
    )
    return sibling_id


def _write_decision(
    is_active: bool, external_ref: str | None, has_active_ref_sibling: bool
) -> str:
    """What `upsert_product` should do with a resolved product row.

    - ``"skip_inactive"``: the row is retired (a roll-up superseded by
      per-holding products, or the retired Fintual aggregate). Leave it frozen
      so a fallback scrape can't resurrect a double-counting aggregate.
    - ``"skip_superseded"``: a NULL-ref roll-up whose per-holding siblings are
      active. Writing it would double-count, so the per-holding rows win.
    - ``"write"``: proceed normally.
    """
    if not is_active:
        return "skip_inactive"
    if external_ref is None and has_active_ref_sibling:
        return "skip_superseded"
    return "write"


# --- Adopting a stored row onto a new external_id (issue #57) -----------------
#
# A scraper that changes how it keys a movement would otherwise re-import its
# whole window: `ON CONFLICT (product_id, external_id) DO NOTHING` sees a new id
# and inserts a second row. Unlike V017's re-key, BanChile's new ids are the
# bank's own operation ids and cannot be computed in SQL from anything stored,
# so the migration cannot do it: the stored rows are re-keyed *here*, the first
# time a scrape brings a movement's new id along.
#
# Only BanChile takes this path. Every id it has ever emitted starts with
# `bch_`, and the legacy ones are `bch_` + md5 hex, which can never collide with
# the new `bch_op_` / `bch_ref_` / `bch_fp_` prefixes ("p", "r" and "_" are not
# hex digits). A row already carrying an operation-id key is *final*: the bank
# identified it, so it is never claimed away by another movement.
_ADOPTING_PREFIX = "bch_"
_FINAL_ID_PREFIXES = ("bch_op_", "bch_ref_")


def _adopts_stored_rows(external_id: str | None) -> bool:
    """True when this key's institution re-keys its stored rows in place."""
    return bool(external_id) and external_id.startswith(_ADOPTING_PREFIX)


def _is_final_id(external_id: str | None) -> bool:
    """True when a key is derived from an operation id the bank assigned."""
    return bool(external_id) and external_id.startswith(_FINAL_ID_PREFIXES)


def _claim_decision(
    external_id: str,
    siblings: list[tuple],
    claimed: set,
    incoming_ids: set[str],
) -> tuple[str, object]:
    """What to do with a movement whose key matches no stored row.

    `siblings` are the rows of the same product with the same date and amount
    from the same scraper, oldest `created_at` first: candidates for being the
    same movement under its old key. Returns one of

    - ``("rekey", row_id)``: the oldest unclaimed sibling that is not already
      finally identified is this movement under a superseded key. Rewriting it
      is what migrates the stored history, and what closes issue #56 when a card
      charge crosses from the unbilled leg (a fingerprint key) to the billed one
      (its `numReferencia`).
    - ``("keep", row_id)``: every candidate is finally identified and this
      movement is not, which is what a transient failure to fetch the operation
      id looks like (the portal's own 503). Leave the row alone and insert
      nothing: re-keying it would throw the bank's id away and it would flap on
      the next run.
    - ``("insert", None)``: a genuinely new movement.

    Claiming is one-to-one and deterministic (oldest first, each row claimed at
    most once per call), so N identical movements map onto N stored rows instead
    of collapsing. A row whose id is one of this scrape's own keys is never
    claimed: it belongs to the movement carrying that key.

    Residual, and deliberate: two identical movements on the same day for the
    same amount where one is genuinely new *and* the other's operation id failed
    to load would be read as one movement ("keep"). It self-heals on the next
    run, when both ids load and the new one inserts.
    """
    incoming_is_final = _is_final_id(external_id)
    fallback = None
    for row_id, row_external_id in siblings:
        if row_id in claimed or row_external_id in incoming_ids:
            continue
        if _is_final_id(row_external_id):
            if not incoming_is_final and fallback is None:
                fallback = row_id
            continue
        return "rekey", row_id
    if fallback is not None:
        return "keep", fallback
    return "insert", None


def _sibling_rows(conn, product_id: str, txn: ScrapedTransaction, source: str) -> list[tuple]:
    """Stored rows that could be `txn` under a superseded key, oldest first.

    Scoped as tightly as the identity allows: same product, same date, same
    amount, same scraper source. That is narrow enough that an unrelated row
    (a manual entry, another institution, another product of the same
    institution) can never be claimed.
    """
    return conn.execute(
        """
        SELECT id, external_id FROM transactions
        WHERE product_id = %s AND transaction_date = %s AND amount = %s
          AND source = %s AND external_id IS NOT NULL
        ORDER BY created_at, id
        """,
        (product_id, txn.transaction_date, txn.amount, source),
    ).fetchall()


def upsert_transactions(transactions: list[ScrapedTransaction]) -> int:
    """Insert transactions, skipping duplicates. Returns count of new rows.

    For institutions that re-key their stored rows (see `_adopts_stored_rows`),
    a movement whose key matches nothing is first offered the stored rows it
    could be under an older key, and adopts one instead of inserting a duplicate
    (issue #57). Everything else keeps the plain
    `ON CONFLICT (product_id, external_id) DO NOTHING` insert.
    """
    if not transactions:
        return 0

    pool = get_pool()
    inserted = 0

    with pool.connection() as conn:
        products: dict[tuple, str] = {}

        def product_of(txn: ScrapedTransaction) -> str:
            key = (txn.institution, txn.product_kind, txn.currency)
            if key not in products:
                products[key] = _resolve_product(
                    conn, txn.institution, txn.product_kind, txn.currency
                )
            return products[key]

        # Every key this scrape carries, per product: a stored row already
        # holding one of them belongs to that movement and is never adopted.
        incoming: dict[str, set[str]] = {}
        for txn in transactions:
            incoming.setdefault(product_of(txn), set()).add(txn.external_id)

        claimed: set = set()
        for txn in transactions:
            product_id = product_of(txn)
            source = f"scraper_{txn.institution}"
            try:
                row = conn.execute(
                    "SELECT id FROM transactions "
                    "WHERE product_id = %s AND external_id = %s",
                    (product_id, txn.external_id),
                ).fetchone()
                if row:
                    claimed.add(row[0])
                    continue

                if _adopts_stored_rows(txn.external_id):
                    action, row_id = _claim_decision(
                        txn.external_id,
                        _sibling_rows(conn, product_id, txn, source),
                        claimed,
                        incoming[product_id],
                    )
                    if action != "insert":
                        claimed.add(row_id)
                    if action == "rekey":
                        conn.execute(
                            "UPDATE transactions SET external_id = %s, "
                            "updated_at = now() WHERE id = %s",
                            (txn.external_id, row_id),
                        )
                        logger.info(
                            "Adopted stored transaction %s onto %s",
                            row_id,
                            txn.external_id,
                        )
                        continue
                    if action == "keep":
                        logger.info(
                            "Transaction %s already stored under a bank id; "
                            "left as it is",
                            txn.external_id,
                        )
                        continue

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
                        source,
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

    Two guards keep a roll-up total and its per-holding rows from both counting
    (see `_write_decision`): a retired product is left frozen, and a NULL-ref
    roll-up is dropped when active per-holding siblings exist. Conversely, a
    per-holding write retires any active NULL-ref sibling for its account/kind.
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

        account_id, kind, currency, external_ref, is_active = _resolve_product_row(
            conn, product_id
        )
        has_sibling = external_ref is None and _has_active_ref_sibling(
            conn, account_id, kind, currency
        )
        decision = _write_decision(is_active, external_ref, has_sibling)
        if decision == "skip_inactive":
            logger.warning(
                "Skipping retired product %s/%s %s (external_ref=%s); left frozen",
                sp.institution,
                sp.kind,
                sp.currency,
                external_ref,
            )
            return
        if decision == "skip_superseded":
            logger.warning(
                "Skipping roll-up %s/%s %s: active per-holding products own it",
                sp.institution,
                sp.kind,
                sp.currency,
            )
            return

        # A per-holding write owns the account/kind balance: retire any active
        # summed roll-up so the two representations can't both count.
        if external_ref is not None:
            retired = _retire_rollup_sibling(conn, account_id, kind, currency)
            if retired:
                logger.warning(
                    "Retired roll-up %s (%s/%s %s) superseded by per-holding %s",
                    retired,
                    sp.institution,
                    kind,
                    currency,
                    external_ref,
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
