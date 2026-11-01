"""Write scraped data to the database."""

import json
import logging
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    VanishedWindow,
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


def _sibling_rows(
    conn,
    product_id: str,
    txn: ScrapedTransaction,
    source: str,
    claimed: Collection = frozenset(),
    incoming_ids: Collection[str] = frozenset(),
) -> list[tuple]:
    """Stored rows that could be `txn` under a superseded key, best first.

    The rows on `txn`'s own dates (`_same_day_siblings`), then, for a billed
    card charge only, at most one unbilled row a few days off
    (`_nearby_card_sibling`).
    """
    siblings = _same_day_siblings(conn, product_id, txn, source)
    if txn.product_kind == "credit_card" and txn.billing_status == "billed":
        siblings += _nearby_card_sibling(
            conn,
            product_id,
            txn,
            source,
            {row_id for row_id, _ in siblings} | set(claimed),
            incoming_ids,
        )
    return siblings


# How far a card charge's billed date may sit from the date its unbilled row
# was stored under: the bank re-dates a charge by a day or so when it posts.
_CARD_ADOPTION_WINDOW_DAYS = 3
# The key prefix of the unbilled card leg (a fingerprint; it has no bank id).
_FINGERPRINT_LIKE = "bch\\_fp\\_%"
# Description tokens shorter than this are too generic to tie two rows.
_MIN_SHARED_TOKEN = 4


def _description_tokens(description: str | None) -> tuple[list[str], str]:
    """`description`'s uppercase alphanumeric tokens and their concatenation."""
    normalized = unicodedata.normalize("NFKD", description or "")
    ascii_text = normalized.encode("ascii", "ignore").decode().upper()
    tokens = re.findall(r"[A-Z0-9]+", ascii_text)
    return tokens, "".join(tokens)


def _similar_descriptions(left: str | None, right: str | None) -> bool:
    """True when two card descriptions plausibly name the same merchant.

    The unbilled and billed legs spell a merchant differently (a processor
    prefix glued on, a suffix cut off), so this asks only that a token of
    four or more characters of one appears inside the other, spaces and
    punctuation ignored.
    """
    left_tokens, left_joined = _description_tokens(left)
    right_tokens, right_joined = _description_tokens(right)
    return any(
        len(token) >= _MIN_SHARED_TOKEN and token in other
        for tokens, other in ((left_tokens, right_joined), (right_tokens, left_joined))
        for token in tokens
    )


def _closest_unique(candidates: list[tuple], target: date) -> list[tuple]:
    """The one candidate nearest `target`, or none when that distance is shared.

    `candidates` are (id, external_id, transaction_date) rows. Two rows equally
    near are two charges the bank could have re-dated; picking either could
    attach the wrong one, so neither is adopted.
    """
    if not candidates:
        return []
    distance = {row[0]: abs((row[2] - target).days) for row in candidates}
    nearest = min(distance.values())
    closest = [row for row in candidates if distance[row[0]] == nearest]
    if len(closest) != 1:
        return []
    row_id, external_id, _ = closest[0]
    return [(row_id, external_id)]


def _nearby_card_sibling(
    conn,
    product_id: str,
    txn: ScrapedTransaction,
    source: str,
    excluded: Collection,
    incoming_ids: Collection[str],
) -> list[tuple]:
    """The unbilled row a billed card charge was stored as a few days earlier.

    The bank re-dates a charge by a day or so between the unbilled list and
    the statement, so an exact-date match misses it and the charge would be
    stored twice. Only a billed charge looks this far, and only at rows of the
    same product, amount and source still keyed by the unbilled leg's
    fingerprint, not billed and not deleted (a row the vanished-row cleanup
    removed must not hide the charge it turned out to be), within
    `_CARD_ADOPTION_WINDOW_DAYS`, whose
    description is similar, that no movement of this scrape still carries and
    no other movement claimed. Of those, the single nearest one is adopted
    (`_closest_unique`); a tie adopts nothing.
    """
    window = timedelta(days=_CARD_ADOPTION_WINDOW_DAYS)
    rows = conn.execute(
        """
        SELECT id, external_id, transaction_date, description FROM transactions
        WHERE product_id = %s AND amount = %s AND source = %s
          AND external_id LIKE %s
          AND billing_status IS DISTINCT FROM 'billed'
          AND deleted_at IS NULL
          AND transaction_date BETWEEN %s AND %s
        ORDER BY created_at, id
        """,
        (
            product_id,
            txn.amount,
            source,
            _FINGERPRINT_LIKE,
            txn.transaction_date - window,
            txn.transaction_date + window,
        ),
    ).fetchall()
    candidates = [
        (row_id, external_id, tx_date)
        for row_id, external_id, tx_date, description in rows
        if row_id not in excluded
        and external_id not in incoming_ids
        and _similar_descriptions(description, txn.description)
    ]
    return _closest_unique(candidates, txn.transaction_date)


def _same_day_siblings(
    conn, product_id: str, txn: ScrapedTransaction, source: str
) -> list[tuple]:
    """Stored rows on `txn`'s own dates that could be it under an older key.

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


def _billing_fill(
    stored_status: str | None,
    stored_statement_date: date | None,
    status: str | None,
    statement_date: date | None,
) -> tuple[str, date | None] | None:
    """The (billing_status, statement_date) a stored row moves to, or None.

    The status only moves forward (V044): an incoming `unbilled` only fills a
    status never reported, an incoming `billed` replaces a NULL or `unbilled`
    one, and nothing replaces `billed`. A NULL statement date takes the incoming
    one, a stored one is kept, so an installment purchase stays on the statement
    that first billed it. None (no status, or nothing would change) means the
    row is left as it is.
    """
    if status is None:
        return None
    if status == "unbilled":
        return ("unbilled", None) if stored_status is None else None
    kept_date = stored_statement_date or statement_date
    if stored_status == "billed" and kept_date == stored_statement_date:
        return None
    return "billed", kept_date


def _fill_billing(conn, row_id, txn: ScrapedTransaction) -> None:
    """Move the stored row's billing status forward (see `_billing_fill`).

    The row is locked while it is read, so two writers cannot both decide from
    the same stored state. A row with nothing to change is not written, so
    `updated_at` only moves when something changed.
    """
    if txn.billing_status is None:
        return
    stored = conn.execute(
        "SELECT billing_status, statement_date FROM transactions "
        "WHERE id = %s FOR UPDATE",
        (row_id,),
    ).fetchone()
    if stored is None:
        return
    fill = _billing_fill(stored[0], stored[1], txn.billing_status, txn.statement_date)
    if fill is None:
        return
    conn.execute(
        "UPDATE transactions SET billing_status = %s, statement_date = %s, "
        "updated_at = now() WHERE id = %s",
        (*fill, row_id),
    )


def _stored_row_id(conn, product_id: str, external_id: str):
    """The id of the row already stored under this key, or None."""
    row = conn.execute(
        "SELECT id FROM transactions "
        "WHERE product_id = %s AND external_id = %s",
        (product_id, external_id),
    ).fetchone()
    return row[0] if row else None


def _fill_stored_row(conn, row_id, txn: ScrapedTransaction) -> None:
    """Give a stored row what a later scrape may still add to it."""
    _fill_location(conn, row_id, txn.location)
    _fill_billing(conn, row_id, txn)


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

    `billing_status` and `statement_date` are inserted as given; a stored or
    adopted row only has them moved forward (`_fill_billing`), so a charge
    seen billed is never read back as unbilled. A "kept" row is not filled at
    all, location included: the movement is only presumed to be that row.
    """
    source = f"scraper_{txn.institution}"
    row_id = _stored_row_id(conn, product_id, txn.external_id)
    if row_id is not None:
        _fill_stored_row(conn, row_id, txn)
        return "stored", row_id

    if _adopts_stored_rows(txn.external_id):
        action, row_id = _claim_decision(
            txn.external_id,
            _sibling_rows(conn, product_id, txn, source, claimed, incoming_ids),
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
            _fill_stored_row(conn, row_id, txn)
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
             location_latitude, location_longitude,
             billing_status, statement_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s)
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
            txn.billing_status,
            txn.statement_date,
        ),
    )
    if cur.rowcount and cur.rowcount > 0:
        return "inserted", None
    # A conflict: a concurrent writer stored the key after the lookup above.
    # Its row is committed by now, so it is filled like any stored row.
    row_id = _stored_row_id(conn, product_id, txn.external_id)
    if row_id is not None:
        _fill_stored_row(conn, row_id, txn)
    return "stored", row_id


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
    Its billing status only moves forward, from unreported to `unbilled` or
    `billed` and from `unbilled` to `billed`, and a NULL statement date is
    filled while a stored one is kept (`_fill_billing`, V044).

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


# More vanished rows than this in one window reads as a portal that answered
# with a partial list, not as stale authorizations: the window is skipped.
_MAX_VANISHED_PER_WINDOW = 20


def _null_ref_products(conn, institution: str, kind: str, currency: str) -> list:
    """The ids of an institution's (kind, currency) products with no external_ref."""
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT p.id FROM products p
            JOIN accounts a ON p.account_id = a.id
            JOIN institutions i ON a.institution_id = i.id
            WHERE i.slug = %s AND p.kind = %s AND p.currency = %s
              AND p.external_ref IS NULL
            """,
            (institution, kind, currency),
        ).fetchall()
    ]


@with_db_retry
def soft_delete_vanished_transactions(
    windows: list[VanishedWindow], protected_external_ids: Collection[str]
) -> int:
    """Soft-delete the pending rows a scrape no longer lists. Returns the count.

    Each `VanishedWindow` names a stretch of a product's history its scrape
    listed in full (see it, and `BanChileScraper._vanished_for`). A stored row
    in it is a candidate when its own scraper wrote it, its key starts with the
    window's prefix and is not one of `protected_external_ids` (this scrape's
    keys), it is dated after `after`, is not deleted yet, and its billing
    status is `unbilled` (or NULL, for a window that `include_unreported`).

    A candidate the user touched is left in place and logged: categorised,
    annotated, given a custom description, located, flagged as an internal
    transfer, linked to a transfer or used by a monitor adjustment. The rest
    get a `deleted_at` (V033), so they keep their key and a later scrape that
    lists the movement again finds it instead of importing it twice. A window
    with more candidates than `_MAX_VANISHED_PER_WINDOW` is skipped whole.
    """
    if not windows:
        return 0
    protected = set(protected_external_ids)
    pool = get_pool()
    deleted = 0
    with pool.connection() as conn:
        for window in windows:
            with conn.transaction():
                product_ids = _null_ref_products(
                    conn, window.institution, window.product_kind, window.currency
                )
                if not product_ids:
                    continue
                rows = conn.execute(
                    """
                    SELECT t.id, t.external_id, t.is_manually_categorized,
                           t.is_internal_transfer, t.notes, t.location_is_manual,
                           t.custom_description,
                           EXISTS (
                               SELECT 1 FROM internal_transfers it
                               WHERE it.from_transaction_id = t.id
                                  OR it.to_transaction_id = t.id
                           ),
                           EXISTS (
                               SELECT 1 FROM monitor_adjustments ma
                               WHERE ma.transaction_id = t.id
                           )
                    FROM transactions t
                    WHERE t.product_id = ANY(%s) AND t.source = %s
                      AND t.external_id LIKE %s
                      AND t.deleted_at IS NULL
                      AND t.transaction_date > %s
                      AND (t.billing_status = 'unbilled'
                           OR (%s AND t.billing_status IS NULL))
                    ORDER BY t.transaction_date, t.created_at, t.id
                    FOR UPDATE OF t
                    """,
                    (
                        product_ids,
                        f"scraper_{window.institution}",
                        _like_prefix(window.external_id_prefix),
                        window.after,
                        window.include_unreported,
                    ),
                ).fetchall()
                vanished = [row for row in rows if row[1] not in protected]
                if len(vanished) > _MAX_VANISHED_PER_WINDOW:
                    logger.warning(
                        "%d %s %s %s rows after %s are no longer listed, more than "
                        "%d; left in place in case the listing was partial",
                        len(vanished),
                        window.institution,
                        window.product_kind,
                        window.currency,
                        window.after,
                        _MAX_VANISHED_PER_WINDOW,
                    )
                    continue
                doomed = []
                for row_id, _, *flags in vanished:
                    category, transfer, notes, located, custom, linked, adjusted = flags
                    if (
                        _carries_user_edits(category, transfer, notes, located)
                        or (custom or "").strip()
                        or linked
                        or adjusted
                    ):
                        logger.warning(
                            "Vanished transaction %s left in place: it carries user "
                            "edits (category, notes, description, links or location)",
                            row_id,
                        )
                        continue
                    doomed.append(row_id)
                if doomed:
                    conn.execute(
                        "UPDATE transactions SET deleted_at = now(), updated_at = now() "
                        "WHERE id = ANY(%s) AND deleted_at IS NULL",
                        (doomed,),
                    )
                    deleted += len(doomed)
                    logger.info(
                        "Soft-deleted %d %s %s %s row(s) after %s the source no "
                        "longer lists",
                        len(doomed),
                        window.institution,
                        window.product_kind,
                        window.currency,
                        window.after,
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
