"""The DB writer against a real PostgreSQL whose schema the migrations built.

test_writer.py pins the writer's decisions with fake connections; these tests
run the SQL that carries them out: the institution -> account -> product
resolution chain, transaction dedup and issue #57's in-place re-key,
transactions that name an existing product by its ref, snapshot
history only on change, the roll-up guards, the exchange-rate upsert (V024),
the price-history upsert (V038), the stock re-kind (V039), Banco Falabella's
three singleton products and the scraper run log. Each test gets its own freshly migrated database (see conftest.py) and is
skipped unless TEST_DATABASE_URL is set. Every figure and identifier is
synthetic.
"""

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from db import writer
from tests.conftest import MIGRATIONS_DIR, apply_migrations
from db.writer import (
    UnresolvedPriceScope,
    UnresolvedRateScope,
    delete_superseded_transactions,
    finish_scraper_run,
    start_scraper_run,
    upsert_exchange_rate,
    upsert_price_history,
    upsert_product,
    upsert_transactions,
    write_transactions,
)
from product_model import (
    CheckingAttributes,
    CheckingMetrics,
    CreditCardAttributes,
    CreditCardMetrics,
    DailyPrice,
    DebitCardMetrics,
    InvestmentMetrics,
    LineOfCreditAttributes,
    LineOfCreditMetrics,
    ProductRef,
    ScrapedExchangeRate,
    ScrapedPriceHistory,
    ScrapedProduct,
    ScrapedTransaction,
    StockMetrics,
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
        assert (product["name"], product["default_name"], product["slug"]) == (
            f"{name} - checking",
            f"{name} - checking",
            product_slug,
        )

    def test_a_second_write_reuses_the_product(self, db_conn):
        """No new account or product; the slug is create-only, while a name
        the user never changed follows the scraper's (V027)."""
        upsert_product(_checking())
        upsert_product(_checking(balance=1000000, name="Cuenta Sintetica"))

        product = _only_product(db_conn)
        assert (product["name"], product["default_name"], product["slug"]) == (
            "Cuenta Sintetica",
            "Cuenta Sintetica",
            "banco-de-chile-checking",
        )
        assert _count(db_conn, "accounts") == 1

    def test_a_user_rename_survives_every_later_scrape(self, db_conn):
        """The default keeps refreshing underneath, but `name` stays put."""
        upsert_product(_checking())
        db_conn.execute("UPDATE products SET name = 'Mi cuenta de ejemplo'")
        upsert_product(_checking(balance=1000000, name="Cuenta Sintetica"))

        product = _only_product(db_conn)
        assert (product["name"], product["default_name"]) == (
            "Mi cuenta de ejemplo",
            "Cuenta Sintetica",
        )

    def test_a_reset_name_follows_the_scraper_again(self, db_conn):
        """Resetting (name := default_name, as the dashboard does) re-attaches
        the name to the scraper's."""
        upsert_product(_checking())
        db_conn.execute("UPDATE products SET name = 'Mi cuenta de ejemplo'")
        db_conn.execute("UPDATE products SET name = default_name")
        upsert_product(_checking(balance=1000000, name="Cuenta Sintetica"))

        assert _only_product(db_conn)["name"] == "Cuenta Sintetica"

    def test_a_nameless_resolve_keeps_the_scrapers_name(self, db_conn):
        """The transaction path resolves without a name; it must not flip a
        named product back to the generic default on every scrape."""
        upsert_product(_checking(name="Tarjeta Sintetica"))
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            writer._resolve_product(conn, "banchile", "checking", "CLP")

        product = _only_product(db_conn)
        assert (product["name"], product["default_name"]) == (
            "Tarjeta Sintetica",
            "Tarjeta Sintetica",
        )

    def test_a_nameless_first_write_takes_the_generic_default(self, db_conn):
        """A product first created by the transaction path picks up the
        scraper's name on the next named upsert."""
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            writer._resolve_product(conn, "banchile", "checking", "CLP")
        assert _only_product(db_conn)["default_name"] == "Banco de Chile - checking"

        upsert_product(_checking(name="Tarjeta Sintetica"))
        product = _only_product(db_conn)
        assert (product["name"], product["default_name"]) == (
            "Tarjeta Sintetica",
            "Tarjeta Sintetica",
        )

    def test_an_insert_without_default_name_copies_the_name(self, db_conn):
        """V027's trigger keeps an inserter that predates the column working."""
        upsert_product(_checking())
        account_id = _only_product(db_conn)["account_id"]
        db_conn.execute(
            "INSERT INTO products (account_id, name, slug, kind, currency) "
            "VALUES (%s, 'Cuenta de Ejemplo', 'cuenta-de-ejemplo', 'vista', 'CLP')",
            (account_id,),
        )

        (row,) = db_conn.execute(
            "SELECT default_name FROM products WHERE slug = 'cuenta-de-ejemplo'"
        ).fetchall()
        assert row["default_name"] == "Cuenta de Ejemplo"

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

    def test_a_failing_row_in_the_middle_keeps_the_rows_around_it(self, db_conn):
        """Issue #84: a row that trips a constraint is rolled back on its own,
        so the rows written before and after it still commit. `inserted` counts
        only those, and the failing key is never reported as persisted, so the
        run records what actually landed instead of the whole batch (which, in
        one aborted transaction, Postgres would have quietly rolled back).
        """
        db_conn.execute(
            "ALTER TABLE transactions ADD CONSTRAINT synthetic_failure "
            "CHECK (description <> 'FALLA SINTETICA')"
        )
        failing = _txn("email_mach_0002").model_copy(
            update={"description": "FALLA SINTETICA"}
        )

        result = write_transactions(
            [_txn("email_mach_0001"), failing, _txn("email_mach_0003")]
        )

        assert (result.inserted, result.failed) == (2, 1)
        assert result.persisted_external_ids == {"email_mach_0001", "email_mach_0003"}
        stored = [
            row["external_id"]
            for row in db_conn.execute(
                "SELECT external_id FROM transactions ORDER BY external_id"
            ).fetchall()
        ]
        assert stored == ["email_mach_0001", "email_mach_0003"]

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


class TestTransferLinks:
    """V028: a transfer links the scraped rows that moved its money; scraping
    them again must keep the link and the is_internal_transfer flag.

    The links are written the way PUT /api/transfers/[id]/links does: the FK
    on internal_transfers plus the flag on the row. A synthetic $ 1.000.000
    transfer from a checking account to a wallet.
    """

    def _scrape(self, location=None):
        upsert_transactions(
            [
                _txn(
                    "bch_op_00000000001",
                    institution="banchile",
                    kind="checking",
                    amount=-1000000,
                    location=location,
                ),
                _txn("email_mach_0001", amount=1000000, location=location),
            ]
        )

    def _rows(self, conn):
        return {
            row["external_id"]: row
            for row in conn.execute(
                "SELECT id, external_id, product_id, is_internal_transfer "
                "FROM transactions"
            ).fetchall()
        }

    def _link(self, conn):
        rows = self._rows(conn)
        debit, credit = rows["bch_op_00000000001"], rows["email_mach_0001"]
        (transfer,) = conn.execute(
            """
            INSERT INTO internal_transfers
                (description, amount, from_product_id, to_product_id,
                 transfer_date, from_transaction_id, to_transaction_id)
            VALUES ('Traspaso sintetico', 1000000, %s, %s, '2026-08-20', %s, %s)
            RETURNING id
            """,
            (debit["product_id"], credit["product_id"], debit["id"], credit["id"]),
        ).fetchall()
        conn.execute(
            "UPDATE transactions SET is_internal_transfer = true WHERE id = ANY(%s)",
            ([debit["id"], credit["id"]],),
        )
        return transfer["id"], debit["id"], credit["id"]

    def test_a_rescrape_keeps_the_rows_the_flag_and_the_link(self, db_conn):
        self._scrape()
        transfer_id, debit_id, credit_id = self._link(db_conn)

        # Same keys again, this time with a location to fill: the writer
        # touches the stored rows but must not replace or unflag them.
        self._scrape(location=TransactionLocation(city="Ciudad Sintetica"))

        rows = self._rows(db_conn)
        assert _count(db_conn, "transactions") == 2
        assert rows["bch_op_00000000001"]["id"] == debit_id
        assert rows["email_mach_0001"]["id"] == credit_id
        assert all(row["is_internal_transfer"] for row in rows.values())
        link = db_conn.execute(
            "SELECT from_transaction_id, to_transaction_id "
            "FROM internal_transfers WHERE id = %s",
            (transfer_id,),
        ).fetchone()
        assert link == {
            "from_transaction_id": debit_id,
            "to_transaction_id": credit_id,
        }

    def test_one_row_serves_at_most_one_transfer(self, db_conn):
        self._scrape()
        _, debit_id, _ = self._link(db_conn)

        with pytest.raises(psycopg.errors.UniqueViolation):
            db_conn.execute(
                "INSERT INTO internal_transfers "
                "(description, amount, transfer_date, from_transaction_id) "
                "VALUES ('Otro traspaso sintetico', 1000000, '2026-08-20', %s)",
                (debit_id,),
            )

    def test_deleting_a_linked_row_keeps_the_transfer(self, db_conn):
        self._scrape()
        transfer_id, debit_id, credit_id = self._link(db_conn)

        db_conn.execute("DELETE FROM transactions WHERE id = %s", (debit_id,))

        link = db_conn.execute(
            "SELECT from_transaction_id, to_transaction_id "
            "FROM internal_transfers WHERE id = %s",
            (transfer_id,),
        ).fetchone()
        assert link == {"from_transaction_id": None, "to_transaction_id": credit_id}


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


class TestProductRefTransactions:
    """A transaction naming its product by `product_external_ref` lands on that
    existing product only; nothing is ever created for it.

    Two synthetic USD stock positions at Fintual: stock:1001 (ACME) and
    stock:1002 (GLBX).
    """

    def _position(self, ref, name):
        upsert_product(
            ScrapedProduct(
                institution="fintual",
                kind="stock",
                currency="USD",
                external_ref=ref,
                name=name,
                metrics=StockMetrics(nav=1234.56, deposited=999.99, profit=234.57),
            )
        )

    def _txn(
        self,
        external_id,
        ref="stock:1001",
        institution="fintual",
        internal=True,
        kind="stock",
        amount="1234.56",
    ):
        return ScrapedTransaction(
            institution=institution,
            product_kind=kind,
            product_external_ref=ref,
            description="Compra ACME",
            amount=Decimal(amount),
            currency="USD",
            transaction_date=date(2026, 1, 2),
            external_id=external_id,
            scheduled_month=date(2026, 1, 1),
            internal_transfer=internal,
        )

    def _rows(self, conn):
        return conn.execute(
            """
            SELECT t.external_id, p.external_ref, t.is_internal_transfer
            FROM transactions t JOIN products p ON t.product_id = p.id
            ORDER BY t.external_id, p.external_ref
            """
        ).fetchall()

    def test_each_row_lands_on_its_own_product(self, db_conn):
        self._position("stock:1001", "Acme Corp")
        self._position("stock:1002", "Globex Corporation")

        result = write_transactions(
            [
                self._txn("fintual_stock_buy_1001"),
                self._txn("fintual_stock_buy_1002", ref="stock:1002"),
            ]
        )

        assert (result.inserted, result.unresolved) == (2, 0)
        assert self._rows(db_conn) == [
            {
                "external_id": "fintual_stock_buy_1001",
                "external_ref": "stock:1001",
                "is_internal_transfer": True,
            },
            {
                "external_id": "fintual_stock_buy_1002",
                "external_ref": "stock:1002",
                "is_internal_transfer": True,
            },
        ]
        assert len(_products(db_conn, "fintual")) == 2

    def test_a_missing_product_creates_nothing_and_is_unresolved(self, db_conn):
        self._position("stock:1001", "Acme Corp")
        products_before = _count(db_conn, "products")

        result = write_transactions(
            [
                self._txn("fintual_stock_buy_1001"),
                self._txn("fintual_stock_buy_1003", ref="stock:1003"),
            ]
        )

        assert (result.inserted, result.failed, result.unresolved) == (1, 0, 1)
        assert result.persisted_external_ids == {"fintual_stock_buy_1001"}
        assert _count(db_conn, "products") == products_before
        assert [row["external_id"] for row in self._rows(db_conn)] == [
            "fintual_stock_buy_1001"
        ]

    def test_a_ref_never_falls_back_to_the_ref_less_product(self, db_conn):
        """An existing stock/USD product without a ref is not a fallback:
        a row lands on the product its ref names, or waits for it."""
        upsert_product(
            ScrapedProduct(
                institution="fintual",
                kind="stock",
                currency="USD",
                name="Acciones sinteticas",
                metrics=StockMetrics(nav=999.99),
            )
        )
        products_before = _count(db_conn, "products")

        result = write_transactions([self._txn("fintual_stock_buy_1003", ref="stock:1003")])

        assert (result.inserted, result.failed, result.unresolved) == (0, 0, 1)
        assert _count(db_conn, "transactions") == 0
        assert _count(db_conn, "products") == products_before
        (product,) = _products(db_conn, "fintual")
        assert (product["external_ref"], product["is_active"]) == (None, True)

    def test_a_mixed_wallet_and_stock_batch_lands_on_each_product(self, db_conn):
        """Fintual's shape: wallet rows by kind alone (their product created
        if missing), stock mirrors by ref, one missing ref left for later."""
        self._position("stock:1001", "Acme Corp")

        result = write_transactions(
            [
                self._txn("fintual_wallet_dep_2001", ref=None, kind="wallet"),
                self._txn("fintual_wallet_buy_5001", ref=None, kind="wallet", amount="-999.99"),
                self._txn("fintual_stock_buy_5001", amount="999.99"),
                self._txn("fintual_wallet_div_6001", ref=None, kind="wallet", amount="12.34"),
                self._txn("fintual_stock_div_6001", amount="12.34", internal=False),
                self._txn("fintual_stock_buy_5002", ref="stock:1002", amount="100.00"),
            ]
        )

        assert (result.inserted, result.failed, result.unresolved) == (5, 0, 1)
        rows = db_conn.execute(
            """
            SELECT t.external_id, p.kind, p.external_ref, t.amount,
                   t.is_internal_transfer
            FROM transactions t JOIN products p ON t.product_id = p.id
            ORDER BY t.external_id
            """
        ).fetchall()
        assert [tuple(row.values()) for row in rows] == [
            ("fintual_stock_buy_5001", "stock", "stock:1001", Decimal("999.99"), True),
            ("fintual_stock_div_6001", "stock", "stock:1001", Decimal("12.34"), False),
            ("fintual_wallet_buy_5001", "wallet", None, Decimal("-999.99"), True),
            ("fintual_wallet_dep_2001", "wallet", None, Decimal("1234.56"), True),
            ("fintual_wallet_div_6001", "wallet", None, Decimal("12.34"), True),
        ]
        products = _products(db_conn, "fintual")
        assert sorted((p["kind"], p["external_ref"] or "") for p in products) == [
            ("stock", "stock:1001"),
            ("wallet", ""),
        ]
        assert all(p["is_active"] for p in products)

    def test_a_missing_institution_is_unresolved_and_not_created(self, db_conn):
        institutions_before = _count(db_conn, "institutions")

        result = write_transactions(
            [self._txn("sintetico_stock_buy_1001", institution="banco_sintetico")]
        )

        assert (result.inserted, result.unresolved) == (0, 1)
        assert _count(db_conn, "institutions") == institutions_before
        assert _count(db_conn, "transactions") == 0

    def test_the_same_external_id_on_two_products_inserts_twice(self, db_conn):
        """The dedup key is per product, so each position keeps its own row."""
        self._position("stock:1001", "Acme Corp")
        self._position("stock:1002", "Globex Corporation")

        inserted = upsert_transactions(
            [
                self._txn("fintual_stock_buy_1001"),
                self._txn("fintual_stock_buy_1001", ref="stock:1002"),
            ]
        )

        assert inserted == 2

    def test_sending_the_batch_again_inserts_nothing(self, db_conn):
        self._position("stock:1001", "Acme Corp")
        batch = [self._txn("fintual_stock_buy_1001"), self._txn("fintual_stock_div_1001")]

        assert (upsert_transactions(batch), upsert_transactions(batch)) == (2, 0)

    def test_the_flag_is_stored_once_and_a_cleared_one_stays_cleared(self, db_conn):
        """The user's untick wins over every later scrape."""
        self._position("stock:1001", "Acme Corp")
        upsert_transactions([self._txn("fintual_stock_buy_1001")])
        db_conn.execute("UPDATE transactions SET is_internal_transfer = false")

        upsert_transactions([self._txn("fintual_stock_buy_1001")])

        (row,) = self._rows(db_conn)
        assert row["is_internal_transfer"] is False

    def test_an_unflagged_row_is_stored_unflagged(self, db_conn):
        self._position("stock:1001", "Acme Corp")

        upsert_transactions([self._txn("fintual_stock_div_1001", internal=False)])

        (row,) = self._rows(db_conn)
        assert row["is_internal_transfer"] is False

    def test_an_inactive_product_still_receives_rows(self, db_conn):
        """A retired holding keeps importing its movements."""
        self._position("stock:1001", "Acme Corp")
        db_conn.execute("UPDATE products SET is_active = false")

        result = write_transactions([self._txn("fintual_stock_sell_1001")])

        assert (result.inserted, result.unresolved) == (1, 0)


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

    def test_a_failing_row_keeps_only_its_own_twin(self, db_conn):
        """A row that fails is rolled back alone: the replacement written
        before it stays stored, so its twin goes and the failed key is not
        reported as persisted."""
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

        assert written.persisted_external_ids == {self.REPLACEMENT}
        assert deleted == 1
        assert self._keys(db_conn) == []
        assert self._keys(db_conn, "USD") == [self.REPLACEMENT]

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
        # location columns and V027's default_name, which this schema does
        # not have yet.
        with psycopg.connect(unmigrated_db) as conn:
            (account_id,) = conn.execute(
                "INSERT INTO accounts (user_id, institution_id) "
                "SELECT %s, id FROM institutions WHERE slug = 'banchile' "
                "RETURNING id",
                (SEEDED_USER_ID,),
            ).fetchone()
            (product_id,) = conn.execute(
                "INSERT INTO products (account_id, name, slug, kind, currency) "
                "VALUES (%s, 'Banco de Chile - checking', "
                "'banco-de-chile-checking', 'checking', 'CLP') RETURNING id",
                (account_id,),
            ).fetchone()
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


class TestStockKindMigration:
    """V039 re-kinds Fintual's stock positions from `investment` to `stock`,
    payloads and snapshot history included; the goals stay `investment`, and
    a stock ref can never be an `investment` again."""

    _AS_OF = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)

    def _stage(
        self,
        conn,
        ref,
        *,
        kind="investment",
        currency="USD",
        attributes=None,
        metrics=None,
        snapshots=(),
        is_active=True,
    ):
        """One product at Fintual plus its snapshot payloads, one day apart."""
        (product_id,) = conn.execute(
            "INSERT INTO products (account_id, name, slug, kind, currency, "
            "external_ref, attributes, metrics, current_balance, is_active) "
            "SELECT a.id, %s, %s, %s, %s, %s, %s, %s, %s, %s FROM accounts a "
            "JOIN institutions i ON a.institution_id = i.id "
            "WHERE i.slug = 'fintual' RETURNING id",
            (
                f"Producto {ref}",
                f"producto-{ref.replace(':', '-')}",
                kind,
                currency,
                ref,
                Jsonb(attributes if attributes is not None else {}),
                Jsonb(metrics) if metrics is not None else None,
                metrics["nav"] if metrics else None,
                is_active,
            ),
        ).fetchone()
        for n, payload in enumerate(snapshots):
            conn.execute(
                "INSERT INTO product_snapshots (product_id, balance, metrics, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (
                    product_id,
                    payload.get("nav", 0),
                    Jsonb(payload),
                    self._AS_OF + timedelta(days=n),
                ),
            )
        return product_id

    def _state(self, dsn):
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            products = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT id, kind, attributes, metrics, is_active FROM products"
                )
            }
            snapshots: dict = {}
            for row in conn.execute(
                "SELECT product_id, metrics FROM product_snapshots ORDER BY as_of"
            ):
                snapshots.setdefault(row["product_id"], []).append(row["metrics"])
        return products, snapshots

    def test_stock_positions_move_to_the_stock_kind(self, unmigrated_db):
        apply_migrations(unmigrated_db, before="V039")
        held = {"kind": "investment", "nav": 1234.56, "units": 2.5}
        goal_metrics = {"kind": "investment", "nav": 2500000}
        with psycopg.connect(unmigrated_db) as conn:
            conn.execute(
                "INSERT INTO accounts (user_id, institution_id) "
                "SELECT %s, id FROM institutions WHERE slug = 'fintual'",
                (SEEDED_USER_ID,),
            )
            stock_id = self._stage(
                conn,
                "stock:0001",
                attributes={"kind": "investment", "ticker": "ACME"},
                metrics=held,
                # A legacy observation without a payload, then a typed one.
                snapshots=({}, held),
            )
            # Confirmed but never observed: no metrics at all.
            unobserved_id = self._stage(conn, "stock:0002")
            # V011's check admits attributes without a "kind" key.
            untagged_id = self._stage(
                conn,
                "stock:0003",
                attributes={"ticker": "GLBX"},
                metrics={"kind": "investment", "nav": 0.0, "units": 0.0},
            )
            retired_id = self._stage(
                conn,
                "stock:0004",
                attributes={"kind": "investment", "ticker": "INITK"},
                metrics={"kind": "investment", "nav": 0.0},
                snapshots=({"kind": "investment", "nav": 0.0},),
                is_active=False,
            )
            goal_id = self._stage(
                conn,
                "1001",
                currency="CLP",
                metrics=goal_metrics,
                snapshots=(goal_metrics,),
            )

        apply_migrations(unmigrated_db)
        # Re-running the statements finds nothing left to move.
        migration = (MIGRATIONS_DIR / "V039__stock_product_kind.sql").read_text(
            encoding="utf-8"
        )
        with psycopg.connect(unmigrated_db) as conn:
            conn.execute(migration)

        products, snapshots = self._state(unmigrated_db)
        for product_id in (stock_id, unobserved_id, untagged_id, retired_id):
            assert products[product_id]["kind"] == "stock"
        assert products[stock_id]["attributes"] == {"kind": "stock", "ticker": "ACME"}
        assert products[stock_id]["metrics"] == {**held, "kind": "stock"}
        assert snapshots[stock_id] == [{}, {**held, "kind": "stock"}]
        assert products[unobserved_id]["metrics"] is None
        assert products[unobserved_id]["attributes"] == {}
        assert unobserved_id not in snapshots
        assert products[untagged_id]["attributes"] == {"ticker": "GLBX"}
        assert products[untagged_id]["metrics"]["kind"] == "stock"
        assert products[retired_id]["is_active"] is False
        assert products[retired_id]["attributes"]["kind"] == "stock"
        assert snapshots[retired_id] == [{"kind": "stock", "nav": 0.0}]
        assert products[goal_id]["kind"] == "investment"
        assert products[goal_id]["metrics"] == goal_metrics
        assert snapshots[goal_id] == [goal_metrics]

        # The next scrape of the position updates the re-kinded product.
        upsert_product(
            ScrapedProduct(
                institution="fintual",
                kind="stock",
                currency="USD",
                external_ref="stock:0001",
                name="Acme Corp",
                metrics=StockMetrics(nav=1234.56, units=2.5),
            )
        )
        with psycopg.connect(unmigrated_db, row_factory=dict_row) as conn:
            refs = sorted(
                (row["kind"], row["external_ref"])
                for row in _products(conn, "fintual")
            )
        assert refs == [
            ("investment", "1001"),
            ("stock", "stock:0001"),
            ("stock", "stock:0002"),
            ("stock", "stock:0003"),
            ("stock", "stock:0004"),
        ]

    def test_an_old_scraper_cannot_add_an_investment_stock(self, db_conn):
        """A scraper still on the old code writes a position as an
        `investment`: the guard fails that write instead of adding a twin."""
        upsert_product(
            ScrapedProduct(
                institution="fintual",
                kind="stock",
                currency="USD",
                external_ref="stock:0001",
                name="Acme Corp",
                metrics=StockMetrics(nav=1234.56, units=2.5),
            )
        )

        with pytest.raises(
            psycopg.errors.CheckViolation, match="products_stock_ref_kind_check"
        ):
            upsert_product(
                ScrapedProduct(
                    institution="fintual",
                    kind="investment",
                    currency="USD",
                    external_ref="stock:0001",
                    name="Acme Corp",
                    metrics=InvestmentMetrics(nav=1234.56),
                )
            )

        assert [
            (p["kind"], p["external_ref"]) for p in _products(db_conn, "fintual")
        ] == [("stock", "stock:0001")]
        # A goal (a bare numeric ref) is still an `investment`.
        upsert_product(
            ScrapedProduct(
                institution="fintual",
                kind="investment",
                external_ref="1001",
                name="Meta sintetica",
                metrics=InvestmentMetrics(nav=2500000),
            )
        )
        assert len(_products(db_conn, "fintual")) == 2


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


def _usd_card(external_ref=None):
    return ScrapedProduct(
        institution="banchile",
        kind="credit_card",
        currency="USD",
        external_ref=external_ref,
        metrics=CreditCardMetrics(available=1234.56, limit=2000.0),
    )


def _usd_rate(
    rate="950.00",
    product=True,
    reported_as_of=date(2026, 1, 1),
    institution="banchile",
    external_ref=None,
):
    """A synthetic USD rate: on the USD card, or institution-wide without `product`."""
    ref = ProductRef(kind="credit_card", currency="USD", external_ref=external_ref)
    return ScrapedExchangeRate(
        institution=institution,
        base_currency="USD",
        rate=rate,
        product=ref if product else None,
        source="Pago internacional de tarjeta",
        reported_as_of=reported_as_of,
    )


class TestExchangeRates:
    """`upsert_exchange_rate` keeps the latest rate per scope (V024)."""

    def _rates(self, conn):
        return conn.execute(
            "SELECT * FROM exchange_rates ORDER BY product_id NULLS FIRST"
        ).fetchall()

    def test_a_product_rate_lands_on_the_existing_product(self, db_conn):
        upsert_product(_usd_card())
        card = _only_product(db_conn)

        upsert_exchange_rate(_usd_rate())

        (row,) = self._rates(db_conn)
        institution_id = db_conn.execute(
            "SELECT id FROM institutions WHERE slug = 'banchile'"
        ).fetchone()["id"]
        assert row["product_id"] == card["id"]
        assert row["institution_id"] == institution_id
        assert (row["base_currency"], row["quote_currency"]) == ("USD", "CLP")
        assert row["rate"] == Decimal("950.00")
        assert row["source"] == "Pago internacional de tarjeta"
        assert row["reported_as_of"] == date(2026, 1, 1)
        assert row["observed_at"] == row["created_at"] == row["updated_at"]

    def test_a_rescrape_updates_the_row_in_place(self, db_conn):
        upsert_product(_usd_card())
        upsert_exchange_rate(_usd_rate())
        (first,) = self._rates(db_conn)

        upsert_exchange_rate(_usd_rate(rate="999.99", reported_as_of=date(2026, 1, 2)))

        (row,) = self._rates(db_conn)
        assert row["id"] == first["id"]
        assert row["rate"] == Decimal("999.99")
        assert row["reported_as_of"] == date(2026, 1, 2)
        assert row["observed_at"] > first["observed_at"]
        assert row["updated_at"] == row["observed_at"]
        assert row["created_at"] == first["created_at"]

    def test_an_unchanged_rate_still_moves_observed_at(self, db_conn):
        """observed_at means "last read", which is what staleness compares."""
        upsert_product(_usd_card())
        upsert_exchange_rate(_usd_rate())
        (first,) = self._rates(db_conn)

        upsert_exchange_rate(_usd_rate())

        (row,) = self._rates(db_conn)
        assert row["rate"] == first["rate"]
        assert row["observed_at"] > first["observed_at"]

    def test_institution_and_product_scopes_coexist(self, db_conn):
        upsert_product(_usd_card())
        card = _only_product(db_conn)

        upsert_exchange_rate(_usd_rate(rate="999.99", product=False))
        upsert_exchange_rate(_usd_rate(rate="950.00"))
        upsert_exchange_rate(_usd_rate(rate="1234.56", product=False))

        institution_wide, product_scoped = self._rates(db_conn)
        assert (institution_wide["product_id"], institution_wide["rate"]) == (
            None,
            Decimal("1234.56"),
        )
        assert (product_scoped["product_id"], product_scoped["rate"]) == (
            card["id"],
            Decimal("950.00"),
        )
        assert institution_wide["institution_id"] == product_scoped["institution_id"]

    def test_an_unknown_product_is_reported_and_nothing_is_created(self, db_conn):
        with pytest.raises(UnresolvedRateScope, match="no credit_card product in USD"):
            upsert_exchange_rate(_usd_rate())

        assert _products(db_conn) == []
        assert self._rates(db_conn) == []

    def test_the_product_identity_includes_external_ref(self, db_conn):
        """Same rule as `_resolve_product`: a per-holding product is another one."""
        upsert_product(_usd_card(external_ref="0000"))

        with pytest.raises(UnresolvedRateScope):
            upsert_exchange_rate(_usd_rate())
        upsert_exchange_rate(_usd_rate(external_ref="0000"))

        (row,) = self._rates(db_conn)
        assert row["product_id"] == _only_product(db_conn)["id"]
        assert len(_products(db_conn)) == 1

    def test_an_unknown_institution_is_reported_and_not_created(self, db_conn):
        with pytest.raises(UnresolvedRateScope, match="institution 'sintetico'"):
            upsert_exchange_rate(_usd_rate(product=False, institution="sintetico"))

        assert (
            db_conn.execute(
                "SELECT 1 FROM institutions WHERE slug = 'sintetico'"
            ).fetchone()
            is None
        )
        assert self._rates(db_conn) == []

    def test_deleting_the_product_drops_its_rate(self, db_conn):
        upsert_product(_usd_card())
        upsert_exchange_rate(_usd_rate())
        upsert_exchange_rate(_usd_rate(product=False))

        db_conn.execute("DELETE FROM product_snapshots")
        db_conn.execute("DELETE FROM products")

        (row,) = self._rates(db_conn)
        assert row["product_id"] is None

    @pytest.mark.parametrize(
        ("column", "value"),
        [("rate", 0), ("quote_currency", "USD")],
        ids=["non-positive-rate", "same-currency"],
    )
    def test_the_table_rejects_what_the_envelope_rejects(self, db_conn, column, value):
        institution_id = db_conn.execute(
            "SELECT id FROM institutions WHERE slug = 'banchile'"
        ).fetchone()["id"]
        values = {"rate": Decimal("950.00"), "quote_currency": "CLP", column: value}

        with pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(
                "INSERT INTO exchange_rates "
                "(institution_id, base_currency, quote_currency, rate, source) "
                "VALUES (%s, 'USD', %s, %s, 'Sintetico')",
                (institution_id, values["quote_currency"], values["rate"]),
            )


def _stock_position(external_ref, name):
    return ScrapedProduct(
        institution="fintual",
        kind="stock",
        currency="USD",
        external_ref=external_ref,
        name=name,
        metrics=StockMetrics(nav=250.5, deposited=100.0, profit=150.5),
    )


def _closes(*points, external_ref="stock:0001", institution="fintual"):
    """A synthetic daily series; each point is (YYYY-MM-DD, price)."""
    return ScrapedPriceHistory(
        institution=institution,
        product=ProductRef(
            kind="stock", currency="USD", external_ref=external_ref
        ),
        points=[DailyPrice(date=day, price=price) for day, price in points],
    )


class TestPriceHistory:
    """`upsert_price_history` keeps one close per product and day (V038).

    Two synthetic USD stock positions at Fintual: stock:0001 (ACME) and
    stock:0002 (GLBX).
    """

    def _prices(self, conn):
        return conn.execute(
            """
            SELECT pp.*, p.external_ref FROM product_prices pp
            JOIN products p ON pp.product_id = p.id
            ORDER BY p.external_ref, pp.price_date
            """
        ).fetchall()

    def _positions(self):
        upsert_product(_stock_position("stock:0001", "Acme Corp"))
        upsert_product(_stock_position("stock:0002", "Globex Corporation"))

    def test_a_series_lands_on_its_product_in_its_currency(self, db_conn):
        self._positions()

        written = upsert_price_history(
            _closes(
                ("2026-01-02", "100.0"),
                ("2026-01-05", "250.5"),
                external_ref="stock:0002",
            )
        )

        rows = self._prices(db_conn)
        assert written == 2
        assert [
            (r["external_ref"], r["price_date"], r["price"], r["currency"])
            for r in rows
        ] == [
            ("stock:0002", date(2026, 1, 2), Decimal("100.0"), "USD"),
            ("stock:0002", date(2026, 1, 5), Decimal("250.5"), "USD"),
        ]
        assert all(r["created_at"] == r["updated_at"] for r in rows)

    def test_sending_the_series_again_changes_nothing(self, db_conn):
        self._positions()
        series = _closes(("2026-01-02", "100.0"), ("2026-01-05", "250.5"))
        upsert_price_history(series)
        first = self._prices(db_conn)

        written = upsert_price_history(series)

        assert written == 0
        assert self._prices(db_conn) == first

    def test_a_changed_close_is_updated_in_place(self, db_conn):
        self._positions()
        upsert_price_history(_closes(("2026-01-02", "100.0"), ("2026-01-05", "250.5")))
        before = {r["price_date"]: r for r in self._prices(db_conn)}

        written = upsert_price_history(
            _closes(("2026-01-05", "250.75"), ("2026-01-06", "250.5"))
        )

        after = {r["price_date"]: r for r in self._prices(db_conn)}
        assert written == 2
        assert sorted(after) == [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
        changed, kept = after[date(2026, 1, 5)], after[date(2026, 1, 2)]
        assert changed["id"] == before[date(2026, 1, 5)]["id"]
        assert changed["price"] == Decimal("250.75")
        assert changed["created_at"] == before[date(2026, 1, 5)]["created_at"]
        assert changed["updated_at"] > before[date(2026, 1, 5)]["updated_at"]
        assert kept == before[date(2026, 1, 2)]

    def test_an_unknown_product_is_reported_and_nothing_is_created(self, db_conn):
        upsert_product(_stock_position("stock:0001", "Acme Corp"))

        with pytest.raises(
            UnresolvedPriceScope,
            match="no stock product in USD with that external_ref at 'fintual'",
        ):
            upsert_price_history(
                _closes(("2026-01-02", "100.0"), external_ref="stock:0003")
            )

        refs = [p["external_ref"] for p in _products(db_conn, "fintual")]
        assert refs == ["stock:0001"]
        assert _count(db_conn, "product_prices") == 0

    def test_an_unknown_institution_is_reported_and_not_created(self, db_conn):
        with pytest.raises(UnresolvedPriceScope, match="institution 'sintetico'"):
            upsert_price_history(
                _closes(("2026-01-02", "100.0"), institution="sintetico")
            )

        assert (
            db_conn.execute(
                "SELECT 1 FROM institutions WHERE slug = 'sintetico'"
            ).fetchone()
            is None
        )
        assert _count(db_conn, "product_prices") == 0

    def test_deleting_the_product_drops_its_prices(self, db_conn):
        self._positions()
        upsert_price_history(_closes(("2026-01-02", "100.0")))
        upsert_price_history(
            _closes(("2026-01-02", "250.5"), external_ref="stock:0002")
        )

        db_conn.execute(
            "DELETE FROM product_snapshots WHERE product_id = "
            "(SELECT id FROM products WHERE external_ref = 'stock:0001')"
        )
        db_conn.execute("DELETE FROM products WHERE external_ref = 'stock:0001'")

        assert [r["external_ref"] for r in self._prices(db_conn)] == ["stock:0002"]

    def test_the_table_rejects_a_non_positive_price(self, db_conn):
        upsert_product(_stock_position("stock:0001", "Acme Corp"))
        product_id = _only_product(db_conn, "fintual")["id"]

        with pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(
                "INSERT INTO product_prices (product_id, price_date, price, currency) "
                "VALUES (%s, '2026-01-02', 0, 'USD')",
                (product_id,),
            )


class TestBancoFalabellaProducts:
    """Banco Falabella's checking account and línea join a CMR card the writer
    stored back when the card was the institution's only product: all three
    are singletons of distinct kinds, so the card keeps its row and its
    transactions, and each leg's rows land on their own kind's singleton.
    """

    def _card(self):
        return ScrapedProduct(
            institution="banco_falabella",
            kind="credit_card",
            name="CMR Mastercard",
            attributes=CreditCardAttributes(last4="1234", brand="Mastercard"),
            metrics=CreditCardMetrics(available=999999, limit=2500000, owed=1000000),
        )

    def _new_products(self, rate=1.23):
        return [
            ScrapedProduct(
                institution="banco_falabella",
                kind="checking",
                name="Cuenta Corriente",
                attributes=CheckingAttributes(account_number="00-000-00000-01"),
                metrics=CheckingMetrics(balance=999999, accounting_balance=1000000),
            ),
            ScrapedProduct(
                institution="banco_falabella",
                kind="line_of_credit",
                name="Línea de Crédito",
                attributes=LineOfCreditAttributes(daily_interest_rate_pct=rate),
                metrics=LineOfCreditMetrics(available=999999, limit=2500000, owed=1000000),
            ),
        ]

    def _by_kind(self, conn):
        return {row["kind"]: row for row in _products(conn, "banco_falabella")}

    def test_the_card_keeps_its_row_and_transactions(self, db_conn):
        upsert_product(self._card())
        write_transactions([_txn("bfal_0000000000000001", "banco_falabella", "credit_card")])
        card_id = _only_product(db_conn, "banco_falabella")["id"]

        upsert_product(self._card())
        for product in self._new_products():
            upsert_product(product)
        result = write_transactions(
            [
                _txn("bfal_0000000000000002", "banco_falabella", "credit_card"),
                _txn("bfal_cc_0000000000000001", "banco_falabella", "checking", amount=1000000),
            ]
        )

        products = self._by_kind(db_conn)
        assert sorted(products) == ["checking", "credit_card", "line_of_credit"]
        assert all(row["external_ref"] is None for row in products.values())
        assert products["credit_card"]["id"] == card_id
        assert (result.inserted, result.unresolved) == (2, 0)
        rows = db_conn.execute(
            "SELECT external_id, product_id FROM transactions ORDER BY external_id"
        ).fetchall()
        assert [(r["external_id"], r["product_id"]) for r in rows] == [
            ("bfal_0000000000000001", card_id),
            ("bfal_0000000000000002", card_id),
            ("bfal_cc_0000000000000001", products["checking"]["id"]),
        ]

    def test_a_line_read_without_its_rate_keeps_the_stored_rate(self, db_conn):
        for product in self._new_products():
            upsert_product(product)
        for product in self._new_products(rate=None):
            upsert_product(product)

        line = self._by_kind(db_conn)["line_of_credit"]
        assert line["attributes"]["daily_interest_rate_pct"] == 1.23
