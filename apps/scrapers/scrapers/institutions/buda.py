"""Buda.com REST API scraper.

Auth: HMAC-SHA384 signing.
  Signature string: "{METHOD} {path} {base64_body} {nonce}"
  Headers: X-SBTC-APIKEY, X-SBTC-NONCE, X-SBTC-SIGNATURE

Balances cover every wallet, one `crypto` product per currency. Transactions
cover CLP deposits and withdrawals only: `transactions.amount` is integer CLP,
and a crypto movement has no CLP amount without a price at its date, so crypto
value history comes from the balance snapshots instead (issue #3).
"""

import base64
import hashlib
import hmac
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

from product_model import CryptoMetrics

from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)
from scrapers.retry import send_with_retry

logger = logging.getLogger(__name__)

BUDA_BASE = "https://www.buda.com"

# Both legs file under this kind, so a CLP movement lands on the same product
# as the CLP balance (the writer resolves products by kind and currency).
PRODUCT_KIND = "crypto"
TRANSACTION_CURRENCY = "CLP"
PAGE_SIZE = 50
# 1000 movements per direction; past that the rest are left out, with a warning.
MAX_PAGES = 20
# Movements that never moved money. Both spellings of "annulled" are listed
# because Buda's own (thought to be "anulled") is not verified live.
FAILED_STATES = frozenset({"anulled", "annulled", "rejected", "cancelled", "canceled"})


def _parse_movement(item: dict, tx_type: str) -> ScrapedTransaction | None:
    """One Buda deposit or withdrawal as a CLP transaction, or None to skip it.

    A movement in any currency other than CLP is skipped with a warning rather
    than truncated to whole units and filed under the CLP product. An amount
    with no currency marker is read as CLP, the currency of the endpoint it
    came from. Annulled or rejected movements are skipped too.
    """
    direction = tx_type.removesuffix("s")
    item_id = item.get("id")
    if not item_id:
        logger.warning("Buda %s missing id, skipping", direction)
        return None
    state = str(item.get("state") or "").lower()
    if state in FAILED_STATES:
        logger.info("Buda %s %s is %s, skipping", direction, item_id, state)
        return None

    amount = item.get("amount")
    if not isinstance(amount, list) or not amount:
        logger.warning("Buda %s %s has no amount, skipping", direction, item_id)
        return None
    currency = str(
        amount[1] if len(amount) > 1 else item.get("currency") or TRANSACTION_CURRENCY
    ).upper()
    if currency != TRANSACTION_CURRENCY:
        logger.warning(
            "Buda %s %s is in %s, not CLP; skipping", direction, item_id, currency
        )
        return None

    try:
        value = Decimal(str(amount[0]))
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite():
        logger.warning(
            "Buda %s %s has an unreadable amount, skipping", direction, item_id
        )
        return None
    pesos = abs(int(value.to_integral_value(rounding=ROUND_HALF_UP)))
    if pesos == 0:
        return None
    if tx_type == "withdrawals":
        pesos = -pesos

    created = item.get("created_at", "")
    tx_date = date.today()
    if created:
        try:
            tx_date = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
        except ValueError:
            pass

    return ScrapedTransaction(
        institution="buda",
        product_kind=PRODUCT_KIND,
        currency=TRANSACTION_CURRENCY,
        description=f"Buda {direction} {TRANSACTION_CURRENCY}",
        amount=pesos,
        transaction_date=tx_date,
        external_id=f"buda_{item_id}",
        scheduled_month=date(tx_date.year, tx_date.month, 1),
    )


class BudaScraper(BaseScraper):
    method = "http_api"
    institution = "buda"

    def __init__(self) -> None:
        self.api_key = os.environ["BUDA_API_KEY"]
        self.api_secret = os.environ["BUDA_API_SECRET"]

    def _sign(self, request_method: str, path: str, body: str = "") -> dict[str, str]:
        """Generate HMAC-SHA384 auth headers for a request.

        `path` must be the exact path sent to Buda, including the `.json`
        suffix and any query string — otherwise the signature is rejected.
        The body component is omitted entirely for body-less requests.
        """
        nonce = str(int(time.time() * 1e6))
        components = [request_method, path]
        if body:
            components.append(base64.b64encode(body.encode()).decode())
        components.append(nonce)
        msg = " ".join(components)

        signature = hmac.new(
            self.api_secret.encode(),
            msg.encode(),
            hashlib.sha384,
        ).hexdigest()

        return {
            "X-SBTC-APIKEY": self.api_key,
            "X-SBTC-NONCE": nonce,
            "X-SBTC-SIGNATURE": signature,
        }

    async def _get(self, client: httpx.AsyncClient, path: str) -> httpx.Response:
        """Signed GET with bounded retries; each attempt is re-signed, since
        Buda rejects a nonce it has already seen."""
        return await send_with_retry(
            lambda: client.get(f"{BUDA_BASE}{path}", headers=self._sign("GET", path)),
            label=f"Buda GET {path}",
        )

    async def _pages(
        self, client: httpx.AsyncClient, tx_type: str
    ) -> AsyncIterator[list[dict]]:
        """Yield each page of CLP deposits or withdrawals, in Buda's order.

        Follows `meta.total_pages` when Buda reports it, else stops at the
        first short page. Pages already yielded survive a later page failing.
        Each page is fetched with bounded retries; every attempt is re-signed,
        since Buda rejects a nonce it has already seen.
        """
        currency = TRANSACTION_CURRENCY.lower()
        for page in range(1, MAX_PAGES + 1):
            path = (
                f"/api/v2/currencies/{currency}/{tx_type}.json"
                f"?per={PAGE_SIZE}&page={page}"
            )
            resp = await send_with_retry(
                lambda p=path: client.get(
                    f"{BUDA_BASE}{p}", headers=self._sign("GET", p)
                ),
                label=f"Buda GET {path}",
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get(tx_type) or []
            yield items

            total_pages = (data.get("meta") or {}).get("total_pages")
            if isinstance(total_pages, int):
                if page >= total_pages:
                    return
            elif len(items) < PAGE_SIZE:
                return
        logger.warning(
            "Buda %s: stopped after %d pages; the rest were not fetched",
            tx_type,
            MAX_PAGES,
        )

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """Fetch CLP deposits and withdrawals, up to MAX_PAGES pages each."""
        transactions: list[ScrapedTransaction] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for tx_type in ("deposits", "withdrawals"):
                try:
                    async for items in self._pages(client, tx_type):
                        for item in items:
                            txn = _parse_movement(item, tx_type)
                            if txn is not None:
                                transactions.append(txn)
                except httpx.HTTPStatusError as e:
                    logger.warning("Buda %s failed: %s", tx_type, e)
                except Exception:
                    logger.exception("Buda %s error", tx_type)

        return transactions

    async def scrape_products(self) -> ProductScrapeResult:
        """Fetch all currency balances as crypto products."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await self._get(client, "/api/v2/balances.json")
            resp.raise_for_status()

            products: list[ScrapedProduct] = []
            for bal in resp.json().get("balances", []):
                currency_id = bal.get("id", "")
                available = bal.get("available_amount", ["0"])
                # Keep fractional amounts: 0.5 BTC must not truncate to 0.
                amount = float(available[0])

                if amount > 0:
                    logger.info("Buda balance %s: %s", currency_id, f"{amount:,}")
                    products.append(
                        ScrapedProduct(
                            institution="buda",
                            kind=PRODUCT_KIND,
                            # One product per currency (BTC, CLP, ...)
                            currency=currency_id.upper() or "CLP",
                            metrics=CryptoMetrics(units=amount),
                        )
                    )

            return ProductScrapeResult(products)
