"""Banco de Chile scraper using fintself.

fintself uses Playwright browser automation to scrape BancoDeChile.
MovementModel fields: date, description, amount, currency, transaction_type,
                      account_id, account_type ('corriente'|'credito'|'debito'|'prepago')
"""

import asyncio
import hashlib
import logging
import os
from datetime import date, datetime
from decimal import Decimal

from scrapers.base import BaseScraper, ScrapedBalance, ScrapedTransaction

logger = logging.getLogger(__name__)


class BanChileScraper(BaseScraper):
    @property
    def name(self) -> str:
        return "fintself_banchile"

    def __init__(self) -> None:
        self.rut = os.environ["BANCHILE_RUT"]
        self.password = os.environ["BANCHILE_PASSWORD"]

    def _movement_to_transaction(self, movement) -> ScrapedTransaction:
        """Convert a fintself MovementModel to our ScrapedTransaction."""
        amount_clp = int(movement.amount)
        # movement.date is datetime per fintself's MovementModel
        tx_date = movement.date.date() if isinstance(movement.date, datetime) else movement.date

        # Determine account type
        acct_type = "checking"
        if movement.account_type and movement.account_type.value == "credito":
            acct_type = "credit_card"

        # Build a stable external_id for dedup
        raw_str = f"{tx_date.isoformat()}|{movement.description}|{amount_clp}|{movement.account_id or ''}"
        external_id = f"bch_{hashlib.md5(raw_str.encode()).hexdigest()[:16]}"

        # Derive scheduled month
        scheduled = date(tx_date.year, tx_date.month, 1)

        return ScrapedTransaction(
            account_institution="banchile",
            account_type=acct_type,
            description=movement.description,
            amount=amount_clp,
            transaction_date=tx_date,
            external_id=external_id,
            scheduled_month=scheduled,
            category_hint=movement.transaction_type,
        )

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """Run fintself BancoDeChile scraper and return transactions."""
        try:
            from fintself import get_scraper

            scraper = get_scraper("cl_banco_chile")
            scraper.user = self.rut
            scraper.password = self.password
            scraper.headless = True

            logger.info("Starting BancoDeChile browser scrape...")

            # fintself's scrape() is synchronous and uses Playwright
            # Run in executor to avoid blocking the async loop
            loop = asyncio.get_event_loop()
            movements = await loop.run_in_executor(None, scraper.scrape)

            logger.info("BancoDeChile returned %d movements", len(movements))

            transactions = []
            for mov in movements:
                try:
                    transactions.append(self._movement_to_transaction(mov))
                except Exception:
                    logger.exception("Failed to convert movement: %s", mov.description)

            # Log summary by account type
            checking = [t for t in transactions if t.account_type == "checking"]
            credit = [t for t in transactions if t.account_type == "credit_card"]
            logger.info(
                "BancoDeChile: %d checking, %d credit card transactions",
                len(checking),
                len(credit),
            )

            return transactions

        except ImportError:
            logger.error("fintself not installed. Run: pip install fintself")
            return []
        except Exception:
            logger.exception("BancoDeChile scrape failed")
            raise

    async def scrape_balances(self) -> list[ScrapedBalance]:
        """fintself doesn't expose balances directly; we derive from movements."""
        # The last movement per account type usually has a running balance
        # in raw_data, but this is not guaranteed. For now, return empty.
        # Balance tracking will be handled via the planning page's manual input
        # or future enhancements to fintself.
        return []
