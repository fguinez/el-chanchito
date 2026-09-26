"""MercadoPago scraper: movements from notification e-mails, balance from the REST API.

Movements come from the email backend with MP-specific patterns (needs the
EMAIL_IMAP_* inbox). Those e-mails don't carry a reliable balance, so when
MERCADOPAGO_ACCESS_TOKEN is set the wallet balance is read from Mercado Pago's
official REST API instead:

    GET /users/me                                -> `id` of the token's owner
    GET /users/{id}/mercadopago_account/balance  -> `available_balance`

The token is a production access token from an application the user creates in
their own Mercado Pago developer panel; it acts on the owner's account, so it is
a secret kept in the Keychain like the other credentials. Without a token the
scraper stays email-only and reports no products.
"""

import os

import httpx

from product_model import WalletMetrics

from scrapers.backends.email import (
    MERCHANT_END,
    EmailPattern,
    anchored,
    fetch_transactions_for_pattern,
)
from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)

MP_API_BASE = "https://api.mercadopago.com"

PATTERN = EmailPattern(
    institution="mercadopago",
    product_kind="wallet",
    sender_domains=["mercadopago", "mercadolibre"],
    subject_contains=[
        "pago", "compra", "transferencia", "pagaste", "recibiste", "dinero",
        "devolvimos", "devolucion", "reembolso",
    ],
    amount_rules=[
        anchored(r"pagaste|compraste|compra\s+de|pago\s+de|transferiste|enviaste"),
        anchored(
            r"recibiste|ingresaste|te\s+(?:transfirieron|enviaron|devolvimos|reembolsamos)"
            r"|devoluci[oó]n|reembolso",
            income=True,
        ),
        anchored(r"monto|total"),
    ],
    merchant_patterns=[
        # The amount is optional ("Pagaste en X" / "Pagaste $ N a X"), but the
        # verb is not: a bare "a" or "en" anywhere in the body never anchors.
        rf"\b(?:pagaste|compraste|transferiste|enviaste)\s+(?:\$\s*[\d.,]+\s+)?"
        rf"(?:en|a)\s+(.+?){MERCHANT_END}",
        rf"\brecibiste\s+\$\s*[\d.,]+\s+de\s+(.+?){MERCHANT_END}",
        rf"\bcompra\s+en\s+(.+?){MERCHANT_END}",
        rf"\bcomercio:?\s+(.+?){MERCHANT_END}",
    ],
    # "ingresaste" is a top-up: money into the wallet from a bank account.
    income_keywords=[
        "recibiste dinero", "recibiste una transferencia", "recibiste un pago",
        "ingresaste", "te transfirieron", "te devolvimos", "devolucion",
        "reembolso", "anulad", "revers",
    ],
)


def _parse_balance(payload: dict) -> int:
    """Return the available balance of a balance payload, in whole CLP.

    `available_balance` is what the app shows as "Disponible". JSON may carry
    it as an int or a float, so it is rounded. Errors name keys, never values.
    """
    currency = payload.get("currency_id")
    if currency is not None and currency != "CLP":
        raise ValueError(f"MercadoPago balance currency is {currency!r}, expected 'CLP'")

    value = payload.get("available_balance")
    # bool is an int subclass, but `true` is not a balance.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "MercadoPago balance payload has no numeric 'available_balance' "
            f"(keys: {sorted(payload)})"
        )
    return int(round(value))


async def _get_json(client: httpx.AsyncClient, path: str, what: str) -> dict:
    """GET `path` and return its JSON body.

    Failures raise with a message that never carries the token, the URL (it
    holds the user id) or the response body: it ends up in
    scraper_runs.error_message and on the dashboard.
    """
    try:
        resp = await client.get(path)
    except httpx.TransportError as e:
        raise RuntimeError(
            f"MercadoPago {what} request failed ({type(e).__name__})"
        ) from e

    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"MercadoPago API rejected the access token (HTTP {resp.status_code}); "
            "check MERCADOPAGO_ACCESS_TOKEN"
        )
    if not resp.is_success:
        raise RuntimeError(f"MercadoPago {what} request failed (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError as e:
        raise ValueError(f"MercadoPago {what} response is not JSON") from e
    if not isinstance(body, dict):
        raise ValueError(f"MercadoPago {what} response is not a JSON object")
    return body


async def fetch_wallet_balance(
    access_token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    """Read the wallet's available balance (whole CLP) from the REST API.

    `transport` lets tests plug in an `httpx.MockTransport`.
    """
    async with httpx.AsyncClient(
        base_url=MP_API_BASE,
        timeout=30.0,
        headers={"Authorization": f"Bearer {access_token}"},
        transport=transport,
    ) as client:
        me = await _get_json(client, "/users/me", "user")
        user_id = me.get("id")
        if user_id is None:
            raise ValueError(
                f"MercadoPago /users/me payload has no 'id' (keys: {sorted(me)})"
            )

        payload = await _get_json(
            client, f"/users/{user_id}/mercadopago_account/balance", "balance"
        )

    return _parse_balance(payload)


class MercadoPagoScraper(BaseScraper):
    institution = "mercadopago"

    def __init__(self, read_email: bool = True) -> None:
        self.read_email = read_email
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))
        self.access_token = os.environ.get("MERCADOPAGO_ACCESS_TOKEN") or None

    @property
    def method(self) -> str:
        # With a token the REST API is the authoritative balance source.
        return "http_api" if self.access_token else "email"

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        if not self.read_email:
            return []
        return await fetch_transactions_for_pattern(PATTERN, self.lookback_days)

    async def scrape_products(self) -> ProductScrapeResult:
        """Emit the wallet balance from the REST API; nothing without a token."""
        if not self.access_token:
            return ProductScrapeResult([])

        balance = await fetch_wallet_balance(self.access_token)
        # CLP and no external_ref: the same singleton wallet product the e-mail
        # transactions and the V009 backfill resolve to.
        return ProductScrapeResult(
            [
                ScrapedProduct(
                    institution="mercadopago",
                    kind="wallet",
                    metrics=WalletMetrics(balance=balance),
                )
            ]
        )
