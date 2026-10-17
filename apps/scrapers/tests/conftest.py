"""Fixtures for tests that run the DB writer against a real PostgreSQL.

`TEST_DATABASE_URL` names a *disposable* server (`make test-db` starts one);
the database in the DSN is only the maintenance connection. The session
builds a template database from every migration, exactly as migrate.mjs
applies them, and each test gets a fresh clone of it that is dropped at
teardown. Only `chanchito_test_*` databases this module created are ever
dropped. Without `TEST_DATABASE_URL` the DB tests are skipped; with it set but
the server unreachable they fail.
"""

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from db import connection

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[3] / "packages" / "db-schema" / "migrations"
)

# Same DDL as packages/db-schema/migrate.mjs.
_MIGRATIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS _migrations (
      id SERIAL PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      applied_at TIMESTAMPTZ DEFAULT now()
    )
"""


_DB_FIXTURES = {"db_conn", "unmigrated_db"}


def pytest_collection_modifyitems(items):
    """Mark every test that uses the database, so `-m "not db"` deselects them."""
    for item in items:
        if _DB_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.db)


def _admin(dsn: str) -> psycopg.Connection:
    """An autocommit connection for CREATE/DROP DATABASE."""
    return psycopg.connect(dsn, autocommit=True, connect_timeout=10)


def _create_database(dsn: str, name: str, template: str | None = None) -> None:
    query = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
    if template is not None:
        query += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
    with _admin(dsn) as conn:
        conn.execute(query)


def _drop_database(dsn: str, name: str) -> None:
    with _admin(dsn) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def apply_migrations(dsn: str, before: str | None = None) -> None:
    """Apply the pending migrations in order, one transaction each, like
    migrate.mjs. `before` stops at the first file whose name sorts at or after
    it (e.g. "V023"), so a test can stage data under an older schema."""
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not paths:
        raise FileNotFoundError(f"No migrations found in {MIGRATIONS_DIR}")
    if before is not None:
        paths = [path for path in paths if path.name < before]
    with psycopg.connect(dsn) as conn:
        conn.execute(_MIGRATIONS_TABLE)
        conn.commit()
        applied = {
            row[0] for row in conn.execute("SELECT name FROM _migrations")
        }
        conn.commit()
        for path in paths:
            if path.name in applied:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO _migrations (name) VALUES (%s)", (path.name,)
                )


@pytest.fixture(scope="session")
def _template_db():
    """(maintenance DSN, name of a fully migrated template database)."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn and os.environ.get("CI"):
        pytest.fail(
            "TEST_DATABASE_URL is not set in CI, so the DB tests would skip",
            pytrace=False,
        )
    if not dsn:
        pytest.skip(
            "TEST_DATABASE_URL is not set; run `make test-db` to run the DB tests "
            "against a throwaway PostgreSQL"
        )
    try:
        _admin(dsn).close()
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"TEST_DATABASE_URL is set but the server is unreachable: {exc}",
            pytrace=False,
        )

    name = f"chanchito_test_tpl_{uuid4().hex[:12]}"
    _create_database(dsn, name)
    try:
        # The connection is closed on return: TEMPLATE refuses a source
        # database that has other sessions.
        apply_migrations(make_conninfo(dsn, dbname=name))
        yield dsn, name
    finally:
        _drop_database(dsn, name)


@pytest.fixture
def db_conn(_template_db, monkeypatch):
    """An autocommit, dict-row connection to a fresh migrated database.

    `DATABASE_URL` points at the same database and the writer's pool is reset
    around the test, so `db.writer` functions write where the test reads.
    """
    dsn, template = _template_db
    name = f"chanchito_test_{uuid4().hex[:12]}"
    _create_database(dsn, name, template=template)
    test_dsn = make_conninfo(dsn, dbname=name)
    try:
        connection.close_pool()
        monkeypatch.setenv("DATABASE_URL", test_dsn)
        with psycopg.connect(
            test_dsn, autocommit=True, row_factory=dict_row
        ) as conn:
            yield conn
    finally:
        connection.close_pool()
        _drop_database(dsn, name)


@pytest.fixture
def unmigrated_db(_template_db, monkeypatch):
    """DSN of a fresh, EMPTY database for tests of a migration itself.

    The test applies migrations with `apply_migrations(dsn, before=...)`, stages
    rows, then applies the rest. `DATABASE_URL` points at it as in `db_conn`.
    """
    dsn, _ = _template_db
    name = f"chanchito_test_{uuid4().hex[:12]}"
    _create_database(dsn, name)
    test_dsn = make_conninfo(dsn, dbname=name)
    try:
        connection.close_pool()
        monkeypatch.setenv("DATABASE_URL", test_dsn)
        yield test_dsn
    finally:
        connection.close_pool()
        _drop_database(dsn, name)
