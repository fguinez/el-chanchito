"""Banco de Chile scraper: consumes the fintself backend."""

import hashlib
import logging
import os
from collections import Counter
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

    def _movement_key(self, movement) -> str:
        """The identity fields fintself exposes for a movement.

        `MovementModel` carries no operation id from the bank, so date +
        description + amount + account is everything we can key on. Movements
        that share all four are indistinguishable to us (see
        `_movement_to_transaction` for how repeats are told apart).
        """
        amount_clp = int(movement.amount)
        tx_date = movement.date.date() if isinstance(movement.date, datetime) else movement.date
        return f"{tx_date.isoformat()}|{movement.description}|{amount_clp}|{movement.account_id or ''}"

    def _movement_to_transaction(self, movement, occurrence: int = 0) -> ScrapedTransaction:
        """Convert a fintself MovementModel to our ScrapedTransaction.

        `external_id` hashes the identity fields (`_movement_key`) plus, from
        the second repeat onwards, the movement's occurrence index within the
        scrape. Without that index several genuinely distinct movements sharing
        date, description, amount and account (say a handful of same-day
        transfers from the same payer for the same amount) collapse into one id,
        and `db/writer.py`'s `ON CONFLICT (product_id, external_id) DO NOTHING`
        silently drops all but the first: the dashboard shows one movement
        instead of N.

        Indexing by occurrence is stable because movements sharing every
        identity field are interchangeable: which of them gets index 2 versus 3
        doesn't matter, only that there are as many ids as movements. The first
        occurrence keeps the legacy hash on purpose, so rows imported before
        this fix still match and no re-keying migration is needed.

        Residual limitation: a movement that drops out of the bank's movements
        window and later comes back would shift the indices among its identical
        siblings. The real fix is keying on the bank's own operation id, which
        fintself 1.5 doesn't surface (`raw_data`'s `page_number`/`row_index` are
        positional and shift as new movements arrive, so they can't stand in).
        Trusting repetition also means a spurious repeat (fintself re-reading a
        movements page, say) now creates an extra row where the old collapsing
        hash swallowed it; that tradeoff is accepted, since silently dropping
        genuine movements is the worse failure.
        """
        amount_clp = int(movement.amount)
        tx_date = movement.date.date() if isinstance(movement.date, datetime) else movement.date

        # account_type is a plain Literal str in fintself >= 1.5 (it was an
        # enum before); getattr keeps both shapes working.
        account_type = getattr(movement.account_type, "value", movement.account_type)
        kind = "credit_card" if account_type == "credito" else "checking"

        raw_str = self._movement_key(movement)
        if occurrence > 0:
            raw_str += f"|#{occurrence + 1}"
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
        # How many movements with the same identity fields we've already
        # converted in this scrape; it disambiguates repeats (see
        # `_movement_to_transaction`). A movement that fails to convert doesn't
        # consume a slot, so the surviving repeats keep consecutive indices.
        seen: Counter[str] = Counter()
        for mov in movements:
            try:
                key = self._movement_key(mov)
                transactions.append(self._movement_to_transaction(mov, occurrence=seen[key]))
                seen[key] += 1
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
