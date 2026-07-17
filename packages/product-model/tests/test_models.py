"""Tests for the per-kind payload models, unions, and ingest envelopes."""

from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from product_model import (
    PRODUCT_KINDS,
    REGISTRY,
    CreditCardAttributes,
    ProductMetrics,
    ScrapedProduct,
    ScrapedTransaction,
    TermDepositAttributes,
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
