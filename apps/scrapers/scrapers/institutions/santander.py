"""Banco Santander Chile scraper: headless login, data from the SPA's own JSON.

Decision record (condensed). Rung 5 of the acquisition ladder for both legs:
a headless Playwright login, then the JSON the private SPA loads for itself
(`backends/santander_web.py`), with no DOM scraping. Higher rungs rejected:

  1. Official API: Santander offers none to individuals. Chile's Open Finance
     system (Ley 21.521, NCG 514/569) is expected to operate from July 2027,
     and then only through registered participants.
  2. Maintained library: `fintself` scrapes the rendered DOM and never sees a
     movement id; `kaihv/open-banking-chile` is Node and has no stable ids
     either.
  3. Private API over plain HTTP: the login runs behind Akamai's edge with a
     sensor payload and BioCatch telemetry; a plain HTTP client gets the
     "Revisa tu conexión a internet" block page.
  4. E-mail notifications: transactions only, no balance; kept as the escape
     hatch if the browser path stops working.

Scope: the CLP cuenta vista only, modelled as the existing `vista` kind and
emitted as a singleton (no `external_ref`) so its movements land on it. Any
other product in the list is ignored (logged as a count). Every transaction
carries `scheduled_month` and the bank's posting date as `accounting_date`.

Session: one browser drive per cycle feeds both legs. The transactions leg
runs first (`run_scraper`'s order), drives the session once and caches the
result or the error; the products leg serves the cached result, or re-raises
the cached error without logging in again. There is no session cache between
runs: a daily full login is the whole cost.

Set `SANTANDER_HEADLESS=0` to watch the browser while debugging.
"""

import logging
import os
from datetime import date

from product_model import VistaAttributes, VistaMetrics
from scrapers.backends.santander_web import (
    SantanderAccount,
    SantanderMovement,
    SantanderSessionResult,
    external_id_for,
    fetch_session,
)
from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)

logger = logging.getLogger(__name__)

PRODUCT_KIND = "vista"
PRODUCT_NAME = "Cuenta Vista"
_FALSE_VALUES = {"0", "false", "no", "off"}


def headless_from_env() -> bool:
    """`SANTANDER_HEADLESS`: headless unless set to 0/false/no/off."""
    value = os.environ.get("SANTANDER_HEADLESS", "").strip().lower()
    return value not in _FALSE_VALUES


def account_to_product(account: SantanderAccount) -> ScrapedProduct:
    """The singleton CLP vista product for one parsed account.

    The account number goes in attributes only (display); it never names the
    product or keys it.
    """
    return ScrapedProduct(
        institution=SantanderScraper.institution,
        kind=PRODUCT_KIND,
        currency=account.currency,
        name=PRODUCT_NAME,
        attributes=VistaAttributes(account_number=account.display_number),
        metrics=VistaMetrics(balance=float(account.balance)),
    )


def movement_to_transaction(movement: SantanderMovement) -> ScrapedTransaction:
    """One movement as a transaction on the singleton vista product.

    `transaction_date` is when it happened, `accounting_date` when the bank
    posted it, and `scheduled_month` the first of the occurrence month.
    """
    tx_date = movement.transaction_date
    return ScrapedTransaction(
        institution=SantanderScraper.institution,
        product_kind=PRODUCT_KIND,
        description=movement.description,
        amount=movement.amount,
        currency=movement.currency,
        transaction_date=tx_date,
        accounting_date=movement.accounting_date,
        external_id=external_id_for(movement),
        scheduled_month=date(tx_date.year, tx_date.month, 1),
    )


class SantanderScraper(BaseScraper):
    method = "web"
    institution = "santander"

    def __init__(self) -> None:
        self.rut = os.environ["SANTANDER_RUT"]
        self.password = os.environ["SANTANDER_PASSWORD"]
        self.headless = headless_from_env()
        # One browser drive per cycle (see module docstring).
        self._cached: SantanderSessionResult | None = None
        self._error: Exception | None = None

    async def _drive_once(self) -> SantanderSessionResult:
        """Log in once, caching the result (or the error) for the other leg."""
        self._cached = None
        self._error = None
        try:
            result = await fetch_session(self.rut, self.password, headless=self.headless)
        except ImportError as exc:
            logger.exception("Playwright not installed. Run: pip install -r requirements.txt")
            self._error = exc
            raise
        except Exception as exc:
            logger.error("Santander scrape failed: %s", type(exc).__name__)
            self._error = exc
            raise
        self._cached = result
        return result

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        result = await self._drive_once()
        transactions: list[ScrapedTransaction] = []
        failed = 0
        for movement in result.movements:
            try:
                transactions.append(movement_to_transaction(movement))
            except Exception:
                failed += 1
        if failed:
            logger.warning("Santander: %d movements could not be converted", failed)
        logger.info("Santander: %d cuenta vista transactions", len(transactions))
        return transactions

    async def scrape_products(self) -> ProductScrapeResult:
        """Serve the session the transactions leg opened.

        Re-raises that leg's error without logging in again. Only when neither
        leg has run this cycle (not the `run_scraper` path) does it drive the
        browser itself.
        """
        if self._cached is not None:
            result, self._cached = self._cached, None
        elif self._error is not None:
            error, self._error = self._error, None
            raise error
        else:
            result = await self._drive_once()
            self._cached = None
        products = [account_to_product(result.account)] if result.account else []
        return ProductScrapeResult(products, list(result.warnings))
