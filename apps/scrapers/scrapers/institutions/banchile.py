"""Banco de Chile scraper: consumes the fintself backend."""

import hashlib
import logging
import os
from datetime import date, datetime

from scrapers.backends.banchile_web import _SURFACE_ATTEMPTS, fetch_balances
from scrapers.backends.fintself import run_fintself_scraper
from scrapers.base import BaseScraper, ProductScrapeResult, ScrapedTransaction

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

        # account_type is a plain Literal str in fintself >= 1.5 (it was an
        # enum before); getattr keeps both shapes working.
        account_type = getattr(movement.account_type, "value", movement.account_type)
        kind = "credit_card" if account_type == "credito" else "checking"

        raw_str = f"{tx_date.isoformat()}|{movement.description}|{amount_clp}|{movement.account_id or ''}"
        external_id = f"bch_{hashlib.md5(raw_str.encode()).hexdigest()[:16]}"

        scheduled = date(tx_date.year, tx_date.month, 1)

        return ScrapedTransaction(
            institution="banchile",
            product_kind=kind,
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
            # A missing runtime dep is a broken deployment, not a clean run:
            # raise so the run is recorded as `error` (and shows on the
            # dashboard) instead of silently succeeding with 0 transactions.
            logger.exception("fintself not installed. Run: pip install -r requirements.txt")
            raise
        except Exception:
            logger.exception("BancoDeChile scrape failed")
            raise

        transactions: list[ScrapedTransaction] = []
        for mov in movements:
            try:
                transactions.append(self._movement_to_transaction(mov))
            except Exception:
                logger.exception("Failed to convert movement: %s", getattr(mov, "description", "?"))

        checking = [t for t in transactions if t.product_kind == "checking"]
        credit = [t for t in transactions if t.product_kind == "credit_card"]
        logger.info(
            "BancoDeChile: %d checking, %d credit card transactions",
            len(checking),
            len(credit),
        )
        return transactions

    async def scrape_products(self) -> ProductScrapeResult:
        """Scrape BdC products via our own web session (see `banchile_web.py`).

        `fintself` (used for transactions) never exposes a balance, so this
        runs a *second*, self-contained Playwright login. It reads CLP/USD
        checking off the "Mis Productos" dashboard, plus the card total
        cupo/límite, the línea de crédito, and the depósitos a plazo
        (`term_deposit`) / fondos mutuos (`investment`) — each from their own
        detail page (see `banchile_web.py`). USD figures convert to CLP via
        lib/rates' multi-currency FX.

        It's a heavy login; a failure here is logged and reported as a warning
        rather than raised, so a flaky balance scrape can't fail the whole run
        or lose the transactions that were already imported. Surfaces that
        stayed empty after all their retries become warnings too, so the run
        records `partial` coverage instead of silently losing figures.
        APScheduler's `max_instances=1`/`coalesce` already stop back-to-back
        manual refreshes (#26) from overlapping; a cookie-session cache would
        be the next step if BdC rate-limits us (#28).
        """
        try:
            result = await fetch_balances(self.rut, self.password)
        except Exception as e:
            logger.exception("BanChile balance scrape failed")
            return ProductScrapeResult([], [f"BanChile: product scrape crashed: {e}"])
        warnings = [
            f"BanChile: {surface} surface failed after {_SURFACE_ATTEMPTS} attempts"
            for surface in result.failed_surfaces
        ]
        return ProductScrapeResult(result.products, warnings)
