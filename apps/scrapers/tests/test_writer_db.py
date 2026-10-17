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

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from db import writer
from tests.conftest import apply_migrations
from db.writer import (
    delete_superseded_transactions,
    finish_scraper_run,
    start_scraper_run,
    upsert_product,
    upsert_transactions,
    write_transactions,
)
from product_model import (
    CheckingMetrics,
    CreditCardAttributes,
    DebitCardMetrics,
    ScrapedProduct,
    ScrapedTransaction,
    TermDepositMetrics,
    TransactionLocation,
)
from scrapers.base import SupersededTransaction

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
    amount=-999999,
    currency="CLP",
    location=None,
):
    return ScrapedTransaction(
        institution=institution,
        product_kind=kind,
        description="COMPRA SINTETICA",
        amount=amount,
        transaction_date=tx_date,
        accounting_date=accounting_date,
        external_id=external_id,
        currency=currency,
        scheduled_month=date(tx_date.year, tx_date.month, 1),
        location=location,
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


_LOCATION_COLUMNS = (
    "location_name, location_city, location_country, "
    "location_latitude, location_longitude"
)


def _location_of(conn, external_id):
    return conn.execute(
        f"SELECT {_LOCATION_COLUMNS}, location_is_manual FROM transactions "
        "WHERE external_id = %s",
        (external_id,),
    ).fetchone()


class TestTransactionLocation:
    """V025: inserted with the row, then only ever fills NULL columns."""

    # Synthetic place: no real city, country or coordinates.
    FULL = TransactionLocation(
        name="Tienda Ficticia",
        city="Ciudad Ficticia",
        country="XX",
        latitude=-33.0,
        longitude=-70.0,
    )

    def test_a_new_row_stores_its_location(self, db_conn):
        upsert_transactions([_txn("email_mach_0001", location=self.FULL)])

        assert _location_of(db_conn, "email_mach_0001") == {
            "location_name": "Tienda Ficticia",
            "location_city": "Ciudad Ficticia",
            "location_country": "XX",
            "location_latitude": Decimal("-33.000000"),
            "location_longitude": Decimal("-70.000000"),
            "location_is_manual": False,
        }

    def test_a_row_stored_without_location_gains_it_on_the_next_scrape(self, db_conn):
        upsert_transactions([_txn("email_mach_0001")])
        before = db_conn.execute("SELECT updated_at FROM transactions").fetchone()

        inserted = upsert_transactions(
            [_txn("email_mach_0001", location=TransactionLocation(city="Ciudad Ficticia"))]
        )

        assert inserted == 0
        row = _location_of(db_conn, "email_mach_0001")
        assert row["location_city"] == "Ciudad Ficticia"
        assert row["location_country"] is None
        after = db_conn.execute("SELECT updated_at FROM transactions").fetchone()
        assert after["updated_at"] > before["updated_at"]

    def test_a_non_null_column_is_never_overwritten(self, db_conn):
        upsert_transactions(
            [_txn("email_mach_0001", location=TransactionLocation(city="Ciudad Ficticia"))]
        )

        upsert_transactions(
            [
                _txn(
                    "email_mach_0001",
                    location=TransactionLocation(city="Otra Ciudad", country="XX"),
                )
            ]
        )

        row = _location_of(db_conn, "email_mach_0001")
        assert row["location_city"] == "Ciudad Ficticia"
        assert row["location_country"] == "XX"

    def test_a_location_the_user_edited_is_left_alone(self, db_conn):
        upsert_transactions([_txn("email_mach_0001")])
        db_conn.execute(
            "UPDATE transactions SET location_is_manual = true, "
            "location_name = 'Lugar Manual'"
        )

        upsert_transactions([_txn("email_mach_0001", location=self.FULL)])

        row = _location_of(db_conn, "email_mach_0001")
        assert row["location_name"] == "Lugar Manual"
        assert row["location_city"] is None
        assert row["location_latitude"] is None

    def test_nothing_to_fill_does_not_touch_the_row(self, db_conn):
        upsert_transactions([_txn("email_mach_0001", location=self.FULL)])
        before = db_conn.execute("SELECT updated_at FROM transactions").fetchone()

        upsert_transactions([_txn("email_mach_0001", location=self.FULL)])

        after = db_conn.execute("SELECT updated_at FROM transactions").fetchone()
        assert after == before

    @pytest.mark.parametrize(
        "assignment",
        [
            "location_latitude = 90.5, location_longitude = 0",
            "location_latitude = 0, location_longitude = 180.5",
            "location_latitude = -33",
        ],
    )
    def test_the_schema_rejects_bad_coordinates(self, db_conn, assignment):
        upsert_transactions([_txn("email_mach_0001")])

        with pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(f"UPDATE transactions SET {assignment}")


class TestBanChileUsdCardLocation:
    """The #79 USD statement path carries its location all the way to the row."""

    def _scrape(self, monkeypatch, row):
        from scrapers.backends.banchile_movements import (
            parse_international_billed_movements,
        )
        from scrapers.institutions.banchile import BanChileScraper

        monkeypatch.setenv("BANCHILE_RUT", "test")
        monkeypatch.setenv("BANCHILE_PASSWORD", "test")
        payload = {"seccionCompras": {"transaccionesTarjetas": [row]}}
        (movement,) = parse_international_billed_movements(payload)
        return BanChileScraper()._movement_to_transaction(movement)

    def _row(self, **location):
        """A synthetic USD purchase row; every value is fake."""
        return {
            "numeroDocumento": "2008 " + "1" * 23,
            "fechaTransaccionString": "20/08/2026",
            "descripcion": "COMERCIO SINTETICO",
            "montoDolar": 1234.56,
            "tituloTarjeta": "****1234",
            "totales": False,
            "grupo": "generico",
            "codigoPais": None,
            **location,
        }

    def _stored(self, conn):
        return conn.execute(
            "SELECT p.currency, t.location_city, t.location_country "
            "FROM transactions t JOIN products p ON t.product_id = p.id"
        ).fetchall()

    def test_a_new_usd_charge_is_stored_with_its_location(self, db_conn, monkeypatch):
        txn = self._scrape(
            monkeypatch,
            self._row(ciudadComercio="CIUDAD SINTETICA", pais="PAIS SINTETICO"),
        )

        assert upsert_transactions([txn]) == 1
        assert self._stored(db_conn) == [
            {
                "currency": "USD",
                "location_city": "CIUDAD SINTETICA",
                "location_country": "PAIS SINTETICO",
            }
        ]

    def test_a_usd_charge_stored_before_v024_gains_it_on_rescrape(
        self, db_conn, monkeypatch
    ):
        bare = self._scrape(monkeypatch, self._row(ciudadComercio="", pais=""))
        upsert_transactions([bare])
        assert self._stored(db_conn)[0]["location_city"] is None

        located = self._scrape(
            monkeypatch,
            self._row(ciudadComercio="CIUDAD SINTETICA", pais="PAIS SINTETICO"),
        )

        assert located.external_id == bare.external_id
        assert upsert_transactions([located]) == 0
        assert self._stored(db_conn) == [
            {
                "currency": "USD",
                "location_city": "CIUDAD SINTETICA",
                "location_country": "PAIS SINTETICO",
            }
        ]


class TestDecimalAmounts:
    """Amounts with cents, stored in NUMERIC(18, 2) since V023 (issue #79)."""

    def test_a_usd_amount_keeps_its_cents_on_the_usd_product(self, db_conn):
        """USD 1.234,56 round-trips exactly and lands on the USD product."""
        upsert_transactions(
            [
                _txn(
                    "bch_ref_00000000001",
                    institution="banchile",
                    kind="credit_card",
                    amount=Decimal("-1234.56"),
                    currency="USD",
                )
            ]
        )

        row = db_conn.execute(
            "SELECT t.amount, p.currency FROM transactions t "
            "JOIN products p ON t.product_id = p.id"
        ).fetchone()
        assert row == {"amount": Decimal("-1234.56"), "currency": "USD"}

    def test_a_whole_amount_reads_back_equal_to_the_int_it_was_sent_as(
        self, db_conn
    ):
        upsert_transactions([_txn("email_mach_0001", amount=2500000)])

        amount = db_conn.execute("SELECT amount FROM transactions").fetchone()["amount"]
        assert amount == Decimal("2500000.00") == 2500000

    def test_a_decimal_amount_is_adopted_like_an_integer_one(self, db_conn):
        """The adoption lookup compares `amount = %s` with a Decimal parameter."""
        legacy = dict(
            institution="banchile",
            kind="credit_card",
            amount=Decimal("-1234.56"),
            currency="USD",
            tx_date=date(2026, 8, 21),
        )
        upsert_transactions([_txn("bch_fp_a1b2c3d4e5f60718", **legacy)])

        inserted = upsert_transactions([_txn("bch_ref_00000000001", **legacy)])

        rows = db_conn.execute(
            "SELECT external_id, amount FROM transactions"
        ).fetchall()
        assert inserted == 0
        assert rows == [
            {"external_id": "bch_ref_00000000001", "amount": Decimal("-1234.56")}
        ]


class TestSupersededTransactions:
    """Issue #79: USD charges the CLP-only scraper stored on the CLP card.

    Every row is synthetic: a legacy row holds what that code stored (the USD
    figure rounded to whole "pesos"), next to genuine CLP rows it must spare.
    """

    DAY = date(2026, 8, 20)

    def _card(self, external_id, amount, currency="CLP", tx_date=DAY):
        return _txn(
            external_id,
            institution="banchile",
            kind="credit_card",
            amount=amount,
            currency=currency,
            tx_date=tx_date,
        )

    # The USD row that replaces a twin; persisted unless a test says otherwise.
    REPLACEMENT = "bch_fp_00000000000000ff"
    MIGRATION = "V023__transactions_amount_numeric.sql"

    @pytest.fixture(autouse=True)
    def _fix_ships_tomorrow(self, db_conn):
        """Every row a test stores predates the fix unless the test says not."""
        self._ship_fix(db_conn, "now() + interval '1 day'")

    def _ship_fix(self, conn, when):
        conn.execute(
            sql.SQL("UPDATE _migrations SET applied_at = {} WHERE name = %s").format(
                sql.SQL(when)
            ),
            (self.MIGRATION,),
        )

    def _twin(self, amount, external_id=None, tx_date=DAY):
        return SupersededTransaction(
            institution="banchile",
            product_kind="credit_card",
            currency="CLP",
            transaction_date=tx_date,
            amount=amount,
            replacement_external_id=self.REPLACEMENT,
            created_before_migration=self.MIGRATION,
            external_id=external_id,
            external_id_prefix="bch_fp_",
        )

    def _delete(self, twins, protected=frozenset()):
        return delete_superseded_transactions(twins, {self.REPLACEMENT}, protected)

    def _store(self, conn, *external_ids, amount=-12):
        """CLP-card rows in one batch (so none adopts another), oldest first."""
        upsert_transactions([self._card(key, amount) for key in external_ids])
        for age, key in enumerate(reversed(external_ids)):
            conn.execute(
                "UPDATE transactions SET created_at = now() - make_interval(days => %s) "
                "WHERE external_id = %s",
                (age + 1, key),
            )

    def _keys(self, conn, currency="CLP"):
        return sorted(
            row["external_id"]
            for row in conn.execute(
                "SELECT t.external_id FROM transactions t "
                "JOIN products p ON t.product_id = p.id WHERE p.currency = %s",
                (currency,),
            ).fetchall()
        )

    def test_an_exact_key_deletes_only_its_row(self, db_conn):
        upsert_transactions(
            [
                self._card("bch_fp_000000000000000a", -1235),
                self._card("bch_fp_000000000000000b", -1235),
            ]
        )

        deleted = self._delete(
            [self._twin(-1235, "bch_fp_000000000000000a")]
        )

        assert deleted == 1
        assert self._keys(db_conn) == ["bch_fp_000000000000000b"]

    def test_an_exact_key_still_needs_the_date_and_amount(self, db_conn):
        upsert_transactions([self._card("bch_fp_000000000000000a", -1234)])

        assert self._delete(
            [self._twin(-1235, "bch_fp_000000000000000a")]
        ) == 0

    def test_a_loose_match_is_one_to_one_and_oldest_first(self, db_conn):
        self._store(
            db_conn,
            "bch_fp_000000000000000a",
            "bch_fp_000000000000000b",
            "bch_fp_000000000000000c",
        )

        deleted = self._delete([self._twin(-12), self._twin(-12)])

        assert deleted == 2
        assert self._keys(db_conn) == ["bch_fp_000000000000000c"]

    def test_exact_keys_are_served_before_loose_matches(self, db_conn):
        """A loose match listed first must not take the row a key names."""
        self._store(db_conn, "bch_fp_000000000000000a", "bch_fp_000000000000000b")

        deleted = self._delete(
            [self._twin(-12), self._twin(-12, "bch_fp_000000000000000a")]
        )

        assert deleted == 2
        assert self._keys(db_conn) == []

    def test_only_the_legacy_namespace_on_the_clp_product_qualifies(self, db_conn):
        upsert_transactions(
            [
                self._card("bch_ref_200812345678", -12),
                self._card("bch_op_12345678901", -12),
                self._card("bch_fp_000000000000000a", Decimal("-12.00"), currency="USD"),
                self._card("bch_fp_000000000000000b", -12, tx_date=date(2026, 8, 21)),
                self._card("bch_fp_000000000000000c", -13),
            ]
        )

        assert self._delete([self._twin(-12)]) == 0
        assert len(self._keys(db_conn)) == 4
        assert self._keys(db_conn, "USD") == ["bch_fp_000000000000000a"]

    def test_another_institutions_rows_are_untouched(self, db_conn):
        upsert_transactions(
            [_txn("bch_fp_000000000000000a", institution="tenpo", kind="credit_card", amount=-12)]
        )
        assert self._delete([self._twin(-12)]) == 0

    def test_this_scrapes_own_keys_are_protected(self, db_conn):
        """A row the bank still lists is a genuine CLP movement, not a leftover."""
        upsert_transactions(
            [
                self._card("bch_fp_000000000000000a", -12),
                self._card("bch_fp_000000000000000b", -12),
            ]
        )

        deleted = self._delete(
            [self._twin(-12)], {"bch_fp_000000000000000a"}
        )

        assert deleted == 1
        assert self._keys(db_conn) == ["bch_fp_000000000000000a"]

    @pytest.mark.parametrize(
        "edit",
        [
            "is_manually_categorized = true",
            "is_internal_transfer = true",
            "notes = 'nota sintetica'",
            # A location the user set, or cleared (all columns NULL).
            "location_is_manual = true, location_city = 'Ciudad Ficticia'",
            "location_is_manual = true",
        ],
    )
    def test_a_row_the_user_edited_is_skipped_not_replaced(self, db_conn, edit):
        """The twin is consumed: no other row is deleted in its place."""
        self._store(db_conn, "bch_fp_000000000000000a", "bch_fp_000000000000000b")
        db_conn.execute(
            sql.SQL("UPDATE transactions SET {} WHERE external_id = %s").format(
                sql.SQL(edit)
            ),
            ("bch_fp_000000000000000a",),
        )

        assert self._delete([self._twin(-12)]) == 0
        assert len(self._keys(db_conn)) == 2

    def test_blank_notes_are_not_an_edit(self, db_conn):
        upsert_transactions([self._card("bch_fp_000000000000000a", -12)])
        db_conn.execute("UPDATE transactions SET notes = '  '")

        assert self._delete([self._twin(-12)]) == 1

    def test_a_misrouted_charge_moves_to_the_usd_card(self, db_conn):
        """End to end: the CLP-only row goes, the USD row with cents stays."""
        upsert_transactions(
            [
                self._card("bch_fp_000000000000000a", -1235),  # legacy twin
                self._card("bch_fp_000000000000000b", -999999),  # genuine CLP
            ]
        )
        usd = self._card("bch_fp_00000000000000ff", Decimal("-1234.56"), currency="USD")
        upsert_transactions([usd])

        deleted = self._delete(
            [self._twin(-1235, "bch_fp_000000000000000a")],
            {usd.external_id, "bch_fp_000000000000000b"},
        )

        assert deleted == 1
        assert self._keys(db_conn) == ["bch_fp_000000000000000b"]
        assert db_conn.execute(
            "SELECT t.amount FROM transactions t JOIN products p "
            "ON t.product_id = p.id WHERE p.currency = 'USD'"
        ).fetchone() == {"amount": Decimal("-1234.56")}

    def test_a_twin_whose_replacement_was_not_stored_is_kept(self, db_conn):
        upsert_transactions([self._card("bch_fp_000000000000000a", -12)])

        deleted = delete_superseded_transactions(
            [self._twin(-12, "bch_fp_000000000000000a")], {"bch_fp_00000000000000ee"}
        )

        assert deleted == 0
        assert self._keys(db_conn) == ["bch_fp_000000000000000a"]

    def test_a_batch_a_database_error_rolled_back_deletes_nothing(self, db_conn):
        """One failing row aborts the batch: the replacement written before it
        is rolled back too, so its twin must survive."""
        upsert_transactions([self._card("bch_fp_000000000000000a", -1235)])
        db_conn.execute(
            "ALTER TABLE transactions ADD CONSTRAINT synthetic_failure "
            "CHECK (description <> 'FALLA SINTETICA')"
        )
        usd = self._card(self.REPLACEMENT, Decimal("-1234.56"), currency="USD")
        failing = self._card("bch_fp_00000000000000ee", -999999).model_copy(
            update={"description": "FALLA SINTETICA"}
        )

        written = write_transactions([usd, failing])
        deleted = delete_superseded_transactions(
            [self._twin(-1235, "bch_fp_000000000000000a")],
            written.persisted_external_ids,
            {usd.external_id, failing.external_id},
        )

        assert written.persisted_external_ids == frozenset()
        assert deleted == 0
        assert self._keys(db_conn) == ["bch_fp_000000000000000a"]
        assert self._keys(db_conn, "USD") == []

    def test_a_stored_replacement_lets_its_twin_go(self, db_conn):
        upsert_transactions([self._card("bch_fp_000000000000000a", -1235)])
        usd = self._card(self.REPLACEMENT, Decimal("-1234.56"), currency="USD")

        written = write_transactions([usd])
        deleted = delete_superseded_transactions(
            [self._twin(-1235, "bch_fp_000000000000000a")],
            written.persisted_external_ids,
            {usd.external_id},
        )

        assert written.persisted_external_ids == {self.REPLACEMENT}
        assert deleted == 1
        assert self._keys(db_conn) == []

    def test_rows_stored_after_the_fix_shipped_are_never_deleted(self, db_conn):
        """Neither by exact key nor by date and amount: the fixed code wrote them."""
        self._ship_fix(db_conn, "now() - interval '1 day'")
        upsert_transactions(
            [
                self._card("bch_fp_000000000000000a", -12),
                self._card("bch_fp_000000000000000b", -12),
            ]
        )

        deleted = self._delete([self._twin(-12, "bch_fp_000000000000000a"), self._twin(-12)])

        assert deleted == 0
        assert len(self._keys(db_conn)) == 2

    def test_the_cutoff_splits_older_rows_from_newer_ones(self, db_conn):
        self._store(db_conn, "bch_fp_000000000000000a", "bch_fp_000000000000000b")
        # a is two days old and b one day old; the fix shipped in between.
        self._ship_fix(db_conn, "now() - interval '36 hours'")

        assert self._delete([self._twin(-12), self._twin(-12)]) == 1
        assert self._keys(db_conn) == ["bch_fp_000000000000000b"]

    def test_an_unrecorded_migration_deletes_nothing(self, db_conn):
        upsert_transactions([self._card("bch_fp_000000000000000a", -12)])
        db_conn.execute("DELETE FROM _migrations WHERE name = %s", (self.MIGRATION,))

        assert self._delete([self._twin(-12)]) == 0

    def test_nothing_to_do(self, db_conn):
        assert self._delete([]) == 0
        assert self._delete([self._twin(-12)]) == 0


class TestAmountMigration:
    """V023 turns `transactions.amount` from integer into NUMERIC(18, 2)."""

    def test_existing_integer_amounts_keep_their_exact_values(self, unmigrated_db):
        apply_migrations(unmigrated_db, before="V023")
        amounts = [-999999, 2500000, 1000000, 0]
        legacy = [
            _txn(
                f"bch_a1b2c3d4e5f6071{n}",
                institution="banchile",
                kind="checking",
                amount=amount,
            )
            for n, amount in enumerate(amounts)
        ]
        # Staged with SQL of that era: the writer now also writes V025's
        # location columns, which this schema does not have yet.
        with psycopg.connect(unmigrated_db) as conn:
            product_id = writer._resolve_product(conn, "banchile", "checking", "CLP")
            for txn in legacy:
                conn.execute(
                    "INSERT INTO transactions (product_id, description, amount, "
                    "transaction_date, scheduled_month, source, external_id) "
                    "VALUES (%s, %s, %s, %s, %s, 'scraper_banchile', %s)",
                    (
                        product_id,
                        txn.description,
                        int(txn.amount),
                        txn.transaction_date,
                        txn.scheduled_month,
                        txn.external_id,
                    ),
                )

        apply_migrations(unmigrated_db)

        with psycopg.connect(unmigrated_db, row_factory=dict_row) as conn:
            column = conn.execute(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = 'transactions' AND column_name = 'amount'"
            ).fetchone()
            stored = [
                row["amount"]
                for row in conn.execute(
                    "SELECT amount FROM transactions ORDER BY external_id"
                )
            ]
        assert column == {
            "data_type": "numeric",
            "numeric_precision": 18,
            "numeric_scale": 2,
        }
        assert stored == [Decimal(a) for a in amounts]
        # And an integer from a CLP scraper still finds them: a re-run inserts
        # nothing, and the legacy rows are adopted onto operation ids.
        assert upsert_transactions(legacy) == 0
        operation_id = _txn(
            "bch_op_12345678901",
            institution="banchile",
            kind="checking",
            amount=-999999,
        )
        assert upsert_transactions([operation_id]) == 0


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
