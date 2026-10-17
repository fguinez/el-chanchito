"""Tests for the per-kind payload models, unions, and ingest envelopes."""

import re
from datetime import date
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from product_model import (
    PRODUCT_FAMILIES,
    PRODUCT_KINDS,
    REGISTRY,
    CreditCardAttributes,
    ProductMetrics,
    ScrapedProduct,
    ScrapedTransaction,
    TermDepositAttributes,
    TransactionLocation,
)

METRICS_ADAPTER = TypeAdapter(ProductMetrics)

VALID_ATTRIBUTES: dict[str, dict] = {
    "checking": {"account_number": "00-123-45678-90"},
    "savings": {"account_number": "12345678"},
    "vista": {},
    "wallet": {},
    "term_deposit": {
        "issue_date": "2026-01-15",
        "maturity_date": "2026-07-15",
        "term_days": 181,
        "interest_rate_pct": 0.45,
        "deposit_type": "Renovable",
        "principal": 1_000_000,
        "maturity_value": 1_027_000,
    },
    "credit_card": {
        "last4": "0000",
        "brand": "Visa Signature",
        "statement_day": 23,
        "due_day": 5,
    },
    "debit_card": {"last4": "1111", "brand": "Visa"},
    "prepaid_card": {"last4": "2222"},
    "line_of_credit": {"interest_rate_pct": 26.5},
    "loan": {
        "original_principal": 5_000_000,
        "interest_rate_pct": 18.9,
        "installments_total": 24,
        "opened_date": "2025-03-01",
        "due_day": 5,
    },
    "mortgage": {"original_principal": 80_000_000, "installments_total": 300},
    "investment": {"portfolio": "Risky Norris", "risk_profile": "Arriesgado"},
    "crypto": {},
    "other": {"note": "Caja fuerte"},
}

VALID_METRICS: dict[str, dict] = {
    "checking": {"balance": 1_234_567, "accounting_balance": 1_240_000},
    "savings": {"balance": 4_141_851},
    "vista": {"balance": 50_000},
    "wallet": {"balance": 3_226_170},
    "term_deposit": {"balance": 1_012_345},
    "credit_card": {"available": 3_600_000, "limit": 4_000_000, "owed": 400_000},
    "debit_card": {},
    "prepaid_card": {"balance": 20_000},
    "line_of_credit": {"available": 1_950_000, "limit": 2_000_000, "owed": 50_000},
    "loan": {"owed": 1_400_000, "installments_paid": 6},
    "mortgage": {"owed": 78_000_000, "next_payment_amount": 450_000},
    "investment": {"nav": 15_705_644.0, "deposited": 14_000_000.0, "profit": 1_705_644.0},
    "crypto": {"units": 0.0421, "frozen": 0.001},
    "other": {"balance": 10_000},
}

EXPECTED_HEADLINE: dict[str, float | None] = {
    "checking": 1_234_567,
    "savings": 4_141_851,
    "vista": 50_000,
    "wallet": 3_226_170,
    "term_deposit": 1_012_345,
    "credit_card": 3_600_000,
    "debit_card": None,
    "prepaid_card": 20_000,
    "line_of_credit": 1_950_000,
    "loan": 1_400_000,
    "mortgage": 78_000_000,
    "investment": 15_705_644.0,
    "crypto": 0.0421,
    "other": 10_000,
}


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_registry_covers_kind(kind):
    """Every kind has a spec whose payload classes carry its discriminator."""
    spec = REGISTRY[kind]

    assert spec.kind == kind
    assert spec.label_es
    assert spec.family in PRODUCT_FAMILIES
    assert spec.attributes_cls.model_fields["kind"].default == kind
    assert spec.metrics_cls.model_fields["kind"].default == kind


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_attributes_valid_payload_accepted(kind):
    """Each kind's attributes class accepts its documented payload."""
    payload = {"kind": kind, **VALID_ATTRIBUTES[kind]}

    attributes = REGISTRY[kind].attributes_cls.model_validate(payload)

    assert attributes.kind == kind


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_metrics_valid_payload_accepted(kind):
    """Each kind's metrics class accepts its documented payload."""
    payload = {"kind": kind, **VALID_METRICS[kind]}

    metrics = REGISTRY[kind].metrics_cls.model_validate(payload)

    assert metrics.kind == kind


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_attributes_extra_field_rejected(kind):
    """extra="forbid" rejects fields outside the kind's attribute set."""
    payload = {"kind": kind, **VALID_ATTRIBUTES[kind], "unexpected": 1}

    with pytest.raises(ValidationError):
        REGISTRY[kind].attributes_cls.model_validate(payload)


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_metrics_extra_field_rejected(kind):
    """extra="forbid" rejects fields outside the kind's metric set."""
    payload = {"kind": kind, **VALID_METRICS[kind], "unexpected": 1}

    with pytest.raises(ValidationError):
        REGISTRY[kind].metrics_cls.model_validate(payload)


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_union_rejects_wrong_kind_discriminator(kind):
    """A payload routed to the wrong family fails the discriminated union."""
    wrong_kind = "crypto" if kind == "debit_card" else "debit_card"
    payload = {"kind": wrong_kind, **VALID_METRICS[kind]}

    with pytest.raises(ValidationError):
        METRICS_ADAPTER.validate_python(payload)


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_headline_promotes_the_right_field(kind):
    """headline() returns the field the writer stores as current_balance."""
    metrics = METRICS_ADAPTER.validate_python({"kind": kind, **VALID_METRICS[kind]})

    assert metrics.headline() == EXPECTED_HEADLINE[kind]


def test_date_round_trips_through_json_dump():
    """Dates survive model_dump(mode="json") + model_validate unchanged."""
    original = TermDepositAttributes(
        issue_date=date(2026, 1, 15), maturity_date=date(2026, 7, 15)
    )

    dumped = original.model_dump(mode="json")
    restored = TermDepositAttributes.model_validate(dumped)

    assert dumped["maturity_date"] == "2026-07-15"
    assert restored == original


def test_scraped_product_envelope_discriminates_payloads():
    """The ingest envelope routes raw attribute/metric dicts by kind."""
    product = ScrapedProduct.model_validate(
        {
            "institution": "banchile",
            "kind": "credit_card",
            "attributes": {"kind": "credit_card", "last4": "0000"},
            "metrics": {"kind": "credit_card", "available": 3_600_000, "limit": 4_000_000},
        }
    )

    assert isinstance(product.attributes, CreditCardAttributes)
    assert product.metrics.headline() == 3_600_000
    assert product.currency == "CLP"
    assert product.external_ref is None


def test_scraped_product_rejects_mismatched_attributes_kind():
    """An attributes payload of a different kind fails the envelope."""
    with pytest.raises(
        ValidationError,
        match=r"attributes\.kind 'crypto' does not match product kind 'checking'",
    ):
        ScrapedProduct.model_validate(
            {
                "institution": "banchile",
                "kind": "checking",
                "attributes": {"kind": "crypto"},
            }
        )


def test_scraped_product_rejects_mismatched_metrics_kind():
    """A metrics payload of a different kind fails the envelope."""
    with pytest.raises(
        ValidationError,
        match=r"metrics\.kind 'wallet' does not match product kind 'checking'",
    ):
        ScrapedProduct.model_validate(
            {
                "institution": "banchile",
                "kind": "checking",
                "metrics": {"kind": "wallet", "balance": 1_000},
            }
        )


def test_scraped_product_accepts_matching_payload_kinds():
    """Payloads whose discriminators agree with the envelope kind validate."""
    product = ScrapedProduct.model_validate(
        {
            "institution": "banchile",
            "kind": "checking",
            "attributes": {"kind": "checking", "account_number": "00-123-45678-90"},
            "metrics": {"kind": "checking", "balance": 1_000},
        }
    )

    assert product.attributes.kind == "checking"
    assert product.metrics.kind == "checking"


def test_scraped_transaction_mirrors_the_scraper_dataclass():
    """Field names and defaults match the base.py dataclass for drop-in use."""
    txn = ScrapedTransaction(
        institution="mach",
        product_kind="wallet",
        description="Café",
        amount=-4_500,
        transaction_date=date(2026, 7, 15),
        external_id="mach-1",
    )

    assert txn.currency == "CLP"
    assert txn.category_hint is None
    assert txn.scheduled_month is None
    # Most sources report a single date; a NULL posting date is unremarkable.
    assert txn.accounting_date is None


def test_transaction_keeps_a_separate_posting_date():
    """A source that reports both dates records both (BanChile, V019).

    `transaction_date` is when the movement occurred; `accounting_date` is when
    the institution posted it. Neither is ever part of a dedup key.
    """
    txn = ScrapedTransaction(
        institution="banchile",
        product_kind="checking",
        description="TRANSFERENCIA DE TERCERO",
        amount=1_000_000,
        transaction_date=date(2026, 8, 21),
        accounting_date=date(2026, 8, 24),
        external_id="bch_op_12345678901",
    )

    assert txn.transaction_date == date(2026, 8, 21)
    assert txn.accounting_date == date(2026, 8, 24)


def _usd_txn(amount, currency="USD") -> ScrapedTransaction:
    """A synthetic card movement; only the amount (and currency) varies."""
    return ScrapedTransaction(
        institution="banchile",
        product_kind="credit_card",
        description="COMPRA SINTETICA USD",
        amount=amount,
        transaction_date=date(2026, 8, 21),
        external_id="bch_ref_00000000000",
        currency=currency,
    )


class TestTransactionAmount:
    """`amount` is a Decimal that fits NUMERIC(18, 2) (issue #79)."""

    def test_an_integer_validates_to_the_exact_decimal(self):
        """CLP producers keep sending ints; nothing about them changes."""
        amount = _usd_txn(-999_999, currency="CLP").amount

        assert isinstance(amount, Decimal)
        assert amount == Decimal("-999999") == -999_999
        assert str(amount) == "-999999"
        assert hash(amount) == hash(-999_999)

    @pytest.mark.parametrize("raw", ["-1234.56", -1234.56, Decimal("-1234.56")])
    def test_cents_are_kept(self, raw):
        """USD 1.234,56 keeps its cents whether it arrives as str, float or Decimal."""
        assert _usd_txn(raw).amount == Decimal("-1234.56")

    @pytest.mark.parametrize(
        "raw",
        [
            "1234.567",  # a third decimal does not fit the column
            0.1 + 0.2,  # float noise is rejected, never silently rounded
            10**16,  # 17 integer digits
            "NaN",
            float("inf"),
            True,
        ],
        ids=["three-decimals", "float-noise", "too-large", "nan", "inf", "bool"],
    )
    def test_amounts_the_column_cannot_hold_are_rejected(self, raw):
        with pytest.raises(ValidationError):
            _usd_txn(raw)

    def test_the_json_schema_describes_both_wire_forms(self):
        prop = ScrapedTransaction.model_json_schema()["properties"]["amount"]

        assert {"type": "number"} in prop["anyOf"]
        (string_form,) = [m for m in prop["anyOf"] if m.get("type") == "string"]
        assert string_form["pattern"] == r"^-?\d{1,16}(\.\d{1,2})?$"

    @pytest.mark.parametrize("raw", ["-12.5", -12.5, Decimal("-0.5")])
    def test_a_clp_fraction_is_rejected_not_rounded(self, raw):
        with pytest.raises(ValidationError, match="must be whole"):
            _usd_txn(raw, currency="CLP")

    @pytest.mark.parametrize(
        "raw, canonical",
        [
            (-999_999, "-999999"),
            (Decimal("-999999.00"), "-999999"),
            (Decimal("1E+6"), "1000000"),
        ],
    )
    def test_clp_is_canonical_whole_units(self, raw, canonical):
        assert str(_usd_txn(raw, currency="CLP").amount) == canonical

    @pytest.mark.parametrize(
        "raw, canonical",
        [
            (-12, "-12.00"),
            ("-1234.5", "-1234.50"),
            (-1234.56, "-1234.56"),
            (Decimal("1.2E+3"), "1200.00"),
            (Decimal("-0.1E-1"), "-0.01"),
        ],
    )
    def test_other_currencies_are_canonical_cents(self, raw, canonical):
        amount = _usd_txn(raw).amount

        assert str(amount) == canonical
        assert re.fullmatch(r"^-?\d{1,16}(\.\d{1,2})?$", str(amount))

    @pytest.mark.parametrize("raw", ["1E+3", "+12.30", " 12.30", "12.", ".5", "1,234.56"])
    def test_a_string_off_the_wire_pattern_is_rejected(self, raw):
        with pytest.raises(ValidationError):
            _usd_txn(raw)


def _located_txn(location) -> ScrapedTransaction:
    """A synthetic movement carrying `location` (a dict, a model or None)."""
    return ScrapedTransaction(
        institution="banchile",
        product_kind="credit_card",
        description="COMPRA SINTETICA",
        amount=-999_999,
        transaction_date=date(2026, 8, 21),
        external_id="bch_ref_00000000000",
        location=location,
    )


class TestTransactionLocation:
    """`location` is optional, every field independent (V025)."""

    def test_location_defaults_to_none(self):
        assert _located_txn(None).location is None

    def test_fields_are_trimmed_and_blank_means_absent(self):
        location = _located_txn(
            {"name": "  Tienda Ficticia ", "city": " CIUDAD FICTICIA", "country": "   "}
        ).location

        assert location == TransactionLocation(
            name="Tienda Ficticia", city="CIUDAD FICTICIA"
        )
        assert location.country is None

    def test_text_is_not_recased(self):
        """Stored as the source spells it, like descriptions."""
        assert _located_txn({"city": "CIUDAD FICTICIA"}).location.city == "CIUDAD FICTICIA"

    def test_an_all_blank_location_collapses_to_none(self):
        assert _located_txn({"city": "", "country": " "}).location is None

    def test_coordinates_round_to_the_stored_scale(self):
        location = _located_txn({"latitude": -33.0000004, "longitude": -70.0}).location

        assert location.latitude == -33.0
        assert location.longitude == -70.0

    @pytest.mark.parametrize(
        "coordinates",
        [
            {"latitude": -33.0},
            {"longitude": -70.0},
            {"latitude": 90.5, "longitude": 0},
            {"latitude": 0, "longitude": -180.5},
        ],
    )
    def test_bad_coordinates_are_rejected(self, coordinates):
        with pytest.raises(ValidationError):
            _located_txn(coordinates)

    def test_overlong_text_and_unknown_keys_are_rejected(self):
        with pytest.raises(ValidationError):
            _located_txn({"city": "X" * 201})
        with pytest.raises(ValidationError):
            _located_txn({"street": "Calle Ficticia 123"})
