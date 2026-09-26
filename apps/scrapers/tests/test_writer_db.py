"""The DB writer against a real PostgreSQL whose schema the migrations built.

test_writer.py pins the writer's decisions with fake connections; these tests
run the SQL that carries them out: the institution -> account -> product
resolution chain, transaction dedup and issue #57's in-place re-key, snapshot
history only on change, the roll-up guards and the scraper run log. Each test
gets its own freshly migrated database (see conftest.py) and is skipped unless
TEST_DATABASE_URL is set. Every figure and identifier is synthetic.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from psycopg import sql

from db import writer
from db.writer import (
    finish_scraper_run,
    start_scraper_run,
    upsert_product,
    upsert_transactions,
)
from product_model import (
    CheckingMetrics,
    CreditCardAttributes,
    DebitCardMetrics,
    ScrapedProduct,
    ScrapedTransaction,
    TermDepositMetrics,
)

# The single user V009 seeds; `_resolve_product` attaches everything to it.
SEEDED_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class _Clock:
    """Stands in for `db.writer.datetime`: every now() is one second later.

    Two writes inside one microsecond would collide on the snapshots'
    (product_id, as_of) key, so no assertion depends on the wall clock.
    """

    def __init__(self):
        self._now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self, tz=None):
        self._now += timedelta(seconds=1)
        return self._now


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(writer, "datetime", _Clock())


def _checking(balance=2500000, **fields):
    return ScrapedProduct(
        institution="banchile",
        kind="checking",
        metrics=CheckingMetrics(balance=balance),
        **fields,
    )


def _deposit(balance, external_ref=None):
    return ScrapedProduct(
        institution="banchile",
        kind="term_deposit",
        external_ref=external_ref,
        metrics=TermDepositMetrics(balance=balance),
    )


def _txn(
    external_id,
    institution="mach",
    kind="wallet",
    tx_date=date(2026, 8, 20),
    accounting_date=None,
):
    return ScrapedTransaction(
        institution=institution,
        product_kind=kind,
        description="COMPRA SINTETICA",
        amount=-999999,
        transaction_date=tx_date,
        accounting_date=accounting_date,
        external_id=external_id,
        scheduled_month=date(tx_date.year, tx_date.month, 1),
    )


def _products(conn, institution="banchile"):
    return conn.execute(
        """
        SELECT p.* FROM products p
        JOIN accounts a ON p.account_id = a.id
        JOIN institutions i ON a.institution_id = i.id
        WHERE i.slug = %s
        """,
        (institution,),
    ).fetchall()


def _only_product(conn, institution="banchile"):
    (product,) = _products(conn, institution)
    return product


def _snapshots(conn):
    return conn.execute(
        "SELECT product_id, balance FROM product_snapshots ORDER BY as_of"
    ).fetchall()


def _count(conn, table):
    query = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
    return conn.execute(query).fetchone()["n"]


class TestResolveProduct:
    """`_resolve_product`'s get-or-create chain, through `upsert_product`."""

    @pytest.mark.parametrize(
        ("slug", "name", "kind", "product_slug"),
        [
            ("banchile", "Banco de Chile", "bank", "banco-de-chile-checking"),
            ("banco_sintetico", "banco_sintetico", "other", "banco-sintetico-checking"),
        ],
        ids=["seeded-institution", "new-institution"],
    )
    def test_a_first_write_builds_the_chain(
        self, db_conn, slug, name, kind, product_slug
    ):
        """A seeded institution is reused (an unknown one is created as
        'other'); the account goes to the seeded user and the product takes
        the slug of its default name."""
        upsert_product(
            ScrapedProduct(
                institution=slug,
                kind="checking",
                metrics=CheckingMetrics(balance=2500000),
            )
        )

        (institution,) = db_conn.execute(
            "SELECT * FROM institutions WHERE slug = %s", (slug,)
        ).fetchall()
        (account,) = db_conn.execute(
            "SELECT * FROM accounts WHERE institution_id = %s", (institution["id"],)
        ).fetchall()
        product = _only_product(db_conn, slug)
        assert (institution["name"], institution["kind"]) == (name, kind)
        assert account["user_id"] == SEEDED_USER_ID
        assert product["account_id"] == account["id"]
        assert (product["kind"], product["currency"], product["external_ref"]) == (
            "checking",
            "CLP",
            None,
        )
        assert (product["name"], product["slug"]) == (f"{name} - checking", product_slug)

    def test_a_second_write_reuses_the_product(self, db_conn):
        """No new account or product, and `name` is create-only."""
        upsert_product(_checking())
        upsert_product(_checking(balance=1000000, name="Cuenta Sintetica"))

        product = _only_product(db_conn)
        assert (product["name"], product["slug"]) == (
            "Banco de Chile - checking",
            "banco-de-chile-checking",
        )
        assert _count(db_conn, "accounts") == 1

    @pytest.mark.parametrize(
        ("identity", "slug"),
        [
            ({"external_ref": "00-000-00000-01"}, "banco-de-chile-checking-2"),
            ({"currency": "USD"}, "banco-de-chile-checking-usd"),
        ],
        ids=["external_ref", "currency"],
    )
    def test_a_different_identity_is_a_distinct_product(self, db_conn, identity, slug):
        """A new product whose name slug is taken in the institution gets `-2`."""
        upsert_product(_checking())
        upsert_product(_checking(**identity))

        slugs = {p["slug"] for p in _products(db_conn)}
        assert slugs == {"banco-de-chile-checking", slug}
        assert _count(db_conn, "accounts") == 1


class TestUpsertTransactions:
    """Dedup on (product_id, external_id), and the #57 adoption path."""

    def test_a_new_row_stores_every_field(self, db_conn):
        """Each scraped field lands in its own column, on the resolved product."""
        upsert_transactions(
            [_txn("email_mach_0001", accounting_date=date(2026, 8, 21))]
        )

        row = db_conn.execute(
            """
            SELECT i.slug, p.kind, t.description, t.amount, t.transaction_date,
                   t.accounting_date, t.scheduled_month, t.source, t.external_id
            FROM transactions t
            JOIN products p ON t.product_id = p.id
            JOIN accounts a ON p.account_id = a.id
            JOIN institutions i ON a.institution_id = i.id
            """
        ).fetchone()
        assert row == {
            "slug": "mach",
            "kind": "wallet",
            "description": "COMPRA SINTETICA",
            "amount": -999999,
            "transaction_date": date(2026, 8, 20),
            "accounting_date": date(2026, 8, 21),
            "scheduled_month": date(2026, 8, 1),
            "source": "scraper_mach",
            "external_id": "email_mach_0001",
        }

    def test_the_same_batch_twice_inserts_once(self, db_conn):
        batch = [_txn(f"email_mach_000{n}") for n in range(3)]

        assert (upsert_transactions(batch), upsert_transactions(batch)) == (3, 0)
        assert _count(db_conn, "transactions") == 3

    def test_duplicates_inside_one_batch_insert_once(self, db_conn):
        batch = [_txn("email_mach_0001"), _txn("email_mach_0001"), _txn("email_mach_0002")]

        assert upsert_transactions(batch) == 2
        assert _count(db_conn, "transactions") == 2

    def test_only_new_rows_are_counted(self, db_conn):
        upsert_transactions([_txn("email_mach_0001"), _txn("email_mach_0002")])

        inserted = upsert_transactions(
            [_txn("email_mach_0001"), _txn("email_mach_0002"), _txn("email_mach_0003")]
        )

        assert inserted == 1
        assert _count(db_conn, "transactions") == 3

    def test_the_same_external_id_on_two_products_is_two_rows(self, db_conn):
        """The dedup key is per product, not global."""
        inserted = upsert_transactions(
            [
                _txn("email_tenpo_0001", institution="tenpo", kind="wallet"),
                _txn("email_tenpo_0001", institution="tenpo", kind="prepaid_card"),
            ]
        )

        assert inserted == 2
        assert len(_products(db_conn, "tenpo")) == 2

    def test_a_legacy_banchile_row_is_re_keyed_in_place(self, db_conn):
        """Issue #57: a row stored under its old key and its posting date takes
        the operation id and both dates instead of being imported again."""
        upsert_transactions(
            [
                _txn(
                    "bch_a1b2c3d4e5f60718",
                    institution="banchile",
                    kind="checking",
                    tx_date=date(2026, 8, 24),
                )
            ]
        )
        stored_id = db_conn.execute("SELECT id FROM transactions").fetchone()["id"]

        inserted = upsert_transactions(
            [
                _txn(
                    "bch_op_12345678901",
                    institution="banchile",
                    kind="checking",
                    tx_date=date(2026, 8, 21),
                    accounting_date=date(2026, 8, 24),
                )
            ]
        )

        rows = db_conn.execute(
            "SELECT id, external_id, transaction_date, accounting_date "
            "FROM transactions"
        ).fetchall()
        assert inserted == 0
        assert rows == [
            {
                "id": stored_id,
                "external_id": "bch_op_12345678901",
                "transaction_date": date(2026, 8, 21),
                "accounting_date": date(2026, 8, 24),
            }
        ]


class TestSnapshots:
    """A snapshot row only when the observation changed."""

    def test_an_unchanged_observation_adds_no_snapshot(self, db_conn):
        """The repeat still advances `balance_as_of` ("last confirmed")."""
        upsert_product(_checking())
        first_seen = _only_product(db_conn)["balance_as_of"]

        upsert_product(_checking())

        assert len(_snapshots(db_conn)) == 1
        assert _only_product(db_conn)["balance_as_of"] > first_seen

    def test_a_changed_metric_appends_a_snapshot(self, db_conn):
        upsert_product(_checking(2500000))
        upsert_product(_checking(1000000))

        product = _only_product(db_conn)
        assert [s["balance"] for s in _snapshots(db_conn)] == [2500000, 1000000]
        assert product["current_balance"] == Decimal("1000000")
        assert product["metrics"] == {"kind": "checking", "balance": 1000000}

    def test_a_kind_without_a_headline_gets_no_snapshot(self, db_conn):
        """debit_card's headline is None; only the latest metrics refresh."""
        upsert_product(
            ScrapedProduct(
                institution="banchile", kind="debit_card", metrics=DebitCardMetrics()
            )
        )

        product = _only_product(db_conn)
        assert _snapshots(db_conn) == []
        assert (product["current_balance"], product["metrics"]) == (
            None,
            {"kind": "debit_card"},
        )
        assert product["balance_as_of"] is not None

    def test_attributes_shallow_merge(self, db_conn):
        """A later write without a field keeps the value an earlier one read."""
        upsert_product(
            ScrapedProduct(
                institution="banchile",
                kind="credit_card",
                attributes=CreditCardAttributes(last4="1234", brand="Visa Sintetica"),
            )
        )
        upsert_product(
            ScrapedProduct(
                institution="banchile",
                kind="credit_card",
                attributes=CreditCardAttributes(statement_day=25),
            )
        )

        assert _only_product(db_conn)["attributes"] == {
            "kind": "credit_card",
            "last4": "1234",
            "brand": "Visa Sintetica",
            "statement_day": 25,
        }


class TestRollupGuards:
    """A NULL-ref roll-up and its per-holding products never both count."""

    REF = "00000000000000001"

    def _by_ref(self, conn):
        return {p["external_ref"]: p for p in _products(conn)}

    def test_a_per_holding_write_retires_the_rollup(self, db_conn):
        upsert_product(_deposit(2500000))
        upsert_product(_deposit(1000000, external_ref=self.REF))

        products = self._by_ref(db_conn)
        rollup, holding = products[None], products[self.REF]
        assert rollup["is_active"] is False
        assert (rollup["current_balance"], rollup["metrics"]) == (None, None)
        assert [s["product_id"] for s in _snapshots(db_conn)] == [holding["id"]]
        assert holding["current_balance"] == Decimal("1000000")

    def test_a_retired_rollup_is_left_frozen(self, db_conn):
        upsert_product(_deposit(2500000))
        upsert_product(_deposit(1000000, external_ref=self.REF))

        upsert_product(_deposit(999999))

        rollup = self._by_ref(db_conn)[None]
        assert (rollup["is_active"], rollup["current_balance"]) == (False, None)
        assert rollup["balance_as_of"] is None
        assert len(_snapshots(db_conn)) == 1

    def test_a_rollup_after_per_holding_products_is_dropped(self, db_conn):
        """The late roll-up row exists but takes no balance or history."""
        upsert_product(_deposit(1000000, external_ref=self.REF))

        upsert_product(_deposit(2500000))

        rollup = self._by_ref(db_conn)[None]
        assert (rollup["is_active"], rollup["current_balance"]) == (True, None)
        assert len(_snapshots(db_conn)) == 1


class TestScraperRuns:
    def _run(self, conn, run_id):
        return conn.execute(
            "SELECT * FROM scraper_runs WHERE id = %s", (run_id,)
        ).fetchone()

    def test_a_started_run_is_running(self, db_conn):
        run = self._run(db_conn, start_scraper_run("http_api", "buda"))

        assert (run["method"], run["institution"], run["status"]) == (
            "http_api",
            "buda",
            "running",
        )
        assert run["finished_at"] is None

    @pytest.mark.parametrize(
        ("status", "imported", "message"),
        [("success", 3, None), ("error", 0, "transactions: synthetic failure")],
        ids=["success", "error"],
    )
    def test_finishing_records_the_outcome(self, db_conn, status, imported, message):
        run_id = start_scraper_run("http_api", "buda")

        finish_scraper_run(
            run_id, status, transactions_imported=imported, error_message=message
        )

        run = self._run(db_conn, run_id)
        assert (run["status"], run["transactions_imported"], run["error_message"]) == (
            status,
            imported,
            message,
        )
        assert run["finished_at"] > run["started_at"]
