"""MACH scraper: consumes the email backend with a MACH-specific pattern."""

import os

from scrapers.backends.email import (
    MERCHANT_END,
    EmailPattern,
    anchored,
    fetch_transactions_for_pattern,
)
from scrapers.base import BaseScraper, ProductScrapeResult, ScrapedTransaction

# MACH is a Bci product, but Banco Bci's own notifications must not land
# here: only MACH's own domains are claimed (see #7).
PATTERN = EmailPattern(
    institution="mach",
    product_kind="wallet",
    sender_domains=["somosmach", "mach"],
    subject_contains=[
        "compra", "pago", "transaccion", "transferencia", "recibiste",
        "devolucion", "reembolso",
    ],
    amount_rules=[
        anchored(r"compra(?:ste)?|pagaste|pago\s+de|transferiste|enviaste"),
        anchored(
            r"recibiste|te\s+(?:transfirieron|enviaron|devolvimos|devolvieron)"
            r"|devoluci[oó]n|reembolso",
            income=True,
        ),
        anchored(r"monto|total|valor"),
    ],
    merchant_patterns=[
        rf"\bcompra(?:ste)?\s+en\s+(.+?){MERCHANT_END}",
        rf"\bcompra\s+(?:de|por)\s+\$\s*[\d.,]+\s+en\s+(.+?){MERCHANT_END}",
        rf"\b(?:transferiste|enviaste)\s+\$\s*[\d.,]+\s+a\s+(.+?){MERCHANT_END}",
        rf"\brecibiste\s+una\s+transferencia\s+de\s+(.+?){MERCHANT_END}",
        rf"\bcomercio:?\s+(.+?){MERCHANT_END}",
    ],
    income_keywords=[
        "recibiste una transferencia", "recibiste dinero", "te transfirieron",
        "devolucion", "reembolso", "anulad", "revers",
    ],
)


class MachScraper(BaseScraper):
    method = "email"
    institution = "mach"

    def __init__(self) -> None:
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        return await fetch_transactions_for_pattern(PATTERN, self.lookback_days)

    async def scrape_products(self) -> ProductScrapeResult:
        return ProductScrapeResult([])
