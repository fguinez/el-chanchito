"""Ingest envelopes: what scrapers hand to the DB writer.

These are the ingest contract — today validated in-process, tomorrow the REST
wire format for external scrapers (the generated JSON Schema is the spec).
Neither envelope carries an `as_of`: the writer stamps `now()` at persist
time; bank-printed dates travel inside metrics as `reported_as_of`.
"""

import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from .kinds import ProductKind
from .registry import ProductAttributes, ProductMetrics

# What `transactions.amount` (NUMERIC(18, 2)) can hold. pydantic enforces the
# digits at validation time but leaves them out of the JSON Schema, so the wire
# contract spells the string form out; a JSON number must fit the same bounds.
TransactionAmount = Annotated[
    Decimal,
    Field(max_digits=18, decimal_places=2),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number"},
                {"type": "string", "pattern": r"^-?\d{1,16}(\.\d{1,2})?$"},
            ]
        }
    ),
]


class ScrapedProduct(BaseModel):
    """One scraped observation of a product."""

    model_config = ConfigDict(extra="forbid")

    institution: str = Field(
        description="Institution slug, e.g. 'banchile', 'fintual'."
    )
    kind: ProductKind = Field(
        description="Product kind; selects the attributes/metrics payload family."
    )
    currency: str = Field(
        "CLP", description="ISO currency code of the product (e.g. 'CLP', 'USD')."
    )
    external_ref: str | None = Field(
        None,
        description=(
            "Stable per-institution identifier (e.g. a Fintual goal id); "
            "None for singleton products."
        ),
    )
    name: str | None = Field(
        None,
        description=(
            "Display name used only when creating the product; "
            "never overwrites user renames."
        ),
    )
    attributes: ProductAttributes | None = Field(
        None,
        description="Slow-changing identity/config payload; shallow-merged on write.",
    )
    metrics: ProductMetrics | None = Field(
        None,
        description="Per-scrape observation payload; snapshotted by the writer.",
    )

    @model_validator(mode="after")
    def _payload_kinds_match(self) -> Self:
        """Reject payloads whose discriminator disagrees with the envelope kind."""
        if self.attributes is not None and self.attributes.kind != self.kind:
            raise ValueError(
                f"attributes.kind '{self.attributes.kind}' does not match "
                f"product kind '{self.kind}'"
            )
        if self.metrics is not None and self.metrics.kind != self.kind:
            raise ValueError(
                f"metrics.kind '{self.metrics.kind}' does not match "
                f"product kind '{self.kind}'"
            )
        return self


class ScrapedTransaction(BaseModel):
    """A transaction extracted by a scraper."""

    model_config = ConfigDict(extra="forbid")

    institution: str = Field(
        description="Institution slug, e.g. 'banchile', 'fintual'."
    )
    product_kind: str = Field(
        description="Kind of the product the transaction belongs to, e.g. 'checking'."
    )
    description: str = Field(description="Human-readable transaction description.")
    amount: TransactionAmount = Field(
        description=(
            "Signed amount in the transaction's currency: negative=expense, "
            "positive=income. At most 2 decimals (stored as NUMERIC(18, 2)); "
            "CLP amounts are whole numbers and may be sent as integers."
        ),
    )
    transaction_date: datetime.date = Field(
        description="Date the transaction occurred."
    )
    accounting_date: datetime.date | None = Field(
        None,
        description=(
            "Date the institution posted the transaction, when it reports one "
            "separately from the date it occurred. Nullable: most sources "
            "report a single date, and a NULL simply means 'not reported'. "
            "Never part of a dedup key."
        ),
    )
    external_id: str = Field(description="Unique identifier for dedup.")
    currency: str = Field("CLP", description="ISO currency code (default 'CLP').")
    category_hint: str | None = Field(
        None, description="Scraper's best-guess category."
    )
    scheduled_month: datetime.date | None = Field(
        None,
        description=(
            "First-of-month date the charge lands, for deferred/installment "
            "purchases."
        ),
    )
