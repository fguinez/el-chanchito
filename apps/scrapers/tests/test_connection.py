"""Tests for the scraper DB connection settings.

The scrapers write data, so they must never guess where Postgres is: a missing
DATABASE_URL has to stop them before anything connects or scrapes, instead of
falling back to some localhost port.
"""

from unittest.mock import MagicMock

import pytest

import main as main_mod
from db import connection


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
