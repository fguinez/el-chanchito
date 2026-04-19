"""MercadoPago scraper: consumes the email backend with MP-specific patterns."""

import os

from scrapers.backends.email import EmailPattern, fetch_transactions_for_pattern
from scrapers.base import BaseScraper, ScrapedBalance, ScrapedTransaction

PATTERN = EmailPattern(
    institution="mercadopago",
    account_type="prepaid",
    sender_contains=["mercadopago", "mercadolibre"],
    subject_contains=["pago", "compra", "transferencia", "pagaste"],
    amount_patterns=[
        r"(?:pagaste|pago de|monto:?)\s*\$?\s*([\d.,]+)",
        r"\$\s*([\d.,]+)",
    ],
    merchant_patterns=[
        r"\b(?:en|a)\s+(.+?)(?:\.|$|\n)",
        r"comercio:?\s*(.+?)(?:\.|$|\n)",
    ],
)


class MercadoPagoScraper(BaseScraper):
    method = "email"
    institution = "mercadopago"

    def __init__(self) -> None:
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        return await fetch_transactions_for_pattern(PATTERN, self.lookback_days)

    async def scrape_balances(self) -> list[ScrapedBalance]:
        return []
