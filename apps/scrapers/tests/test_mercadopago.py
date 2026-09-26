"""Tests for the MercadoPago scraper: REST balance leg plus the e-mail switch.

No network: `fetch_wallet_balance` gets an `httpx.MockTransport` serving canned
/users/me and balance payloads, and the scraper tests stub the module-level
fetchers. Every token, user id and amount is synthetic.
"""

import asyncio

import httpx
import pytest

from product_model import WalletMetrics

from scrapers.institutions import mercadopago as mp_mod
from scrapers.institutions.mercadopago import (
    PATTERN,
    MercadoPagoScraper,
    _parse_balance,
    fetch_wallet_balance,
)

TOKEN = "APP_USR-0000-synthetic"
USER_ID = 1234
BALANCE_PATH = f"/users/{USER_ID}/mercadopago_account/balance"


def _transport(balance=None, *, me_status=200, balance_status=200, seen=None):
    """MockTransport answering /users/me and then the balance endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/users/me":
            return httpx.Response(me_status, json={"id": USER_ID, "nickname": "SYNTHETIC"})
        if request.url.path == BALANCE_PATH:
            return httpx.Response(balance_status, json=balance or {})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _fetch(transport):
    return asyncio.run(fetch_wallet_balance(TOKEN, transport=transport))


async def _must_not_be_called(*args, **kwargs):
    pytest.fail("this fetcher must not be called")


class TestParseBalance:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"available_balance": 2500000, "currency_id": "CLP"}, 2500000),
            ({"available_balance": 999999.4, "currency_id": "CLP"}, 999999),
            ({"available_balance": 999998.6, "currency_id": "CLP"}, 999999),
            ({"available_balance": 1000000}, 1000000),
        ],
    )
    def test_returns_whole_clp(self, payload, expected):
        """Int or float balances round to whole CLP; currency_id is optional."""
        result = _parse_balance(payload)

        assert result == expected
        assert isinstance(result, int)

    def test_non_clp_currency_raises(self):
        """A balance in another currency is rejected rather than mislabelled."""
        payload = {"available_balance": 1234.56, "currency_id": "USD"}

        with pytest.raises(ValueError, match="currency"):
            _parse_balance(payload)

    @pytest.mark.parametrize("value", [None, "2500000", True])
    def test_non_numeric_balance_raises(self, value):
        """A null, string or boolean balance is not a number."""
        payload = {"available_balance": value, "currency_id": "CLP"}

        with pytest.raises(ValueError, match="available_balance"):
            _parse_balance(payload)

    def test_missing_balance_names_keys_not_values(self):
        """The error lists the payload's keys but none of its values."""
        payload = {
            "total_amount": 2500000,
            "unavailable_balance": 1000000,
            "currency_id": "CLP",
        }

        with pytest.raises(ValueError) as excinfo:
            _parse_balance(payload)

        message = str(excinfo.value)
        assert "available_balance" in message
        assert "total_amount" in message
        assert "2500000" not in message
        assert "1000000" not in message
        assert "CLP" not in message


class TestFetchWalletBalance:
    @pytest.mark.parametrize(("raw", "expected"), [(2500000, 2500000), (999999.4, 999999)])
    def test_reads_available_balance_with_bearer_token(self, raw, expected):
        """Both calls carry the bearer token; the balance path uses the /users/me id."""
        seen: list[httpx.Request] = []
        transport = _transport({"available_balance": raw, "currency_id": "CLP"}, seen=seen)

        result = _fetch(transport)

        assert result == expected
        assert [r.url.path for r in seen] == ["/users/me", BALANCE_PATH]
        assert all(r.url.host == "api.mercadopago.com" for r in seen)
        assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen)

    @pytest.mark.parametrize(
        ("me_status", "balance_status", "status"),
        [(401, 200, 401), (403, 200, 403), (200, 401, 401)],
    )
    def test_rejected_token_raises_actionable_error(self, me_status, balance_status, status):
        """401/403 on either call points at the token without leaking it or the user id."""
        transport = _transport(
            {"available_balance": 2500000},
            me_status=me_status,
            balance_status=balance_status,
        )

        with pytest.raises(RuntimeError) as excinfo:
            _fetch(transport)

        message = str(excinfo.value)
        assert message == (
            f"MercadoPago API rejected the access token (HTTP {status}); "
            "check MERCADOPAGO_ACCESS_TOKEN"
        )
        assert TOKEN not in message
        assert str(USER_ID) not in message

    @pytest.mark.parametrize(
        ("me_status", "balance_status", "expected"),
        [
            (500, 200, "MercadoPago user request failed (HTTP 500)"),
            (200, 500, "MercadoPago balance request failed (HTTP 500)"),
        ],
    )
    def test_server_error_raises_generic_error(self, me_status, balance_status, expected):
        """Any other non-2xx names the call and the status, nothing else."""
        transport = _transport(
            {"available_balance": 2500000},
            me_status=me_status,
            balance_status=balance_status,
        )

        with pytest.raises(RuntimeError) as excinfo:
            _fetch(transport)

        assert str(excinfo.value) == expected

    def test_network_error_hides_the_url(self):
        """A transport failure is reported by type; the URL (with the user id) stays out."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/me":
                return httpx.Response(200, json={"id": USER_ID})
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        with pytest.raises(RuntimeError) as excinfo:
            _fetch(httpx.MockTransport(handler))

        message = str(excinfo.value)
        assert message == "MercadoPago balance request failed (ConnectError)"
        assert str(USER_ID) not in message

    def test_missing_user_id_raises(self):
        """A /users/me payload without an id stops before the balance call."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"nickname": "SYNTHETIC"})

        with pytest.raises(ValueError, match="'id'"):
            _fetch(httpx.MockTransport(handler))

        assert [r.url.path for r in seen] == ["/users/me"]


class TestMercadoPagoScraper:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [(TOKEN, "http_api"), ("", "email"), (None, "email")],
    )
    def test_method_follows_the_token(self, monkeypatch, token, expected):
        """The API is the run's method only when a non-empty token is configured."""
        if token is None:
            monkeypatch.delenv("MERCADOPAGO_ACCESS_TOKEN", raising=False)
        else:
            monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", token)

        scraper = MercadoPagoScraper()

        assert scraper.method == expected
        assert scraper.name == f"{expected}_mercadopago"

    def test_products_without_token_are_empty(self, monkeypatch):
        """No token means no API call and no products, as before."""
        monkeypatch.delenv("MERCADOPAGO_ACCESS_TOKEN", raising=False)
        monkeypatch.setattr(mp_mod, "fetch_wallet_balance", _must_not_be_called)

        result = asyncio.run(MercadoPagoScraper().scrape_products())

        assert result.products == []
        assert result.warnings == []

    def test_products_with_token_emit_the_wallet(self, monkeypatch):
        """With a token the balance becomes the singleton CLP wallet product."""
        monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", TOKEN)
        tokens: list[str] = []

        async def fake_fetch(access_token, **kwargs):
            tokens.append(access_token)
            return 2500000

        monkeypatch.setattr(mp_mod, "fetch_wallet_balance", fake_fetch)

        result = asyncio.run(MercadoPagoScraper(read_email=False).scrape_products())

        assert tokens == [TOKEN]
        assert result.warnings == []
        assert len(result.products) == 1
        product = result.products[0]
        assert product.institution == "mercadopago"
        assert product.kind == "wallet"
        assert product.currency == "CLP"
        assert product.external_ref is None
        assert product.metrics == WalletMetrics(balance=2500000)

    def test_transactions_skip_email_when_disabled(self, monkeypatch):
        """read_email=False never touches the inbox."""
        monkeypatch.setattr(mp_mod, "fetch_transactions_for_pattern", _must_not_be_called)

        result = asyncio.run(MercadoPagoScraper(read_email=False).scrape_transactions())

        assert result == []

    def test_transactions_read_email_by_default(self, monkeypatch):
        """The default scraper still reads movements with the MP e-mail pattern."""
        monkeypatch.setenv("EMAIL_LOOKBACK_DAYS", "3")
        calls: list[tuple] = []

        async def fake_fetch(pattern, lookback_days):
            calls.append((pattern, lookback_days))
            return []

        monkeypatch.setattr(mp_mod, "fetch_transactions_for_pattern", fake_fetch)

        result = asyncio.run(MercadoPagoScraper().scrape_transactions())

        assert result == []
        assert calls == [(PATTERN, 3)]
