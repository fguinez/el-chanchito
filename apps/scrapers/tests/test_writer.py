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
"""

from decimal import Decimal

import pytest

from db import writer
from db.writer import (
    _canonical_metrics,
    _headline_decimal,
    _write_decision,
    upsert_product,
)
from product_model import (
    InvestmentMetrics,
    ScrapedProduct,
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
