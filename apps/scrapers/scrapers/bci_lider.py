"""Tarjeta Lider BCI scraper wrapping open-banking-chile."""

from datetime import date

from .base import BaseScraper, ScrapedBalance, ScrapedTransaction


class BciLiderScraper(BaseScraper):
    @property
    def name(self) -> str:
        return "bci_lider"

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        # TODO: Integrate open-banking-chile for BCI Lider card
        return []

    async def scrape_balances(self) -> list[ScrapedBalance]:
        # TODO: Integrate open-banking-chile for BCI Lider balance
        return []
