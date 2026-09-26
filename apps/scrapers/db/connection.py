"""Database connection pool for scrapers."""

import functools
import os
from collections.abc import Callable
from typing import ParamSpec, TypeVar

import psycopg
from psycopg_pool import ConnectionPool

from scrapers.retry import retry_sync

P = ParamSpec("P")
T = TypeVar("T")

# Recycle connections well before anything between us and Postgres drops them.
_MAX_LIFETIME_SECONDS = 30 * 60
# How long a writer waits for a connection before PoolTimeout. Short, so a
# Postgres that's down fails each attempt fast and with_db_retry can move on.
_CONNECTION_TIMEOUT_SECONDS = 10
DB_ATTEMPTS = 3

_pool: ConnectionPool | None = None


def database_url() -> str:
    """Return DATABASE_URL; no fallback, as a guess could hit the wrong Postgres."""
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Run through `make` (scrapers-once, "
            "scrapers-start, dev), copy .env.example to .env, or export it, "
            "e.g. postgres://finance:finance@localhost:5435/finance"
        )
    return dsn


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # `check` pings each connection before handing it out, so the ones a
        # Postgres restart killed are replaced instead of failing a write.
        _pool = ConnectionPool(
            database_url(),
            min_size=1,
            max_size=3,
            open=True,
            check=ConnectionPool.check_connection,
            max_lifetime=_MAX_LIFETIME_SECONDS,
            timeout=_CONNECTION_TIMEOUT_SECONDS,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def is_transient_db_error(exc: BaseException) -> bool:
    """A dropped or refused connection, a pool timeout, or a serialization or
    deadlock rollback: failures another attempt on a fresh connection can
    clear. Data and integrity errors are not retried."""
    return isinstance(exc, psycopg.OperationalError)


def with_db_retry(fn: Callable[P, T]) -> Callable[P, T]:
    """Retry `fn` on transient DB errors, at most DB_ATTEMPTS times.

    Only for functions that are safe to repeat, and that roll a failed attempt
    back whole or, like `upsert_transactions`, commit in idempotent steps. An
    error `fn` catches itself (a per-row failure in `upsert_transactions`)
    never reaches the retry.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return retry_sync(
            lambda: fn(*args, **kwargs),
            is_transient=is_transient_db_error,
            label=fn.__name__,
            attempts=DB_ATTEMPTS,
        )

    return wrapper
