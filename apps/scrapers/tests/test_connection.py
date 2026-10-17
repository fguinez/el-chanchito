"""Tests for the DB pool settings and the transient-error retry around writes.

The scrapers write data, so they must never guess where Postgres is: a missing
DATABASE_URL has to stop them before anything connects or scrapes, instead of
falling back to some localhost port. No database: the pool class is replaced
by a recorder or mock, and the retried functions are plain callables that
raise psycopg errors on cue.
"""

from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import PoolTimeout

import main as main_mod
from db import connection
from db.connection import is_transient_db_error, with_db_retry
from scrapers import retry


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(retry, "BASE_DELAY_SECONDS", 0.0)


@pytest.fixture
def pool_cls(monkeypatch):
    """Replace ConnectionPool with a mock and reset the cached pool around the test."""
    mock = MagicMock()
    monkeypatch.setattr(connection, "ConnectionPool", mock)
    monkeypatch.setattr(connection, "_pool", None)
    return mock


class TestDatabaseUrl:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_raises(self, monkeypatch, value):
        """Unset, empty or blank DATABASE_URL is an error, not a default."""
        if value is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", value)

        with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
            connection.database_url()

    def test_set_is_returned(self, monkeypatch):
        """A configured DATABASE_URL is returned."""
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db.example:6543/x")

        assert connection.database_url() == "postgres://u:p@db.example:6543/x"


class TestIsTransientDbError:
    @pytest.mark.parametrize(
        ("exc", "transient"),
        [
            (psycopg.OperationalError("server closed the connection"), True),
            (PoolTimeout("couldn't get a connection"), True),
            (psycopg.errors.SerializationFailure("could not serialize"), True),
            (psycopg.IntegrityError("duplicate key"), False),
            (psycopg.DataError("value too long"), False),
            (ValueError("not a db error"), False),
        ],
    )
    def test_only_connection_level_failures_are_transient(self, exc, transient):
        assert is_transient_db_error(exc) is transient


class TestWithDbRetry:
    def test_a_dropped_connection_is_retried(self):
        calls = []

        @with_db_retry
        def write(value):
            calls.append(value)
            if len(calls) == 1:
                raise psycopg.OperationalError("server closed the connection")
            return value

        assert (write("row"), calls) == ("row", ["row", "row"])

    def test_an_integrity_error_is_not_retried(self):
        calls = []

        @with_db_retry
        def write():
            calls.append(1)
            raise psycopg.IntegrityError("duplicate key")

        with pytest.raises(psycopg.IntegrityError):
            write()

        assert len(calls) == 1


class TestGetPool:
    def test_missing_url_never_builds_a_pool(self, monkeypatch, pool_cls):
        """get_pool raises before any connection is attempted."""
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with pytest.raises(RuntimeError):
            connection.get_pool()

        pool_cls.assert_not_called()

    def test_pool_uses_configured_url(self, monkeypatch, pool_cls):
        """The pool is built from DATABASE_URL."""
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db.example:6543/x")

        connection.get_pool()

        assert pool_cls.call_args.args == ("postgres://u:p@db.example:6543/x",)

    def test_connections_are_checked_before_being_handed_out(
        self, monkeypatch, pool_cls
    ):
        """A connection a Postgres restart killed is replaced, not used."""
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db.example:6543/x")

        connection.get_pool()

        kwargs = pool_cls.call_args.kwargs
        assert kwargs["check"] is pool_cls.check_connection
        assert kwargs["max_lifetime"] == connection._MAX_LIFETIME_SECONDS
        assert kwargs["timeout"] == connection._CONNECTION_TIMEOUT_SECONDS


class TestEntryPoints:
    @pytest.mark.parametrize("entry", ["main_once", "main_scheduled"])
    def test_missing_url_exits_before_scraping(self, monkeypatch, entry):
        """Both service modes exit 1 before building any scraper."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(
            main_mod,
            "build_scrapers",
            MagicMock(side_effect=AssertionError("scrapers were built")),
        )

        with pytest.raises(SystemExit) as exc:
            getattr(main_mod, entry)()

        assert exc.value.code == 1
