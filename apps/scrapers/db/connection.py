"""Database connection pool for scrapers."""

import os

from psycopg_pool import ConnectionPool

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
        _pool = ConnectionPool(database_url(), min_size=1, max_size=3)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
