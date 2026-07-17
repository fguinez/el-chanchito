"""Fintual scraper (web-session auth).

Fintual retired the old API-token flow: `POST /api/access_tokens` still issues a
token, but `/api/goals` now sits behind the website's session auth, so the token
alone gets a 401. The working flow mirrors a browser sign-in:

    GET  /f/sign-in/                     -> establish session
    POST /auth/sessions/initiate_login   -> {email, password}; 201 => e-mail 2FA
    POST /auth/sessions/finalize_login_web -> {email, password, code}; sets cookies
    GET  /api/goals                      -> old JSON:API shape, `nav` per goal

Because the 2FA code is e-mailed to the account address (not a mailbox we can
read), sign-in is a manual step: run `make fintual-login`, type the code, and the
session cookies are cached to disk. Scheduled scrapes reuse the cached session and
fail with a clear message when it expires so the login can be repeated.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable

import httpx

from product_model import InvestmentMetrics

from scrapers.base import BaseScraper, ScrapedProduct, ScrapedTransaction

logger = logging.getLogger(__name__)

FINTUAL_ORIGIN = "https://fintual.cl"
SIGN_IN_URL = f"{FINTUAL_ORIGIN}/f/sign-in/"
INITIATE_LOGIN_URL = f"{FINTUAL_ORIGIN}/auth/sessions/initiate_login"
FINALIZE_LOGIN_URL = f"{FINTUAL_ORIGIN}/auth/sessions/finalize_login_web"
GOALS_URL = f"{FINTUAL_ORIGIN}/api/goals"

# A desktop Chrome UA; Fintual's session endpoints behave differently without one.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Cookie that proves an authenticated session; its presence gates a cache hit.
_SESSION_COOKIE = "monolith_token"

# apps/scrapers/, resolved from this file so cwd doesn't matter.
_APP_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_SESSION_FILE = _APP_DIR / ".fintual_session.json"


class FintualSessionError(RuntimeError):
    """Raised when there is no usable cached session and 2FA login is required."""


class FintualScraper(BaseScraper):
    method = "http_api"
    institution = "fintual"

    def __init__(self) -> None:
        self.email = os.environ["FINTUAL_EMAIL"]
        # FINTUAL_TOKEN is the legacy name for the same value: the account password.
        self.password = os.environ.get("FINTUAL_PASSWORD") or os.environ["FINTUAL_TOKEN"]
        self.session_file = Path(
            os.environ.get("FINTUAL_SESSION_FILE", str(_DEFAULT_SESSION_FILE))
        )

    # -- session persistence ------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        return {"User-Agent": BROWSER_USER_AGENT, "Origin": FINTUAL_ORIGIN}

    def _save_session(self, client: httpx.AsyncClient) -> None:
        cookies = {c.name: c.value for c in client.cookies.jar if c.value}
        self.session_file.write_text(json.dumps(cookies))
        # Session tokens are secrets — keep the cache private.
        os.chmod(self.session_file, 0o600)
        logger.info("Saved Fintual session to %s", self.session_file)

    def _load_session(self, client: httpx.AsyncClient) -> bool:
        """Restore cached cookies onto `client`; return True if a session exists."""
        if not self.session_file.exists():
            return False
        try:
            cookies = json.loads(self.session_file.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        for name, value in cookies.items():
            client.cookies.set(name, value, domain="fintual.cl")
        return _SESSION_COOKIE in cookies

    # -- login (manual, interactive) ---------------------------------------

    async def login(self, code_provider: Callable[[], str]) -> None:
        """Sign in and cache the session.

        `code_provider` is called (only when 2FA is required) to obtain the
        6-digit code Fintual e-mails to the account address.
        """
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=self._base_headers()
        ) as client:
            await client.get(
                SIGN_IN_URL,
                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            )

            resp = await client.post(
                INITIATE_LOGIN_URL,
                headers={"Accept": "application/json", "Referer": SIGN_IN_URL},
                json={"email": self.email, "password": self.password},
            )

            if resp.status_code == 201:
                logger.info("Fintual login initiated; e-mail 2FA required.")
                code = code_provider().strip()
                resp = await client.post(
                    FINALIZE_LOGIN_URL,
                    headers={"Accept": "application/json", "Referer": SIGN_IN_URL},
                    json={"email": self.email, "password": self.password, "code": code},
                )
                resp.raise_for_status()
                logger.info("Fintual 2FA accepted; session established.")
            elif resp.status_code == 200:
                logger.info("Fintual login succeeded without 2FA.")
            else:
                resp.raise_for_status()

            self._save_session(client)

    # -- scraping -----------------------------------------------------------

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        # Fintual doesn't expose individual buy/sell transactions via API.
        return []

    async def scrape_products(self) -> list[ScrapedProduct]:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=self._base_headers()
        ) as client:
            if not self._load_session(client):
                raise FintualSessionError(
                    "No cached Fintual session. Run `make fintual-login` to sign in."
                )

            resp = await client.get(GOALS_URL, headers={"Accept": "application/json"})
            if resp.status_code == 401:
                raise FintualSessionError(
                    "Fintual session expired. Run `make fintual-login` to sign in again."
                )
            resp.raise_for_status()

            # One product per goal: the JSON:API resource id is the stable
            # external_ref, so each goal keeps its own balance history.
            products: list[ScrapedProduct] = []
            total_nav = 0

            for goal in resp.json().get("data", []):
                attrs = goal.get("attributes", {})
                nav = int(round(float(attrs.get("nav", 0))))
                total_nav += nav

                # deposited/profit ride along when the payload reports them.
                deposited = attrs.get("deposited")
                profit = attrs.get("profit")

                products.append(
                    ScrapedProduct(
                        institution="fintual",
                        kind="investment",
                        currency="CLP",
                        external_ref=str(goal["id"]),
                        name=attrs.get("name", "Unknown"),
                        metrics=InvestmentMetrics(
                            nav=nav,
                            deposited=(
                                float(deposited) if deposited is not None else None
                            ),
                            profit=float(profit) if profit is not None else None,
                        ),
                    )
                )

            logger.info(
                "Fintual: %d goals, total nav $%s CLP",
                len(products),
                f"{total_nav:,}",
            )

            return products


def _login_cli() -> None:
    """Manual sign-in: `python -m scrapers.institutions.fintual` (or `make fintual-login`)."""
    from dotenv import load_dotenv

    load_dotenv()  # walks up to the repo-root .env for FINTUAL_EMAIL
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    scraper = FintualScraper()

    def prompt_code() -> str:
        return input("Enter the 6-digit code Fintual e-mailed you: ")

    asyncio.run(scraper.login(prompt_code))
    print(f"\n✅ Signed in. Session cached at {scraper.session_file}")


if __name__ == "__main__":
    _login_cli()
