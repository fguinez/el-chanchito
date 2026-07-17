"""Tarjeta Lider BCI scraper (stub wrapping open-banking-chile)."""

from scrapers.base import BaseScraper, ScrapedProduct, ScrapedTransaction


class BciLiderScraper(BaseScraper):
    method = "open_banking"
    institution = "bci_lider"

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        # TODO: Integrate open-banking-chile for BCI Lider card
        return []

    async def scrape_products(self) -> list[ScrapedProduct]:
        # TODO: Integrate open-banking-chile for BCI Lider balance
        return []
