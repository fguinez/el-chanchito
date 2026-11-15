"""Tests for the Mercado Pago scraper: page parsers, transport and both legs.

No browser, no Chrome, no Playwright and no network. The parsers get HTML and
page data built here from scratch to mirror the structure of the real pages
(field names and nesting only). The transport runs against a fake tab that
answers the in-page fetch script from a table, CDP connections are scripted
fakes on a fake clock, the real-Chrome helpers are replaced by recorders
that launch nothing, and the scraper's legs run against
a monkeypatched backend facade (`scrape_account`).

Every value is synthetic, from the repo's canonical set: amounts 2.500.000 /
1.000.000 / 999.999, movement ids like A00000000000000B1, the pot uuid
00000000-0000-0000-0000-000000000001, "Persona Sintética", "Reserva
sintética".
"""

import asyncio
import contextlib
import inspect
import json
import logging
import sqlite3
import sys
import threading
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import main as main_mod
from db.writer import TransactionWriteResult, VanishedProductsResult
from scrapers import sign_in_marker
from scrapers.backends import mercadopago_web as web
from scrapers.backends import real_chrome
from scrapers.backends.mercadopago_web import (
    MercadoPagoAccount,
    MercadoPagoConnectError,
    MercadoPagoConnectionLostError,
    MercadoPagoFetchError,
    MercadoPagoPageError,
    MercadoPagoSessionError,
    MercadoPagoWebError,
    Movement,
    Pot,
)
from scrapers.backends.real_chrome import TargetSummary
from scrapers.institutions import mercadopago as mp_mod
from scrapers.institutions.mercadopago import MercadoPagoScraper

POT_ID = "00000000-0000-0000-0000-000000000001"
POT_ID_2 = "00000000-0000-0000-0000-000000000002"
MOVEMENT_ID = "A00000000000000B1"
MOVEMENT_ID_2 = "A00000000000000B2"
MOVEMENT_ID_3 = "A00000000000000B3"
# U+2212 MINUS SIGN, which the page may print instead of an ASCII hyphen.
UNICODE_MINUS = chr(0x2212)
# 15:00 UTC is midday in Santiago; 02:30 UTC is still the previous evening.
MIDDAY_UTC = "2026-11-10T15:00:00.000Z"
EVENING_UTC = "2026-11-11T02:30:00.000Z"


def _html(context) -> str:
    """A page whose inline script assigns the data, then runs more script."""
    return (
        "<!doctype html><html><head><title>Sintetico</title></head><body>"
        f"<script>_n.ctx.r={json.dumps(context)};"
        '_n.ctx.s={"synthetic":true};window.synthetic=function(){return {};};'
        "</script></body></html>"
    )


def _page(props: dict) -> str:
    return _html({"appProps": {"pageProps": props}, "synthetic": True})


def _balance_props(
    raw=2_500_000,
    status="success",
    gain=999_999,
    subtitle="Generando ganancias al 1,0% anual",
) -> dict:
    return {
        "initialResult": {
            "balance": {
                "status": {"type": status},
                "result": {
                    "available": {
                        "id": "available",
                        "title": "Invertido en BICE",
                        "amount": {
                            "raw": raw,
                            "value": {"fraction": "2.500.000"},
                            "iso": "CLP",
                            "currency": "$",
                        },
                    },
                    "invested": {
                        "data": {
                            "title": {"middle": {"raw": gain}},
                            "subtitle": subtitle,
                        }
                    },
                },
            }
        }
    }


def _pot(pot_id=POT_ID, name="Reserva sintética", raw=1_000_000, currency="CLP") -> dict:
    return {
        "id": pot_id,
        "name": name,
        "category": {"id": "synthetic"},
        "amount": {"currency_id": currency, "fraction": "1.000.000", "raw": raw},
        "goal": {},
        "template_name": "synthetic",
        "created_at": "2026-01-01T00:00:00.000Z",
    }


def _pots_props(pots: list, status="success", rate="1.0", is_invested=True) -> dict:
    return {
        "hub": {
            "result": {
                "potsDataHub": {
                    "status": {"type": status},
                    "result": {"pots": pots, "pots_amount": {"raw": 1_000_000}},
                },
                "investmentData": {
                    "data": {"is_invested": is_invested, "rate": rate, "fund_name": "Sintetico"}
                },
            }
        }
    }


def _item(
    movement_id=MOVEMENT_ID,
    when=MIDDAY_UTC,
    fraction="-999.999",
    cents="00",
    title="COMERCIO SINTETICO",
    description="Pago",
    kind="payment",
    currency="CLP",
) -> dict:
    return {
        "id": f"synthetic-{movement_id}",
        "link": f"/banking/balance/movements/{movement_id}",
        "movement_id": movement_id,
        "datetime": when,
        "date_text": "12:00",
        "amount": {"currency_id": currency, "fraction": fraction, "cents": cents},
        "title": title,
        "description": description,
        "thumbnail": {"url": None, "icon": "synthetic"},
        "metadata": {
            "kind": kind,
            "detail": "synthetic",
            "description": "synthetic",
            "roles": [],
            "processing_strategy": "synthetic",
            "generated_from": "synthetic",
        },
    }


def _movements_props(items: list, pages=1) -> dict:
    return {
        "page": 1,
        "listData": {
            "total": len(items),
            "pages": pages,
            "groups": [{"title": "Hoy", "right_content": {}, "items": items}] if items else [],
        },
    }


class TestExtractContext:
    def test_decodes_the_object_and_ignores_the_script_after_it(self):
        """More JavaScript follows the object in the same script."""
        context = {"appProps": {"pageProps": {"synthetic": 1}}}

        assert web.extract_context(_html(context)) == context

    def test_script_like_text_inside_a_string_does_not_end_the_object(self):
        """Only the JSON grammar decides where the object ends."""
        context = {"appProps": {"pageProps": {"title": 'x"};_n.ctx.s={};</script>'}}}

        assert web.extract_context(_html(context)) == context

    def test_whitespace_after_the_marker(self):
        """A space between the marker and the object is tolerated."""
        html = '<script>_n.ctx.r= {"appProps": 1};more();</script>'

        assert web.extract_context(html) == {"appProps": 1}

    @pytest.mark.parametrize(
        "before",
        [
            pytest.param("<script>_n.ctx.r=_n.ctx.r||{};</script>", id="guard-assignment"),
            pytest.param('<script>var s="_n.ctx.r=";</script>', id="inside-a-string"),
            pytest.param("<script>// set by _n.ctx.r= below\n</script>", id="inside-a-comment"),
            pytest.param("<script>_n.ctx.r={};</script>", id="an-empty-object-first"),
            pytest.param(
                '<script>_n.ctx.r={"other": {"appProps": 1}};</script>', id="nested-elsewhere"
            ),
            pytest.param("<script>if(_n.ctx.r==null){}</script>", id="a-comparison"),
        ],
    )
    def test_an_earlier_marker_without_the_data_is_passed_over(self, before):
        """Every occurrence is tried until one carries appProps."""
        context = {"appProps": {"pageProps": {"synthetic": 1}}}

        assert web.extract_context(before + _html(context)) == context

    @pytest.mark.parametrize(
        ("html", "message"),
        [
            pytest.param(
                "<html><body>Ingresa a tu cuenta</body></html>", "marker missing", id="no-marker"
            ),
            pytest.param("<script>_n.ctx.r=undefined;</script>", "not JSON", id="not-json"),
            pytest.param("<script>_n.ctx.r=</script>", "not JSON", id="nothing-after-marker"),
            pytest.param("<script>_n.ctx.r=_n.ctx.r||{};</script>", "not JSON", id="only-a-guard"),
            pytest.param("<script>_n.ctx.r=[1, 2];</script>", "no appProps", id="not-an-object"),
            pytest.param(
                '<script>_n.ctx.r={"a": 1};</script>', "no appProps", id="object-without-app-props"
            ),
            pytest.param(None, "not text", id="not-text"),
        ],
    )
    def test_unreadable_page_raises(self, html, message):
        """A page without the data object is not one this module reads."""
        with pytest.raises(MercadoPagoPageError, match=message):
            web.extract_context(html)

    def test_error_carries_nothing_from_the_page(self):
        """Neither the message nor a chained error holds page content."""
        html = '<script>_n.ctx.r={"name": "Persona Sintética", broken;</script>'

        with pytest.raises(MercadoPagoPageError) as info:
            web.extract_context(html)

        assert "Persona" not in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None


class TestPageProps:
    def test_reads_app_props_page_props(self):
        """Everything a page carries lives under appProps.pageProps."""
        assert web.page_props_from_html(_page({"synthetic": 1})) == {"synthetic": 1}

    @pytest.mark.parametrize(
        "context",
        [
            pytest.param({}, id="no-app-props"),
            pytest.param({"appProps": {}}, id="no-page-props"),
            pytest.param({"appProps": None}, id="app-props-null"),
        ],
    )
    def test_missing_node_raises(self, context):
        """A context without pageProps is not a page this module reads."""
        with pytest.raises(MercadoPagoPageError, match="missing"):
            web.page_props(context)


class TestParseClpAmount:
    @pytest.mark.parametrize(
        ("fraction", "cents", "expected"),
        [
            pytest.param("999.999", "00", 999_999, id="positive"),
            pytest.param("-999.999", "00", -999_999, id="ascii-hyphen"),
            pytest.param(f"{UNICODE_MINUS}999.999", "00", -999_999, id="unicode-minus"),
            pytest.param("2.500.000", "00", 2_500_000, id="millions"),
            pytest.param("1000000", "00", 1_000_000, id="no-separators"),
            pytest.param("999.999", None, 999_999, id="no-cents"),
            pytest.param("999.999", "", 999_999, id="blank-cents"),
            pytest.param("999.999", "49", 999_999, id="rounds-down"),
            pytest.param("999.999", "50", 1_000_000, id="half-rounds-up"),
            pytest.param("-999.999", "50", -1_000_000, id="negative-half-rounds-away"),
            pytest.param("0", "40", 0, id="under-half-a-peso"),
        ],
    )
    def test_fraction_and_cents(self, fraction, cents, expected):
        """Dots group thousands, the sign leads, cents round half up."""
        amount = {"currency_id": "CLP", "fraction": fraction, "cents": cents}

        assert web.parse_clp_amount(amount) == expected

    @pytest.mark.parametrize(
        "amount",
        [
            pytest.param(None, id="not-an-object"),
            pytest.param({"cents": "00"}, id="no-fraction"),
            pytest.param({"fraction": 999_999, "cents": "00"}, id="fraction-not-text"),
            pytest.param({"fraction": "abc", "cents": "00"}, id="not-a-number"),
            pytest.param({"fraction": "1.5", "cents": "00"}, id="dot-is-not-a-decimal"),
            pytest.param({"fraction": "999,999", "cents": "00"}, id="comma"),
            pytest.param({"fraction": "+999.999", "cents": "00"}, id="plus-sign"),
            pytest.param({"fraction": "999.999", "cents": "5"}, id="one-digit-cents"),
            pytest.param({"fraction": "999.999", "cents": 50}, id="cents-not-text"),
        ],
    )
    def test_unexpected_form_is_none(self, amount):
        """An amount not in the page's form is never guessed at."""
        assert web.parse_clp_amount(amount) is None


class TestSantiagoDate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(MIDDAY_UTC, date(2026, 11, 10), id="midday-utc"),
            pytest.param(EVENING_UTC, date(2026, 11, 10), id="evening-utc-is-the-previous-day"),
            pytest.param("2026-11-01T01:00:00.000Z", date(2026, 10, 31), id="crosses-the-month"),
            pytest.param("2026-11-10T23:30:00-03:00", date(2026, 11, 10), id="explicit-offset"),
        ],
    )
    def test_utc_is_converted_before_taking_the_date(self, raw, expected):
        """The ledger stamps UTC; the movement's day is Santiago's."""
        assert web.santiago_date(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("2026-11-10T15:00:00", id="no-offset"),
            pytest.param("10/11/2026", id="not-iso"),
            pytest.param("", id="blank"),
            pytest.param(None, id="missing"),
            pytest.param(1_000_000, id="not-text"),
        ],
    )
    def test_unusable_timestamp_is_none(self, raw):
        """A timestamp without its zone could land on the wrong day."""
        assert web.santiago_date(raw) is None


class TestParseBalance:
    def test_available_amount_in_whole_pesos(self):
        """The wallet balance is result.available.amount.raw."""
        assert web.parse_balance(_balance_props(raw=2_500_000)) == 2_500_000

    def test_a_fractional_raw_rounds_half_up(self):
        """CLP is whole: a raw with cents rounds, never truncates."""
        assert web.parse_balance(_balance_props(raw=999_999.5)) == 1_000_000

    def test_through_the_html(self):
        """The whole path: page text to balance."""
        html = _page(_balance_props(raw=1_000_000))

        assert web.parse_balance(web.page_props_from_html(html)) == 1_000_000

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("2500000", id="text"),
            pytest.param(True, id="bool"),
            pytest.param(None, id="null"),
            pytest.param(float("nan"), id="nan"),
            pytest.param(1e40, id="too-large"),
        ],
    )
    def test_a_raw_that_is_not_a_number_raises(self, raw):
        """No balance beats a guessed one."""
        with pytest.raises(MercadoPagoPageError, match="not a number"):
            web.parse_balance(_balance_props(raw=raw))

    def test_status_other_than_success_raises(self):
        """The page says when its balance widget failed to load."""
        with pytest.raises(MercadoPagoPageError, match="status is not success"):
            web.parse_balance(_balance_props(status="error"))

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param(("initialResult",), id="initialResult"),
            pytest.param(("initialResult", "balance"), id="balance"),
            pytest.param(("initialResult", "balance", "result"), id="result"),
            pytest.param(("initialResult", "balance", "result", "available"), id="available"),
            pytest.param(
                ("initialResult", "balance", "result", "available", "amount"), id="amount"
            ),
        ],
    )
    def test_missing_node_raises_naming_the_field(self, path):
        """A missing top-level node is a changed page, named by its field."""
        props = _balance_props()
        node = props
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

        with pytest.raises(MercadoPagoPageError, match=f"{path[-1]} missing"):
            web.parse_balance(props)


class TestParseWalletYield:
    def test_rate_and_gain_are_read(self):
        """The gain is title.middle.raw; the rate comes out of the subtitle."""
        props = _balance_props(gain=999_999, subtitle="Generando ganancias al 4,50% anual")

        result = web.parse_wallet_yield(props)

        assert result == web.WalletYield(rate_pct=4.5, profit_12m=999_999)

    @pytest.mark.parametrize(
        ("subtitle", "expected"),
        [
            pytest.param("Generando ganancias al 1,0% anual", 1.0, id="comma-decimal"),
            pytest.param("Generando ganancias al 1.0% anual", 1.0, id="dot-decimal"),
            pytest.param("Generando ganancias al 4 % anual", 4.0, id="whole-with-space"),
            pytest.param("Generando ganancias al 4,50% anual", 4.5, id="two-decimals"),
            pytest.param("Generando ganancias al 1% anual", 1.0, id="whole-no-space"),
            pytest.param("Generando ganancias al 12,5 % anual", 12.5, id="decimal-with-space"),
            pytest.param("Generando ganancias al -0,5% anual", -0.5, id="negative"),
            pytest.param(
                "Generando ganancias al \u22120,5% anual", -0.5, id="negative-unicode-minus"
            ),
            pytest.param("GENERANDO GANANCIAS AL 1,0% ANUAL", 1.0, id="upper-case"),
            pytest.param("100% disponible, al 1,0% anual", 1.0, id="first-percent-not-anual"),
        ],
    )
    def test_rate_is_the_number_before_percent_anual(self, subtitle, expected):
        """Only the number between "al" and "% anual" is the rate."""
        assert web.parse_wallet_yield(_balance_props(subtitle=subtitle)).rate_pct == expected

    @pytest.mark.parametrize(
        "subtitle",
        [
            pytest.param("Generando ganancias", id="no-percent"),
            pytest.param("Generando ganancias al %", id="percent-without-number"),
            pytest.param("Generando ganancias al 1.234,5% anual", id="dotted-thousands"),
            pytest.param("Generando ganancias al 1.0.0% anual", id="two-dots"),
            pytest.param("Activa y gana hasta 1,0% anual", id="hasta-promo"),
            pytest.param("Generando ganancias al 1,0%", id="percent-without-anual"),
            pytest.param("100% disponible", id="percent-not-a-rate"),
            pytest.param(None, id="null"),
            pytest.param(1.0, id="not-text"),
        ],
    )
    def test_malformed_subtitle_leaves_the_rate_empty(self, subtitle):
        """No rate beats a guessed one; the gain is still read."""
        result = web.parse_wallet_yield(_balance_props(subtitle=subtitle))

        assert result == web.WalletYield(rate_pct=None, profit_12m=999_999)

    @pytest.mark.parametrize(
        ("gain", "expected"),
        [
            pytest.param(999_999.5, 1_000_000, id="rounds-half-up"),
            pytest.param("999999", None, id="text"),
            pytest.param(True, None, id="bool"),
            pytest.param(None, None, id="null"),
        ],
    )
    def test_gain_is_whole_pesos_or_empty(self, gain, expected):
        """A gain that is not a JSON number is left empty."""
        assert web.parse_wallet_yield(_balance_props(gain=gain)).profit_12m == expected

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="empty-page"),
            pytest.param("synthetic", id="not-an-object"),
            pytest.param(None, id="null"),
        ],
    )
    def test_missing_page_data_is_empty_not_an_error(self, props):
        """These are extras: a page without them never fails the read."""
        assert web.parse_wallet_yield(props) == web.WalletYield()

    def test_missing_invested_node_is_empty_and_balance_still_reads(self):
        """A balance page without `invested` keeps its balance, without yield."""
        props = _balance_props()
        del props["initialResult"]["balance"]["result"]["invested"]

        result = web.parse_wallet_yield(props)

        assert result == web.WalletYield(rate_pct=None, profit_12m=None)
        assert web.parse_balance(props) == 2_500_000


class TestParsePots:
    def test_no_pots(self):
        """An account without pots is a real reading, not a failure."""
        assert web.parse_pots(_pots_props([])) == ([], 0)

    def test_one_pot(self):
        """Its uuid, name and CLP amount."""
        pots, skipped = web.parse_pots(_pots_props([_pot()]))

        assert pots == [Pot(id=POT_ID, name="Reserva sintética", amount=1_000_000, rate_pct=1.0)]
        assert skipped == 0

    def test_every_pot_gets_the_hub_rate_and_no_gain(self):
        """One shared rate for all pots; the web shows no per-pot gain."""
        props = _pots_props([_pot(), _pot(pot_id=POT_ID_2, raw=999_999)], rate="4,50")

        pots, _ = web.parse_pots(props)

        assert [pot.rate_pct for pot in pots] == [4.5, 4.5]

    @pytest.mark.parametrize(
        ("rate", "is_invested"),
        [
            pytest.param("1.0", False, id="not-invested"),
            pytest.param("1.0", None, id="invested-flag-missing"),
            pytest.param("1.0", "true", id="invested-flag-not-a-bool"),
            pytest.param("sintetico", True, id="rate-not-a-number"),
            pytest.param("1.0.0", True, id="rate-two-decimal-marks"),
            pytest.param("", True, id="rate-blank"),
            pytest.param(None, True, id="rate-null"),
            pytest.param(True, True, id="rate-bool"),
        ],
    )
    def test_pot_rate_is_empty_unless_invested_with_a_readable_rate(self, rate, is_invested):
        """The pots keep their balances; only the rate is left empty."""
        props = _pots_props([_pot()], rate=rate, is_invested=is_invested)

        pots, skipped = web.parse_pots(props)

        assert [(pot.amount, pot.rate_pct) for pot in pots] == [(1_000_000, None)]
        assert skipped == 0

    def test_a_numeric_rate_is_read_too(self):
        """A rate sent as a JSON number reads like its string form."""
        pots, _ = web.parse_pots(_pots_props([_pot()], rate=1.0))

        assert pots[0].rate_pct == 1.0

    @pytest.mark.parametrize("rate", ["-0,5", "\u22120.5"], ids=["ascii-minus", "unicode-minus"])
    def test_a_negative_rate_is_read_like_the_wallets(self, rate):
        """The pots accept the same signed rate the wallet subtitle does."""
        pots, _ = web.parse_pots(_pots_props([_pot()], rate=rate))

        assert pots[0].rate_pct == -0.5

    def test_missing_investment_data_leaves_the_rate_empty(self):
        """A hub without `investmentData` still yields its pots."""
        props = _pots_props([_pot()])
        del props["hub"]["result"]["investmentData"]

        pots, skipped = web.parse_pots(props)

        assert ([pot.rate_pct for pot in pots], skipped) == ([None], 0)

    def test_several_pots_keep_their_order(self):
        """One record per pot, in the page's order."""
        props = _pots_props(
            [_pot(), _pot(pot_id=POT_ID_2, name="Reserva sintética 2", raw=999_999)]
        )

        pots, skipped = web.parse_pots(props)

        assert [(pot.id, pot.amount) for pot in pots] == [
            (POT_ID, 1_000_000),
            (POT_ID_2, 999_999),
        ]
        assert skipped == 0

    def test_uuid_is_lower_cased(self):
        """The id feeds external_ref, so its case must not split a product."""
        pots, _ = web.parse_pots(_pots_props([_pot(pot_id=POT_ID.replace("0", "A"))]))

        assert pots[0].id == POT_ID.replace("0", "a")

    def test_amount_falls_back_to_the_fraction(self):
        """A pot without `raw` is read from its dotted fraction."""
        pot = _pot()
        del pot["amount"]["raw"]

        pots, _ = web.parse_pots(_pots_props([pot]))

        assert pots[0].amount == 1_000_000

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(_pot(pot_id="synthetic-not-a-uuid"), id="id-not-a-uuid"),
            pytest.param(_pot(pot_id=None), id="no-id"),
            pytest.param(_pot(currency="USD"), id="not-clp"),
            pytest.param({"id": POT_ID_2, "name": "Reserva sintética"}, id="no-amount"),
            pytest.param("synthetic", id="not-an-object"),
            pytest.param(_pot(), id="repeats-an-id"),
        ],
    )
    def test_a_bad_entry_is_skipped_and_counted(self, bad, caplog):
        """The good pot survives; the skip is counted, never described."""
        caplog.set_level(logging.WARNING, logger=web.__name__)

        pots, skipped = web.parse_pots(_pots_props([_pot(), bad]))

        assert [pot.id for pot in pots] == [POT_ID]
        assert skipped == 1
        assert "1 of 2 pot(s) skipped" in caplog.text
        assert "Reserva" not in caplog.text

    def test_a_missing_name_is_blank(self):
        """A pot keeps its balance even without a name."""
        pot = _pot()
        del pot["name"]

        pots, skipped = web.parse_pots(_pots_props([pot]))

        assert (pots[0].name, skipped) == ("", 0)

    def test_status_other_than_success_raises(self):
        """A pots widget that failed to load is not an empty account."""
        with pytest.raises(MercadoPagoPageError, match="status is not success"):
            web.parse_pots(_pots_props([], status="error"))

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="no-hub"),
            pytest.param({"hub": {"result": {}}}, id="no-pots-data"),
            pytest.param(
                {"hub": {"result": {"potsDataHub": {"status": {"type": "success"}, "result": {}}}}},
                id="no-pots-list",
            ),
            pytest.param(_pots_props("synthetic"), id="pots-not-a-list"),
        ],
    )
    def test_missing_node_raises(self, props):
        """A changed page raises instead of reading as no pots."""
        with pytest.raises(MercadoPagoPageError):
            web.parse_pots(props)


class TestParseMovements:
    def test_money_out_is_negative(self):
        """The ledger's own sign is kept: negative is money out."""
        rows, pages, _ = web.parse_movements(_movements_props([_item()]))

        assert rows == [
            Movement(
                movement_id=MOVEMENT_ID,
                date=date(2026, 11, 10),
                amount=-999_999,
                title="COMERCIO SINTETICO",
                description="Pago",
            )
        ]
        assert pages == 1

    def test_money_in_is_positive(self):
        """A transfer received: the title is the counterparty."""
        item = _item(
            fraction="1.000.000",
            title="Persona Sintética",
            description="Transferencia recibida",
            kind="transfer",
        )

        rows, _, _ = web.parse_movements(_movements_props([item]))

        assert (rows[0].amount, rows[0].title) == (1_000_000, "Persona Sintética")

    @pytest.mark.parametrize("minus", ["-", UNICODE_MINUS], ids=["ascii-hyphen", "unicode-minus"])
    def test_both_minus_characters(self, minus):
        """The page may print either minus character."""
        rows, _, _ = web.parse_movements(_movements_props([_item(fraction=f"{minus}999.999")]))

        assert rows[0].amount == -999_999

    @pytest.mark.parametrize(
        ("title", "description", "kind"),
        [
            pytest.param("Rendimientos BICE", "Rentabilidad", "investment", id="daily-yield"),
            pytest.param("Beneficio de Mercado Pago", "", "gift", id="cashback"),
        ],
    )
    def test_yield_and_cashback_rows_are_ordinary_income(self, title, description, kind):
        """They are ledger rows like any other, positive."""
        item = _item(fraction="999.999", title=title, description=description, kind=kind)

        rows, _, _ = web.parse_movements(_movements_props([item]))

        assert (rows[0].amount, rows[0].title, rows[0].description) == (
            999_999,
            title,
            description,
        )

    @pytest.mark.parametrize("description", ["", None, "   "], ids=["blank", "null", "spaces"])
    def test_empty_description(self, description):
        """A row without a description keeps a blank one."""
        rows, _, _ = web.parse_movements(_movements_props([_item(description=description)]))

        assert rows[0].description == ""

    def test_evening_utc_row_lands_on_the_previous_santiago_day(self):
        """02:30 UTC is still the evening before in Santiago."""
        rows, _, _ = web.parse_movements(_movements_props([_item(when=EVENING_UTC)]))

        assert rows[0].date == date(2026, 11, 10)

    def test_page_count_is_the_one_the_page_reports(self):
        """`pages` is recomputed by the server for the page size asked."""
        _, pages, _ = web.parse_movements(_movements_props([_item()], pages=3))

        assert pages == 3

    def test_rows_of_every_group_in_order(self):
        """The ledger groups rows by day; all of them are read, newest first."""
        props = _movements_props([_item()])
        props["listData"]["groups"].append(
            {
                "title": "Ayer",
                "items": [_item(movement_id=MOVEMENT_ID_2), _item(movement_id=MOVEMENT_ID_3)],
            }
        )

        rows, _, _ = web.parse_movements(props)

        assert [row.movement_id for row in rows] == [MOVEMENT_ID, MOVEMENT_ID_2, MOVEMENT_ID_3]

    def test_empty_ledger(self):
        """No groups and no pages: nothing happened, nothing failed."""
        assert web.parse_movements(_movements_props([], pages=0)) == ([], 0, 0)

    @pytest.mark.parametrize(
        ("bad", "reason"),
        [
            pytest.param(_item(movement_id=None), "id", id="no-id"),
            pytest.param(_item(movement_id="synthetic id"), "id", id="id-with-a-space"),
            pytest.param(_item(when="10/11/2026"), "date", id="date-not-iso"),
            pytest.param(_item(when="2026-11-10T15:00:00"), "date", id="date-without-zone"),
            pytest.param(_item(currency="USD"), "currency", id="not-clp"),
            pytest.param(_item(fraction="abc"), "amount", id="amount-not-a-number"),
            pytest.param(_item(title=""), "title", id="blank-title"),
            pytest.param("synthetic", "shape", id="not-an-object"),
        ],
    )
    def test_a_malformed_row_is_skipped_with_a_count(self, bad, reason, caplog):
        """The good row survives; the warning counts, it never quotes a row."""
        caplog.set_level(logging.WARNING, logger=web.__name__)
        good = _item(movement_id=MOVEMENT_ID_2)

        rows, _, skipped = web.parse_movements(_movements_props([bad, good]))

        assert [row.movement_id for row in rows] == [MOVEMENT_ID_2]
        assert skipped == 1
        assert f"1 movement row(s) skipped ({reason}: 1)" in caplog.text
        assert "SINTETICO" not in caplog.text
        assert MOVEMENT_ID not in caplog.text

    def test_a_row_that_rounds_to_zero_is_dropped_quietly(self, caplog):
        """Under half a peso is no money: skipped without a warning."""
        caplog.set_level(logging.WARNING, logger=web.__name__)
        items = [_item(fraction="0", cents="40"), _item(movement_id=MOVEMENT_ID_2)]

        rows, _, skipped = web.parse_movements(_movements_props(items))

        assert [row.movement_id for row in rows] == [MOVEMENT_ID_2]
        assert skipped == 0
        assert caplog.records == []

    def test_a_page_whose_rows_all_fail_raises(self):
        """Every row unreadable is a changed page, not a quiet month."""
        items = [_item(movement_id=None), _item(movement_id=None)]

        with pytest.raises(MercadoPagoPageError, match="no row could be read"):
            web.parse_movements(_movements_props(items))

    @pytest.mark.parametrize(
        ("props", "message"),
        [
            pytest.param({}, "listData missing", id="no-list-data"),
            pytest.param(
                {"listData": "synthetic"}, "pages is not a count", id="list-data-not-an-object"
            ),
            pytest.param({"listData": {"groups": []}}, "pages is not a count", id="no-pages"),
            pytest.param(
                {"listData": {"pages": True, "groups": []}}, "pages is not a count", id="pages-bool"
            ),
            pytest.param(
                {"listData": {"pages": -1, "groups": []}},
                "pages is not a count",
                id="pages-negative",
            ),
            pytest.param({"listData": {"pages": 1}}, "groups is not a list", id="no-groups"),
        ],
    )
    def test_missing_node_raises(self, props, message):
        """A changed page raises instead of reading as an empty ledger."""
        with pytest.raises(MercadoPagoPageError, match=message):
            web.parse_movements(props)


# --- Transport: the in-page fetch contract ------------------------------------


class _TargetClosedError(PlaywrightError):
    """Named like Playwright's private `TargetClosedError`, which is matched by name."""


_TargetClosedError.__name__ = "TargetClosedError"


def _raise(error):
    def call(*args, **kwargs):
        raise error

    return call


class _FakeHandle:
    def __init__(self, value):
        self.value = value
        self.disposed = False

    def json_value(self):
        return self.value

    def dispose(self):
        self.disposed = True


class _FakeTab:
    """A signed-in tab stand-in: answers the in-page fetch script from a table.

    `responses` maps a page name ("balance", "pots") or a ("movements", N)
    pair to the script's result, or to an exception the call raises.
    """

    url = web.START_URL

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple] = []
        self.scripts: list[str] = []
        self.timeouts: list = []
        self.handles: list[_FakeHandle] = []

    def wait_for_function(self, script, arg=None, timeout=None):
        self.scripts.append(script)
        self.timeouts.append(timeout)
        surface, number = arg["surface"], arg["pageNumber"]
        self.calls.append((surface, number) if surface == "movements" else surface)
        result = self.responses.get((surface, number), self.responses.get(surface))
        if isinstance(result, Exception):
            raise result
        handle = _FakeHandle(result)
        self.handles.append(handle)
        return handle


def _ok(props: dict) -> dict:
    return {"status": "ok", "html": _page(props)}


def _tab(**overrides) -> _FakeTab:
    """A healthy account: a balance, one pot and a one-page ledger."""
    responses = {
        "balance": _ok(_balance_props()),
        "pots": _ok(_pots_props([_pot()])),
        ("movements", 1): _ok(_movements_props([_item()])),
    }
    responses.update(overrides)
    return _FakeTab(responses)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)


class TestFetchScript:
    def test_only_the_three_account_pages_can_be_fetched(self):
        """The allowlist is fixed inside the script; Python only names a page."""
        assert web.ALLOWED_PATHS == {
            "balance": "/banking/balance",
            "pots": "/savings/hub",
            "movements": "/banking/balance/movements",
        }
        assert json.dumps(web.ALLOWED_PATHS) in web._FETCH_JS

    def test_the_script_checks_the_host_and_never_follows_a_redirect(self):
        """Same origin only, with the session cookie, redirects left unfollowed."""
        script = web._FETCH_JS

        assert 'location.host !== "www.mercadopago.cl"' in script
        assert "credentials: 'include'" in script
        assert "redirect: 'manual'" in script
        assert "location.origin + path" in script
        assert script.index("location.host") < script.index("fetch(")

    @pytest.mark.parametrize("path", ["/home", "/activities", "/banking/movements"])
    def test_forbidden_pages_are_not_reachable(self, path):
        """Neither the home route nor the other two feeds is in the script."""
        assert path not in web.ALLOWED_PATHS.values()
        assert f'"{path}"' not in web._FETCH_JS
        assert f"'{path}'" not in web._FETCH_JS

    def test_the_backend_never_navigates(self):
        """No goto, reload or new tab anywhere: the tab only ever fetches."""
        source = inspect.getsource(web)

        for call in (".goto(", ".reload(", ".new_page(", ".go_back(", ".evaluate("):
            assert call not in source

    def test_the_tab_opens_on_the_balance_page(self):
        """The start URL is the balance page, not the home route."""
        assert web.START_URL == "https://www.mercadopago.cl/banking/balance"


class TestHtmlFromFetch:
    def test_ok_returns_the_page_text(self):
        """A 200 with text is the page."""
        assert (
            web.html_from_fetch({"status": "ok", "html": "<html></html>"}, "balance")
            == "<html></html>"
        )

    @pytest.mark.parametrize(
        "result",
        [
            pytest.param({"status": "redirect"}, id="opaque-redirect"),
            pytest.param({"status": "off_portal"}, id="tab-off-the-portal"),
            pytest.param({"status": "http", "code": 401}, id="http-401"),
            pytest.param({"status": "http", "code": 403}, id="http-403"),
        ],
    )
    def test_session_lost_signals(self, result):
        """Each means a human must sign in again, and the message says how."""
        with pytest.raises(MercadoPagoSessionError, match="make mercadopago-login"):
            web.html_from_fetch(result, "balance")

    @pytest.mark.parametrize(
        ("result", "message"),
        [
            pytest.param({"status": "http", "code": 500}, "pots: HTTP 500", id="http-500"),
            pytest.param({"status": "http", "code": 404}, "pots: HTTP 404", id="http-404"),
            pytest.param({"status": "http", "code": "x"}, "pots: HTTP error", id="http-no-code"),
            pytest.param({"status": "timeout"}, "pots: timed out", id="timeout"),
            pytest.param({"status": "error"}, "pots: network error", id="network"),
            pytest.param({"status": "too_large"}, "pots: page too large", id="too-large"),
            pytest.param({"status": "not_allowed"}, "pots: page not allowed", id="not-allowed"),
            pytest.param({"status": "ok"}, "pots: no page text", id="ok-without-text"),
            pytest.param(
                {"status": "synthetic"}, "pots: unexpected fetch result", id="unknown-status"
            ),
            pytest.param(None, "pots: unexpected fetch result", id="no-result"),
        ],
    )
    def test_other_failures_name_the_page_and_a_status(self, result, message):
        """A failed fetch is a fetch error, never a session error."""
        with pytest.raises(MercadoPagoFetchError) as info:
            web.html_from_fetch(result, "pots")

        assert str(info.value) == message

    def test_an_unknown_status_is_never_echoed(self):
        """The message is built from fixed words, not from the result."""
        result = {"status": "Persona Sintética $ 2.500.000", "html": "<b>999.999</b>"}

        with pytest.raises(MercadoPagoFetchError) as info:
            web.html_from_fetch(result, "movements")

        assert str(info.value) == "movements: unexpected fetch result"


class TestReadAccount:
    def test_reads_balance_pots_and_one_ledger_page(self):
        """The healthy path: three fetches, nothing to warn about."""
        tab = _tab()

        account = web.read_account(tab)

        assert account.balance == 2_500_000
        assert account.wallet_yield == web.WalletYield(rate_pct=1.0, profit_12m=999_999)
        assert account.pots == [
            Pot(id=POT_ID, name="Reserva sintética", amount=1_000_000, rate_pct=1.0)
        ]
        assert [row.movement_id for row in account.movements] == [MOVEMENT_ID]
        assert account.warnings == []
        assert tab.calls == ["balance", "pots", ("movements", 1)]

    def test_every_page_call_is_bounded_and_uses_the_one_script(self):
        """Each read runs the fixed script under a timeout and frees its handle."""
        tab = _tab()

        web.read_account(tab)

        assert all(script is web._FETCH_JS for script in tab.scripts)
        assert tab.timeouts == [web.EVALUATE_TIMEOUT_MS] * 3
        assert all(handle.disposed for handle in tab.handles)

    @pytest.mark.parametrize(
        "balance",
        [
            pytest.param({"status": "redirect"}, id="redirect"),
            pytest.param({"status": "off_portal"}, id="tab-off-the-portal"),
            pytest.param({"status": "http", "code": 401}, id="http-401"),
            pytest.param({"status": "http", "code": 403}, id="http-403"),
        ],
    )
    def test_session_lost_on_the_balance_page_stops_everything(self, balance):
        """The first page is the session probe: nothing else is fetched after it."""
        tab = _tab(balance=balance)

        with pytest.raises(MercadoPagoSessionError, match="make mercadopago-login"):
            web.read_account(tab)

        assert tab.calls == ["balance"]

    @pytest.mark.parametrize(
        ("balance", "reason"),
        [
            pytest.param(
                {"status": "ok", "html": "<html><body>Sintetico</body></html>"},
                "page data marker missing",
                id="page-without-the-marker",
            ),
            pytest.param(
                _ok({"synthetic": 1}),
                "balance: initialResult missing",
                id="page-without-the-balance-node",
            ),
            pytest.param(
                _ok(_balance_props(status="error")),
                "balance: status is not success",
                id="balance-widget-failed",
            ),
        ],
    )
    def test_a_200_balance_page_without_its_data_is_not_a_lost_session(self, balance, reason):
        """A changed page or a widget that did not load asks nobody to sign in."""
        tab = _tab(balance=balance)

        with pytest.raises(MercadoPagoWebError) as info:
            web.read_account(tab)

        assert not isinstance(info.value, MercadoPagoSessionError)
        assert f"balance page not readable ({reason})" in str(info.value)
        assert "make mercadopago-login" not in str(info.value)
        assert tab.calls == ["balance"]

    @pytest.mark.parametrize(
        "balance",
        [
            pytest.param({"status": "timeout"}, id="timeout"),
            pytest.param({"status": "error"}, id="network"),
            pytest.param({"status": "http", "code": 503}, id="http-503"),
            pytest.param(PlaywrightTimeoutError("synthetic"), id="call-timed-out"),
        ],
    )
    def test_an_unreachable_balance_page_is_not_a_lost_session(self, balance):
        """A network or server failure does not ask the human to sign in."""
        tab = _tab(balance=balance)

        with pytest.raises(MercadoPagoWebError) as info:
            web.read_account(tab)

        assert not isinstance(info.value, MercadoPagoSessionError)
        assert "balance page not fetched" in str(info.value)
        assert tab.calls == ["balance"]

    @pytest.mark.parametrize(
        ("pots", "reason"),
        [
            pytest.param({"status": "http", "code": 500}, "pots: HTTP 500", id="http-500"),
            pytest.param({"status": "timeout"}, "pots: timed out", id="timeout"),
            pytest.param(
                {"status": "ok", "html": "<html></html>"},
                "page data marker missing",
                id="no-marker",
            ),
            pytest.param(
                _ok(_pots_props([], status="error")),
                "pots: status is not success",
                id="widget-failed",
            ),
            pytest.param(_ok({"synthetic": 1}), "pots: hub missing", id="changed-page"),
        ],
    )
    def test_pots_failing_after_the_balance_is_a_warning(self, pots, reason):
        """The balance and the ledger are kept; the run will end partial."""
        tab = _tab(pots=pots)

        account = web.read_account(tab)

        assert account.balance == 2_500_000
        assert account.pots == []
        assert account.warnings == [f"MercadoPago: savings pots not read ({reason})"]
        assert len(account.movements) == 1

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(PlaywrightTimeoutError("synthetic"), id="timed-out"),
            pytest.param(PlaywrightError("Execution context was destroyed"), id="navigated"),
        ],
    )
    def test_a_pots_call_that_failed_on_a_live_tab_is_a_warning(self, error):
        """A timeout or a navigation leaves the tab usable: read on."""
        tab = _tab(pots=error)

        account = web.read_account(tab)

        assert account.warnings == [
            f"MercadoPago: savings pots not read (pots: in-page fetch failed ({type(error).__name__}))"
        ]
        assert len(account.movements) == 1

    @pytest.mark.parametrize(
        ("overrides", "calls"),
        [
            pytest.param({"balance": RuntimeError("synthetic")}, ["balance"], id="balance"),
            pytest.param({"pots": RuntimeError("synthetic")}, ["balance", "pots"], id="pots"),
            pytest.param(
                {("movements", 1): RuntimeError("synthetic")},
                ["balance", "pots", ("movements", 1)],
                id="movements",
            ),
            pytest.param(
                {"pots": _TargetClosedError("synthetic")}, ["balance", "pots"], id="target-closed"
            ),
        ],
    )
    def test_a_call_that_failed_otherwise_ends_the_read(self, overrides, calls):
        """A dead driver or a closed tab: no partial account, no call after it."""
        tab = _tab()
        tab.responses.update(overrides)

        with pytest.raises(MercadoPagoConnectionLostError) as info:
            web.read_account(tab)

        assert "lost its connection to the tab" in str(info.value)
        assert "make mercadopago-login" not in str(info.value)
        assert tab.calls == calls

    def test_a_failed_copy_out_ends_the_read_without_a_dispose(self):
        """`json_value` failing may mean a dead connection: no call follows it."""
        tab = _tab()
        handles: list = []
        fetch = tab.wait_for_function

        def failing_copy(script, arg=None, timeout=None):
            handle = fetch(script, arg=arg, timeout=timeout)
            if arg["surface"] == "pots":
                handle.json_value = _raise(RuntimeError("synthetic"))
            handles.append(handle)
            return handle

        tab.wait_for_function = failing_copy

        with pytest.raises(MercadoPagoConnectionLostError):
            web.read_account(tab)

        assert [handle.disposed for handle in handles] == [True, False]
        assert tab.calls == ["balance", "pots"]

    def test_a_skipped_pot_is_a_warning(self):
        """A pot left out is money left out, so the run says so."""
        tab = _tab(pots=_ok(_pots_props([_pot(), _pot(pot_id=POT_ID_2, currency="USD")])))

        account = web.read_account(tab)

        assert [pot.id for pot in account.pots] == [POT_ID]
        assert account.warnings == ["MercadoPago: 1 savings pot(s) not read"]

    @pytest.mark.parametrize("surface", ["pots", ("movements", 1)], ids=["pots", "ledger"])
    def test_a_redirect_after_the_balance_is_still_a_lost_session(self, surface):
        """A redirect anywhere ends the drive; it is never worked around."""
        tab = _tab()
        tab.responses[surface] = {"status": "redirect"}

        with pytest.raises(MercadoPagoSessionError):
            web.read_account(tab)

    def test_no_pots_is_not_a_warning(self):
        """An account without pots reads clean."""
        tab = _tab(pots=_ok(_pots_props([])))

        account = web.read_account(tab)

        assert (account.pots, account.warnings) == ([], [])

    @pytest.mark.parametrize(
        ("pots", "complete"),
        [
            pytest.param(_ok(_pots_props([_pot()])), True, id="success"),
            pytest.param(_ok(_pots_props([])), True, id="empty-success"),
            pytest.param({"status": "http", "code": 500}, False, id="page-error"),
            pytest.param(_ok(_pots_props([_pot()], status="error")), False, id="not-success"),
            pytest.param(
                _ok(_pots_props([_pot(), _pot(pot_id=POT_ID_2, currency="USD")])),
                False,
                id="skipped-entry",
            ),
        ],
    )
    def test_pots_complete_only_for_a_full_successful_read(self, pots, complete):
        """Only a pots list read whole can tell a closed pot from a failed read."""
        tab = _tab(pots=pots)

        account = web.read_account(tab)

        assert account.pots_complete is complete


class TestLedgerPaging:
    def _ledger(self, pages: int, **overrides) -> _FakeTab:
        """A ledger of `pages` pages, one distinct row on each."""
        responses = {
            "balance": _ok(_balance_props()),
            "pots": _ok(_pots_props([])),
        }
        for number in range(1, pages + 1):
            item = _item(movement_id=f"A00000000000000B{number}")
            responses[("movements", number)] = _ok(_movements_props([item], pages=pages))
        responses.update(overrides)
        return _FakeTab(responses)

    def _ledger_calls(self, tab: _FakeTab) -> list[int]:
        return [call[1] for call in tab.calls if isinstance(call, tuple)]

    def test_default_reads_only_the_newest_page(self, no_sleep):
        """One page of 100 rows per run; older pages are not a warning."""
        tab = self._ledger(3)

        account = web.read_account(tab)

        assert self._ledger_calls(tab) == [1]
        assert account.warnings == []

    def test_reads_as_many_pages_as_asked(self, no_sleep):
        """MERCADOPAGO_MOVEMENT_PAGES pages, newest first."""
        tab = self._ledger(3)

        account = web.read_account(tab, movement_pages=2)

        assert self._ledger_calls(tab) == [1, 2]
        assert len(account.movements) == 2

    def test_zero_reads_every_page_the_ledger_reports(self, no_sleep):
        """0 walks the whole ledger and stops at its last page."""
        tab = self._ledger(3)

        account = web.read_account(tab, movement_pages=0)

        assert self._ledger_calls(tab) == [1, 2, 3]
        assert [row.movement_id for row in account.movements] == [
            "A00000000000000B1",
            "A00000000000000B2",
            "A00000000000000B3",
        ]
        assert account.warnings == []

    def test_asking_for_more_pages_than_exist_stops_at_the_last(self, no_sleep):
        """The ledger's own page count ends the loop."""
        tab = self._ledger(2)

        web.read_account(tab, movement_pages=9)

        assert self._ledger_calls(tab) == [1, 2]

    def test_reading_every_page_is_capped(self, no_sleep, monkeypatch):
        """The pager is bounded; hitting the bound is reported."""
        monkeypatch.setattr(web, "MAX_MOVEMENT_PAGES", 2)
        tab = self._ledger(3)

        account = web.read_account(tab, movement_pages=0)

        assert self._ledger_calls(tab) == [1, 2]
        assert account.warnings == ["MercadoPago: movements capped at 2 of 3 pages"]

    def test_a_later_page_failing_keeps_the_earlier_pages(self, no_sleep):
        """What was read is kept, and the gap is a warning."""
        tab = self._ledger(3)
        tab.responses[("movements", 2)] = {"status": "http", "code": 500}

        account = web.read_account(tab, movement_pages=0)

        assert [row.movement_id for row in account.movements] == ["A00000000000000B1"]
        assert account.warnings == ["MercadoPago: movements page 2 not read (movements: HTTP 500)"]
        assert self._ledger_calls(tab) == [1, 2]

    def test_the_first_page_failing_is_a_warning_not_a_quiet_empty_ledger(self, no_sleep):
        """Zero rows from a failed page must not look like a quiet month."""
        tab = self._ledger(1)
        tab.responses[("movements", 1)] = {"status": "timeout"}

        account = web.read_account(tab)

        assert account.movements == []
        assert account.warnings == ["MercadoPago: movements page 1 not read (movements: timed out)"]

    def test_a_row_seen_on_two_pages_is_kept_once(self, no_sleep):
        """A movement arriving between two fetches shifts the pages by one row."""
        tab = self._ledger(2)
        repeated = [_item(movement_id="A00000000000000B1"), _item(movement_id="A00000000000000B2")]
        tab.responses[("movements", 2)] = _ok(_movements_props(repeated, pages=2))

        account = web.read_account(tab, movement_pages=0)

        assert [row.movement_id for row in account.movements] == [
            "A00000000000000B1",
            "A00000000000000B2",
        ]

    def test_rows_that_could_not_be_read_are_a_warning(self, no_sleep):
        """A movement left out is money left out: the run must not read clean."""
        tab = self._ledger(1)
        items = [
            _item(),
            _item(movement_id=MOVEMENT_ID_2, currency="USD"),
            _item(movement_id="synthetic id"),
        ]
        tab.responses[("movements", 1)] = _ok(_movements_props(items))

        account = web.read_account(tab)

        assert [row.movement_id for row in account.movements] == [MOVEMENT_ID]
        assert account.warnings == ["MercadoPago: 2 movement row(s) not read"]

    def test_unread_rows_are_counted_across_pages_in_one_warning(self, no_sleep):
        """One warning for the whole ledger, with the total."""
        tab = self._ledger(2)
        for number in (1, 2):
            items = [_item(movement_id=f"A00000000000000B{number}"), _item(movement_id=None)]
            tab.responses[("movements", number)] = _ok(_movements_props(items, pages=2))

        account = web.read_account(tab, movement_pages=0)

        assert len(account.movements) == 2
        assert account.warnings == ["MercadoPago: 2 movement row(s) not read"]

    def test_rows_that_round_to_zero_are_not_a_warning(self, no_sleep):
        """Under half a peso is no money: nothing was left out."""
        tab = self._ledger(1)
        items = [_item(), _item(movement_id=MOVEMENT_ID_2, fraction="0", cents="40")]
        tab.responses[("movements", 1)] = _ok(_movements_props(items))

        account = web.read_account(tab)

        assert account.warnings == []

    def test_a_spent_read_budget_stops_the_ledger(self, no_sleep, monkeypatch):
        """Once the drive's time budget is gone, no further page is fetched."""
        monkeypatch.setattr(web, "READ_BUDGET_S", -1)
        tab = self._ledger(3)

        account = web.read_account(tab, movement_pages=0)

        assert self._ledger_calls(tab) == []
        assert account.warnings == [
            "MercadoPago: movements stopped before page 1 (read budget spent)"
        ]


# --- Transport: Chrome plumbing (never launched) --------------------------------


LASTING_SSID = {"name": "ssid", "value": "synthetic", "expires": 1_900_000_000.0}
SESSION_SSID = {"name": "ssid", "value": "synthetic", "expires": -1}


class _Jar:
    """Chrome's cookie jar: each read returns the next scripted entry (last repeats).

    With no script it holds a persistent `ssid`. An entry that is an exception
    makes that read fail.
    """

    def __init__(self, *script):
        self.script = list(script) or [[LASTING_SSID]]
        self.reads: list[str] = []

    def cookies(self, url):
        entry = self.script[min(len(self.reads), len(self.script) - 1)]
        self.reads.append(url)
        if isinstance(entry, Exception):
            raise entry
        return entry


class _Tab:
    def __init__(self, url, jar=None):
        self.url = url
        self.context = jar if jar is not None else _Jar()


class _Browser:
    def __init__(self, *urls, jar=None):
        tabs = [_Tab(url, jar) for url in urls]
        self.contexts = [type("Context", (), {"pages": tabs})()]


class _Clock:
    """A monotonic clock that only moves when `sleep` is called."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(web.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(web.time, "sleep", fake.sleep)
    return fake


class TestPortalTab:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param("https://www.mercadopago.cl/banking/balance", True, id="balance-page"),
            pytest.param("https://www.mercadopago.cl/", True, id="portal-root"),
            pytest.param("http://www.mercadopago.cl/banking/balance", False, id="not-https"),
            pytest.param("https://mercadopago.cl/banking/balance", False, id="bare-domain"),
            pytest.param("https://www.mercadopago.cl.example.invalid/", False, id="lookalike-host"),
            pytest.param("https://signin.example.invalid/login", False, id="sign-in-site"),
            pytest.param("about:blank", False, id="blank"),
            pytest.param("", False, id="empty"),
        ],
    )
    def test_only_the_exact_https_host_is_the_portal(self, url, expected):
        """The host must match exactly; a lookalike is not the portal."""
        assert web.on_portal(url) is expected

    def test_one_connection_is_watched_until_the_tab_shows(self, clock):
        """Among several tabs, the one on the portal is returned at once."""
        browser = _Browser("about:blank", "https://www.mercadopago.cl/banking/balance")

        page = web._watch_for_portal_tab(browser, wait_s=3)

        assert page.url == "https://www.mercadopago.cl/banking/balance"
        assert clock.now == 0

    @pytest.mark.parametrize(
        "urls",
        [
            pytest.param([""], id="not-attached-yet"),
            pytest.param(["about:blank"], id="not-navigated-yet"),
            pytest.param([], id="no-tab"),
            pytest.param(["https://signin.example.invalid/login"], id="another-site"),
        ],
    )
    def test_a_connection_that_never_shows_the_tab_is_given_up_after_the_watch(self, clock, urls):
        """A stale connection holds the tab with an empty URL for as long as it lives."""
        assert web._watch_for_portal_tab(_Browser(*urls), wait_s=3) is None
        assert clock.now == 3


def _summary(portal=0, elsewhere=0, key="synthetic-key", types=None) -> TargetSummary:
    """What Chrome's own target list would report: counts only."""
    if types is None:
        types = {"page": portal + elsewhere} if portal + elsewhere else {"browser_ui": 1}
    return TargetSummary(
        types=types,
        matching_pages=portal,
        other_web_pages=elsewhere,
        matching_key=key if portal else "",
    )


PORTAL = _summary(portal=1)  # Chrome lists a tab on the portal
ELSEWHERE = _summary(elsewhere=1)  # a tab on another site, none on the portal
NO_TAB = _summary()  # no web tab at all
DOWN = None  # the debug port does not answer


class _Looks:
    """Stands in for Chrome's `/json/list`: each look returns the next entry.

    The last entry repeats. Nothing is requested from any port.
    """

    def __init__(self, monkeypatch, *script):
        self.script = list(script)
        self.count = 0
        monkeypatch.setattr(web.real_chrome, "summarize_targets", self)

    def __call__(self, cdp_url, matches, **kwargs):
        entry = self.script[min(self.count, len(self.script) - 1)]
        self.count += 1
        return entry


class _Connections:
    """Stands in for `_cdp_connection`: every call opens the next scripted browser.

    An entry that is an exception makes that connection fail (after using up
    its whole timeout on the fake clock, when `slow`); the last entry
    repeats. Counts what was opened and closed, and how many were open at once.
    """

    def __init__(self, monkeypatch, *script, clock=None, slow=False):
        self.script = list(script)
        self.clock = clock
        self.slow = slow
        self.attempts = 0
        self.opened = 0
        self.closed = 0
        self.open_now = 0
        self.max_open = 0
        self.waits: list[float] = []
        self.started_at: list[float] = []
        self.processes: list = []
        monkeypatch.setattr(web, "_cdp_connection", self)

    @contextlib.contextmanager
    def __call__(self, cdp_url, wait_s, process=None, kill_switch=None):
        entry = self.script[min(self.attempts, len(self.script) - 1)]
        self.attempts += 1
        self.waits.append(wait_s)
        self.processes.append(process)
        if self.clock is not None:
            self.started_at.append(self.clock.now)
        if isinstance(entry, Exception):
            if self.slow:
                self.clock.sleep(wait_s)
            raise entry
        self.opened += 1
        self.open_now += 1
        self.max_open = max(self.max_open, self.open_now)
        try:
            yield entry
        finally:
            self.closed += 1
            self.open_now -= 1


class _Exited:
    """A launched Chrome that has already ended."""

    def poll(self):
        return 0


def _chain(error):
    """Every exception reachable from `error` through its cause and context."""
    seen: list = []
    todo = [error]
    while todo:
        link = todo.pop()
        if link is None or any(link is other for other in seen):
            continue
        seen.append(link)
        todo += [link.__cause__, link.__context__]
    return seen


CDP_URL = "http://127.0.0.1:9223"
STALE = ""  # the URL Playwright shows for a tab it attached to while it loaded
SIGN_IN_URL = "https://signin.example.invalid/login?synthetic=1"
NO_ATTACH = MercadoPagoConnectError("Could not connect to the Chrome debug port (TimeoutError).")


def _find(action=lambda page: "read", tab_wait=90, connect_wait=30, process=None):
    return web._with_portal_tab(
        CDP_URL,
        action,
        connect_wait_s=connect_wait,
        tab_wait_s=tab_wait,
        start_hint="synthetic start hint",
        no_tab="synthetic no-tab hint",
        process=process,
    )


class TestTabDiscovery:
    """Chrome's target list says where the tab is; Playwright attaches last."""

    # --- a tab on the portal: attach, and reattach while the connection is stale

    def test_stale_connections_are_dropped_until_one_sees_the_tab(self, clock, monkeypatch):
        """Each attach is a fresh connection; the stale ones are all closed."""
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(
            monkeypatch, _Browser(STALE), _Browser(STALE), _Browser(STALE), _Browser(web.START_URL)
        )
        seen = {}

        def action(page):
            seen.update(url=page.url, open_now=connections.open_now, closed=connections.closed)
            return "read"

        result = _find(action)

        assert result == "read"
        assert seen == {"url": web.START_URL, "open_now": 1, "closed": 3}
        assert (connections.opened, connections.closed, connections.max_open) == (4, 4, 1)

    def test_the_action_gets_the_tab_of_the_connection_that_found_it(self, clock, monkeypatch):
        """The read happens on the live connection, not on a stale one."""
        fresh = _Browser(web.START_URL)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(STALE), fresh)

        page = _find(lambda page: page)

        assert page is fresh.contexts[0].pages[0]

    def test_the_pauses_between_attaches_grow(self, clock, monkeypatch):
        """A slow page load costs few drivers: each stale attach waits longer."""
        _Looks(monkeypatch, PORTAL)
        stale = [_Browser(STALE)] * 6
        connections = _Connections(monkeypatch, *stale, _Browser(web.START_URL), clock=clock)

        _find()

        assert connections.started_at == [0, 5, 12, 21, 32, 43, 54]

    def test_the_first_attach_gets_the_launch_budget_later_ones_a_short_one(
        self, clock, monkeypatch
    ):
        """The first attach after a launch is slow, so it gets one long attempt."""
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(
            monkeypatch, _Browser(STALE), _Browser(STALE), _Browser(web.START_URL)
        )

        _find()

        assert connections.waits == [web.CONNECT_WAIT_S, web.RECONNECT_WAIT_S, web.RECONNECT_WAIT_S]

    def test_a_failed_attach_is_survived(self, clock, monkeypatch):
        """Chrome is up; an attach that times out during the load is tried again."""
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(monkeypatch, NO_ATTACH, _Browser(web.START_URL))

        assert _find() == "read"
        assert connections.attempts == 2

    def test_a_page_playwright_never_attaches_to_is_a_web_error_at_the_deadline(
        self, clock, monkeypatch
    ):
        """Chrome has the page: not a lost session, and it says what happened."""
        _Looks(monkeypatch, _summary(portal=1, types={"page": 1, "service_worker": 2}))
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoWebError) as info:
            _find()

        message = str(info.value)
        assert not isinstance(info.value, MercadoPagoSessionError)
        assert (
            "Chrome has the Mercado Pago page open, but Playwright never attached to it" in message
        )
        assert f"{connections.attempts} connection(s)" in message
        assert "Chrome targets: page: 1, service_worker: 2" in message
        assert "make mercadopago-login" not in message
        assert "http" not in message and "127.0.0.1" not in message
        assert 90 <= clock.now <= 90 + web.ATTACH_OVERRUN_S
        assert connections.opened == connections.closed
        assert connections.max_open == 1

    def test_attaches_that_all_fail_end_as_never_attached_too(self, clock, monkeypatch):
        """The port answers throughout, so this is not 'Chrome did not start'."""
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, NO_ATTACH)

        with pytest.raises(MercadoPagoWebError, match="never attached to it") as info:
            _find()

        assert type(info.value) is MercadoPagoWebError

    def test_an_ended_chrome_is_not_attached_to_again(self, clock, monkeypatch):
        """The launched process is gone: its port will never answer again."""
        gone = MercadoPagoWebError("The launched Chrome is no longer running.")
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(monkeypatch, _Browser(STALE), gone)

        with pytest.raises(MercadoPagoWebError) as info:
            _find()

        assert info.value is gone
        assert connections.attempts == 2

    def test_an_error_from_the_read_is_not_retried(self, clock, monkeypatch):
        """Reattaching is for finding the tab; a failed read ends the drive."""
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(monkeypatch, _Browser(web.START_URL))

        def action(page):
            raise MercadoPagoSessionError("synthetic")

        with pytest.raises(MercadoPagoSessionError):
            _find(action)

        assert (connections.opened, connections.closed) == (1, 1)

    # --- a tab on another site: the lost session, with no Playwright at all -----

    def test_a_tab_on_another_site_is_a_lost_session_without_attaching(self, clock, monkeypatch):
        """Two looks in a row; automation never touches the sign-in page."""
        looks = _Looks(monkeypatch, ELSEWHERE)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoSessionError) as info:
            _find()

        assert "make mercadopago-login" in str(info.value)
        assert "signin.example" not in str(info.value)
        assert connections.attempts == 0
        assert looks.count == 2
        assert clock.now == web._LOOK_PAUSE_S

    def test_one_sighting_on_another_site_is_not_yet_a_lost_session(self, clock, monkeypatch):
        """A redirect on its way back to the portal is given a second look."""
        _Looks(monkeypatch, ELSEWHERE, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL))

        assert _find() == "read"

    def test_a_look_without_an_answer_does_not_reset_the_count(self, clock, monkeypatch):
        """A hiccup of the port between two sightings is not a third opinion."""
        _Looks(monkeypatch, ELSEWHERE, DOWN, ELSEWHERE)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoSessionError):
            _find()

        assert connections.attempts == 0

    def test_sightings_that_are_not_in_a_row_do_not_add_up(self, clock, monkeypatch):
        """A look with no web tab in between starts the count again."""
        looks = _Looks(monkeypatch, ELSEWHERE, NO_TAB, ELSEWHERE, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL))

        assert _find() == "read"
        assert looks.count == 4

    def test_another_site_on_the_last_look_is_a_lost_session(self, clock, monkeypatch):
        """When the wait ends on a bounced tab, that is what gets reported."""
        _Looks(monkeypatch, NO_TAB, NO_TAB, ELSEWHERE)
        _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoSessionError):
            _find(tab_wait=4)

        assert clock.now == 4

    # --- no tab, no Chrome ------------------------------------------------------

    def test_no_tab_at_all_is_a_web_error_with_the_callers_hint(self, clock, monkeypatch):
        """Nothing on the portal and nothing elsewhere: not a session problem."""
        _Looks(monkeypatch, NO_TAB)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoWebError) as info:
            _find()

        assert not isinstance(info.value, MercadoPagoSessionError)
        assert str(info.value) == "synthetic no-tab hint (Chrome targets: browser_ui: 1)."
        assert connections.attempts == 0
        assert clock.now == 90

    def test_a_port_that_never_answers_is_chrome_not_starting(self, clock, monkeypatch):
        """No attach is tried without a Chrome; the launch hint is raised."""
        _Looks(monkeypatch, DOWN)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoConnectError, match="synthetic start hint"):
            _find()

        assert connections.attempts == 0
        assert clock.now == web.CONNECT_WAIT_S

    def test_a_launch_that_ended_before_answering_fails_at_once(self, clock, monkeypatch):
        """Swallowed by a Chrome already open on the profile: no waiting."""
        _Looks(monkeypatch, DOWN)
        _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoWebError, match="no longer running") as info:
            _find(process=_Exited())

        assert not isinstance(info.value, MercadoPagoConnectError)
        assert clock.now == 0

    def test_a_target_list_that_stops_answering_is_chrome_gone(self, clock, monkeypatch):
        """Three looks in a row without an answer, after it had answered."""
        looks = _Looks(monkeypatch, NO_TAB, DOWN)

        with pytest.raises(MercadoPagoConnectError, match="stopped answering") as info:
            _find()

        assert "make mercadopago-login" not in str(info.value)
        assert looks.count == 1 + web._MAX_UNANSWERED_LOOKS
        assert clock.now < 90

    def test_a_chrome_that_ends_mid_wait_fails_at_the_next_look(self, clock, monkeypatch):
        """The launched process ended: one unanswered look is enough."""
        looks = _Looks(monkeypatch, NO_TAB, DOWN)

        with pytest.raises(MercadoPagoWebError, match="no longer running"):
            _find(process=_Exited())

        assert looks.count == 2

    # --- the time bounds, with connections that use up all they are given -------

    def test_slow_attaches_near_the_deadline_stay_inside_every_bound(self, clock, monkeypatch):
        """Worst case: the port answers at the last moment and every attach
        times out, the last one begun as the tab wait ends."""
        port_polls = int(web.CONNECT_WAIT_S / web._PORT_POLL_S)
        _Looks(monkeypatch, *([DOWN] * port_polls), PORTAL)
        connections = _Connections(monkeypatch, NO_ATTACH, clock=clock, slow=True)

        with pytest.raises(MercadoPagoWebError, match="never attached to it"):
            _find(tab_wait=web.MANAGED_TAB_WAIT_S, connect_wait=web.CONNECT_WAIT_S)

        attach_phase = clock.now
        port_answered_at = connections.started_at[0]
        assert port_answered_at == web.CONNECT_WAIT_S
        assert connections.waits[0] == web.CONNECT_WAIT_S
        assert max(connections.waits[1:]) <= web.RECONNECT_WAIT_S
        assert connections.started_at[-1] <= port_answered_at + web.MANAGED_TAB_WAIT_S
        assert attach_phase > port_answered_at + web.MANAGED_TAB_WAIT_S
        assert attach_phase <= (web.CONNECT_WAIT_S + web.MANAGED_TAB_WAIT_S + web.ATTACH_OVERRUN_S)
        assert attach_phase + web.READ_BUDGET_S < web.MANAGED_HARD_LIMIT_S < web.DRIVE_TIMEOUT_S


class _FakeDriver:
    """What `sync_playwright()` returns: logs its start, stop and connections.

    `proc` stands in for the driver subprocess, where Playwright keeps it.
    """

    def __init__(self, log, connect, proc=None):
        self.log = log
        self.connect = connect
        self.chromium = self
        if proc is not None:
            transport = types.SimpleNamespace(_proc=proc)
            self._connection = types.SimpleNamespace(_transport=transport)

    def __enter__(self):
        self.log.append("start")
        return self

    def __exit__(self, *exc_info):
        self.log.append("stop")
        return False

    def connect_over_cdp(self, cdp_url, timeout=None):
        self.log.append(("connect", timeout))
        return self.connect()


@pytest.fixture
def driver_log(monkeypatch):
    """Install a fake `playwright.sync_api`; no driver process is started."""
    log: list = []
    state = {"connect": lambda: "browser", "proc": None}
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _FakeDriver(log, lambda: state["connect"](), state["proc"])
    module.Error = PlaywrightError
    package = types.ModuleType("playwright")
    package.sync_api = module
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    log_and_state = (log, state)
    return log_and_state


class TestCdpConnection:
    """The real `_cdp_connection` and `_connect`, on a fake Playwright module."""

    def _refuse(self, state):
        def refuse():
            raise RuntimeError(
                "connect ECONNREFUSED ws://127.0.0.1:9223/devtools/browser/synthetic-id\\n"
                "Call log: - <ws connecting> synthetic-cookie"
            )

        state["connect"] = refuse

    def test_the_driver_is_started_connected_once_and_stopped(self, driver_log):
        """One attempt with the whole wait as its timeout; the driver always stops."""
        log, _ = driver_log

        with web._cdp_connection(CDP_URL, 7) as browser:
            assert browser == "browser"
            assert log == ["start", ("connect", 7000)]

        assert log == ["start", ("connect", 7000), "stop"]

    def test_the_driver_is_stopped_when_the_connection_fails(self, driver_log):
        """A failed attach leaks no driver, and says Chrome is up."""
        log, state = driver_log
        self._refuse(state)

        with (
            pytest.raises(MercadoPagoConnectError) as info,
            web._cdp_connection(CDP_URL, 30),
        ):
            raise AssertionError("the body must not run")

        assert log == ["start", ("connect", 30000), "stop"]
        assert "Chrome is up, but Playwright could not attach to it" in str(info.value)
        assert "Chrome did not start" not in str(info.value)

    def test_the_driver_is_stopped_when_the_body_raises(self, driver_log):
        """An error while reading still drops the connection."""
        log, _ = driver_log

        with pytest.raises(MercadoPagoSessionError), web._cdp_connection(CDP_URL, 7):
            raise MercadoPagoSessionError("synthetic")

        assert log[-1] == "stop"

    def test_a_failed_attach_carries_only_the_type_of_the_playwright_error(self, driver_log):
        """Its text holds the CDP URL, a browser id and a call log: none survive."""
        _, state = driver_log
        self._refuse(state)

        with pytest.raises(MercadoPagoConnectError) as info, web._cdp_connection(CDP_URL, 30):
            pass

        assert "RuntimeError" in str(info.value)
        for link in _chain(info.value):
            assert not isinstance(link, RuntimeError) or isinstance(link, MercadoPagoWebError)
            for leak in ("127.0.0.1", "synthetic-id", "synthetic-cookie", "ws://", "Call log"):
                assert leak not in str(link)

    def test_an_ended_chrome_is_final_and_still_stops_the_driver(self, driver_log):
        """The launched process is gone: not a connect error that waiting cures."""
        log, state = driver_log
        self._refuse(state)

        with (
            pytest.raises(MercadoPagoWebError, match="no longer running") as info,
            web._cdp_connection(CDP_URL, 30, _Exited()),
        ):
            pass

        assert not isinstance(info.value, MercadoPagoConnectError)
        assert log[-1] == "stop"
        assert [entry for entry in log if entry != "start" and entry != "stop"] == [
            ("connect", 30000)
        ]


class TestConfiguration:
    def test_default_port_leaves_9222_to_bci_lider(self, monkeypatch):
        """Two real-Chrome scrapers must not share a debug port."""
        monkeypatch.delenv("MERCADOPAGO_CDP_PORT", raising=False)

        assert web.cdp_port() == 9223

    def test_port_override(self, monkeypatch):
        """MERCADOPAGO_CDP_PORT moves the managed Chrome's port."""
        monkeypatch.setenv("MERCADOPAGO_CDP_PORT", "9333")

        assert web.cdp_port() == 9333

    def test_a_bad_port_is_a_clear_error(self, monkeypatch):
        """A typo fails the run with the variable's name, not a traceback."""
        monkeypatch.setenv("MERCADOPAGO_CDP_PORT", "synthetic")

        with pytest.raises(MercadoPagoWebError, match="MERCADOPAGO_CDP_PORT"):
            web.cdp_port()

    def test_default_profile_lives_next_to_the_scrapers(self, monkeypatch):
        """The default profile is the gitignored directory under apps/scrapers."""
        monkeypatch.delenv("MERCADOPAGO_CHROME_PROFILE", raising=False)

        profile = web.profile_dir()

        assert profile.name == ".mercadopago_chrome_profile"
        assert profile.parent.name == "scrapers"

    def test_profile_override(self, monkeypatch, tmp_path):
        """MERCADOPAGO_CHROME_PROFILE moves the profile."""
        monkeypatch.setenv("MERCADOPAGO_CHROME_PROFILE", str(tmp_path / "profile"))

        assert web.profile_dir() == tmp_path / "profile"


class _FakeProc:
    """What `launch_chrome` returns, as far as the managed flow uses it."""

    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode


class _Chrome:
    """Records what the real-Chrome helpers were asked to do; launches nothing."""

    def __init__(
        self, monkeypatch, tmp_path, port_in_use=False, profile_exists=True, fake_drive=True
    ):
        self.launches: list[dict] = []
        self.terminated: list = []
        self.closed_via: list = []
        self.drives: list[dict] = []
        self.on_disk = True
        self.disk_checks: list[float] = []
        self.proc = _FakeProc()
        self.outcome = MercadoPagoAccount(balance=2_500_000)
        profile = tmp_path / "profile"
        if profile_exists:
            profile.mkdir()
        monkeypatch.setenv("MERCADOPAGO_CHROME_PROFILE", str(profile))
        monkeypatch.delenv("MERCADOPAGO_CDP_URL", raising=False)
        monkeypatch.delenv("MERCADOPAGO_CDP_PORT", raising=False)
        monkeypatch.setattr(web, "_require_playwright", lambda: None)
        self.reclaims: list[dict] = []
        monkeypatch.setattr(
            web.real_chrome,
            "reclaim_orphaned_chrome",
            lambda **kwargs: self.reclaims.append(kwargs),
        )
        monkeypatch.setattr(web.real_chrome, "port_in_use", lambda port: port_in_use)
        monkeypatch.setattr(web.real_chrome, "find_chrome", lambda env: "/synthetic/chrome")
        monkeypatch.setattr(web.real_chrome, "launch_chrome", self._launch)
        monkeypatch.setattr(web.real_chrome, "terminate_chrome", self._terminate)
        monkeypatch.setattr(web, "_session_on_disk_after_close", self._on_disk)
        self.terminate_stops = True
        if fake_drive:
            monkeypatch.setattr(web, "_drive_over_cdp", self._drive)

    def _launch(self, executable, **kwargs):
        self.launches.append({"executable": executable, **kwargs})
        return self.proc

    def _on_disk(self, profile, since):
        self.disk_checks.append(since)
        return self.on_disk

    def _terminate(self, proc, cdp_url=None):
        self.terminated.append(proc)
        self.closed_via.append(cdp_url)
        if self.terminate_stops:
            proc.returncode = 0

    def _drive(self, cdp_url, movement_pages, **kwargs):
        self.drives.append({"cdp_url": cdp_url, "movement_pages": movement_pages, **kwargs})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.usefixtures("timers")
class TestModes:
    def test_managed_launches_offscreen_reads_and_terminates(self, monkeypatch, tmp_path):
        """Default mode: its own Chrome, on the profile, on the balance page."""
        chrome = _Chrome(monkeypatch, tmp_path)

        account = asyncio.run(web.scrape_account(movement_pages=2))

        (launch,) = chrome.launches
        assert account is chrome.outcome
        assert launch["offscreen"] is True
        assert launch["url"] == web.START_URL
        assert launch["port"] == 9223
        assert launch["profile_dir"] == tmp_path / "profile"
        assert chrome.drives[0]["cdp_url"] == "http://127.0.0.1:9223"
        assert chrome.drives[0]["movement_pages"] == 2
        assert chrome.terminated == [chrome.proc]

    def test_managed_never_retries_a_lost_session(self, monkeypatch, tmp_path):
        """One launch, one drive, Chrome terminated, the error raised as is."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.outcome = MercadoPagoSessionError("synthetic")

        with pytest.raises(MercadoPagoSessionError):
            asyncio.run(web.scrape_account())

        assert len(chrome.launches) == 1
        assert len(chrome.drives) == 1
        assert chrome.terminated == [chrome.proc]

    def test_managed_without_a_profile_asks_for_the_login_and_launches_nothing(
        self, monkeypatch, tmp_path
    ):
        """No profile yet means nobody signed in: no Chrome is started."""
        chrome = _Chrome(monkeypatch, tmp_path, profile_exists=False)

        with pytest.raises(MercadoPagoSessionError, match="make mercadopago-login"):
            asyncio.run(web.scrape_account())

        assert chrome.launches == []

    def test_managed_refuses_a_busy_port(self, monkeypatch, tmp_path):
        """A Chrome already on the port is never read and never closed."""
        chrome = _Chrome(monkeypatch, tmp_path, port_in_use=True)

        with pytest.raises(MercadoPagoWebError, match="MERCADOPAGO_CDP_URL"):
            asyncio.run(web.scrape_account())

        assert chrome.launches == []
        assert chrome.terminated == []

    def test_reuse_attaches_and_never_launches_or_terminates(self, monkeypatch, tmp_path):
        """MERCADOPAGO_CDP_URL: someone else's Chrome is left running."""
        chrome = _Chrome(monkeypatch, tmp_path)
        monkeypatch.setenv("MERCADOPAGO_CDP_URL", "http://localhost:9333")

        account = asyncio.run(web.scrape_account())

        assert account is chrome.outcome
        assert chrome.drives[0]["cdp_url"] == "http://localhost:9333"
        assert chrome.launches == []
        assert chrome.terminated == []

    def test_reuse_does_not_need_the_profile(self, monkeypatch, tmp_path):
        """The profile belongs to the managed Chrome only."""
        chrome = _Chrome(monkeypatch, tmp_path, profile_exists=False)

        asyncio.run(web.scrape_account(cdp_url="http://localhost:9333"))

        assert len(chrome.drives) == 1

    @pytest.mark.parametrize("cdp_url", [None, "http://localhost:9333"], ids=["managed", "reuse"])
    def test_a_drive_that_never_returns_does_not_hold_the_run(self, monkeypatch, tmp_path, cdp_url):
        """A call stuck on a frozen browser ends as an error, not as a hung service."""
        chrome = _Chrome(monkeypatch, tmp_path)
        release = threading.Event()

        def stuck(*args, **kwargs):
            release.wait(timeout=5)
            return chrome.outcome

        monkeypatch.setattr(web, "_drive_over_cdp", stuck)
        monkeypatch.setattr(web, "DRIVE_TIMEOUT_S", 0.05)

        async def run():
            try:
                await web.scrape_account(cdp_url=cdp_url)
            finally:
                release.set()

        with pytest.raises(MercadoPagoWebError, match="did not finish in time"):
            asyncio.run(run())

    @pytest.mark.parametrize("cdp_url", [None, "http://localhost:9333"], ids=["managed", "reuse"])
    def test_an_unwrapped_driver_error_carries_only_its_type(self, monkeypatch, tmp_path, cdp_url):
        """A driver or dropped-connection error keeps its CDP URL and call log out of the run."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.outcome = RuntimeError("ws://127.0.0.1:9223/devtools/browser/synthetic-id Call log")

        with pytest.raises(MercadoPagoWebError, match=r"read failed \(RuntimeError\)") as info:
            asyncio.run(web.scrape_account(cdp_url=cdp_url))

        for link in _chain(info.value):
            assert "ws://" not in str(link) and "Call log" not in str(link)


@pytest.mark.usefixtures("timers")
class TestRealDrives:
    """Both modes through the real discovery loop, on fake looks and connections."""

    def _browser_with(self, tab):
        browser = _Browser()
        browser.contexts[0].pages.append(tab)
        return browser

    def test_reuse_reads_once_the_tab_attaches_and_never_terminates_chrome(
        self, clock, monkeypatch, tmp_path
    ):
        """Someone else's Chrome: connections come and go, the browser stays."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        tab = _tab()
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(monkeypatch, _Browser(STALE), self._browser_with(tab))

        account = web._drive_reuse("http://localhost:9333", 1)

        assert account.balance == 2_500_000
        assert tab.calls == ["balance", "pots", ("movements", 1)]
        assert (connections.opened, connections.closed) == (2, 2)
        assert set(connections.processes) == {None}
        assert chrome.launches == []
        assert chrome.terminated == []

    def test_reuse_with_the_tab_closed_asks_to_open_it_not_to_sign_in(
        self, clock, monkeypatch, tmp_path
    ):
        """No tab anywhere is not a lost session: the hint is to open the page."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, NO_TAB)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoWebError) as info:
            web._drive_reuse("http://localhost:9333", 1)

        assert not isinstance(info.value, MercadoPagoSessionError)
        assert "open the balance page in that Chrome" in str(info.value)
        assert "make mercadopago-login" not in str(info.value)
        assert connections.attempts == 0
        assert clock.now == web.REUSE_TAB_WAIT_S
        assert chrome.terminated == []

    def test_reuse_with_a_tab_on_another_site_is_a_lost_session(self, clock, monkeypatch, tmp_path):
        """A tab that sits on another site is the one case that asks to sign in."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, ELSEWHERE)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoSessionError, match="sign in in that Chrome"):
            web._drive_reuse("http://localhost:9333", 1)

        assert connections.attempts == 0
        assert chrome.terminated == []

    def test_reuse_without_a_chrome_says_so_quickly(self, clock, monkeypatch, tmp_path):
        """A Chrome that is not running is not waited for like a launch."""
        _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, DOWN)

        with pytest.raises(MercadoPagoConnectError, match="Start it again"):
            web._drive_reuse("http://localhost:9333", 1)

        assert clock.now == web.REUSE_CONNECT_WAIT_S

    def test_managed_reads_once_the_tab_attaches_then_terminates(
        self, clock, monkeypatch, tmp_path
    ):
        """The live failure: stale connections first, the tab on a later one."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, DOWN, DOWN, NO_TAB, PORTAL)
        stale = [_Browser(STALE)] * 4
        connections = _Connections(monkeypatch, *stale, self._browser_with(_tab()))

        account = web._drive_managed(1)

        assert account.balance == 2_500_000
        assert account.warnings == []
        assert (connections.opened, connections.closed) == (5, 5)
        assert set(connections.processes) == {chrome.proc}
        assert chrome.terminated == [chrome.proc]

    def test_managed_lost_session_never_attaches_and_closes_chrome(
        self, clock, monkeypatch, tmp_path
    ):
        """The tab was bounced to sign in: a session error, with no Playwright."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, DOWN, ELSEWHERE)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoSessionError, match="make mercadopago-login"):
            web._drive_managed(1)

        assert connections.attempts == 0
        assert len(chrome.launches) == 1
        assert chrome.terminated == [chrome.proc]

    def test_managed_terminates_chrome_when_the_tab_never_attaches(
        self, clock, monkeypatch, tmp_path
    ):
        """One launch, the whole tab wait, a web error, and Chrome is closed."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoWebError, match="never attached"):
            web._drive_managed(1)

        assert len(chrome.launches) == 1
        assert chrome.terminated == [chrome.proc]
        assert clock.now >= web.MANAGED_TAB_WAIT_S

    def test_managed_without_any_tab_says_chrome_opened_none(self, clock, monkeypatch, tmp_path):
        """Chrome is up but never opened the page: a machine problem."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, NO_TAB)

        with pytest.raises(MercadoPagoWebError, match="Chrome opened no Mercado Pago tab") as info:
            web._drive_managed(1)

        assert "make mercadopago-login" not in str(info.value)
        assert chrome.terminated == [chrome.proc]

    def test_managed_whose_chrome_never_answers_names_the_launch(
        self, clock, monkeypatch, tmp_path
    ):
        """No debug port in time: Chrome did not start."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        _Looks(monkeypatch, DOWN)

        with pytest.raises(MercadoPagoConnectError, match="Chrome did not start"):
            web._drive_managed(1)

        assert clock.now == web.CONNECT_WAIT_S
        assert chrome.terminated == [chrome.proc]

    def test_the_login_closes_through_cdp_then_checks_the_disk(self, monkeypatch, tmp_path):
        """Wait, CDP close, then the disk once more, all keyed to the login's start."""
        chrome = _Chrome(monkeypatch, tmp_path)
        events = []
        waits = []

        def wait(url, timeout, process=None, since=None):
            events.append("wait")
            waits.append(since)

        monkeypatch.setattr(web, "_await_sign_in", wait)
        monkeypatch.setattr(web.time, "time", lambda: 1_700_000_000.0)
        recorded_terminate = chrome._terminate
        monkeypatch.setattr(
            web.real_chrome,
            "terminate_chrome",
            lambda proc, cdp_url=None: events.append("close") or recorded_terminate(proc, cdp_url),
        )
        recorded_disk = chrome._on_disk
        monkeypatch.setattr(
            web,
            "_session_on_disk_after_close",
            lambda profile, since: events.append("disk") or recorded_disk(profile, since),
        )

        web.save_login_session(timeout_s=60)

        assert events == ["wait", "close", "disk"]
        assert chrome.closed_via == ["http://127.0.0.1:9223"]
        assert waits == chrome.disk_checks == [1_700_000_000.0]

    def test_a_session_missing_on_disk_after_the_close_is_no_success(self, monkeypatch, tmp_path):
        """The post-close check fails the login instead of reporting it signed in."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.on_disk = False
        monkeypatch.setattr(
            web, "_await_sign_in", lambda url, timeout, process=None, since=None: None
        )

        with pytest.raises(MercadoPagoSessionError, match="not in the profile on disk"):
            web.save_login_session(timeout_s=60)

        assert chrome.terminated == [chrome.proc]

    def test_a_failed_sign_in_skips_the_disk_check(self, monkeypatch, tmp_path):
        """The wait's own error is what the human sees."""
        chrome = _Chrome(monkeypatch, tmp_path)

        def never(url, timeout, process=None, since=None):
            raise MercadoPagoSessionError("Sign-in not detected in time.")

        monkeypatch.setattr(web, "_await_sign_in", never)

        with pytest.raises(MercadoPagoSessionError, match="not detected"):
            web.save_login_session(timeout_s=60)

        assert chrome.disk_checks == []


LOGIN_START = 1_700_000_000.0  # synthetic Unix time of the login's start


def _chrome_time(unix_s: float) -> int:
    """Unix seconds as Chrome's cookie time (microseconds since 1601-01-01 UTC)."""
    return int((unix_s + 11_644_473_600) * 1_000_000)


FRESH = _chrome_time(LOGIN_START + 5)
CREATED_BEFORE_LOGIN = _chrome_time(LOGIN_START - 3600)
LATER = _chrome_time(LOGIN_START + 400 * 86_400)
PAST = _chrome_time(LOGIN_START - 86_400)


class TestSessionOnDisk:
    """The profile's cookie store, built in tmp_path with synthetic rows only."""

    @pytest.fixture(autouse=True)
    def _now(self, monkeypatch):
        monkeypatch.setattr(web.time, "time", lambda: LOGIN_START + 60)

    def _store(self, profile, rows, relative="Default/Network/Cookies"):
        db = profile / relative
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE cookies (creation_utc INTEGER, host_key TEXT, name TEXT, "
            "value TEXT, encrypted_value BLOB, expires_utc INTEGER, is_persistent INTEGER)"
        )
        conn.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, 'synthetic', x'00', ?, ?)",
            [
                (
                    row[3] if len(row) > 3 else FRESH,
                    row[0],
                    row[1],
                    row[4] if len(row) > 4 else LATER,
                    row[2],
                )
                for row in rows
            ],
        )
        conn.commit()
        conn.close()
        return db

    def test_chrome_time_converts_from_1601_microseconds(self):
        """Chrome's epoch is 1601-01-01 UTC; 0 Unix seconds is 11644473600 s after it."""
        from datetime import datetime

        unix_epoch = datetime(1970, 1, 1, tzinfo=UTC)
        y2k = datetime(2000, 1, 1, tzinfo=UTC)
        chrome_epoch = datetime(1601, 1, 1, tzinfo=UTC)

        assert web.chrome_time_to_unix(11_644_473_600_000_000) == 0
        assert (
            web.chrome_time_to_unix(int((y2k - chrome_epoch).total_seconds() * 1_000_000))
            == (y2k - unix_epoch).total_seconds()
        )

    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            pytest.param([(".mercadopago.cl", "ssid", 1)], True, id="persistent-portal"),
            pytest.param([("www.mercadopago.cl", "ssid", 1)], True, id="persistent-www"),
            pytest.param([(".mercadopago.cl", "ssid", 0)], False, id="session-row"),
            pytest.param([(".mercadolibre.cl", "ssid", 1)], False, id="other-domain"),
            pytest.param([(".mercadopago.cl", "orguserid", 1)], False, id="other-cookie"),
            pytest.param(
                [(".mercadopago.cl", "ssid", 1, CREATED_BEFORE_LOGIN)], False, id="before-login"
            ),
            pytest.param([(".mercadopago.cl", "ssid", 1, FRESH, PAST)], False, id="expired"),
            pytest.param(
                [(".mercadopago.cl", "ssid", 1, _chrome_time(LOGIN_START))], True, id="at-start"
            ),
            pytest.param(
                [
                    (".mercadopago.cl", "ssid", 1, CREATED_BEFORE_LOGIN),
                    (".mercadopago.cl", "ssid", 1),
                ],
                True,
                id="stale-and-fresh",
            ),
            pytest.param([], False, id="empty"),
        ],
    )
    def test_only_a_fresh_persistent_portal_ssid_counts(self, tmp_path, rows, expected):
        """A leftover from an earlier sign-in, an expired row or a session row does not."""
        self._store(tmp_path, rows)

        assert web._session_on_disk(tmp_path, LOGIN_START) is expected

    def _store_with_update_time(self, profile, created, updated):
        """A store in the newer schema, which has `last_update_utc`; one portal `ssid` row."""
        db = profile / "Default" / "Network" / "Cookies"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE cookies (creation_utc INTEGER, host_key TEXT, name TEXT, "
            "value TEXT, encrypted_value BLOB, expires_utc INTEGER, is_persistent INTEGER, "
            "last_update_utc INTEGER)"
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, '.mercadopago.cl', 'ssid', 'synthetic', x'00', ?, 1, ?)",
            (created, LATER, updated),
        )
        conn.commit()
        conn.close()

    @pytest.mark.parametrize(
        ("created", "updated", "expected"),
        [
            pytest.param(CREATED_BEFORE_LOGIN, FRESH, True, id="same-value-reset"),
            pytest.param(FRESH, FRESH, True, id="new-cookie"),
            pytest.param(CREATED_BEFORE_LOGIN, CREATED_BEFORE_LOGIN, False, id="untouched"),
            pytest.param(FRESH, 0, True, id="no-update-time"),
        ],
    )
    def test_a_cookie_updated_since_the_login_counts(self, tmp_path, created, updated, expected):
        """Chrome keeps the old creation time when the same value is set again."""
        self._store_with_update_time(tmp_path, created, updated)

        assert web._session_on_disk(tmp_path, LOGIN_START) is expected

    @pytest.mark.parametrize("relative", ["Default/Network/Cookies", "Default/Cookies"])
    def test_both_cookie_store_locations_are_read(self, tmp_path, relative):
        """Newer Chrome keeps the store under `Network/`; older ones do not."""
        self._store(tmp_path, [(".mercadopago.cl", "ssid", 1)], relative)

        assert web._session_on_disk(tmp_path, LOGIN_START) is True

    @pytest.mark.parametrize(
        "content",
        [pytest.param(b"not a database", id="corrupt"), pytest.param(None, id="missing")],
    )
    def test_an_unreadable_store_is_not_yet(self, tmp_path, content):
        """A sqlite error or a missing file means "keep waiting", never a crash."""
        if content is not None:
            db = tmp_path / "Default" / "Network" / "Cookies"
            db.parent.mkdir(parents=True)
            db.write_bytes(content)

        assert web._session_on_disk(tmp_path, LOGIN_START) is False

    def _hot_journal(self, db):
        """Leave `db` mid-write with a non-empty journal; returns the open writer."""
        writer = sqlite3.connect(db, isolation_level=None)
        writer.execute("PRAGMA cache_size=1")
        writer.execute("BEGIN")
        for _ in range(500):
            writer.execute(
                "INSERT INTO cookies VALUES (?, 'x.example.invalid', 'filler', 'synthetic', "
                "x'00', ?, 0)",
                (FRESH, LATER),
            )
        assert (db.parent / "Cookies-journal").stat().st_size > 0
        return writer

    def test_a_copied_hot_journal_is_not_yet(self, tmp_path):
        """A write in flight cannot be rolled back on a read-only copy: keep waiting."""
        db = self._store(tmp_path, [(".mercadopago.cl", "ssid", 1)])
        writer = self._hot_journal(db)
        try:
            assert web._session_on_disk(tmp_path, LOGIN_START) is False
        finally:
            writer.execute("ROLLBACK")
            writer.close()

    def test_a_store_that_changes_while_copied_is_not_yet(self, tmp_path, monkeypatch):
        """The copy is not atomic: a write between the stamps means "not yet"."""
        db = self._store(tmp_path, [(".mercadopago.cl", "ssid", 1)])
        real_copy = web.shutil.copyfile

        def copy_then_write(src, dst):
            result = real_copy(src, dst)
            if Path(src) == db:
                with open(db, "ab") as handle:
                    handle.write(b"\0" * 4096)
            return result

        monkeypatch.setattr(web.shutil, "copyfile", copy_then_write)

        assert web._session_on_disk(tmp_path, LOGIN_START) is False

    def test_a_copy_is_read_and_the_live_file_is_untouched(self, tmp_path, monkeypatch):
        """Chrome's own file is never opened; the copy is gone afterwards."""
        db = self._store(tmp_path, [(".mercadopago.cl", "ssid", 1)])
        (db.parent / "Cookies-journal").write_bytes(b"")
        opened = []
        real_connect = sqlite3.connect
        monkeypatch.setattr(
            web.sqlite3,
            "connect",
            lambda target, **kwargs: opened.append(target) or real_connect(target, **kwargs),
        )

        assert web._session_on_disk(tmp_path, LOGIN_START) is True

        (target,) = opened
        assert target.endswith("?mode=ro")
        assert db.as_uri() not in target
        assert not Path(target.split("?")[0].removeprefix("file://")).exists()

    def test_no_value_column_is_ever_selected(self, tmp_path, monkeypatch):
        """Only host, name, persistence, creation and expiry are read."""
        self._store(tmp_path, [(".mercadopago.cl", "ssid", 1)])
        statements = []
        real_connect = sqlite3.connect

        def traced(target, **kwargs):
            conn = real_connect(target, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(web.sqlite3, "connect", traced)

        web._session_on_disk(tmp_path, LOGIN_START)

        assert statements
        assert not any("value" in statement for statement in statements)

    def test_the_post_close_check_retries_while_chrome_still_holds_the_store(
        self, tmp_path, monkeypatch
    ):
        """A hot journal right after the exit: retried, then read once Chrome lets go."""
        db = self._store(tmp_path, [(".mercadopago.cl", "ssid", 1)])
        writer = self._hot_journal(db)
        fake = _Clock()
        monkeypatch.setattr(web.time, "monotonic", fake.monotonic)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            fake.sleep(seconds)
            if len(sleeps) == 2:
                writer.execute("ROLLBACK")
                writer.close()

        monkeypatch.setattr(web.time, "sleep", sleep)

        assert web._session_on_disk_after_close(tmp_path, LOGIN_START) is True
        assert sleeps == [web._POST_CLOSE_DISK_POLL_S] * 2

    def test_the_post_close_check_gives_up_after_its_bound(self, tmp_path, monkeypatch):
        """Nothing on disk: False after about 10 s of retries, never longer."""
        self._store(tmp_path, [(".mercadopago.cl", "ssid", 0)])
        fake = _Clock()
        monkeypatch.setattr(web.time, "monotonic", fake.monotonic)
        monkeypatch.setattr(web.time, "sleep", fake.sleep)

        assert web._session_on_disk_after_close(tmp_path, LOGIN_START) is False
        assert fake.now == web._POST_CLOSE_DISK_WAIT_S


class _FakeTimer:
    """Stands in for `threading.Timer`: records itself and fires only on demand."""

    created: list = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.started = False
        self.cancelled = False
        self.daemon = False
        self.finished = threading.Event()
        _FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args)
        self.finished.set()


@pytest.fixture
def timers(monkeypatch):
    _FakeTimer.created = []
    monkeypatch.setattr(web.threading, "Timer", _FakeTimer)
    return _FakeTimer.created


class TestLaunchSafety:
    def test_the_watchdog_is_armed_for_the_hard_limit_and_disarmed_after(
        self, monkeypatch, tmp_path, timers
    ):
        """A drive that finishes cancels the watchdog it started."""
        _Chrome(monkeypatch, tmp_path)

        web._drive_managed(1)

        (watchdog,) = timers
        assert watchdog.interval == web.MANAGED_HARD_LIMIT_S
        assert (watchdog.started, watchdog.daemon, watchdog.cancelled) == (True, True, True)

    def test_a_firing_watchdog_terminates_the_chrome_the_drive_waits_on(
        self, monkeypatch, tmp_path, timers
    ):
        """The overrun path: the watchdog ends Chrome, which ends the stuck call."""
        chrome = _Chrome(monkeypatch, tmp_path)

        def overrun(cdp_url, movement_pages, **kwargs):
            assert chrome.terminated == []
            timers[0].fire()
            assert chrome.terminated == [chrome.proc]
            raise MercadoPagoWebError("synthetic: the browser went away")

        monkeypatch.setattr(web, "_drive_over_cdp", overrun)

        with pytest.raises(MercadoPagoWebError, match="went away"):
            web._drive_managed(1)

        assert chrome.terminated == [chrome.proc, chrome.proc]
        assert timers[0].cancelled is True

    def test_a_connection_cut_by_the_watchdog_reads_as_the_timeout(
        self, monkeypatch, tmp_path, timers
    ):
        """Terminating Chrome is what lost the connection: say it overran."""
        _Chrome(monkeypatch, tmp_path)

        def overrun(cdp_url, movement_pages, **kwargs):
            timers[0].fire()
            raise MercadoPagoConnectionLostError("synthetic")

        monkeypatch.setattr(web, "_drive_over_cdp", overrun)

        with pytest.raises(MercadoPagoWebError, match="did not finish in time"):
            web._drive_managed(1)

    def test_a_connection_lost_before_the_watchdog_fired_keeps_its_reason(
        self, monkeypatch, tmp_path, timers
    ):
        """Only the watchdog's own cut is reported as a timeout."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.outcome = MercadoPagoConnectionLostError("synthetic: lost")

        with pytest.raises(MercadoPagoConnectionLostError, match="synthetic: lost"):
            web._drive_managed(1)

    def test_chrome_is_closed_when_the_watchdog_cannot_start(self, monkeypatch, tmp_path):
        """Nothing between the launch and the cleanup may leak the Chrome."""
        chrome = _Chrome(monkeypatch, tmp_path)

        def no_threads(*args, **kwargs):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(web.threading, "Timer", no_threads)

        with pytest.raises(RuntimeError):
            web._drive_managed(1)

        assert len(chrome.launches) == 1
        assert chrome.drives == []
        assert chrome.terminated == [chrome.proc]

    def test_the_sign_in_window_is_closed_when_the_prompt_cannot_print(self, monkeypatch, tmp_path):
        """A closed stdout must not leave the visible Chrome behind."""
        chrome = _Chrome(monkeypatch, tmp_path)

        def broken_pipe(*args, **kwargs):
            raise BrokenPipeError("synthetic")

        monkeypatch.setattr("builtins.print", broken_pipe)

        with pytest.raises(BrokenPipeError):
            web.save_login_session(timeout_s=60)

        assert chrome.terminated == [chrome.proc]

    def test_the_sign_in_wait_gets_the_launched_process(self, monkeypatch, tmp_path):
        """So a closed window ends the wait instead of running out the clock."""
        chrome = _Chrome(monkeypatch, tmp_path)
        seen = {}
        monkeypatch.setattr(
            web,
            "_await_sign_in",
            lambda url, timeout, process=None, since=None: seen.update(process=process),
        )

        web.save_login_session(timeout_s=60)

        assert seen == {"process": chrome.proc}

    def test_the_managed_drive_gets_the_launched_process(self, monkeypatch, tmp_path, timers):
        """So a launch swallowed by another Chrome on the profile fails fast."""
        chrome = _Chrome(monkeypatch, tmp_path)

        web._drive_managed(1)

        assert chrome.drives[0]["process"] is chrome.proc

    def test_the_drive_timeout_leaves_room_for_a_whole_teardown(self):
        """After the watchdog fires, the drive timeout must outlast `terminate_chrome`."""
        assert web.DRIVE_TIMEOUT_S - web.MANAGED_HARD_LIMIT_S > real_chrome.TERMINATE_WORST_CASE_S

    def test_the_managed_run_closes_chrome_through_cdp(self, monkeypatch, tmp_path, timers):
        """So cookies the portal rotated during the read reach the profile."""
        chrome = _Chrome(monkeypatch, tmp_path)

        web._drive_managed(1)

        assert chrome.closed_via == ["http://127.0.0.1:9223"]

    def test_an_orphan_is_reclaimed_before_the_busy_port_check(self, monkeypatch, tmp_path):
        """A killed run's Chrome is stopped first; the port check still guards the rest."""
        _Chrome(monkeypatch, tmp_path, port_in_use=True)
        events = []
        monkeypatch.setattr(
            web.real_chrome,
            "reclaim_orphaned_chrome",
            lambda **kwargs: events.append(("reclaim", kwargs)),
        )
        monkeypatch.setattr(
            web.real_chrome, "port_in_use", lambda port: events.append(("port", port)) or True
        )

        with pytest.raises(MercadoPagoWebError, match="already in use"):
            web._drive_managed(1)

        assert events == [
            ("reclaim", {"port": 9223, "profile_dir": tmp_path / "profile"}),
            ("port", 9223),
        ]

    @pytest.mark.parametrize("fails", [False, True], ids=["read", "drive-fails"])
    def test_the_managed_run_records_its_chrome_and_clears_it(
        self, monkeypatch, tmp_path, timers, fails
    ):
        """The pidfile names the launch while it runs and is gone after the finally."""
        chrome = _Chrome(monkeypatch, tmp_path)
        pidfile = real_chrome.managed_pidfile(tmp_path / "profile")
        seen = []

        def drive(cdp_url, movement_pages, **kwargs):
            seen.append(json.loads(pidfile.read_text()))
            if fails:
                raise MercadoPagoWebError("synthetic")
            return chrome.outcome

        monkeypatch.setattr(web, "_drive_over_cdp", drive)
        outcome = pytest.raises(MercadoPagoWebError) if fails else contextlib.nullcontext()

        with outcome:
            web._drive_managed(1)

        assert [(record["pid"], record["port"]) for record in seen] == [(4242, 9223)]
        assert not pidfile.exists()

    def test_a_chrome_that_survives_teardown_keeps_its_record(self, monkeypatch, tmp_path, timers):
        """Only a stopped Chrome is forgotten, so the next run can still reclaim it."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.terminate_stops = False

        web._drive_managed(1)

        record = json.loads(real_chrome.managed_pidfile(tmp_path / "profile").read_text())
        assert record["pid"] == 4242

    def test_the_sign_in_window_records_its_chrome_and_clears_it(self, monkeypatch, tmp_path):
        """The login Chrome is reclaimable too, and cleared once closed."""
        _Chrome(monkeypatch, tmp_path, profile_exists=False)
        pidfile = real_chrome.managed_pidfile(tmp_path / "profile")
        seen = []
        monkeypatch.setattr(
            web,
            "_await_sign_in",
            lambda url, timeout, process=None, since=None: seen.append(
                json.loads(pidfile.read_text())["pid"]
            ),
        )

        web.save_login_session(timeout_s=60)

        assert seen == [4242]
        assert not pidfile.exists()

    def test_a_binary_that_cannot_start_names_the_setting_not_the_path(self, monkeypatch, tmp_path):
        """The override's path must not travel into the run's error message."""
        terminated = []
        monkeypatch.setenv("MERCADOPAGO_CHROME_PROFILE", str(tmp_path))
        monkeypatch.setenv("MERCADOPAGO_CHROME_PATH", "/synthetic/secret/chrome")
        monkeypatch.delenv("MERCADOPAGO_CDP_PORT", raising=False)
        monkeypatch.setattr(web, "_require_playwright", lambda: None)
        monkeypatch.setattr(real_chrome, "port_in_use", lambda port: False)
        monkeypatch.setattr(real_chrome, "terminate_chrome", terminated.append)

        def missing(args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", args[0])

        monkeypatch.setattr(real_chrome.subprocess, "Popen", missing)

        with pytest.raises(MercadoPagoWebError) as info:
            web._drive_managed(1)

        error = info.value
        assert "MERCADOPAGO_CHROME_PATH" in str(error)
        while error is not None:
            assert "/synthetic" not in str(error)
            error = error.__cause__ or error.__context__
        assert terminated == []


class _DriverProc:
    """The driver subprocess, and how Playwright before 1.64 behaved once killed.

    `kill` ends the call blocked on it. The first call after the kill fails;
    any later one would spin forever on those versions, so here it is
    recorded in `late_calls` (and fails, so a test cannot hang on it).
    """

    def __init__(self):
        self.killed = threading.Event()
        self.failed_once = False
        self.late_calls: list[str] = []

    def kill(self):
        self.killed.set()

    def call(self, name):
        if not self.killed.is_set():
            return
        if self.failed_once:
            self.late_calls.append(name)
            raise RuntimeError("synthetic: a dead dispatcher would spin here")
        self.failed_once = True
        raise RuntimeError("Connection closed while reading from the driver")


class _DriverHandle:
    """A fetch result handle on the fake driver; `on_json` runs inside `json_value`."""

    def __init__(self, proc, value, on_json=None):
        self.proc = proc
        self.value = value
        self.on_json = on_json

    def json_value(self):
        self.proc.call("json_value")
        if self.on_json is not None:
            self.on_json()
        return self.value

    def dispose(self):
        self.proc.call("dispose")


class _DriverTab:
    """A portal tab on the fake driver, answering each surface like `_tab()`.

    `hooks` maps a surface (or a ("movements", N) pair) to a callable run
    while that fetch copies its result out of the page.
    """

    url = web.START_URL

    def __init__(self, proc, log, hooks):
        self.proc = proc
        self.log = log
        self.hooks = hooks
        self.responses = _tab().responses
        self.calls: list = []

    def wait_for_function(self, script, arg=None, timeout=None):
        self.proc.call("wait_for_function")
        surface, number = arg["surface"], arg["pageNumber"]
        key = (surface, number) if surface == "movements" else surface
        self.calls.append(key)
        value = self.responses.get(key, self.responses.get(surface))
        return _DriverHandle(self.proc, value, self.hooks.get(key))

    def close(self):
        self.log.append("page.close")


class TestReuseWatchdog:
    """Reuse mode, through the real `_cdp_connection` on a fake Playwright."""

    def _install(self, monkeypatch, driver_log, hooks):
        log, state = driver_log
        proc = _DriverProc()
        tab = _DriverTab(proc, log, hooks)
        browser = _Browser()
        browser.contexts[0].pages.append(tab)
        browser.close = lambda: log.append("browser.close")
        state["proc"] = proc
        state["connect"] = lambda: browser
        _Looks(monkeypatch, PORTAL)
        return log, proc, tab

    def _run_reuse(self):
        with pytest.raises(MercadoPagoWebError) as info:
            asyncio.run(web.scrape_account(cdp_url="http://localhost:9333"))
        return info.value

    def _frozen_until_killed(self, timers, proc, worker):
        """A copy out of the page that never returns until the watchdog fires."""

        def freeze():
            worker.append(threading.current_thread())
            firing = threading.Thread(target=timers[0].fire, daemon=True)
            firing.start()
            assert proc.killed.wait(timeout=5), "the driver was never killed"
            firing.join(timeout=5)
            proc.call("json_value")  # the pending call fails with the driver

        return freeze

    def test_the_watchdog_is_armed_for_the_reuse_limit_and_disarmed_after(
        self, monkeypatch, tmp_path, timers
    ):
        """It lands before the run's own timeout; the drive gets its kill switch."""
        chrome = _Chrome(monkeypatch, tmp_path)

        web._drive_reuse("http://localhost:9333", 1)

        (watchdog,) = timers
        assert watchdog.interval == web.REUSE_HARD_LIMIT_S < web.DRIVE_TIMEOUT_S
        assert (watchdog.started, watchdog.daemon, watchdog.cancelled) == (True, True, True)
        assert isinstance(chrome.drives[0]["kill_switch"], real_chrome.DriverKillSwitch)

    def test_managed_mode_keeps_its_chrome_watchdog_and_no_kill_switch(
        self, monkeypatch, tmp_path, timers
    ):
        chrome = _Chrome(monkeypatch, tmp_path)

        web._drive_managed(1)

        (watchdog,) = timers
        assert watchdog.args == (chrome.proc,)
        assert "kill_switch" not in chrome.drives[0]

    def test_a_frozen_balance_read_ends_without_another_playwright_call(
        self, clock, monkeypatch, tmp_path, timers, driver_log
    ):
        """The worker thread ends, nothing is disposed, Chrome is left alone."""
        chrome = _Chrome(monkeypatch, tmp_path, fake_drive=False)
        worker: list = []
        hooks: dict = {}
        log, proc, tab = self._install(monkeypatch, driver_log, hooks)
        hooks["balance"] = self._frozen_until_killed(timers, proc, worker)

        error = self._run_reuse()

        assert str(error) == "Mercado Pago read did not finish in time."
        assert proc.late_calls == []
        assert tab.calls == ["balance"]
        worker[0].join(timeout=5)
        assert not worker[0].is_alive()
        assert log == ["start", ("connect", web.REUSE_TAB_WAIT_S * 1000), "stop"]
        assert chrome.terminated == []
        for link in _chain(error):
            assert "Connection closed" not in str(link)

    def test_a_frozen_pots_read_is_not_a_warning_to_carry_on_from(
        self, clock, monkeypatch, tmp_path, timers, driver_log
    ):
        """No partial account: the ledger is not fetched after the kill."""
        _Chrome(monkeypatch, tmp_path, fake_drive=False)
        worker: list = []
        hooks: dict = {}
        _, proc, tab = self._install(monkeypatch, driver_log, hooks)
        hooks["pots"] = self._frozen_until_killed(timers, proc, worker)

        error = self._run_reuse()

        assert "did not finish in time" in str(error)
        assert proc.late_calls == []
        assert tab.calls == ["balance", "pots"]

    def test_a_kill_between_fetches_stops_before_the_next_call(
        self, clock, monkeypatch, tmp_path, timers, driver_log
    ):
        """Fired while no call was pending: the result is not disposed, nothing follows."""
        _Chrome(monkeypatch, tmp_path, fake_drive=False)
        hooks: dict = {}
        _, proc, tab = self._install(monkeypatch, driver_log, hooks)
        hooks["balance"] = lambda: timers[0].fire()

        error = self._run_reuse()

        assert "did not finish in time" in str(error)
        assert proc.killed.is_set()
        assert (proc.failed_once, proc.late_calls) == (False, [])
        assert tab.calls == ["balance"]

    def test_no_attach_starts_once_the_watchdog_has_fired(
        self, clock, monkeypatch, tmp_path, timers, driver_log
    ):
        """A watchdog that fires between attaches ends the drive before another driver."""
        _Chrome(monkeypatch, tmp_path, fake_drive=False)
        log, state = driver_log
        state["proc"] = _DriverProc()
        state["connect"] = lambda: _Browser(STALE)
        looks = _Looks(monkeypatch, PORTAL)

        def look(cdp_url, matches, **kwargs):
            if looks.count == 1:
                timers[0].fire()
            return looks(cdp_url, matches)

        monkeypatch.setattr(web.real_chrome, "summarize_targets", look)

        with pytest.raises(MercadoPagoWebError, match="did not finish in time"):
            web._drive_reuse("http://localhost:9333", 1)

        assert log == ["start", ("connect", web.REUSE_TAB_WAIT_S * 1000), "stop"]
        assert state["proc"].killed.is_set() is False  # no driver was live to kill

    def test_a_kill_during_the_connect_ends_the_drive_as_a_timeout(
        self, clock, monkeypatch, tmp_path, timers, driver_log
    ):
        """The failed attach is not retried, nor is a tab that then disappears
        reported as missing: the drive ends as the timeout it is."""
        _Chrome(monkeypatch, tmp_path, fake_drive=False)
        log, state = driver_log
        proc = state["proc"] = _DriverProc()
        _Looks(monkeypatch, PORTAL, NO_TAB)

        def connect():
            timers[0].fire()
            proc.call("connect_over_cdp")

        state["connect"] = connect

        with pytest.raises(MercadoPagoWebError, match="did not finish in time"):
            web._drive_reuse("http://localhost:9333", 1)

        assert proc.late_calls == []
        assert log == ["start", ("connect", web.REUSE_TAB_WAIT_S * 1000), "stop"]

    def test_a_scraper_error_after_the_kill_is_reported_as_itself(
        self, monkeypatch, tmp_path, timers
    ):
        """Only a driver error becomes the timeout; a lost session stays one."""
        _Chrome(monkeypatch, tmp_path)

        def drive(*args, **kwargs):
            timers[0].fire()
            raise MercadoPagoSessionError("synthetic: sign in again")

        monkeypatch.setattr(web, "_drive_over_cdp", drive)

        with pytest.raises(MercadoPagoSessionError, match="sign in again"):
            web._drive_reuse("http://localhost:9333", 1)


def _patched_writer(monkeypatch):
    """Patch run_scraper's DB seam (no database); return the finish mock."""
    finish = MagicMock()
    monkeypatch.setattr(main_mod, "start_scraper_run", MagicMock(return_value="run-1"))
    monkeypatch.setattr(
        main_mod, "write_transactions", MagicMock(return_value=TransactionWriteResult())
    )
    monkeypatch.setattr(main_mod, "upsert_product", MagicMock())
    monkeypatch.setattr(
        main_mod,
        "retire_vanished_products",
        MagicMock(return_value=VanishedProductsResult()),
    )
    monkeypatch.setattr(main_mod, "finish_scraper_run", finish)
    return finish


class TestRunStatus:
    """The whole way: fake tab, real backend read, real scraper, real run_scraper."""

    def _run(self, monkeypatch, tab) -> MagicMock:
        async def scrape_account(cdp_url=None, movement_pages=1):
            return web.read_account(tab, movement_pages)

        monkeypatch.setattr(mp_mod, "scrape_account", scrape_account)
        finish = _patched_writer(monkeypatch)
        asyncio.run(main_mod.run_scraper(MercadoPagoScraper()))
        return finish

    def test_a_clean_read_is_a_success(self, monkeypatch, no_sleep):
        """Balance, pots and ledger all read: nothing to report."""
        finish = self._run(monkeypatch, _tab())

        finish.assert_called_once_with(
            "run-1", "success", transactions_imported=0, error_message=None
        )

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            pytest.param(
                {"pots": {"status": "http", "code": 500}},
                "MercadoPago: savings pots not read (pots: HTTP 500)",
                id="pots-page-failed",
            ),
            pytest.param(
                {("movements", 1): {"status": "timeout"}},
                "MercadoPago: movements page 1 not read (movements: timed out)",
                id="ledger-page-failed",
            ),
            pytest.param(
                {("movements", 1): _ok(_movements_props([_item(), _item(movement_id=None)]))},
                "MercadoPago: 1 movement row(s) not read",
                id="ledger-row-skipped",
            ),
        ],
    )
    def test_a_partial_read_ends_the_run_partial(self, monkeypatch, no_sleep, overrides, message):
        """What was read is stored, and the run says what is missing."""
        tab = _tab()
        tab.responses.update(overrides)

        finish = self._run(monkeypatch, tab)

        finish.assert_called_once_with(
            "run-1", "partial", transactions_imported=0, error_message=message
        )

    def test_a_lost_session_ends_the_run_in_error_naming_the_login(self, monkeypatch, no_sleep):
        """Both legs report the one failure; Chrome was driven once."""
        tab = _tab(balance={"status": "redirect"})

        finish = self._run(monkeypatch, tab)

        (_, status), kwargs = finish.call_args
        assert status == "error"
        assert kwargs["error_message"].count("make mercadopago-login") == 2
        assert tab.calls == ["balance"]


class TestManualSignIn:
    @pytest.fixture(autouse=True)
    def _session_reaches_disk(self, monkeypatch):
        """Unless a test says otherwise, the cookie is already on disk."""
        self.disk_reads: list[float] = []
        self.disk_from = 0.0
        monkeypatch.setattr(web, "_session_on_disk", self._disk)

    def _disk(self, profile, since):
        now = web.time.monotonic()
        self.disk_reads.append(now)
        return now >= self.disk_from

    def test_nothing_is_attached_or_fetched_while_the_tab_is_on_the_sign_in_site(
        self, clock, monkeypatch
    ):
        """Only Chrome's target list is read: no automation on the sign-in pages."""
        probes = []
        monkeypatch.setattr(web, "_balance_readable", lambda page: probes.append(page.url) or True)
        looks = _Looks(monkeypatch, ELSEWHERE)
        connections = _Connections(monkeypatch, _Browser(SIGN_IN_URL))

        with pytest.raises(MercadoPagoSessionError, match="make mercadopago-login"):
            web._await_sign_in(CDP_URL, timeout_s=30)

        assert connections.attempts == 0
        assert probes == []
        assert looks.count > 10

    def test_the_disk_is_read_only_after_the_jar_holds_the_lasting_cookie(
        self, clock, monkeypatch, capsys
    ):
        """Jar first, then the cookie store on disk; Chrome stays open meanwhile."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        jar = _Jar([SESSION_SSID], [SESSION_SSID], [LASTING_SSID])
        connections = _Connections(monkeypatch, _Browser(web.START_URL, jar=jar))
        self.disk_from = 8.0

        web._await_sign_in(CDP_URL, timeout_s=30)

        assert self.disk_reads == [4.0, 6.0, 8.0]
        assert clock.now == 8.0
        assert (connections.opened, connections.closed) == (1, 1)
        assert capsys.readouterr().out.count("waiting for it to reach the profile on disk") == 1

    def test_a_session_that_never_reaches_the_disk_fails_after_its_floor(self, clock, monkeypatch):
        """Closing Chrome now would lose it: an error, after the disk wait's own floor
        even though the sign-in deadline came first."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL))
        self.disk_from = float("inf")

        with pytest.raises(MercadoPagoSessionError, match="did not write the session"):
            web._await_sign_in(CDP_URL, timeout_s=20)

        assert clock.now == web._DISK_WAIT_FLOOR_S

    def test_the_disk_wait_runs_to_the_sign_in_deadline_when_that_is_later(
        self, clock, monkeypatch
    ):
        """The floor only ever lengthens the wait."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL))
        self.disk_from = float("inf")

        with pytest.raises(MercadoPagoSessionError, match="did not write the session"):
            web._await_sign_in(CDP_URL, timeout_s=200)

        assert clock.now == 200

    def test_the_login_start_reaches_the_disk_check(self, clock, monkeypatch):
        """Only cookies set after this login started may count on disk."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL))
        seen = []
        monkeypatch.setattr(
            web, "_session_on_disk", lambda profile, since: seen.append(since) or True
        )

        web._await_sign_in(CDP_URL, timeout_s=30, since=1_700_000_000.0)

        assert seen == [1_700_000_000.0]

    def test_a_window_closed_while_waiting_for_the_disk_ends_the_wait(self, clock, monkeypatch):
        """The launched Chrome is gone: no waiting out the clock."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL))
        self.disk_from = float("inf")

        with pytest.raises(MercadoPagoWebError, match="no longer running"):
            web._await_sign_in(CDP_URL, timeout_s=600, process=_Exited())

        assert clock.now == 0

    def test_returns_once_the_balance_page_reads(self, clock, monkeypatch):
        """Signed in: the first successful probe ends the wait."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(monkeypatch, _Browser(web.START_URL))

        web._await_sign_in(CDP_URL, timeout_s=30)

        assert clock.now == 0
        assert (connections.opened, connections.closed) == (1, 1)

    def test_a_readable_balance_waits_for_the_lasting_login_cookie(self, clock, monkeypatch):
        """The cookie lands on a later local read: no second balance probe, one connection."""
        probes = []
        monkeypatch.setattr(web, "_balance_readable", lambda page: probes.append(clock.now) or True)
        _Looks(monkeypatch, PORTAL)
        jar = _Jar([], [SESSION_SSID], [SESSION_SSID, LASTING_SSID])
        connections = _Connections(monkeypatch, _Browser(web.START_URL, jar=jar))

        web._await_sign_in(CDP_URL, timeout_s=30)

        assert probes == [0.0]
        assert len(jar.reads) == 3
        assert clock.now == 2 * web._LOGIN_POLL_S
        assert (connections.opened, connections.closed) == (1, 1)

    def test_only_a_session_cookie_fails_at_the_shared_deadline(self, clock, monkeypatch, capsys):
        """Signed in late with nothing lasting: the cookie wait ends at the sign-in deadline."""
        probes = []
        monkeypatch.setattr(
            web, "_balance_readable", lambda page: probes.append(clock.now) or clock.now >= 16
        )
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL, jar=_Jar([SESSION_SSID])))

        with pytest.raises(MercadoPagoSessionError, match="would not survive Chrome closing"):
            web._await_sign_in(CDP_URL, timeout_s=30)

        assert probes == [0.0, 16.0]
        assert clock.now == 30
        assert capsys.readouterr().out.count("waiting for Mercado Pago to save the session") == 1

    @pytest.mark.parametrize(
        "jar",
        [
            pytest.param(_Jar(RuntimeError("closed")), id="jar-unreadable"),
            pytest.param(_Jar([SESSION_SSID]), id="session-ssid-only"),
        ],
    )
    def test_a_window_closed_while_waiting_for_the_cookie_ends_the_wait(
        self, clock, monkeypatch, jar
    ):
        """The launched Chrome is gone: no waiting out the clock."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL, jar=jar))

        with pytest.raises(MercadoPagoWebError, match="no longer running"):
            web._await_sign_in(CDP_URL, timeout_s=600, process=_Exited())

        assert clock.now == 0

    def test_a_jar_that_keeps_failing_is_a_chrome_problem(self, clock, monkeypatch):
        """Failed reads in a row end the wait early, naming the failure type only."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        jar = _Jar(RuntimeError("synthetic"))
        _Connections(monkeypatch, _Browser(web.START_URL, jar=jar))

        with pytest.raises(MercadoPagoWebError, match=r"5 times in a row \(RuntimeError\)"):
            web._await_sign_in(CDP_URL, timeout_s=600)

        assert len(jar.reads) == web._MAX_JAR_READ_FAILURES
        assert clock.now == (web._MAX_JAR_READ_FAILURES - 1) * web._LOGIN_POLL_S

    def test_a_failed_jar_read_followed_by_the_cookie_succeeds(self, clock, monkeypatch):
        """One unreadable moment is not fatal; the count resets on a good read."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        failed = RuntimeError("synthetic")
        jar = _Jar(failed, [SESSION_SSID], failed, [LASTING_SSID])
        _Connections(monkeypatch, _Browser(web.START_URL, jar=jar))

        web._await_sign_in(CDP_URL, timeout_s=600)

        assert len(jar.reads) == 4

    def test_the_deadline_logs_cookie_names_never_values(self, clock, monkeypatch, caplog):
        """What Chrome held is logged by name and persistence only."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, PORTAL)
        _Connections(monkeypatch, _Browser(web.START_URL, jar=_Jar([SESSION_SSID])))

        with caplog.at_level("INFO"), pytest.raises(MercadoPagoSessionError):
            web._await_sign_in(CDP_URL, timeout_s=10)

        assert "ssid(session)" in caplog.text
        assert "synthetic" not in caplog.text

    @pytest.mark.parametrize(
        ("cookies", "expected"),
        [
            pytest.param([LASTING_SSID], True, id="persistent-ssid"),
            pytest.param([SESSION_SSID], False, id="session-only-ssid"),
            pytest.param([{"name": "orguserid", "expires": 1_900_000_000.0}], False, id="other"),
            pytest.param([], False, id="none"),
        ],
    )
    def test_only_a_persistent_ssid_is_a_lasting_session(self, cookies, expected):
        """Read from Chrome's jar for the portal; a session cookie does not count."""
        jar = _Jar(cookies)

        assert web._has_lasting_session(_Tab(web.START_URL, jar)) is expected
        assert jar.reads == [f"https://{web.PORTAL_HOST}/"]

    def test_probes_sparingly_while_the_tab_does_not_move(self, clock, monkeypatch):
        """An unreadable portal tab is probed, and attached to, on an interval."""
        probes = []
        monkeypatch.setattr(
            web, "_balance_readable", lambda page: probes.append(clock.now) and False
        )
        _Looks(monkeypatch, PORTAL)
        connections = _Connections(monkeypatch, _Browser("https://www.mercadopago.cl/"))

        with pytest.raises(MercadoPagoSessionError):
            web._await_sign_in(CDP_URL, timeout_s=60)

        assert probes == [0.0, 16.0, 32.0, 48.0]
        assert (connections.opened, connections.closed) == (4, 4)

    def test_a_tab_that_moves_is_probed_at_once(self, clock, monkeypatch):
        """Landing on a new portal page after signing in does not wait the interval."""
        probes = []
        monkeypatch.setattr(
            web, "_balance_readable", lambda page: probes.append(clock.now) and False
        )
        _Looks(
            monkeypatch,
            _summary(portal=1, key="synthetic-key-1"),
            _summary(portal=1, key="synthetic-key-2"),
        )
        _Connections(monkeypatch, _Browser("https://www.mercadopago.cl/"))

        with pytest.raises(MercadoPagoSessionError):
            web._await_sign_in(CDP_URL, timeout_s=10)

        assert probes == [0.0, 2.0]

    def test_a_stale_attach_is_followed_by_a_fresh_one(self, clock, monkeypatch):
        """A connection made while the portal tab loads never sees it."""
        monkeypatch.setattr(web, "_balance_readable", lambda page: True)
        _Looks(monkeypatch, ELSEWHERE, ELSEWHERE, PORTAL)
        connections = _Connections(monkeypatch, _Browser(STALE), NO_ATTACH, _Browser(web.START_URL))

        web._await_sign_in(CDP_URL, timeout_s=600)

        assert (connections.attempts, connections.opened, connections.closed) == (3, 2, 2)
        assert connections.max_open == 1

    def test_a_chrome_that_never_starts_fails_without_attaching(self, clock, monkeypatch):
        """The debug port never answers: the launch hint, after the launch wait."""
        _Looks(monkeypatch, DOWN)
        connections = _Connections(monkeypatch, _Browser(STALE))

        with pytest.raises(MercadoPagoConnectError, match="Chrome did not start"):
            web._await_sign_in(CDP_URL, timeout_s=600)

        assert connections.attempts == 0
        assert clock.now == web.CONNECT_WAIT_S

    def test_a_closed_window_ends_the_wait_at_once(self, clock, monkeypatch):
        """The launched Chrome has ended: no point waiting out the clock."""
        _Looks(monkeypatch, ELSEWHERE, DOWN)

        with pytest.raises(MercadoPagoWebError, match="no longer running"):
            web._await_sign_in(CDP_URL, timeout_s=600, process=_Exited())

        assert clock.now == web._LOGIN_POLL_S

    def test_a_port_that_stops_answering_ends_the_wait_early(self, clock, monkeypatch):
        """Without a process to ask, several unanswered looks in a row decide."""
        looks = _Looks(monkeypatch, ELSEWHERE, DOWN)

        with pytest.raises(MercadoPagoConnectError, match="stopped answering"):
            web._await_sign_in(CDP_URL, timeout_s=600)

        assert looks.count == 1 + web._MAX_UNANSWERED_LOOKS
        assert clock.now < 60

    def test_a_probe_is_one_bounded_fetch_of_the_balance_page(self):
        """The probe reads the balance page and nothing else."""
        tab = _tab()

        assert web._balance_readable(tab) is True
        assert tab.calls == ["balance"]

    def test_a_redirected_probe_is_just_not_signed_in_yet(self):
        """While the human signs in, a redirect is not an error."""
        tab = _tab(balance={"status": "redirect"})

        assert web._balance_readable(tab) is False

    def test_opens_a_visible_chrome_and_closes_it_afterwards(self, monkeypatch, tmp_path, capsys):
        """The human needs to see the window; Chrome is closed when done."""
        chrome = _Chrome(monkeypatch, tmp_path, profile_exists=False)
        waits = []
        monkeypatch.setattr(
            web, "_await_sign_in", lambda url, timeout, process=None, since=None: waits.append(url)
        )

        web.save_login_session(timeout_s=60)

        (launch,) = chrome.launches
        assert launch["offscreen"] is False
        assert launch["url"] == web.START_URL
        assert waits == ["http://127.0.0.1:9223"]
        assert chrome.terminated == [chrome.proc]
        assert "nothing is typed for you" in capsys.readouterr().out

    def test_closes_chrome_when_the_sign_in_times_out(self, monkeypatch, tmp_path):
        """A timeout still tears the window down."""
        chrome = _Chrome(monkeypatch, tmp_path)

        def never(url, timeout, process=None, since=None):
            raise MercadoPagoSessionError("Sign-in not detected in time.")

        monkeypatch.setattr(web, "_await_sign_in", never)

        with pytest.raises(MercadoPagoSessionError):
            web.save_login_session(timeout_s=60)

        assert chrome.terminated == [chrome.proc]


SYNTHETIC_SINCE = "2026-01-01T00:00:00+00:00"


class TestSignInMarker:
    """A lost managed session is remembered, so later runs skip Chrome until a sign-in."""

    def _marker(self, tmp_path):
        return tmp_path / "profile.needs-sign-in"

    def test_the_marker_sits_next_to_the_profile(self, monkeypatch, tmp_path):
        """`<profile>.needs-sign-in`, a sibling of the managed profile directory."""
        monkeypatch.setenv("MERCADOPAGO_CHROME_PROFILE", str(tmp_path / "profile"))

        assert web.sign_in_marker_path() == self._marker(tmp_path)

    @pytest.mark.parametrize(
        "reason",
        ["the portal redirected to sign in", "the tab left the portal", "HTTP 401"],
    )
    def test_a_lost_session_leaves_a_private_timestamped_marker(
        self, monkeypatch, tmp_path, reason
    ):
        """Redirect, bounced tab or 401/403: the marker holds when it happened."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.outcome = web._session_lost(reason)

        with pytest.raises(MercadoPagoSessionError):
            asyncio.run(web.scrape_account())

        marker = self._marker(tmp_path)
        written = datetime.fromisoformat(marker.read_text())
        assert written.utcoffset() == timedelta(0)

    @pytest.mark.parametrize(
        "error",
        [
            MercadoPagoPageError("balance: missing initialResult"),
            MercadoPagoFetchError("balance: HTTP 500"),
            MercadoPagoConnectError("Chrome did not start"),
            MercadoPagoWebError("Mercado Pago read did not finish in time."),
        ],
        ids=["page-looks-wrong", "fetch-failed", "connect-failed", "plain-web-error"],
    )
    def test_other_failures_leave_no_marker(self, monkeypatch, tmp_path, error):
        """Only a lost session asks for a sign-in; a broken page or Chrome does not."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.outcome = error

        with pytest.raises(MercadoPagoWebError):
            asyncio.run(web.scrape_account())

        assert not self._marker(tmp_path).exists()

    def test_a_missing_profile_leaves_no_marker(self, monkeypatch, tmp_path):
        """Nothing was signed in to lose, and nothing was launched."""
        _Chrome(monkeypatch, tmp_path, profile_exists=False)

        with pytest.raises(MercadoPagoSessionError):
            asyncio.run(web.scrape_account())

        assert not self._marker(tmp_path).exists()

    def test_a_marked_run_skips_without_launching_or_probing_the_port(self, monkeypatch, tmp_path):
        """No port probe, no launch, no drive: the error names when and the login."""
        chrome = _Chrome(monkeypatch, tmp_path)
        self._marker(tmp_path).write_text(SYNTHETIC_SINCE)
        probes = []
        monkeypatch.setattr(web.real_chrome, "port_in_use", probes.append)

        with pytest.raises(MercadoPagoSessionError) as info:
            asyncio.run(web.scrape_account())

        assert str(info.value) == (
            "Mercado Pago needs sign-in: run `make mercadopago-login` "
            "(skipped without opening Chrome; lost since 2026-01-01 00:00 UTC)."
        )
        assert probes == []
        assert chrome.launches == []
        assert chrome.drives == []
        assert self._marker(tmp_path).exists()

    def test_an_empty_marker_still_skips(self, monkeypatch, tmp_path):
        """A marker without a time is still a marker."""
        chrome = _Chrome(monkeypatch, tmp_path)
        self._marker(tmp_path).write_text("")

        with pytest.raises(MercadoPagoSessionError, match="lost since an earlier run"):
            asyncio.run(web.scrape_account())

        assert chrome.launches == []

    def _drive_reads(self, monkeypatch, tab):
        """Make the managed drive run the real read on `tab`."""
        monkeypatch.setattr(
            web, "_drive_over_cdp", lambda cdp_url, pages, **kwargs: web.read_account(tab, pages)
        )

    def test_a_redirect_on_the_balance_page_marks(self, monkeypatch, tmp_path, no_sleep):
        """The balance page is what `make mercadopago-login` checks, so it can clear this."""
        _Chrome(monkeypatch, tmp_path)
        self._drive_reads(monkeypatch, _tab(balance={"status": "redirect"}))

        with pytest.raises(MercadoPagoSessionError):
            asyncio.run(web.scrape_account())

        assert self._marker(tmp_path).exists()

    @pytest.mark.parametrize("surface", ["pots", ("movements", 1)], ids=["pots", "ledger"])
    def test_a_lost_session_after_the_balance_read_does_not_mark(
        self, monkeypatch, tmp_path, no_sleep, surface
    ):
        """The run still fails, but a login (which checks only the balance) could not clear it."""
        chrome = _Chrome(monkeypatch, tmp_path)
        tab = _tab()
        tab.responses[surface] = {"status": "redirect"}
        self._drive_reads(monkeypatch, tab)

        with pytest.raises(MercadoPagoSessionError, match="make mercadopago-login") as info:
            asyncio.run(web.scrape_account())

        assert info.value.after_balance is True
        assert not self._marker(tmp_path).exists()
        assert chrome.terminated == [chrome.proc]

    def test_reuse_mode_ignores_the_marker(self, monkeypatch, tmp_path):
        """The user signs in in their own Chrome there, so a marker would block forever."""
        chrome = _Chrome(monkeypatch, tmp_path)
        self._marker(tmp_path).write_text(SYNTHETIC_SINCE)

        account = asyncio.run(web.scrape_account(cdp_url="http://localhost:9333"))

        assert account is chrome.outcome
        assert self._marker(tmp_path).read_text() == SYNTHETIC_SINCE

    def test_a_lost_session_in_reuse_mode_leaves_no_marker(self, monkeypatch, tmp_path):
        """Only the managed Chrome's sign-in (`make mercadopago-login`) can clear one."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.outcome = web._session_lost("HTTP 403")

        with pytest.raises(MercadoPagoSessionError):
            asyncio.run(web.scrape_account(cdp_url="http://localhost:9333"))

        assert not self._marker(tmp_path).exists()

    def test_a_marker_that_cannot_be_written_only_warns(self, monkeypatch, tmp_path, caplog):
        """The run still ends with the lost-session error, not a filesystem one."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.outcome = web._session_lost("HTTP 401")

        def unwritable(**kwargs):
            raise PermissionError("synthetic")

        monkeypatch.setattr(sign_in_marker.tempfile, "mkstemp", unwritable)

        with (
            caplog.at_level(logging.WARNING, logger="scrapers.sign_in_marker"),
            pytest.raises(MercadoPagoSessionError, match="HTTP 401"),
        ):
            asyncio.run(web.scrape_account())

        assert "Could not write the sign-in marker" in caplog.text
        assert not self._marker(tmp_path).exists()
        assert chrome.terminated == [chrome.proc]

    def test_a_successful_sign_in_clears_the_marker(self, monkeypatch, tmp_path):
        """`make mercadopago-login` that reads the balance lifts the skip."""
        _Chrome(monkeypatch, tmp_path)
        self._marker(tmp_path).write_text(SYNTHETIC_SINCE)
        monkeypatch.setattr(
            web, "_await_sign_in", lambda url, timeout, process=None, since=None: None
        )

        web.save_login_session(timeout_s=60)

        assert not self._marker(tmp_path).exists()

    def test_a_session_missing_on_disk_after_the_login_keeps_the_marker(
        self, monkeypatch, tmp_path
    ):
        """A login whose session did not reach the profile would only be signed out again."""
        chrome = _Chrome(monkeypatch, tmp_path)
        chrome.on_disk = False
        self._marker(tmp_path).write_text(SYNTHETIC_SINCE)
        monkeypatch.setattr(
            web, "_await_sign_in", lambda url, timeout, process=None, since=None: None
        )

        with pytest.raises(MercadoPagoSessionError, match="not in the profile on disk"):
            web.save_login_session(timeout_s=60)

        assert self._marker(tmp_path).read_text() == SYNTHETIC_SINCE

    def test_a_failed_sign_in_keeps_the_marker(self, monkeypatch, tmp_path):
        """Runs keep skipping until a sign-in actually works."""
        _Chrome(monkeypatch, tmp_path)
        self._marker(tmp_path).write_text(SYNTHETIC_SINCE)

        def never(url, timeout, process=None, since=None):
            raise MercadoPagoSessionError("Sign-in not detected in time.")

        monkeypatch.setattr(web, "_await_sign_in", never)

        with pytest.raises(MercadoPagoSessionError):
            web.save_login_session(timeout_s=60)

        assert self._marker(tmp_path).read_text() == SYNTHETIC_SINCE

    def test_a_skipped_run_logs_a_warning_without_a_traceback(self, monkeypatch, tmp_path, caplog):
        """Expected on every run until a sign-in, so the scraper log stays short."""
        _Chrome(monkeypatch, tmp_path)
        self._marker(tmp_path).write_text(SYNTHETIC_SINCE)

        with (
            caplog.at_level(logging.WARNING, logger=mp_mod.__name__),
            pytest.raises(web.MercadoPagoSignInPending),
        ):
            asyncio.run(MercadoPagoScraper().scrape_transactions())

        (record,) = [r for r in caplog.records if r.name == mp_mod.__name__]
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert "needs sign-in" in record.getMessage()

    def test_a_skipped_run_still_finishes_its_run_row_in_error(self, monkeypatch, tmp_path):
        """The web poller waits for a finished row: it gets one, with the hint."""
        chrome = _Chrome(monkeypatch, tmp_path)
        self._marker(tmp_path).write_text(SYNTHETIC_SINCE)
        finish = _patched_writer(monkeypatch)

        asyncio.run(main_mod.run_scraper(MercadoPagoScraper()))

        (run_id, status), kwargs = finish.call_args
        assert (run_id, status) == ("run-1", "error")
        assert "make mercadopago-login" in kwargs["error_message"][:120]
        assert chrome.launches == []


# --- The scraper: both legs through a faked backend facade ----------------------


def _movement(
    movement_id=MOVEMENT_ID,
    day=date(2026, 11, 10),
    amount=-999_999,
    title="COMERCIO SINTETICO",
    description="Pago",
) -> Movement:
    return Movement(
        movement_id=movement_id, date=day, amount=amount, title=title, description=description
    )


def _account(**overrides) -> MercadoPagoAccount:
    fields = {
        "balance": 2_500_000,
        "pots": [
            Pot(id=POT_ID, name="Reserva sintética", amount=1_000_000),
            Pot(id=POT_ID_2, name="Reserva sintética 2", amount=999_999),
        ],
        "movements": [
            _movement(),
            _movement(
                movement_id=MOVEMENT_ID_2,
                day=date(2026, 10, 31),
                amount=1_000_000,
                title="Persona Sintética",
                description="Transferencia recibida",
            ),
            _movement(
                movement_id=MOVEMENT_ID_3,
                amount=999_999,
                title="Rendimientos BICE",
                description="Rentabilidad",
            ),
        ],
    }
    fields.update(overrides)
    return MercadoPagoAccount(**fields)


def _install(monkeypatch, account=None, exc=None) -> dict:
    """Replace the backend facade; count the drives and record their arguments."""
    calls = {"n": 0, "movement_pages": []}

    async def fake_scrape_account(cdp_url=None, movement_pages=1):
        calls["n"] += 1
        calls["movement_pages"].append(movement_pages)
        if exc is not None:
            raise exc
        return account

    monkeypatch.setattr(mp_mod, "scrape_account", fake_scrape_account)
    return calls


class TestIdentity:
    def test_external_id_is_the_ledger_id_with_the_prefix(self):
        """`mp_` plus the ledger's own per-row id."""
        assert mp_mod.movement_external_id(MOVEMENT_ID) == f"mp_{MOVEMENT_ID}"

    def test_external_id_ignores_cosmetic_fields(self):
        """A rewritten title or description never re-keys a movement."""
        scraper = MercadoPagoScraper()

        first = scraper._to_transaction(_movement())
        second = scraper._to_transaction(_movement(title="OTRO COMERCIO", description=""))

        assert first.external_id == second.external_id

    def test_distinct_movements_get_distinct_ids(self):
        """Two rows of the same day and amount stay two transactions."""
        scraper = MercadoPagoScraper()

        first = scraper._to_transaction(_movement())
        second = scraper._to_transaction(_movement(movement_id=MOVEMENT_ID_2))

        assert first.external_id != second.external_id

    def test_pot_ref_is_stable_and_prefixed(self):
        """One helper builds the savings products' external_ref."""
        assert mp_mod.pot_ref(POT_ID) == f"mp_pot_{POT_ID}"

    @pytest.mark.parametrize(
        ("title", "description", "expected"),
        [
            pytest.param("COMERCIO SINTETICO", "Pago", "COMERCIO SINTETICO (Pago)", id="both"),
            pytest.param("COMERCIO SINTETICO", "", "COMERCIO SINTETICO", id="no-description"),
            pytest.param(
                "Rentabilidad", "rentabilidad", "Rentabilidad", id="description-repeats-title"
            ),
            pytest.param(
                "Persona Sintética",
                "Transferencia recibida",
                "Persona Sintética (Transferencia recibida)",
                id="transfer",
            ),
        ],
    )
    def test_description_combines_title_and_description(self, title, description, expected):
        """The title alone when the description adds nothing."""
        assert mp_mod.movement_description(title, description) == expected


class TestLegs:
    def setup_method(self):
        self.scraper = MercadoPagoScraper()

    def test_is_a_web_scraper_for_mercadopago(self):
        """The method changed with the technique; the institution did not."""
        assert (self.scraper.method, self.scraper.institution) == ("web", "mercadopago")

    def test_transactions_then_products_drives_chrome_once(self, monkeypatch):
        """run_scraper's order: the transactions leg drives, the products leg reuses."""
        calls = _install(monkeypatch, account=_account())

        transactions = asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert len(transactions) == 3
        assert len(result.products) == 3

    def test_products_alone_drives_chrome(self, monkeypatch):
        """Outside run_scraper's order the products leg drives on its own."""
        calls = _install(monkeypatch, account=_account())

        result = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert len(result.products) == 3

    def test_a_failed_drive_is_re_raised_not_retried(self, monkeypatch):
        """A lost session is never tried twice in a cycle."""
        error = MercadoPagoSessionError("Run `make mercadopago-login`.")
        calls = _install(monkeypatch, exc=error)

        with pytest.raises(MercadoPagoSessionError):
            asyncio.run(self.scraper.scrape_transactions())
        with pytest.raises(MercadoPagoSessionError) as info:
            asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert info.value is error

    def test_each_cycle_drives_again(self, monkeypatch):
        """The scraper lives for the whole service: a cycle never reuses the last."""
        calls = _install(monkeypatch, account=_account())

        for _ in range(2):
            asyncio.run(self.scraper.scrape_transactions())
            asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 2

    def test_a_new_cycle_reads_the_new_figures(self, monkeypatch):
        """Nothing of the previous cycle's result survives into the next."""
        _install(monkeypatch, account=_account())
        asyncio.run(self.scraper.scrape_transactions())
        asyncio.run(self.scraper.scrape_products())
        _install(monkeypatch, account=_account(balance=1_000_000, pots=[]))

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert [product.metrics.balance for product in result.products] == [1_000_000]

    def test_an_old_error_does_not_outlive_its_cycle(self, monkeypatch):
        """A cycle that fails does not poison the next one."""
        _install(monkeypatch, exc=MercadoPagoSessionError("synthetic"))
        with pytest.raises(MercadoPagoSessionError):
            asyncio.run(self.scraper.scrape_transactions())
        _install(monkeypatch, account=_account())

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert len(result.products) == 3

    def test_wallet_product_is_the_singleton(self, monkeypatch):
        """The same product the old rows used: wallet, CLP, no external_ref."""
        _install(monkeypatch, account=_account())

        wallet = asyncio.run(self.scraper.scrape_products()).products[0]

        assert (wallet.institution, wallet.kind, wallet.currency, wallet.external_ref) == (
            "mercadopago",
            "wallet",
            "CLP",
            None,
        )
        assert wallet.metrics.balance == 2_500_000

    def test_each_pot_is_a_savings_product_with_its_ref(self, monkeypatch):
        """One product per pot, named after it, told apart by external_ref."""
        _install(monkeypatch, account=_account())

        pots = asyncio.run(self.scraper.scrape_products()).products[1:]

        assert [(p.kind, p.currency, p.external_ref, p.name, p.metrics.balance) for p in pots] == [
            ("savings", "CLP", f"mp_pot_{POT_ID}", "Reserva sintética", 1_000_000),
            ("savings", "CLP", f"mp_pot_{POT_ID_2}", "Reserva sintética 2", 999_999),
        ]

    def test_products_carry_the_yield_metrics(self, monkeypatch):
        """The wallet's rate and gain, and each pot's rate, land in the metrics."""
        account = _account(
            wallet_yield=web.WalletYield(rate_pct=1.0, profit_12m=999_999),
            pots=[Pot(id=POT_ID, name="Reserva sintética", amount=1_000_000, rate_pct=4.5)],
        )
        _install(monkeypatch, account=account)

        wallet, pot = asyncio.run(self.scraper.scrape_products()).products

        assert (wallet.metrics.annual_rate_pct, wallet.metrics.profit_12m) == (1.0, 999_999)
        assert (pot.metrics.annual_rate_pct, pot.metrics.profit) == (4.5, None)

    def test_missing_yield_leaves_the_metrics_empty(self, monkeypatch):
        """No rate or gain on the pages: the metrics stay None, never zero."""
        _install(monkeypatch, account=_account())

        wallet, *pots = asyncio.run(self.scraper.scrape_products()).products

        assert (wallet.metrics.annual_rate_pct, wallet.metrics.profit_12m) == (None, None)
        assert {(p.metrics.annual_rate_pct, p.metrics.profit) for p in pots} == {(None, None)}

    def test_no_savings_product_is_ever_ref_less(self, monkeypatch):
        """A ref-less savings product would be a roll-up the writer retires."""
        _install(monkeypatch, account=_account())

        products = asyncio.run(self.scraper.scrape_products()).products

        assert all(p.external_ref for p in products if p.kind == "savings")
        assert [p.kind for p in products if p.external_ref is None] == ["wallet"]

    def test_an_account_without_pots_emits_only_the_wallet(self, monkeypatch):
        """No pots, no savings product."""
        _install(monkeypatch, account=_account(pots=[]))

        products = asyncio.run(self.scraper.scrape_products()).products

        assert [p.kind for p in products] == ["wallet"]

    def test_a_complete_pots_read_declares_the_pots_scope(self, monkeypatch):
        """Savings, CLP, the pot prefix, every pot's ref, closed at a zero balance."""
        _install(monkeypatch, account=_account(pots_complete=True))

        (scope,) = asyncio.run(self.scraper.scrape_products()).vanished_product_scopes

        assert (
            scope.institution,
            scope.product_kind,
            scope.currency,
            scope.external_ref_prefix,
            scope.present_refs,
            scope.closed_metrics.balance,
        ) == (
            "mercadopago",
            "savings",
            "CLP",
            "mp_pot_",
            frozenset({f"mp_pot_{POT_ID}", f"mp_pot_{POT_ID_2}"}),
            0,
        )

    def test_a_complete_read_without_pots_declares_an_empty_scope(self, monkeypatch):
        """No pots at all is a real reading: every stored pot was closed."""
        _install(monkeypatch, account=_account(pots=[], pots_complete=True))

        (scope,) = asyncio.run(self.scraper.scrape_products()).vanished_product_scopes

        assert scope.present_refs == frozenset()

    def test_an_incomplete_pots_read_declares_no_scope(self, monkeypatch):
        """A failed or partial pots read must never retire a pot."""
        _install(monkeypatch, account=_account(pots_complete=False))

        result = asyncio.run(self.scraper.scrape_products())

        assert result.vanished_product_scopes == []

    def test_an_unnamed_pot_gets_the_default_name(self, monkeypatch):
        """A blank name is sent as none, so the writer names the product."""
        _install(monkeypatch, account=_account(pots=[Pot(id=POT_ID, name="", amount=1_000_000)]))

        pot = asyncio.run(self.scraper.scrape_products()).products[1]

        assert pot.name is None

    def test_wallet_rows_land_on_a_product_the_run_emits(self, monkeypatch):
        """No row names a ref, and the singleton they target is emitted."""
        _install(monkeypatch, account=_account())

        transactions = asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products()).products

        singletons = {(p.kind, p.currency) for p in products if p.external_ref is None}
        assert all(t.product_external_ref is None for t in transactions)
        assert {(t.product_kind, t.currency) for t in transactions} <= singletons
        assert {t.institution for t in transactions} == {"mercadopago"}

    def test_no_pot_transactions_are_emitted(self, monkeypatch):
        """The pot pages carry no movements, so nothing targets savings."""
        _install(monkeypatch, account=_account())

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert {t.product_kind for t in transactions} == {"wallet"}

    def test_every_transaction_has_its_scheduled_month(self, monkeypatch):
        """The first of the month of its Santiago date; Gastos filters on it."""
        _install(monkeypatch, account=_account())

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert [(t.transaction_date, t.scheduled_month) for t in transactions] == [
            (date(2026, 11, 10), date(2026, 11, 1)),
            (date(2026, 10, 31), date(2026, 10, 1)),
            (date(2026, 11, 10), date(2026, 11, 1)),
        ]

    def test_transaction_fields(self, monkeypatch):
        """Sign kept, CLP, prefixed id, readable description."""
        _install(monkeypatch, account=_account())

        first, second, third = asyncio.run(self.scraper.scrape_transactions())

        assert (first.amount, first.currency, first.external_id, first.description) == (
            -999_999,
            "CLP",
            f"mp_{MOVEMENT_ID}",
            "COMERCIO SINTETICO (Pago)",
        )
        assert (second.amount, second.description) == (
            1_000_000,
            "Persona Sintética (Transferencia recibida)",
        )
        assert (third.amount, third.description) == (999_999, "Rendimientos BICE (Rentabilidad)")

    def test_backend_warnings_reach_the_products_leg(self, monkeypatch):
        """Partial coverage makes the run partial: the warnings ride on products."""
        warning = "MercadoPago: savings pots not read (pots: HTTP 500)"
        _install(monkeypatch, account=_account(pots=[], warnings=[warning]))

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert result.warnings == [warning]
        assert [p.kind for p in result.products] == ["wallet"]

    def test_an_empty_ledger_is_no_transactions(self, monkeypatch):
        """Nothing happened: an empty list, not an error."""
        _install(monkeypatch, account=_account(movements=[]))

        assert asyncio.run(self.scraper.scrape_transactions()) == []


class TestMovementPages:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(None, 1, id="unset"),
            pytest.param("", 1, id="empty"),
            pytest.param("3", 3, id="three-pages"),
            pytest.param("0", 0, id="zero-means-all"),
        ],
    )
    def test_env_sets_the_pages_per_run(self, monkeypatch, value, expected):
        """MERCADOPAGO_MOVEMENT_PAGES is passed to the backend as is."""
        if value is None:
            monkeypatch.delenv("MERCADOPAGO_MOVEMENT_PAGES", raising=False)
        else:
            monkeypatch.setenv("MERCADOPAGO_MOVEMENT_PAGES", value)
        calls = _install(monkeypatch, account=_account())

        asyncio.run(MercadoPagoScraper().scrape_transactions())

        assert calls["movement_pages"] == [expected]

    @pytest.mark.parametrize("value", ["synthetic", "-1", "1.5"])
    def test_a_bad_value_falls_back_to_the_default(self, monkeypatch, caplog, value):
        """A typo must not stop the service at startup: one page, and a warning."""
        caplog.set_level(logging.WARNING, logger=mp_mod.__name__)
        monkeypatch.setenv("MERCADOPAGO_MOVEMENT_PAGES", value)

        scraper = MercadoPagoScraper()

        assert scraper.movement_pages == 1
        assert "MERCADOPAGO_MOVEMENT_PAGES" in caplog.text


class TestLoginCli:
    """`make mercadopago-login`: the sign-in itself is the backend's, faked here."""

    @pytest.fixture(autouse=True)
    def _no_dotenv(self, monkeypatch):
        monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
        monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)

    def test_reports_success(self, monkeypatch, capsys):
        """A sign-in that completes says where the session went."""
        calls = []
        monkeypatch.setattr(mp_mod, "save_login_session", lambda: calls.append("login"))

        mp_mod._login_cli()

        assert calls == ["login"]
        assert "Signed in" in capsys.readouterr().out

    def test_a_missed_sign_in_exits_non_zero_with_the_reason(self, monkeypatch, capsys):
        """No traceback for the human: the message, and a failing exit status."""

        def timed_out():
            raise MercadoPagoSessionError("Sign-in not detected in time.")

        monkeypatch.setattr(mp_mod, "save_login_session", timed_out)

        with pytest.raises(SystemExit) as info:
            mp_mod._login_cli()

        assert info.value.code == 1
        assert "Sign-in not detected in time." in capsys.readouterr().out


class TestNothingLeaks:
    """Error messages are stored in scraper_runs and shown on the dashboard."""

    SECRETS = (
        "Persona Sintética",
        "Reserva sintética",
        "2.500.000",
        "2500000",
        "999.999",
        "999999",
        MOVEMENT_ID,
        POT_ID,
        "<html",
        "<script",
        "synthetic-cookie",
        "/banking/balance/movements/",
    )

    def _leaky_page(self) -> str:
        """A page full of things an error message must never repeat."""
        props = _movements_props([_item(title="Persona Sintética")])
        props["listData"]["pages"] = "synthetic-cookie"
        props["pot"] = _pot()
        props["balance"] = _balance_props()
        return _page(props)

    def _assert_clean(self, error: Exception) -> None:
        """Nothing leaks from any exception in the chain, whatever its type:
        a traceback prints every cause and context."""
        for link in _chain(error):
            for secret in self.SECRETS:
                assert secret not in str(link)

    def test_a_page_error_names_fields_only(self):
        """A changed ledger page: the message is the field name, not the page."""
        with pytest.raises(MercadoPagoPageError) as info:
            web.parse_movements(web.page_props_from_html(self._leaky_page()))

        assert str(info.value) == "movements: pages is not a count"
        self._assert_clean(info.value)

    def test_an_error_from_the_balance_page_carries_nothing_of_it(self):
        """The balance probe failing on a full page still leaks none of it."""
        tab = _tab(balance={"status": "ok", "html": self._leaky_page()})

        with pytest.raises(MercadoPagoWebError) as info:
            web.read_account(tab)

        assert "balance: initialResult missing" in str(info.value)
        self._assert_clean(info.value)

    def test_a_fetch_failure_does_not_quote_the_playwright_error(self):
        """Only the exception's type is kept from a failed in-page call."""
        failure = RuntimeError("Persona Sintética at https://example.invalid/?id=synthetic-cookie")
        tab = _tab(balance=failure)

        with pytest.raises(MercadoPagoWebError) as info:
            web.read_account(tab)

        assert "RuntimeError" in str(info.value)
        assert failure not in _chain(info.value)
        self._assert_clean(info.value)

    def test_warnings_carry_counts_and_codes_only(self, no_sleep):
        """Warnings join the run's message too."""
        tab = _tab(pots={"status": "ok", "html": self._leaky_page()})
        tab.responses[("movements", 1)] = _ok(
            _movements_props([_item(movement_id=None, title="Persona Sintética")])
        )

        account = web.read_account(tab)

        assert account.warnings == [
            "MercadoPago: savings pots not read (pots: hub missing)",
            "MercadoPago: movements page 1 not read (movements: no row could be read)",
        ]

    def test_logs_carry_counts_only(self, caplog):
        """The read's log lines never quote a name, an id or an amount."""
        caplog.set_level(logging.DEBUG, logger=web.__name__)
        tab = _tab(
            pots=_ok(_pots_props([_pot(), _pot(pot_id=POT_ID_2, currency="USD")])),
        )

        web.read_account(tab)

        for secret in self.SECRETS:
            assert secret not in caplog.text
