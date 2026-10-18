"""Tests for the Fintual scraper: per-goal products, stock positions and
session handling.

These never hit the network: `httpx.AsyncClient` is swapped for a fake that
answers each Fintual URL from a canned queue (or a real client over
`httpx.MockTransport` for the stocks leg), and the session cache lives in a
per-test tmp file. Every cookie value, ticker and figure is synthetic.
"""

import asyncio
import json
import os
import re
import stat
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

os.environ.setdefault("FINTUAL_EMAIL", "test@example.com")
os.environ.setdefault("FINTUAL_TOKEN", "test_password")

from scrapers.institutions import fintual as fintual_mod
from scrapers.institutions.fintual import (
    APP_URL,
    FINALIZE_LOGIN_URL,
    GOALS_URL,
    GRAPHQL_URL,
    INITIATE_LOGIN_URL,
    SIGN_IN_URL,
    STOCKS_WALLET_NAME,
    FintualScraper,
    FintualSessionError,
    StockPrice,
    details_query,
    parse_prices,
    stock_attributes,
    stock_metrics,
)

# Captured before any test swaps the module's AsyncClient for a fake.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

# What StocksPortfolio is taken to answer for an account that never opened the
# brokerage (this shape is unverified live).
_NO_STOCKS = {"data": {"stocksWallet": None, "stocksPositionsV2": None}}


@pytest.fixture(autouse=True)
def session_file(monkeypatch, tmp_path):
    """Point the session cache at a tmp file so no test touches a real one."""
    path = tmp_path / "fintual_session.json"
    monkeypatch.setenv("FINTUAL_SESSION_FILE", str(path))
    return path


def _goal(goal_id, **attributes):
    """Build one JSON:API goal resource as returned by GET /api/goals."""
    return {"id": goal_id, "type": "goal", "attributes": attributes}


class _FakeClient:
    """Async-context stand-in for httpx.AsyncClient with a canned GET; the
    stocks query finds no brokerage account."""

    def __init__(self, payload):
        self._payload = payload
        self.cookies = httpx.Cookies()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = self._payload
        return response

    async def post(self, url, **kwargs):
        return httpx.Response(200, json=_NO_STOCKS, request=httpx.Request("POST", url))


class TestScrapeProducts:
    def _scrape(self, monkeypatch, goals):
        """Run scrape_products() against a canned goals payload."""
        monkeypatch.setattr(
            FintualScraper, "_load_session", lambda self, client: True
        )
        monkeypatch.setattr(
            fintual_mod.httpx,
            "AsyncClient",
            lambda **kwargs: _FakeClient({"data": goals}),
        )
        return asyncio.run(FintualScraper().scrape_products())

    def test_one_product_per_goal(self, monkeypatch):
        """Two goals become two products keyed by the JSON:API resource id."""
        goals = [
            _goal(
                "34567",
                name="Risky Norris",
                nav=12345678.33,
                deposited=12000000.0,
                profit=345678.33,
            ),
            _goal(
                "34811",
                name="Conservative Clooney",
                nav=2500000.0,
                deposited=2600000.0,
                profit=-100000.0,
            ),
        ]

        result = self._scrape(monkeypatch, goals)

        assert result.warnings == []
        assert len(result.products) == 2
        first, second = result.products
        assert first.institution == "fintual"
        assert first.kind == "investment"
        assert first.currency == "CLP"
        assert first.external_ref == "34567"
        assert first.name == "Risky Norris"
        assert first.metrics.nav == 12345678  # rounded to whole CLP
        assert first.metrics.deposited == 12000000.0
        assert first.metrics.profit == 345678.33
        assert second.external_ref == "34811"
        assert second.name == "Conservative Clooney"
        assert second.metrics.nav == 2500000
        assert second.metrics.profit == -100000.0

    def test_missing_deposited_and_profit_are_omitted(self, monkeypatch):
        """A goal without deposited/profit still emits; the fields stay None."""
        goals = [_goal(777, name="Solo", nav=100000)]

        products = self._scrape(monkeypatch, goals).products

        assert len(products) == 1
        product = products[0]
        assert product.external_ref == "777"  # non-string ids are coerced
        assert product.metrics.nav == 100000
        assert product.metrics.deposited is None
        assert product.metrics.profit is None

    def test_null_nav_emits_zero(self, monkeypatch):
        """An explicit `"nav": null` emits the goal valued at 0, not an error."""
        goals = [_goal("901", name="Fresh Goal", nav=None)]

        products = self._scrape(monkeypatch, goals).products

        assert len(products) == 1
        product = products[0]
        assert product.external_ref == "901"
        assert product.metrics.nav == 0

    def test_goal_without_id_is_skipped(self, monkeypatch):
        """Entries with a missing or null id are skipped; other goals still emit."""
        goals = [
            {"type": "goal", "attributes": {"name": "No Id", "nav": 5000.0}},
            _goal(None, name="Null Id", nav=7000.0),
            _goal("34567", name="Risky Norris", nav=12345678.33),
        ]

        result = self._scrape(monkeypatch, goals)

        assert len(result.products) == 1
        assert result.products[0].external_ref == "34567"
        assert result.products[0].metrics.nav == 12345678
        assert result.warnings == ["Fintual: skipped 2 goal(s) without an id"]

    def test_empty_goals_list(self, monkeypatch):
        """No goals means no products, not an error."""
        result = self._scrape(monkeypatch, [])

        assert result.products == []
        assert result.warnings == []


class _RoutedClient:
    """Async-context httpx stand-in that answers each URL from its own queue.

    `routes` maps a URL to (status, payload) answers given in order, the last
    one repeating; an exception instance as an answer is raised instead.
    `set_cookies` maps a URL to the cookies a 2xx answer from it sets, like a
    Set-Cookie header would. Unless routed, the stocks query finds no
    brokerage account.
    """

    def __init__(self, routes, set_cookies=None):
        routes = {GRAPHQL_URL: [(200, _NO_STOCKS)], **routes}
        self._routes = {url: list(answers) for url, answers in routes.items()}
        self._set_cookies = set_cookies or {}
        self.cookies = httpx.Cookies()
        self.calls: list[str] = []
        self.first_called_at: dict[str, datetime] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _answer(self, url):
        self.calls.append(url)
        self.first_called_at.setdefault(url, datetime.now(timezone.utc))
        answers = self._routes[url]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, payload = answer
        if 200 <= status < 300:
            for name, value in self._set_cookies.get(url, {}).items():
                self.cookies.set(name, value, domain="fintual.cl")
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    async def get(self, url, **kwargs):
        return self._answer(url)

    async def post(self, url, **kwargs):
        return self._answer(url)


_ONE_GOAL = {"data": [_goal("34567", name="Risky Norris", nav=12345678.33)]}

# A sign-in that needs the e-mailed code, then succeeds.
_LOGIN_ROUTES = {
    SIGN_IN_URL: [(200, {})],
    INITIATE_LOGIN_URL: [(201, {})],
    FINALIZE_LOGIN_URL: [(200, {})],
}


def _install(monkeypatch, client):
    monkeypatch.setattr(fintual_mod.httpx, "AsyncClient", lambda **kwargs: client)
    return client


def _write_session(path, **cookies):
    path.write_text(json.dumps(cookies))


def _imap_is_the_fintual_account(monkeypatch):
    monkeypatch.setenv("FINTUAL_EMAIL", "test@example.com")
    monkeypatch.setenv("EMAIL_IMAP_USER", "test@example.com")


def _marker(session_file):
    return session_file.with_name(session_file.name + ".login-failed")


_LOGIN_URLS = {SIGN_IN_URL, INITIATE_LOGIN_URL, FINALIZE_LOGIN_URL}


class _SingleUseRefreshServer:
    """Fintual stand-in whose refresh token works once, like the real one.

    /app/goals trades the current auth_refresh_token for a new token pair;
    /api/goals answers 200 only to the newest monolith_token. `client()` hands
    out separate clients (own jars), and every request yields to the loop so
    concurrent runs interleave.
    """

    def __init__(self, refresh):
        self.refresh = refresh
        self.token = None
        self.refreshes = 0

    def client(self, **kwargs):
        server = self

        class Client:
            def __init__(self):
                self.cookies = httpx.Cookies()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **kwargs):
                await asyncio.sleep(0)
                return server.answer(self.cookies, url)

            async def post(self, url, **kwargs):
                await asyncio.sleep(0)
                return server.answer(self.cookies, url)

        return Client()

    def answer(self, cookies, url):
        status, payload = 401, {}
        if url == GRAPHQL_URL:
            status, payload = 200, _NO_STOCKS
        if url == APP_URL and cookies.get("auth_refresh_token") == self.refresh:
            self.refreshes += 1
            self.token = f"synthetic-token-{self.refreshes}"
            self.refresh = f"synthetic-refresh-{self.refreshes + 1}"
            cookies.set("monolith_token", self.token, domain="fintual.cl")
            cookies.set("auth_refresh_token", self.refresh, domain="fintual.cl")
            status = 200
        elif url == GOALS_URL and self.token and cookies.get("monolith_token") == self.token:
            status, payload = 200, _ONE_GOAL
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))


class TestSessionRefresh:
    def test_an_expired_token_is_refreshed_without_a_login(
        self, monkeypatch, session_file
    ):
        """goals 401 -> /app/goals re-issues the token -> goals 200, no 2FA;
        the rotated refresh token is saved at once, privately."""
        _write_session(
            session_file,
            monolith_token="synthetic-expired",
            auth_refresh_token="synthetic-refresh-1",
        )
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        client = _install(
            monkeypatch,
            _RoutedClient(
                {GOALS_URL: [(401, {}), (200, _ONE_GOAL)], APP_URL: [(200, {})]},
                set_cookies={
                    APP_URL: {
                        "monolith_token": "synthetic-fresh",
                        "auth_refresh_token": "synthetic-refresh-2",
                    }
                },
            ),
        )

        result = asyncio.run(FintualScraper().scrape_products())

        login.assert_not_awaited()
        assert client.calls == [GOALS_URL, APP_URL, GOALS_URL, GRAPHQL_URL]
        assert [p.external_ref for p in result.products] == ["34567"]
        assert json.loads(session_file.read_text()) == {
            "monolith_token": "synthetic-fresh",
            "auth_refresh_token": "synthetic-refresh-2",
        }
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600

    def test_failed_refresh_relogs_in_via_imap(self, monkeypatch, session_file):
        """When FINTUAL_EMAIL == EMAIL_IMAP_USER and the refresh token is dead
        too, the scraper signs in again via the e-mailed code and retries."""
        _imap_is_the_fintual_account(monkeypatch)
        _write_session(session_file, auth_refresh_token="synthetic-refresh-1")
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        client = _install(
            monkeypatch,
            _RoutedClient({GOALS_URL: [(401, {}), (200, _ONE_GOAL)], APP_URL: [(401, {})]}),
        )

        result = asyncio.run(FintualScraper().scrape_products())

        login.assert_awaited_once()
        assert client.calls == [GOALS_URL, APP_URL, GOALS_URL, GRAPHQL_URL]
        assert [p.external_ref for p in result.products] == ["34567"]

    def test_failed_refresh_without_imap_match_keeps_manual_error(
        self, monkeypatch, session_file
    ):
        """With a different IMAP user, a dead session surfaces the
        manual-login error instead of attempting an automatic sign-in."""
        monkeypatch.delenv("EMAIL_IMAP_USER", raising=False)
        _write_session(session_file, auth_refresh_token="synthetic-refresh-1")
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        _install(monkeypatch, _RoutedClient({GOALS_URL: [(401, {})], APP_URL: [(401, {})]}))

        with pytest.raises(FintualSessionError, match="fintual-login"):
            asyncio.run(FintualScraper().scrape_products())

        login.assert_not_awaited()

    def test_the_rotated_token_is_saved_before_anything_else_can_fail(
        self, monkeypatch, session_file
    ):
        """The old refresh token is spent once /app/goals answers, so the new
        one must already be on disk when the next request fails."""
        monkeypatch.setattr("scrapers.retry.BASE_DELAY_SECONDS", 0.0)
        _write_session(session_file, auth_refresh_token="synthetic-refresh-1")
        _install(
            monkeypatch,
            _RoutedClient(
                {GOALS_URL: [(401, {}), (500, {})], APP_URL: [(200, {})]},
                set_cookies={APP_URL: {"auth_refresh_token": "synthetic-refresh-2"}},
            ),
        )

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(FintualScraper().scrape_products())

        assert json.loads(session_file.read_text()) == {
            "auth_refresh_token": "synthetic-refresh-2"
        }
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600

    def test_concurrent_runs_spend_the_refresh_token_once(
        self, monkeypatch, session_file
    ):
        """A manual refresh overlapping the startup scrape waits for it and
        reuses the rotated cache instead of spending the old token again."""
        monkeypatch.delenv("EMAIL_IMAP_USER", raising=False)
        _write_session(
            session_file,
            monolith_token="synthetic-expired",
            auth_refresh_token="synthetic-refresh-1",
        )
        server = _SingleUseRefreshServer("synthetic-refresh-1")
        monkeypatch.setattr(fintual_mod.httpx, "AsyncClient", server.client)
        scraper = FintualScraper()

        async def both():
            return await asyncio.gather(scraper.scrape_products(), scraper.scrape_products())

        results = asyncio.run(both())

        assert server.refreshes == 1
        assert [len(r.products) for r in results] == [1, 1]
        assert json.loads(session_file.read_text())["auth_refresh_token"] == (
            "synthetic-refresh-2"
        )

    def test_a_run_never_signs_in_twice(self, monkeypatch):
        """If the session a run just signed in for is refused, the run fails
        instead of mailing a second code."""
        _imap_is_the_fintual_account(monkeypatch)
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        _install(monkeypatch, _RoutedClient({GOALS_URL: [(401, {})], APP_URL: [(401, {})]}))

        with pytest.raises(FintualSessionError, match="just issued"):
            asyncio.run(FintualScraper().scrape_products())

        login.assert_awaited_once()

    def test_a_transient_503_is_retried_without_relogging_in(self, monkeypatch):
        """A 5xx is the server's hiccup, not an expired session."""
        monkeypatch.setattr("scrapers.retry.BASE_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(FintualScraper, "_load_session", lambda self, client: True)
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        client = _install(
            monkeypatch,
            _RoutedClient({GOALS_URL: [(503, {}), (200, _ONE_GOAL)]}),
        )

        result = asyncio.run(FintualScraper().scrape_products())

        login.assert_not_awaited()
        assert client.calls == [GOALS_URL, GOALS_URL, GRAPHQL_URL]
        assert len(result.products) == 1

    def test_a_refresh_outage_fails_the_run_without_a_login(
        self, monkeypatch, session_file
    ):
        """A 5xx from /app/goals that outlives the retries is an outage, not a
        dead refresh token: the run fails and no code is e-mailed."""
        monkeypatch.setattr("scrapers.retry.BASE_DELAY_SECONDS", 0.0)
        _imap_is_the_fintual_account(monkeypatch)
        monkeypatch.setattr(
            fintual_mod, "fetch_latest_code", AsyncMock(return_value="000000")
        )
        _write_session(session_file, auth_refresh_token="synthetic-refresh-1")
        client = _install(
            monkeypatch,
            _RoutedClient({**_LOGIN_ROUTES, GOALS_URL: [(401, {})], APP_URL: [(503, {})]}),
        )

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(FintualScraper().scrape_products())

        assert not _LOGIN_URLS & set(client.calls)
        assert not _marker(session_file).exists()


class TestAutoLogin:
    def test_missing_session_with_imap_match_relogs_in(self, monkeypatch):
        """No cached session + matching IMAP user logs in automatically."""
        _imap_is_the_fintual_account(monkeypatch)
        monkeypatch.setattr(FintualScraper, "_load_session", lambda self, client: False)
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        _install(monkeypatch, _RoutedClient({GOALS_URL: [(200, _ONE_GOAL)]}))

        asyncio.run(FintualScraper().scrape_products())

        login.assert_awaited_once()

    def test_only_a_code_sent_after_the_login_started_is_accepted(
        self, monkeypatch, session_file
    ):
        """The code lookup ignores e-mails from before initiate_login, so a
        previous login's spent code is never sent to finalize."""
        _imap_is_the_fintual_account(monkeypatch)
        lookups = []

        async def fake_fetch_latest_code(**kwargs):
            lookups.append(kwargs)
            return "000000"

        monkeypatch.setattr(fintual_mod, "fetch_latest_code", fake_fetch_latest_code)
        client = _install(
            monkeypatch,
            _RoutedClient(
                {**_LOGIN_ROUTES, GOALS_URL: [(200, _ONE_GOAL)]},
                set_cookies={FINALIZE_LOGIN_URL: {"auth_refresh_token": "synthetic-refresh-1"}},
            ),
        )

        before = datetime.now(timezone.utc)

        result = asyncio.run(FintualScraper().scrape_products())

        initiated = client.first_called_at[INITIATE_LOGIN_URL]
        skew = fintual_mod._CODE_CLOCK_SKEW
        assert len(lookups) == 1
        assert before - skew <= lookups[0]["received_after"] <= initiated - skew
        assert [p.external_ref for p in result.products] == ["34567"]
        assert json.loads(session_file.read_text()) == {
            "auth_refresh_token": "synthetic-refresh-1"
        }


class TestLoginFailureGuard:
    def test_a_rejected_code_writes_the_marker_and_a_clean_error(
        self, monkeypatch, session_file
    ):
        """A failed automatic sign-in leaves the marker (private) and raises a
        short message with no URL or response body."""
        _imap_is_the_fintual_account(monkeypatch)
        monkeypatch.setattr(
            fintual_mod, "fetch_latest_code", AsyncMock(return_value="000000")
        )
        _install(
            monkeypatch,
            _RoutedClient(
                {**_LOGIN_ROUTES, FINALIZE_LOGIN_URL: [(401, {"error": "synthetic body"})]}
            ),
        )
        marker = session_file.with_name(session_file.name + ".login-failed")

        with pytest.raises(FintualSessionError) as caught:
            asyncio.run(FintualScraper().scrape_products())

        message = str(caught.value)
        assert "HTTP 401" in message and "fintual-login" in message
        assert "://" not in message and "fintual.cl" not in message
        assert "synthetic body" not in message
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    def test_a_code_that_never_arrives_also_trips_the_guard(
        self, monkeypatch, session_file
    ):
        """The inbox is polled a bounded number of times, then the login fails."""
        _imap_is_the_fintual_account(monkeypatch)
        monkeypatch.setattr(fintual_mod, "_CODE_POLL_INTERVAL_SECONDS", 0)
        lookup = AsyncMock(return_value=None)
        monkeypatch.setattr(fintual_mod, "fetch_latest_code", lookup)
        client = _install(monkeypatch, _RoutedClient(_LOGIN_ROUTES))

        with pytest.raises(FintualSessionError, match="did not reach the IMAP inbox"):
            asyncio.run(FintualScraper().scrape_products())

        assert lookup.await_count == fintual_mod._CODE_POLL_ATTEMPTS
        assert FINALIZE_LOGIN_URL not in client.calls
        assert _marker(session_file).exists()

    def test_a_transport_error_trips_the_guard_with_only_its_type(
        self, monkeypatch, session_file
    ):
        """httpx error texts can carry URLs, so only the exception type shows."""
        _imap_is_the_fintual_account(monkeypatch)
        failure = httpx.ConnectError(
            f"synthetic failure reaching {INITIATE_LOGIN_URL}",
            request=httpx.Request("POST", INITIATE_LOGIN_URL),
        )
        _install(monkeypatch, _RoutedClient({**_LOGIN_ROUTES, INITIATE_LOGIN_URL: [failure]}))

        with pytest.raises(FintualSessionError) as caught:
            asyncio.run(FintualScraper().scrape_products())

        message = str(caught.value)
        assert "(ConnectError)" in message and "fintual-login" in message
        assert "://" not in message and "synthetic failure" not in message
        assert _marker(session_file).exists()

    def test_after_a_failure_the_next_run_does_not_sign_in(
        self, monkeypatch, session_file
    ):
        """With the marker present, no login is attempted (no new code e-mail)
        until `make fintual-login` succeeds."""
        _imap_is_the_fintual_account(monkeypatch)
        _marker(session_file).write_text("")
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        client = _install(monkeypatch, _RoutedClient({GOALS_URL: [(200, _ONE_GOAL)]}))

        with pytest.raises(FintualSessionError, match="failed earlier"):
            asyncio.run(FintualScraper().scrape_products())

        login.assert_not_awaited()
        assert client.calls == []

    def test_the_marker_does_not_block_a_working_refresh(
        self, monkeypatch, session_file
    ):
        """The guard only stops logins: a refresh still serves the run."""
        _imap_is_the_fintual_account(monkeypatch)
        _write_session(session_file, auth_refresh_token="synthetic-refresh-1")
        _marker(session_file).write_text("synthetic-marker")
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        _install(
            monkeypatch,
            _RoutedClient({GOALS_URL: [(401, {}), (200, _ONE_GOAL)], APP_URL: [(200, {})]}),
        )

        result = asyncio.run(FintualScraper().scrape_products())

        login.assert_not_awaited()
        assert len(result.products) == 1
        assert _marker(session_file).read_text() == "synthetic-marker"

    def test_the_marker_and_a_dead_refresh_token_fail_without_a_login(
        self, monkeypatch, session_file
    ):
        """No request reaches the login endpoints, so no code is e-mailed."""
        _imap_is_the_fintual_account(monkeypatch)
        _write_session(session_file, auth_refresh_token="synthetic-refresh-1")
        _marker(session_file).write_text("")
        client = _install(
            monkeypatch,
            _RoutedClient({**_LOGIN_ROUTES, GOALS_URL: [(401, {})], APP_URL: [(401, {})]}),
        )

        with pytest.raises(FintualSessionError, match="failed earlier"):
            asyncio.run(FintualScraper().scrape_products())

        assert client.calls == [GOALS_URL, APP_URL]
        assert not _LOGIN_URLS & set(client.calls)

    def test_a_successful_login_clears_the_marker(self, monkeypatch, session_file):
        """The manual `make fintual-login` path re-arms automatic sign-in."""
        marker = session_file.with_name(session_file.name + ".login-failed")
        marker.write_text("")
        _install(
            monkeypatch,
            _RoutedClient(
                _LOGIN_ROUTES,
                set_cookies={FINALIZE_LOGIN_URL: {"auth_refresh_token": "synthetic-refresh-1"}},
            ),
        )

        asyncio.run(FintualScraper().login(lambda: "000000"))

        assert not marker.exists()
        assert json.loads(session_file.read_text()) == {
            "auth_refresh_token": "synthetic-refresh-1"
        }


class TestRealCookieJar:
    """httpx's own cookie jar, fed real Set-Cookie headers over MockTransport."""

    def _refresh(self, handler):
        async def go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True
            ) as client:
                scraper = FintualScraper()
                scraper._load_session(client)
                return await scraper._refresh_session(client)

        return asyncio.run(go())

    def test_the_new_refresh_token_is_saved_not_the_loaded_one(self, session_file):
        """Host-only cookies from /app/goals replace the ones loaded from the
        cache (no duplicate names), so the rotated value is what gets saved."""
        _write_session(
            session_file,
            monolith_token="synthetic-expired",
            auth_refresh_token="synthetic-refresh-1",
        )
        sent = []

        def handler(request):
            sent.append(request.headers.get("cookie"))
            return httpx.Response(
                200,
                headers=[
                    ("set-cookie", "monolith_token=synthetic-fresh; Path=/"),
                    ("set-cookie", "auth_refresh_token=synthetic-refresh-2; Path=/"),
                ],
                text="<html></html>",
            )

        refreshed = self._refresh(handler)

        assert refreshed is True
        assert "auth_refresh_token=synthetic-refresh-1" in sent[0]
        assert json.loads(session_file.read_text()) == {
            "monolith_token": "synthetic-fresh",
            "auth_refresh_token": "synthetic-refresh-2",
        }

    def test_a_redirect_to_sign_in_is_not_a_refresh(self, session_file):
        """It ends in a 200 too, but must neither count nor rewrite the cache."""
        _write_session(session_file, auth_refresh_token="synthetic-refresh-1")
        cached = session_file.read_text()

        def handler(request):
            if request.url.path.startswith("/app"):
                return httpx.Response(302, headers={"location": SIGN_IN_URL})
            return httpx.Response(200, text="<html></html>")

        refreshed = self._refresh(handler)

        assert refreshed is False
        assert session_file.read_text() == cached


class TestLoadSession:
    @pytest.mark.parametrize(
        ("cookies", "hit"),
        [
            ({"monolith_token": "synthetic"}, False),
            ({"auth_refresh_token": "synthetic"}, True),
            ({"monolith_token": "synthetic", "auth_refresh_token": "synthetic"}, True),
        ],
    )
    def test_a_cache_hit_needs_the_refresh_token(self, session_file, cookies, hit):
        """Only the refresh token keeps a session alive past a few minutes."""
        _write_session(session_file, **cookies)
        client = SimpleNamespace(cookies=httpx.Cookies())

        loaded = FintualScraper()._load_session(client)

        assert loaded is hit
        assert dict(client.cookies) == cookies


# -- stocks -----------------------------------------------------------------

_JWT_COOKIE = fintual_mod._PRICING_JWT_COOKIE
_QUOTED_AT = datetime(2026, 1, 2, 15, 4, 5, tzinfo=timezone.utc)


def _position(asset_id, symbol, name, shares):
    """One StocksPortfolio position (stocksPositionsV2.positions[])."""
    return {
        "id": f"9{asset_id}",
        "shares": shares,
        "asset": {"id": asset_id, "symbol": symbol, "name": name},
    }


_ACME = _position("000101", "ACME", "Acme Corp", 2.5)
_GLBX = _position("000102", "GLBX", "Globex Corporation", 4.0)
_INITK_SOLD = _position("000103", "INITK", "Initech Inc", 0.0)


def _portfolio(*positions, wallet=1234.56, currency="USD"):
    """A StocksPortfolio answer; `wallet=None` means no cash wallet."""
    stocks_wallet = None
    if wallet is not None:
        stocks_wallet = {
            "id": "000001",
            "balance": {"amount": wallet, "currency": currency},
            "updatedAt": "2026-01-02T15:04:05Z",
        }
    return {
        "data": {
            "stocksWallet": stocks_wallet,
            "stocksPositionsV2": {"positions": list(positions)},
        }
    }


def _performance(subscriptions=300.0, redemptions=50.0, realized=10.0, dividends=1.5):
    """stocksPositionPerformance totals; the defaults are the spec's example."""
    return {
        "id": "000001",
        "realizedPl": realized,
        "totalCompletedSubscriptions": subscriptions,
        "totalCompletedRedemptions": redemptions,
        "totalReceivedDividends": dividends,
        "shares": 2.5,
    }


def _profile(asset_id, symbol, name):
    """A stocksAsset profile with one emoji tag and one without."""
    return {
        "id": asset_id,
        "name": name,
        "symbol": symbol,
        "overview": f"{name} es una empresa sintetica de prueba.",
        "assetTags": [
            {"id": "1", "emoji": "💻", "displayName": "Tecnología"},
            {"id": "2", "emoji": "", "displayName": "Sintetica"},
        ],
    }


_DETAILS = {
    "ACME": (_performance(), _profile("000101", "ACME", "Acme Corp")),
    "GLBX": (
        _performance(subscriptions=999.99, redemptions=0.0, realized=0.0, dividends=0.0),
        _profile("000102", "GLBX", "Globex Corporation"),
    ),
    "INITK": (
        _performance(subscriptions=300.0, redemptions=320.0, realized=20.0),
        _profile("000103", "INITK", "Initech Inc"),
    ),
}
_PRICES = {"ACME": 100.0, "GLBX": 250.5}


class _FintualStocks:
    """fintual.cl stand-in for a whole run, behind a real httpx client over
    MockTransport so the cookie jar and query strings behave for real.

    StocksDetails answers each `$s{i}` from `details[symbol]` (performance,
    profile) under the aliases p{i}/a{i}. The pricing service answers 401
    until a request carries the cookie /auth/jwt mints; with
    `jwt_accepted=False` it never takes it. `answers` maps a GraphQL operation
    or a path to (status, payload) answers given in order, the last repeating.
    """

    def __init__(
        self, portfolio, details=_DETAILS, prices=_PRICES, *, jwt_accepted=True, answers=None
    ):
        self.portfolio = portfolio
        self.details = details
        self.prices = prices
        self.jwt_accepted = jwt_accepted
        self.answers = {key: list(queue) for key, queue in (answers or {}).items()}
        self.requests: list[httpx.Request] = []
        self.operations: list[str] = []
        self.details_variables = None

    def _canned(self, key):
        queue = self.answers.get(key)
        if not queue:
            return None
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=payload)

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/gql/":
            body = json.loads(request.content)
            operation = re.match(r"query (\w+)", body["query"]).group(1)
            self.operations.append(operation)
            return self._canned(operation) or self._graphql(operation, body["variables"])
        canned = self._canned(path)
        if canned is not None:
            return canned
        if path == "/api/goals":
            return httpx.Response(200, json=_ONE_GOAL)
        if path == "/app/goals":
            return httpx.Response(
                200,
                headers=[("set-cookie", "auth_refresh_token=synthetic-refresh-2; Path=/")],
                text="<html></html>",
            )
        if path == "/auth/jwt":
            token = "synthetic-jwt" if self.jwt_accepted else "synthetic-stale-jwt"
            return httpx.Response(
                201,
                headers=[("set-cookie", f"{_JWT_COOKIE}={token}; Path=/")],
                json={"message": "synthetic"},
            )
        if path == "/stocks-pricing-cl/current-prices":
            if f"{_JWT_COOKIE}=synthetic-jwt" not in request.headers.get("cookie", ""):
                return httpx.Response(401, json={"title": "Unauthorized"})
            symbols = request.url.params["symbols"].split(",")
            return httpx.Response(
                200,
                json={
                    "prices": [
                        {
                            "symbol": symbol,
                            "price": self.prices[symbol],
                            "price_datetime": "2026-01-02T15:04:05Z",
                            "provider": "synthetic",
                        }
                        for symbol in symbols
                        if symbol in self.prices
                    ]
                },
            )
        return httpx.Response(404, json={})

    def _graphql(self, operation, variables):
        if operation == "StocksPortfolio":
            return httpx.Response(200, json=self.portfolio)
        self.details_variables = variables
        data = {}
        for name, symbol in variables.items():
            performance, profile = self.details.get(symbol, (None, None))
            data[f"p{name[1:]}"] = performance
            data[f"a{name[1:]}"] = profile
        return httpx.Response(200, json={"data": data})

    def paths(self):
        return [request.url.path for request in self.requests]


def _run_stocks(monkeypatch, session_file, server, **cookies):
    """Run scrape_products() against `server` with a cached session."""
    _write_session(session_file, auth_refresh_token="synthetic-refresh-1", **cookies)
    monkeypatch.setattr(
        fintual_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(server.handler), **kwargs
        ),
    )
    return asyncio.run(FintualScraper().scrape_products())


def _by_ref(products):
    return {(p.kind, p.currency, p.external_ref): p for p in products}


class TestStockMetrics:
    def test_the_web_apps_formulas(self):
        """The spec's worked example: 2.5 shares at 100.0, 300.0 in, 50.0 out,
        10.0 realized."""
        metrics = stock_metrics(
            Decimal("2.5"), StockPrice(Decimal("100.0"), _QUOTED_AT), _performance()
        )

        assert metrics.nav == 250.0
        assert metrics.deposited == 250.0
        assert metrics.profit == 0.0
        assert metrics.unrealized_profit == -10.0
        assert metrics.average_cost == 104.0
        assert metrics.unrealized_profit_pct == -3.85  # -10 / 260, to 2 places
        assert metrics.realized_profit == 10.0
        assert metrics.dividends == 1.5
        assert (metrics.units, metrics.unit_price) == (2.5, 100.0)
        assert metrics.unit_price_as_of == _QUOTED_AT

    def test_a_sold_out_position_is_worth_zero_without_a_price(self):
        """No shares, no price needed: nav 0 and the per-share figures 0."""
        performance = _performance(subscriptions=300.0, redemptions=320.0, realized=20.0)

        metrics = stock_metrics(Decimal("0"), None, performance)

        assert metrics.nav == 0.0
        assert metrics.deposited == -20.0
        assert metrics.profit == 20.0
        assert (metrics.unrealized_profit, metrics.average_cost) == (0.0, 0.0)
        assert metrics.unrealized_profit_pct == 0.0
        assert (metrics.unit_price, metrics.unit_price_as_of) == (None, None)

    def test_figures_round_half_up_to_cents(self):
        """0.25 x 12.34 = 3.085 rounds to 3.09; a float would give 3.08."""
        performance = _performance(subscriptions=3.0, redemptions=0.0, realized=0.0)

        metrics = stock_metrics(Decimal("0.25"), StockPrice(Decimal("12.34"), None), performance)

        assert metrics.nav == 3.09
        assert metrics.profit == 0.09

    def test_profit_is_derived_from_the_rounded_value(self):
        """profit = nav - deposited to the cent: 3.09 - 5.00 = -1.91, where
        rounding the exact -1.915 on its own would give -1.92."""
        performance = _performance(subscriptions=5.0, redemptions=0.0, realized=0.0)

        metrics = stock_metrics(Decimal("0.25"), StockPrice(Decimal("12.34"), None), performance)

        assert (metrics.nav, metrics.deposited, metrics.profit) == (3.09, 5.0, -1.91)
        assert metrics.unrealized_profit == -1.91
        assert metrics.average_cost == 20.0  # the held shares' 5.00 cost per share

    def test_a_quote_under_a_dollar_keeps_its_average_cost(self):
        """The average cost keeps 4 places and the percent 2."""
        performance = _performance(subscriptions=2.0, redemptions=0.0, realized=0.0)

        metrics = stock_metrics(Decimal("3"), StockPrice(Decimal("0.5"), None), performance)

        assert metrics.average_cost == 0.6667
        assert metrics.unrealized_profit_pct == -25.0

    @pytest.mark.parametrize(
        ("shares", "price", "performance"),
        [
            pytest.param(
                Decimal("2.5"),
                StockPrice(Decimal("100.0"), None),
                {"totalCompletedSubscriptions": 300.0, "totalCompletedRedemptions": 50.0},
                id="missing-realized",
            ),
            pytest.param(Decimal("2.5"), None, _performance(), id="held-without-price"),
        ],
    )
    def test_unusable_inputs_raise(self, shares, price, performance):
        with pytest.raises(ValueError):
            stock_metrics(shares, price, performance)


class TestStockParsers:
    def test_prices_without_a_positive_number_are_left_out(self):
        """So a position is never valued at 0 by accident."""
        payload = {
            "prices": [
                {"symbol": "ACME", "price": 100.0, "price_datetime": "2026-01-02T15:04:05Z"},
                {"symbol": "GLBX", "price": 0},
                {"symbol": "INITK", "price": "250.5"},
                {"price": 250.5},
            ]
        }

        prices = parse_prices(payload)

        assert prices == {"ACME": StockPrice(Decimal("100.0"), _QUOTED_AT)}

    def test_the_details_query_aliases_each_position(self):
        query = details_query(2)

        assert query.startswith("query StocksDetails($s0: String!, $s1: String!) {")
        assert "p0: stocksPositionPerformance(assetSymbol: $s0) {" in query
        assert "a1: stocksAsset(assetSymbol: $s1) {" in query

    def test_null_tags_keep_the_stored_ones(self):
        """A null assetTags leaves tags None (the writer keeps the stored
        list); an empty list replaces it; entries that aren't tags are skipped."""
        profile = _profile("000101", "ACME", "Acme Corp")

        assert stock_attributes("ACME", {**profile, "assetTags": None}).tags is None
        assert stock_attributes("ACME", {**profile, "assetTags": []}).tags == []
        assert stock_attributes(
            "ACME", {**profile, "assetTags": ["x", None, *profile["assetTags"]]}
        ).tags == ["💻 Tecnología", "Sintetica"]


class TestStocksLeg:
    def test_goals_positions_and_wallet_become_products(self, monkeypatch, session_file):
        """One CLP product per goal, one USD investment per position keyed by
        asset id, and the USD cash wallet; no warnings."""
        server = _FintualStocks(_portfolio(_ACME, _GLBX))

        result = _run_stocks(monkeypatch, session_file, server)

        assert result.warnings == []
        products = _by_ref(result.products)
        assert set(products) == {
            ("investment", "CLP", "34567"),
            ("investment", "USD", "stock:000101"),
            ("investment", "USD", "stock:000102"),
            ("wallet", "USD", None),
        }
        acme = products[("investment", "USD", "stock:000101")]
        assert acme.institution == "fintual"
        assert acme.name == "Acme Corp"
        assert (acme.metrics.nav, acme.metrics.deposited, acme.metrics.profit) == (
            250.0,
            250.0,
            0.0,
        )
        assert acme.metrics.average_cost == 104.0
        assert acme.metrics.unit_price_as_of == _QUOTED_AT
        assert acme.attributes.ticker == "ACME"
        assert acme.attributes.description == "Acme Corp es una empresa sintetica de prueba."
        assert acme.attributes.tags == ["💻 Tecnología", "Sintetica"]
        glbx = products[("investment", "USD", "stock:000102")]
        assert (glbx.name, glbx.metrics.nav, glbx.metrics.profit) == (
            "Globex Corporation",
            1002.0,
            2.01,
        )
        wallet = products[("wallet", "USD", None)]
        assert (wallet.name, wallet.metrics.balance) == (STOCKS_WALLET_NAME, 1234.56)
        assert server.details_variables == {"s0": "ACME", "s1": "GLBX"}

    def test_the_pricing_token_is_minted_on_a_401_and_saved(self, monkeypatch, session_file):
        """current-prices 401 -> POST /auth/jwt -> retry 200, as the web app
        does; the new cookie lands in the private session cache."""
        server = _FintualStocks(_portfolio(_ACME, _GLBX))

        result = _run_stocks(monkeypatch, session_file, server)

        assert result.warnings == []
        assert server.paths() == [
            "/api/goals",
            "/gql/",
            "/gql/",
            "/stocks-pricing-cl/current-prices",
            "/auth/jwt",
            "/stocks-pricing-cl/current-prices",
        ]
        assert server.operations == ["StocksPortfolio", "StocksDetails"]
        assert server.requests[-1].url.params["symbols"] == "ACME,GLBX"
        assert json.loads(session_file.read_text()) == {
            "auth_refresh_token": "synthetic-refresh-1",
            _JWT_COOKIE: "synthetic-jwt",
        }
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600

    def test_a_cached_pricing_token_is_reused(self, monkeypatch, session_file):
        """A token saved by an earlier run spares the mint."""
        server = _FintualStocks(_portfolio(_ACME))

        result = _run_stocks(
            monkeypatch, session_file, server, **{_JWT_COOKIE: "synthetic-jwt"}
        )

        assert result.warnings == []
        assert "/auth/jwt" not in server.paths()
        assert ("investment", "USD", "stock:000101") in _by_ref(result.products)

    def test_a_refused_pricing_token_keeps_the_goals(self, monkeypatch, session_file):
        """A second 401 fails the positions only: the goals and the wallet
        land and a short warning (no URL, no ticker) records the run as
        partial."""
        server = _FintualStocks(_portfolio(_ACME, _GLBX), jwt_accepted=False)

        result = _run_stocks(monkeypatch, session_file, server)

        assert set(_by_ref(result.products)) == {
            ("investment", "CLP", "34567"),
            ("wallet", "USD", None),
        }
        assert result.warnings == [
            "Fintual stock positions skipped: the stocks pricing service refused "
            "a fresh token (HTTP 401)"
        ]
        assert server.paths().count("/auth/jwt") == 1
        assert "://" not in result.warnings[0] and "ACME" not in result.warnings[0]

    @pytest.mark.parametrize(
        "portfolio",
        [
            pytest.param(_NO_STOCKS, id="null-wallet-and-positions"),
            pytest.param(_portfolio(wallet=None), id="no-wallet-empty-positions"),
            pytest.param({**_NO_STOCKS, "errors": []}, id="empty-errors-list"),
        ],
    )
    def test_an_account_without_stocks_yields_only_the_goals(
        self, monkeypatch, session_file, portfolio
    ):
        """Never having opened the brokerage (nothing resolves, no GraphQL
        error) is no failure: no stock products, no warning, and no further
        stocks request."""
        server = _FintualStocks(portfolio)

        result = _run_stocks(monkeypatch, session_file, server)

        assert [p.external_ref for p in result.products] == ["34567"]
        assert result.warnings == []
        assert server.paths() == ["/api/goals", "/gql/"]

    @pytest.mark.parametrize(
        "portfolio",
        [
            pytest.param(
                {**_NO_STOCKS, "errors": [{"message": "synthetic error"}]},
                id="resolver-error",
            ),
            pytest.param(
                {"data": None, "errors": [{"message": "synthetic error"}]},
                id="null-data",
            ),
        ],
    )
    def test_nothing_resolved_with_errors_fails_the_stocks_leg(
        self, monkeypatch, session_file, portfolio
    ):
        """Nothing resolving next to a GraphQL error is a failed leg, not an
        account without stocks: the goals land and a warning records the run
        as partial, so earlier stock products never freeze unnoticed."""
        server = _FintualStocks(portfolio)

        result = _run_stocks(monkeypatch, session_file, server)

        assert [p.external_ref for p in result.products] == ["34567"]
        assert result.warnings == ["Fintual: skipped the stocks leg (GraphQL error)"]
        assert server.paths() == ["/api/goals", "/gql/"]

    @pytest.mark.parametrize(
        ("answers", "reason"),
        [
            pytest.param(
                {"StocksPortfolio": [(200, {"errors": [{"message": "synthetic"}]})]},
                "StocksPortfolio was refused by the GraphQL server",
                id="portfolio-refused",
            ),
            pytest.param(
                {"StocksPortfolio": [(200, {"data": {"stocksPositionsV2": ["x"]}})]},
                "AttributeError",
                id="schema-surprise",
            ),
        ],
    )
    def test_a_failed_stocks_query_keeps_the_goals(
        self, monkeypatch, session_file, answers, reason
    ):
        """GraphQL refusals and schema surprises cost the stocks only; an
        unexpected error shows nothing but its type."""
        server = _FintualStocks(_portfolio(_ACME), answers=answers)

        result = _run_stocks(monkeypatch, session_file, server)

        assert [p.external_ref for p in result.products] == ["34567"]
        assert result.warnings == [f"Fintual stocks skipped: {reason}"]

    @pytest.mark.parametrize(
        ("answers", "reason"),
        [
            pytest.param(
                {"StocksDetails": [(200, {"errors": [{"message": "synthetic"}]})]},
                "StocksDetails was refused by the GraphQL server",
                id="details-refused",
            ),
            pytest.param(
                {"StocksDetails": [(200, {"data": None, "errors": [{"message": "x"}]})]},
                "StocksDetails answered no data",
                id="details-null-data",
            ),
            pytest.param(
                {"StocksDetails": [(500, {})]},
                "StocksDetails answered HTTP 500",
                id="details-outage",
            ),
            pytest.param(
                {"/stocks-pricing-cl/current-prices": [(500, {})]},
                "the stocks pricing service answered HTTP 500",
                id="pricing-outage",
            ),
            pytest.param(
                {"/auth/jwt": [(403, {})]},
                "the stocks pricing token request answered HTTP 403",
                id="pricing-token-refused",
            ),
        ],
    )
    def test_a_failed_positions_query_keeps_the_wallet(
        self, monkeypatch, session_file, answers, reason
    ):
        """A failure on the positions' own requests costs the positions only:
        the goals and the USD wallet still land."""
        monkeypatch.setattr("scrapers.retry.BASE_DELAY_SECONDS", 0.0)
        server = _FintualStocks(_portfolio(_ACME), answers=answers)

        result = _run_stocks(monkeypatch, session_file, server)

        assert set(_by_ref(result.products)) == {
            ("investment", "CLP", "34567"),
            ("wallet", "USD", None),
        }
        assert result.warnings == [f"Fintual stock positions skipped: {reason}"]

    @pytest.mark.parametrize(
        ("portfolio", "kept", "warnings"),
        [
            pytest.param(
                {
                    "data": {**_portfolio()["data"], "stocksPositionsV2": None},
                    "errors": [{"message": "synthetic error"}],
                },
                {("wallet", "USD", None)},
                ["Fintual: skipped the stock positions (GraphQL error)"],
                id="positions-failed",
            ),
            pytest.param(
                {
                    "data": {
                        **_portfolio()["data"],
                        "stocksPositionsV2": {"positions": None},
                    },
                    "errors": [{"message": "synthetic error"}],
                },
                {("wallet", "USD", None)},
                ["Fintual: skipped the stock positions (GraphQL error)"],
                id="positions-list-failed",
            ),
            pytest.param(
                {
                    "data": {**_portfolio(_ACME)["data"], "stocksWallet": None},
                    "errors": [{"message": "synthetic error"}],
                },
                {("investment", "USD", "stock:000101")},
                ["Fintual: skipped the stocks wallet (GraphQL error)"],
                id="wallet-failed",
            ),
            pytest.param(
                {"data": {**_portfolio()["data"], "stocksPositionsV2": None}},
                {("wallet", "USD", None)},
                [],
                id="no-positions-without-errors",
            ),
        ],
    )
    def test_a_field_that_failed_to_resolve_is_a_warning(
        self, monkeypatch, session_file, portfolio, kept, warnings
    ):
        """With the account there, a null field next to GraphQL errors failed
        to resolve: what did answer lands, and a warning keeps the run from
        passing as clean while the rest sits frozen."""
        server = _FintualStocks(portfolio)

        result = _run_stocks(monkeypatch, session_file, server)

        assert set(_by_ref(result.products)) == {("investment", "CLP", "34567"), *kept}
        assert result.warnings == warnings

    def test_a_sold_out_position_is_valued_at_zero(self, monkeypatch, session_file):
        """It stays a product at nav 0, so it drops out of net worth, and needs
        no price (none is asked for)."""
        server = _FintualStocks(_portfolio(_INITK_SOLD))

        result = _run_stocks(monkeypatch, session_file, server)

        assert result.warnings == []
        initk = _by_ref(result.products)[("investment", "USD", "stock:000103")]
        assert (initk.metrics.nav, initk.metrics.units, initk.metrics.unit_price) == (
            0.0,
            0.0,
            None,
        )
        assert initk.metrics.profit == 20.0
        assert "/stocks-pricing-cl/current-prices" not in server.paths()

    def test_a_position_without_a_price_is_skipped_with_a_warning(
        self, monkeypatch, session_file
    ):
        """Never valued at 0 silently: left out, counted in a warning."""
        server = _FintualStocks(_portfolio(_ACME, _GLBX), prices={"ACME": 100.0})

        result = _run_stocks(monkeypatch, session_file, server)

        assert ("investment", "USD", "stock:000102") not in _by_ref(result.products)
        assert ("investment", "USD", "stock:000101") in _by_ref(result.products)
        assert result.warnings == [
            "Fintual: skipped 1 stock position(s) without a current price"
        ]

    def test_a_position_without_totals_is_skipped_with_a_warning(
        self, monkeypatch, session_file
    ):
        """A null performance (a resolver error) skips that position only."""
        details = {**_DETAILS, "GLBX": (None, None)}
        server = _FintualStocks(_portfolio(_ACME, _GLBX), details=details)

        result = _run_stocks(monkeypatch, session_file, server)

        assert ("investment", "USD", "stock:000101") in _by_ref(result.products)
        assert ("investment", "USD", "stock:000102") not in _by_ref(result.products)
        assert result.warnings == ["Fintual: skipped 1 stock position(s) without totals"]

    def test_the_wallet_keeps_its_cents(self, monkeypatch, session_file):
        """A brokerage with cash and no positions is the USD wallet alone."""
        server = _FintualStocks(_portfolio(wallet=12.34))

        result = _run_stocks(monkeypatch, session_file, server)

        wallet = _by_ref(result.products)[("wallet", "USD", None)]
        assert wallet.metrics.balance == 12.34
        assert wallet.metrics.headline() == 12.34
        assert result.warnings == []
        assert server.paths() == ["/api/goals", "/gql/"]

    def test_a_wallet_in_another_currency_is_skipped_with_a_warning(
        self, monkeypatch, session_file
    ):
        server = _FintualStocks(_portfolio(_ACME, currency="CLP"))

        result = _run_stocks(monkeypatch, session_file, server)

        assert ("wallet", "USD", None) not in _by_ref(result.products)
        assert ("investment", "USD", "stock:000101") in _by_ref(result.products)
        assert result.warnings == [
            "Fintual: skipped the stocks wallet (no readable USD balance)"
        ]

    def test_a_graphql_401_refreshes_the_session_once(self, monkeypatch, session_file):
        """An expired token on /gql/ gets the website's refresh, never a login."""
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        server = _FintualStocks(
            _portfolio(_ACME), answers={"StocksPortfolio": [(401, {}), (200, _portfolio(_ACME))]}
        )

        result = _run_stocks(monkeypatch, session_file, server)

        login.assert_not_awaited()
        assert result.warnings == []
        assert server.paths().count("/app/goals") == 1
        assert ("investment", "USD", "stock:000101") in _by_ref(result.products)
        assert json.loads(session_file.read_text())["auth_refresh_token"] == (
            "synthetic-refresh-2"
        )

    def test_a_run_refreshes_the_session_at_most_once(self, monkeypatch, session_file):
        """Once the goals leg has refreshed, a /gql/ 401 doesn't spend the
        rotated refresh token again: it fails the stocks leg instead."""
        login = AsyncMock()
        monkeypatch.setattr(FintualScraper, "login", login)
        server = _FintualStocks(
            _portfolio(_ACME),
            answers={
                "/api/goals": [(401, {}), (200, _ONE_GOAL)],
                "StocksPortfolio": [(401, {})],
            },
        )

        result = _run_stocks(monkeypatch, session_file, server)

        login.assert_not_awaited()
        assert server.paths().count("/app/goals") == 1
        assert [p.external_ref for p in result.products] == ["34567"]
        assert result.warnings == ["Fintual stocks skipped: StocksPortfolio answered HTTP 401"]


# -- one session per cycle ---------------------------------------------------


def _both_legs(scraper):
    """Run both legs in run_scraper's order; return (transactions, products
    or the exception each leg raised)."""

    async def run():
        outcomes = []
        for leg in (scraper.scrape_transactions, scraper.scrape_products):
            try:
                outcomes.append(await leg())
            except Exception as exc:
                outcomes.append(exc)
        return outcomes

    return asyncio.run(run())


def _serve(monkeypatch, session_file, server, **cookies):
    """Cache a session and route every client to `server`."""
    _write_session(session_file, auth_refresh_token="synthetic-refresh-1", **cookies)
    monkeypatch.setattr(
        fintual_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(server.handler), **kwargs
        ),
    )


class TestSessionCycle:
    """Both legs share one session drive per run (the transactions leg runs it)."""

    def test_both_legs_read_the_goals_once(self, monkeypatch, session_file):
        server = _FintualStocks(_portfolio(_ACME))
        _serve(monkeypatch, session_file, server)

        transactions, result = _both_legs(FintualScraper())

        assert transactions == []
        assert server.paths().count("/api/goals") == 1
        assert server.operations.count("StocksPortfolio") == 1
        assert ("investment", "USD", "stock:000101") in _by_ref(result.products)

    def test_an_expired_session_is_refreshed_once_per_run(self, monkeypatch, session_file):
        server = _FintualStocks(
            _portfolio(_ACME), answers={"/api/goals": [(401, {}), (200, _ONE_GOAL)]}
        )
        _serve(monkeypatch, session_file, server)

        _, result = _both_legs(FintualScraper())

        assert server.paths().count("/app/goals") == 1
        assert server.paths().count("/api/goals") == 2
        assert [p.external_ref for p in result.products][0] == "34567"

    def test_a_failed_sign_in_is_re_raised_without_a_second_login(self, monkeypatch):
        """The products leg re-raises the cached error: one 2FA mail per run."""
        _imap_is_the_fintual_account(monkeypatch)
        login = AsyncMock(side_effect=FintualSessionError("synthetic refusal"))
        monkeypatch.setattr(FintualScraper, "login", login)
        client = _install(monkeypatch, _RoutedClient({GOALS_URL: [(200, _ONE_GOAL)]}))

        tx_outcome, products_outcome = _both_legs(FintualScraper())

        assert isinstance(tx_outcome, FintualSessionError)
        assert products_outcome is tx_outcome
        login.assert_awaited_once()
        assert client.calls == []

    def test_each_run_drives_its_own_session(self, monkeypatch, session_file):
        """A failed cycle's error doesn't leak into the next run."""
        monkeypatch.setattr("scrapers.retry.BASE_DELAY_SECONDS", 0.0)
        server = _FintualStocks(_portfolio(_ACME), answers={"/api/goals": [(404, {})]})
        _serve(monkeypatch, session_file, server)
        scraper = FintualScraper()

        first = _both_legs(scraper)
        server.answers.pop("/api/goals")
        second = _both_legs(scraper)

        assert all(isinstance(outcome, httpx.HTTPStatusError) for outcome in first)
        assert second[0] == []
        assert ("investment", "USD", "stock:000101") in _by_ref(second[1].products)
        assert server.paths().count("/api/goals") == 2

    def test_the_products_leg_alone_drives_the_session(self, monkeypatch, session_file):
        """A products-only dry run works, and twice means two drives."""
        server = _FintualStocks(_portfolio(_ACME))
        _serve(monkeypatch, session_file, server)
        scraper = FintualScraper()

        first = asyncio.run(scraper.scrape_products())
        second = asyncio.run(scraper.scrape_products())

        assert ("investment", "USD", "stock:000101") in _by_ref(first.products)
        assert _by_ref(second.products) == _by_ref(first.products)
        assert server.paths().count("/api/goals") == 2
