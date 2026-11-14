"""Mercado Pago (Chile) scraper: a real Chrome over CDP, read by fetch only.

Acquisition decision (rung numbers are the acquisition ladder's):

- Legs: balances (the `wallet` and one `savings` product per pot) and
  transactions (the wallet ledger). All three come from rung 7.
- Rung 1, official API: rejected for now. The account balance endpoint is
  absent from the Chilean API reference and is reported to answer 403 to
  personal tokens. The Reports API (`release_report`) may carry a ledger with
  the yield rows, but that is unverified, needs the user to create a
  developer application, and covers neither the savings pots nor crypto. It
  is the possible later climb for the wallet ledger.
- Rung 2: no library covers Mercado Pago Chile. Open finance (SFA) is
  postponed to 2027 or later, and aggregators do not expose Mercado Pago
  consumer data.
- Rungs 3, 5 and 6: the login cannot be automated (a reCAPTCHA on the e-mail
  step, a step-up on the password step), so there is no plain-HTTP and no
  headless login.
- Rung 4, e-mail (the previous technique): transactions only, heuristic, no
  balance, and it also captured purchases where Mercado Pago was only the
  payment gateway for another card.
- Rung 7, chosen: a dedicated real-Chrome profile a human signed in to
  (`make mercadopago-login`), driven over CDP. The profile keeps the session
  across relaunches, off-screen ones included, and every page used here
  reads by same-origin fetch without navigating.
- Out of scope: crypto (Meli Dólar, Bitcoin). On the web it only shows on
  `/home`, which redirects through the auth gateway, and the units exist only
  in the mobile app.

Hard contract (`backends/mercadopago_web.py` enforces it): never navigate to
or fetch `/home`, never follow a redirect, never retry a lost session. A
redirect means the session is gone and a human must run
`make mercadopago-login`.

What each leg returns, both from ONE Chrome drive per cycle (the
transactions leg drives and caches the result or the error; the products leg
consumes it, re-raising a cached error without driving again):

- `scrape_transactions()`: the wallet ledger (`/banking/balance/movements`),
  the newest `MERCADOPAGO_MOVEMENT_PAGES` pages of 100 rows (default 1; 0
  reads every page). Rows land on the singleton `wallet` product, keyed
  `mp_<movement_id>`. The ledger's sign is kept (negative is money out) and
  its UTC timestamp becomes an America/Santiago date. Daily yield and
  cashback rows are ordinary income rows.
- `scrape_products()`: the singleton CLP `wallet` with its balance, and one
  CLP `savings` product per pot, told apart by `external_ref`
  (`mp_pot_<uuid>`) and named after the pot. Pots have no movements on the
  web, so no `savings` transaction is ever emitted (one without a ref would
  create a roll-up product). A pot that disappears from the page keeps its
  last balance: an absent pot cannot be told from a failed read.

If the pots page or a ledger page fails after the balance was read, or a pot
or a ledger row cannot be read, the run ends `partial` with a warning instead
of quietly storing less.

Verified live (managed mode, off-screen, a real signed-in profile):

- a whole run end to end: the launch, reattaching until the tab shows, both
  legs and the teardown. The in-page read through `wait_for_function` works
  under the portal's content security policy;
- the products: the ref-less CLP wallet and a `savings` pot with its ref and
  name, both balances equal to what the account shows;
- the ledger: the default single page and the whole history
  (`MERCADOPAGO_MOVEMENT_PAGES=0`, several pages) read without duplicates or
  skipped rows, in whole CLP amounts, every row with its `scheduled_month`,
  and as many rows as the page reports in total;
- `movement_id` is the same across separate Chrome launches of one signed-in
  profile (every id of a first dry run reappears in a second one);
- the session survives repeated launch and SIGTERM teardown cycles of the
  managed Chrome;
- the stale connection that shows the tab with an empty URL is real, and
  reattaching resolves it.

Not verified live:

- `make mercadopago-login` against a live window (no fresh sign-in was
  performed), and where the portal lands the tab after signing in;
- that `movement_id` is the same across separate sign-ins (only across
  launches of one sign-in);
- how a lost session presents (the tab bounced off the portal, or a redirect
  on the fetch): both paths are handled, neither was observed;
- how long a sign-in lasts;
- which ledger rows are movements between the user's own products (none is
  flagged `internal_transfer`);
- reuse mode (`MERCADOPAGO_CDP_URL`) against a live Chrome.

The e-mail leg this replaces keyed its rows `email_mercadopago_<digest>`. The
same movements now arrive from the ledger under `mp_` keys, so the old rows
stay in the database until they are removed by hand: nothing here deletes
them.
"""

import datetime
import logging
import os
import sys

from product_model import SavingsMetrics, WalletMetrics

from scrapers.backends.mercadopago_web import (
    DEFAULT_MOVEMENT_PAGES,
    MercadoPagoAccount,
    MercadoPagoSessionError,
    MercadoPagoWebError,
    Movement,
    save_login_session,
    scrape_account,
)
from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)

logger = logging.getLogger(__name__)

MOVEMENT_PAGES_ENV = "MERCADOPAGO_MOVEMENT_PAGES"
# The wallet is the singleton the transactions land on; both legs must agree.
WALLET_KIND = "wallet"
POT_KIND = "savings"
CURRENCY = "CLP"


def pot_ref(pot_id: str) -> str:
    """The `external_ref` of the savings product of the pot `pot_id` (a uuid)."""
    return f"mp_pot_{pot_id}"


def movement_external_id(movement_id: str) -> str:
    """A ledger row's dedup key: the ledger's own per-row id, prefixed."""
    return f"mp_{movement_id}"


def movement_description(title: str, description: str) -> str:
    """A readable description from a ledger row's title and description.

    The title is the merchant or, for a transfer between people, the
    counterparty; the description is the movement type and may be empty.
    """
    if not description or description.casefold() == title.casefold():
        return title
    return f"{title} ({description})"


def _movement_pages() -> int:
    """`MERCADOPAGO_MOVEMENT_PAGES`: ledger pages per run (0 reads them all)."""
    raw = os.environ.get(MOVEMENT_PAGES_ENV, "").strip()
    if not raw:
        return DEFAULT_MOVEMENT_PAGES
    try:
        pages = int(raw)
    except ValueError:
        pages = -1
    if pages < 0:
        logger.warning(
            "%s must be a whole number of pages (0 reads all); using %d",
            MOVEMENT_PAGES_ENV,
            DEFAULT_MOVEMENT_PAGES,
        )
        return DEFAULT_MOVEMENT_PAGES
    return pages


class MercadoPagoScraper(BaseScraper):
    method = "web"
    institution = "mercadopago"

    def __init__(self) -> None:
        self.movement_pages = _movement_pages()
        # One Chrome drive feeds both legs (see the module docstring): the
        # transactions leg runs first (run_scraper's order) and stashes the
        # result or the error.
        self._cached: MercadoPagoAccount | None = None
        self._error: Exception | None = None

    def _to_transaction(self, movement: Movement) -> ScrapedTransaction:
        """A ledger row as a wallet transaction.

        No `product_external_ref`: the row lands on the singleton wallet, the
        same product the balance leg emits.
        """
        day = movement.date
        return ScrapedTransaction(
            institution=self.institution,
            product_kind=WALLET_KIND,
            description=movement_description(movement.title, movement.description),
            amount=movement.amount,
            transaction_date=day,
            external_id=movement_external_id(movement.movement_id),
            currency=CURRENCY,
            scheduled_month=datetime.date(day.year, day.month, 1),
        )

    async def _drive_once(self) -> MercadoPagoAccount:
        """Drive the real Chrome once, caching the result (or error) per cycle."""
        self._cached = None
        self._error = None
        try:
            account = await scrape_account(movement_pages=self.movement_pages)
        except ImportError as exc:
            # A missing runtime dep is a broken deployment, not a clean run.
            logger.exception("Playwright not installed. Run: pip install -r requirements.txt")
            self._error = exc
            raise
        except Exception as exc:
            # MercadoPagoSessionError (a human must sign in again) and any
            # other failure land here; cached so the products leg neither
            # drives again nor retries a lost session.
            logger.exception("MercadoPago read failed")
            self._error = exc
            raise
        self._cached = account
        return account

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """The wallet ledger rows of this cycle's drive."""
        account = await self._drive_once()
        transactions: list[ScrapedTransaction] = []
        failed = 0
        for movement in account.movements:
            try:
                transactions.append(self._to_transaction(movement))
            except Exception:
                failed += 1
        if failed:
            logger.warning("MercadoPago: %d movement(s) could not be converted", failed)
            account.warnings.append(f"MercadoPago: {failed} movement(s) not converted")
        logger.info("MercadoPago: %d wallet transactions", len(transactions))
        return transactions

    async def scrape_products(self) -> ProductScrapeResult:
        """The wallet and the savings pots from the drive the transactions leg made.

        Reuses the cached result so Chrome is driven once per cycle. If that
        drive failed, its error is re-raised here without driving again. Only
        when neither leg has run yet (not the run_scraper path) does this
        drive Chrome on its own.
        """
        if self._cached is not None:
            account, self._cached = self._cached, None
            return self._products_result(account)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        account = await self._drive_once()
        self._cached = None
        return self._products_result(account)

    def _products_result(self, account: MercadoPagoAccount) -> ProductScrapeResult:
        products = [
            ScrapedProduct(
                institution=self.institution,
                kind=WALLET_KIND,
                currency=CURRENCY,
                metrics=WalletMetrics(balance=account.balance),
            )
        ]
        for pot in account.pots:
            products.append(
                ScrapedProduct(
                    institution=self.institution,
                    kind=POT_KIND,
                    currency=CURRENCY,
                    external_ref=pot_ref(pot.id),
                    name=pot.name or None,
                    metrics=SavingsMetrics(balance=pot.amount),
                )
            )
        return ProductScrapeResult(products, list(account.warnings))


def _login_cli() -> None:
    """Manual sign-in: `python -m scrapers.institutions.mercadopago` (or `make mercadopago-login`).

    Runs in the main thread (no event loop), so Ctrl-C reaches the wait and
    the Chrome window is closed on the way out.
    """
    from dotenv import load_dotenv

    load_dotenv()  # walks up to the repo-root .env for the MERCADOPAGO_* settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    try:
        save_login_session()
    except (MercadoPagoSessionError, MercadoPagoWebError) as exc:
        print(f"\nNot signed in: {exc}")
        sys.exit(1)
    print(
        "\nSigned in. The session is saved in the dedicated Chrome profile and "
        "the scraper reuses it; Chrome was closed."
    )


if __name__ == "__main__":
    _login_cli()
