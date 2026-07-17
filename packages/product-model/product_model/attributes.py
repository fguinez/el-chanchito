"""Slow-changing identity/config payloads, one class per product kind.

Attributes are shallow-merged on write: a scrape that only saw the dashboard
can never wipe a field (e.g. `last4`) read earlier from a detail page.
"""

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BaseAttributes(BaseModel):
    """Common base for every per-kind attributes payload."""

    model_config = ConfigDict(extra="forbid")


class BankAccountAttributes(BaseAttributes):
    """Shared identity fields for bank accounts (checking/savings/vista)."""

    account_number: str | None = Field(
        None, description="Account number as shown by the institution."
    )


class CardAttributes(BaseAttributes):
    """Shared plastic-card identity fields (credit/debit/prepaid)."""

    last4: str | None = Field(
        None,
        description="Last four digits of the card number (from the masked '****0000').",
    )
    brand: str | None = Field(
        None, description="Card network or product brand (e.g. 'Visa Signature')."
    )


class RevolvingCreditAttributes(BaseAttributes):
    """Marker base for revolving credit products (credit_card/line_of_credit)."""


class InstallmentLoanAttributes(BaseAttributes):
    """Shared config fields for installment loans (loan/mortgage)."""

    original_principal: int | None = Field(
        None, description="Original loan principal in CLP."
    )
    interest_rate_pct: float | None = Field(
        None, description="Annual interest rate as a percent number (e.g. 18.9)."
    )
    installments_total: int | None = Field(
        None, description="Total number of installments (cuotas)."
    )
    opened_date: datetime.date | None = Field(
        None, description="Date the loan was granted."
    )
    due_day: int | None = Field(
        None, description="Day of month each installment is due."
    )


class CheckingAttributes(BankAccountAttributes):
    """Identity fields for a checking account (cuenta corriente)."""

    kind: Literal["checking"] = Field(
        "checking", description='Payload discriminator; always "checking".'
    )


class SavingsAttributes(BankAccountAttributes):
    """Identity fields for a savings account (cuenta de ahorro)."""

    kind: Literal["savings"] = Field(
        "savings", description='Payload discriminator; always "savings".'
    )


class VistaAttributes(BankAccountAttributes):
    """Identity fields for a vista account (cuenta vista)."""

    kind: Literal["vista"] = Field(
        "vista", description='Payload discriminator; always "vista".'
    )


class WalletAttributes(BaseAttributes):
    """Identity fields for an app wallet (MACH, Tenpo, Mercado Pago)."""

    kind: Literal["wallet"] = Field(
        "wallet", description='Payload discriminator; always "wallet".'
    )


class TermDepositAttributes(BaseAttributes):
    """Config fields for a term deposit (depósito a plazo)."""

    kind: Literal["term_deposit"] = Field(
        "term_deposit", description='Payload discriminator; always "term_deposit".'
    )
    issue_date: datetime.date | None = Field(
        None, description="Date the deposit was taken out (Fecha inicio)."
    )
    maturity_date: datetime.date | None = Field(
        None, description="Date the deposit matures (Fecha vencimiento)."
    )
    term_days: int | None = Field(None, description="Term length in days.")
    interest_rate_pct: float | None = Field(
        None, description="Agreed period rate as a percent number (Tasa período)."
    )
    deposit_type: str | None = Field(
        None, description="Deposit modality (e.g. 'Renovable', 'Plazo fijo')."
    )
    principal: int | None = Field(
        None, description="Invested principal in CLP (Monto invertido)."
    )
    maturity_value: int | None = Field(
        None, description="Amount payable at maturity in CLP (Monto al vencimiento)."
    )


class CreditCardAttributes(CardAttributes, RevolvingCreditAttributes):
    """Identity/config fields for a credit card."""

    kind: Literal["credit_card"] = Field(
        "credit_card", description='Payload discriminator; always "credit_card".'
    )
    statement_day: int | None = Field(
        None, description="Day of month the statement closes (facturación)."
    )
    due_day: int | None = Field(
        None, description="Day of month the payment is due (vencimiento)."
    )


class DebitCardAttributes(CardAttributes):
    """Identity fields for a debit card; money lives in the parent checking."""

    kind: Literal["debit_card"] = Field(
        "debit_card", description='Payload discriminator; always "debit_card".'
    )


class PrepaidCardAttributes(CardAttributes):
    """Identity fields for a prepaid card."""

    kind: Literal["prepaid_card"] = Field(
        "prepaid_card", description='Payload discriminator; always "prepaid_card".'
    )


class LineOfCreditAttributes(RevolvingCreditAttributes):
    """Config fields for a line of credit (línea de crédito)."""

    kind: Literal["line_of_credit"] = Field(
        "line_of_credit", description='Payload discriminator; always "line_of_credit".'
    )
    interest_rate_pct: float | None = Field(
        None, description="Annual interest rate as a percent number."
    )


class LoanAttributes(InstallmentLoanAttributes):
    """Config fields for a consumer loan (préstamo)."""

    kind: Literal["loan"] = Field(
        "loan", description='Payload discriminator; always "loan".'
    )


class MortgageAttributes(InstallmentLoanAttributes):
    """Config fields for a mortgage (crédito hipotecario)."""

    kind: Literal["mortgage"] = Field(
        "mortgage", description='Payload discriminator; always "mortgage".'
    )


class InvestmentAttributes(BaseAttributes):
    """Identity fields for an investment position (fund, goal, portfolio)."""

    kind: Literal["investment"] = Field(
        "investment", description='Payload discriminator; always "investment".'
    )
    portfolio: str | None = Field(
        None, description="Portfolio the money is invested in (e.g. 'Risky Norris')."
    )
    risk_profile: str | None = Field(
        None, description="Risk profile label (e.g. 'Arriesgado')."
    )
    fund_name: str | None = Field(
        None, description="Fund name for single-fund products (e.g. a fondo mutuo)."
    )


class CryptoAttributes(BaseAttributes):
    """Identity fields for a crypto holding."""

    kind: Literal["crypto"] = Field(
        "crypto", description='Payload discriminator; always "crypto".'
    )


class OtherAttributes(BaseAttributes):
    """Identity fields for products that fit no other kind."""

    kind: Literal["other"] = Field(
        "other", description='Payload discriminator; always "other".'
    )
    note: str | None = Field(
        None, description="Free-form note describing the product."
    )
