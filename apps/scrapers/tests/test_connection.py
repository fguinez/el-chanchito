"""Tests for the DB pool settings and the transient-error retry around writes.

No database: the pool class is replaced by a recorder, and the retried
functions are plain callables that raise psycopg errors on cue.
"""

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from db import connection
from db.connection import is_transient_db_error, with_db_retry
from scrapers import retry


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(retry, "BASE_DELAY_SECONDS", 0.0)


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
    def test_connections_are_checked_before_being_handed_out(self, monkeypatch):
        """A connection a Postgres restart killed is replaced, not used."""
        created = []

        class _RecordingPool:
            check_connection = object()

            def __init__(self, dsn, **kwargs):
                created.append(kwargs)

        monkeypatch.setattr(connection, "ConnectionPool", _RecordingPool)
        monkeypatch.setattr(connection, "_pool", None)

        connection.get_pool()

        assert created[0]["check"] is _RecordingPool.check_connection
