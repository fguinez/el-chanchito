"""Ingest envelopes: what scrapers hand to the DB writer.

These are the ingest contract — today validated in-process, tomorrow the REST
wire format for external scrapers (the generated JSON Schema is the spec).
No envelope carries an `as_of`: the writer stamps `now()` at persist time;
bank-printed dates travel as `reported_as_of` (inside metrics for a product,
on the envelope for an exchange rate).
"""

import datetime
import re
from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from .kinds import ProductKind
from .registry import ProductAttributes, ProductMetrics

# What `transactions.amount` (NUMERIC(18, 2)) can hold. pydantic enforces the
# digits at validation time but leaves them out of the JSON Schema, so the wire
# contract spells the string form out; a JSON number must fit the same bounds.
_AMOUNT_PATTERN = r"^-?\d{1,16}(\.\d{1,2})?$"
TransactionAmount = Annotated[
    Decimal,
    Field(max_digits=18, decimal_places=2),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number"},
                {"type": "string", "pattern": _AMOUNT_PATTERN},
            ]
        }
    ),
]
_CENT = Decimal("0.01")

# Longest location text the API accepts; the dashboard enforces the same bound.
LOCATION_TEXT_MAX_LENGTH = 200
# `transactions.location_latitude/longitude` are NUMERIC(9, 6).
_COORDINATE_DECIMALS = 6


def _location_text(value):
    """Trim a location string; blank (or None) becomes None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("location text must be a string")
    value = value.strip()
    return value or None


class TransactionLocation(BaseModel):
    """Where a transaction happened, as far as its source says.

    Every field is optional and independent of the others: a source that
    reports only a city sends only `city`. Text is trimmed, blank means
    absent, and nothing is re-cased (descriptions are stored as given too).
    Coordinates come in pairs. Frozen, so it can ride on hashable records
    such as a scraper's frozen dataclasses.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(
        None,
        max_length=LOCATION_TEXT_MAX_LENGTH,
        description="Merchant or place name, when it differs from the description.",
    )
    city: str | None = Field(
        None,
        max_length=LOCATION_TEXT_MAX_LENGTH,
        description="City, as the source spells it.",
    )
    country: str | None = Field(
        None,
        max_length=LOCATION_TEXT_MAX_LENGTH,
        description=(
            "Country name as the source spells it (e.g. 'Chile'); a source that "
            "only reports an ISO 3166 code sends the code."
        ),
    )
    latitude: float | None = Field(
        None, ge=-90, le=90, description="WGS84 latitude in degrees."
    )
    longitude: float | None = Field(
        None, ge=-180, le=180, description="WGS84 longitude in degrees."
    )

    @field_validator("name", "city", "country", mode="before")
    @classmethod
    def _trim(cls, value):
        return _location_text(value)

    @field_validator("latitude", "longitude")
    @classmethod
    def _round_to_stored_scale(cls, value):
        return None if value is None else round(value, _COORDINATE_DECIMALS)

    @model_validator(mode="after")
    def _coordinates_come_in_pairs(self) -> Self:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be given together")
        return self

    def is_empty(self) -> bool:
        """True when no field carries a value."""
        return all(
            value is None
            for value in (
                self.name,
                self.city,
                self.country,
                self.latitude,
                self.longitude,
            )
        )


# What `exchange_rates.rate` (NUMERIC(20, 8), CHECK > 0) can hold, spelled out
# for the wire contract the same way as the transaction amount.
_RATE_PATTERN = r"^\d{1,12}(\.\d{1,8})?$"
ExchangeRateValue = Annotated[
    Decimal,
    Field(gt=0, max_digits=20, decimal_places=8),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number", "exclusiveMinimum": 0},
                {"type": "string", "pattern": _RATE_PATTERN},
            ]
        }
    ),
]
# An upper-case currency code: ISO 4217 for fiat ("USD"), a ticker for crypto.
_CURRENCY_CODE_PATTERN = r"^[A-Z]{3,10}$"


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
    product_external_ref: str | None = Field(
        None,
        min_length=1,
        description=(
            "external_ref of the product of this institution, product_kind and "
            "currency the transaction belongs to, for kinds with one product per "
            "holding (e.g. a stock position). Such a product is never created "
            "for a transaction: until a product leg has stored it, the "
            "transaction is left for a later run. None files it on the product "
            "with no external_ref, created if missing."
        ),
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
    internal_transfer: bool = Field(
        False,
        description=(
            "True when the movement only moves the user's money between their "
            "own products (e.g. a stock purchase paid from a cash wallet), so "
            "it is neither spending nor income. Stored as is_internal_transfer "
            "when the row is first written, and never rewritten by a re-scrape, "
            "so a user's change wins."
        ),
    )
    scheduled_month: datetime.date | None = Field(
        None,
        description=(
            "First-of-month date the charge lands, for deferred/installment "
            "purchases."
        ),
    )
    location: TransactionLocation | None = Field(
        None,
        description=(
            "Where the transaction happened, when the source reports it. "
            "Written only into location columns that are still NULL, and never "
            "onto a row whose location the user edited."
        ),
    )

    @field_validator("location")
    @classmethod
    def _empty_location_is_none(cls, value):
        """A location with no field set is the same as no location."""
        return None if value is not None and value.is_empty() else value

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_string_matches_the_wire_pattern(cls, value):
        """A string amount must be the schema's form: no exponent, sign or spaces."""
        if isinstance(value, str) and not re.fullmatch(_AMOUNT_PATTERN, value):
            raise ValueError(f"amount string {value!r} must match {_AMOUNT_PATTERN}")
        return value

    @model_validator(mode="after")
    def _amount_fits_its_currency(self) -> Self:
        """CLP is whole; every amount is stored in one canonical form.

        A CLP amount with a fraction is rejected rather than rounded. After
        that, CLP is quantized to units and any other currency to cents, so
        `str(amount)` never shows an exponent ("1E+3") and always agrees with
        the JSON Schema's string pattern.
        """
        if self.currency == "CLP":
            if self.amount != self.amount.to_integral_value():
                raise ValueError(f"a CLP amount must be whole, got {self.amount}")
            self.amount = self.amount.quantize(Decimal(1))
        else:
            self.amount = self.amount.quantize(_CENT)
        return self


class ProductRef(BaseModel):
    """The identity of one product of the envelope's institution.

    The same (kind, currency, external_ref) triple the writer resolves a
    `ScrapedProduct` by, so a scraper names a product exactly as it emits it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ProductKind = Field(description="Kind of the product, e.g. 'credit_card'.")
    currency: str = Field(
        description="Upper-case currency code of the product, e.g. 'USD'.",
        pattern=_CURRENCY_CODE_PATTERN,
    )
    external_ref: str | None = Field(
        None,
        description="The product's external_ref; None for singleton products.",
    )


class ScrapedExchangeRate(BaseModel):
    """One exchange rate an institution applies, as a scraper read it.

    Scoped to one product (`product`) or to the whole institution (None). The
    writer only ever attaches it to an institution and product that already
    exist: it never creates one to hang a rate on.
    """

    model_config = ConfigDict(extra="forbid")

    institution: str = Field(
        description="Institution slug, e.g. 'banchile', 'fintual'."
    )
    base_currency: str = Field(
        description="Upper-case code of the currency being priced, e.g. 'USD'.",
        pattern=_CURRENCY_CODE_PATTERN,
    )
    quote_currency: str = Field(
        "CLP",
        description="Upper-case code of the currency the rate is quoted in.",
        pattern=_CURRENCY_CODE_PATTERN,
    )
    rate: ExchangeRateValue = Field(
        description=(
            "Quote units per 1 base unit (e.g. CLP per USD); positive, at most "
            "8 decimals (stored as NUMERIC(20, 8))."
        ),
    )
    product: ProductRef | None = Field(
        None,
        description=(
            "The product of this institution the rate applies to; None for an "
            "institution-wide rate."
        ),
    )
    source: str = Field(
        min_length=1,
        description="Spanish label of where the rate was read, shown in the UI.",
    )
    reported_as_of: datetime.date | None = Field(
        None,
        description="Validity date the institution prints next to the rate, if any.",
    )

    @field_validator("rate", mode="before")
    @classmethod
    def _rate_string_matches_the_wire_pattern(cls, value):
        """A string rate must be the schema's form: plain digits, a dot decimal."""
        if isinstance(value, str) and not re.fullmatch(_RATE_PATTERN, value):
            raise ValueError(f"rate string {value!r} must match {_RATE_PATTERN}")
        return value

    @field_validator("source")
    @classmethod
    def _source_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source must not be blank")
        return value

    @model_validator(mode="after")
    def _currencies_are_consistent(self) -> Self:
        """A rate prices one currency in another; a product rate prices its own."""
        if self.base_currency == self.quote_currency:
            raise ValueError(
                f"base_currency and quote_currency are both '{self.base_currency}'"
            )
        if self.product is not None and self.product.currency != self.base_currency:
            raise ValueError(
                f"product currency '{self.product.currency}' does not match "
                f"base_currency '{self.base_currency}'"
            )
        return self
