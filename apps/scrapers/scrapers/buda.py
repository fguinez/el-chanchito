"""Buda.com REST API client scraper.

Auth: HMAC-SHA384 signing.
  Signature string: "{METHOD} {path} {base64_body} {nonce}"
  Headers: X-SBTC-APIKEY, X-SBTC-NONCE, X-SBTC-SIGNATURE

Endpoints:
  GET /api/v2/balances — all currency balances
  GET /api/v2/currencies/{currency}/deposits — deposit history
  GET /api/v2/currencies/{currency}/withdrawals — withdrawal history
"""

import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import date, datetime

import httpx

from scrapers.base import BaseScraper, ScrapedBalance, ScrapedTransaction

logger = logging.getLogger(__name__)

BUDA_BASE = "https://www.buda.com"


class BudaScraper(BaseScraper):
    @property
    def name(self) -> str:
        return "buda_api"

    def __init__(self) -> None:
        self.api_key = os.environ["BUDA_API_KEY"]
        self.api_secret = os.environ["BUDA_API_SECRET"]

    def _sign(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Generate HMAC-SHA384 auth headers for a request."""
        nonce = str(int(time.time() * 1e6))
        encoded_body = base64.b64encode(body.encode()).decode() if body else ""
        msg = f"{method} {path} {encoded_body} {nonce}"

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

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """Fetch recent CLP deposits and withdrawals."""
        transactions: list[ScrapedTransaction] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for currency in ["clp", "btc"]:
                for tx_type in ["deposits", "withdrawals"]:
                    path = f"/api/v2/currencies/{currency}/{tx_type}"
                    headers = self._sign("GET", path)

                    try:
                        resp = await client.get(
                            f"{BUDA_BASE}{path}.json",
                            headers=headers,
                            params={"per": 50},
                        )
                        resp.raise_for_status()
                        data = resp.json()

                        items = data.get(tx_type, [])
                        for item in items:
                            amount_arr = item.get("amount", ["0", "CLP"])
                            amount_val = int(float(amount_arr[0]))
                            item_currency = amount_arr[1] if len(amount_arr) > 1 else currency.upper()

                            # Skip zero amounts
                            if amount_val == 0:
                                continue

                            # Withdrawals are expenses (negative)
                            if tx_type == "withdrawals":
                                amount_val = -abs(amount_val)

                            created = item.get("created_at", "")
                            tx_date = date.today()
                            if created:
                                try:
                                    tx_date = datetime.fromisoformat(
                                        created.replace("Z", "+00:00")
                                    ).date()
                                except ValueError:
                                    pass

                            item_id = item.get("id")
                            if not item_id:
                                logger.warning("Buda %s missing id, skipping", tx_type)
                                continue

                            transactions.append(
                                ScrapedTransaction(
                                    account_institution="buda",
                                    account_type="crypto",
                                    description=f"Buda {tx_type[:-1]} {item_currency}",
                                    amount=amount_val,
                                    transaction_date=tx_date,
                                    external_id=f"buda_{item_id}",
                                    scheduled_month=date(
                                        tx_date.year, tx_date.month, 1
                                    ),
                                )
                            )

                    except httpx.HTTPStatusError as e:
                        logger.warning(
                            "Buda %s/%s failed: %s", currency, tx_type, e
                        )
                    except Exception:
                        logger.exception("Buda %s/%s error", currency, tx_type)

        return transactions

    async def scrape_balances(self) -> list[ScrapedBalance]:
        """Fetch all currency balances."""
        path = "/api/v2/balances"
        headers = self._sign("GET", path)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BUDA_BASE}{path}.json", headers=headers
            )
            resp.raise_for_status()

            balances: list[ScrapedBalance] = []
            for bal in resp.json().get("balances", []):
                currency_id = bal.get("id", "")
                available = bal.get("available_amount", ["0"])
                amount = int(float(available[0]))

                if amount > 0:
                    logger.info(
                        "Buda balance %s: %s", currency_id, f"{amount:,}"
                    )
                    balances.append(
                        ScrapedBalance(
                            account_institution="buda",
                            account_type="crypto",
                            balance=amount,
                            as_of=date.today(),
                        )
                    )

            return balances
