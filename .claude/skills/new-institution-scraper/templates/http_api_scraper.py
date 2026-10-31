"""Acme scraper: <one line saying what the institution is and what we read>.

TEMPLATE. Copy to `apps/scrapers/scrapers/institutions/<slug>.py`, rename
Acme/acme/ACME, and delete every part your institution doesn't need. It is
shaped for rungs 1 and 3 of the acquisition ladder (an official API with a
user-issued token, or a private JSON API). For a cached web session and 2FA,
graft in the session methods of `institutions/fintual.py`.

Acquisition decision (keep this paragraph, rewritten for the real case):
balances and movements come from Acme's <official / private> JSON API over
httpx. <Why the higher rungs were rejected, in a sentence each.> Auth is a
personal API token (`ACME_API_TOKEN`, held in the secret store); a rejected token
raises `AcmeAuthError` naming the fix.

`scrape_products()` emits the CLP checking account as one `checking` product;
`scrape_transactions()` maps its movements, newest first, up to MAX_PAGES
pages. <Unverified live: list what is assumed.>

The product is a singleton (no `external_ref`) because transactions carry no
product reference: the writer files them on the product with no
`external_ref` for their (institution, kind, currency). An institution with
several accounts of one kind and currency can't have per-account movements
until the ingest contract grows a product reference.
"""

import logging
import os
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from product_model import CheckingMetrics

from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)

logger = logging.getLogger(__name__)

ACME_BASE = "https://api.acme.example"
LOCAL_TZ = ZoneInfo("America/Santiago")

# Both legs file under this kind and currency, so a movement lands on the same
# product as the balance (the writer resolves by kind and currency).
PRODUCT_KIND = "checking"
CURRENCY = "CLP"
ID_PREFIX = "acme_"
PAGE_SIZE = 50
# Past this many pages per account the rest are left out, with a warning.
MAX_PAGES = 20


class AcmeAuthError(RuntimeError):
    """Raised when Acme rejects the token; the message names the fix."""


def _to_clp(raw: Any) -> int | None:
    """Whole CLP from a JSON number or numeric string ("1234.5"), half up; else None.

    API payloads use machine formats. Display text such as "$ 1.234.567" needs
    a Chilean-format parser instead (`_to_clp` in `backends/email.py`).
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _local_date(raw: Any) -> date | None:
    """The Santiago calendar date of an ISO timestamp, or None if unreadable.

    UTC timestamps must be converted before taking the date, or an evening
    movement lands on the next day.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(LOCAL_TZ).date()


def _parse_movement(item: dict) -> ScrapedTransaction | None:
    """One Acme movement as a transaction, or None to skip it.

    `external_id` is Acme's own movement id, which stays the same across
    logins. Never derive it from the description: institutions rewrite
    descriptions after posting.
    """
    movement_id = item.get("id")
    if not movement_id:
        logger.warning("Acme movement without id, skipping")
        return None
    currency = str(item.get("currency") or CURRENCY).upper()
    if currency != CURRENCY:
        logger.warning("Acme movement %s is in %s, not CLP; skipping", movement_id, currency)
        return None
    amount = _to_clp(item.get("amount"))
    if not amount:
        return None
    tx_date = _local_date(item.get("booked_at"))
    if tx_date is None:
        logger.warning("Acme movement %s has no readable date, skipping", movement_id)
        return None
    direction = item.get("direction")
    if direction not in ("debit", "credit"):
        logger.warning("Acme movement %s has no known direction, skipping", movement_id)
        return None
    amount = -abs(amount) if direction == "debit" else abs(amount)

    return ScrapedTransaction(
        institution="acme",
        product_kind=PRODUCT_KIND,
        currency=CURRENCY,
        description=str(item.get("description") or "Acme movement"),
        amount=amount,
        transaction_date=tx_date,
        external_id=f"{ID_PREFIX}{movement_id}",
        # The Expenses page filters on it; a deferred charge would use its billing month.
        scheduled_month=date(tx_date.year, tx_date.month, 1),
    )


def _parse_account(item: dict) -> ScrapedProduct | None:
    """The Acme account as a checking observation, or None if unusable."""
    balance = _to_clp(item.get("available_balance"))
    currency = str(item.get("currency") or CURRENCY).upper()
    if balance is None or currency != CURRENCY:
        return None
    return ScrapedProduct(
        institution="acme",
        kind=PRODUCT_KIND,
        currency=CURRENCY,
        name="Cuenta Acme",
        metrics=CheckingMetrics(balance=balance),
    )


class AcmeScraper(BaseScraper):
    method = "http_api"
    institution = "acme"

    def __init__(self) -> None:
        self.token = os.environ["ACME_API_TOKEN"]
        # `scrape_transactions` returns a bare list, so its partial-coverage
        # warnings ride out with the products leg, which runs next.
        self._warnings: list[str] = []

    def _client(self) -> httpx.AsyncClient:
        """The one place the HTTP client is built, so tests can swap its transport."""
        return httpx.AsyncClient(
            base_url=ACME_BASE,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, what: str, params: dict | None = None
    ) -> dict:
        """GET a JSON object, raising AcmeAuthError on 401/403.

        Error messages land in `scraper_runs.error_message` and on the
        dashboard, so they name `what` was being read and the status code only:
        never the token, the URL (it can carry ids) or the response body.
        """
        resp = await client.get(path, params=params)
        if resp.status_code in (401, 403):
            raise AcmeAuthError(
                f"Acme rejected the API token ({resp.status_code}). "
                "Issue a new one and run `make secret-set KEY=ACME_API_TOKEN`."
            )
        if resp.is_error:
            raise RuntimeError(f"Acme answered {resp.status_code} reading {what}")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise RuntimeError(f"Acme sent an unexpected payload reading {what}")
        return data

    async def _movement_pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[dict]]:
        """Yield each page of movements, newest first.

        Stops at the last page Acme reports, else at the first short page.
        """
        for page in range(1, MAX_PAGES + 1):
            data = await self._get_json(
                client,
                "/v1/account/movements",
                "movements",
                {"page": page, "per_page": PAGE_SIZE},
            )
            items = [m for m in data.get("movements") or [] if isinstance(m, dict)]
            yield items
            total_pages = (data.get("meta") or {}).get("total_pages")
            if isinstance(total_pages, int):
                if page >= total_pages:
                    return
            elif len(items) < PAGE_SIZE:
                return
        logger.warning("Acme: stopped after %d pages; older movements not fetched", MAX_PAGES)
        self._warnings.append(f"Acme: movements capped at {MAX_PAGES} pages")

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """The account's CLP movements, up to MAX_PAGES pages.

        A failure on a later page keeps the pages already read and leaves a
        warning; a failure before anything was read (including any auth
        failure) fails the leg.
        """
        self._warnings = []
        transactions: list[ScrapedTransaction] = []
        async with self._client() as client:
            try:
                async for items in self._movement_pages(client):
                    for item in items:
                        txn = _parse_movement(item)
                        if txn is not None:
                            transactions.append(txn)
            except AcmeAuthError:
                raise
            except (httpx.HTTPError, RuntimeError) as e:
                if not transactions:
                    raise
                logger.warning("Acme: movements stopped early, keeping what was read: %s", e)
                self._warnings.append(f"Acme: movements stopped early ({e})")
        logger.info("Acme: %d transactions", len(transactions))
        return transactions

    async def scrape_products(self) -> ProductScrapeResult:
        """The checking account's balance, plus any warnings either leg raised."""
        async with self._client() as client:
            data = await self._get_json(client, "/v1/account", "the account")
        # Taken only after the fetch, so a failing fetch can't lose them.
        warnings, self._warnings = self._warnings, []
        product = _parse_account(data)
        if product is None:
            return ProductScrapeResult([], [*warnings, "Acme: account without a CLP balance"])
        return ProductScrapeResult([product], warnings)
