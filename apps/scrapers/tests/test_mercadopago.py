"""Tests for the Mercado Pago scraper: page parsers.

No browser, no Chrome and no network: the parsers get HTML and page data
built here from scratch to mirror the structure of the real pages (field
names and nesting only). Every value is synthetic, from the repo's canonical
set: amounts 2.500.000 / 1.000.000 / 999.999, movement ids like
A00000000000000B1, the pot uuid 00000000-0000-0000-0000-000000000001,
"Persona Sintética", "Reserva sintética".
"""

import json
import logging
from datetime import date

import pytest

from scrapers.backends import mercadopago_web as web
from scrapers.backends.mercadopago_web import MercadoPagoPageError, Movement, Pot

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


def _balance_props(raw=2_500_000, status="success") -> dict:
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
                            "title": {"middle": {"raw": 999_999}},
                            "subtitle": "Generando ganancias al 1,0% anual",
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


def _pots_props(pots: list, status="success") -> dict:
    return {
        "hub": {
            "result": {
                "potsDataHub": {
                    "status": {"type": status},
                    "result": {"pots": pots, "pots_amount": {"raw": 1_000_000}},
                },
                "investmentData": {
                    "data": {"is_invested": True, "rate": "1.0", "fund_name": "Sintetico"}
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
            pytest.param('<script>_n.ctx.r={"other": {"appProps": 1}};</script>', id="nested-elsewhere"),
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
            pytest.param("<html><body>Ingresa a tu cuenta</body></html>", "marker missing", id="no-marker"),
            pytest.param("<script>_n.ctx.r=undefined;</script>", "not JSON", id="not-json"),
            pytest.param("<script>_n.ctx.r=</script>", "not JSON", id="nothing-after-marker"),
            pytest.param("<script>_n.ctx.r=_n.ctx.r||{};</script>", "not JSON", id="only-a-guard"),
            pytest.param("<script>_n.ctx.r=[1, 2];</script>", "no appProps", id="not-an-object"),
            pytest.param('<script>_n.ctx.r={"a": 1};</script>', "no appProps", id="object-without-app-props"),
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


class TestParsePots:
    def test_no_pots(self):
        """An account without pots is a real reading, not a failure."""
        assert web.parse_pots(_pots_props([])) == ([], 0)

    def test_one_pot(self):
        """Its uuid, name and CLP amount."""
        pots, skipped = web.parse_pots(_pots_props([_pot()]))

        assert pots == [Pot(id=POT_ID, name="Reserva sintética", amount=1_000_000)]
        assert skipped == 0

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
            {"title": "Ayer", "items": [_item(movement_id=MOVEMENT_ID_2), _item(movement_id=MOVEMENT_ID_3)]}
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
            pytest.param({"listData": "synthetic"}, "pages is not a count", id="list-data-not-an-object"),
            pytest.param({"listData": {"groups": []}}, "pages is not a count", id="no-pages"),
            pytest.param({"listData": {"pages": True, "groups": []}}, "pages is not a count", id="pages-bool"),
            pytest.param({"listData": {"pages": -1, "groups": []}}, "pages is not a count", id="pages-negative"),
            pytest.param({"listData": {"pages": 1}}, "groups is not a list", id="no-groups"),
        ],
    )
    def test_missing_node_raises(self, props, message):
        """A changed page raises instead of reading as an empty ledger."""
        with pytest.raises(MercadoPagoPageError, match=message):
            web.parse_movements(props)
