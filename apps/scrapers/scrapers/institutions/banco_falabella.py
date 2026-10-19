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

Scope: three CLP products, each a singleton of its own existing kind (no
`external_ref`; the writer keys on institution, kind and currency, so they
never collide and the card keeps matching the row it wrote before the other
two existed):

  * CMR Mastercard, `credit_card`. The dashboard product card gives the cupo
    figures (`available`/`limit`/`owed`), and the card-detail shadow-DOM table
    gives the movements: the unbilled "Ultimos movimientos" tab and the current
    billed "Movimientos facturados" statement. Older statements (the tab's
    month select) are not iterated yet: a daily run captures each movement
    while it is unbilled and again while it is the current statement, so only
    pre-existing history is left out.
  * Cuenta Corriente, `checking`. The dashboard card gives "Saldo disponible"
    (`balance`) and "Saldo contable" (`accounting_balance`); the account number
    goes in attributes for display only. Movements come from the detail's
    default "Ultimos movimientos" table (Cargo is money out, Abono money in);
    older months in its select are not read, as on the card.
  * Linea de Credito, `line_of_credit`. The dashboard card gives "Cupo
    autorizado" (`limit`), "Saldo utilizado" (`owed`) and "Saldo disponible"
    (`available`); the detail adds the "Tasa diaria", kept as quoted in
    `daily_interest_rate_pct`. Balance only: its movements view errored live
    and lists draws that also reach the checking account.

Session: one browser drive per cycle feeds both legs, all three products
included. The transactions leg runs
first (`run_scraper`'s order), drives the session once and caches the result or
the error; the products leg serves the cached result, or re-raises the cached
error without logging in again. There is no session cache between runs: the
Falabella session is short-lived, so a daily full login is the whole cost.

Identity: neither surface exposes a movement id, so each card purchase is
emitted once at its full "Monto total" under a fingerprint of its immutable
fields (`external_id_for`), and each checking movement under one that adds the
running balance (`checking_external_id_for`); both document their collision
limit. The checking view is a legacy server-rendered page, so the decrypted
JSON a `JSON.parse` hook can see never carries its rows.

Verified live: the headless login, both legs, the writer invariants, a card
payment's sign (printed "-$..." on the unbilled tab, emitted as money in), and
the checking and línea figures and an incoming transfer's sign against the
bank's own movements export. UNVERIFIED: whether a per-row id exists in the
decrypted card BFF payload that would make a sturdier key than the
fingerprint, and the checking table's shape for an outgoing movement (only an
Abono row has been seen).

Set `BANCO_FALABELLA_HEADLESS=0` to watch the browser while debugging.
"""

import hashlib
import logging
import os
from datetime import date

from product_model import (
    CheckingAttributes,
    CheckingMetrics,
    CreditCardAttributes,
    CreditCardMetrics,
    LineOfCreditAttributes,
    LineOfCreditMetrics,
)
from scrapers.backends.banco_falabella_web import (
    FalabellaSession,
    RawBalance,
    RawCheckingBalance,
    RawCheckingMovement,
    RawLineBalance,
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
CHECKING_KIND = "checking"
CHECKING_NAME = "Cuenta Corriente"
LINE_KIND = "line_of_credit"
LINE_NAME = "Línea de Crédito"
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


def checking_external_id_for(movement: RawCheckingMovement) -> str:
    """The `external_id` of one Cuenta Corriente movement.

    ``bfal_cc_<sha1(date|amount|currency|balance)[:16]>`` over the occurrence
    date, the signed amount as an integer, the currency and the running
    "Saldo" after the movement (empty when the cell was blank). The table and
    its legacy view carry no movement id (the date's sort key has no time of
    day either), and the description is left out like on the card leg.

    The running balance is what tells apart two same-day movements of the same
    amount and direction, since the ledger moves between them. Collision limit:
    such a pair only collapses when the balance returns to the same figure in
    between (for example, an incoming transfer, an equal charge, then a second
    equal transfer, all on one day).

    Re-key risk: the key is only as stable as the printed running balance. If
    the bank re-sorts same-day rows (a movement booked earlier listed after a
    later one), or a hold or reversal changes the Saldo printed on an existing
    row, that movement hashes to a new key and is stored a second time.
    UNVERIFIED as well: whether a movement booked after the cut-off hour is
    first listed under one date and later re-dated to the next business day,
    which would re-key it the same way.
    """
    amount_int = int(movement.amount)
    balance = "" if movement.balance is None else str(int(movement.balance))
    raw = f"{movement.transaction_date.isoformat()}|{amount_int}|{movement.currency}|{balance}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"bfal_cc_{digest}"


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


def checking_to_product(checking: RawCheckingBalance) -> ScrapedProduct:
    """The singleton CLP checking product for the parsed Cuenta Corriente card.

    `balance` is the "Saldo disponible" and `accounting_balance` the "Saldo
    contable". The account number goes in attributes only (display); it never
    names or keys the product. Requires a parsed `available`.
    """
    return ScrapedProduct(
        institution=BancoFalabellaScraper.institution,
        kind=CHECKING_KIND,
        currency="CLP",
        external_ref=None,
        name=CHECKING_NAME,
        attributes=CheckingAttributes(account_number=checking.account_number),
        metrics=CheckingMetrics(
            balance=int(checking.available),
            accounting_balance=_optional_int(checking.accounting),
        ),
    )


def line_to_product(line: RawLineBalance) -> ScrapedProduct:
    """The singleton CLP line_of_credit product for the parsed Línea de Crédito.

    `available` is the "Saldo disponible", `limit` the "Cupo autorizado" and
    `owed` the "Saldo utilizado". The daily rate rides in attributes as quoted;
    when the detail was not read it stays None, which the attributes merge
    reads as "keep the stored value". Requires a parsed `available`.
    """
    return ScrapedProduct(
        institution=BancoFalabellaScraper.institution,
        kind=LINE_KIND,
        currency="CLP",
        external_ref=None,
        name=LINE_NAME,
        attributes=LineOfCreditAttributes(
            daily_interest_rate_pct=(
                float(line.daily_rate_pct) if line.daily_rate_pct is not None else None
            ),
        ),
        metrics=LineOfCreditMetrics(
            available=int(line.available),
            limit=_optional_int(line.limit),
            owed=_optional_int(line.owed),
        ),
    )


def products_for(session: FalabellaSession) -> list[ScrapedProduct]:
    """Every product one session read, each a singleton of its own kind.

    The writer keys a product on (institution, kind, currency, external_ref),
    so the CMR card, the checking account and the línea never share a row and
    all three keep `external_ref` None: the card goes on matching the row it
    has always written to, and each leg's transactions land on their kind's
    singleton.
    """
    products: list[ScrapedProduct] = []
    if session.balance is not None:
        products.append(balance_to_product(session.balance))
    if session.checking is not None and session.checking.available is not None:
        products.append(checking_to_product(session.checking))
    if session.line is not None and session.line.available is not None:
        products.append(line_to_product(session.line))
    return products


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

    def _checking_movement_to_transaction(
        self, movement: RawCheckingMovement
    ) -> ScrapedTransaction:
        """One checking movement as a transaction on the singleton checking product.

        The table prints one date per movement, so `accounting_date` is left
        NULL like on the card leg. See `checking_external_id_for` for the key.
        """
        tx_date = movement.transaction_date
        return ScrapedTransaction(
            institution=self.institution,
            product_kind=CHECKING_KIND,
            description=movement.description,
            amount=movement.amount,
            currency=movement.currency,
            transaction_date=tx_date,
            external_id=checking_external_id_for(movement),
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
        converters = [(self._movement_to_transaction, m) for m in result.movements] + [
            (self._checking_movement_to_transaction, m) for m in result.checking_movements
        ]
        for convert, movement in converters:
            try:
                transaction = convert(movement)
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
            # documented same-day/same-amount collisions would hide.
            logger.debug("Banco Falabella: %d repeated movement ids folded", repeated)
        checking = sum(1 for t in transactions if t.product_kind == CHECKING_KIND)
        logger.info(
            "Banco Falabella: %d credit card, %d checking transactions",
            len(transactions) - checking,
            checking,
        )
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
        return ProductScrapeResult(products_for(result), list(result.warnings))
