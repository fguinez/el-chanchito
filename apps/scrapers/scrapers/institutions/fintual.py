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
                                            batches each position's totals,
                                            WalletMovements pages through the
                                            USD wallet's ledger
    GET  /stocks-pricing-cl/current-prices -> latest price per symbol; wants a
                                            per-service JWT cookie minted by
                                            POST /auth/jwt
    GET  /stocks-pricing-cl/historical-prices -> one symbol's daily closes,
                                            same cookie

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
`stock` product per position (`external_ref` "stock:<asset id>") and one
USD `wallet` for the cash not yet invested in stocks. Position figures follow
the web app's own formulas (see `stock_metrics`). The stocks are a separate
part of the same run: if they fail, the goals still land and the run is
recorded partial; if only the positions fail, the wallet lands too. An
account that never opened the brokerage (nothing resolves and GraphQL reports
no error) gets no stock products, no movements and no warning; nothing
resolving next to a GraphQL error is a failed leg.

The transactions are the wallet's movements (`backends/fintual_stocks.py`):
one row on the wallet each, plus a mirror row on the stock's product for buys,
sells, dividends and cash mergers. The whole ledger is read every run (the
writer skips the rows it has); a stock row whose product doesn't exist yet
lands on the next run. A failure there costs the movements only. When the
ledger is complete it is checked against the wallet's balance, and a stock it
shows sold (or paid out in a cash merger) that Fintual no longer lists gets a
zero-valued product.

Each run also reads, after the movements, the last year of daily closes of
every stock still held (one request per stock) as a price history for its
product. A stock whose history fails is skipped, counted in one warning; a
failure of the pricing service itself stops the step, which also has a time
budget. The rest of the run never depends on it.
"""

import asyncio
import inspect
import json
import logging
import os
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

import httpx

from product_model import (
    InvestmentMetrics,
    StockAttributes,
    StockMetrics,
    WalletMetrics,
)

from scrapers.backends.email import fetch_latest_code
from scrapers.backends.fintual_stocks import (
    MAX_PAGES,
    PAGE_SIZE,
    STOCKS_CURRENCY,
    WALLET_MOVEMENTS_QUERY,
    LedgerEntry,
    WalletPage,
    cents,
    ledger_balance,
    ledger_to_transactions,
    number,
    parse_wallet_page,
    shows_unsold_stocks,
    sold_out_products,
    stock_ref,
    text,
    usd,
)
from scrapers.base import (
    BaseScraper,
    DailyPrice,
    ProductRef,
    ProductScrapeResult,
    ScrapedPriceHistory,
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
HISTORICAL_PRICES_URL = f"{FINTUAL_ORIGIN}/stocks-pricing-cl/historical-prices"
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
# Every run reads a trailing year of daily closes per held stock: the endpoint
# takes one symbol per call, so a cursor of what is stored would save nothing.
PRICE_HISTORY_DAYS = 365
# `limit` counts bars forward from `start`; a year is about 250 trading days.
_PRICE_HISTORY_LIMIT = 400
# One request per stock; a wider portfolio gets the first ones and a warning.
MAX_PRICE_HISTORY_SYMBOLS = 30
# The whole step's time budget: it runs holding the session lock, and a
# degraded service could otherwise stall the run for minutes.
PRICE_HISTORY_BUDGET_SECONDS = 120.0
# What `product_prices.price` keeps.
_PRICE_STEP = Decimal("1e-8")

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


class FintualPricingUnavailable(FintualStocksError):
    """The stocks pricing service itself failed (a refused or failed token
    mint, or a 429/5xx that outlived the retries), whatever the symbol."""


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


def _nonzero_usd(value: Decimal | None) -> float | None:
    """`_usd`, or None for a missing or zero total: the Acciones table hides
    Realizada and Dividendos until a position has them."""
    return (_usd(value) or None) if value is not None else None


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


def listed_asset_ids(portfolio: dict) -> frozenset[str] | None:
    """Every asset id the positions list holds, readable or not; None unless
    the whole list was read (it failed to resolve, or a row has no id).

    A stock is valued at zero for leaving this list, so a held position
    skipped for lack of a price, still listed, is never zeroed.
    """
    container = portfolio.get("stocksPositionsV2")
    rows = container.get("positions") if isinstance(container, dict) else None
    if not isinstance(rows, list):
        return None
    ids: set[str] = set()
    for row in rows:
        asset = row.get("asset") if isinstance(row, dict) else None
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        if asset_id is None:
            return None
        ids.add(str(asset_id))
    return frozenset(ids)


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


def history_start(today: date) -> str:
    """The `start` of a trailing year of daily bars, as the RFC 3339 instant
    historical-prices wants (a bare date is refused)."""
    return f"{today - timedelta(days=PRICE_HISTORY_DAYS):%Y-%m-%d}T00:00:00Z"


def _bar_time(time: Any) -> datetime:
    """A bar's ISO timestamp in UTC (no offset reads as UTC); ValueError if
    it isn't one."""
    if not isinstance(time, str):
        raise ValueError("a bar's time must be an ISO datetime string")
    stamp = datetime.fromisoformat(time)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def trading_date(time: str) -> date:
    """The trading day of a daily bar: the UTC date of its timestamp.

    Fintual stamps a day's bar at New York midnight (`T04:00:00Z`, or
    `T05:00:00Z` under standard time), so unlike a movement's date, which is
    read in America/Santiago, the bar is never converted: in Chile it would
    land on the day before. A timestamp without an offset is read as UTC.
    Raises ValueError when it isn't an ISO datetime.
    """
    return _bar_time(time).date()


def parse_price_history(payload: Any) -> tuple[list[DailyPrice], int]:
    """The daily closes of a historical-prices answer, oldest first, plus how
    many bars were unusable.

    A bar without a positive price (after rounding to the stored 8 decimals)
    or a readable time is dropped and counted. Two bars on one trading day
    collapse to the later one. Raises ValueError when the answer has no
    price list at all.
    """
    rows = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("historical-prices answered no price list")
    latest: dict[date, tuple[datetime, DailyPrice]] = {}
    skipped = 0
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError("not a bar")
            # The stamp orders a day's bars; the day itself is trading_date's.
            stamp = _bar_time(row.get("time"))
            day = trading_date(row.get("time"))
            price = number(row.get("price"))
            if price is None:
                raise ValueError("no price")
            price = price.quantize(_PRICE_STEP, rounding=ROUND_HALF_UP)
            point = DailyPrice(date=day, price=price)
        # pydantic's ValidationError is a ValueError; an absurd price can
        # overflow the quantize (decimal.InvalidOperation, an ArithmeticError).
        except (ValueError, ArithmeticError):
            skipped += 1
            continue
        kept = latest.get(day)
        if kept is None or stamp >= kept[0]:
            latest[day] = (stamp, point)
    return [latest[day][1] for day in sorted(latest)], skipped


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
) -> StockMetrics:
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
    worth 0. A zero realized or dividend total is left out. Raises ValueError
    when a performance total is missing, or a held position has no price.
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

    return StockMetrics(
        nav=float(value),
        deposited=float(net_invested),
        profit=float(total_pl),
        units=float(shares),
        unit_price=float(price.price) if price else None,
        average_cost=float(average_cost),
        unrealized_profit=float(unrealized),
        unrealized_profit_pct=float(unrealized_pct),
        realized_profit=_nonzero_usd(realized),
        dividends=_nonzero_usd(dividends),
        unit_price_as_of=price.as_of if price else None,
    )


def stock_attributes(symbol: str, asset: Any) -> StockAttributes:
    """Identity of a position: its ticker, plus the profile when StocksDetails
    returned one (otherwise those fields stay None and keep their stored value)."""
    if not isinstance(asset, dict):
        return StockAttributes(ticker=symbol)
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
    return StockAttributes(
        ticker=symbol, description=text(asset.get("overview")), tags=tags
    )


def wallet_balance(wallet: Any) -> Decimal | None:
    """The USD cash wallet's balance, in cents; None when it is unreadable or
    not in USD."""
    if not isinstance(wallet, dict):
        return None
    balance = wallet.get("balance") or {}
    if balance.get("currency") != STOCKS_CURRENCY:
        return None
    return usd(balance.get("amount"))


def stock_wallet_product(wallet: Any) -> ScrapedProduct | None:
    """The USD cash wallet ("sin usar en acciones"); None when its balance is
    unreadable or not in USD."""
    balance = wallet_balance(wallet)
    if balance is None:
        return None
    return ScrapedProduct(
        institution="fintual",
        kind="wallet",
        currency=STOCKS_CURRENCY,
        name=STOCKS_WALLET_NAME,
        metrics=WalletMetrics(balance=float(balance)),
    )


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _pricing_service_failed(exc: Exception) -> bool:
    """Whether a stock's price history failed for a reason the next symbol
    would meet too: the network (timeouts included) or the pricing service
    itself, not that symbol's answer."""
    return isinstance(exc, (httpx.TransportError, FintualPricingUnavailable))


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
    price_histories: list[ScrapedPriceHistory] = field(default_factory=list)


@dataclass(frozen=True)
class _Stocks:
    """What the brokerage's portfolio read, for the movements read after it.

    `listed_asset_ids` is None unless the full positions list was read, and
    `wallet_balance` None unless the wallet's USD balance was.
    `held_positions` are the positions with shares that got a product, the
    ones whose price history is read.
    """

    products: list[ScrapedProduct]
    warnings: list[str]
    has_account: bool = True
    listed_asset_ids: frozenset[str] | None = None
    wallet_balance: Decimal | None = None
    held_positions: tuple[StockPosition, ...] = ()


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
            raise FintualPricingUnavailable(
                f"the stocks pricing token request answered HTTP {resp.status_code}"
            )

    async def _pricing_get(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str], label: str
    ) -> httpx.Response:
        """GET from the stocks pricing service, retrying transient failures.

        On a 401 the service JWT is minted once and the request retried once,
        as the web app does (the cookie lives minutes, so a long run may need
        a new one); a second 401, or any other failure, raises. A failure no
        other symbol would escape (the token, or a 429/5xx) raises
        `FintualPricingUnavailable`.
        """

        async def get() -> httpx.Response:
            return await send_with_retry(
                lambda: client.get(
                    url,
                    params=params,
                    headers={"Accept": "application/json", "Referer": _STOCKS_REFERER},
                ),
                label=label,
            )

        resp = await get()
        if resp.status_code == 401:
            await self._mint_pricing_jwt(client)
            resp = await get()
        if resp.status_code == 401:
            raise FintualPricingUnavailable(
                "the stocks pricing service refused a fresh token (HTTP 401)"
            )
        if resp.status_code in TRANSIENT_HTTP_STATUSES:
            raise FintualPricingUnavailable(
                f"the stocks pricing service answered HTTP {resp.status_code}"
            )
        if not resp.is_success:
            raise FintualStocksError(
                f"the stocks pricing service answered HTTP {resp.status_code}"
            )
        return resp

    async def _stock_prices(
        self, client: httpx.AsyncClient, symbols: list[str]
    ) -> dict[str, StockPrice]:
        """Latest price per symbol; a refused token fails the stocks leg."""
        resp = await self._pricing_get(
            client,
            STOCK_PRICES_URL,
            {"symbols": ",".join(symbols)},
            "Fintual GET current-prices",
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
                    kind="stock",
                    currency=STOCKS_CURRENCY,
                    external_ref=stock_ref(position.asset_id),
                    name=position.name,
                    attributes=stock_attributes(position.symbol, details.get(f"a{i}")),
                    metrics=metrics,
                )
            )
        return products, unpriced, without_totals

    async def _scrape_stocks(self, client: httpx.AsyncClient) -> _Stocks:
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
                # The movements are still read, unchecked (see _Stocks).
                return _Stocks([], ["Fintual: skipped the stocks leg (GraphQL error)"])
            # Never opened the brokerage: a null wallet and no positions, with
            # no GraphQL error. Not a failure. (This shape is unverified live.)
            logger.info("Fintual: no stocks account")
            return _Stocks([], [], has_account=False)

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
        held: list[StockPosition] = []
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
                emitted = {p.external_ref for p in position_products}
                held = [
                    p
                    for p in positions
                    if p.shares > 0 and stock_ref(p.asset_id) in emitted
                ]

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
            sum(p.kind == "stock" for p in products),
            ", plus the USD wallet" if any(p.kind == "wallet" for p in products) else "",
        )
        return _Stocks(
            products,
            warnings,
            listed_asset_ids=None if errors else listed_asset_ids(portfolio),
            wallet_balance=wallet_balance(wallet),
            held_positions=tuple(held),
        )

    async def _price_history(
        self, client: httpx.AsyncClient, symbol: str, start: str
    ) -> tuple[list[DailyPrice], int]:
        """One symbol's daily closes since `start`, plus the bars dropped.
        Raises when the request fails or no close is usable."""
        resp = await self._pricing_get(
            client,
            HISTORICAL_PRICES_URL,
            {
                "symbol": symbol,
                "start": start,
                "timeframe": "1Day",
                "limit": str(_PRICE_HISTORY_LIMIT),
            },
            "Fintual GET historical-prices",
        )
        points, skipped = parse_price_history(resp.json())
        if not points:
            raise FintualStocksError("historical-prices answered no usable close")
        return points, skipped

    async def _price_histories(
        self, client: httpx.AsyncClient, positions: list[StockPosition]
    ) -> tuple[list[ScrapedPriceHistory], list[str]]:
        """A trailing year of daily closes for each held position, one request
        each, in turn, plus the warnings.

        Each series names its product through `stock_ref`, exactly as the
        position's product does. A stock whose history fails is skipped and
        counted into one warning that carries only counts and exception types
        (an httpx error's message has the URL, and so the ticker). A failure
        that isn't the symbol's own (see `_pricing_service_failed`) stops the
        loop, and the stocks not read yet count as failed too: one token mint
        and one round of retries per run, not one per stock. Running over the
        step's time budget stops it too, keeping the series already read.
        """
        warnings: list[str] = []
        # One series per product, even if a position were listed twice.
        unique = list({p.asset_id: p for p in positions}.values())
        targets = unique[:MAX_PRICE_HISTORY_SYMBOLS]
        if len(unique) > len(targets):
            logger.warning(
                "Fintual: price history limited to %d of %d stocks",
                len(targets),
                len(unique),
            )
            warnings.append(
                f"Fintual: read the price history of {len(targets)} of "
                f"{len(unique)} stocks (at most {MAX_PRICE_HISTORY_SYMBOLS} per run)"
            )

        start = history_start(_utc_today())
        deadline = asyncio.get_running_loop().time() + PRICE_HISTORY_BUDGET_SECONDS
        histories: list[ScrapedPriceHistory] = []
        failures: Counter = Counter()
        dropped = 0
        not_read = 0
        for i, position in enumerate(targets):
            try:
                async with asyncio.timeout_at(deadline):
                    points, skipped = await self._price_history(
                        client, position.symbol, start
                    )
                history = ScrapedPriceHistory(
                    institution="fintual",
                    product=ProductRef(
                        kind="stock",
                        currency=STOCKS_CURRENCY,
                        external_ref=stock_ref(position.asset_id),
                    ),
                    points=points,
                )
            except TimeoutError:
                not_read = len(targets) - i
                logger.warning(
                    "Fintual: stock price history ran over its %.0f s budget; "
                    "%d stock(s) not read",
                    PRICE_HISTORY_BUDGET_SECONDS,
                    not_read,
                )
                break
            except Exception as exc:
                reason = type(exc).__name__
                if not _pricing_service_failed(exc):
                    logger.warning("Fintual: a stock's price history failed (%s)", reason)
                    failures[reason] += 1
                    continue
                # The next stock would fail the same way: count this one and
                # every one not read yet, and stop.
                left = len(targets) - i
                logger.warning(
                    "Fintual: price history stopped at a pricing service failure "
                    "(%s); %d stock(s) not read",
                    reason,
                    left,
                )
                failures[reason] += left
                break
            dropped += skipped
            histories.append(history)

        if failures:
            warnings.append(
                f"Fintual: stock price history failed for {sum(failures.values())} "
                f"of {len(targets)} stock(s) ({', '.join(sorted(failures))})"
            )
        if not_read:
            warnings.append(
                "Fintual: stock price history ran over its "
                f"{PRICE_HISTORY_BUDGET_SECONDS:.0f} s budget ({not_read} of "
                f"{len(targets)} stock(s) not read)"
            )
        if dropped:
            # Noise in the provider's series, not a gap worth a partial run.
            logger.info("Fintual: dropped %d unusable daily price bar(s)", dropped)
        logger.info(
            "Fintual: price history for %d of %d stock(s), %d daily closes",
            len(histories),
            len(targets),
            sum(len(h.points) for h in histories),
        )
        return histories, warnings

    async def _wallet_pages(
        self, client: httpx.AsyncClient, now: datetime
    ) -> AsyncIterator[WalletPage]:
        """Yield each WalletMovements page, up to the empty one that ends the
        ledger (yielded too, so the caller knows it was reached). `now` dates
        the movements still not executed (see `parse_wallet_page`).

        Every list advances by the longest one returned, in case Fintual caps
        a list below the limit asked for, but never by more than that limit:
        a list that ignored it would push the offset past rows the others
        haven't returned yet (the rows it repeats are deduplicated). Pages
        already yielded survive a later page failing.
        """
        offset = 0
        for _ in range(MAX_PAGES):
            data, errors = await self._graphql(
                client,
                "WalletMovements",
                WALLET_MOVEMENTS_QUERY,
                {"limit": PAGE_SIZE, "offset": offset},
            )
            if errors:
                # A list that failed to resolve answers null, which reads as
                # empty: the page can't be trusted.
                raise FintualStocksError(
                    f"WalletMovements answered {errors} GraphQL error(s)"
                )
            page = parse_wallet_page(data, now)
            yield page
            if not page.length:
                return
            offset += min(page.length, PAGE_SIZE)

    async def _scrape_movements(
        self, client: httpx.AsyncClient, stocks: _Stocks | None
    ) -> tuple[list[ScrapedTransaction], list[ScrapedProduct], list[str]]:
        """The USD wallet ledger as transactions, the zero-valued products of
        the stocks it shows sold out, and the warnings.

        `stocks` is what the portfolio read this cycle, None if it failed: the
        movements are read either way, but valuing sold-out stocks at zero and
        checking the ledger against the wallet's balance need it.
        """
        warnings: list[str] = []

        def warn(message: str) -> None:
            logger.warning("%s", message)
            warnings.append(message)

        entries: dict[tuple[str, str], LedgerEntry] = {}
        pending = cancelled = divestments = without_asset = 0
        skipped: Counter = Counter()
        # Reached the empty page that ends the ledger (never after a failure).
        complete = failed = False
        try:
            async for page in self._wallet_pages(client, datetime.now(timezone.utc)):
                complete = not page.length
                for entry in page.entries:
                    # A list that ignored the offset would repeat its rows.
                    entries.setdefault((entry.type.tag, entry.source_id), entry)
                pending += page.pending
                cancelled += page.cancelled
                divestments += page.divestments
                without_asset += page.without_asset
                skipped.update(page.skipped)
        except Exception as exc:
            failed = True
            logger.exception("Fintual: stock movements failed; keeping the pages read")
            warnings.append(
                f"Fintual: stock movements failed ({_stocks_failure_reason(exc)})"
            )
        if not complete and not failed:
            warn(
                f"Fintual: stopped after {MAX_PAGES} pages of stock movements; "
                "the rest were not read"
            )

        # One warning per kind of problem, with counts only.
        if skipped["not_usd"]:
            warn(f"Fintual: skipped {skipped['not_usd']} stock movement(s) not in USD")
        if skipped["unreadable"]:
            warn(f"Fintual: skipped {skipped['unreadable']} unreadable stock movement(s)")
        if divestments:
            warn(f"Fintual: skipped {divestments} stock divestment(s) (not imported)")
        if without_asset:
            warn(
                f"Fintual: {without_asset} stock movement(s) without a stock id "
                "kept on the USD wallet only"
            )
        if pending:
            # They come back once executed; only the balance check minds.
            logger.info("Fintual: %d stock movement(s) still pending", pending)
        if cancelled:
            # Never executed, so they never moved the balance: nothing to check.
            logger.info(
                "Fintual: %d stock movement(s) never executed, taken as cancelled",
                cancelled,
            )

        ledger = list(entries.values())
        sold_out: list[ScrapedProduct] = []
        if complete and stocks is not None and stocks.listed_asset_ids is not None:
            if not stocks.listed_asset_ids and shows_unsold_stocks(ledger):
                # An empty list next to stocks bought and never sold looks like
                # a list that failed to load, not a portfolio sold out.
                warn(
                    "Fintual: the stock positions list is empty while the movements "
                    "show stocks bought and never sold; no stock was valued at zero"
                )
            else:
                sold_out = sold_out_products(ledger, stocks.listed_asset_ids)
        if (
            complete
            and stocks is not None
            and stocks.wallet_balance is not None
            and not (pending or divestments or skipped)
        ):
            if ledger_balance(ledger) == stocks.wallet_balance:
                logger.info("Fintual: the USD wallet movements match its balance")
            else:
                # No amounts: the message reaches the dashboard.
                warn(
                    "Fintual: the USD wallet movements don't add up to its "
                    "balance; a movement type may be missing"
                )

        transactions = ledger_to_transactions(ledger)
        logger.info(
            "Fintual: %d stock and wallet movements (%d transactions)%s",
            len(ledger),
            len(transactions),
            f", {len(sold_out)} sold-out stock(s)" if sold_out else "",
        )
        return transactions, sold_out, warnings

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
        """One session drive: the goals, then the stocks (portfolio,
        movements, price history), then save the jar."""
        async with self._session_lock, httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=self._base_headers()
        ) as client:
            self._refreshed_this_run = False
            products, warnings = await self._goal_products(client)

            # The stocks are a separate leg: whatever fails there, the goals
            # still land and the warning records the run as partial.
            stocks: _Stocks | None = None
            try:
                stocks = await self._scrape_stocks(client)
            except Exception as exc:
                logger.exception("Fintual: stocks failed; keeping the goals")
                warnings.append(f"Fintual stocks skipped: {_stocks_failure_reason(exc)}")
            else:
                products.extend(stocks.products)
                warnings.extend(stocks.warnings)

            # So are the movements, read even when the portfolio failed (an
            # account without the brokerage has none).
            transactions: list[ScrapedTransaction] = []
            if stocks is None or stocks.has_account:
                try:
                    transactions, sold_out, movement_warnings = (
                        await self._scrape_movements(client, stocks)
                    )
                except Exception as exc:
                    logger.exception("Fintual: stock movements failed; keeping the rest")
                    warnings.append(
                        f"Fintual: stock movements failed ({_stocks_failure_reason(exc)})"
                    )
                else:
                    products.extend(sold_out)
                    warnings.extend(movement_warnings)

            # The held stocks' daily closes, last: a slow pricing service
            # must not age the session before the movements are read (a
            # stale pricing cookie is just minted again). Its own part too,
            # under a time budget: a failure costs the price history only.
            price_histories: list[ScrapedPriceHistory] = []
            if stocks is not None and stocks.held_positions:
                try:
                    price_histories, history_warnings = await self._price_histories(
                        client, list(stocks.held_positions)
                    )
                except Exception as exc:
                    logger.exception(
                        "Fintual: stock price history failed; keeping the rest"
                    )
                    warnings.append(
                        "Fintual: stock price history failed "
                        f"({_stocks_failure_reason(exc)})"
                    )
                else:
                    warnings.extend(history_warnings)

            # Keep the newest jar on disk (the stocks leg may have refreshed the
            # session or minted the pricing cookie). The refresh token itself
            # was saved the moment it rotated, so a failed save here costs at
            # most a new pricing cookie on the next run.
            try:
                self._save_session(client)
            except OSError:
                logger.warning("Fintual: could not save the session", exc_info=True)

            return _Cycle(products, transactions, warnings, price_histories)

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
        """The stock and USD wallet movements (goals expose none). The cycle
        runs here, first, so the products leg reuses it."""
        return (await self._scrape_cycle()).transactions

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
        return ProductScrapeResult(
            cycle.products, cycle.warnings, price_histories=cycle.price_histories
        )


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
