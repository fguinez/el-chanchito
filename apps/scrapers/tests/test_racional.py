"""Tests for the Racional scraper.

These never hit the network: `httpx.AsyncClient` is swapped for a real client
over an `httpx.MockTransport` serving a small fake of Racional's web app, its
Cloud Functions, Firebase Auth and Firestore. The IMAP reader is an AsyncMock.
Every figure, id, token and address is synthetic.
"""

import asyncio
import base64
import json
import logging
import stat
from unittest.mock import AsyncMock

import httpx
import pytest

from scrapers.institutions import racional as racional_mod
from scrapers.institutions.racional import (
    RacionalDeviceSecurityError,
    RacionalLoginError,
    RacionalScraper,
    RacionalSessionError,
    _decode_value,
    _documents,
    _find_firebase_api_key,
    _find_main_bundle,
    _parse_portfolio,
    _percent,
    _token_claims,
)

EMAIL = "user@example.com"
PASSWORD = "synthetic-password"
API_KEY = "synthetic-web-api-key"
UID = "synthetic-uid-0001"
CODE = "000000"
BUNDLE = "main-es2015.0000000000000000abcd.js"


def _jwt(**claims) -> str:
    """An unsigned, synthetic ID token carrying `claims`."""
    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'user_id': UID, **claims})}.synthetic-signature"


def _fs(value) -> dict:
    """A Python value as a Firestore REST typed value."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_fs(v) for v in value]}}
    return {"mapValue": {"fields": {k: _fs(v) for k, v in value.items()}}}


def _evolution(total=2500000, profit=1000000, **overrides) -> dict:
    """Synthetic `lastHistoricEvolution` figures (returns are fractions)."""
    figures = {
        "totalAmount": total,
        "allTimeProfitAmount": profit,
        "accumulatedInputAmount": 1500000,
        "dailyProfitPercentage": 0.0012,
        "monthProfitPercentage": 0.0123,
        "ytdProfitPercentage": -0.0456,
        "updatedAt": "2026-01-15T12:00:00Z",
    }
    figures.update(overrides)
    return figures


def _portfolio(name="Meta Sintetica", **overrides) -> dict:
    """A synthetic CLP portfolio document's fields."""
    fields = {"userId": UID, "portfolioName": name, "lastHistoricEvolution": _evolution()}
    fields.update(overrides)
    return fields


def _stocks(**usd) -> dict:
    """A synthetic Racional Stocks (USD) portfolio document's fields."""
    figures = {"totalAmount": 1234.56, "allTimeProfitAmount": 234.56, **usd}
    return {
        "userId": UID,
        "portfolioName": "Stocks",
        "isDrivewealth": True,
        "lastHistoricEvolution": {"totalAmount": 999999, "portfolioEvolutionUSD": figures},
    }


def _doc(doc_id: str, fields: dict) -> dict:
    """One runQuery result entry."""
    return {
        "document": {
            "name": f"projects/racional-prod/databases/(default)/documents/portfolios/{doc_id}",
            "fields": {k: _fs(v) for k, v in fields.items()},
        },
        "readTime": "2026-01-15T12:00:00Z",
    }


class _Racional:
    """Racional's web app, Cloud Functions, Firebase Auth and Firestore, faked.

    `portfolios` maps document ids to fields. Refresh tokens in `valid_refresh`
    mint ID tokens with `claims`; Firestore answers only those ID tokens, and
    only for a query filtered on the signed-in uid.
    """

    def __init__(self, portfolios=None, *, show_code=True, claims=None, query_status=None):
        self.portfolios = portfolios if portfolios is not None else {}
        self.show_code = show_code
        self.claims = claims or {}
        self.query_status = query_status
        self.api_key = API_KEY
        self.valid_refresh = {"synthetic-refresh-1"}
        self.rotate_to = None
        self.sign_in_error = None
        self.sign_in_extra = {}
        self.unreadable_tokens = False
        self.drop_mfa_start = False
        self.issued: set[str] = set()
        self.requests: list[httpx.Request] = []

    def calls(self, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in str(r.url)]

    def _id_token(self) -> str:
        token = f"opaque-{len(self.issued)}" if self.unreadable_tokens else _jwt(**self.claims, n=len(self.issued))
        self.issued.add(token)
        return token

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == racional_mod.LOGIN_PAGE_URL:
            return httpx.Response(200, text=f'<script src="{BUNDLE}" type="module"></script>')
        if url.endswith(BUNDLE):
            return httpx.Response(
                200,
                text='SE_CONFIG:{apiKey:"synthetic-web-api-key",'
                'authDomain:"racional-prod.firebaseapp.com",projectId:"racional-prod"}',
            )
        if url == racional_mod.MFA_START_URL:
            if self.drop_mfa_start:
                raise httpx.ConnectError("synthetic dropped connection", request=request)
            return httpx.Response(200, json={"showMfaModal": self.show_code, "canSkipMfa": False})
        if url == racional_mod.MFA_VERIFY_URL:
            body = json.loads(request.content)
            return httpx.Response(200, json={"success": body["code"] == CODE})
        if request.url.host in ("identitytoolkit.googleapis.com", "securetoken.googleapis.com"):
            if request.headers.get(racional_mod.API_KEY_HEADER) != self.api_key:
                return self._error(400, "API key not valid. Please pass a valid API key.")
            if request.url.host == "identitytoolkit.googleapis.com":
                return self._sign_in(json.loads(request.content))
            return self._refresh(dict(httpx.QueryParams(request.content.decode())))
        if url == racional_mod.RUN_QUERY_URL:
            return self._query(request)
        return httpx.Response(404)

    @staticmethod
    def _error(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": status, "message": message}})

    def _sign_in(self, body: dict) -> httpx.Response:
        if self.sign_in_error:
            return self._error(400, self.sign_in_error)
        if body != {"email": EMAIL, "password": PASSWORD, "returnSecureToken": True}:
            return self._error(400, "INVALID_LOGIN_CREDENTIALS")
        self.valid_refresh.add("synthetic-refresh-2")
        return httpx.Response(
            200,
            json={
                "idToken": self._id_token(),
                "refreshToken": "synthetic-refresh-2",
                "localId": UID,
                **self.sign_in_extra,
            },
        )

    def _refresh(self, form: dict) -> httpx.Response:
        if form.get("grant_type") != "refresh_token" or form.get("refresh_token") not in self.valid_refresh:
            return self._error(400, "INVALID_REFRESH_TOKEN")
        refresh = self.rotate_to or form["refresh_token"]
        self.valid_refresh.add(refresh)
        return httpx.Response(
            200, json={"id_token": self._id_token(), "refresh_token": refresh, "user_id": UID}
        )

    def _query(self, request: httpx.Request) -> httpx.Response:
        if self.query_status is not None:
            return httpx.Response(self.query_status, json={"error": {"message": "synthetic"}})
        if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.issued:
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        where = json.loads(request.content)["structuredQuery"]["where"]["fieldFilter"]
        if where["field"]["fieldPath"] != "userId" or where["value"]["stringValue"] != UID:
            return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
        if not self.portfolios:
            return httpx.Response(200, json=[{"readTime": "2026-01-15T12:00:00Z"}])
        return httpx.Response(200, json=[_doc(i, f) for i, f in self.portfolios.items()])


def _install(monkeypatch, fake: _Racional) -> None:
    """Route the scraper's httpx.AsyncClient through the fake."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        racional_mod.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fake.handler), **kwargs),
    )


@pytest.fixture
def session_file(tmp_path, monkeypatch):
    """A per-test session cache path, with no IMAP auto-login configured."""
    path = tmp_path / ".racional_session.json"
    monkeypatch.setenv("RACIONAL_EMAIL", EMAIL)
    monkeypatch.setenv("RACIONAL_PASSWORD", PASSWORD)
    monkeypatch.setenv("RACIONAL_SESSION_FILE", str(path))
    for var in ("RACIONAL_FIREBASE_API_KEY", "EMAIL_IMAP_HOST", "EMAIL_IMAP_USER", "EMAIL_IMAP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return path


def _write_session(path, **overrides) -> None:
    session = {
        "refresh_token": "synthetic-refresh-1",
        "uid": UID,
        "device_id": "00000000-0000-4000-8000-000000000001",
        "api_key": API_KEY,
    }
    session.update(overrides)
    path.write_text(json.dumps(session))


def _enable_imap(monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("EMAIL_IMAP_USER", EMAIL.upper())
    monkeypatch.setenv("EMAIL_IMAP_PASSWORD", "synthetic-password")


class TestDecodeValue:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            pytest.param({"integerValue": "2500000"}, 2500000, id="integer-string"),
            pytest.param({"doubleValue": 1234.56}, 1234.56, id="double"),
            pytest.param({"stringValue": "Meta"}, "Meta", id="string"),
            pytest.param({"booleanValue": True}, True, id="boolean"),
            pytest.param({"nullValue": None}, None, id="null"),
            pytest.param({"timestampValue": "2026-01-15T12:00:00Z"}, "2026-01-15T12:00:00Z", id="timestamp"),
            pytest.param(
                {"mapValue": {"fields": {"a": {"integerValue": "1"}}}}, {"a": 1}, id="map"
            ),
            pytest.param({"mapValue": {}}, {}, id="empty-map"),
            pytest.param(
                {"arrayValue": {"values": [{"stringValue": "x"}, {"doubleValue": 0.5}]}},
                ["x", 0.5],
                id="array",
            ),
            pytest.param({"arrayValue": {}}, [], id="empty-array"),
            pytest.param({"integerValue": "not-a-number"}, None, id="bad-integer"),
            pytest.param({"geoPointValue": {"latitude": 0}}, None, id="unknown-type"),
            pytest.param("raw", None, id="not-wrapped"),
        ],
    )
    def test_decodes_typed_values(self, typed, expected):
        """Firestore's one-key type wrappers become plain Python values."""
        assert _decode_value(typed) == expected


class TestDocuments:
    def test_reads_ids_and_fields(self):
        """Each result yields its document id and decoded fields."""
        payload = [_doc("portfolio-0001", {"portfolioName": "Meta Sintetica"})]

        assert _documents(payload) == [("portfolio-0001", {"portfolioName": "Meta Sintetica"})]

    def test_bare_read_time_is_no_result(self):
        """An empty answer carries only a readTime entry."""
        assert _documents([{"readTime": "2026-01-15T12:00:00Z"}]) == []

    def test_name_without_an_id_is_skipped(self):
        """An empty id would collide with the identity of a product without an external_ref."""
        payload = [{"document": {"name": "projects/racional-prod/documents/portfolios/", "fields": {}}}]

        assert _documents(payload) == []

    def test_non_list_payload_raises(self):
        """An error object instead of the result array is not silently empty."""
        with pytest.raises(RuntimeError) as excinfo:
            _documents({"error": {"message": "synthetic"}})

        assert "portfolios" in str(excinfo.value)


class TestPercent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(0.0123, 1.23, id="fraction-to-percent"),
            pytest.param(-0.0456, -4.56, id="negative"),
            pytest.param(0, 0.0, id="zero"),
            pytest.param(None, None, id="missing"),
            pytest.param("0.01", None, id="string-rejected"),
            pytest.param(True, None, id="bool-rejected"),
        ],
    )
    def test_converts_fractions(self, raw, expected):
        """Racional's returns are fractions; the metrics want percent numbers."""
        assert _percent(raw) == expected


class TestParsePortfolio:
    def test_clp_portfolio(self):
        """A Racional Portafolios document becomes a CLP investment keyed by its id."""
        product, reason = _parse_portfolio("portfolio-0001", _portfolio())

        assert reason is None
        assert (product.institution, product.kind, product.currency) == ("racional", "investment", "CLP")
        assert product.external_ref == "portfolio-0001"
        assert product.name == "Meta Sintetica"
        assert product.metrics.nav == 2500000
        assert product.metrics.deposited == 1500000
        assert product.metrics.profit == 1000000
        assert product.metrics.var_daily_pct == 0.12
        assert product.metrics.var_30d_pct == 1.23
        assert product.metrics.var_ytd_pct == -4.56

    def test_clp_value_rounds_half_up(self):
        """A fractional CLP value is rounded half up to whole pesos."""
        fields = _portfolio(lastHistoricEvolution=_evolution(total=999998.5))

        product, _ = _parse_portfolio("portfolio-0001", fields)

        assert product.metrics.nav == 999999

    def test_usd_rounds_half_up_to_cents(self):
        """A USD figure past the cent is rounded half up, not banker's rounding."""
        product, _ = _parse_portfolio("portfolio-0002", _stocks(totalAmount=1234.565))

        assert product.metrics.nav == 1234.57

    def test_deposited_fallback_rounds_once(self):
        """The fallback subtracts the exact figures and rounds the result once."""
        evolution = _evolution(total=999998.5, profit=0.4)
        del evolution["accumulatedInputAmount"]

        product, _ = _parse_portfolio("portfolio-0001", _portfolio(lastHistoricEvolution=evolution))

        assert product.metrics.nav == 999999
        assert product.metrics.deposited == 999998

    def test_deposited_falls_back_to_value_minus_profit(self):
        """Without accumulatedInputAmount, contributions are value minus all-time profit."""
        evolution = _evolution(total=2500000, profit=1000000)
        del evolution["accumulatedInputAmount"]

        product, _ = _parse_portfolio("portfolio-0001", _portfolio(lastHistoricEvolution=evolution))

        assert product.metrics.deposited == 1500000

    def test_no_profit_and_no_contributions_leave_both_empty(self):
        """Neither figure is invented when Racional reports neither."""
        evolution = {"totalAmount": 1000000}

        product, _ = _parse_portfolio("portfolio-0001", _portfolio(lastHistoricEvolution=evolution))

        assert product.metrics.nav == 1000000
        assert product.metrics.deposited is None
        assert product.metrics.profit is None

    def test_stocks_portfolio_is_usd(self):
        """The Racional Stocks portfolio reports its USD figures, cents kept."""
        product, reason = _parse_portfolio("portfolio-0002", _stocks())

        assert reason is None
        assert product.currency == "USD"
        assert product.name == "Stocks"
        assert product.metrics.nav == 1234.56
        assert product.metrics.profit == 234.56
        assert product.metrics.deposited == 1000.0

    def test_stocks_uses_reported_usd_contributions(self):
        """A reported USD accumulatedInputAmount wins over the derived figure."""
        product, _ = _parse_portfolio("portfolio-0002", _stocks(accumulatedInputAmount=999.99))

        assert product.metrics.deposited == 999.99

    def test_stocks_without_usd_figures_is_skipped_quietly(self):
        """A Stocks portfolio that never held anything is no product and no warning."""
        fields = _stocks()
        fields["lastHistoricEvolution"] = {"totalAmount": 0}

        assert _parse_portfolio("portfolio-0002", fields) == (None, None)

    def test_stocks_without_usd_figures_but_a_value_warns(self):
        """A Stocks portfolio whose CLP value is non-zero but has no USD figures is a warning."""
        fields = _stocks()
        fields["lastHistoricEvolution"] = {"totalAmount": 999999}

        assert _parse_portfolio("portfolio-0002", fields) == (None, "without USD figures")

    def test_archived_empty_portfolio_drops_to_zero(self):
        """An emptied, archived portfolio is still emitted, so its value can't go stale."""
        fields = _portfolio(archived=True, lastHistoricEvolution=_evolution(total=0, profit=0))

        product, reason = _parse_portfolio("portfolio-0001", fields)

        assert reason is None
        assert product.metrics.nav == 0

    def test_archived_portfolio_with_value_is_kept(self):
        """An archived portfolio that still holds money still counts."""
        product, _ = _parse_portfolio("portfolio-0001", _portfolio(archived=True))

        assert product is not None
        assert product.metrics.nav == 2500000

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            pytest.param({"portfolioName": "Meta Sintetica"}, "without figures", id="no-evolution"),
            pytest.param(
                _portfolio(lastHistoricEvolution={"allTimeProfitAmount": 0}),
                "without a readable value",
                id="no-total",
            ),
            pytest.param(
                _portfolio(lastHistoricEvolution={"totalAmount": "2500000"}),
                "without a readable value",
                id="string-total",
            ),
        ],
    )
    def test_unreadable_portfolios_give_a_reason(self, fields, reason):
        """A portfolio whose value can't be read is skipped with a reason to warn about."""
        assert _parse_portfolio("portfolio-0001", fields) == (None, reason)

    def test_missing_name_gets_a_generic_label(self):
        """Without a portfolioName the product is still created, with a generic name."""
        product, _ = _parse_portfolio("portfolio-0001", _portfolio(name=""))

        assert product.name == "Portafolio Racional"

    def test_external_ref_ignores_cosmetic_changes(self):
        """Renaming a portfolio or a new valuation keeps the same external_ref."""
        first, _ = _parse_portfolio("portfolio-0001", _portfolio())
        later, _ = _parse_portfolio(
            "portfolio-0001",
            _portfolio(name="Otra Meta", lastHistoricEvolution=_evolution(total=1000000)),
        )

        assert first.external_ref == later.external_ref


class TestBundleParsers:
    def test_finds_the_main_bundle(self):
        """The ES2015 main bundle name is read from the index page."""
        html = f'<script src="runtime-es2015.0000.js" type="module"></script><script src="{BUNDLE}" type="module"></script>'

        assert _find_main_bundle(html) == BUNDLE

    def test_finds_the_project_api_key(self):
        """The key next to Racional's own authDomain is the one picked."""
        js = (
            'other:{apiKey:"synthetic-other-key",authDomain:"elsewhere.firebaseapp.com"},'
            'SE_CONFIG:{apiKey:"synthetic-web-api-key",authDomain:"racional-prod.firebaseapp.com"}'
        )

        assert _find_firebase_api_key(js) == API_KEY

    def test_missing_key_is_none(self):
        """A bundle without the config yields None, not a wrong key."""
        assert _find_firebase_api_key("console.log(1)") is None


class TestTokenClaims:
    def test_reads_claims(self):
        """The payload segment of an ID token decodes to its claims."""
        assert _token_claims(_jwt(deviceSecurityEnabled=True))["deviceSecurityEnabled"] is True

    def test_garbage_is_empty(self):
        """An unreadable token has no claims rather than raising."""
        assert _token_claims("not-a-token") == {}


class TestScrapeProducts:
    def test_one_product_per_portfolio(self, monkeypatch, session_file):
        """Each portfolio becomes its own investment product, CLP or USD."""
        _write_session(session_file)
        fake = _Racional({"portfolio-0001": _portfolio(), "portfolio-0002": _stocks()})
        _install(monkeypatch, fake)

        result = asyncio.run(RacionalScraper().scrape_products())

        assert result.warnings == []
        assert [(p.external_ref, p.currency) for p in result.products] == [
            ("portfolio-0001", "CLP"),
            ("portfolio-0002", "USD"),
        ]
        assert len(fake.calls("documents:runQuery")) == 1

    def test_a_scheduled_run_never_signs_in(self, monkeypatch, session_file):
        """With a cached session a run is one token refresh and one query."""
        _write_session(session_file)
        fake = _Racional({"portfolio-0001": _portfolio()})
        _install(monkeypatch, fake)

        asyncio.run(RacionalScraper().scrape_products())

        assert [r.url.host for r in fake.requests] == [
            "securetoken.googleapis.com",
            "firestore.googleapis.com",
        ]

    def test_rotated_refresh_token_is_cached_privately(self, monkeypatch, session_file):
        """A new refresh token from Firebase replaces the cached one, owner-only."""
        _write_session(session_file)
        fake = _Racional({"portfolio-0001": _portfolio()})
        fake.rotate_to = "synthetic-refresh-3"
        _install(monkeypatch, fake)

        asyncio.run(RacionalScraper().scrape_products())

        assert json.loads(session_file.read_text())["refresh_token"] == "synthetic-refresh-3"
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600

    def test_a_world_readable_cache_becomes_owner_only(self, monkeypatch, session_file):
        """An existing 0644 cache is replaced by an owner-only file, with no temp left behind."""
        _write_session(session_file)
        session_file.chmod(0o644)
        fake = _Racional({"portfolio-0001": _portfolio()})
        fake.rotate_to = "synthetic-refresh-3"
        _install(monkeypatch, fake)

        asyncio.run(RacionalScraper().scrape_products())

        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
        assert [p.name for p in session_file.parent.iterdir()] == [session_file.name]

    def test_corrupt_cache_counts_as_blocked(self, monkeypatch, session_file):
        """A damaged session file never leads to an unattended sign-in."""
        session_file.write_text('{"refresh_token": "synthetic-refr')
        _enable_imap(monkeypatch)
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        assert "couldn't be read" in str(excinfo.value)
        assert fake.requests == []

    def test_unreadable_id_token_fails_closed(self, monkeypatch, session_file):
        """If the claims can't be read, device security can't be ruled out: nothing is read."""
        _write_session(session_file)
        fake = _Racional({"portfolio-0001": _portfolio()})
        fake.unreadable_tokens = True
        _install(monkeypatch, fake)

        with pytest.raises(RacionalSessionError, match="couldn't be read"):
            asyncio.run(RacionalScraper().scrape_products())

        assert fake.calls("documents:runQuery") == []

    def test_no_portfolios_is_not_an_error(self, monkeypatch, session_file):
        """An account without portfolios yields no products and no warnings."""
        _write_session(session_file)
        _install(monkeypatch, _Racional({}))

        result = asyncio.run(RacionalScraper().scrape_products())

        assert (result.products, result.warnings) == ([], [])

    def test_unreadable_portfolio_is_a_warning(self, monkeypatch, session_file, caplog):
        """A portfolio without a value is skipped; the rest emit and the run is partial."""
        _write_session(session_file)
        portfolios = {
            "portfolio-0001": _portfolio(),
            "portfolio-0003": {"userId": UID, "portfolioName": "Rota"},
        }
        _install(monkeypatch, _Racional(portfolios))

        with caplog.at_level(logging.WARNING):
            result = asyncio.run(RacionalScraper().scrape_products())

        assert [p.external_ref for p in result.products] == ["portfolio-0001"]
        assert result.warnings == ["Racional: skipped 1 portfolio(s) without figures"]
        assert "skipped 1 portfolio(s)" in caplog.text

    def test_no_session_without_auto_login_raises_before_any_request(self, monkeypatch, session_file):
        """No cache and no IMAP mailbox: an actionable error and no network at all."""
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        assert "make racional-login" in str(excinfo.value)
        assert fake.requests == []

    def test_expired_session_raises_without_leaking(self, monkeypatch, session_file):
        """A dead refresh token names the fix and carries no token, uid or e-mail."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        message = str(excinfo.value)
        assert "make racional-login" in message
        for secret in ("synthetic-refresh", UID, EMAIL, API_KEY):
            assert secret not in message
        assert fake.calls("documents:runQuery") == []

    def test_device_security_stops_before_reading(self, monkeypatch, session_file):
        """An account with device security raises and Firestore is never queried."""
        _write_session(session_file)
        fake = _Racional({"portfolio-0001": _portfolio()}, claims={"deviceSecurityEnabled": True})
        _install(monkeypatch, fake)

        with pytest.raises(RacionalDeviceSecurityError):
            asyncio.run(RacionalScraper().scrape_products())

        assert fake.calls("documents:runQuery") == []

    def test_device_security_drops_the_session_for_good(self, monkeypatch, session_file):
        """After device security is seen, later runs make no request and say why."""
        _write_session(session_file)
        fake = _Racional({"portfolio-0001": _portfolio()}, claims={"deviceSecurityEnabled": True})
        _install(monkeypatch, fake)

        with pytest.raises(RacionalDeviceSecurityError):
            asyncio.run(RacionalScraper().scrape_products())
        fake.requests.clear()
        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        session = json.loads(session_file.read_text())
        assert "refresh_token" not in session
        assert session["device_id"] == "00000000-0000-4000-8000-000000000001"
        assert "device security" in str(excinfo.value)
        assert fake.requests == []

    @pytest.mark.parametrize(
        ("status", "error", "fragment"),
        [
            pytest.param(401, RacionalSessionError, "make racional-login", id="401"),
            pytest.param(403, RuntimeError, "App Check", id="403"),
            pytest.param(500, RuntimeError, "500", id="500"),
        ],
    )
    def test_query_failures_raise(self, monkeypatch, session_file, status, error, fragment):
        """A refused query fails the leg with a short message and no response body."""
        _write_session(session_file)
        _install(monkeypatch, _Racional(query_status=status))

        with pytest.raises(error) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        assert fragment in str(excinfo.value)
        assert "synthetic" not in str(excinfo.value)

    def test_rotated_api_key_is_read_again_once(self, monkeypatch, session_file):
        """A rejected cached key is re-read from the public bundle and cached."""
        _write_session(session_file, api_key="synthetic-old-key")
        fake = _Racional({"portfolio-0001": _portfolio()})
        _install(monkeypatch, fake)

        result = asyncio.run(RacionalScraper().scrape_products())

        assert len(result.products) == 1
        assert len(fake.calls(BUNDLE)) == 1
        assert json.loads(session_file.read_text())["api_key"] == API_KEY
        for request in fake.requests:
            assert "key" not in request.url.params
            assert API_KEY not in str(request.url) and "synthetic-old-key" not in str(request.url)

    def test_rejected_override_on_refresh_names_it(self, monkeypatch, session_file):
        """A hand-set key Google refuses is reported as such, without the key or a bundle read."""
        _write_session(session_file)
        monkeypatch.setenv("RACIONAL_FIREBASE_API_KEY", "synthetic-wrong-key")
        fake = _Racional({"portfolio-0001": _portfolio()})
        _install(monkeypatch, fake)

        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        assert "RACIONAL_FIREBASE_API_KEY" in str(excinfo.value)
        assert "synthetic-wrong-key" not in str(excinfo.value)
        assert fake.calls(BUNDLE) == []
        assert fake.calls("documents:runQuery") == []

    def test_api_key_override_skips_the_bundle(self, monkeypatch, session_file):
        """RACIONAL_FIREBASE_API_KEY is used as is; the web app is never fetched."""
        _write_session(session_file, api_key=None)
        monkeypatch.setenv("RACIONAL_FIREBASE_API_KEY", API_KEY)
        fake = _Racional({"portfolio-0001": _portfolio()})
        _install(monkeypatch, fake)

        asyncio.run(RacionalScraper().scrape_products())

        assert fake.calls("app.racional.cl") == []


class TestApiKeyStaysOutOfUrls:
    def test_key_travels_in_a_header_never_in_a_url(self, monkeypatch, session_file, caplog):
        """Sign-in and refresh carry the key in a header, so no URL or httpx log line holds it."""
        fake = _Racional({"portfolio-0001": _portfolio()})
        _install(monkeypatch, fake)

        with caplog.at_level(logging.INFO, logger="httpx"):
            asyncio.run(RacionalScraper().login(lambda: CODE))
            asyncio.run(RacionalScraper().scrape_products())

        keyed = fake.calls("identitytoolkit") + fake.calls("securetoken")
        assert len(keyed) == 2
        assert all(r.headers[racional_mod.API_KEY_HEADER] == API_KEY for r in keyed)
        assert all("key" not in r.url.params for r in fake.requests)
        assert all(API_KEY not in str(r.url) for r in fake.requests)
        assert "HTTP Request" in caplog.text
        assert API_KEY not in caplog.text


class TestScrapeTransactions:
    def test_returns_nothing_without_network(self, monkeypatch, session_file):
        """Movements aren't imported (per-portfolio products), so the leg is empty and offline."""
        fake = _Racional()
        _install(monkeypatch, fake)

        assert asyncio.run(RacionalScraper().scrape_transactions()) == []
        assert fake.requests == []


class TestLogin:
    def test_signs_in_like_the_web_app(self, monkeypatch, session_file):
        """Start the OTP, verify the code, sign in, and cache the session."""
        fake = _Racional()
        _install(monkeypatch, fake)
        prompts = []

        asyncio.run(RacionalScraper().login(lambda: prompts.append(1) or CODE))

        assert prompts == [1]
        order = [r.url.path for r in fake.requests if r.method == "POST"]
        assert order == ["/authMfaStart", "/authMfaVerify", "/v1/accounts:signInWithPassword"]
        start = json.loads(fake.calls("authMfaStart")[0].content)
        assert start["method"] == "email"
        session = json.loads(session_file.read_text())
        assert session["refresh_token"] == "synthetic-refresh-2"
        assert session["uid"] == UID
        assert session["api_key"] == API_KEY
        assert session["device_id"] == start["deviceId"]
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600

    def test_reuses_the_device_id(self, monkeypatch, session_file):
        """A later sign-in presents the same device, so Racional sees one device."""
        _write_session(session_file, device_id="00000000-0000-4000-8000-000000000001")
        fake = _Racional()
        _install(monkeypatch, fake)

        asyncio.run(RacionalScraper().login(lambda: CODE))

        start = json.loads(fake.calls("authMfaStart")[0].content)
        assert start["deviceId"] == "00000000-0000-4000-8000-000000000001"

    def test_no_code_when_racional_doesnt_ask(self, monkeypatch, session_file):
        """When authMfaStart says no modal, the code provider is never called."""
        _install(monkeypatch, _Racional(show_code=False))
        provider = AsyncMock(return_value=CODE)

        asyncio.run(RacionalScraper().login(provider))

        provider.assert_not_awaited()
        assert json.loads(session_file.read_text())["refresh_token"] == "synthetic-refresh-2"

    def test_wrong_code_stops_before_the_password(self, monkeypatch, session_file):
        """A rejected code raises, and the password is never sent."""
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalLoginError) as excinfo:
            asyncio.run(RacionalScraper().login(lambda: "999999"))

        assert "verification code" in str(excinfo.value)
        assert fake.calls("signInWithPassword") == []
        assert "refresh_token" not in json.loads(session_file.read_text())

    def test_device_id_is_saved_before_racional_sees_it(self, monkeypatch, session_file):
        """A failed first sign-in still remembers the device it presented."""
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalLoginError):
            asyncio.run(RacionalScraper().login(lambda: "999999"))

        start = json.loads(fake.calls("authMfaStart")[0].content)
        assert json.loads(session_file.read_text())["device_id"] == start["deviceId"]

    def test_stale_cached_key_is_read_again_for_a_sign_in(self, monkeypatch, session_file):
        """A sign-in reads the key fresh, so a rotated key can't waste the e-mailed code."""
        _write_session(session_file, refresh_token=None, api_key="synthetic-old-key")
        fake = _Racional()
        _install(monkeypatch, fake)

        asyncio.run(RacionalScraper().login(lambda: CODE))

        assert fake.calls("signInWithPassword")[0].headers[racional_mod.API_KEY_HEADER] == API_KEY
        assert json.loads(session_file.read_text())["api_key"] == API_KEY

    def test_rejected_key_at_sign_in_names_the_override(self, monkeypatch, session_file):
        """A key Google refuses is reported as such, not as wrong credentials."""
        monkeypatch.setenv("RACIONAL_FIREBASE_API_KEY", "synthetic-wrong-key")
        _install(monkeypatch, _Racional())

        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().login(lambda: CODE))

        assert not isinstance(excinfo.value, RacionalLoginError)
        assert "RACIONAL_FIREBASE_API_KEY" in str(excinfo.value)
        assert "synthetic-wrong-key" not in str(excinfo.value)

    @pytest.mark.parametrize(
        ("firebase_error", "fragment"),
        [
            pytest.param("INVALID_LOGIN_CREDENTIALS", "e-mail or password", id="credentials"),
            pytest.param("TOO_MANY_ATTEMPTS_TRY_LATER : synthetic detail", "rate-limiting", id="rate-limit"),
            pytest.param("MISSING_RECAPTCHA_TOKEN", "reCAPTCHA", id="recaptcha"),
            pytest.param("OPERATION_NOT_ALLOWED", "OPERATION_NOT_ALLOWED", id="other"),
            pytest.param(
                "Requests from referer synthetic.example.com are blocked.",
                "no error code",
                id="free-text",
            ),
        ],
    )
    def test_sign_in_refusals_are_explained_once(self, monkeypatch, session_file, firebase_error, fragment):
        """Each Firebase refusal raises one readable error, with no secret in it."""
        fake = _Racional()
        fake.sign_in_error = firebase_error
        _install(monkeypatch, fake)

        with pytest.raises(RacionalLoginError) as excinfo:
            asyncio.run(RacionalScraper().login(lambda: CODE))

        message = str(excinfo.value)
        assert fragment in message
        assert PASSWORD not in message and EMAIL not in message
        assert "synthetic detail" not in message
        assert "REFERER" not in message.upper() and "example.com" not in message
        assert len(fake.calls("signInWithPassword")) == 1

    def test_sms_second_factor_is_refused(self, monkeypatch, session_file):
        """An account with Firebase SMS MFA raises instead of half signing in."""
        fake = _Racional()
        fake.sign_in_extra = {"mfaPendingCredential": "synthetic-pending", "idToken": None}
        _install(monkeypatch, fake)

        with pytest.raises(RacionalLoginError) as excinfo:
            asyncio.run(RacionalScraper().login(lambda: CODE))

        assert "SMS" in str(excinfo.value)
        assert "refresh_token" not in json.loads(session_file.read_text())

    def test_device_security_is_not_cached(self, monkeypatch, session_file):
        """A sign-in that reveals device security raises and caches no session."""
        _install(monkeypatch, _Racional(claims={"deviceSecurityEnabled": True}))

        with pytest.raises(RacionalDeviceSecurityError):
            asyncio.run(RacionalScraper().login(lambda: CODE))

        session = json.loads(session_file.read_text())
        assert "refresh_token" not in session
        assert session["auto_login_blocked"]

    def test_missing_password_raises_before_any_request(self, monkeypatch, session_file):
        """Without RACIONAL_PASSWORD nothing is sent and the fix is named."""
        monkeypatch.delenv("RACIONAL_PASSWORD")
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalLoginError) as excinfo:
            asyncio.run(RacionalScraper().login(lambda: CODE))

        assert "make secret-set KEY=RACIONAL_PASSWORD" in str(excinfo.value)
        assert fake.requests == []


class TestAutoLogin:
    def _imap(self, monkeypatch, codes):
        reader = AsyncMock(side_effect=codes)
        monkeypatch.setattr(racional_mod, "fetch_latest_code", reader)
        monkeypatch.setattr(racional_mod, "OTP_POLL_SECONDS", 0)
        return reader

    def test_expired_session_signs_in_with_the_mailed_code(self, monkeypatch, session_file):
        """When the account is the IMAP mailbox, a dead session is renewed unattended."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        _enable_imap(monkeypatch)
        reader = self._imap(monkeypatch, ["111111", "111111", CODE])
        fake = _Racional({"portfolio-0001": _portfolio()})
        _install(monkeypatch, fake)

        result = asyncio.run(RacionalScraper().scrape_products())

        assert len(result.products) == 1
        assert reader.await_count == 3
        assert len(fake.calls("signInWithPassword")) == 1
        assert len(fake.calls("securetoken")) == 1
        assert json.loads(session_file.read_text())["refresh_token"] == "synthetic-refresh-2"

    def test_signed_in_once_the_next_run_only_refreshes(self, monkeypatch, session_file):
        """After an unattended sign-in, the following run uses the new session."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        _enable_imap(monkeypatch)
        self._imap(monkeypatch, [None, CODE])
        fake = _Racional({"portfolio-0001": _portfolio()})
        _install(monkeypatch, fake)

        asyncio.run(RacionalScraper().scrape_products())
        asyncio.run(RacionalScraper().scrape_products())

        assert len(fake.calls("signInWithPassword")) == 1
        assert len(fake.calls("authMfaStart")) == 1

    @pytest.mark.parametrize(
        ("setup", "error", "fragment"),
        [
            pytest.param("password", RacionalLoginError, "e-mail or password", id="wrong-password"),
            pytest.param("device", RacionalDeviceSecurityError, "device security", id="device-security"),
        ],
    )
    def test_any_refusal_blocks_and_is_remembered(self, monkeypatch, session_file, setup, error, fragment):
        """A refused unattended sign-in blocks the next ones, keeps the device and names the cause."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        _enable_imap(monkeypatch)
        self._imap(monkeypatch, [None, CODE])
        fake = _Racional(claims={"deviceSecurityEnabled": True} if setup == "device" else None)
        if setup == "password":
            monkeypatch.setenv("RACIONAL_PASSWORD", "synthetic-wrong-password")
        _install(monkeypatch, fake)

        with pytest.raises(error):
            asyncio.run(RacionalScraper().scrape_products())
        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        session = json.loads(session_file.read_text())
        assert session["device_id"] == "00000000-0000-4000-8000-000000000001"
        assert fragment in str(excinfo.value)
        assert len(fake.calls("authMfaStart")) == 1

    def test_failed_auto_login_turns_auto_login_off(self, monkeypatch, session_file):
        """A refused automatic sign-in isn't attempted again on the next cycle."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        _enable_imap(monkeypatch)
        self._imap(monkeypatch, [None, "999999", "999999"])
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalLoginError):
            asyncio.run(RacionalScraper().scrape_products())
        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        assert "make racional-login" in str(excinfo.value)
        assert len(fake.calls("authMfaStart")) == 1

    def test_manual_login_turns_auto_login_back_on(self, monkeypatch, session_file):
        """A successful `make racional-login` clears the block."""
        _write_session(session_file, auto_login_blocked=True)
        _install(monkeypatch, _Racional())

        asyncio.run(RacionalScraper().login(lambda: CODE))

        assert "auto_login_blocked" not in json.loads(session_file.read_text())

    def test_code_that_never_arrives_gives_up(self, monkeypatch, session_file):
        """If only the earlier sign-in's code is in the inbox, polling stops with an error."""
        _enable_imap(monkeypatch)
        self._imap(monkeypatch, ["111111"] * (racional_mod.OTP_POLLS + 1))
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalLoginError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        assert "didn't arrive" in str(excinfo.value)
        assert fake.calls("signInWithPassword") == []
        session = json.loads(session_file.read_text())
        assert "didn't arrive" in session["auto_login_blocked"]
        assert session["device_id"]

    def test_imap_failure_before_any_request_doesnt_block(self, monkeypatch, session_file):
        """If the inbox can't be read before the code is requested, Racional saw nothing: no block."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        _enable_imap(monkeypatch)
        self._imap(monkeypatch, [OSError("synthetic IMAP failure")])
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(OSError):
            asyncio.run(RacionalScraper().scrape_products())

        assert "auto_login_blocked" not in json.loads(session_file.read_text())
        assert fake.calls("authMfaStart") == []

    def test_dropped_connection_doesnt_block(self, monkeypatch, session_file):
        """A transport error says nothing about the account, so the next cycle may sign in again."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        _enable_imap(monkeypatch)
        self._imap(monkeypatch, [None, CODE])
        fake = _Racional()
        fake.drop_mfa_start = True
        _install(monkeypatch, fake)

        with pytest.raises(httpx.TransportError):
            asyncio.run(RacionalScraper().scrape_products())

        assert "auto_login_blocked" not in json.loads(session_file.read_text())
        assert fake.calls("signInWithPassword") == []

    def test_foreign_error_is_remembered_by_type_only(self, monkeypatch, session_file):
        """An exception that isn't ours blocks, but only its type is stored and shown."""
        _write_session(session_file, refresh_token="synthetic-refresh-dead")
        _enable_imap(monkeypatch)
        self._imap(monkeypatch, [None, ValueError("synthetic-refresh-secret")])
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(ValueError):
            asyncio.run(RacionalScraper().scrape_products())
        with pytest.raises(RacionalSessionError) as excinfo:
            asyncio.run(RacionalScraper().scrape_products())

        assert json.loads(session_file.read_text())["auto_login_blocked"] == "an unexpected ValueError."
        assert "synthetic-refresh-secret" not in session_file.read_text()
        assert "ValueError" in str(excinfo.value)
        assert "synthetic-refresh-secret" not in str(excinfo.value)

    @pytest.mark.parametrize(
        ("imap_host", "imap_user", "imap_password"),
        [
            pytest.param("imap.example.com", "other@example.com", "synthetic-password", id="other-mailbox"),
            pytest.param("imap.example.com", EMAIL, "", id="no-imap-password"),
            pytest.param("", EMAIL, "synthetic-password", id="no-imap-host"),
        ],
    )
    def test_auto_login_needs_the_same_mailbox(
        self, monkeypatch, session_file, imap_host, imap_user, imap_password
    ):
        """Only the account's own mailbox, with IMAP configured, enables unattended sign-in."""
        monkeypatch.setenv("EMAIL_IMAP_HOST", imap_host)
        monkeypatch.setenv("EMAIL_IMAP_USER", imap_user)
        monkeypatch.setenv("EMAIL_IMAP_PASSWORD", imap_password)
        reader = self._imap(monkeypatch, [CODE])
        fake = _Racional()
        _install(monkeypatch, fake)

        with pytest.raises(RacionalSessionError):
            asyncio.run(RacionalScraper().scrape_products())

        assert fake.requests == []
        reader.assert_not_awaited()
