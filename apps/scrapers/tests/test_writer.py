"""Tests for the DB writer's pure helpers plus its double-counting guards.

`_canonical_metrics` drives snapshot change detection: a history row is
appended only when the canonical form of the scraped metrics differs from the
stored one. `_headline_decimal` converts a metrics headline into the Decimal
persisted in the NUMERIC current_balance/balance columns. `_write_decision`
and the sibling helpers keep a summed roll-up and its per-holding rows from
both counting toward net worth; the fake connection below exercises the SQL
side-effects `upsert_product` issues for them. `_FakeResolveConn` drives the
real `_resolve_product` to check a new product's INSERT carries an
institution-unique slug (the slug helpers themselves live in test_slug.py).
`_claim_decision` and `_FakeTxConn` cover issue #57's adoption path: a stored
BanChile row is re-keyed in place when its movement comes back under the bank's
operation id, instead of being imported a second time.
"""

from datetime import date
from decimal import Decimal

import pytest

from db import writer
from db.writer import (
    _adopts_stored_rows,
    _canonical_metrics,
    _claim_decision,
    _headline_decimal,
    _is_final_id,
    _write_decision,
    upsert_product,
    upsert_transactions,
)
from product_model import (
    InvestmentMetrics,
    ScrapedProduct,
    ScrapedTransaction,
    TermDepositMetrics,
)


class TestCanonicalMetrics:
    def test_key_order_does_not_matter(self):
        """Postgres jsonb reorders keys; the canonical form must not care."""
        a = {"kind": "credit_card", "available": 3600000, "limit": 4000000}
        b = {"limit": 4000000, "available": 3600000, "kind": "credit_card"}

        assert _canonical_metrics(a) == _canonical_metrics(b)

    def test_none_differs_from_empty_dict(self):
        """No stored metrics (None) is not the same observation as `{}`."""
        assert _canonical_metrics(None) != _canonical_metrics({})

    def test_none_equals_none(self):
        assert _canonical_metrics(None) == _canonical_metrics(None)

    def test_nested_values_compared(self):
        assert _canonical_metrics({"a": {"x": 1}}) == _canonical_metrics(
            {"a": {"x": 1}}
        )
        assert _canonical_metrics({"a": {"x": 1}}) != _canonical_metrics(
            {"a": {"x": 2}}
        )

    def test_int_and_float_of_equal_value_are_unchanged(self):
        """A jsonb integer literal (e.g. the V011 seed's `"limit": 4000000`)
        loads as int while a pydantic dump of the same quantity is a float —
        the numeric type alone must not read as a change."""
        seeded = {"kind": "credit_card", "available": 3600000, "limit": 4000000}
        scraped = {
            "kind": "credit_card",
            "available": 3600000.0,
            "limit": 4000000.0,
        }

        assert _canonical_metrics(scraped) == _canonical_metrics(seeded)

    def test_int_vs_float_of_different_value_still_changed(self):
        assert _canonical_metrics(
            {"kind": "credit_card", "available": 3600000.0}
        ) != _canonical_metrics({"kind": "credit_card", "available": 3500000})

    @pytest.mark.parametrize(
        ("new", "current", "changed"),
        [
            # First observation ever recorded.
            ({"kind": "crypto", "units": 0.5}, None, True),
            # Re-confirmation of the same payload.
            ({"kind": "crypto", "units": 0.5}, {"kind": "crypto", "units": 0.5}, False),
            # Same payload, different stored key order.
            ({"kind": "crypto", "units": 0.5}, {"units": 0.5, "kind": "crypto"}, False),
            # A value moved.
            ({"kind": "crypto", "units": 0.6}, {"kind": "crypto", "units": 0.5}, True),
            # A field appeared (e.g. the límite parsed this time).
            (
                {"kind": "credit_card", "available": 100, "limit": 200},
                {"kind": "credit_card", "available": 100},
                True,
            ),
            # A field disappeared (exclude_none dropped it).
            (
                {"kind": "credit_card", "available": 100},
                {"kind": "credit_card", "available": 100, "limit": 200},
                True,
            ),
        ],
    )
    def test_changed_detection(self, new, current, changed):
        """The writer snapshots iff the canonical forms differ."""
        assert (_canonical_metrics(new) != _canonical_metrics(current)) is changed


class TestHeadlineDecimal:
    def test_int_headline(self):
        assert _headline_decimal(12345678) == Decimal("12345678")

    def test_float_headline_keeps_printed_value(self):
        """str() conversion avoids the float's binary expansion (0.1 != 0.1000...)."""
        assert _headline_decimal(0.1) == Decimal("0.1")

    def test_fractional_crypto_units(self):
        assert _headline_decimal(0.0421) == Decimal("0.0421")

    def test_none_passes_through(self):
        """Kinds with no headline (e.g. debit_card) produce no balance."""
        assert _headline_decimal(None) is None


class TestWriteDecision:
    """The pure guard that keeps roll-up and per-holding rows exclusive."""

    def test_inactive_row_is_left_frozen(self):
        """A retired product is skipped whatever its ref/sibling state."""
        assert _write_decision(False, None, False) == "skip_inactive"
        assert _write_decision(False, "dep-1", True) == "skip_inactive"

    def test_rollup_yields_to_active_per_holding_siblings(self):
        """An active NULL-ref roll-up is dropped when per-holding rows exist."""
        assert _write_decision(True, None, True) == "skip_superseded"

    def test_rollup_writes_when_alone(self):
        """With no per-holding siblings the roll-up is the only representation."""
        assert _write_decision(True, None, False) == "write"

    def test_per_holding_always_writes(self):
        """A ref'd product writes regardless of any roll-up sibling."""
        assert _write_decision(True, "dep-1", True) == "write"
        assert _write_decision(True, "dep-1", False) == "write"


class _FakeCursor:
    def __init__(self, result=None, rowcount=0):
        self._result = result
        self.rowcount = rowcount

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []


class _FakeConn:
    """A products/snapshots stand-in that answers `upsert_product`'s queries.

    Just enough SQL routing (by distinctive fragment) to drive the guard,
    exclusivity and retirement branches; `_resolve_product` is monkeypatched to
    a fixed id so only the post-resolve queries reach here. `products` maps id ->
    a mutable row dict; `snapshots` is a flat list of ``{"product_id": ...}``.
    """

    def __init__(self, products, snapshots=None):
        self.products = products
        self.snapshots = snapshots if snapshots is not None else []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        q = " ".join(sql.split())
        p = params or ()
        if "SELECT account_id, kind, currency, external_ref, is_active" in q:
            r = self.products[p[0]]
            return _FakeCursor(
                (r["account_id"], r["kind"], r["currency"], r["external_ref"], r["is_active"])
            )
        if "SELECT 1 FROM products" in q and "external_ref IS NOT NULL" in q:
            account_id, kind, currency = p
            hit = any(
                r["account_id"] == account_id
                and r["kind"] == kind
                and r["currency"] == currency
                and r["external_ref"] is not None
                and r["is_active"]
                for r in self.products.values()
            )
            return _FakeCursor((1,) if hit else None)
        if "SELECT id FROM products" in q and "external_ref IS NULL" in q:
            account_id, kind, currency = p
            for pid, r in self.products.items():
                if (
                    r["account_id"] == account_id
                    and r["kind"] == kind
                    and r["currency"] == currency
                    and r["external_ref"] is None
                    and r["is_active"]
                ):
                    return _FakeCursor((pid,))
            return _FakeCursor(None)
        if "DELETE FROM product_snapshots" in q:
            pid = p[0]
            kept = [s for s in self.snapshots if s["product_id"] != pid]
            removed = len(self.snapshots) - len(kept)
            self.snapshots = kept
            return _FakeCursor(rowcount=removed)
        if "UPDATE products SET is_active = false" in q:
            r = self.products[p[0]]
            r.update(is_active=False, current_balance=None, metrics=None, balance_as_of=None)
            return _FakeCursor()
        if "SELECT metrics FROM products" in q:
            return _FakeCursor((self.products[p[0]]["metrics"],))
        if "INSERT INTO product_snapshots" in q:
            self.snapshots.append({"product_id": p[1], "balance": p[2]})
            return _FakeCursor(rowcount=1)
        if "UPDATE products SET current_balance" in q:
            self.products[p[-1]].update(current_balance=p[0])
            return _FakeCursor()
        return _FakeCursor()


class _FakeResolveConn:
    """Answers `_resolve_product`'s own query chain, unlike `_FakeConn` which
    only sees the post-resolve writes.

    Same routing-by-SQL-fragment idea: institution, user and account lookups
    all hit, `taken_slugs` is what the institution already holds, and the
    product INSERT echoes back its id param. Assertions read `executed`.
    """

    def __init__(self, taken_slugs):
        self.taken_slugs = taken_slugs
        self.executed = []

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        self.executed.append((q, params))
        if "SELECT id, name FROM institutions" in q:
            return _FakeCursor(("inst-1", "Banco Sintético"))
        if "SELECT id FROM users" in q:
            return _FakeCursor(("user-1",))
        if "SELECT id FROM accounts" in q:
            return _FakeCursor(("acc-1",))
        if "SELECT p.slug FROM products p" in q:
            return _FakeCursor([(s,) for s in self.taken_slugs])
        if "INSERT INTO products" in q:
            return _FakeCursor((params[0],))
        return _FakeCursor()


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def _use_conn(monkeypatch, conn, product_id):
    monkeypatch.setattr(writer, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(
        writer,
        "_resolve_product",
        lambda conn, institution, kind, currency="CLP", external_ref=None, name=None: product_id,
    )


def _executed(conn, *needles):
    return [q for q, _ in conn.executed if all(n in q for n in needles)]


class TestUpsertProductExclusivity:
    """`upsert_product`'s roll-up vs per-holding guards, via the fake connection."""

    def test_inactive_product_is_left_frozen(self, monkeypatch):
        """A retired product takes no metrics, no snapshot, no balance change."""
        conn = _FakeConn(
            {
                "rollup": {
                    "account_id": "acc",
                    "kind": "term_deposit",
                    "currency": "CLP",
                    "external_ref": None,
                    "is_active": False,
                    "metrics": None,
                    "current_balance": None,
                }
            }
        )
        _use_conn(monkeypatch, conn, "rollup")

        upsert_product(
            ScrapedProduct(
                institution="banchile",
                kind="term_deposit",
                metrics=TermDepositMetrics(balance=2500000),
            )
        )

        assert conn.snapshots == []
        assert conn.products["rollup"]["current_balance"] is None
        assert _executed(conn, "INSERT INTO product_snapshots") == []
        assert _executed(conn, "UPDATE products SET current_balance") == []

    def test_rollup_skipped_when_per_holding_siblings_active(self, monkeypatch):
        """A NULL-ref roll-up write is dropped while per-holding rows are active."""
        conn = _FakeConn(
            {
                "rollup": {
                    "account_id": "acc",
                    "kind": "investment",
                    "currency": "CLP",
                    "external_ref": None,
                    "is_active": True,
                    "metrics": None,
                    "current_balance": None,
                },
                "fund-1": {
                    "account_id": "acc",
                    "kind": "investment",
                    "currency": "CLP",
                    "external_ref": "Fondo Sintético|A",
                    "is_active": True,
                    "metrics": None,
                    "current_balance": 450000,
                },
            }
        )
        _use_conn(monkeypatch, conn, "rollup")

        upsert_product(
            ScrapedProduct(
                institution="banchile",
                kind="investment",
                metrics=InvestmentMetrics(nav=1000000),
            )
        )

        assert conn.snapshots == []
        assert _executed(conn, "UPDATE products SET current_balance") == []

    def test_per_holding_write_retires_active_rollup(self, monkeypatch):
        """Writing a per-holding product deactivates and empties its roll-up."""
        conn = _FakeConn(
            products={
                "dep-1": {
                    "account_id": "acc",
                    "kind": "term_deposit",
                    "currency": "CLP",
                    "external_ref": "00000000000000001",
                    "is_active": True,
                    "metrics": None,
                    "current_balance": None,
                },
                "rollup": {
                    "account_id": "acc",
                    "kind": "term_deposit",
                    "currency": "CLP",
                    "external_ref": None,
                    "is_active": True,
                    "metrics": {"kind": "term_deposit", "balance": 3500000},
                    "current_balance": 3500000,
                },
            },
            snapshots=[{"product_id": "rollup"}, {"product_id": "rollup"}],
        )
        _use_conn(monkeypatch, conn, "dep-1")

        upsert_product(
            ScrapedProduct(
                institution="banchile",
                kind="term_deposit",
                currency="CLP",
                external_ref="00000000000000001",
                name="Depósito a Plazo 0001",
                metrics=TermDepositMetrics(balance=2500000),
            )
        )

        assert conn.products["rollup"]["is_active"] is False
        assert conn.products["rollup"]["current_balance"] is None
        assert all(s["product_id"] != "rollup" for s in conn.snapshots)
        assert any(s["product_id"] == "dep-1" for s in conn.snapshots)
        assert conn.products["dep-1"]["current_balance"] == Decimal("2500000")

    def test_per_holding_write_without_rollup_retires_nothing(self, monkeypatch):
        """With no roll-up sibling the per-holding write skips retirement."""
        conn = _FakeConn(
            {
                "dep-1": {
                    "account_id": "acc",
                    "kind": "term_deposit",
                    "currency": "CLP",
                    "external_ref": "00000000000000001",
                    "is_active": True,
                    "metrics": None,
                    "current_balance": None,
                }
            }
        )
        _use_conn(monkeypatch, conn, "dep-1")

        upsert_product(
            ScrapedProduct(
                institution="banchile",
                kind="term_deposit",
                currency="CLP",
                external_ref="00000000000000001",
                name="Depósito a Plazo 0001",
                metrics=TermDepositMetrics(balance=2500000),
            )
        )

        assert _executed(conn, "UPDATE products SET is_active = false") == []
        assert conn.products["dep-1"]["current_balance"] == Decimal("2500000")


class TestResolveProductSlug:
    """`_resolve_product` mints an institution-unique slug on its INSERT path."""

    def _product_insert(self, conn):
        return next(
            (q, p) for q, p in conn.executed if "INSERT INTO products" in q
        )

    def test_new_product_gets_suffixed_slug(self):
        """A name whose slug the institution already holds gets the -2 suffix,
        with columns and params in matching order (name then slug)."""
        conn = _FakeResolveConn(taken_slugs=["cuenta-corriente"])

        writer._resolve_product(
            conn, "banco-sintetico", "checking", name="Cuenta Corriente"
        )

        q, p = self._product_insert(conn)
        assert "(id, account_id, name, slug, kind, currency, external_ref)" in q
        assert p[2] == "Cuenta Corriente"
        assert p[3] == "cuenta-corriente-2"

    def test_first_product_keeps_bare_slug(self):
        """With no slugs taken the bare slugified name is used."""
        conn = _FakeResolveConn(taken_slugs=[])

        writer._resolve_product(
            conn, "banco-sintetico", "checking", name="Cuenta Corriente"
        )

        _, p = self._product_insert(conn)
        assert p[3] == "cuenta-corriente"


class TestClaimDecision:
    """The pure half of the issue #57 adoption path.

    `siblings` are (row_id, external_id) pairs, oldest `created_at` first: the
    stored rows of the same product, date, amount and scraper source that a
    movement whose key matches nothing could be under an older key.
    """

    def test_a_legacy_row_is_adopted(self):
        siblings = [("row-1", "bch_a1b2c3d4e5f60718")]
        assert _claim_decision("bch_op_12345678901", siblings, set(), set()) == (
            "rekey",
            "row-1",
        )

    def test_a_fingerprint_row_is_adopted_by_its_reference(self):
        """Issue #56: an unbilled charge picks up its numReferencia when billed."""
        siblings = [("row-1", "bch_fp_a1b2c3d4e5f60718")]
        assert _claim_decision("bch_ref_200812345678", siblings, set(), set()) == (
            "rekey",
            "row-1",
        )

    def test_claiming_is_oldest_first_and_one_to_one(self):
        """N identical movements must map onto the N stored rows, not one."""
        siblings = [("row-1", "bch_aaa"), ("row-2", "bch_bbb"), ("row-3", "bch_ccc")]
        claimed = set()
        picked = []
        for external_id in ("bch_op_1", "bch_op_2", "bch_op_3"):
            action, row_id = _claim_decision(external_id, siblings, claimed, set())
            assert action == "rekey"
            claimed.add(row_id)
            picked.append(row_id)

        assert picked == ["row-1", "row-2", "row-3"]

    def test_a_fourth_movement_inserts(self):
        siblings = [("row-1", "bch_aaa")]
        assert _claim_decision("bch_op_2", siblings, {"row-1"}, set()) == (
            "insert",
            None,
        )

    def test_a_row_holding_one_of_this_scrapes_keys_is_never_stolen(self):
        """It belongs to the movement carrying that key."""
        siblings = [("row-1", "bch_op_12345678901")]
        assert _claim_decision(
            "bch_op_12345678902", siblings, set(), {"bch_op_12345678901"}
        ) == ("insert", None)

    def test_two_bank_ids_on_the_same_day_are_distinct_movements(self):
        """A row already identified by the bank is never re-keyed onto another."""
        siblings = [("row-1", "bch_op_12345678901")]
        assert _claim_decision("bch_op_12345678902", siblings, set(), set()) == (
            "insert",
            None,
        )

    def test_a_lost_operation_id_keeps_the_stored_row(self):
        """The transient 503 case: don't throw the bank's id away, don't insert."""
        siblings = [("row-1", "bch_op_12345678901")]
        assert _claim_decision("bch_fp_a1b2c3d4e5f60718", siblings, set(), set()) == (
            "keep",
            "row-1",
        )

    def test_a_legacy_row_wins_over_a_final_one(self):
        """Adoption prefers the row that still needs re-keying."""
        siblings = [("row-1", "bch_op_12345678901"), ("row-2", "bch_aaa")]
        assert _claim_decision("bch_fp_deadbeefdeadbeef", siblings, set(), set()) == (
            "rekey",
            "row-2",
        )

    def test_no_siblings_inserts(self):
        assert _claim_decision("bch_op_1", [], set(), set()) == ("insert", None)


class TestAdoptsStoredRows:
    def test_only_banchile_keys_adopt(self):
        assert _adopts_stored_rows("bch_op_12345678901") is True
        assert _adopts_stored_rows("bch_a1b2c3d4e5f60718") is True
        assert _adopts_stored_rows("bcl_a1b2c3d4e5f60718") is False
        assert _adopts_stored_rows("buda_1234") is False
        assert _adopts_stored_rows(None) is False

    def test_a_legacy_hash_can_never_look_final(self):
        """`bch_` + md5 hex: "p", "r" and "_" are not hex digits."""
        assert _is_final_id("bch_0123456789abcdef") is False
        assert _is_final_id("bch_op_12345678901") is True
        assert _is_final_id("bch_ref_200812345678") is True
        assert _is_final_id("bch_fp_0123456789abcdef") is False


class _FakeTxConn:
    """A transactions table stand-in for `upsert_transactions`.

    Rows are dicts; `created_at` is the insertion order, which is what the real
    query orders by. `_resolve_product` is monkeypatched, so only the
    transaction queries reach here.
    """

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executed = []
        self._clock = 100

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        self.executed.append((q, params))
        p = params or ()
        if "SELECT id FROM transactions" in q:
            product_id, external_id = p
            for row in self.rows:
                if row["product_id"] == product_id and row["external_id"] == external_id:
                    return _FakeCursor((row["id"],))
            return _FakeCursor(None)
        if "SELECT id, external_id FROM transactions" in q:
            # (product_id, amount, source, LIKE pattern, occurrence date,
            #  accounting date, the date the ordering prefers)
            product_id, amount, source, like, occurred, accounted, preferred = p
            prefix = like.replace("\\", "").rstrip("%")
            hits = [
                row
                for row in self.rows
                if row["product_id"] == product_id
                and row["amount"] == amount
                and row["source"] == source
                and (row["external_id"] or "").startswith(prefix)
                and row["transaction_date"] in (occurred, accounted)
            ]
            hits.sort(
                key=lambda row: (
                    row["transaction_date"] != preferred,
                    row["created_at"],
                    row["id"],
                )
            )
            return _FakeCursor([(row["id"], row["external_id"]) for row in hits])
        if "UPDATE transactions SET external_id" in q:
            external_id, tx_date, accounting_date, month, row_id = p
            for row in self.rows:
                if row["id"] == row_id:
                    row.update(
                        external_id=external_id,
                        transaction_date=tx_date,
                        accounting_date=accounting_date,
                        scheduled_month=month,
                    )
            return _FakeCursor(rowcount=1)
        if "INSERT INTO transactions" in q:
            (
                row_id,
                product_id,
                description,
                amount,
                tx_date,
                accounting_date,
                month,
                source,
                ext,
            ) = p
            self._clock += 1
            self.rows.append(
                {
                    "id": row_id,
                    "product_id": product_id,
                    "external_id": ext,
                    "description": description,
                    "amount": amount,
                    "transaction_date": tx_date,
                    "accounting_date": accounting_date,
                    "scheduled_month": month,
                    "source": source,
                    "created_at": self._clock,
                }
            )
            return _FakeCursor(rowcount=1)
        return _FakeCursor()


def _legacy_row(
    row_id,
    external_id,
    created_at,
    amount=-999999,
    description="COMPRA",
    tx_date=date(2026, 8, 20),
):
    """A stored row as the pre-#57 scraper wrote it.

    fintself dated every BanChile row by the portal's `fechaContable` column,
    so a legacy row holds the POSTING date in `transaction_date` and has no
    `accounting_date` at all.
    """
    return {
        "id": row_id,
        "product_id": "prod-1",
        "external_id": external_id,
        "description": description,
        "amount": amount,
        "transaction_date": tx_date,
        "accounting_date": None,
        "scheduled_month": date(tx_date.year, tx_date.month, 1),
        "source": "scraper_banchile",
        "created_at": created_at,
    }


def _txn(
    external_id,
    amount=-999999,
    description="COMPRA",
    kind="checking",
    tx_date=date(2026, 8, 20),
    accounting_date=None,
):
    return ScrapedTransaction(
        institution="banchile",
        product_kind=kind,
        description=description,
        amount=amount,
        transaction_date=tx_date,
        accounting_date=accounting_date,
        external_id=external_id,
        scheduled_month=date(tx_date.year, tx_date.month, 1),
    )


def _use_tx_conn(monkeypatch, conn):
    monkeypatch.setattr(writer, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(
        writer,
        "_resolve_product",
        lambda conn, institution, kind, currency="CLP", external_ref=None, name=None: "prod-1",
    )


class TestUpsertTransactionsAdoption:
    """Issue #57: a re-keyed movement is rewritten in place, never duplicated."""

    def test_a_legacy_row_is_re_keyed_not_duplicated(self, monkeypatch):
        conn = _FakeTxConn([_legacy_row("row-1", "bch_a1b2c3d4e5f60718", 1)])
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions([_txn("bch_op_12345678901")])

        assert inserted == 0
        assert len(conn.rows) == 1
        assert conn.rows[0]["external_id"] == "bch_op_12345678901"

    def test_n_identical_movements_map_one_to_one(self, monkeypatch):
        """Three stored rows, three incoming ids, three rewrites and no inserts."""
        conn = _FakeTxConn(
            [
                _legacy_row("row-1", "bch_aaaaaaaaaaaaaaaa", 1),
                _legacy_row("row-2", "bch_bbbbbbbbbbbbbbbb", 2),
                _legacy_row("row-3", "bch_cccccccccccccccc", 3),
            ]
        )
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions(
            [_txn(f"bch_op_1234567890{n}") for n in range(3)]
        )

        assert inserted == 0
        assert sorted(row["external_id"] for row in conn.rows) == [
            "bch_op_12345678900",
            "bch_op_12345678901",
            "bch_op_12345678902",
        ]

    def test_a_fourth_movement_inserts(self, monkeypatch):
        conn = _FakeTxConn([_legacy_row("row-1", "bch_aaaaaaaaaaaaaaaa", 1)])
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions(
            [_txn("bch_op_12345678901"), _txn("bch_op_12345678902")]
        )

        assert inserted == 1
        assert len(conn.rows) == 2

    def test_running_twice_inserts_nothing_the_second_time(self, monkeypatch):
        conn = _FakeTxConn([_legacy_row("row-1", "bch_a1b2c3d4e5f60718", 1)])
        _use_tx_conn(monkeypatch, conn)
        batch = [_txn("bch_op_12345678901"), _txn("bch_op_12345678902")]

        first = upsert_transactions(batch)
        second = upsert_transactions(batch)

        assert (first, second) == (1, 0)
        assert len(conn.rows) == 2

    def test_a_billed_charge_adopts_its_unbilled_row(self, monkeypatch):
        """Issue #56: the two card legs share no identity field."""
        conn = _FakeTxConn([_legacy_row("row-1", "bch_fp_a1b2c3d4e5f60718", 1)])
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions(
            [_txn("bch_ref_200812345678", kind="credit_card")]
        )

        assert inserted == 0
        assert conn.rows[0]["external_id"] == "bch_ref_200812345678"

    def test_an_unrelated_row_is_never_claimed(self, monkeypatch):
        """A different amount, date or source is a different movement."""
        conn = _FakeTxConn(
            [
                _legacy_row("row-1", "bch_aaaaaaaaaaaaaaaa", 1, amount=-2500000),
                dict(_legacy_row("row-2", "manual-1", 2), source="manual"),
            ]
        )
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions([_txn("bch_op_12345678901")])

        assert inserted == 1
        assert conn.rows[0]["external_id"] == "bch_aaaaaaaaaaaaaaaa"
        assert conn.rows[1]["external_id"] == "manual-1"

    def test_a_description_change_alone_re_keys_nothing(self, monkeypatch):
        """The key is description-free, so the row matches directly."""
        conn = _FakeTxConn([_legacy_row("row-1", "bch_op_12345678901", 1)])
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions(
            [_txn("bch_op_12345678901", description="COMERCIO SINTETICO S.A.")]
        )

        assert (inserted, len(conn.rows)) == (0, 1)
        assert conn.executed[-1][0].startswith("SELECT id FROM transactions")

    def test_a_lost_operation_id_does_not_duplicate(self, monkeypatch):
        """A transient 503 falls back to a fingerprint key; keep the stored row."""
        conn = _FakeTxConn([_legacy_row("row-1", "bch_op_12345678901", 1)])
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions([_txn("bch_fp_a1b2c3d4e5f60718")])

        assert (inserted, len(conn.rows)) == (0, 1)
        assert conn.rows[0]["external_id"] == "bch_op_12345678901"

    def test_other_institutions_keep_the_plain_insert(self, monkeypatch):
        """Only BanChile re-keys; a bcl_ movement must never adopt a row."""
        conn = _FakeTxConn(
            [dict(_legacy_row("row-1", "bcl_a1b2c3d4e5f60718", 1), source="scraper_bci_lider")]
        )
        _use_tx_conn(monkeypatch, conn)
        txn = ScrapedTransaction(
            institution="bci_lider",
            product_kind="credit_card",
            description="COMPRA",
            amount=-999999,
            transaction_date=date(2026, 8, 20),
            external_id="bcl_ffffffffffffffff",
        )

        inserted = upsert_transactions([txn])

        assert inserted == 1
        assert len(conn.rows) == 2


class TestAdoptionAcrossTheDateShift:
    """Issue #57 moved BanChile's `transaction_date` to the occurrence date.

    Every stored row was written from the POSTING date (fintself read the
    portal's `fechaContable` column), so an incoming movement usually no longer
    matches its own row on the date. The lookup is tolerant of exactly that, and
    adoption corrects the row instead of leaving it on the wrong day.
    """

    def _incoming(self, external_id="bch_op_12345678901"):
        """A movement that happened on the 21st and posted on the 24th."""
        return _txn(
            external_id,
            tx_date=date(2026, 8, 21),
            accounting_date=date(2026, 8, 24),
        )

    def test_a_row_stored_under_its_posting_date_is_adopted(self, monkeypatch):
        conn = _FakeTxConn(
            [_legacy_row("row-1", "bch_a1b2c3d4e5f60718", 1, tx_date=date(2026, 8, 24))]
        )
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions([self._incoming()])

        assert (inserted, len(conn.rows)) == (0, 1)
        assert conn.rows[0]["external_id"] == "bch_op_12345678901"

    def test_adoption_corrects_both_dates_and_the_scheduled_month(self, monkeypatch):
        conn = _FakeTxConn(
            [_legacy_row("row-1", "bch_a1b2c3d4e5f60718", 1, tx_date=date(2026, 8, 24))]
        )
        _use_tx_conn(monkeypatch, conn)

        upsert_transactions([self._incoming()])

        row = conn.rows[0]
        assert row["transaction_date"] == date(2026, 8, 21)
        assert row["accounting_date"] == date(2026, 8, 24)
        assert row["scheduled_month"] == date(2026, 8, 1)

    def test_an_exact_occurrence_match_is_preferred(self, monkeypatch):
        """Two candidates: the one already on the occurrence date wins.

        Otherwise two movements a few days apart for the same amount could
        claim each other's rows.
        """
        conn = _FakeTxConn(
            [
                _legacy_row("row-posted", "bch_aaaaaaaaaaaaaaaa", 1, tx_date=date(2026, 8, 24)),
                _legacy_row("row-exact", "bch_bbbbbbbbbbbbbbbb", 2, tx_date=date(2026, 8, 21)),
            ]
        )
        _use_tx_conn(monkeypatch, conn)

        upsert_transactions([self._incoming()])

        by_id = {row["id"]: row for row in conn.rows}
        assert by_id["row-exact"]["external_id"] == "bch_op_12345678901"
        assert by_id["row-posted"]["external_id"] == "bch_aaaaaaaaaaaaaaaa"

    def test_a_row_on_neither_date_is_never_claimed(self, monkeypatch):
        """The tolerance is two dates wide, not a range."""
        conn = _FakeTxConn(
            [_legacy_row("row-1", "bch_aaaaaaaaaaaaaaaa", 1, tx_date=date(2026, 8, 19))]
        )
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions([self._incoming()])

        assert (inserted, len(conn.rows)) == (1, 2)
        assert conn.rows[0]["external_id"] == "bch_aaaaaaaaaaaaaaaa"

    def test_a_foreign_external_id_on_the_same_day_is_never_claimed(self, monkeypatch):
        """The query itself restricts candidates to the `bch_` namespace."""
        conn = _FakeTxConn(
            [
                dict(
                    _legacy_row("row-1", "csv_import_1", 1, tx_date=date(2026, 8, 24)),
                    source="scraper_banchile",
                )
            ]
        )
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions([self._incoming()])

        assert (inserted, len(conn.rows)) == (1, 2)
        assert conn.rows[0]["external_id"] == "csv_import_1"

    def test_a_new_row_carries_both_dates(self, monkeypatch):
        conn = _FakeTxConn([])
        _use_tx_conn(monkeypatch, conn)

        upsert_transactions([self._incoming()])

        assert conn.rows[0]["transaction_date"] == date(2026, 8, 21)
        assert conn.rows[0]["accounting_date"] == date(2026, 8, 24)

    def test_a_null_accounting_date_still_matches_on_the_one_date(self, monkeypatch):
        """The card legs report no posting date; the lookup must still work."""
        conn = _FakeTxConn(
            [_legacy_row("row-1", "bch_a1b2c3d4e5f60718", 1, tx_date=date(2026, 8, 20))]
        )
        _use_tx_conn(monkeypatch, conn)

        inserted = upsert_transactions(
            [_txn("bch_ref_200812345678", kind="credit_card")]
        )

        assert (inserted, len(conn.rows)) == (0, 1)
        assert conn.rows[0]["external_id"] == "bch_ref_200812345678"
        assert conn.rows[0]["accounting_date"] is None
