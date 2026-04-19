"""Database connection pool for scrapers."""

import os

import psycopg
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get(
            "DATABASE_URL",
            "postgres://finance:finance@localhost:5432/finance",
        )
        _pool = ConnectionPool(dsn, min_size=1, max_size=3)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
