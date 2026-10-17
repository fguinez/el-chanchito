"""Tests for the bounded-retry helpers (scrapers/retry.py).

Waits go through an injected `sleep` that only records the delays, so nothing
here sleeps for real and no request leaves the process.
"""

import asyncio

import httpx
import pytest

from scrapers.retry import backoff_delay, retry_sync, send_with_retry


class _Transient(Exception):
    pass


def _is_transient(exc):
    return isinstance(exc, _Transient)


class _Calls:
    """An operation that raises, or returns, the scripted outcomes in order."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.count = 0

    def __call__(self):
        outcome = self._outcomes[self.count]
        self.count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TestBackoffDelay:
    @pytest.mark.parametrize(
        ("attempt", "delay"), [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 10.0)]
    )
    def test_doubles_up_to_the_cap(self, attempt, delay):
        assert backoff_delay(attempt) == delay


class TestRetrySync:
    def test_a_transient_failure_is_retried(self):
        operation = _Calls(_Transient("reset"), "ok")
        delays = []

        result = retry_sync(
            operation, is_transient=_is_transient, label="op", sleep=delays.append
        )

        assert (result, operation.count, delays) == ("ok", 2, [1.0])

    def test_a_permanent_failure_is_not_retried(self):
        operation = _Calls(ValueError("bad row"), "ok")

        with pytest.raises(ValueError):
            retry_sync(
                operation, is_transient=_is_transient, label="op", sleep=lambda _: None
            )

        assert operation.count == 1

    def test_gives_up_after_the_last_attempt(self):
        operation = _Calls(_Transient("1"), _Transient("2"), _Transient("3"), "ok")
        delays = []

        with pytest.raises(_Transient, match="3"):
            retry_sync(
                operation,
                is_transient=_is_transient,
                label="op",
                attempts=3,
                sleep=delays.append,
            )

        assert (operation.count, delays) == (3, [1.0, 2.0])


def _send(*outcomes):
    """An async `send` that yields the scripted responses or errors in order."""
    calls = _Calls(*outcomes)

    async def send():
        return calls()

    return send, calls


def _run(send):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    response = asyncio.run(send_with_retry(send, label="GET /x", sleep=sleep))
    return response, delays


class TestSendWithRetry:
    @pytest.mark.parametrize(
        "first",
        [
            httpx.Response(503),
            httpx.Response(429),
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("timed out"),
        ],
    )
    def test_transient_failures_are_retried(self, first):
        send, calls = _send(first, httpx.Response(200))

        response, delays = _run(send)

        assert (response.status_code, calls.count, delays) == (200, 2, [1.0])

    def test_a_client_error_is_returned_at_once(self):
        """A 401 is the caller's to handle (Fintual re-logs in), not a retry."""
        send, calls = _send(httpx.Response(401), httpx.Response(200))

        response, _ = _run(send)

        assert (response.status_code, calls.count) == (401, 1)

    def test_the_last_transient_response_is_returned_not_raised(self):
        send, calls = _send(httpx.Response(502), httpx.Response(502), httpx.Response(503))

        response, delays = _run(send)

        assert (response.status_code, calls.count, delays) == (503, 3, [1.0, 2.0])

    def test_a_transport_error_on_the_last_attempt_propagates(self):
        send, _ = _send(
            httpx.ConnectError("1"), httpx.ConnectError("2"), httpx.ConnectError("3")
        )

        with pytest.raises(httpx.ConnectError, match="3"):
            _run(send)
