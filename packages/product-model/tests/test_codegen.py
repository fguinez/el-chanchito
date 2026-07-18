"""Tests for the artifact generator: committed output must never drift."""

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "product_model_generate", PACKAGE_ROOT / "scripts" / "generate.py"
)
generate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate)

ARTIFACTS = (
    Path("generated") / "product-model.schema.json",
    Path("generated") / "index.ts",
    Path("PRODUCTS.md"),
)


@pytest.mark.parametrize("artifact", ARTIFACTS, ids=lambda path: path.name)
def test_committed_artifact_matches_generator_output(artifact, tmp_path):
    """Regenerating into a tmp dir reproduces the committed artifact exactly."""
    generate.generate(tmp_path)

    fresh = (tmp_path / artifact).read_text(encoding="utf-8")
    committed = (PACKAGE_ROOT / artifact).read_text(encoding="utf-8")
    assert fresh == committed, f"{artifact} drifted; run `make product-model-generate`"


def test_generated_ts_marks_nullable_optionals():
    """Nullable optional fields must emit `field?: T | null;` in index.ts."""
    index_ts = (PACKAGE_ROOT / "generated" / "index.ts").read_text(encoding="utf-8")

    assert "last4?: string | null;" in index_ts
    assert "account_number?: string | null;" in index_ts


def test_generator_raises_on_unsupported_construct():
    """Constructs outside the closed scalar set must fail generation loudly."""

    class Unsupported(BaseModel):
        tags: list[str]

    with pytest.raises(generate.UnsupportedSchemaError):
        generate._emit_interface(Unsupported)


def test_generated_ts_exports_metric_fields():
    """METRIC_FIELDS maps each kind's numeric metrics to a denomination."""
    index_ts = (PACKAGE_ROOT / "generated" / "index.ts").read_text(encoding="utf-8")

    assert 'export type MetricDenomination = "currency" | "percent" | "count";' in index_ts
    assert "export const METRIC_FIELDS: Record<" in index_ts
    assert '    owed: { denomination: "currency" },' in index_ts
    assert '    installments_paid: { denomination: "count" },' in index_ts
    assert '    var_daily_pct: { denomination: "percent" },' in index_ts
    assert "  debit_card: {}," in index_ts
    assert "reported_as_of: { denomination" not in index_ts


def test_generator_raises_on_unmarked_numeric_metric():
    """A numeric metric field without a denomination marker must fail loudly."""

    class Unmarked(BaseModel):
        balance: int

    with pytest.raises(generate.UnsupportedSchemaError):
        generate._metric_denominations(Unmarked)
