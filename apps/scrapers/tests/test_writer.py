"""Tests for the DB writer's pure helpers (no database).

`_canonical_metrics` drives snapshot change detection: a history row is
appended only when the canonical form of the scraped metrics differs from the
stored one. `_headline_decimal` converts a metrics headline into the Decimal
persisted in the NUMERIC current_balance/balance columns.
"""

from decimal import Decimal

import pytest

from db.writer import _canonical_metrics, _headline_decimal


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
