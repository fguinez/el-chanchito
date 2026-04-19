"""Tenpo scraper: consumes the email backend with a Tenpo-specific pattern."""

import os

from scrapers.backends.email import EmailPattern, fetch_transactions_for_pattern
from scrapers.base import BaseScraper, ScrapedBalance, ScrapedTransaction

PATTERN = EmailPattern(
    institution="tenpo",
    account_type="prepaid",
    sender_contains=["tenpo"],
    subject_contains=["compra", "pago", "transaccion"],
    amount_patterns=[
        r"\$\s*([\d.,]+)",
        r"(?:monto|valor):?\s*\$?\s*([\d.,]+)",
    ],
    merchant_patterns=[
        r"(?:en|comercio)\s+(.+?)(?:\s+por|\.|$|\n)",
    ],
)


class TenpoScraper(BaseScraper):
    method = "email"
    institution = "tenpo"

    def __init__(self) -> None:
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        return await fetch_transactions_for_pattern(PATTERN, self.lookback_days)

    async def scrape_balances(self) -> list[ScrapedBalance]:
        return []
