"""Tests for the Banco de Chile USD card exchange rate surface.

Never launches a browser or hits the real bank: the in-page script is checked
as text, and the surface runs on a fake page whose bounded script call returns synthetic
results shaped like the script's. Every rate, date, name and id below is
fabricated (see the repo's personal-data policy).
"""

import datetime
import logging
import re
import time
from decimal import Decimal

import pytest

from scrapers.backends import banchile_fx as fx_mod
from scrapers.backends.banchile_fx import (
    SOURCE_LABEL,
    SURFACE,
    describe_failure,
    exchange_rate_from_result,
    parse_rate,
    read_exchange_rate_surface,
)
from scrapers.backends.banchile_web import _PORTAL_HOME, _PORTAL_HOST
from scrapers.base import ProductRef

# What the selector and the rate response carry besides the rate. None of it
# may ever leave the page; the fake results smuggle it in to prove the Python
# side ignores it even if a future script returned it.
_PRIVATE = {
    "nombre": "TITULAR SINTETICO",
    "rut": "RUT-SINTETICO",
    "idTarjeta": "TARJETA-SINTETICA",
    "mail": "sintetico@example.invalid",
    "anexoPagoDolar": "ANEXO-SINTETICO",
    "configuracionCajaDesafio": "CAJA-SINTETICA",
}


def _ok(rate=950.0, date="01/01/2026", **extra):
    result = {"status": "ok", "tipoCambio": rate, "fechaConsultaTipoCambio": date}
    return {**result, **extra}


class _Handle:
    def __init__(self, value):
        self.value = value

    def json_value(self):
        return self.value


class _FakeFxPage:
    """A portal page whose bounded script call (`wait_for_function`, then
    `json_value`) answers with canned script results.

    One result per call (the last one repeats); an exception instance is raised
    instead of returned.
    """

    def __init__(self, *results, url=_PORTAL_HOME):
        self.url = url
        self.results = list(results)
        self.calls: list[tuple[str, dict]] = []
        self.timeouts: list[int] = []
        self.gotos: list[str] = []

    def wait_for_function(self, script, arg=None, timeout=None):
        assert timeout, "the script call must be bounded"
        self.calls.append((script, arg))
        self.timeouts.append(timeout)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return _Handle(result)

    def wait_for_timeout(self, ms):
        return None

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url


class TestTheInPageScript:
    """The whole chain stays in the page; Python gets the rate and a status."""

    _ALLOWED_KEYS = {"status", "code", "tipoCambio", "fechaConsultaTipoCambio"}

    def test_every_return_is_an_object_with_only_the_allowed_keys(self):
        script = fx_mod._EXCHANGE_RATE_JS
        returns = re.findall(r"\breturn\b\s*(.)", script)

        literals = re.findall(r"\breturn\s*\{(.*?)\};", script, re.S)

        assert returns and set(returns) == {"{"}, "every return must be a literal"
        assert len(literals) == len(returns)
        for literal in literals:
            keys = set(re.findall(r"(?:^|[{,])\s*(\w+)\s*:", literal, re.M))
            assert keys <= self._ALLOWED_KEYS, keys

    def test_it_fetches_only_the_selector_and_the_rate(self):
        script = fx_mod._EXCHANGE_RATE_JS

        assert script.count("fetch(") == 2
        assert "fetch(productsPath" in script and "fetch(ratePath" in script
        assert fx_mod._PRODUCTS_PATH.endswith("obtenerProductos?incluirTarjetas=true")
        assert fx_mod._RATE_PATH.endswith("pago/calcular-pago-internacional")

    def test_it_never_touches_the_payment_flow(self):
        everything = "\n".join(
            (fx_mod._EXCHANGE_RATE_JS, fx_mod._PRODUCTS_PATH, fx_mod._RATE_PATH)
        )

        for forbidden in (
            "autorizacion-pago-internacional",
            "validar-pago",
            "tarjeta-credito/pagos",
            "indices-financieros",
        ):
            assert forbidden not in everything

    def test_it_never_navigates_the_tab(self):
        navigation = re.compile(
            r"location\.(?:href|hash)\s*=(?!=)"
            r"|location\.(?:assign|replace)\b"
            r"|window\.location\s*=(?!=)"
        )

        assert not navigation.search(fx_mod._EXCHANGE_RATE_JS)

    def test_off_the_portal_host_it_fetches_nothing(self):
        script = fx_mod._EXCHANGE_RATE_JS

        guard = script.index(f"if (location.hostname !== '{_PORTAL_HOST}')")
        bail = script.index("return {status: 'off_portal'};")

        assert guard < bail < script.index("fetch(")

    def test_every_status_it_returns_is_whitelisted(self):
        statuses = set()
        for value in re.findall(r"\bstatus:\s*([^,}]+)", fx_mod._EXCHANGE_RATE_JS):
            statuses.update(re.findall(r"'(\w+)'", value))

        assert statuses == fx_mod._STATUSES

    def test_the_page_gets_the_paths_and_a_time_budget(self):
        page = _FakeFxPage(_ok())

        read_exchange_rate_surface(page)

        ((_, arg),) = page.calls
        # The whole call outlives the in-page fetch budget only by a margin.
        assert page.timeouts == [
            fx_mod._RENDER_TIMEOUTS_MS[0] + fx_mod._PAGE_CALL_TIMEOUT_MS
        ]
        assert arg == {
            "productsPath": fx_mod._PRODUCTS_PATH,
            "ratePath": fx_mod._RATE_PATH,
            "timeoutMs": fx_mod._RENDER_TIMEOUTS_MS[0],
        }


class TestParseRate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (950.0, Decimal("950.0")),
            (999.99, Decimal("999.99")),
            (950, Decimal("950")),
            ("1234.56", Decimal("1234.56")),
            (" 999.99 ", Decimal("999.99")),
        ],
    )
    def test_a_number_or_dot_decimal_string(self, raw, expected):
        assert parse_rate(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [None, 0, 0.0, -950.0, "", "abc", "999,99", "1.234,56", "1e3", 1e20,
         float("nan"), float("inf"), True, [950.0], {"valor": 950.0}, "1.123456789"],
    )
    def test_anything_else_is_no_rate(self, raw):
        assert parse_rate(raw) is None


class TestExchangeRateFromResult:
    def test_builds_the_usd_card_rate(self):
        rate = exchange_rate_from_result(_ok(rate=999.99, date="01/01/2026"))

        assert rate.institution == "banchile"
        assert (rate.base_currency, rate.quote_currency) == ("USD", "CLP")
        assert rate.rate == Decimal("999.99")
        assert rate.product == ProductRef(
            kind="credit_card", currency="USD", external_ref=None
        )
        assert rate.source == SOURCE_LABEL == "Pago internacional de tarjeta"
        assert rate.reported_as_of == datetime.date(2026, 1, 1)

    @pytest.mark.parametrize("date", [None, "", "sin fecha", "31/02/2026", 20260101])
    def test_a_missing_or_bad_date_leaves_reported_as_of_empty(self, date):
        rate = exchange_rate_from_result(_ok(date=date))

        assert rate.rate == Decimal("950.0")
        assert rate.reported_as_of is None

    @pytest.mark.parametrize(
        "result",
        [
            None,
            "ok",
            [],
            {},
            {"status": "no_card"},
            {"status": "rate_http", "code": 500},
            _ok(rate=None),
            _ok(rate=0),
            _ok(rate="no disponible"),
            {"tipoCambio": 950.0, "fechaConsultaTipoCambio": "01/01/2026"},
        ],
        ids=[
            "none", "string", "list", "empty", "no-card", "http-500", "no-rate",
            "zero", "text", "no-status",
        ],
    )
    def test_no_rate_without_an_ok_status_and_a_usable_rate(self, result):
        assert exchange_rate_from_result(result) is None

    def test_only_the_rate_and_its_date_are_read(self):
        rate = exchange_rate_from_result(_ok(**_PRIVATE))

        dumped = rate.model_dump_json()
        for value in _PRIVATE.values():
            assert value not in dumped


class TestDescribeFailure:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (None, "no result"),
            ({"status": "no_card"}, "no_card"),
            ({"status": "off_portal"}, "off_portal"),
            ({"status": "rate_http", "code": 500}, "rate_http 500"),
            ({"status": "products_http", "code": 401}, "products_http 401"),
            ({"status": "timeout"}, "timeout"),
            ({"status": "rate_http", "code": "500"}, "rate_http"),
            (_ok(rate=None), "no usable rate"),
            ({"status": "TITULAR SINTETICO"}, "unexpected result"),
            ({"status": ["no_card"]}, "unexpected result"),
        ],
    )
    def test_names_the_status_and_code_only(self, result, expected):
        assert describe_failure(result) == expected

    def test_never_echoes_a_payload_value(self):
        result = {"status": "rate_http", "code": 500, **_PRIVATE}

        described = describe_failure(result)

        for value in _PRIVATE.values():
            assert value not in described


class TestSurface:
    @pytest.fixture(autouse=True)
    def _no_waits(self, monkeypatch):
        monkeypatch.setattr(fx_mod, "_RENDER_TIMEOUTS_MS", (0,))

    def test_a_read_yields_one_product_scoped_rate(self):
        page = _FakeFxPage(_ok(rate=950.0))

        rates, failed = read_exchange_rate_surface(page)

        assert failed == ()
        assert [(r.product.kind, r.product.currency, r.rate) for r in rates] == [
            ("credit_card", "USD", Decimal("950.0"))
        ]
        assert len(page.calls) == 1

    def test_a_flaky_first_attempt_is_retried(self):
        page = _FakeFxPage({"status": "timeout"}, _ok(rate=999.99))

        rates, failed = read_exchange_rate_surface(page)

        assert failed == ()
        assert [r.rate for r in rates] == [Decimal("999.99")]
        assert len(page.calls) == 2
        assert page.gotos == [_PORTAL_HOME]  # the retry starts from the home

    @pytest.mark.parametrize(
        "result",
        [
            {"status": "no_card"},
            {"status": "off_portal"},
            {"status": "rate_http", "code": 500},
            {"status": "products_http", "code": 401},
            {"status": "rate_error"},
            _ok(rate=None),
            _ok(rate="no disponible"),
            RuntimeError("evaluate failed"),
        ],
        ids=["no-card", "off-portal", "rate-500", "products-401", "rate-error",
             "no-rate", "bad-rate", "evaluate-raises"],
    )
    def test_a_read_that_never_succeeds_is_a_failed_surface(self, result):
        page = _FakeFxPage(result)

        rates, failed = read_exchange_rate_surface(page)

        assert rates == []
        assert failed == (SURFACE,) == ("tipo de cambio",)
        assert len(page.calls) == fx_mod._SURFACE_ATTEMPTS

    def test_a_spent_read_budget_skips_the_rate(self, caplog):
        page = _FakeFxPage(_ok())

        with caplog.at_level(logging.WARNING):
            rates, failed = read_exchange_rate_surface(
                page, deadline=time.monotonic() - 1
            )

        assert (rates, failed) == ([], (SURFACE,))
        assert page.calls == []
        assert "skipped surfaces: tipo de cambio" in caplog.text

    def test_an_off_portal_page_is_brought_back_first(self):
        page = _FakeFxPage(_ok(), url="https://sitiospublicos.bancochile.cl/personas")

        rates, failed = read_exchange_rate_surface(page)

        assert failed == ()
        assert len(rates) == 1
        assert page.gotos == [_PORTAL_HOME]

    def test_logs_never_carry_payload_values(self, caplog):
        page = _FakeFxPage({"status": "rate_http", "code": 500, **_PRIVATE})

        with caplog.at_level(logging.DEBUG):
            read_exchange_rate_surface(page)

        assert "rate_http 500" in caplog.text
        for value in _PRIVATE.values():
            assert value not in caplog.text
