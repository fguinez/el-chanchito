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
    POST /gql/                           -> the stock brokerage ("Acciones"):
                                            StocksPortfolio lists the USD wallet
                                            and the positions, StocksDetails
                                            batches each position's totals
    GET  /stocks-pricing-cl/current-prices -> latest price per symbol; wants a
                                            per-service JWT cookie minted by
                                            POST /auth/jwt

The session cookies are cached to disk. The monolith_token that /api/goals
checks lives only a few minutes, so scheduled scrapes refresh the session the
way the website does (no 2FA) and save the rotated refresh token right away. A
full 2FA login is only needed once the refresh token itself has expired: when
FINTUAL_EMAIL equals EMAIL_IMAP_USER the scraper reads the e-mailed code from
the mailbox and signs in by itself; otherwise run `make fintual-login` and type
the code. An automatic login that fails once is not retried until
`make fintual-login` succeeds.

Both legs of a run share one session drive: the transactions leg (which
`run_scraper` calls first) runs it and caches it, or its error, for the
products leg, so a run refreshes the session or signs in at most once.

Each run emits one CLP `investment` product per goal, then the stocks: one USD
`investment` product per position (`external_ref` "stock:<asset id>") and one
USD `wallet` for the cash not yet invested in stocks. Position figures follow
the web app's own formulas (see `stock_metrics`). The stocks are a separate
leg of the same run: if they fail, the goals still land and the run is
recorded partial; if only the positions fail, the wallet lands too. An
account that never opened the brokerage (nothing resolves and GraphQL reports
no error) gets no stock products and no warning; nothing resolving next to a
GraphQL error is a failed leg.
"""

import asyncio
import inspect
import json
import logging
import os
import tempfile
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

import httpx

from product_model import InvestmentAttributes, InvestmentMetrics, WalletMetrics

from scrapers.backends.email import fetch_latest_code
from scrapers.backends.fintual_stocks import (
    STOCKS_CURRENCY,
    cents,
    number,
    stock_ref,
    text,
)
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
GRAPHQL_URL = f"{FINTUAL_ORIGIN}/gql/"
STOCK_PRICES_URL = f"{FINTUAL_ORIGIN}/stocks-pricing-cl/current-prices"
# Mints the per-service JWT cookie the stocks pricing service checks.
SERVICE_JWT_URL = f"{FINTUAL_ORIGIN}/auth/jwt"
# The web app page the stocks requests come from.
_STOCKS_REFERER = f"{FINTUAL_ORIGIN}/f/stocks/wallet/"
_PRICING_AUDIENCE = "stocks-pricing"
_PRICING_JWT_COOKIE = "stocks-pricing_service_jwt"

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

STOCKS_WALLET_NAME = "Acciones: saldo disponible"
# Per-share costs keep more places: a quote can be under a dollar.
_AVERAGE_COST_STEP = Decimal("0.0001")

_PORTFOLIO_QUERY = """\
query StocksPortfolio {
  stocksWallet { id balance { amount currency } updatedAt }
  stocksPositionsV2 { positions { id shares asset { id symbol name } } }
}"""
_PERFORMANCE_FIELDS = (
    "id realizedPl totalCompletedSubscriptions totalCompletedRedemptions "
    "totalReceivedDividends shares"
)
_ASSET_FIELDS = "id name symbol overview assetTags { id emoji displayName }"

# apps/scrapers/, resolved from this file so cwd doesn't matter.
_APP_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_SESSION_FILE = _APP_DIR / ".fintual_session.json"


class FintualSessionError(RuntimeError):
    """Raised when there is no usable cached session and 2FA login is required."""


class FintualStocksError(RuntimeError):
    """The stocks leg failed. The message is ours, safe for the dashboard: no
    URLs (they carry the held symbols), tokens or response bodies."""


# -- stocks: pure parsing and maths ----------------------------------------


@dataclass(frozen=True)
class StockPosition:
    """One holding from StocksPortfolio."""

    asset_id: str
    symbol: str
    name: str
    shares: Decimal


@dataclass(frozen=True)
class StockPrice:
    """A symbol's latest price from the pricing service."""

    price: Decimal
    as_of: datetime | None


def _usd(value: Decimal) -> float:
    return float(cents(value))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_positions(portfolio: dict) -> tuple[list[StockPosition], int]:
    """The positions of a StocksPortfolio answer, plus how many were unreadable.

    A null or empty list (an account without stocks) is no positions.
    """
    container = portfolio.get("stocksPositionsV2") or {}
    positions: list[StockPosition] = []
    unreadable = 0
    for row in container.get("positions") or []:
        row = row or {}
        asset = row.get("asset") or {}
        symbol = text(asset.get("symbol"))
        shares = number(row.get("shares"))
        if asset.get("id") is None or symbol is None or shares is None or shares < 0:
            unreadable += 1
            continue
        positions.append(
            StockPosition(
                asset_id=str(asset["id"]),
                symbol=symbol,
                name=text(asset.get("name")) or symbol,
                shares=shares,
            )
        )
    return positions, unreadable


def parse_prices(payload: Any) -> dict[str, StockPrice]:
    """Map symbol -> latest price. A row without a positive price is left out,
    so its position is never silently valued at 0."""
    prices: dict[str, StockPrice] = {}
    rows = payload.get("prices") if isinstance(payload, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        price = number(row.get("price"))
        if not isinstance(symbol, str) or price is None or price <= 0:
            continue
        prices[symbol] = StockPrice(price, _parse_datetime(row.get("price_datetime")))
    return prices


def details_query(count: int) -> str:
    """One StocksDetails query for `count` positions: position i's totals and
    asset profile come back under the aliases p{i} and a{i}, for $s{i}."""
    variables = ", ".join(f"$s{i}: String!" for i in range(count))
    fields = "\n".join(
        f"  p{i}: stocksPositionPerformance(assetSymbol: $s{i}) {{ {_PERFORMANCE_FIELDS} }}\n"
        f"  a{i}: stocksAsset(assetSymbol: $s{i}) {{ {_ASSET_FIELDS} }}"
        for i in range(count)
    )
    return f"query StocksDetails({variables}) {{\n{fields}\n}}"


def stock_metrics(
    shares: Decimal, price: StockPrice | None, performance: dict
) -> InvestmentMetrics:
    """A position's figures, by the web app's own formulas (verified to the cent):

        value          = shares * price
        total_pl       = value - subscriptions + redemptions
        unrealized_pl  = total_pl - realized_pl
        average_cost   = price - unrealized_pl / shares       (0 without shares)
        unrealized_pct = unrealized_pl / (average_cost * shares)
        net_invested   = subscriptions - redemptions

    Amounts are rounded to cents before anything is derived from them, so
    profit = nav - deposited to the cent, and average_cost is the held
    shares' cost (nav - unrealized_profit) per share. nav/deposited/profit
    keep the goals' meaning. A position without shares needs no price: it is
    worth 0. Raises ValueError when a performance total is missing, or a held
    position has no price.
    """
    subscriptions = number(performance.get("totalCompletedSubscriptions"))
    redemptions = number(performance.get("totalCompletedRedemptions"))
    realized = number(performance.get("realizedPl"))
    if subscriptions is None or redemptions is None or realized is None:
        raise ValueError("stock position performance totals are missing")
    if price is None and shares:
        raise ValueError("a held stock position needs a price")
    dividends = number(performance.get("totalReceivedDividends"))

    value = cents(shares * price.price) if price else Decimal(0)
    net_invested = cents(subscriptions - redemptions)
    total_pl = value - net_invested
    unrealized = total_pl - cents(realized)
    if shares:
        cost = value - unrealized
        average_cost = cost / shares
        unrealized_pct = unrealized / cost * 100 if cost else Decimal(0)
    else:
        average_cost = unrealized_pct = Decimal(0)
    average_cost = average_cost.quantize(_AVERAGE_COST_STEP, rounding=ROUND_HALF_UP)
    unrealized_pct = cents(unrealized_pct)

    return InvestmentMetrics(
        nav=float(value),
        deposited=float(net_invested),
        profit=float(total_pl),
        units=float(shares),
        unit_price=float(price.price) if price else None,
        average_cost=float(average_cost),
        unrealized_profit=float(unrealized),
        unrealized_profit_pct=float(unrealized_pct),
        realized_profit=_usd(realized),
        dividends=_usd(dividends) if dividends is not None else None,
        unit_price_as_of=price.as_of if price else None,
    )


def stock_attributes(symbol: str, asset: Any) -> InvestmentAttributes:
    """Identity of a position: its ticker, plus the profile when StocksDetails
    returned one (otherwise those fields stay None and keep their stored value)."""
    if not isinstance(asset, dict):
        return InvestmentAttributes(ticker=symbol)
    raw_tags = asset.get("assetTags")
    tags: list[str] | None = None  # a null list keeps the stored tags
    if isinstance(raw_tags, list):
        tags = []
        for tag in raw_tags:
            label = text(tag.get("displayName")) if isinstance(tag, dict) else None
            if label is None:
                continue
            emoji = text(tag.get("emoji"))
            tags.append(f"{emoji} {label}" if emoji else label)
    return InvestmentAttributes(
        ticker=symbol, description=text(asset.get("overview")), tags=tags
    )


def stock_wallet_product(wallet: Any) -> ScrapedProduct | None:
    """The USD cash wallet ("sin usar en acciones"); None when its balance is
    unreadable or not in USD."""
    if not isinstance(wallet, dict):
        return None
    balance = wallet.get("balance") or {}
    amount = number(balance.get("amount"))
    if amount is None or balance.get("currency") != STOCKS_CURRENCY:
        return None
    return ScrapedProduct(
        institution="fintual",
        kind="wallet",
        currency=STOCKS_CURRENCY,
        name=STOCKS_WALLET_NAME,
        metrics=WalletMetrics(balance=_usd(amount)),
    )


def _stocks_failure_reason(exc: Exception) -> str:
    """What a stocks failure may say on the dashboard: our own message, else
    only the exception type (httpx errors carry URLs)."""
    if isinstance(exc, FintualStocksError):
        return str(exc)
    return type(exc).__name__


@dataclass(frozen=True)
class _Cycle:
    """What one session drive read, for both legs of a run."""

    products: list[ScrapedProduct]
    transactions: list[ScrapedTransaction]
    warnings: list[str]


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
        # Set once this run has refreshed the session, so a later 401 doesn't
        # spend the rotated refresh token again.
        self._refreshed_this_run = False
        # One session feeds both legs: the transactions leg runs first
        # (run_scraper's order) and caches the cycle, or its error, for the
        # products leg, so a run never refreshes or signs in (and mails a
        # 2FA code) twice.
        self._cached: _Cycle | None = None
        self._error: Exception | None = None

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
        self._refreshed_this_run = True
        logger.info("Fintual: session refreshed.")
        return True

    # -- stocks -------------------------------------------------------------

    async def _graphql(
        self, client: httpx.AsyncClient, operation: str, query: str, variables: dict
    ) -> tuple[Any, int]:
        """POST a read-only GraphQL query; return its `data` and how many
        errors came with it.

        Queries only read, so transient failures are retried like a GET. A 401
        gets a session refresh unless the run already refreshed, never a
        sign-in. GraphQL reports errors in a 200: an answer without a `data`
        key means the request itself was refused (a parse or validation
        error, say a renamed field), which raises. Errors met while resolving
        still answer `data`, with the failed fields null; the caller decides
        what that means.
        """

        async def post() -> httpx.Response:
            return await send_with_retry(
                lambda: client.post(
                    GRAPHQL_URL,
                    headers={"Accept": "application/json", "Referer": _STOCKS_REFERER},
                    json={"query": query, "variables": variables},
                ),
                label=f"Fintual POST /gql/ {operation}",
            )

        resp = await post()
        if (
            resp.status_code == 401
            and not self._refreshed_this_run
            and await self._refresh_session(client)
        ):
            resp = await post()
        if not resp.is_success:
            raise FintualStocksError(f"{operation} answered HTTP {resp.status_code}")
        body = resp.json()
        if not isinstance(body, dict) or "data" not in body:
            raise FintualStocksError(f"{operation} was refused by the GraphQL server")
        errors = len(body.get("errors") or [])
        if errors:
            logger.info("Fintual: %s answered %d GraphQL error(s)", operation, errors)
        return body["data"], errors

    async def _mint_pricing_jwt(self, client: httpx.AsyncClient) -> None:
        """Get the pricing service's JWT cookie, as the web app does on a 401."""
        # The cached one was refused. If the server scopes its new cookie to
        # another path, a stale copy restored from the session cache would
        # ride along with it, so drop the old one first.
        client.cookies.delete(_PRICING_JWT_COOKIE)
        resp = await send_with_retry(
            lambda: client.post(
                SERVICE_JWT_URL,
                headers={"Accept": "application/json", "Referer": _STOCKS_REFERER},
                json={"aud": _PRICING_AUDIENCE},
            ),
            label="Fintual POST /auth/jwt",
        )
        if not resp.is_success:
            raise FintualStocksError(
                f"the stocks pricing token request answered HTTP {resp.status_code}"
            )

    async def _stock_prices(
        self, client: httpx.AsyncClient, symbols: list[str]
    ) -> dict[str, StockPrice]:
        """Latest price per symbol. On a 401 the service JWT is minted once
        and the request retried once; a second 401 fails the stocks leg."""

        async def get() -> httpx.Response:
            return await send_with_retry(
                lambda: client.get(
                    STOCK_PRICES_URL,
                    params={"symbols": ",".join(symbols)},
                    headers={"Accept": "application/json", "Referer": _STOCKS_REFERER},
                ),
                label="Fintual GET current-prices",
            )

        resp = await get()
        if resp.status_code == 401:
            await self._mint_pricing_jwt(client)
            resp = await get()
        if resp.status_code == 401:
            raise FintualStocksError(
                "the stocks pricing service refused a fresh token (HTTP 401)"
            )
        if not resp.is_success:
            raise FintualStocksError(
                f"the stocks pricing service answered HTTP {resp.status_code}"
            )
        return parse_prices(resp.json())

    async def _position_products(
        self, client: httpx.AsyncClient, positions: list[StockPosition]
    ) -> tuple[list[ScrapedProduct], int, int]:
        """One product per position, plus how many were skipped for lack of a
        price and of totals."""
        details, _ = await self._graphql(
            client,
            "StocksDetails",
            details_query(len(positions)),
            {f"s{i}": position.symbol for i, position in enumerate(positions)},
        )
        if not isinstance(details, dict):
            raise FintualStocksError("StocksDetails answered no data")
        # A sold-out position needs no price: it is worth 0 either way.
        held = list(dict.fromkeys(p.symbol for p in positions if p.shares))
        prices = await self._stock_prices(client, held) if held else {}

        products: list[ScrapedProduct] = []
        unpriced = without_totals = 0
        for i, position in enumerate(positions):
            price = prices.get(position.symbol)
            if price is None and position.shares:
                unpriced += 1
                continue
            performance = details.get(f"p{i}")
            try:
                metrics = stock_metrics(position.shares, price, performance or {})
            except ValueError:
                without_totals += 1
                continue
            products.append(
                ScrapedProduct(
                    institution="fintual",
                    kind="investment",
                    currency=STOCKS_CURRENCY,
                    external_ref=stock_ref(position.asset_id),
                    name=position.name,
                    attributes=stock_attributes(position.symbol, details.get(f"a{i}")),
                    metrics=metrics,
                )
            )
        return products, unpriced, without_totals

    async def _scrape_stocks(
        self, client: httpx.AsyncClient
    ) -> tuple[list[ScrapedProduct], list[str]]:
        """The brokerage: the USD cash wallet plus one product per position."""
        portfolio, errors = await self._graphql(
            client, "StocksPortfolio", _PORTFOLIO_QUERY, {}
        )
        portfolio = portfolio if isinstance(portfolio, dict) else {}
        wallet = portfolio.get("stocksWallet")
        positions, unreadable = parse_positions(portfolio)
        if wallet is None and not positions and not unreadable:
            if errors:
                # Nothing resolved and GraphQL said why: the leg failed, and
                # stock products from earlier runs would freeze unnoticed.
                logger.warning("Fintual: the stocks portfolio failed to resolve")
                return [], ["Fintual: skipped the stocks leg (GraphQL error)"]
            # Never opened the brokerage: a null wallet and no positions, with
            # no GraphQL error. Not a failure. (This shape is unverified live.)
            logger.info("Fintual: no stocks account")
            return [], []

        products: list[ScrapedProduct] = []
        warnings: list[str] = []
        if wallet is not None:
            wallet_product = stock_wallet_product(wallet)
            if wallet_product is None:
                warnings.append(
                    "Fintual: skipped the stocks wallet (no readable USD balance)"
                )
            else:
                products.append(wallet_product)
        if unreadable:
            warnings.append(f"Fintual: skipped {unreadable} unreadable stock position(s)")
        if errors:
            # The account exists (something answered), so a field that failed
            # to resolve answered null: say so, or its products would sit
            # frozen at their last values in a clean-looking run.
            if wallet is None:
                logger.warning("Fintual: the stocks wallet failed to resolve")
                warnings.append("Fintual: skipped the stocks wallet (GraphQL error)")
            container = portfolio.get("stocksPositionsV2") or {}
            if not isinstance(container.get("positions"), list):
                logger.warning("Fintual: the stock positions failed to resolve")
                warnings.append("Fintual: skipped the stock positions (GraphQL error)")

        unpriced = without_totals = 0
        if positions:
            # Whatever fails on the positions' own requests, the wallet lands.
            try:
                position_products, unpriced, without_totals = (
                    await self._position_products(client, positions)
                )
            except Exception as exc:
                logger.exception("Fintual: stock positions failed; keeping the wallet")
                warnings.append(
                    f"Fintual stock positions skipped: {_stocks_failure_reason(exc)}"
                )
            else:
                products.extend(position_products)

        if unpriced:
            logger.warning(
                "Fintual: %d stock position(s) without a current price", unpriced
            )
            warnings.append(
                f"Fintual: skipped {unpriced} stock position(s) without a current price"
            )
        if without_totals:
            logger.warning("Fintual: %d stock position(s) without totals", without_totals)
            warnings.append(
                f"Fintual: skipped {without_totals} stock position(s) without totals"
            )
        logger.info(
            "Fintual: %d stock positions%s",
            sum(p.kind == "investment" for p in products),
            ", plus the USD wallet" if any(p.kind == "wallet" for p in products) else "",
        )
        return products, warnings

    async def _goal_products(
        self, client: httpx.AsyncClient
    ) -> tuple[list[ScrapedProduct], list[str]]:
        """Make sure the session works, then read one product per goal.

        A session that can't be refreshed or signed in again raises: without
        it there is nothing to read.
        """
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
        return products, warnings

    async def _run_cycle(self) -> _Cycle:
        """One session drive: the goals, then the stocks, then save the jar."""
        async with self._session_lock, httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=self._base_headers()
        ) as client:
            self._refreshed_this_run = False
            products, warnings = await self._goal_products(client)

            # The stocks are a separate leg: whatever fails there, the goals
            # still land and the warning records the run as partial.
            try:
                stock_products, stock_warnings = await self._scrape_stocks(client)
            except Exception as exc:
                logger.exception("Fintual: stocks failed; keeping the goals")
                warnings.append(f"Fintual stocks skipped: {_stocks_failure_reason(exc)}")
            else:
                products.extend(stock_products)
                warnings.extend(stock_warnings)

            # Keep the newest jar on disk (the stocks leg may have refreshed the
            # session or minted the pricing cookie). The refresh token itself
            # was saved the moment it rotated, so a failed save here costs at
            # most a new pricing cookie on the next run.
            try:
                self._save_session(client)
            except OSError:
                logger.warning("Fintual: could not save the session", exc_info=True)

            return _Cycle(products, [], warnings)

    async def _scrape_cycle(self) -> _Cycle:
        """Drive the session once, caching the cycle (or its error) for the
        products leg. Resetting first matters: the instance lives as long as
        the service."""
        self._cached = None
        self._error = None
        try:
            cycle = await self._run_cycle()
        except Exception as exc:
            self._error = exc
            raise
        self._cached = cycle
        return cycle

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        # Goals expose no individual movements, and stock movements aren't
        # read yet (they'd need transactions that target one position). The
        # cycle still runs here, first, so the products leg reuses it.
        await self._scrape_cycle()
        return []

    async def scrape_products(self) -> ProductScrapeResult:
        """The products of the cycle the transactions leg drove.

        If that drive failed, its error is re-raised without driving again (no
        second refresh, sign-in or 2FA mail). Called alone (a products-only dry
        run), it drives the session itself.
        """
        if self._cached is not None:
            cycle, self._cached = self._cached, None
        elif self._error is not None:
            error, self._error = self._error, None
            raise error
        else:
            try:
                cycle = await self._scrape_cycle()
            finally:
                self._cached = self._error = None
        return ProductScrapeResult(cycle.products, cycle.warnings)


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
