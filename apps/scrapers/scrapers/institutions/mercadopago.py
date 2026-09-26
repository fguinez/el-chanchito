"""MercadoPago scraper: consumes the email backend with MP-specific patterns."""

import os

from scrapers.backends.email import (
    MERCHANT_END,
    EmailPattern,
    anchored,
    fetch_transactions_for_pattern,
)
from scrapers.base import BaseScraper, ProductScrapeResult, ScrapedTransaction

PATTERN = EmailPattern(
    institution="mercadopago",
    product_kind="wallet",
    sender_domains=["mercadopago", "mercadolibre"],
    subject_contains=[
        "pago", "compra", "transferencia", "pagaste", "recibiste", "dinero",
        "devolvimos", "devolucion", "reembolso",
    ],
    amount_rules=[
        anchored(r"pagaste|compraste|compra\s+de|pago\s+de|transferiste|enviaste"),
        anchored(
            r"recibiste|ingresaste|te\s+(?:transfirieron|enviaron|devolvimos|reembolsamos)"
            r"|devoluci[oó]n|reembolso",
            income=True,
        ),
        anchored(r"monto|total"),
    ],
    merchant_patterns=[
        # The amount is optional ("Pagaste en X" / "Pagaste $ N a X"), but the
        # verb is not: a bare "a" or "en" anywhere in the body never anchors.
        rf"\b(?:pagaste|compraste|transferiste|enviaste)\s+(?:\$\s*[\d.,]+\s+)?"
        rf"(?:en|a)\s+(.+?){MERCHANT_END}",
        rf"\brecibiste\s+\$\s*[\d.,]+\s+de\s+(.+?){MERCHANT_END}",
        rf"\bcompra\s+en\s+(.+?){MERCHANT_END}",
        rf"\bcomercio:?\s+(.+?){MERCHANT_END}",
    ],
    # "ingresaste" is a top-up: money into the wallet from a bank account.
    income_keywords=[
        "recibiste dinero", "recibiste una transferencia", "recibiste un pago",
        "ingresaste", "te transfirieron", "te devolvimos", "devolucion",
        "reembolso", "anulad", "revers",
    ],
)


class MercadoPagoScraper(BaseScraper):
    method = "email"
    institution = "mercadopago"

    def __init__(self) -> None:
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        return await fetch_transactions_for_pattern(PATTERN, self.lookback_days)

    async def scrape_products(self) -> ProductScrapeResult:
        return ProductScrapeResult([])
