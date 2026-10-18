"""Write scraped data to the database."""

import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from db.connection import get_pool, with_db_retry
from db.slug import slugify, unique_slug
from scrapers.base import (
    ScrapedExchangeRate,
    ScrapedPriceHistory,
    ScrapedProduct,
    ScrapedTransaction,
    SupersededTransaction,
    TransactionLocation,
)

logger = logging.getLogger(__name__)


def start_scraper_run(method: str, institution: str) -> str:
    """Record the start of a scraper run. Returns the run ID.

    `method` is the scraping mechanism ('email', 'fintself', 'web', 'http_api',
    'open_banking'); `institution` is the platform being scraped.
    """
    run_id = str(uuid4())
    _insert_scraper_run(run_id, method, institution, datetime.now(timezone.utc))
    return run_id


@with_db_retry
def _insert_scraper_run(
    run_id: str, method: str, institution: str, started_at: datetime
) -> None:
    """The id is minted by the caller, so a retry after a commit whose
    acknowledgement was lost finds its own row instead of adding a second one
    that would stay `running` forever."""
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO scraper_runs (id, method, institution, started_at, status)
            VALUES (%s, %s, %s, %s, 'running')
            ON CONFLICT (id) DO NOTHING
            """,
            (run_id, method, institution, started_at),
        )


@with_db_retry
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
    SELECT-then-INSERT. The slug is create-only, so it stays stable. A
    scraper-supplied `name` refreshes `default_name` and moves `name` with it
    only while the user has not renamed the product. Without one (the
    transaction path resolves by identity alone) both columns are left as they
    are, so the generic default never flips a scraper's name back and forth.
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
        INSERT INTO products (id, account_id, name, default_name, slug, kind,
                              currency, external_ref)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, kind, currency, COALESCE(external_ref, ''))
        DO UPDATE SET
            name = CASE WHEN %s AND products.name = products.default_name
                        THEN EXCLUDED.default_name
                        ELSE products.name END,
            default_name = CASE WHEN %s THEN EXCLUDED.default_name
                                ELSE products.default_name END,
            updated_at = now()
        RETURNING id
        """,
        (
            str(uuid4()),
            account_id,
            product_name,
            product_name,
            slug,
            kind,
            currency,
            external_ref,
            name is not None,
            name is not None,
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
# The same prefix as a LIKE pattern: "_" is a LIKE wildcard, so it is escaped
# (Postgres's default escape character is the backslash, as in V017).
_ADOPTING_LIKE = "bch\\_%"
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
      movement is not, which is what a failure to fetch the operation id looks
      like. Leave the row alone and insert nothing: re-keying it would throw the
      bank's id away and it would flap on the next run. This is the one branch
      that drops an incoming movement, so the caller logs it at WARNING. It is
      self-correcting for the portal's transient 503 (the id comes back next
      run and matches directly), but NOT for a movement the bank simply has no
      glosa for: that one keeps arriving under its fingerprint key and keeps
      being folded onto the same stored row, which is the intended reading as
      long as the row really is that movement.
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
    """Stored rows that could be `txn` under a superseded key, best first.

    Scoped as tightly as the identity allows: same product, same amount, same
    scraper source, and an `external_id` that belongs to the re-keying
    institution's own namespace. That is narrow enough that an unrelated row (a
    manual entry, another institution, another product of the same institution)
    can never be claimed.

    The date match is the one deliberate loosening. A BanChile movement carries
    two dates now, and which one a stored row holds depends on when it was
    written: rows imported before issue #57 hold the *posting* date (fintself
    read the portal's `fechaContable` column), while an incoming movement is
    dated by when it *occurred*. So a candidate matches on either of the
    incoming dates, and exact occurrence-date matches are ordered first, so two
    movements a few days apart for the same amount cannot cross over. Adoption
    then corrects the row's dates (see `upsert_transactions`).
    """
    accounting_date = txn.accounting_date or txn.transaction_date
    return conn.execute(
        """
        SELECT id, external_id FROM transactions
        WHERE product_id = %s AND amount = %s AND source = %s
          AND external_id IS NOT NULL AND external_id LIKE %s
          AND transaction_date IN (%s, %s)
        ORDER BY (transaction_date = %s) DESC, created_at, id
        """,
        (
            product_id,
            txn.amount,
            source,
            _ADOPTING_LIKE,
            txn.transaction_date,
            accounting_date,
            txn.transaction_date,
        ),
    ).fetchall()


def _location_values(location: TransactionLocation | None) -> tuple:
    """The five location columns' values, in table order (all None for none)."""
    if location is None:
        return (None, None, None, None, None)
    return (
        location.name,
        location.city,
        location.country,
        location.latitude,
        location.longitude,
    )


def _fill_location(conn, row_id, location: TransactionLocation | None) -> None:
    """Fill the stored row's NULL location columns from a scraped location.

    A non-NULL column is never overwritten, whoever wrote it, and a row whose
    location the user set or cleared (`location_is_manual`, V025) is never
    touched at all. This is how rows stored before V025, or before their
    source reported a location, gain one on the next scrape. The coordinates
    move as a pair: both are filled only while the stored latitude is NULL
    (the schema keeps the two NULL together). A row with nothing to fill is
    not written, so `updated_at` only moves when something changed.
    """
    if location is None:
        return
    name, city, country, latitude, longitude = _location_values(location)
    sets: list[str] = []
    gaps: list[str] = []
    params: list = []
    for column, value in (
        ("location_name", name),
        ("location_city", city),
        ("location_country", country),
    ):
        if value is not None:
            sets.append(f"{column} = COALESCE({column}, %s)")
            gaps.append(f"{column} IS NULL")
            params.append(value)
    if latitude is not None and longitude is not None:
        # Every expression of an UPDATE reads the row as it was, so both
        # CASEs test the stored latitude.
        sets.append(
            "location_latitude = CASE WHEN location_latitude IS NULL "
            "THEN %s ELSE location_latitude END"
        )
        sets.append(
            "location_longitude = CASE WHEN location_latitude IS NULL "
            "THEN %s ELSE location_longitude END"
        )
        gaps.append("location_latitude IS NULL")
        params.extend((latitude, longitude))
    if not sets:
        return
    conn.execute(
        "UPDATE transactions SET "
        + ", ".join(sets)
        + ", updated_at = now() WHERE id = %s AND NOT location_is_manual AND ("
        + " OR ".join(gaps)
        + ")",
        (*params, row_id),
    )


def _write_transaction(
    conn,
    txn: ScrapedTransaction,
    product_id: str,
    claimed: set,
    incoming_ids: set[str],
) -> tuple[str, str | None]:
    """Store one scraped transaction and say what became of it.

    Returns the outcome and the stored row it claims, if any. The outcome is
    "inserted" for a new row, "stored" when its key already holds a row
    (found, adopted, or taken by a concurrent insert) and "kept" when a stored
    row with a bank id keeps the movement out (see `_claim_decision`). A
    movement already stored under its key is only claimed; one that adopts a
    stored row (see `write_transactions`) re-keys it in place. `claimed` is
    only read: the caller records the claim once this row's block commits.

    `internal_transfer` is written only by the INSERT: neither a stored nor an
    adopted row has its `is_internal_transfer` touched, so a user who unticks
    it keeps it unticked. A row a scraper flags counts as a user edit for
    `_carries_user_edits`, so the twin cleanup leaves it in place.
    """
    source = f"scraper_{txn.institution}"
    row = conn.execute(
        "SELECT id FROM transactions "
        "WHERE product_id = %s AND external_id = %s",
        (product_id, txn.external_id),
    ).fetchone()
    if row:
        _fill_location(conn, row[0], txn.location)
        return "stored", row[0]

    if _adopts_stored_rows(txn.external_id):
        action, row_id = _claim_decision(
            txn.external_id,
            _sibling_rows(conn, product_id, txn, source),
            claimed,
            incoming_ids,
        )
        if action == "rekey":
            # The dates move with the key: a legacy row is sitting under its
            # posting date, and leaving it there would show the movement on the
            # wrong day and force the same decision again on every later run.
            conn.execute(
                "UPDATE transactions SET external_id = %s, "
                "transaction_date = %s, accounting_date = %s, "
                "scheduled_month = %s, updated_at = now() "
                "WHERE id = %s",
                (
                    txn.external_id,
                    txn.transaction_date,
                    txn.accounting_date,
                    txn.scheduled_month,
                    row_id,
                ),
            )
            _fill_location(conn, row_id, txn.location)
            logger.info(
                "Adopted stored transaction %s onto %s",
                row_id,
                txn.external_id,
            )
            return "stored", row_id
        if action == "keep":
            logger.warning(
                "Transaction %s (%s, %s) has no bank id this run and "
                "matches stored row %s, which has one; left as it is "
                "and not imported",
                txn.external_id,
                txn.transaction_date,
                txn.amount,
                row_id,
            )
            return "kept", row_id

    cur = conn.execute(
        """
        INSERT INTO transactions
            (id, product_id, description, amount, transaction_date,
             accounting_date, scheduled_month, source, external_id,
             is_internal_transfer,
             location_name, location_city, location_country,
             location_latitude, location_longitude)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s)
        ON CONFLICT (product_id, external_id) DO NOTHING
        """,
        (
            str(uuid4()),
            product_id,
            txn.description,
            txn.amount,
            txn.transaction_date,
            txn.accounting_date,
            txn.scheduled_month,
            source,
            txn.external_id,
            txn.internal_transfer,
            *_location_values(txn.location),
        ),
    )
    # A conflict means the row is already stored under this key.
    return ("inserted" if cur.rowcount and cur.rowcount > 0 else "stored"), None


def _lookup_transaction_product(
    conn, institution_slug: str, kind: str, currency: str, external_ref: str
) -> str | None:
    """The existing product a transaction's `product_external_ref` names, or None.

    Lookup only, like an exchange rate's product scope: nothing of the
    institution -> account -> product chain is created. Creating it here would
    file the rows of a per-holding product before its products leg ran, with a
    generic name and no metrics. An inactive product still counts: a retired
    holding keeps importing its movements.
    """
    row = conn.execute(
        "SELECT id FROM institutions WHERE slug = %s", (institution_slug,)
    ).fetchone()
    if not row:
        return None
    return _lookup_product(conn, row[0], kind, currency, external_ref)


def _product_key(txn: ScrapedTransaction) -> tuple:
    """The identity `write_transactions` resolves a transaction's product by."""
    return (txn.institution, txn.product_kind, txn.currency, txn.product_external_ref)


@dataclass(frozen=True)
class TransactionWriteResult:
    """What one `write_transactions` call did.

    `inserted` counts rows newly inserted and `failed` the transactions that
    raised and were skipped. `unresolved` counts the transactions whose
    `product_external_ref` names a product that does not exist yet: nothing
    was written for them, and a later run (after the products leg stored the
    product) lands them. `persisted_external_ids` holds the keys a stored row
    carries once the call returns: inserted, already stored, or adopted onto.
    """

    inserted: int = 0
    failed: int = 0
    unresolved: int = 0
    persisted_external_ids: frozenset[str] = frozenset()


def upsert_transactions(transactions: list[ScrapedTransaction]) -> int:
    """Insert transactions, skipping duplicates. Returns count of new rows.

    See `write_transactions`, which also reports failures and which keys were
    persisted.
    """
    return write_transactions(transactions).inserted


@with_db_retry
def write_transactions(transactions: list[ScrapedTransaction]) -> TransactionWriteResult:
    """Insert transactions, skipping duplicates, and report what was persisted.

    For institutions that re-key their stored rows (see `_adopts_stored_rows`),
    a movement whose key matches nothing is first offered the stored rows it
    could be under an older key, and adopts one instead of inserting a duplicate
    (issue #57): the row's `external_id`, both dates and `scheduled_month` are
    rewritten in place. Everything else keeps the plain
    `ON CONFLICT (product_id, external_id) DO NOTHING` insert.

    A movement that is already stored (or adopted) only ever FILLS its row's
    location: NULL location columns take the scraped values, non-NULL ones and
    rows the user edited the location of are left alone (`_fill_location`).

    Transactions are grouped by (institution, kind, currency,
    product_external_ref). Without a ref they land on the product with no
    external_ref, created if missing (`_resolve_product`). With one they land
    on that existing product, looked up and never created
    (`_lookup_transaction_product`); when it doesn't exist yet they are counted
    in `unresolved` and written by a later run, once the products leg has
    stored it (it runs after this one, so a new holding's first movements wait
    one run).

    A row that never gets adopted, because its movement fell out of the bank's
    window before the first post-#57 scrape, keeps the date it was imported with
    (BanChile's legacy rows hold the *posting* date) and a NULL
    `accounting_date`. Neither is derivable from anything stored, so nothing
    backfills them.

    Each product resolution and each transaction runs in its own
    `conn.transaction()` block, so one that fails (a serialization failure
    included) is rolled back alone and counted in `failed` instead of leaving
    the connection in an aborted transaction that fails every row after it. A
    dropped connection is the exception: it re-raises, and `with_db_retry`
    replays the batch, which is safe because every step is idempotent (rows the
    lost attempt committed are found, not re-inserted, so they are missing from
    `inserted`).
    """
    if not transactions:
        return TransactionWriteResult()

    pool = get_pool()
    inserted = 0
    failed = 0
    unresolved = 0
    persisted: set[str] = set()

    with pool.connection() as conn:
        # None caches a product ref that names no product yet.
        products: dict[tuple, str | None] = {}

        # Every key this scrape carries, per product: a stored row already
        # holding one of them belongs to that movement and is never adopted.
        # Only a product whose block committed is cached.
        incoming: dict[str, set[str]] = {}
        for txn in transactions:
            key = _product_key(txn)
            try:
                if key not in products:
                    with conn.transaction():
                        # A ref never falls back to the get-or-create path: a
                        # created ref-less product of a per-holding kind would
                        # be retired, snapshots and all, by the next product
                        # write of a holding (`_retire_rollup_sibling`).
                        if txn.product_external_ref is None:
                            product_id = _resolve_product(
                                conn, txn.institution, txn.product_kind, txn.currency
                            )
                        else:
                            product_id = _lookup_transaction_product(
                                conn,
                                txn.institution,
                                txn.product_kind,
                                txn.currency,
                                txn.product_external_ref,
                            )
                    products[key] = product_id
                    if product_id is None:
                        # The ref (a holding's id) stays out of the log.
                        logger.warning(
                            "No %s %s product of '%s' with the external_ref a "
                            "transaction names yet; its transactions are left "
                            "for a later run",
                            txn.product_kind,
                            txn.currency,
                            txn.institution,
                        )
            except Exception:
                if conn.broken:
                    raise
                logger.exception(
                    "Failed to resolve the product for: %s", txn.external_id
                )
                continue
            if products[key] is not None:
                incoming.setdefault(products[key], set()).add(txn.external_id)

        claimed: set = set()
        for txn in transactions:
            key = _product_key(txn)
            if key not in products:
                # Its product failed to resolve, and was logged, above.
                failed += 1
                continue
            product_id = products[key]
            if product_id is None:
                unresolved += 1
                continue
            try:
                with conn.transaction():
                    outcome, claim = _write_transaction(
                        conn, txn, product_id, claimed, incoming.get(product_id, set())
                    )
            except Exception:
                if conn.broken:
                    raise
                failed += 1
                logger.exception("Failed to insert transaction: %s", txn.external_id)
                continue
            if claim is not None:
                claimed.add(claim)
            if outcome != "kept":
                persisted.add(txn.external_id)
            if outcome == "inserted":
                inserted += 1

    return TransactionWriteResult(
        inserted=inserted,
        failed=failed,
        unresolved=unresolved,
        persisted_external_ids=frozenset(persisted),
    )


def _like_prefix(prefix: str) -> str:
    """`prefix` as a LIKE pattern, its wildcards escaped (backslash, as above)."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _carries_user_edits(
    is_manually_categorized, is_internal_transfer, notes, location_is_manual=False
) -> bool:
    """True for a row the user touched: categorised, annotated, paired or located.

    A flag a scraper set on insert (`ScrapedTransaction.internal_transfer`)
    can't be told apart from the user's, so it counts too.
    """
    return bool(
        is_manually_categorized
        or is_internal_transfer
        or (notes or "").strip()
        or location_is_manual
    )


def delete_superseded_transactions(
    superseded: list[SupersededTransaction],
    persisted_external_ids: Collection[str],
    protected_external_ids: Collection[str] = frozenset(),
) -> int:
    """Delete the stored rows a scrape has replaced (issue #79). Returns the count.

    Only an item whose `replacement_external_id` is in `persisted_external_ids`
    (`TransactionWriteResult.persisted_external_ids` of this run's write) is
    acted on: a twin is never deleted unless its replacement is stored.

    Each `SupersededTransaction` removes at most one row of its institution's
    (kind, currency) product, one-to-one and oldest first, among the rows its
    own scraper wrote whose `external_id` starts with `external_id_prefix` and
    whose date and amount match exactly. With an `external_id`, only the row
    under that very key qualifies; without one, any such row does. Exact keys
    are claimed first, so a date-and-amount match can never take a row an
    exact key names.

    A row whose key is in `protected_external_ids` (this scrape's own keys) is
    never a candidate: it is a movement the bank still lists, not a leftover.
    Nor is a row created at or after the moment the item's
    `created_before_migration` was applied (per `_migrations.applied_at`); an
    item whose migration is not recorded matches nothing.
    A matching row the user categorised, annotated, gave a location to (or
    cleared one on) or marked as an internal transfer is left in place and
    logged instead.
    """
    replaced = [
        item for item in superseded if item.replacement_external_id in persisted_external_ids
    ]
    if len(replaced) < len(superseded):
        logger.warning(
            "%d superseded transaction(s) left in place: their replacement was "
            "not stored this run",
            len(superseded) - len(replaced),
        )
    superseded = replaced
    if not superseded:
        return 0

    pool = get_pool()
    deleted = 0
    kept: list = []
    claimed: set = set()
    with pool.connection() as conn:
        products: dict[tuple, list] = {}

        def products_of(item: SupersededTransaction) -> list:
            key = (item.institution, item.product_kind, item.currency)
            if key not in products:
                products[key] = [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT p.id FROM products p
                        JOIN accounts a ON p.account_id = a.id
                        JOIN institutions i ON a.institution_id = i.id
                        WHERE i.slug = %s AND p.kind = %s AND p.currency = %s
                          AND p.external_ref IS NULL
                        """,
                        key,
                    ).fetchall()
                ]
            return products[key]

        cutoffs: dict = {}

        def cutoff_of(item: SupersededTransaction):
            name = item.created_before_migration
            if name not in cutoffs:
                row = conn.execute(
                    "SELECT applied_at FROM _migrations WHERE name = %s", (name,)
                ).fetchone()
                cutoffs[name] = row[0] if row else None
                if cutoffs[name] is None:
                    logger.warning(
                        "Migration %s is not recorded; superseded rows bound to "
                        "it are left in place",
                        name,
                    )
            return cutoffs[name]

        # Exact keys first: they name their row, so a looser match must not
        # take it from them.
        ordered = sorted(superseded, key=lambda item: item.external_id is None)
        for item in ordered:
            product_ids = products_of(item)
            cutoff = cutoff_of(item)
            if not product_ids or cutoff is None:
                continue
            exact = item.external_id is not None
            rows = conn.execute(
                """
                SELECT id, external_id, is_manually_categorized,
                       is_internal_transfer, notes, location_is_manual
                FROM transactions
                WHERE product_id = ANY(%s) AND source = %s
                  AND transaction_date = %s AND amount = %s
                  AND external_id LIKE %s
                  AND (%s::text IS NULL OR external_id = %s)
                  AND created_at < %s
                ORDER BY created_at, id
                """,
                (
                    product_ids,
                    f"scraper_{item.institution}",
                    item.transaction_date,
                    item.amount,
                    _like_prefix(item.external_id_prefix),
                    item.external_id,
                    item.external_id,
                    cutoff,
                ),
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in rows
                    if candidate[0] not in claimed
                    and candidate[1] not in protected_external_ids
                ),
                None,
            )
            if row is None:
                continue
            row_id, _, *edits = row
            claimed.add(row_id)
            if _carries_user_edits(*edits):
                kept.append(row_id)
                logger.warning(
                    "Superseded transaction %s left in place: it carries user "
                    "edits (category, notes, transfer pairing or location)",
                    row_id,
                )
                continue
            conn.execute("DELETE FROM transactions WHERE id = %s", (row_id,))
            deleted += 1
            logger.debug(
                "Deleted superseded transaction %s (%s match)",
                row_id,
                "key" if exact else "date and amount",
            )

    if deleted or kept:
        logger.info(
            "Deleted %d superseded transaction(s); %d skipped for user edits",
            deleted,
            len(kept),
        )
    return deleted


@with_db_retry
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


# --- Exchange rates (V024) -----------------------------------------------------


class UnresolvedRateScope(LookupError):
    """An exchange rate names an institution or product the database lacks.

    `run_scraper` turns it into a run warning: the rate is dropped, and nothing
    is created to hang it on.
    """


def _lookup_product(
    conn,
    institution_id,
    kind: str,
    currency: str,
    external_ref: str | None,
):
    """The id of the existing product with this identity, or None.

    The identity rule of `_resolve_product` (the writer's account at the
    institution, i.e. the oldest user's first one, then kind, currency and
    COALESCE(external_ref, '')), but lookup-only: no link of the chain is
    ever created.
    """
    row = conn.execute(
        """
        SELECT p.id FROM products p
        WHERE p.account_id = (
            SELECT a.id FROM accounts a
            WHERE a.institution_id = %s
              AND a.user_id = (SELECT id FROM users ORDER BY created_at LIMIT 1)
            ORDER BY a.display_order LIMIT 1
        )
          AND p.kind = %s AND p.currency = %s
          AND COALESCE(p.external_ref, '') = COALESCE(%s, '')
        """,
        (institution_id, kind, currency, external_ref),
    ).fetchone()
    return row[0] if row else None


def upsert_exchange_rate(rate: ScrapedExchangeRate) -> None:
    """Record the latest reading of one institution or product exchange rate.

    One row per (institution, product or none, base, quote): a re-scrape
    updates the rate, source and `reported_as_of` in place and moves
    `observed_at`, which is what the dashboard's staleness warning reads.
    Raises `UnresolvedRateScope` when the institution or the product the
    rate names does not exist yet.
    """
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM institutions WHERE slug = %s", (rate.institution,)
        ).fetchone()
        if not row:
            raise UnresolvedRateScope(f"institution '{rate.institution}' not found")
        institution_id = row[0]

        product_id = None
        if rate.product is not None:
            ref = rate.product
            product_id = _lookup_product(
                conn, institution_id, ref.kind, ref.currency, ref.external_ref
            )
            if product_id is None:
                raise UnresolvedRateScope(
                    f"no {ref.kind} product in {ref.currency}"
                    + (" with that external_ref" if ref.external_ref else "")
                    + f" at '{rate.institution}'"
                )

        now = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO exchange_rates
                (institution_id, product_id, base_currency, quote_currency, rate,
                 source, reported_as_of, observed_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (
                institution_id,
                COALESCE(product_id, '00000000-0000-0000-0000-000000000000'::uuid),
                base_currency,
                quote_currency
            )
            DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source,
                          reported_as_of = EXCLUDED.reported_as_of,
                          observed_at = EXCLUDED.observed_at,
                          updated_at = EXCLUDED.updated_at
            """,
            (
                institution_id,
                product_id,
                rate.base_currency,
                rate.quote_currency,
                rate.rate,
                rate.source,
                rate.reported_as_of,
                now,
                now,
                now,
            ),
        )
        logger.info(
            "Exchange rate %s %s/%s (%s) = %s",
            rate.institution,
            rate.base_currency,
            rate.quote_currency,
            "product" if product_id is not None else "institution",
            rate.rate,
        )


# --- Price histories (V038) ----------------------------------------------------


class UnresolvedPriceScope(LookupError):
    """A price history names an institution or product the database lacks.

    `run_scraper` turns it into a run warning: the series is dropped, and
    nothing is created to hang it on.
    """


@with_db_retry
def upsert_price_history(history: ScrapedPriceHistory) -> int:
    """Record one product's daily closing prices; return the days written.

    One row per (product, day): a new day is inserted, a day whose price
    changed is updated in place, and an unchanged day is left alone,
    `updated_at` included, so the count returned is the new or changed days.
    Nothing is pruned. Every point lands in one transaction, in the product's
    currency. Raises `UnresolvedPriceScope` when the institution or the
    product does not exist yet; neither is ever created.
    """
    ref = history.product
    pool = get_pool()
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT id FROM institutions WHERE slug = %s", (history.institution,)
        ).fetchone()
        if not row:
            raise UnresolvedPriceScope(
                f"institution '{history.institution}' not found"
            )
        product_id = _lookup_product(
            conn, row[0], ref.kind, ref.currency, ref.external_ref
        )
        if product_id is None:
            raise UnresolvedPriceScope(
                f"no {ref.kind} product in {ref.currency}"
                + (" with that external_ref" if ref.external_ref else "")
                + f" at '{history.institution}'"
            )

        # The lookup matched the product on its currency, so `ref.currency`
        # is the product's own.
        cur = conn.execute(
            """
            INSERT INTO product_prices (product_id, price_date, price, currency)
            SELECT %s, point.day, point.price, %s
            FROM unnest(%s::date[], %s::numeric[]) AS point(day, price)
            ON CONFLICT (product_id, price_date) DO UPDATE
                SET price = EXCLUDED.price, updated_at = now()
                WHERE product_prices.price IS DISTINCT FROM EXCLUDED.price
            """,
            (
                product_id,
                ref.currency,
                [point.date for point in history.points],
                [point.price for point in history.points],
            ),
        )
        written = max(cur.rowcount, 0)
        # Counts only: the prices and the product's ref would say what is held.
        logger.info(
            "Price history %s/%s %s: %d point(s), %d new or changed",
            history.institution,
            ref.kind,
            ref.currency,
            len(history.points),
            written,
        )
        return written
