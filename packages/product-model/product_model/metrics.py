"""Per-scrape observation payloads, one class per product kind.

Metrics are snapshotted to history on change and denormalized as the latest
value on the product. Each class promotes one field via `headline()`; the
writer stores it in the universal `current_balance` column.

Conventions: CLP amounts are int, USD/crypto amounts are float, dates are
`datetime.date`, percentages are percent numbers (e.g. 4.2 for 4.2%). Every
numeric field carries a `json_schema_extra={"denomination": ...}` marker
("currency" for amounts in the product's currency, "percent" for percent
numbers, "count" for unit counts); the codegen raises if one is missing.
"""

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BaseMetrics(BaseModel):
    """Common base for every per-kind metrics payload."""

    model_config = ConfigDict(extra="forbid")

    def headline(self) -> float | None:
        """Value promoted to the universal current_balance column."""
        raise NotImplementedError


class BankAccountMetrics(BaseMetrics):
    """Shared observation fields for bank accounts (checking/savings/vista)."""

    balance: float = Field(
        description="Available balance in account currency (Saldo disponible).",
        json_schema_extra={"denomination": "currency"},
    )
    accounting_balance: float | None = Field(
        None,
        description="Accounting balance in account currency (Saldo contable).",
        json_schema_extra={"denomination": "currency"},
    )
    reported_as_of: datetime.date | None = Field(
        None, description="Date the institution printed next to the balance."
    )

    def headline(self) -> float | None:
        return self.balance


class RevolvingCreditMetrics(BaseMetrics):
    """Shared observation fields for revolving credit (credit_card/line_of_credit)."""

    available: float = Field(
        description="Available credit in product currency (Disponible).",
        json_schema_extra={"denomination": "currency"},
    )
    limit: float | None = Field(
        None,
        description="Total credit limit in product currency (Cupo total).",
        json_schema_extra={"denomination": "currency"},
    )
    owed: float | None = Field(
        None,
        description="Amount drawn as reported by the bank (Utilizado / Monto utilizado).",
        json_schema_extra={"denomination": "currency"},
    )

    def headline(self) -> float | None:
        return self.available


class InstallmentLoanMetrics(BaseMetrics):
    """Shared observation fields for installment loans (loan/mortgage)."""

    owed: int = Field(
        description="Outstanding amount owed in CLP.",
        json_schema_extra={"denomination": "currency"},
    )
    installments_paid: int | None = Field(
        None,
        description="Number of installments already paid.",
        json_schema_extra={"denomination": "count"},
    )
    next_payment_date: datetime.date | None = Field(
        None, description="Due date of the next installment."
    )
    next_payment_amount: int | None = Field(
        None,
        description="Amount of the next installment in CLP.",
        json_schema_extra={"denomination": "currency"},
    )

    def headline(self) -> float | None:
        return self.owed


class CheckingMetrics(BankAccountMetrics):
    """Observation for a checking account; headline is `balance`."""

    kind: Literal["checking"] = Field(
        "checking", description='Payload discriminator; always "checking".'
    )


class SavingsMetrics(BankAccountMetrics):
    """Observation for a savings account; headline is `balance`."""

    kind: Literal["savings"] = Field(
        "savings", description='Payload discriminator; always "savings".'
    )


class VistaMetrics(BankAccountMetrics):
    """Observation for a vista account; headline is `balance`."""

    kind: Literal["vista"] = Field(
        "vista", description='Payload discriminator; always "vista".'
    )


class WalletMetrics(BaseMetrics):
    """Observation for an app wallet; headline is `balance`."""

    kind: Literal["wallet"] = Field(
        "wallet", description='Payload discriminator; always "wallet".'
    )
    balance: int = Field(
        description="Wallet balance in CLP.",
        json_schema_extra={"denomination": "currency"},
    )

    def headline(self) -> float | None:
        return self.balance


class TermDepositMetrics(BaseMetrics):
    """Observation for a term deposit; headline is `balance`."""

    kind: Literal["term_deposit"] = Field(
        "term_deposit", description='Payload discriminator; always "term_deposit".'
    )
    balance: int = Field(
        description="Current value of the deposit in CLP.",
        json_schema_extra={"denomination": "currency"},
    )

    def headline(self) -> float | None:
        return self.balance


class CreditCardMetrics(RevolvingCreditMetrics):
    """Observation for a credit card; headline is `available`."""

    kind: Literal["credit_card"] = Field(
        "credit_card", description='Payload discriminator; always "credit_card".'
    )
    reported_as_of: datetime.date | None = Field(
        None, description="Date the institution printed next to the card figures."
    )


class DebitCardMetrics(BaseMetrics):
    """Observation for a debit card; empty — money lives in the parent checking."""

    kind: Literal["debit_card"] = Field(
        "debit_card", description='Payload discriminator; always "debit_card".'
    )

    def headline(self) -> float | None:
        return None


class PrepaidCardMetrics(BaseMetrics):
    """Observation for a prepaid card; headline is `balance`."""

    kind: Literal["prepaid_card"] = Field(
        "prepaid_card", description='Payload discriminator; always "prepaid_card".'
    )
    balance: int = Field(
        description="Prepaid balance in CLP.",
        json_schema_extra={"denomination": "currency"},
    )

    def headline(self) -> float | None:
        return self.balance


class LineOfCreditMetrics(RevolvingCreditMetrics):
    """Observation for a line of credit; headline is `available`."""

    kind: Literal["line_of_credit"] = Field(
        "line_of_credit", description='Payload discriminator; always "line_of_credit".'
    )


class LoanMetrics(InstallmentLoanMetrics):
    """Observation for a consumer loan; headline is `owed`."""

    kind: Literal["loan"] = Field(
        "loan", description='Payload discriminator; always "loan".'
    )


class MortgageMetrics(InstallmentLoanMetrics):
    """Observation for a mortgage; headline is `owed`."""

    kind: Literal["mortgage"] = Field(
        "mortgage", description='Payload discriminator; always "mortgage".'
    )


class InvestmentMetrics(BaseMetrics):
    """Observation for an investment position; headline is `nav`."""

    kind: Literal["investment"] = Field(
        "investment", description='Payload discriminator; always "investment".'
    )
    nav: float = Field(
        description="Current market value of the position (net asset value).",
        json_schema_extra={"denomination": "currency"},
    )
    deposited: float | None = Field(
        None,
        description="Total amount deposited to date.",
        json_schema_extra={"denomination": "currency"},
    )
    profit: float | None = Field(
        None,
        description="Cumulative profit as reported (nav minus deposited).",
        json_schema_extra={"denomination": "currency"},
    )
    var_daily_pct: float | None = Field(
        None,
        description="Daily variation as a percent number (Var. diaria).",
        json_schema_extra={"denomination": "percent"},
    )
    var_30d_pct: float | None = Field(
        None,
        description="30-day variation as a percent number (Var. 30 días).",
        json_schema_extra={"denomination": "percent"},
    )
    var_ytd_pct: float | None = Field(
        None,
        description="Year-to-date variation as a percent number (Var. año).",
        json_schema_extra={"denomination": "percent"},
    )

    def headline(self) -> float | None:
        return self.nav


class CryptoMetrics(BaseMetrics):
    """Observation for a crypto holding; headline is `units`."""

    kind: Literal["crypto"] = Field(
        "crypto", description='Payload discriminator; always "crypto".'
    )
    units: float = Field(
        description="Coin units held (fractional).",
        json_schema_extra={"denomination": "currency"},
    )
    frozen: float | None = Field(
        None,
        description="Units locked in open orders.",
        json_schema_extra={"denomination": "currency"},
    )
    pending: float | None = Field(
        None,
        description="Units pending confirmation.",
        json_schema_extra={"denomination": "currency"},
    )

    def headline(self) -> float | None:
        return self.units


class OtherMetrics(BaseMetrics):
    """Observation for an uncategorized product; headline is `balance` if known."""

    kind: Literal["other"] = Field(
        "other", description='Payload discriminator; always "other".'
    )
    balance: int | None = Field(
        None,
        description="Balance in CLP when known.",
        json_schema_extra={"denomination": "currency"},
    )

    def headline(self) -> float | None:
        return self.balance
