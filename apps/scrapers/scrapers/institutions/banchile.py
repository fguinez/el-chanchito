"""Banco de Chile scraper: one Playwright session for balances and movements.

Since issue #57 this scraper owns both legs. Transactions used to come from
`fintself`, which parses the rendered movements table and therefore never sees
an operation id, so every `external_id` was a hash of date + description +
amount + account: colliding movements were silently dropped (#55) and a
reworded or re-sectioned one was imported twice (#56). We now read the
movements ourselves (`backends/banchile_movements.py`) and key them on the
bank's own ids, which also removes the second heavy login per run (#28): the
transactions leg opens the session, reads products *and* movements, and the
products leg serves its half from that cache.
"""

import hashlib
import logging
import os
import re
from datetime import date

from scrapers.backends.banchile_movements import (
    BanChileMovement,
    BanChileSessionResult,
    fetch_session,
)
from scrapers.backends.banchile_web import _SURFACE_ATTEMPTS, fetch_balances
from scrapers.base import BaseScraper, ProductScrapeResult, ScrapedTransaction

logger = logging.getLogger(__name__)

# An operation id is opaque, so it is normalised rather than interpreted: a
# cosmetic change (padding, a separator, lower case) must not re-key a movement,
# but the id stays readable in the DB instead of being hashed away.
_ID_NOISE_RE = re.compile(r"[^0-9A-Z]")


def _normalized_id(raw: str | None) -> str | None:
    """An opaque bank id, uppercased with punctuation and spacing dropped.

    None when nothing is left, so a blank id can never become a key that every
    idless movement would share.
    """
    if not raw:
        return None
    normalized = _ID_NOISE_RE.sub("", raw.strip().upper())
    return normalized or None


def external_id_for(movement: BanChileMovement) -> str:
    """The `external_id` of one BanChile movement.

    Three forms, each greppable by its prefix, and none of them derived from the
    description, the section, or the movement's position or multiplicity within
    a scrape:

    ``bch_op_<TRANSACCIONID>``
        A checking movement the bank gave an operation id for: the "ID
        Transacción" of the UI's "+" expander, read inline from `detalleGlosa`
        or from the `cartola/detalle-glosa` response. Normalised (uppercased,
        punctuation dropped) so a cosmetic drift cannot re-key it, but kept
        readable: an opaque-but-legible id is far easier to debug than a hash.
        The observed shapes are all opaque and treated strictly as strings: an
        11-digit number, ``TEF_IPE…``, ``TEFMBCO…``, a letter plus digits, and a
        ``WORD_X`` plus digits form.

    ``bch_ref_<NUMREFERENCIA>``
        A billed card row's `numReferencia` ("DDMM NNNNNNNN"), unique across a
        statement and byte-identical across two separate logins. An all-zero
        suffix means the bank has no reference for that row (observed on a
        payment) and is treated as absent by `billed_reference`, never as a
        value.

    ``bch_fp_<md5(fingerprint)[:16]>``
        The description-free fallback for everything else, hashing the leg's
        identity fields (`BanChileMovement.fingerprint`):

        * checking without an operation id, a small minority of the observed
          window (a transient 503, and movements whose inline glosa carried no
          id line): the bank's composite `id` plus `saldo`. Both are
          byte-stable across logins; the composite `id` alone is not unique
          (same-second batch credits collide) and `saldo` is a true running
          ledger balance, so together they are. Such a movement is *adopted*
          onto its ``bch_op_`` key by `db/writer.py` the first time the bank
          answers with one, instead of being imported a second time.
        * card, unbilled: posting date, authorisation date and time, amount,
          card last four and the Transbank merchant code. This leg has no id of
          any kind and its detail endpoint is unimplemented, so a charge re-keys
          itself when it is billed; the writer's adoption path is what closes
          issue #56, since the two card legs share no identity field and no
          mapping rule can bridge them.
        * card, billed without a usable reference: date, amount, card and the
          statement date.
    """
    operation_id = _normalized_id(movement.operation_id)
    if operation_id is not None:
        prefix = "bch_ref_" if movement.source == "card_billed" else "bch_op_"
        return f"{prefix}{operation_id}"
    raw = "|".join(movement.fingerprint)
    return f"bch_fp_{hashlib.md5(raw.encode()).hexdigest()[:16]}"


class BanChileScraper(BaseScraper):
    method = "web"
    institution = "banchile"

    def __init__(self) -> None:
        self.rut = os.environ["BANCHILE_RUT"]
        self.password = os.environ["BANCHILE_PASSWORD"]
        # One session feeds both legs (issue #28). `run_scraper` calls
        # transactions first: that call opens the session and stashes the
        # products half here for `scrape_products` to serve.
        self._session: BanChileSessionResult | None = None

    def _movement_to_transaction(self, movement: BanChileMovement) -> ScrapedTransaction:
        """Convert a backend movement into our ScrapedTransaction.

        `external_id` is `external_id_for`'s (see it for the whole scheme).

        `transaction_date` is when the movement HAPPENED and `accounting_date`
        is when the bank posted it, NULL where the leg reports no posting date
        (both card legs). `scheduled_month` keeps the pre-#57 convention, the
        first of the movement's own month, and so now follows the occurrence
        date. Neither date is part of any key.
        """
        tx_date = movement.transaction_date
        return ScrapedTransaction(
            institution=self.institution,
            product_kind=movement.product_kind,
            description=movement.description,
            amount=movement.amount,
            transaction_date=tx_date,
            accounting_date=movement.accounting_date,
            external_id=external_id_for(movement),
            scheduled_month=date(tx_date.year, tx_date.month, 1),
        )

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """Open the shared session and convert its movements.

        A crash here is raised so the run is recorded as `error`, and the cache
        is left empty on purpose: the products leg then opens its own
        balance-only session rather than inheriting this failure, which keeps
        the two legs independent the way `run_scraper` expects.
        """
        self._session = None
        try:
            session = await fetch_session(self.rut, self.password)
        except Exception:
            logger.exception("BanChile scrape failed")
            raise
        self._session = session

        transactions: list[ScrapedTransaction] = []
        for movement in session.movements:
            try:
                transactions.append(self._movement_to_transaction(movement))
            except Exception:
                logger.exception("Failed to convert a %s movement", movement.source)

        checking = sum(1 for t in transactions if t.product_kind == "checking")
        logger.info(
            "BanChile: %d checking, %d credit card transactions",
            checking,
            len(transactions) - checking,
        )
        return transactions

    async def scrape_products(self) -> ProductScrapeResult:
        """Serve the products the shared session already read.

        Since #57 the session is opened by the transactions leg, so the common
        path costs no extra login. If that leg never ran or crashed, this falls
        back to a balance-only session (`banchile_web.fetch_balances`), so a
        movements failure can't cost the balances the dashboard shows.

        Either way a failure here is a warning, not a raise: a flaky login must
        not fail the whole run or lose transactions already imported. Surfaces
        that stayed empty after all their retries (product *and* movement ones,
        since one session read both) become warnings too, so the run records
        `partial` coverage instead of silently losing figures.
        """
        session, self._session = self._session, None
        if session is None:
            try:
                result = await fetch_balances(self.rut, self.password)
            except Exception as e:
                logger.exception("BanChile balance scrape failed")
                return ProductScrapeResult([], [f"BanChile: product scrape crashed: {e}"])
            products, failed = result.products, result.failed_surfaces
        else:
            products, failed = session.products, session.failed_surfaces
        warnings = [
            f"BanChile: {surface} surface failed after {_SURFACE_ATTEMPTS} attempts"
            for surface in failed
        ]
        return ProductScrapeResult(products, warnings)
