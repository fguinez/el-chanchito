"""MACH scraper: consumes the email backend with a MACH-specific pattern."""

import os

from scrapers.backends.email import EmailPattern, fetch_transactions_for_pattern
from scrapers.base import BaseScraper, ScrapedProduct, ScrapedTransaction

PATTERN = EmailPattern(
    institution="mach",
    product_kind="wallet",
    sender_contains=["mach", "somosmach", "bci"],
    subject_contains=["compra", "pago", "transaccion", "transferencia"],
    amount_patterns=[
        r"\$\s*([\d.,]+)",
        r"(?:monto|valor):?\s*\$?\s*([\d.,]+)",
    ],
    merchant_patterns=[
        r"(?:en|comercio)\s+(.+?)(?:\s+por|\.|$|\n)",
    ],
)


class MachScraper(BaseScraper):
    method = "email"
    institution = "mach"

    def __init__(self) -> None:
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        return await fetch_transactions_for_pattern(PATTERN, self.lookback_days)

    async def scrape_products(self) -> list[ScrapedProduct]:
        return []
