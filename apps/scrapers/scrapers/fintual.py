"""Fintual API client scraper.

Auth: POST /access_tokens with email/password -> returns token
Goals: GET /goals with X-User-Email + X-User-Token headers
Each goal has a `nav` field = current net asset value in CLP.
"""

import logging
import os
from datetime import date

import httpx

from scrapers.base import BaseScraper, ScrapedBalance, ScrapedTransaction

logger = logging.getLogger(__name__)

FINTUAL_API = "https://fintual.cl/api"


class FintualScraper(BaseScraper):
    @property
    def name(self) -> str:
        return "fintual_api"

    def __init__(self) -> None:
        self.email = os.environ["FINTUAL_EMAIL"]
        self.password = os.environ["FINTUAL_TOKEN"]
        self._token: str | None = None

    async def _authenticate(self, client: httpx.AsyncClient) -> str:
        """Get an access token from Fintual."""
        if self._token:
            return self._token

        resp = await client.post(
            f"{FINTUAL_API}/access_tokens",
            json={"user": {"email": self.email, "password": self.password}},
        )
        resp.raise_for_status()
        self._token = resp.json()["data"]["attributes"]["token"]
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "X-User-Email": self.email,
            "X-User-Token": self._token or "",
        }

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        # Fintual doesn't expose individual buy/sell transactions via API.
        # Portfolio value changes are tracked via balance snapshots.
        return []

    async def scrape_balances(self) -> list[ScrapedBalance]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._authenticate(client)

            resp = await client.get(
                f"{FINTUAL_API}/goals",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()

            balances: list[ScrapedBalance] = []
            total_nav = 0

            for goal in resp.json().get("data", []):
                attrs = goal.get("attributes", {})
                nav = attrs.get("nav", 0)
                goal_name = attrs.get("name", "Unknown")

                if nav and float(nav) > 0:
                    balance_clp = int(round(float(nav)))
                    total_nav += balance_clp
                    logger.info(
                        "Fintual goal '%s': $%s CLP",
                        goal_name,
                        f"{balance_clp:,}",
                    )

            # Return a single aggregated balance for Fintual
            if total_nav > 0:
                balances.append(
                    ScrapedBalance(
                        account_institution="fintual",
                        account_type="investment",
                        balance=total_nav,
                        as_of=date.today(),
                    )
                )

            return balances
