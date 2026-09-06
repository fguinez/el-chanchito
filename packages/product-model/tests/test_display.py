"""Tests for display families and the per-family column resolver."""

from typing import get_args

import pytest

from product_model import (
    FAMILIES,
    PRODUCT_FAMILIES,
    PRODUCT_KINDS,
    REGISTRY,
    ColumnFormat,
    ColumnSource,
    ColumnSpec,
    DisplaySpecError,
    FamilySpec,
    ProductFamily,
    ProductKind,
    family_kinds,
    resolve_columns,
    validate_display_specs,
)
from product_model.display import _resolve_family

EXPECTED_KINDS: dict[str, tuple[str, ...]] = {
    "cash": ("checking", "savings", "vista", "wallet", "prepaid_card"),
    "term_deposit": ("term_deposit",),
    "revolving_credit": ("credit_card", "line_of_credit"),
    "installment_loan": ("loan", "mortgage"),
    "investment": ("investment",),
    "crypto": ("crypto",),
    "other": ("debit_card", "other"),
}


def _spec(*columns: ColumnSpec, family: str = "cash") -> FamilySpec:
    return FamilySpec(family=family, label_es="Prueba", columns=columns)


# Each case pairs a throwaway spec with the fragment of the error it must raise,
# so a spec routed into the wrong branch cannot pass on another branch's error.
INVALID_SPECS: dict[str, tuple[FamilySpec, str]] = {
    "unknown metric field": (
        _spec(
            ColumnSpec(key="x", label_es="X", source=ColumnSource.METRIC, field="nope")
        ),
        "no kind of the family has a metrics.nope field",
    ),
    "metric field without denomination": (
        _spec(
            ColumnSpec(
                key="x",
                label_es="X",
                source=ColumnSource.METRIC,
                field="reported_as_of",
            )
        ),
        "lacks a valid denomination marker",
    ),
    "metric format disagrees with denomination": (
        _spec(
            ColumnSpec(
                key="x",
                label_es="X",
                source=ColumnSource.METRIC,
                field="balance",
                format=ColumnFormat.PERCENT,
            )
        ),
        "disagrees with the 'currency' denomination",
    ),
    "attribute without format": (
        _spec(
            ColumnSpec(
                key="x",
                label_es="X",
                source=ColumnSource.ATTRIBUTE,
                field="account_number",
            )
        ),
        "attribute columns need an explicit format",
    ),
    "attribute format mismatches field type": (
        _spec(
            ColumnSpec(
                key="x",
                label_es="X",
                source=ColumnSource.ATTRIBUTE,
                field="maturity_date",
                format=ColumnFormat.CURRENCY,
            ),
            family="term_deposit",
        ),
        "does not fit the 'date' attribute",
    ),
    "installments outside loans": (
        _spec(ColumnSpec(key="x", label_es="X", source=ColumnSource.INSTALLMENTS)),
        "has both metrics.installments_paid",
    ),
    "reserved key": (
        _spec(
            ColumnSpec(
                key="producto", label_es="Producto", source=ColumnSource.HEADLINE
            )
        ),
        "reserved for a universal column",
    ),
    "duplicate key": (
        _spec(
            ColumnSpec(key="saldo", label_es="Saldo", source=ColumnSource.HEADLINE),
            ColumnSpec(
                key="saldo",
                label_es="Saldo",
                source=ColumnSource.METRIC,
                field="balance",
            ),
        ),
        "duplicate column key",
    ),
    "field on headline": (
        _spec(
            ColumnSpec(
                key="saldo",
                label_es="Saldo",
                source=ColumnSource.HEADLINE,
                field="balance",
            )
        ),
        "headline columns take no field",
    ),
    "signed on a date column": (
        _spec(
            ColumnSpec(
                key="x",
                label_es="X",
                source=ColumnSource.ATTRIBUTE,
                field="maturity_date",
                format=ColumnFormat.DATE,
                signed=True,
            ),
            family="term_deposit",
        ),
        "signed applies only to currency, percent or count columns, not date",
    ),
}


def test_shipped_specs_validate():
    """The shipped families and columns are consistent with the registry."""
    validate_display_specs()


def test_literal_types_mirror_the_tuples():
    """ProductKind and ProductFamily restate PRODUCT_KINDS and PRODUCT_FAMILIES."""
    assert get_args(ProductKind) == PRODUCT_KINDS
    assert get_args(ProductFamily) == PRODUCT_FAMILIES


def test_families_iterate_in_display_order():
    """FAMILIES has exactly the PRODUCT_FAMILIES ids, in that order."""
    assert tuple(FAMILIES) == PRODUCT_FAMILIES


@pytest.mark.parametrize("kind", PRODUCT_KINDS)
def test_kind_belongs_to_exactly_one_family(kind):
    """Each kind's family is known and family_kinds lists the kind only there."""
    owners = [family for family in PRODUCT_FAMILIES if kind in family_kinds(family)]

    assert REGISTRY[kind].family in PRODUCT_FAMILIES
    assert owners == [REGISTRY[kind].family]


@pytest.mark.parametrize("family", PRODUCT_FAMILIES)
def test_family_kinds_match_the_design(family):
    """family_kinds partitions PRODUCT_KINDS as the design table says."""
    assert family_kinds(family) == EXPECTED_KINDS[family]


def test_revolving_credit_columns_are_right_aligned_currency():
    """Cards and lines show Disponible, Cupo and Utilizado as currency."""
    columns = resolve_columns("revolving_credit")
    keys = tuple(column.key for column in columns)

    assert keys == ("disponible", "cupo", "utilizado")
    assert {column.format for column in columns} == {ColumnFormat.CURRENCY}
    assert {column.align for column in columns} == {"right"}


def test_term_deposit_maturity_is_a_left_aligned_date():
    """An attribute date column keeps its declared format and aligns left."""
    columns = {column.key: column for column in resolve_columns("term_deposit")}

    assert columns["vencimiento"].source is ColumnSource.ATTRIBUTE
    assert columns["vencimiento"].format is ColumnFormat.DATE
    assert columns["vencimiento"].align == "left"


def test_investment_columns_derive_percent_and_keep_signed():
    """Metric formats come from denominations; the signed flag passes through."""
    columns = {column.key: column for column in resolve_columns("investment")}

    assert columns["ganancia"].signed is True
    assert columns["ganancia"].format is ColumnFormat.CURRENCY
    assert columns["var_30d"].signed is True
    assert columns["var_30d"].format is ColumnFormat.PERCENT
    assert columns["aportado"].signed is False


def test_installments_resolve_to_count():
    """The derived installments column is a count with no backing field."""
    columns = {column.key: column for column in resolve_columns("installment_loan")}

    assert columns["cuotas"].source is ColumnSource.INSTALLMENTS
    assert columns["cuotas"].field is None
    assert columns["cuotas"].format is ColumnFormat.COUNT
    assert columns["cuotas"].align == "right"


@pytest.mark.parametrize("family", PRODUCT_FAMILIES)
def test_alignment_follows_format(family):
    """Numeric formats align right; date and text align left."""
    columns = resolve_columns(family)

    for column in columns:
        is_text = column.format in (ColumnFormat.DATE, ColumnFormat.TEXT)
        assert column.align == ("left" if is_text else "right")


def test_resolve_columns_rejects_unknown_family():
    """A family id outside PRODUCT_FAMILIES fails loudly."""
    with pytest.raises(DisplaySpecError, match="unknown family"):
        resolve_columns("stocks")


@pytest.mark.parametrize(
    ("spec", "message"), INVALID_SPECS.values(), ids=INVALID_SPECS.keys()
)
def test_resolver_rejects_inconsistent_spec(spec, message):
    """Inconsistent specs raise DisplaySpecError naming the actual problem."""
    with pytest.raises(DisplaySpecError, match=message):
        _resolve_family(spec)
