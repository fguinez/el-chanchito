"""Bounded retries with exponential backoff for transient failures.

Only idempotent work goes through here: HTTP GETs, and DB writes that run in
one transaction each (a failed attempt rolls back whole, and the writers are
upserts, so another attempt can't double-write). Logins and other
non-idempotent requests are deliberately never retried: a repeated Fintual
`initiate_login` would e-mail a second 2FA code, and bank logins are
rate-limited.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 10.0

# What a server answers when it's overloaded or briefly down, as opposed to
# when the request itself is wrong.
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def backoff_delay(attempt: int) -> float:
    """Seconds to wait after failed attempt `attempt` (1-based): 1, 2, 4, ...
    capped at MAX_DELAY_SECONDS."""
    return min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1))


def _log_retry(label: str, attempt: int, attempts: int, reason: str) -> float:
    delay = backoff_delay(attempt)
    logger.warning(
        "%s failed (attempt %d/%d): %s; retrying in %.0fs",
        label,
        attempt,
        attempts,
        reason,
        delay,
    )
    return delay


def retry_sync(
    operation: Callable[[], T],
    *,
    is_transient: Callable[[BaseException], bool],
    label: str,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `operation` up to `attempts` times while it raises transient errors.

    A non-transient error propagates at once; the last attempt's error
    propagates whatever it is.
    """
    for attempt in range(1, attempts):
        try:
            return operation()
        except Exception as exc:
            if not is_transient(exc):
                raise
            sleep(_log_retry(label, attempt, attempts, str(exc) or type(exc).__name__))
    return operation()


async def send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    label: str,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Send an idempotent request, retrying transport errors and 429/5xx.

    `send` is called afresh for every attempt, so anything computed per request
    (Buda's nonce-bearing signature) is recomputed. The last attempt's response
    is returned whatever its status, so the caller's own status handling
    (`raise_for_status`, Fintual's 401 re-login) applies unchanged.
    """
    for attempt in range(1, attempts):
        try:
            response = await send()
        except httpx.TransportError as exc:
            reason = str(exc) or type(exc).__name__
        else:
            if response.status_code not in TRANSIENT_HTTP_STATUSES:
                return response
            reason = f"HTTP {response.status_code}"
        await sleep(_log_retry(label, attempt, attempts, reason))
    return await send()
