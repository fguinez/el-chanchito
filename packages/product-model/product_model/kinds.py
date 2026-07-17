"""Product kinds and the classification enums the registry hangs off them."""

from enum import StrEnum
from typing import Literal

PRODUCT_KINDS: tuple[str, ...] = (
    "checking",
    "savings",
    "vista",
    "wallet",
    "term_deposit",
    "credit_card",
    "debit_card",
    "prepaid_card",
    "line_of_credit",
    "loan",
    "mortgage",
    "investment",
    "crypto",
    "other",
)

ProductKind = Literal[
    "checking",
    "savings",
    "vista",
    "wallet",
    "term_deposit",
    "credit_card",
    "debit_card",
    "prepaid_card",
    "line_of_credit",
    "loan",
    "mortgage",
    "investment",
    "crypto",
    "other",
]


class NetWorthRole(StrEnum):
    """How a kind contributes to net worth."""

    ASSET = "asset"
    LIABILITY = "liability"
    NONE = "none"


class BalanceConvention(StrEnum):
    """What the headline metric promoted to current_balance means."""

    VALUE = "value"
    AVAILABLE = "available"
    OWED = "owed"
    UNITS = "units"
    NONE = "none"
