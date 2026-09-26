"""Tenpo scraper: consumes the email backend with a Tenpo-specific pattern."""

import os

from scrapers.backends.email import (
    MERCHANT_END,
    EmailPattern,
    anchored,
    fetch_transactions_for_pattern,
)
from scrapers.base import BaseScraper, ProductScrapeResult, ScrapedTransaction

PATTERN = EmailPattern(
    institution="tenpo",
    product_kind="wallet",
    sender_domains=["tenpo"],
    subject_contains=[
        "compra", "pago", "transaccion", "transferencia", "recibiste",
        "devolucion", "reembolso", "cashback",
    ],
    amount_rules=[
        anchored(r"compra(?:ste)?|pagaste|pago\s+de|transferiste|enviaste"),
        anchored(
            r"recibiste|te\s+(?:transfirieron|abonamos|devolvimos)"
            r"|devoluci[oó]n|reembolso|cashback",
            income=True,
        ),
        anchored(r"monto|total|valor"),
    ],
    merchant_patterns=[
        rf"\bcompra\s+(?:de|por)\s+\$\s*[\d.,]+\s+en\s+(.+?){MERCHANT_END}",
        rf"\bcompra(?:ste)?\s+en\s+(.+?){MERCHANT_END}",
        rf"\b(?:transferiste|enviaste)\s+\$\s*[\d.,]+\s+a\s+(.+?){MERCHANT_END}",
        rf"\brecibiste\s+\$\s*[\d.,]+\s+de\s+(.+?){MERCHANT_END}",
        rf"\bcomercio:?\s+(.+?){MERCHANT_END}",
    ],
    # Not "cashback": a purchase email may mention the cashback it earned.
    income_keywords=[
        "recibiste una transferencia", "recibiste dinero", "te transfirieron",
        "devolucion", "reembolso", "anulad", "revers",
    ],
)


class TenpoScraper(BaseScraper):
    method = "email"
    institution = "tenpo"

    def __init__(self) -> None:
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        return await fetch_transactions_for_pattern(PATTERN, self.lookback_days)

    async def scrape_products(self) -> ProductScrapeResult:
        return ProductScrapeResult([])
