"""Skipo scraper: balances, CLP ledger and coin valuations from Skipo's API.

Skipo (skipo.com) is a Chilean crypto and fiat wallet and exchange.

Acquisition decision: rung 1 of the ladder. Skipo publishes a documented REST
API (`https://api.skipo.com/v2`) with user-issued keys, so no lower rung was
considered. Auth is a personal read-only key (`SKIPO_API_KEY`, `skp_live_...`,
held in the Keychain) sent as a bearer token. It needs the `accounts:read`
scope (balances and ledger) and `market_data:read` (asset catalog). The key
carries no IP allowlist because the scrapers run from a dynamic residential
IP. Every call is a GET; nothing here writes.

Errors are `application/problem+json` with a stable `code`. A 401 or 403
raises `SkipoAuthError` carrying only the status and that code; 429 and 5xx
are retried by `scrapers.retry`.

`scrape_products()` reads:
- `/v2/assets` (all pages) to classify each symbol as FIAT, CRYPTO,
  STABLECOIN or STOCK;
- `/v2/balances` (a bare JSON array of decimal strings): CLP becomes the
  singleton `wallet` product, always emitted, even at zero, so a drained
  wallet stops showing its old balance. Each held crypto or stablecoin
  becomes one `crypto` product per symbol (units, frozen, pending), like
  Buda. Fiat other than CLP is skipped with a warning (`WalletMetrics`
  holds whole CLP only), as are tokenized stocks (no product kind fits yet)
  and unclassified symbols. Skipped holdings are counted in the run's
  warnings, so the run is recorded partial;
- `/v2/account`, for `preferredCurrency` only. The rest of that payload is
  personal and is neither stored nor logged. When it is CLP, each held coin's
  `balancePrefCurrency / balance` is emitted as an institution-scoped
  `ScrapedExchangeRate`, so the dashboard can value coins Buda's tickers do
  not cover.

`scrape_transactions()` reads only the CLP ledger
(`/v2/ledger?assetSymbol=CLP`, paged), the single source for the CLP wallet's
cash flow. Coin ledgers are skipped for the same reason Buda skips coin
movements: `transactions.amount` keeps two decimals and a coin movement has
no CLP amount. The ledger entry id is the dedup key (`skipo_<id>`).

Verified live: the shapes of balances, assets and ledger; on the CLP ledger
(by running-balance deltas) DEPOSIT raises the balance and SELL (CLP spent
buying a coin) lowers it.
Not verified live: that BUY raises and WITHDRAWAL lowers it (assumed by
symmetry); whether `total` equals amount plus fee on outflows (every fee seen
was 0; `total` is used when readable, else `amount`); whether ledger ids stay
the same across key rotations; whether `balance` includes the frozen and
pending units; and how STOCK holdings should be modelled (none held when this
was written).
"""

import logging
import os
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from product_model import CryptoMetrics, WalletMetrics

from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedExchangeRate,
    ScrapedProduct,
    ScrapedTransaction,
)
from scrapers.retry import send_with_retry

logger = logging.getLogger(__name__)

SKIPO_BASE = "https://api.skipo.com"
LOCAL_TZ = ZoneInfo("America/Santiago")

# The CLP wallet and its ledger share this kind and currency, so a movement
# lands on the balance's product (the writer resolves by kind and currency).
WALLET_KIND = "wallet"
CRYPTO_KIND = "crypto"
CLP = "CLP"
ID_PREFIX = "skipo_"
PAGE_SIZE = 100
# Without startDate/endDate the ledger returns the whole history (verified
# live: both echo back null, and an early startDate gives the same totalItems),
# newest first. So the cap keeps the newest 5000 entries; past it the older ones
# are left out, with a warning.
MAX_PAGES = 50
# The asset catalog is small (dozens of symbols); this only bounds the loop.
MAX_ASSET_PAGES = 10

COIN_CLASSES = frozenset({"CRYPTO", "STABLECOIN"})
RATE_SOURCE = "Skipo (valorización del saldo)"

# Signed direction of each ledger type on the CLP ledger. SELL sells CLP for a
# coin (money out), BUY sells a coin for CLP (money in).
_LEDGER_SIGNS = {"DEPOSIT": 1, "BUY": 1, "WITHDRAWAL": -1, "SELL": -1}
_LEDGER_DESCRIPTIONS = {
    "DEPOSIT": "Skipo depósito",
    "WITHDRAWAL": "Skipo retiro",
    "SELL": "Skipo compra cripto",
    "BUY": "Skipo venta cripto",
}
_SUBTYPE_DESCRIPTIONS = {"INTERNAL_EARN_DISTRIBUTION": "Skipo rendimiento"}

# A problem `code` is a short snake_case token (verified live, e.g.
# `validation_error`, `unprocessable`); anything else in that field is dropped
# rather than copied into an error message.
_PROBLEM_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RATE_QUANTUM = Decimal("0.00000001")
# `exchange_rates.rate` is NUMERIC(20, 8): at most 12 integer digits.
_MAX_RATE = Decimal("1e12")


class SkipoAuthError(RuntimeError):
    """Skipo refused the API key (401/403); the message names the fix."""


class SkipoApiError(RuntimeError):
    """Skipo answered with an error, or a payload of an unexpected shape."""


def _decimal(raw: Any) -> Decimal | None:
    """A finite Decimal from a JSON number or a decimal string; else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _to_clp(raw: Any) -> int | None:
    """Whole CLP from a decimal string or number, half up; else None."""
    value = _decimal(raw)
    if value is None:
        return None
    try:
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        # More whole digits than the decimal context holds: no real amount.
        return None


def _local_date(raw: Any) -> date | None:
    """The Santiago calendar date of an ISO-8601 timestamp, or None.

    Converted before taking the date, or an evening movement in UTC lands on
    the next day. A timestamp without an offset is read as UTC.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date()


def _problem_code(resp: httpx.Response) -> str | None:
    """The `code` of a problem+json body, when it is a plain token."""
    try:
        body = resp.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    if isinstance(code, str) and _PROBLEM_CODE_RE.fullmatch(code):
        return code
    return None


def _asset_classes(assets: list[dict]) -> dict[str, str]:
    """Symbol to asset class (both upper case) from the asset catalog."""
    classes: dict[str, str] = {}
    for asset in assets:
        symbol = asset.get("assetSymbol")
        asset_class = asset.get("assetClass")
        if isinstance(symbol, str) and isinstance(asset_class, str):
            classes[symbol.upper()] = asset_class.upper()
    return classes


def _units(row: dict) -> tuple[Decimal, Decimal, Decimal] | None:
    """(balance, frozen, pending) of a balance row; None if balance is unreadable.

    A missing or unreadable frozen or pending amount counts as zero.
    """
    balance = _decimal(row.get("balance"))
    if balance is None:
        return None
    frozen = _decimal(row.get("balanceFrozen")) or Decimal(0)
    pending = _decimal(row.get("balancePending")) or Decimal(0)
    return balance, frozen, pending


def _optional_units(value: Decimal) -> float | None:
    return float(value) if value else None


def _parse_balances(
    rows: list[dict], classes: dict[str, str]
) -> tuple[list[ScrapedProduct], list[str]]:
    """Balance rows as products, plus warnings naming what was left out.

    The CLP row is always emitted (a zero balance is a real observation).
    Other rows count as held when balance, frozen or pending is non-zero;
    rows held nowhere are dropped silently. Warnings carry counts, never
    amounts.
    """
    products: list[ScrapedProduct] = []
    unreadable = other_fiat = stocks = unknown = 0

    for row in rows:
        symbol = row.get("assetSymbol")
        if not isinstance(symbol, str) or not symbol.strip():
            unreadable += 1
            continue
        symbol = symbol.strip().upper()
        units = _units(row)
        if units is None:
            logger.warning("Skipo balance for %s is unreadable, skipping", symbol)
            unreadable += 1
            continue
        balance, frozen, pending = units

        if symbol == CLP:
            pesos = _to_clp(balance)
            if pesos is None:
                logger.warning("Skipo CLP balance doesn't fit whole pesos, skipping")
                unreadable += 1
                continue
            products.append(
                ScrapedProduct(
                    institution="skipo",
                    kind=WALLET_KIND,
                    currency=CLP,
                    metrics=WalletMetrics(balance=pesos),
                )
            )
            continue
        if not (balance or frozen or pending):
            continue

        asset_class = classes.get(symbol)
        if asset_class in COIN_CLASSES:
            products.append(
                ScrapedProduct(
                    institution="skipo",
                    kind=CRYPTO_KIND,
                    currency=symbol,
                    metrics=CryptoMetrics(
                        units=float(balance),
                        frozen=_optional_units(frozen),
                        pending=_optional_units(pending),
                    ),
                )
            )
        elif asset_class == "FIAT":
            logger.warning("Skipo %s wallet skipped: only CLP wallets are modelled", symbol)
            other_fiat += 1
        elif asset_class == "STOCK":
            logger.warning("Skipo %s tokenized stock skipped: no product kind fits", symbol)
            stocks += 1
        else:
            logger.warning("Skipo %s has an unknown asset class, skipping", symbol)
            unknown += 1

    warnings: list[str] = []
    if other_fiat:
        warnings.append(f"Skipo: {other_fiat} non-CLP fiat balance(s) not imported")
    if stocks:
        warnings.append(f"Skipo: {stocks} tokenized stock holding(s) not imported")
    if unknown:
        warnings.append(f"Skipo: {unknown} balance(s) of an unknown asset class not imported")
    if unreadable:
        warnings.append(f"Skipo: {unreadable} unreadable balance row(s) skipped")
    return products, warnings


def _rate(raw: Any) -> Decimal | None:
    """A rate `exchange_rates.rate` can hold, rounded half up to 8 decimals; else None.

    The bound is checked before rounding: quantizing a value with more digits
    than the decimal context holds raises instead of rounding.
    """
    value = _decimal(raw)
    if value is None or not 0 < value < _MAX_RATE:
        return None
    try:
        rate = value.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return rate if rate > 0 else None


def _exchange_rates(rows: list[dict], classes: dict[str, str]) -> list[ScrapedExchangeRate]:
    """CLP per unit of each held coin, from its CLP-valued balance.

    Only for rows whose whole holding is `balance` (no frozen or pending
    units): whether `balancePrefCurrency` values the balance alone or the
    total is not verified, and recording no rate beats recording a wrong one.
    The caller has checked that the preferred currency is CLP.
    """
    rates: list[ScrapedExchangeRate] = []
    for row in rows:
        symbol = row.get("assetSymbol")
        if not isinstance(symbol, str):
            continue
        symbol = symbol.strip().upper()
        if symbol == CLP or classes.get(symbol) not in COIN_CLASSES:
            continue
        units = _units(row)
        value = _decimal(row.get("balancePrefCurrency"))
        if units is None or value is None:
            continue
        balance, frozen, pending = units
        if balance <= 0 or value <= 0:
            continue
        if frozen or pending:
            logger.info("Skipo %s has frozen or pending units; no rate derived", symbol)
            continue
        rate = _rate(value / balance)
        if rate is None:
            continue
        try:
            rates.append(
                ScrapedExchangeRate(
                    institution="skipo",
                    base_currency=symbol,
                    quote_currency=CLP,
                    rate=rate,
                    source=RATE_SOURCE,
                )
            )
        except ValueError:
            logger.info("Skipo %s can't be stored as a rate currency, skipping", symbol)
    return rates


def _parse_ledger_entry(entry: dict) -> ScrapedTransaction | None:
    """One CLP ledger entry as a transaction, or None to skip it.

    Amounts are positive magnitudes; the sign comes from `type`. `total` is
    preferred over `amount` when readable, on the unverified assumption that
    it includes the fee.
    """
    entry_id = entry.get("id")
    if not entry_id:
        logger.warning("Skipo ledger entry without id, skipping")
        return None
    symbol = str(entry.get("assetSymbol") or CLP).upper()
    if symbol != CLP:
        logger.warning("Skipo ledger entry %s is in %s, not CLP; skipping", entry_id, symbol)
        return None
    entry_type = str(entry.get("type") or "").upper()
    sign = _LEDGER_SIGNS.get(entry_type)
    if sign is None:
        logger.warning("Skipo ledger entry %s has an unknown type, skipping", entry_id)
        return None
    pesos = _to_clp(entry.get("total"))
    if pesos is None:
        pesos = _to_clp(entry.get("amount"))
    if pesos is None:
        logger.warning("Skipo ledger entry %s has an unreadable amount, skipping", entry_id)
        return None
    if pesos == 0:
        logger.warning("Skipo ledger entry %s has a zero amount, skipping", entry_id)
        return None
    tx_date = _local_date(entry.get("createdAt"))
    if tx_date is None:
        logger.warning("Skipo ledger entry %s has no readable date, skipping", entry_id)
        return None

    sub_type = str(entry.get("subType") or "").upper()
    description = _SUBTYPE_DESCRIPTIONS.get(sub_type) or _LEDGER_DESCRIPTIONS[entry_type]
    try:
        return ScrapedTransaction(
            institution="skipo",
            product_kind=WALLET_KIND,
            currency=CLP,
            description=description,
            amount=sign * abs(pesos),
            transaction_date=tx_date,
            external_id=f"{ID_PREFIX}{entry_id}",
            scheduled_month=date(tx_date.year, tx_date.month, 1),
        )
    except ValueError:
        # pydantic's ValidationError is a ValueError: an amount past what
        # `transactions.amount` holds, say. One bad row never fails the leg.
        logger.warning("Skipo ledger entry %s doesn't fit a transaction, skipping", entry_id)
        return None


class SkipoScraper(BaseScraper):
    method = "http_api"
    institution = "skipo"

    def __init__(self) -> None:
        self.api_key = os.environ["SKIPO_API_KEY"]
        # `scrape_transactions` returns a bare list, so its partial-coverage
        # warnings ride out with the products leg, which runs next.
        self._warnings: list[str] = []

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=SKIPO_BASE,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        what: str,
        params: dict | None = None,
    ) -> Any:
        """GET with bounded retries; raise on any error status.

        Messages end up in `scraper_runs.error_message`, so they carry only
        what was being read, the status and the problem code: never the key,
        the URL or the body.
        """
        resp = await send_with_retry(
            lambda: client.get(path, params=params), label=f"Skipo GET {what}"
        )
        if resp.status_code in (401, 403):
            code = _problem_code(resp) or "no code"
            raise SkipoAuthError(
                f"Skipo refused the API key ({resp.status_code}, {code}). Issue a "
                "read-only key with accounts:read and market_data:read and run "
                "`make secret-set KEY=SKIPO_API_KEY`."
            )
        if resp.is_error:
            code = _problem_code(resp)
            suffix = f", {code}" if code else ""
            raise SkipoApiError(f"Skipo answered {resp.status_code}{suffix} reading {what}")
        try:
            return resp.json()
        except ValueError:
            raise SkipoApiError(f"Skipo sent a non-JSON answer reading {what}") from None

    async def _paged(
        self,
        client: httpx.AsyncClient,
        path: str,
        what: str,
        params: dict,
        max_pages: int,
    ) -> AsyncIterator[list[dict]]:
        """Yield each page's `data`, following `pagination.totalPages`.

        Stops at the last page Skipo reports, else at the first short page.
        Pages already yielded survive a later page failing. Past `max_pages`
        a warning joins the run's.
        """
        for page in range(1, max_pages + 1):
            body = await self._get_json(
                client, path, what, {**params, "page": page, "limit": PAGE_SIZE}
            )
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise SkipoApiError(f"Skipo sent an unexpected payload reading {what}")
            items = [item for item in body["data"] if isinstance(item, dict)]
            yield items
            total_pages = (body.get("pagination") or {}).get("totalPages")
            if isinstance(total_pages, int) and not isinstance(total_pages, bool):
                if page >= total_pages:
                    return
            elif len(items) < PAGE_SIZE:
                return
        logger.warning("Skipo %s: stopped after %d pages; the rest were not read", what, max_pages)
        self._warnings.append(f"Skipo: {what} capped at {max_pages} pages")

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """The CLP ledger's entries, up to MAX_PAGES pages.

        A failure on a later page keeps the pages already read and leaves a
        warning; a failure before any page was read (and any auth failure)
        fails the leg.
        """
        self._warnings = []
        transactions: list[ScrapedTransaction] = []
        pages_read = 0
        async with self._client() as client:
            try:
                async for entries in self._paged(
                    client, "/v2/ledger", "the CLP ledger", {"assetSymbol": CLP}, MAX_PAGES
                ):
                    pages_read += 1
                    for entry in entries:
                        txn = _parse_ledger_entry(entry)
                        if txn is not None:
                            transactions.append(txn)
            except SkipoAuthError:
                raise
            except (httpx.HTTPError, SkipoApiError) as e:
                if not pages_read:
                    raise
                reason = str(e) or type(e).__name__
                logger.warning("Skipo: ledger stopped early, keeping what was read: %s", reason)
                self._warnings.append(f"Skipo: CLP ledger stopped early ({reason})")
        logger.info("Skipo: %d transactions", len(transactions))
        return transactions

    async def _preferred_currency(self, client: httpx.AsyncClient) -> str | None:
        """The account's preferred currency; nothing else of `/v2/account` is kept."""
        account = await self._get_json(client, "/v2/account", "the account")
        currency = account.get("preferredCurrency") if isinstance(account, dict) else None
        return currency.upper() if isinstance(currency, str) else None

    async def scrape_products(self) -> ProductScrapeResult:
        """Balances as products, coin valuations as rates, and both legs' warnings."""
        async with self._client() as client:
            assets: list[dict] = []
            async for page in self._paged(
                client, "/v2/assets", "the asset catalog", {}, MAX_ASSET_PAGES
            ):
                assets.extend(page)
            rows = await self._get_json(client, "/v2/balances", "balances")
            if not isinstance(rows, list):
                raise SkipoApiError("Skipo sent an unexpected payload reading balances")
            rows = [row for row in rows if isinstance(row, dict)]
            preferred = await self._preferred_currency(client)

        # Taken only after the fetches, so a failing fetch can't lose them.
        warnings, self._warnings = self._warnings, []
        classes = _asset_classes(assets)
        products, balance_warnings = _parse_balances(rows, classes)
        warnings.extend(balance_warnings)
        if preferred == CLP:
            rates = _exchange_rates(rows, classes)
        else:
            logger.info("Skipo preferred currency is not CLP; no coin rates derived")
            rates = []
        logger.info("Skipo: %d products, %d rates", len(products), len(rates))
        return ProductScrapeResult(products, warnings, rates)
