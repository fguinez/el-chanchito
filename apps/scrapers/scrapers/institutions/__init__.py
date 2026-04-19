"""Platform-specific scraper implementations.

Each module exposes one `BaseScraper` subclass bound to a single institution.
Email-based scrapers consume `scrapers.backends.email`; the BanChile scraper
consumes `scrapers.backends.fintself`. Buda, Fintual and BCI Lider are
self-contained (no shared backend yet).
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
