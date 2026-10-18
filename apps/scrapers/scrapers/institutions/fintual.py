"""Fintual scraper (web-session auth).

Fintual retired the old API-token flow: `POST /api/access_tokens` still issues a
token, but `/api/goals` now sits behind the website's session auth, so the token
alone gets a 401. The working flow mirrors a browser sign-in:

    GET  /f/sign-in/                     -> establish session
    POST /auth/sessions/initiate_login   -> {email, password}; 201 => e-mail 2FA
    POST /auth/sessions/finalize_login_web -> {email, password, code}; sets cookies
    GET  /api/goals                      -> old JSON:API shape, `nav` per goal
    GET  /app/goals                      -> with auth_refresh_token, re-issues a
                                            short-lived monolith_token and
                                            rotates the refresh token

The session cookies are cached to disk. The monolith_token that /api/goals
checks lives only a few minutes, so scheduled scrapes refresh the session the
way the website does (no 2FA) and save the rotated refresh token right away. A
full 2FA login is only needed once the refresh token itself has expired: when
FINTUAL_EMAIL equals EMAIL_IMAP_USER the scraper reads the e-mailed code from
the mailbox and signs in by itself; otherwise run `make fintual-login` and type
the code. An automatic login that fails once is not retried until
`make fintual-login` succeeds.
"""

import asyncio
import inspect
import json
import logging
import os
import tempfile
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from product_model import InvestmentMetrics

from scrapers.backends.email import fetch_latest_code
from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)
from scrapers.retry import TRANSIENT_HTTP_STATUSES, send_with_retry

logger = logging.getLogger(__name__)

FINTUAL_ORIGIN = "https://fintual.cl"
SIGN_IN_URL = f"{FINTUAL_ORIGIN}/f/sign-in/"
INITIATE_LOGIN_URL = f"{FINTUAL_ORIGIN}/auth/sessions/initiate_login"
FINALIZE_LOGIN_URL = f"{FINTUAL_ORIGIN}/auth/sessions/finalize_login_web"
GOALS_URL = f"{FINTUAL_ORIGIN}/api/goals"
# The web app page whose server render refreshes the session.
APP_URL = f"{FINTUAL_ORIGIN}/app/goals"

# A desktop Chrome UA; Fintual's session endpoints behave differently without one.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Cookie that keeps the session alive (it re-issues the short-lived
# monolith_token); its presence gates a cache hit.
_SESSION_COOKIE = "auth_refresh_token"

# A fresh 2FA e-mail can take tens of seconds to reach the inbox.
_CODE_POLL_ATTEMPTS = 30
_CODE_POLL_INTERVAL_SECONDS = 2
# Slack between our clock and the mail server's when picking a fresh code.
_CODE_CLOCK_SKEW = timedelta(seconds=30)

_RELOGIN_HINT = "Run `make fintual-login` to sign in again."

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
        # The refresh token is single-use, so two runs at once (the startup
        # scrape isn't a scheduler job, so a manual refresh can overlap it)
        # would both spend it. A run waits, then reads the rotated cache.
        self._session_lock = asyncio.Lock()

    # -- session persistence ------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        return {"User-Agent": BROWSER_USER_AGENT, "Origin": FINTUAL_ORIGIN}

    @property
    def _login_failed_file(self) -> Path:
        """Marker left by a failed automatic login, next to the session cache."""
        return self.session_file.with_name(self.session_file.name + ".login-failed")

    def _save_session(self, client: httpx.AsyncClient) -> None:
        cookies = {c.name: c.value for c in client.cookies.jar if c.value}
        # Session tokens are secrets: the temp file is created private (0600),
        # and swapping it in whole means a kill mid-write can't lose a rotated
        # refresh token.
        fd, tmp = tempfile.mkstemp(
            dir=self.session_file.parent, prefix=self.session_file.name + "."
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cookies, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.session_file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
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

    # -- login (auto via IMAP, or manual, interactive) ----------------------

    @property
    def _can_auto_login(self) -> bool:
        """True when the Fintual account is our IMAP mailbox, so the 2FA code
        e-mailed to it can be read automatically."""
        return os.environ.get("EMAIL_IMAP_USER") == self.email

    async def _email_code_provider(self, received_after: datetime) -> str:
        """Poll the inbox for a Fintual 2FA code e-mailed after `received_after`."""
        for _ in range(_CODE_POLL_ATTEMPTS):
            code = await fetch_latest_code(
                sender_contains=["hola@fintual.com"],
                subject_contains=["código para entrar"],
                received_after=received_after,
            )
            if code:
                logger.info("Fintual: read 2FA code from e-mail.")
                return code
            await asyncio.sleep(_CODE_POLL_INTERVAL_SECONDS)
        raise FintualSessionError(
            "Fintual's 2FA code e-mail did not reach the IMAP inbox."
        )

    async def login(self, code_provider: Callable[[], str | Awaitable[str]]) -> None:
        """Sign in and cache the session.

        `code_provider` is called (only when 2FA is required) to obtain the
        6-digit code Fintual e-mails to the account address. It may be sync
        (interactive prompt) or async (reading the code from the IMAP inbox).
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
                result = code_provider()
                if inspect.isawaitable(result):
                    result = await result
                code = result.strip()
                resp = await client.post(
                    FINALIZE_LOGIN_URL,
                    headers={"Accept": "application/json", "Referer": SIGN_IN_URL},
                    json={"email": self.email, "password": self.password, "code": code},
                )
                if not resp.is_success:
                    raise FintualSessionError(
                        f"Fintual rejected the sign-in (HTTP {resp.status_code})."
                    )
                logger.info("Fintual 2FA accepted; session established.")
            elif resp.status_code == 200:
                logger.info("Fintual login succeeded without 2FA.")
            else:
                raise FintualSessionError(
                    f"Fintual refused to start the sign-in (HTTP {resp.status_code})."
                )

            self._save_session(client)
            self._login_failed_file.unlink(missing_ok=True)

    async def _auto_login(self) -> None:
        """Sign in unattended, reading the 2FA code from the IMAP inbox.

        Gives up after one failure until a manual login succeeds: retrying on
        every run would e-mail a new code each time, which looks like an attack.
        """
        if self._login_failed_file.exists():
            raise FintualSessionError(
                f"Fintual automatic sign-in failed earlier. {_RELOGIN_HINT}"
            )
        # Taken before initiate_login: only a code e-mailed after this point is
        # valid, and the inbox may still hold the previous login's spent one.
        received_after = datetime.now(timezone.utc) - _CODE_CLOCK_SKEW
        try:
            await self.login(lambda: self._email_code_provider(received_after))
        except Exception as exc:
            self._mark_login_failed()
            # Only our own messages are safe to show: httpx errors carry URLs.
            reason = (
                str(exc)
                if isinstance(exc, FintualSessionError)
                else f"Fintual automatic sign-in failed ({type(exc).__name__})."
            )
            raise FintualSessionError(f"{reason} {_RELOGIN_HINT}") from exc

    def _mark_login_failed(self) -> None:
        try:
            self._login_failed_file.write_text(datetime.now(timezone.utc).isoformat())
            os.chmod(self._login_failed_file, 0o600)
        except OSError:
            logger.warning("Fintual: could not write %s", self._login_failed_file)

    async def _ensure_session(self, client: httpx.AsyncClient) -> bool:
        """Load a cached session, or auto-login via IMAP when possible.

        Returns True when it had to sign in, so the run doesn't sign in twice.
        """
        if self._load_session(client):
            return False
        if self._can_auto_login:
            logger.info("Fintual: no cached session; signing in via e-mailed 2FA code.")
            await self._auto_login()
            self._load_session(client)
            return True
        raise FintualSessionError(
            "No cached Fintual session. Run `make fintual-login` to sign in."
        )

    # -- scraping -----------------------------------------------------------

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        # Fintual doesn't expose individual buy/sell transactions via API.
        return []

    async def _get_goals(self, client: httpx.AsyncClient) -> httpx.Response:
        """GET /api/goals, retrying transient failures. A 401 is returned, not
        retried: it means the session expired, which the caller handles."""
        return await send_with_retry(
            lambda: client.get(GOALS_URL, headers={"Accept": "application/json"}),
            label="Fintual GET /api/goals",
        )

    async def _refresh_session(self, client: httpx.AsyncClient) -> bool:
        """Refresh an expired session the way the website does; True on success.

        The refresh token rotates on every use, so the jar is saved at once,
        before anything else can fail and strand the spent one on disk.
        """
        resp = await send_with_retry(
            lambda: client.get(
                APP_URL,
                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            ),
            label="Fintual GET /app/goals",
        )
        # An outage that outlived the retries says nothing about the refresh
        # token: fail the run instead of signing in (and mailing a code).
        if resp.status_code in TRANSIENT_HTTP_STATUSES:
            resp.raise_for_status()
        # A redirect to the sign-in page also ends in a 200: not a refresh.
        if resp.status_code != 200 or not resp.url.path.startswith("/app"):
            logger.info(
                "Fintual: session refresh answered HTTP %d at %s.",
                resp.status_code,
                resp.url.path,
            )
            return False
        self._save_session(client)
        logger.info("Fintual: session refreshed.")
        return True

    async def scrape_products(self) -> ProductScrapeResult:
        async with self._session_lock, httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=self._base_headers()
        ) as client:
            signed_in = await self._ensure_session(client)

            resp = await self._get_goals(client)
            if resp.status_code == 401 and await self._refresh_session(client):
                resp = await self._get_goals(client)
            if resp.status_code == 401:
                # The refresh token itself expired or was revoked: only a full
                # sign-in helps.
                if signed_in:
                    raise FintualSessionError(
                        f"Fintual rejected the session it just issued. {_RELOGIN_HINT}"
                    )
                if not self._can_auto_login:
                    raise FintualSessionError(f"Fintual session expired. {_RELOGIN_HINT}")
                logger.info("Fintual: session expired; signing in again.")
                await self._auto_login()
                self._load_session(client)
                resp = await self._get_goals(client)
            resp.raise_for_status()

            # One product per goal: the JSON:API resource id is the stable
            # external_ref, so each goal keeps its own balance history.
            products: list[ScrapedProduct] = []
            warnings: list[str] = []
            total_nav = 0
            skipped = 0

            for goal in resp.json().get("data", []):
                goal_id = goal.get("id")
                if goal_id is None:
                    # Without an id there is no stable external_ref; skip the
                    # entry rather than abort the run.
                    skipped += 1
                    continue

                attrs = goal.get("attributes", {})
                # A brand-new goal can report `"nav": null`; value it at 0.
                nav = int(round(float(attrs.get("nav") or 0)))
                total_nav += nav

                # deposited/profit ride along when the payload reports them.
                deposited = attrs.get("deposited")
                profit = attrs.get("profit")

                products.append(
                    ScrapedProduct(
                        institution="fintual",
                        kind="investment",
                        currency="CLP",
                        external_ref=str(goal_id),
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

            if skipped:
                logger.warning("Fintual: skipped %d goal(s) without an id", skipped)
                warnings.append(f"Fintual: skipped {skipped} goal(s) without an id")

            logger.info(
                "Fintual: %d goals, total nav $%s CLP",
                len(products),
                f"{total_nav:,}",
            )

            return ProductScrapeResult(products, warnings)


def _login_cli() -> None:
    """Manual sign-in: `python -m scrapers.institutions.fintual` (or `make fintual-login`)."""
    from dotenv import load_dotenv

    load_dotenv()  # walks up to the repo-root .env for FINTUAL_EMAIL
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    scraper = FintualScraper()

    async def prompt_code() -> str:
        return input("Enter the 6-digit code Fintual e-mailed you: ")

    asyncio.run(scraper.login(prompt_code))
    print(f"\n✅ Signed in. Session cached at {scraper.session_file}")


if __name__ == "__main__":
    _login_cli()
