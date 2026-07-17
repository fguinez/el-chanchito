"""Platform-specific scraper implementations.

Each module exposes one `BaseScraper` subclass bound to a single institution.
Email-based scrapers consume `scrapers.backends.email`; the BanChile scraper
consumes `scrapers.backends.fintself` for transactions and
`scrapers.backends.banchile_web` (its own Playwright login) for balances; the
BCI Lider scraper consumes `scrapers.backends.bci_lider_web` (a real Chrome driven
over CDP) for both legs. Buda and Fintual are self-contained (HTTP APIs).
"""

from scrapers.institutions.banchile import BanChileScraper
from scrapers.institutions.bci_lider import BciLiderScraper
from scrapers.institutions.buda import BudaScraper
from scrapers.institutions.fintual import FintualScraper
from scrapers.institutions.mach import MachScraper
from scrapers.institutions.mercadopago import MercadoPagoScraper
from scrapers.institutions.tenpo import TenpoScraper

__all__ = [
    "BanChileScraper",
    "BciLiderScraper",
    "BudaScraper",
    "FintualScraper",
    "MachScraper",
    "MercadoPagoScraper",
    "TenpoScraper",
]
