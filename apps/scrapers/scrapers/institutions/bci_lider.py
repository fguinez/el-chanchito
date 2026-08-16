"""Tarjeta Lider Bci scraper: real Chrome driven over CDP.

Tarjeta Lider Bci (the retailcard.cl credit card co-branded by BCI) has no
open-banking API for individuals and isn't covered by `fintself`, and its login
sits behind a Cloudflare Turnstile that passes only for a genuine browser (a
Playwright-launched or headless one gets an unsolvable interactive check). So the
scraper drives a real Chrome over CDP (`backends/bci_lider_web.py`): by default it
runs *managed* (launches a headed Chrome, signs in via autofill, scrapes, closes
it) so scheduled runs are fully unattended (needs a machine with a display).
Setting `LIDER_BCI_CDP_URL` switches to reusing a long-running Chrome from `make
bci-lider-login`. Either way it reuses an already-signed-in tab or re-logs-in via
autofill, and fails with a clear "run make bci-lider-login" message when Chrome
isn't reachable.

`scrape_transactions()` maps the Nacionales (CLP) charges to `ScrapedTransaction`s
and `scrape_products()` emits the CLP + USD `credit_card` observations. Both come
from one browser session per cycle: the transactions leg does the scrape and
caches the result for the products leg (a session error is re-raised, not
retried), so the Chrome is driven once.
"""

import asyncio
import hashlib
import logging
import os

from scrapers.backends.bci_lider_web import (
    BciLiderCardResult,
    save_login_session,
    scrape_card,
)
from scrapers.base import BaseScraper, ProductScrapeResult, ScrapedTransaction

logger = logging.getLogger(__name__)


class BciLiderScraper(BaseScraper):
    method = "web"
    institution = "bci_lider"

    def __init__(self) -> None:
        self.rut = os.environ["LIDER_BCI_RUT"]
        self.password = os.environ.get("LIDER_BCI_PASSWORD")
        # One browser session feeds both legs (see module docstring): the
        # transactions leg runs first (run_scraper's order) and stashes the
        # result or the error.
        self._cached: BciLiderCardResult | None = None
        self._error: Exception | None = None

    def _movement_to_transaction(self, movement: dict) -> ScrapedTransaction:
        """Convert a raw backend movement dict to a ScrapedTransaction.

        `external_id` is a stable hash of date + amount (the portal exposes no
        per-movement id in the DOM), so a charge dedups across runs and across the
        "Por facturar" -> "Último periodo facturado" transition. Charges are
        already signed negative by the backend.

        The description is deliberately NOT hashed: the portal rewrites it when a
        charge is billed (a city suffix appears, a truncated name grows, or the
        merchant's legal name replaces the storefront one), so hashing it imported
        the same charge a second time and doubled the month's spend.

        Known limitation: two genuinely distinct charges sharing date and amount
        collapse to one row; without a per-movement id this is unavoidable, and it
        mirrors BanChile's `bch_` scheme. `cuotas` is left out of the hash for the
        same reason as the description: the installment counter changes between
        cycles.
        """
        tx_date = movement["date"]
        description = movement["description"]
        amount = movement["amount"]

        raw_str = f"{tx_date.isoformat()}|{amount}|CLP"
        external_id = f"bcl_{hashlib.md5(raw_str.encode()).hexdigest()[:16]}"

        return ScrapedTransaction(
            institution=self.institution,
            product_kind="credit_card",
            description=description,
            amount=amount,
            transaction_date=tx_date,
            external_id=external_id,
        )

    async def _scrape_once(self) -> BciLiderCardResult:
        """Drive the real Chrome once, caching the result (or error) per cycle."""
        self._cached = None
        self._error = None
        try:
            result = await scrape_card(rut=self.rut, password=self.password)
        except ImportError as exc:
            # A missing runtime dep is a broken deployment, not a clean run.
            logger.exception("Playwright not installed. Run: pip install -r requirements.txt")
            self._error = exc
            raise
        except Exception as exc:
            # BciLiderSessionError (Chrome unreachable / sign-in failed) and any
            # other failure land here; cache so the products leg doesn't re-drive.
            logger.exception("BciLider scrape failed")
            self._error = exc
            raise
        self._cached = result
        return result

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        result = await self._scrape_once()
        transactions: list[ScrapedTransaction] = []
        for mov in result.movements:
            try:
                transactions.append(self._movement_to_transaction(mov))
            except Exception:
                logger.exception("Failed to convert movement: %s", mov.get("description", "?"))
        logger.info("BciLider: %d credit card transactions", len(transactions))
        return transactions

    async def scrape_products(self) -> ProductScrapeResult:
        """Emit the card products from the session the transactions leg opened.

        Reuses the cached result so the Chrome is driven once per cycle. If that
        drive failed (Chrome unreachable, sign-in failed, etc.), its error is
        re-raised here without re-driving. Only if neither leg has run yet (not the
        run_scraper path) does this drive the Chrome on its own.
        """
        if self._cached is not None:
            result, self._cached = self._cached, None
            return ProductScrapeResult(result.products, result.warnings)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        result = await self._scrape_once()
        return ProductScrapeResult(result.products, result.warnings)

    async def login(self) -> str:
        """Manual sign-in: launch a real Chrome, autofill the login, leave it running."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, save_login_session, self.rut, self.password
        )


def _login_cli() -> None:
    """Manual sign-in: `python -m scrapers.institutions.bci_lider` (or `make bci-lider-login`)."""
    from dotenv import load_dotenv

    load_dotenv()  # walks up to the repo-root .env for LIDER_BCI_RUT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    scraper = BciLiderScraper()
    cdp_url = asyncio.run(scraper.login())
    print(f"\n✅ Signed in. Keep this Chrome running and set LIDER_BCI_CDP_URL={cdp_url}")


if __name__ == "__main__":
    _login_cli()
