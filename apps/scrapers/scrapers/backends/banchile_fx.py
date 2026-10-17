"""Banco de Chile's USD card exchange rate ("Tipo de cambio").

Banco de Chile bills its credit card's USD debt in pesos at a rate of its own,
the one its international card payment page prints as "Tipo de cambio" (the
page credits Banchile Corredores). It is neither the market reference
`lib/rates` falls back to nor the observed dollar on the home's "INDICADORES"
widget (`home/indices-financieros`), so it is read here and stored as a
product-scoped `ScrapedExchangeRate` for the USD card.

Where the rate lives (read-only recon, one live session): the payment page's
display call `tarjeta-credito-digital/pago/calcular-pago-internacional`, a POST
whose body is `{idTarjeta, nombreTitular, tipoCliente}`. All three come from the
product selector (`selectorproductos/.../obtenerProductos?incluirTarjetas=true`,
a GET): the id of the first product of `tipo == "tarjeta"`, the root `nombre`,
and the first letter of that card's `tipoCliente`. Taking the first card
matches the scraper's single USD card product (credit_card, USD, no
external_ref). Both calls answer to the session cookie alone (the recon
replayed them from the portal home, `#/home`), so this surface is
fetch-only: it never routes the SPA to the payment pages
(`#/tarjeta-credito/pagos/*`) and never calls any other payment endpoint (the
same SPA service posts real payments to `autorizacion-pago-internacional` and
`validar-pago`; neither is ever touched).

Privacy: the selector answers with the customer's name, RUT and card ids, and
the rate response also carries the e-mail, an opaque payment token and the 2FA
device setup. So the whole chain runs inside ONE `page.evaluate`, which hands
Python only the rate, its date and a status code for diagnostics. Nothing else
of either payload ever reaches Python, a log line or a run warning.

Same discipline as the other BanChile surfaces: the in-page script only fetches,
a pure helper (`exchange_rate_from_result`) interprets its result, and the
surface goes through banchile_web's bounded-retry machinery, so a failure here
is a failed surface ("tipo de cambio") and a run warning, never a crash.
"""

import logging
import re
from decimal import Decimal
from typing import Optional

from scrapers.backends.banchile_web import (
    _RENDER_TIMEOUTS_MS,
    _SURFACE_ATTEMPTS,
    _budget,
    _ensure_on_portal,
    _parse_date_ddmmyyyy,
    _read_surface_with_retries,
)
from scrapers.base import ProductRef, ScrapedExchangeRate

logger = logging.getLogger(__name__)

SURFACE = "tipo de cambio"
SOURCE_LABEL = "Pago internacional de tarjeta"
# The existing BanChile USD card identity (see `card_balances_from_text`).
_CARD_USD = ProductRef(kind="credit_card", currency="USD", external_ref=None)

_PRODUCTS_PATH = (
    "/mibancochile/rest/persona/selectorproductos/selectorProductos/"
    "obtenerProductos?incluirTarjetas=true"
)
_RATE_PATH = (
    "/mibancochile/rest/persona/tarjeta-credito-digital/pago/"
    "calcular-pago-internacional"
)

# Every `return` hands back an object literal with only these keys: `status`
# (one of _STATUSES), an HTTP `code`, and on success the two rate fields. The
# selector and the rate response stay inside the page, and nothing is fetched
# unless the tab is on the portal's own host (the paths are origin-relative).
_EXCHANGE_RATE_JS = """
async ({productsPath, ratePath, timeoutMs}) => {
  if (location.hostname !== 'portalpersonas.bancochile.cl') {
    return {status: 'off_portal'};
  }
  const signal = typeof AbortSignal.timeout === 'function'
    ? AbortSignal.timeout(timeoutMs) : undefined;
  const isTimeout = (error) => Boolean(error) && error.name === 'TimeoutError';
  let selector = null;
  try {
    const response = await fetch(productsPath, {credentials: 'include', signal});
    if (!response.ok) {
      return {status: 'products_http', code: response.status};
    }
    selector = await response.json();
  } catch (error) {
    return {status: isTimeout(error) ? 'timeout' : 'products_error'};
  }
  const productos = selector && Array.isArray(selector.productos)
    ? selector.productos : [];
  const card = productos.find((producto) => producto && producto.tipo === 'tarjeta');
  if (!card || card.id === undefined || card.id === null) {
    return {status: 'no_card'};
  }
  const body = JSON.stringify({
    idTarjeta: String(card.id),
    nombreTitular: String(selector.nombre || ''),
    tipoCliente: String(card.tipoCliente || '').charAt(0),
  });
  let payment = null;
  try {
    const response = await fetch(ratePath, {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: body,
      signal,
    });
    if (!response.ok) {
      return {status: 'rate_http', code: response.status};
    }
    payment = await response.json();
  } catch (error) {
    return {status: isTimeout(error) ? 'timeout' : 'rate_error'};
  }
  const rate = payment ? payment.tipoCambio : null;
  const date = payment ? payment.fechaConsultaTipoCambio : null;
  return {
    status: 'ok',
    tipoCambio: typeof rate === 'number' || typeof rate === 'string' ? rate : null,
    fechaConsultaTipoCambio: typeof date === 'string' ? date : null,
  };
}
"""
_STATUSES = frozenset(
    {
        "ok",
        "off_portal",
        "products_http",
        "products_error",
        "no_card",
        "rate_http",
        "rate_error",
        "timeout",
    }
)

# What `exchange_rates.rate` holds: plain digits with a dot decimal, at most
# 8 decimals. The portal sends a JSON number (2 decimals observed).
_RATE_RE = re.compile(r"^\d{1,12}(\.\d{1,8})?$")


# --- Pure helpers (unit-tested; no browser) -----------------------------------


def parse_rate(raw) -> Optional[Decimal]:
    """The portal's `tipoCambio` as a positive Decimal; None when unusable.

    A JSON number or a dot-decimal string. Anything else (a bool, NaN, an
    exponent, a comma decimal, zero) is None: recording no rate beats
    recording a wrong one, since the dashboard would value the card with it.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    text = raw.strip() if isinstance(raw, str) else str(raw)
    if not _RATE_RE.fullmatch(text):
        return None
    rate = Decimal(text)
    return rate if rate > 0 else None


def exchange_rate_from_result(result) -> Optional[ScrapedExchangeRate]:
    """Build the USD card's rate from the in-page script's result, or None.

    Only `tipoCambio` and `fechaConsultaTipoCambio` are read; whatever else a
    result might carry is ignored. A missing or unreadable date leaves
    `reported_as_of` empty without failing the read.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    rate = parse_rate(result.get("tipoCambio"))
    if rate is None:
        return None
    raw_date = result.get("fechaConsultaTipoCambio")
    if not isinstance(raw_date, str):
        raw_date = None
    reported_as_of = _parse_date_ddmmyyyy(raw_date)
    return ScrapedExchangeRate(
        institution="banchile",
        base_currency="USD",
        quote_currency="CLP",
        rate=rate,
        product=_CARD_USD,
        source=SOURCE_LABEL,
        reported_as_of=reported_as_of,
    )


def describe_failure(result) -> str:
    """Why a result yielded no rate, from its status and HTTP code alone.

    Never echoes a payload value: an unknown status reads as "unexpected" and
    only an integer code is shown.
    """
    if not isinstance(result, dict):
        return "no result"
    status = result.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        return "unexpected result"
    if status == "ok":
        return "no usable rate"
    code = result.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return f"{status} {code}"
    return status


# --- Browser plumbing ---------------------------------------------------------


def _read_exchange_rate(page, attempt: int) -> list[ScrapedExchangeRate]:
    """Fetch the rate from inside the authenticated page (one attempt).

    The recon replayed both calls from the portal home (`#/home`) only; they
    are same-origin and cookie-authed, so the fence is staying on the portal
    (checked here and again inside the script). Retries start from the home,
    which `_read_surface_with_retries` recovers to.
    """
    if not _ensure_on_portal(page):
        return []
    result = page.evaluate(
        _EXCHANGE_RATE_JS,
        {
            "productsPath": _PRODUCTS_PATH,
            "ratePath": _RATE_PATH,
            "timeoutMs": _budget(_RENDER_TIMEOUTS_MS, attempt),
        },
    )
    rate = exchange_rate_from_result(result)
    if rate is None:
        logger.info(
            "BanChile: tipo de cambio not read: %s (attempt %d/%d)",
            describe_failure(result),
            attempt + 1,
            _SURFACE_ATTEMPTS,
        )
        return []
    return [rate]


def read_exchange_rate_surface(
    page,
) -> tuple[list[ScrapedExchangeRate], tuple[str, ...]]:
    """Read the USD card rate with retries: (rates, failed surfaces).

    The account always has the card, so a read that never yields the rate is a
    failure (`SURFACE` in the second element), which the scraper reports as a
    run warning.
    """
    rates = _read_surface_with_retries(page, SURFACE, _read_exchange_rate)
    if rates:
        logger.info("BanChile: tipo de cambio read for the USD card")
        return rates, ()
    return [], (SURFACE,)
