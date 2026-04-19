"""Banco de Chile scraper: consumes the fintself backend."""

import hashlib
import logging
import os
from datetime import date, datetime

from scrapers.backends.fintself import run_fintself_scraper
from scrapers.base import BaseScraper, ScrapedBalance, ScrapedTransaction

logger = logging.getLogger(__name__)


class BanChileScraper(BaseScraper):
    method = "fintself"
    institution = "banchile"

    def __init__(self) -> None:
        self.rut = os.environ["BANCHILE_RUT"]
        self.password = os.environ["BANCHILE_PASSWORD"]

    def _movement_to_transaction(self, movement) -> ScrapedTransaction:
        """Convert a fintself MovementModel to our ScrapedTransaction."""
        amount_clp = int(movement.amount)
        tx_date = movement.date.date() if isinstance(movement.date, datetime) else movement.date

        acct_type = "checking"
        if movement.account_type and movement.account_type.value == "credito":
            acct_type = "credit_card"

        raw_str = f"{tx_date.isoformat()}|{movement.description}|{amount_clp}|{movement.account_id or ''}"
        external_id = f"bch_{hashlib.md5(raw_str.encode()).hexdigest()[:16]}"

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
        try:
            movements = await run_fintself_scraper(
                bank_key="cl_banco_chile",
                user=self.rut,
                password=self.password,
            )
        except ImportError:
            logger.error("fintself not installed. Run: pip install fintself")
            return []
        except Exception:
            logger.exception("BancoDeChile scrape failed")
            raise

        transactions: list[ScrapedTransaction] = []
        for mov in movements:
            try:
                transactions.append(self._movement_to_transaction(mov))
            except Exception:
                logger.exception("Failed to convert movement: %s", getattr(mov, "description", "?"))

        checking = [t for t in transactions if t.account_type == "checking"]
        credit = [t for t in transactions if t.account_type == "credit_card"]
        logger.info(
            "BancoDeChile: %d checking, %d credit card transactions",
            len(checking),
            len(credit),
        )
        return transactions

    async def scrape_balances(self) -> list[ScrapedBalance]:
        # fintself doesn't expose balances directly.
        return []
