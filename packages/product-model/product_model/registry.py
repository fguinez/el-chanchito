"""Kind registry: one spec per product kind, plus the discriminated unions."""

from dataclasses import dataclass
from typing import Annotated, Union

from pydantic import Field

from .attributes import (
    BaseAttributes,
    CheckingAttributes,
    CreditCardAttributes,
    CryptoAttributes,
    DebitCardAttributes,
    InvestmentAttributes,
    LineOfCreditAttributes,
    LoanAttributes,
    MortgageAttributes,
    OtherAttributes,
    PrepaidCardAttributes,
    SavingsAttributes,
    TermDepositAttributes,
    VistaAttributes,
    WalletAttributes,
)
from .kinds import BalanceConvention, NetWorthRole, ProductKind
from .metrics import (
    BaseMetrics,
    CheckingMetrics,
    CreditCardMetrics,
    CryptoMetrics,
    DebitCardMetrics,
    InvestmentMetrics,
    LineOfCreditMetrics,
    LoanMetrics,
    MortgageMetrics,
    OtherMetrics,
    PrepaidCardMetrics,
    SavingsMetrics,
    TermDepositMetrics,
    VistaMetrics,
    WalletMetrics,
)


@dataclass(frozen=True, slots=True)
class KindSpec:
    """Everything the system knows about one product kind."""

    kind: ProductKind
    label_es: str
    role: NetWorthRole
    balance_convention: BalanceConvention
    attributes_cls: type[BaseAttributes]
    metrics_cls: type[BaseMetrics]


_SPECS: tuple[KindSpec, ...] = (
    KindSpec(
        kind="checking",
        label_es="Cuenta corriente",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.VALUE,
        attributes_cls=CheckingAttributes,
        metrics_cls=CheckingMetrics,
    ),
    KindSpec(
        kind="savings",
        label_es="Ahorro",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.VALUE,
        attributes_cls=SavingsAttributes,
        metrics_cls=SavingsMetrics,
    ),
    KindSpec(
        kind="vista",
        label_es="Cuenta vista",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.VALUE,
        attributes_cls=VistaAttributes,
        metrics_cls=VistaMetrics,
    ),
    KindSpec(
        kind="wallet",
        label_es="Billetera",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.VALUE,
        attributes_cls=WalletAttributes,
        metrics_cls=WalletMetrics,
    ),
    KindSpec(
        kind="term_deposit",
        label_es="Depósito a plazo",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.VALUE,
        attributes_cls=TermDepositAttributes,
        metrics_cls=TermDepositMetrics,
    ),
    KindSpec(
        kind="credit_card",
        label_es="Tarjeta de crédito",
        role=NetWorthRole.LIABILITY,
        balance_convention=BalanceConvention.AVAILABLE,
        attributes_cls=CreditCardAttributes,
        metrics_cls=CreditCardMetrics,
    ),
    KindSpec(
        kind="debit_card",
        label_es="Tarjeta de débito",
        role=NetWorthRole.NONE,
        balance_convention=BalanceConvention.NONE,
        attributes_cls=DebitCardAttributes,
        metrics_cls=DebitCardMetrics,
    ),
    KindSpec(
        kind="prepaid_card",
        label_es="Tarjeta prepago",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.VALUE,
        attributes_cls=PrepaidCardAttributes,
        metrics_cls=PrepaidCardMetrics,
    ),
    KindSpec(
        kind="line_of_credit",
        label_es="Línea de crédito",
        role=NetWorthRole.LIABILITY,
        balance_convention=BalanceConvention.AVAILABLE,
        attributes_cls=LineOfCreditAttributes,
        metrics_cls=LineOfCreditMetrics,
    ),
    KindSpec(
        kind="loan",
        label_es="Préstamo",
        role=NetWorthRole.LIABILITY,
        balance_convention=BalanceConvention.OWED,
        attributes_cls=LoanAttributes,
        metrics_cls=LoanMetrics,
    ),
    KindSpec(
        kind="mortgage",
        label_es="Hipotecario",
        role=NetWorthRole.LIABILITY,
        balance_convention=BalanceConvention.OWED,
        attributes_cls=MortgageAttributes,
        metrics_cls=MortgageMetrics,
    ),
    KindSpec(
        kind="investment",
        label_es="Inversión",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.VALUE,
        attributes_cls=InvestmentAttributes,
        metrics_cls=InvestmentMetrics,
    ),
    KindSpec(
        kind="crypto",
        label_es="Cripto",
        role=NetWorthRole.ASSET,
        balance_convention=BalanceConvention.UNITS,
        attributes_cls=CryptoAttributes,
        metrics_cls=CryptoMetrics,
    ),
    KindSpec(
        kind="other",
        label_es="Otro",
        role=NetWorthRole.NONE,
        balance_convention=BalanceConvention.NONE,
        attributes_cls=OtherAttributes,
        metrics_cls=OtherMetrics,
    ),
)

REGISTRY: dict[str, KindSpec] = {spec.kind: spec for spec in _SPECS}

ProductAttributes = Annotated[
    Union[
        CheckingAttributes,
        SavingsAttributes,
        VistaAttributes,
        WalletAttributes,
        TermDepositAttributes,
        CreditCardAttributes,
        DebitCardAttributes,
        PrepaidCardAttributes,
        LineOfCreditAttributes,
        LoanAttributes,
        MortgageAttributes,
        InvestmentAttributes,
        CryptoAttributes,
        OtherAttributes,
    ],
    Field(discriminator="kind"),
]

ProductMetrics = Annotated[
    Union[
        CheckingMetrics,
        SavingsMetrics,
        VistaMetrics,
        WalletMetrics,
        TermDepositMetrics,
        CreditCardMetrics,
        DebitCardMetrics,
        PrepaidCardMetrics,
        LineOfCreditMetrics,
        LoanMetrics,
        MortgageMetrics,
        InvestmentMetrics,
        CryptoMetrics,
        OtherMetrics,
    ],
    Field(discriminator="kind"),
]
