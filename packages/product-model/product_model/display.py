"""Display families and per-family column specs.

Kinds are grouped into display families; each family declares the columns of
its products table and the web renders one table per family from the
generated `FAMILY_INFO`. Column formats are derived from the registry's
pydantic classes (metric denominations, attribute types) so a spec can never
disagree with the payloads it reads.
"""

import functools
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from .kinds import PRODUCT_FAMILIES, PRODUCT_KINDS, ProductFamily
from .registry import REGISTRY


class DisplaySpecError(ValueError):
    """A family or column spec is inconsistent with the registry."""


class ColumnSource(StrEnum):
    """Where a column's value comes from."""

    HEADLINE = "headline"
    CLP_VALUE = "clp_value"
    METRIC = "metric"
    ATTRIBUTE = "attribute"
    INSTALLMENTS = "installments"


class ColumnFormat(StrEnum):
    """How a column's value is rendered."""

    CURRENCY = "currency"
    PERCENT = "percent"
    COUNT = "count"
    DATE = "date"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column as declared in a family spec.

    `format` is required for attribute columns and derived for every other
    source; `field` names the metrics/attributes field for those two sources.
    """

    key: str
    label_es: str
    source: ColumnSource
    field: str | None = None
    format: ColumnFormat | None = None
    optional: bool = False
    signed: bool = False


@dataclass(frozen=True, slots=True)
class FamilySpec:
    """A display family: its heading and the columns of its products table."""

    family: ProductFamily
    label_es: str
    columns: tuple[ColumnSpec, ...]


@dataclass(frozen=True, slots=True)
class ResolvedColumn:
    """A column with its format and alignment settled against the registry."""

    key: str
    label_es: str
    source: ColumnSource
    field: str | None
    format: ColumnFormat
    align: str
    optional: bool
    signed: bool


_FAMILY_SPECS: tuple[FamilySpec, ...] = (
    FamilySpec(
        family="cash",
        label_es="Cuentas y efectivo",
        columns=(
            ColumnSpec(key="saldo", label_es="Saldo", source=ColumnSource.HEADLINE),
            ColumnSpec(
                key="saldo_contable",
                label_es="Saldo contable",
                source=ColumnSource.METRIC,
                field="accounting_balance",
                optional=True,
            ),
        ),
    ),
    FamilySpec(
        family="term_deposit",
        label_es="Depósitos a plazo",
        columns=(
            ColumnSpec(key="valor", label_es="Valor", source=ColumnSource.HEADLINE),
            ColumnSpec(
                key="vencimiento",
                label_es="Vencimiento",
                source=ColumnSource.ATTRIBUTE,
                field="maturity_date",
                format=ColumnFormat.DATE,
                optional=True,
            ),
            ColumnSpec(
                key="tasa",
                label_es="Tasa",
                source=ColumnSource.ATTRIBUTE,
                field="interest_rate_pct",
                format=ColumnFormat.PERCENT,
                optional=True,
            ),
        ),
    ),
    FamilySpec(
        family="revolving_credit",
        label_es="Tarjetas y líneas de crédito",
        columns=(
            ColumnSpec(
                key="disponible", label_es="Disponible", source=ColumnSource.HEADLINE
            ),
            ColumnSpec(
                key="cupo", label_es="Cupo", source=ColumnSource.METRIC, field="limit"
            ),
            ColumnSpec(
                key="utilizado",
                label_es="Utilizado",
                source=ColumnSource.METRIC,
                field="owed",
            ),
        ),
    ),
    FamilySpec(
        family="installment_loan",
        label_es="Deuda en cuotas",
        columns=(
            ColumnSpec(key="deuda", label_es="Deuda", source=ColumnSource.HEADLINE),
            ColumnSpec(
                key="proxima_cuota",
                label_es="Próxima cuota",
                source=ColumnSource.METRIC,
                field="next_payment_amount",
                optional=True,
            ),
            ColumnSpec(
                key="cuotas",
                label_es="Cuotas",
                source=ColumnSource.INSTALLMENTS,
                optional=True,
            ),
            ColumnSpec(
                key="tasa",
                label_es="Tasa",
                source=ColumnSource.ATTRIBUTE,
                field="interest_rate_pct",
                format=ColumnFormat.PERCENT,
                optional=True,
            ),
        ),
    ),
    FamilySpec(
        family="investment",
        label_es="Inversiones",
        columns=(
            ColumnSpec(key="valor", label_es="Valor", source=ColumnSource.HEADLINE),
            ColumnSpec(
                key="aportado",
                label_es="Aportado",
                source=ColumnSource.METRIC,
                field="deposited",
            ),
            ColumnSpec(
                key="ganancia",
                label_es="Ganancia",
                source=ColumnSource.METRIC,
                field="profit",
                signed=True,
            ),
            ColumnSpec(
                key="var_30d",
                label_es="Var 30d",
                source=ColumnSource.METRIC,
                field="var_30d_pct",
                signed=True,
            ),
            ColumnSpec(
                key="var_anual",
                label_es="Var año",
                source=ColumnSource.METRIC,
                field="var_ytd_pct",
                optional=True,
                signed=True,
            ),
        ),
    ),
    FamilySpec(
        family="crypto",
        label_es="Cripto",
        columns=(
            ColumnSpec(
                key="unidades",
                label_es="Unidades",
                source=ColumnSource.METRIC,
                field="units",
            ),
            ColumnSpec(
                key="congelado",
                label_es="Congelado",
                source=ColumnSource.METRIC,
                field="frozen",
                optional=True,
            ),
            ColumnSpec(key="clp", label_es="≈ CLP", source=ColumnSource.CLP_VALUE),
        ),
    ),
    FamilySpec(
        family="other",
        label_es="Otros",
        columns=(
            ColumnSpec(
                key="saldo",
                label_es="Saldo",
                source=ColumnSource.HEADLINE,
                optional=True,
            ),
        ),
    ),
)

FAMILIES: dict[str, FamilySpec] = {spec.family: spec for spec in _FAMILY_SPECS}

_RESERVED_KEYS = frozenset({"producto", "tipo", "actualizado"})

_DENOMINATION_FORMATS = {
    "currency": ColumnFormat.CURRENCY,
    "percent": ColumnFormat.PERCENT,
    "count": ColumnFormat.COUNT,
}

_NUMERIC_FORMATS = frozenset(
    {ColumnFormat.CURRENCY, ColumnFormat.PERCENT, ColumnFormat.COUNT}
)

_ATTRIBUTE_FORMATS: dict[str, frozenset[ColumnFormat]] = {
    "date": frozenset({ColumnFormat.DATE}),
    "string": frozenset({ColumnFormat.TEXT}),
    "integer": _NUMERIC_FORMATS,
    "number": _NUMERIC_FORMATS,
}


def family_kinds(family: str) -> tuple[str, ...]:
    """Kinds whose registry spec belongs to `family`, in PRODUCT_KINDS order."""
    return tuple(kind for kind in PRODUCT_KINDS if REGISTRY[kind].family == family)


@functools.cache
def _properties(cls: type[BaseModel]) -> dict[str, dict[str, Any]]:
    return cls.model_json_schema().get("properties", {})


def _schema_type(prop: dict[str, Any]) -> str | None:
    """Scalar type of a property schema, unwrapping `X | None`; None if unsupported."""
    if "anyOf" in prop:
        others = [member for member in prop["anyOf"] if member.get("type") != "null"]
        return _schema_type(others[0]) if len(others) == 1 else None
    type_ = prop.get("type")
    if type_ == "string" and prop.get("format") == "date":
        return "date"
    return type_ if type_ in ("string", "integer", "number", "boolean") else None


def _fixed_format(
    where: str, column: ColumnSpec, format_: ColumnFormat
) -> ColumnFormat:
    if column.field is not None:
        raise DisplaySpecError(f"{where}: {column.source.value} columns take no field")
    if column.format not in (None, format_):
        raise DisplaySpecError(
            f"{where}: {column.source.value} columns are always {format_.value}"
        )
    return format_


def _metric_format(
    where: str, column: ColumnSpec, kinds: tuple[str, ...]
) -> ColumnFormat:
    if column.field is None:
        raise DisplaySpecError(f"{where}: metric columns need a field")
    denominations = {
        prop.get("denomination")
        for kind in kinds
        if (prop := _properties(REGISTRY[kind].metrics_cls).get(column.field))
        is not None
    }
    if not denominations:
        raise DisplaySpecError(
            f"{where}: no kind of the family has a metrics.{column.field} field"
        )
    if len(denominations) > 1:
        raise DisplaySpecError(
            f"{where}: metrics.{column.field} has conflicting denominations "
            f"{sorted(map(str, denominations))} across the family's kinds"
        )
    (denomination,) = denominations
    derived = _DENOMINATION_FORMATS.get(denomination)
    if derived is None:
        raise DisplaySpecError(
            f"{where}: metrics.{column.field} lacks a valid denomination marker "
            f"(got {denomination!r})"
        )
    if column.format not in (None, derived):
        raise DisplaySpecError(
            f"{where}: format {column.format.value!r} disagrees with the "
            f"{denomination!r} denomination of metrics.{column.field}"
        )
    return derived


def _attribute_format(
    where: str, column: ColumnSpec, kinds: tuple[str, ...]
) -> ColumnFormat:
    if column.field is None:
        raise DisplaySpecError(f"{where}: attribute columns need a field")
    if column.format is None:
        raise DisplaySpecError(f"{where}: attribute columns need an explicit format")
    types = {
        _schema_type(prop)
        for kind in kinds
        if (prop := _properties(REGISTRY[kind].attributes_cls).get(column.field))
        is not None
    }
    if not types:
        raise DisplaySpecError(
            f"{where}: no kind of the family has an attributes.{column.field} field"
        )
    for type_ in sorted(types, key=str):
        if column.format not in _ATTRIBUTE_FORMATS.get(type_ or "", frozenset()):
            raise DisplaySpecError(
                f"{where}: format {column.format.value!r} does not fit the "
                f"{type_!r} attribute {column.field}"
            )
    return column.format


def _installments_format(
    where: str, column: ColumnSpec, kinds: tuple[str, ...]
) -> ColumnFormat:
    format_ = _fixed_format(where, column, ColumnFormat.COUNT)
    if not any(
        "installments_paid" in _properties(REGISTRY[kind].metrics_cls)
        and "installments_total" in _properties(REGISTRY[kind].attributes_cls)
        for kind in kinds
    ):
        raise DisplaySpecError(
            f"{where}: no kind of the family has both metrics.installments_paid "
            "and attributes.installments_total"
        )
    return format_


def _resolve_family(spec: FamilySpec) -> tuple[ResolvedColumn, ...]:
    """Resolve `spec`'s columns against the payload classes of the family's kinds."""
    kinds = family_kinds(spec.family)
    seen: set[str] = set()
    resolved: list[ResolvedColumn] = []
    for column in spec.columns:
        where = f"{spec.family}.{column.key}"
        if column.key in _RESERVED_KEYS:
            raise DisplaySpecError(f"{where}: key is reserved for a universal column")
        if column.key in seen:
            raise DisplaySpecError(f"{where}: duplicate column key")
        seen.add(column.key)
        match column.source:
            case ColumnSource.HEADLINE | ColumnSource.CLP_VALUE:
                format_ = _fixed_format(where, column, ColumnFormat.CURRENCY)
            case ColumnSource.METRIC:
                format_ = _metric_format(where, column, kinds)
            case ColumnSource.ATTRIBUTE:
                format_ = _attribute_format(where, column, kinds)
            case ColumnSource.INSTALLMENTS:
                format_ = _installments_format(where, column, kinds)
        if column.signed and format_ not in _NUMERIC_FORMATS:
            raise DisplaySpecError(
                f"{where}: signed applies only to currency, percent or count "
                f"columns, not {format_.value}"
            )
        resolved.append(
            ResolvedColumn(
                key=column.key,
                label_es=column.label_es,
                source=column.source,
                field=column.field,
                format=format_,
                align="right" if format_ in _NUMERIC_FORMATS else "left",
                optional=column.optional,
                signed=column.signed,
            )
        )
    return tuple(resolved)


def resolve_columns(family: str) -> tuple[ResolvedColumn, ...]:
    """Columns of `family` with formats and alignment derived from the registry."""
    spec = FAMILIES.get(family)
    if spec is None:
        raise DisplaySpecError(f"unknown family {family!r}")
    return _resolve_family(spec)


def validate_display_specs() -> None:
    """Check every kind's family and every family's columns against the registry.

    Not run at import time; codegen and the tests call it so an inconsistent
    spec fails generation loudly instead of reaching the dashboard.
    """
    for kind in PRODUCT_KINDS:
        family = REGISTRY[kind].family
        if family not in PRODUCT_FAMILIES:
            raise DisplaySpecError(f"{kind}: unknown family {family!r}")
        if family not in FAMILIES:
            raise DisplaySpecError(f"{kind}: family {family!r} has no FamilySpec")
    if tuple(FAMILIES) != PRODUCT_FAMILIES:
        raise DisplaySpecError(
            f"FAMILIES must list {PRODUCT_FAMILIES!r} in order, got {tuple(FAMILIES)!r}"
        )
    for family in PRODUCT_FAMILIES:
        if not family_kinds(family):
            raise DisplaySpecError(f"{family}: no kind belongs to this family")
        resolve_columns(family)
