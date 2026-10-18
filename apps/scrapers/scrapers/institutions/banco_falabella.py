"""Banco Falabella scraper: headless login, balances and movements from the DOM.

Decision record (condensed). Rung 6 of the acquisition ladder for both legs: a
headless Playwright login, then the figures read from the rendered DOM
(`backends/banco_falabella_web.py`). Higher rungs rejected:

  1. Official API: Banco Falabella offers none to individuals. Chile's Open
     Finance system (Ley 21.521) is not operating yet.
  2. Maintained library: none covers Banco Falabella with stable movement ids.
  3. Private API over plain HTTP: the data BFF (`/movement-cc/*`) encrypts its
     request bodies client-side (RSA + AES), so an HTTP client cannot ask it for
     anything. The data is read from the rendered page instead.
  5. E-mail notifications: not used here (the card leg needs balances too).

Scope: one product, a CMR Mastercard credit card in CLP, modelled as the
existing `credit_card` kind and emitted as a singleton (no `external_ref`) so
its movements land on it. The dashboard product card gives the cupo figures
(`available`/`limit`/`owed`), and the card-detail shadow-DOM table gives the
movements: the unbilled "Ultimos movimientos" tab and the current billed
"Movimientos facturados" statement. Older statements (the tab's month select)
are not iterated yet: a daily run captures each movement while it is unbilled
and again while it is the current statement, so only pre-existing history is
left out.

Session: one browser drive per cycle feeds both legs. The transactions leg runs
first (`run_scraper`'s order), drives the session once and caches the result or
the error; the products leg serves the cached result, or re-raises the cached
error without logging in again. There is no session cache between runs: the
Falabella session is short-lived, so a daily full login is the whole cost.

Identity: the DOM exposes no movement id, so each purchase is emitted once at
its full "Monto total" under a fingerprint of its immutable fields; see
`external_id_for` for the scheme and its collision limit.

Verified live: the headless login, both legs and the writer invariants.
UNVERIFIED: the payment/credit sign heuristic (no payment row was seen live),
and whether a per-row id exists in the decrypted BFF payload that would make a
sturdier key than the fingerprint.

Set `BANCO_FALABELLA_HEADLESS=0` to watch the browser while debugging.
"""

import hashlib
import logging
import os
from datetime import date

from product_model import CreditCardAttributes, CreditCardMetrics
from scrapers.backends.banco_falabella_web import (
    FalabellaSession,
    RawBalance,
    RawMovement,
    fetch_session,
)
from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)

logger = logging.getLogger(__name__)

PRODUCT_KIND = "credit_card"
PRODUCT_NAME = "CMR Mastercard"
_FALSE_VALUES = {"0", "false", "no", "off"}


def headless_from_env() -> bool:
    """`BANCO_FALABELLA_HEADLESS`: headless unless set to 0/false/no/off."""
    value = os.environ.get("BANCO_FALABELLA_HEADLESS", "").strip().lower()
    return value not in _FALSE_VALUES


def external_id_for(movement: RawMovement) -> str:
    """The `external_id` of one Banco Falabella movement.

    ``bfal_<sha1(date|amount|currency|persona)[:16]>`` over immutable fields
    only: the occurrence date, the signed full "Monto total" as an integer, the
    currency and the persona (Titular / Adicional). Description, the installment
    counter, the section label and the billing month are deliberately left out,
    because the portal rewrites them when a purchase is billed. This makes a
    multi-installment purchase (which repeats across months with the same date,
    Monto total and persona) and a movement seen in both the unbilled and billed
    tabs dedup to ONE row, recorded once at the full amount.

    Collision limit: two genuinely distinct charges on the same day, for the
    same amount and the same persona, collapse into one row. Without a
    per-movement id in the DOM this is unavoidable, and it mirrors the BCI Lider
    and BanChile fingerprint schemes.
    """
    amount_int = int(movement.amount)
    raw = f"{movement.transaction_date.isoformat()}|{amount_int}|{movement.currency}|{movement.persona}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"bfal_{digest}"


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


def balance_to_product(balance: RawBalance) -> ScrapedProduct:
    """The singleton CLP CMR Mastercard product for one parsed balance.

    `available` is required (the headline); `limit` and `owed` ride along when
    they parsed. The masked `last4` and the brand go in attributes (statement
    and due days are not read yet, so they stay None).
    """
    return ScrapedProduct(
        institution=BancoFalabellaScraper.institution,
        kind=PRODUCT_KIND,
        currency="CLP",
        external_ref=None,
        name=balance.name or PRODUCT_NAME,
        attributes=CreditCardAttributes(
            last4=balance.last4,
            brand=balance.brand,
            statement_day=balance.statement_day,
            due_day=balance.due_day,
        ),
        metrics=CreditCardMetrics(
            available=int(balance.available),
            limit=_optional_int(balance.limit),
            owed=_optional_int(balance.owed),
            reported_as_of=None,
        ),
    )


class BancoFalabellaScraper(BaseScraper):
    method = "web"
    institution = "banco_falabella"

    def __init__(self) -> None:
        self.rut = os.environ["BANCO_FALABELLA_RUT"]
        self.password = os.environ["BANCO_FALABELLA_PASSWORD"]
        self.headless = headless_from_env()
        # One browser drive per cycle (see module docstring).
        self._cached: FalabellaSession | None = None
        self._error: Exception | None = None

    def _movement_to_transaction(self, movement: RawMovement) -> ScrapedTransaction:
        """One movement as a transaction on the singleton CMR card.

        `amount` is the signed full "Monto total" (money out negative) and
        `scheduled_month` the first of the occurrence month. The card reports no
        separate posting date, so `accounting_date` is left NULL. See
        `external_id_for` for the identity and its collision limit.
        """
        tx_date = movement.transaction_date
        return ScrapedTransaction(
            institution=self.institution,
            product_kind=PRODUCT_KIND,
            description=movement.description,
            amount=movement.amount,
            currency=movement.currency,
            transaction_date=tx_date,
            external_id=external_id_for(movement),
            scheduled_month=date(tx_date.year, tx_date.month, 1),
        )

    async def _drive_once(self) -> FalabellaSession:
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
            logger.error("Banco Falabella scrape failed: %s", type(exc).__name__)
            self._error = exc
            raise
        self._cached = result
        return result

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        result = await self._drive_once()
        transactions: list[ScrapedTransaction] = []
        seen: set[str] = set()
        failed = 0
        repeated = 0
        for movement in result.movements:
            try:
                transaction = self._movement_to_transaction(movement)
            except Exception:
                failed += 1
                continue
            if transaction.external_id in seen:
                repeated += 1
                continue
            seen.add(transaction.external_id)
            transactions.append(transaction)
        if failed:
            logger.warning("Banco Falabella: %d movements could not be converted", failed)
        if repeated:
            # Expected when a purchase shows in both tabs; also where the
            # documented same-day/same-amount/same-persona collision would hide.
            logger.debug("Banco Falabella: %d repeated movement ids folded", repeated)
        logger.info("Banco Falabella: %d credit card transactions", len(transactions))
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
        products = [balance_to_product(result.balance)] if result.balance else []
        return ProductScrapeResult(products, list(result.warnings))
