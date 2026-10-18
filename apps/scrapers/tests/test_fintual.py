"""Tests for the Fintual scraper: per-goal products and session handling.

These never hit the network: `httpx.AsyncClient` is swapped for a fake that
answers each Fintual URL from a canned queue, and the session cache lives in a
per-test tmp file. Every cookie value is a synthetic placeholder.
"""

import asyncio
import json
import os
import stat
from datetime import datetime, timezone
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
    INITIATE_LOGIN_URL,
    SIGN_IN_URL,
    FintualScraper,
    FintualSessionError,
)


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
    """Async-context stand-in for httpx.AsyncClient with a canned GET."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = self._payload
        return response


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
    Set-Cookie header would.
    """

    def __init__(self, routes, set_cookies=None):
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

        return Client()

    def answer(self, cookies, url):
        status, payload = 401, {}
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
        assert client.calls == [GOALS_URL, APP_URL, GOALS_URL]
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
        assert client.calls == [GOALS_URL, APP_URL, GOALS_URL]
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
        assert (client.calls, len(result.products)) == ([GOALS_URL, GOALS_URL], 1)

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
