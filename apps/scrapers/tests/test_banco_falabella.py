"""Tests for the Banco Falabella scraper: parsers, ids, and the two legs.

The page text and table rows are written from scratch to mirror the shape of the
dashboard product cards, the card-detail movements table, and the checking and
línea detail views; every identifier, figure, rate, merchant and person is
synthetic. No test launches a browser: the
scraper legs run against a faked `fetch_session`.
"""

import asyncio
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

os.environ.setdefault("BANCO_FALABELLA_RUT", "00.000.000-0")
os.environ.setdefault("BANCO_FALABELLA_PASSWORD", "synthetic-password")

from scrapers.backends import banco_falabella_web as web
from scrapers.backends.banco_falabella_web import (
    BancoFalabellaLoginError,
    FalabellaSession,
    RawBalance,
    RawCheckingBalance,
    RawCheckingMovement,
    RawLineBalance,
)
from scrapers.institutions import banco_falabella as mod
from scrapers.institutions.banco_falabella import (
    BancoFalabellaScraper,
    checking_external_id_for,
    external_id_for,
)

# The dashboard product card innerText (synthetic). Each cupo amount is the line
# BEFORE its label; the Avance "Monto disponible" line is a cash-advance figure
# the parser must not read as a card balance.
_DASHBOARD_CARD_TEXT = """CMR Mastercard
• • • • 1234
$ 2.500.000
Cupo de compras
$ 999.999
Cupo utilizado
Avance
$ 1.500.001
Cupo disponible
$ 1.000.000
Monto disponible
Estado de cuenta
Pagar
Simular / Transferir"""


# The Cuenta Corriente and Línea de Crédito dashboard cards (synthetic), in the
# same amount-then-label layout as the credit card. Every figure is a
# placeholder from the canonical fake set: the línea's three are distinct so a
# swapped label shows, and deliberately do not add up (nothing cross-checks
# them).
_CHECKING_CARD_TEXT = """Cuenta Corriente
00-000-00000-01
$ 1.000.000
Saldo contable
$ 999.999
Saldo disponible
Transf. entre cuentas
Transferir a terceros"""

_LINE_CARD_TEXT = """Línea de Crédito
00-000-00000-02
$ 2.500.000
Cupo autorizado
$ 1.000.000
Saldo utilizado
$ 999.999
Saldo disponible
Aumentar mi línea
Pagar / Configurar"""

# The línea detail's "Detalle" block as innerText renders it: label and value
# with no space between them, then the movements table whose header repeats
# "Tasa diaria" as a column name.
_LINE_DETAIL_TEXT = """Detalle
Titular(es)NOMBRE SINTETICO
Fecha de activación01-01-2025
Tasa diaria1,23 %
Selecciona período
Detalle de movimientos
Fecha\tDescripción\tCargos\tAbonos\tMonto utilizado\tTasa diaria\tIntereses
No existen registros"""


def _cc_row(
    fecha="15-03-2025",
    descripcion="TRANSFERENCIA SINTETICA",
    cargo="",
    abono="$ 1.000.000",
    saldo="$ 2.500.000",
) -> list[str]:
    return [fecha, descripcion, cargo, abono, saldo]


def _checking(**overrides) -> RawCheckingBalance:
    values = dict(
        account_number="00-000-00000-01",
        available=Decimal(999_999),
        accounting=Decimal(1_000_000),
    )
    values.update(overrides)
    return RawCheckingBalance(**values)


def _line(**overrides) -> RawLineBalance:
    values = dict(
        limit=Decimal(2_500_000),
        owed=Decimal(1_000_000),
        available=Decimal(999_999),
        daily_rate_pct=Decimal("1.23"),
    )
    values.update(overrides)
    return RawLineBalance(**values)


def _row(
    fecha="15/03/2025",
    descripcion="COMPRA COMERCIO SINTETICO",
    persona="Titular",
    monto="$ 12.345",
    cuotas="01/01",
    cuota_a_pagar="$ 12.345",
) -> list[str]:
    return [fecha, descripcion, persona, monto, cuotas, cuota_a_pagar]


def _balance(**overrides) -> RawBalance:
    values = dict(
        name="CMR Mastercard",
        last4="1234",
        brand="Mastercard",
        limit=Decimal(2_500_000),
        owed=Decimal(999_999),
        available=Decimal(1_500_001),
        statement_day=None,
        due_day=None,
    )
    values.update(overrides)
    return RawBalance(**values)


def _session(**overrides) -> FalabellaSession:
    movements = web.movements_from_rows(
        [
            _row(),
            _row(
                fecha="08/03/2025",
                descripcion="SERVICIO SINTETICO",
                persona="Adicional",
                monto="$ 6.789",
                cuotas="03/12",
                cuota_a_pagar="$ 2.500",
            ),
        ],
        "billed",
    )
    values = dict(balance=_balance(), movements=movements, warnings=[])
    values.update(overrides)
    return FalabellaSession(**values)


class TestParseClp:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("$1.000.000", Decimal(1_000_000), id="no-space"),
            pytest.param("$ 2.500.000", Decimal(2_500_000), id="spaced"),
            pytest.param("$ 999.999", Decimal(999_999), id="under-a-million"),
            pytest.param("$ 0", Decimal(0), id="zero"),
            pytest.param("$ 1.000,50", Decimal(1_001), id="half-up-rounds-up"),
            pytest.param("$ 1.000,49", Decimal(1_000), id="half-up-rounds-down"),
            pytest.param("-$ 1.000", Decimal(-1_000), id="negative"),
            pytest.param("$ -1.000", Decimal(-1_000), id="negative-after-spaced-marker"),
            pytest.param("$-1.000", Decimal(-1_000), id="negative-after-marker"),
        ],
    )
    def test_parses(self, raw, expected):
        """CLP figures parse to whole pesos, spaced or not, rounded half-up."""
        assert web.parse_clp(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("", id="blank"),
            pytest.param(None, id="none"),
            pytest.param("abc", id="letters"),
            pytest.param("$", id="marker-only"),
            pytest.param("   ", id="spaces"),
        ],
    )
    def test_rejects_junk(self, raw):
        """Anything without a digit to read is unparseable."""
        assert web.parse_clp(raw) is None


class TestParseDmy:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("15/03/2025", date(2025, 3, 15), id="day"),
            pytest.param("08/03/2025", date(2025, 3, 8), id="other-day"),
            pytest.param("15-03-2025", date(2025, 3, 15), id="dashed"),
        ],
    )
    def test_parses(self, raw, expected):
        """A DD/MM/YYYY date parses to the calendar date."""
        assert web.parse_dmy(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "2026-09-24", "32/01/2026", "15-03/2025"])
    def test_rejects_other_shapes(self, raw):
        """Other formats and impossible dates are None."""
        assert web.parse_dmy(raw) is None


class TestParseCuotas:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("01/01", (1, 1), id="single"),
            pytest.param("03/12", (3, 12), id="installment"),
        ],
    )
    def test_parses(self, raw, expected):
        """An installment counter parses to (current, total)."""
        assert web.parse_cuotas(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "1-2", "abc"])
    def test_rejects_junk(self, raw):
        """Anything but NN/NN is None."""
        assert web.parse_cuotas(raw) is None


class TestBalanceFromText:
    def test_reads_the_card(self):
        """The dashboard card yields the three cupos, the last4 and the brand."""
        balance = web.balance_from_text(_DASHBOARD_CARD_TEXT)

        assert balance.limit == Decimal(2_500_000)
        assert balance.owed == Decimal(999_999)
        assert balance.available == Decimal(1_500_001)
        assert balance.last4 == "1234"
        assert balance.brand == "Mastercard"
        assert balance.name == "CMR Mastercard"

    def test_avance_line_is_not_a_balance(self):
        """The Avance "Monto disponible" figure is never read as a card balance."""
        balance = web.balance_from_text(_DASHBOARD_CARD_TEXT)

        assert balance.available == Decimal(1_500_001)
        assert Decimal(1_000_000) not in (balance.limit, balance.owed, balance.available)

    def test_masked_number_with_asterisks(self):
        """The "**** 1234" masked form yields the same last4."""
        text = _DASHBOARD_CARD_TEXT.replace("• • • • 1234", "**** 1234")

        assert web.balance_from_text(text).last4 == "1234"

    def test_missing_figure_is_tolerated(self):
        """A card missing one label leaves that field None, keeping the others."""
        text = _DASHBOARD_CARD_TEXT.replace("$ 999.999\nCupo utilizado\n", "")

        balance = web.balance_from_text(text)

        assert balance.owed is None
        assert balance.limit == Decimal(2_500_000)
        assert balance.available == Decimal(1_500_001)


class TestMovementsFromRows:
    def test_happy_path(self):
        """Two rows become two movements with the parsed fields."""
        movements = web.movements_from_rows([_row(), _row(fecha="08/03/2025")], "billed")

        assert len(movements) == 2
        assert movements[0].transaction_date == date(2025, 3, 15)
        assert movements[0].persona == "Titular"
        assert movements[0].currency == "CLP"
        assert (movements[0].cuotas_current, movements[0].cuotas_total) == (1, 1)

    def test_charge_is_negative(self):
        """A plain purchase is money out."""
        movement = web.movements_from_rows([_row()], "billed")[0]

        assert movement.amount == Decimal(-12_345)

    @pytest.mark.parametrize(
        "descripcion",
        [
            pytest.param("PAGO PESOS SINTETICO", id="pago"),
            pytest.param("PAGOS RECIBIDOS SINTETICO", id="pagos"),
            pytest.param("ABONO SINTETICO", id="abono"),
            pytest.param("DEVOLUCION SINTETICA", id="devolucion"),
            pytest.param("REVERSA COMPRA SINTETICA", id="reversa"),
            pytest.param("ANULACION SINTETICA", id="anulacion"),
        ],
    )
    def test_payment_is_positive(self, descripcion):
        """A payment, abono, refund or reversal flips the amount positive."""
        movement = web.movements_from_rows([_row(descripcion=descripcion)], "billed")[0]

        assert movement.amount == Decimal(12_345)

    @pytest.mark.parametrize(
        "descripcion",
        [
            pytest.param("MERCADOPAGO*COMERCIO SINTETICO", id="pago-inside-merchant"),
            pytest.param("GRANULADOS SINTETICOS SPA", id="anula-inside-word"),
            pytest.param("ABONOSINTETICO SPA", id="abono-glued-to-a-name"),
        ],
    )
    def test_payment_word_inside_a_name_stays_a_charge(self, descripcion):
        """A payment word embedded in a merchant name never flips a charge."""
        movement = web.movements_from_rows([_row(descripcion=descripcion)], "billed")[0]

        assert movement.amount == Decimal(-12_345)

    @pytest.mark.parametrize(
        ("descripcion", "monto"),
        [
            pytest.param("NOTA CREDITO SINTETICA", "$-12.345", id="signed-credit"),
            pytest.param("PAGO PESOS SINTETICO", "-$ 12.345", id="signed-payment"),
            pytest.param("PAGO TARJETA SINTETICA", "-$12.345", id="portal-card-payment"),
        ],
    )
    def test_printed_sign_is_a_credit(self, descripcion, monto):
        """A "-" the portal prints marks a credit, whatever the description."""
        movement = web.movements_from_rows(
            [_row(descripcion=descripcion, monto=monto)], "billed"
        )[0]

        assert movement.amount == Decimal(12_345)

    def test_amount_is_monto_total_not_cuota(self):
        """The amount is the full Monto total, not the monthly Cuota a pagar."""
        movement = web.movements_from_rows(
            [_row(monto="$ 6.789", cuota_a_pagar="$ 2.500")], "billed"
        )[0]

        assert movement.amount == Decimal(-6_789)

    @pytest.mark.parametrize(
        "row",
        [
            pytest.param(_row(fecha="no es fecha"), id="bad-date"),
            pytest.param(_row(monto="sin monto"), id="bad-amount"),
            pytest.param(["15/03/2025"], id="too-few-cells"),
        ],
    )
    def test_malformed_row_is_skipped_with_a_warning(self, row, caplog):
        """A row without a usable date or amount is dropped and logged."""
        with caplog.at_level("WARNING"):
            movements = web.movements_from_rows([row, _row()], "unbilled")

        assert len(movements) == 1
        assert "skip" in caplog.text.lower()


class TestExternalId:
    def _movement(self, **overrides):
        return web.movements_from_rows([_row(**overrides)], "billed")[0]

    def test_prefix_and_length(self):
        """Every id is the bfal_ prefix plus a 16-char hash."""
        external_id = external_id_for(self._movement())

        assert external_id.startswith("bfal_")
        assert len(external_id) == len("bfal_") + 16

    def test_stable(self):
        """The same movement yields the same id on every call."""
        assert external_id_for(self._movement()) == external_id_for(self._movement())

    def test_distinct_movements_distinct_ids(self):
        """A different date or amount gives a different id."""
        a = external_id_for(self._movement())
        b = external_id_for(self._movement(fecha="08/03/2025"))
        c = external_id_for(self._movement(monto="$ 6.789", cuota_a_pagar="$ 6.789"))

        assert a != b
        assert a != c

    def test_description_rewrite_keeps_the_id(self):
        """Rewording the description never re-keys a movement."""
        before = self._movement()
        after = self._movement(descripcion="COMERCIO SINTETICO CON OTRO NOMBRE")

        assert external_id_for(before) == external_id_for(after)

    def test_cuotas_counter_change_keeps_the_id(self):
        """The installment counter changing between cycles never re-keys it."""
        before = self._movement(cuotas="01/12")
        after = self._movement(cuotas="03/12")

        assert external_id_for(before) == external_id_for(after)

    def test_same_id_across_the_two_legs(self):
        """A purchase seen as unbilled and later as billed keeps one id."""
        unbilled = web.movements_from_rows([_row()], "unbilled")[0]
        billed = web.movements_from_rows(
            [_row(descripcion="COMERCIO SINTETICO (FACTURADO)", cuotas="02/12", cuota_a_pagar="$ 2.000")],
            "billed",
        )[0]

        assert external_id_for(unbilled) == external_id_for(billed)


class TestCheckingFromText:
    def test_reads_the_card(self):
        """Saldo disponible is the balance, Saldo contable the accounting one."""
        checking = web.checking_from_text(_CHECKING_CARD_TEXT)

        assert checking.available == Decimal(999_999)
        assert checking.accounting == Decimal(1_000_000)
        assert checking.account_number == "00-000-00000-01"

    def test_missing_figure_is_tolerated(self):
        """A card without its contable line still yields the available balance."""
        text = _CHECKING_CARD_TEXT.replace("$ 1.000.000\nSaldo contable\n", "")

        checking = web.checking_from_text(text)

        assert checking.available == Decimal(999_999)
        assert checking.accounting is None

    def test_amount_after_the_name_is_not_an_account_number(self):
        """Only a digit line that is not an amount is taken as the number."""
        checking = web.checking_from_text("Cuenta Corriente\n$ 999.999\nSaldo disponible")

        assert checking.account_number is None
        assert checking.available == Decimal(999_999)


class TestLineFromText:
    def test_reads_the_card(self):
        """Cupo autorizado, Saldo utilizado and Saldo disponible map to the line."""
        line = web.line_from_text(_LINE_CARD_TEXT)

        assert (line.limit, line.owed, line.available) == (
            Decimal(2_500_000),
            Decimal(1_000_000),
            Decimal(999_999),
        )
        assert line.daily_rate_pct is None


class TestDailyRate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("1.23 %", Decimal("1.23"), id="dot"),
            pytest.param("1,23 %", Decimal("1.23"), id="comma"),
            pytest.param("1.23", Decimal("1.23"), id="bare"),
        ],
    )
    def test_parse_percent(self, raw, expected):
        """The rate field's text parses with either decimal separator."""
        assert web.parse_percent(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "abc %", "1,23 % diario"])
    def test_parse_percent_rejects_junk(self, raw):
        """Anything but a lone percent figure is None."""
        assert web.parse_percent(raw) is None

    def test_from_the_detail_text(self):
        """The label's figure is read, not the movements column header."""
        assert web.daily_rate_from_text(_LINE_DETAIL_TEXT) == Decimal("1.23")

    def test_column_header_alone_is_no_rate(self):
        """A page with only the column header yields None."""
        text = "Fecha\tDescripción\tCargos\tAbonos\tMonto utilizado\tTasa diaria\tIntereses"

        assert web.daily_rate_from_text(text) is None


class TestCheckingMovementsFromRows:
    def test_abono_is_money_in(self):
        """An Abono row is a positive amount with its running balance."""
        (movement,) = web.checking_movements_from_rows([_cc_row()])

        assert movement.transaction_date == date(2025, 3, 15)
        assert movement.description == "TRANSFERENCIA SINTETICA"
        assert movement.amount == Decimal(1_000_000)
        assert movement.balance == Decimal(2_500_000)
        assert movement.currency == "CLP"

    def test_cargo_is_money_out(self):
        """A Cargo row is a negative amount."""
        (movement,) = web.checking_movements_from_rows(
            [_cc_row(cargo="$ 999.999", abono="", saldo="$ 1.000.000")]
        )

        assert movement.amount == Decimal(-999_999)

    def test_blank_saldo_is_none(self):
        """A missing running balance does not drop the movement."""
        (movement,) = web.checking_movements_from_rows([_cc_row(saldo="")])

        assert movement.balance is None

    def test_filler_and_no_records_rows_are_dropped_silently(self, caplog):
        """The table's single-cell rows are not movements and not warnings."""
        rows = [[""], ["No existen registros"], _cc_row()]

        movements = web.checking_movements_from_rows(rows)

        assert len(movements) == 1
        assert "skipped" not in caplog.text

    @pytest.mark.parametrize(
        "row",
        [
            pytest.param(_cc_row(cargo="", abono=""), id="no-amount"),
            pytest.param(_cc_row(cargo="$ 999.999", abono="$ 1.000.000"), id="both-amounts"),
            pytest.param(_cc_row(fecha="sin fecha"), id="bad-date"),
        ],
    )
    def test_unusable_row_is_skipped_with_a_warning(self, row, caplog):
        """An unusable row is dropped and logged without its figures."""
        movements = web.checking_movements_from_rows([row, _cc_row()])

        assert len(movements) == 1
        assert "skipped 1 unusable checking rows" in caplog.text
        assert "999.999" not in caplog.text and "1.000.000" not in caplog.text


def _cc_movement(**overrides) -> RawCheckingMovement:
    values = dict(
        transaction_date=date(2025, 3, 15),
        description="TRANSFERENCIA SINTETICA",
        amount=Decimal(1_000_000),
        currency="CLP",
        balance=Decimal(2_500_000),
    )
    values.update(overrides)
    return RawCheckingMovement(**values)


class TestCheckingExternalId:
    def test_prefix_and_length(self):
        """bfal_cc_ plus 16 hex characters."""
        external_id = checking_external_id_for(_cc_movement())

        assert external_id.startswith("bfal_cc_")
        assert len(external_id) == len("bfal_cc_") + 16

    def test_stable_and_description_free(self):
        """The same movement keys the same, whatever its description says."""
        assert checking_external_id_for(_cc_movement()) == checking_external_id_for(
            _cc_movement(description="TRANSF. SINTETICA ABREVIADA")
        )

    def test_running_balance_tells_same_day_twins_apart(self):
        """Two equal same-day transfers differ by the balance after each."""
        first = _cc_movement(balance=Decimal(1_000_000))
        second = _cc_movement(balance=Decimal(2_500_000))

        assert checking_external_id_for(first) != checking_external_id_for(second)

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"amount": Decimal(-1_000_000)}, id="direction"),
            pytest.param({"transaction_date": date(2025, 3, 16)}, id="date"),
            pytest.param({"balance": None}, id="blank-balance"),
        ],
    )
    def test_identity_fields_change_the_id(self, change):
        """Date, signed amount and balance are all part of the key."""
        assert checking_external_id_for(_cc_movement()) != checking_external_id_for(
            _cc_movement(**change)
        )


class TestPickGrid:
    def test_picks_by_product_name(self):
        """The grid whose name line matches is chosen, accents ignored."""
        texts = ["Cuenta Vista\n$ 999.999\nSaldo disponible", _CHECKING_CARD_TEXT]

        assert web._pick_grid(texts, web._CHECKING_NAME) == _CHECKING_CARD_TEXT
        assert web._pick_grid([_LINE_CARD_TEXT], web._LINE_NAME) == _LINE_CARD_TEXT

    @pytest.mark.parametrize("texts", [None, [], ["Cuenta Vista"], "Cuenta Corriente"])
    def test_none_when_absent(self, texts):
        """No listed product of that name (or a malformed read) is None."""
        assert web._pick_grid(texts, web._CHECKING_NAME) is None


class _GridsPage:
    """A dashboard whose product grids render after a few polls."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.waits = 0

    def evaluate(self, script, *args):
        assert script == web._PRODUCT_GRIDS_JS
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def wait_for_timeout(self, ms):
        self.waits += 1


class TestReadOtherProducts:
    def test_waits_for_the_figures(self, monkeypatch):
        """A card listed without its figures yet is polled until they land."""
        page = _GridsPage(
            [
                {"accounts": ["Cuenta Corriente\n00-000-00000-01"], "agreements": []},
                {"accounts": [_CHECKING_CARD_TEXT], "agreements": [_LINE_CARD_TEXT]},
            ]
        )

        checking, line = web._read_other_products(page)

        assert checking.available == Decimal(999_999)
        assert line.available == Decimal(999_999)
        assert page.waits == 1

    def test_client_without_those_products_gets_none(self, monkeypatch):
        """Nothing listed is None for both, after the bounded backstop."""
        clock = iter(range(0, 1000, 5))
        monkeypatch.setattr(web.time, "monotonic", lambda: next(clock))
        page = _GridsPage([{"accounts": [], "agreements": []}])

        assert web._read_other_products(page) == (None, None)


class _LegacyViewPage:
    """A checking or línea detail answering scripted reads."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.calls = []

    def evaluate(self, script, *args):
        self.calls.append(script)
        if script in (web._IDLE_MODAL_JS,):
            return False
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def wait_for_timeout(self, ms):
        pass


class TestReadCheckingRows:
    def test_rows_once_the_table_renders(self):
        """The table's body rows come back as soon as it exists."""
        page = _LegacyViewPage(
            [{"found": False, "failed": False, "rows": []}, {"found": True, "failed": False, "rows": [_cc_row()]}]
        )

        assert web._read_checking_rows(page) == [_cc_row()]

    def test_table_beats_the_error_banner(self):
        """A rendered table counts even when the page also shows the error view."""
        page = _LegacyViewPage([{"found": True, "failed": True, "rows": [["No existen registros"]]}])

        assert web._read_checking_rows(page) == [["No existen registros"]]

    def test_error_view_without_a_table_is_none(self):
        """The legacy error view with no table is a failed surface."""
        page = _LegacyViewPage([{"found": False, "failed": True, "rows": []}])

        assert web._read_checking_rows(page) is None

    def test_never_rendered_is_none(self, monkeypatch):
        """A view that never renders runs out its bounded wait."""
        clock = iter(range(0, 1000, 5))
        monkeypatch.setattr(web.time, "monotonic", lambda: next(clock))
        page = _LegacyViewPage([{"found": False, "failed": False, "rows": []}])

        assert web._read_checking_rows(page) is None


class TestReadLineRate:
    def test_field_text_is_read(self):
        """The rate field's own text is parsed first."""
        page = _LegacyViewPage([{"rate": "1,23 %", "text": ""}])

        assert web._read_line_rate(page) == Decimal("1.23")

    def test_falls_back_to_the_page_text(self):
        """Without the field id the labelled figure in the page text is used."""
        page = _LegacyViewPage([{"rate": None, "text": _LINE_DETAIL_TEXT}])

        assert web._read_line_rate(page) == Decimal("1.23")

    def test_zero_rate_is_a_rate(self):
        """A "0,00 %" field is a zero rate, not a miss that falls through."""
        page = _LegacyViewPage([{"rate": "0,00 %", "text": ""}])

        assert web._read_line_rate(page) == Decimal("0")


class TestScraperLegs:
    def setup_method(self):
        self.scraper = BancoFalabellaScraper()

    def _install(self, monkeypatch, result=None, exc=None):
        calls = {"n": 0, "headless": []}

        async def fake_fetch_session(rut, password, *, headless=True):
            calls["n"] += 1
            calls["headless"].append(headless)
            if exc is not None:
                raise exc
            return result

        monkeypatch.setattr(mod, "fetch_session", fake_fetch_session)
        return calls

    def test_one_drive_feeds_both_legs(self, monkeypatch):
        """Transactions then products log in exactly once."""
        calls = self._install(monkeypatch, result=_session())

        transactions = asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert len(transactions) == 2
        assert len(products.products) == 1

    def test_product_is_the_singleton_credit_card(self, monkeypatch):
        """The card is a singleton CLP credit_card with its cupos and last4."""
        self._install(monkeypatch, result=_session())

        asyncio.run(self.scraper.scrape_transactions())
        product = asyncio.run(self.scraper.scrape_products()).products[0]

        assert (product.institution, product.kind, product.currency) == (
            "banco_falabella",
            "credit_card",
            "CLP",
        )
        assert product.external_ref is None
        assert product.name == "CMR Mastercard"
        assert product.metrics.available == 1_500_001
        assert product.metrics.limit == 2_500_000
        assert product.metrics.owed == 999_999
        assert product.attributes.last4 == "1234"
        assert product.attributes.brand == "Mastercard"

    def test_transactions_land_on_the_card(self, monkeypatch):
        """Every transaction matches the singleton and is signed."""
        self._install(monkeypatch, result=_session())

        charge, _ = asyncio.run(self.scraper.scrape_transactions())

        assert (charge.institution, charge.product_kind, charge.currency) == (
            "banco_falabella",
            "credit_card",
            "CLP",
        )
        assert charge.amount == Decimal(-12_345)
        assert charge.description == "COMPRA COMERCIO SINTETICO"

    def test_scheduled_month_on_every_transaction(self, monkeypatch):
        """scheduled_month is the first of each movement's occurrence month."""
        self._install(monkeypatch, result=_session())

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert all(t.scheduled_month is not None for t in transactions)
        assert transactions[0].scheduled_month == date(2025, 3, 1)

    def test_dedup_across_legs(self, monkeypatch):
        """A purchase in both the unbilled and billed tabs is written once."""
        movements = web.movements_from_rows([_row()], "unbilled") + web.movements_from_rows(
            [_row(descripcion="COMERCIO SINTETICO (FACTURADO)", cuotas="02/12")], "billed"
        )
        self._install(monkeypatch, result=_session(movements=movements))

        transactions = asyncio.run(self.scraper.scrape_transactions())

        assert len(transactions) == 1

    def test_products_alone_drives_once(self, monkeypatch):
        """Called on its own, the products leg logs in itself."""
        calls = self._install(monkeypatch, result=_session(warnings=["w"]))

        products = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert products.warnings == ["w"]

    def test_cache_resets_between_cycles(self, monkeypatch):
        """Each cycle logs in once, never reusing the previous cycle's data."""
        calls = self._install(monkeypatch, result=_session())

        for _ in range(2):
            asyncio.run(self.scraper.scrape_transactions())
            asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 2

    def test_failed_login_not_retried_by_products(self, monkeypatch):
        """The products leg re-raises the cached error without a second login."""
        calls = self._install(monkeypatch, exc=BancoFalabellaLoginError("login"))

        with pytest.raises(BancoFalabellaLoginError):
            asyncio.run(self.scraper.scrape_transactions())
        with pytest.raises(BancoFalabellaLoginError):
            asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1

    def test_partial_coverage_reaches_products_warnings(self, monkeypatch):
        """A surface that failed in the transactions drive surfaces as a warning."""
        warning = "Banco Falabella: unbilled movements surface never loaded"
        self._install(monkeypatch, result=_session(movements=[], warnings=[warning]))

        transactions = asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products())

        assert transactions == []
        assert len(products.products) == 1
        assert products.warnings == [warning]

    def test_three_products_each_a_singleton_of_its_kind(self, monkeypatch):
        """Card, checking and línea are CLP singletons of three distinct kinds."""
        self._install(monkeypatch, result=_session(checking=_checking(), line=_line()))

        asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products()).products

        assert [(p.kind, p.currency, p.external_ref) for p in products] == [
            ("credit_card", "CLP", None),
            ("checking", "CLP", None),
            ("line_of_credit", "CLP", None),
        ]

    def test_card_product_unchanged_by_the_new_products(self, monkeypatch):
        """The CMR card emits exactly as before, so it keeps matching its stored row."""
        self._install(monkeypatch, result=_session())
        alone = asyncio.run(self.scraper.scrape_products()).products
        self._install(monkeypatch, result=_session(checking=_checking(), line=_line()))
        together = asyncio.run(self.scraper.scrape_products()).products

        assert alone == [together[0]]

    def test_checking_product(self, monkeypatch):
        """The checking balance, accounting balance and account number map through."""
        self._install(monkeypatch, result=_session(checking=_checking()))

        product = asyncio.run(self.scraper.scrape_products()).products[1]

        assert product.kind == "checking"
        assert product.name == "Cuenta Corriente"
        assert product.metrics.balance == 999_999
        assert product.metrics.accounting_balance == 1_000_000
        assert product.attributes.account_number == "00-000-00000-01"

    def test_line_product(self, monkeypatch):
        """The línea cupos map through and the daily rate stays as quoted."""
        self._install(monkeypatch, result=_session(line=_line()))

        product = asyncio.run(self.scraper.scrape_products()).products[1]

        assert product.kind == "line_of_credit"
        assert product.name == "Línea de Crédito"
        assert (product.metrics.available, product.metrics.limit, product.metrics.owed) == (
            999_999,
            2_500_000,
            1_000_000,
        )
        assert product.attributes.daily_interest_rate_pct == 1.23
        assert product.attributes.interest_rate_pct is None

    def test_line_without_its_rate_still_emits(self, monkeypatch):
        """A línea whose detail was not read keeps its balances; the rate is None."""
        self._install(monkeypatch, result=_session(line=_line(daily_rate_pct=None)))

        product = asyncio.run(self.scraper.scrape_products()).products[1]

        assert product.metrics.available == 999_999
        assert product.attributes.daily_interest_rate_pct is None

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"checking": _checking(available=None)}, id="checking"),
            pytest.param({"line": _line(available=None)}, id="line"),
        ],
    )
    def test_product_without_available_is_not_emitted(self, monkeypatch, overrides):
        """A checking or línea card without its headline figure emits nothing."""
        self._install(monkeypatch, result=_session(**overrides))

        products = asyncio.run(self.scraper.scrape_products()).products

        assert [p.kind for p in products] == ["credit_card"]

    def test_checking_transactions_land_on_the_checking_product(self, monkeypatch):
        """Checking movements are signed checking transactions; card ones stay on the card."""
        checking_movements = web.checking_movements_from_rows(
            [_cc_row(), _cc_row(cargo="$ 999.999", abono="", saldo="$ 1.234.567")]
        )
        self._install(monkeypatch, result=_session(checking_movements=checking_movements))

        transactions = asyncio.run(self.scraper.scrape_transactions())

        kinds = [t.product_kind for t in transactions]
        assert kinds == ["credit_card", "credit_card", "checking", "checking"]
        abono, cargo = transactions[2:]
        assert (abono.currency, abono.amount, abono.product_external_ref) == ("CLP", Decimal(1_000_000), None)
        assert cargo.amount == Decimal(-999_999)
        assert abono.external_id.startswith("bfal_cc_")
        assert abono.scheduled_month == date(2025, 3, 1)
        assert abono.accounting_date is None

    def test_no_balance_emits_no_product(self, monkeypatch):
        """Without a parseable available cupo the products leg is empty, with a warning."""
        warning = "Banco Falabella: the card available cupo did not parse"
        self._install(monkeypatch, result=_session(balance=None, movements=[], warnings=[warning]))

        products = asyncio.run(self.scraper.scrape_products())

        assert products.products == []
        assert products.warnings == [warning]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(None, True, id="unset"),
            pytest.param("1", True, id="one"),
            pytest.param("0", False, id="zero"),
            pytest.param("false", False, id="false"),
        ],
    )
    def test_headless_env(self, monkeypatch, value, expected):
        """BANCO_FALABELLA_HEADLESS=0 turns the browser visible; headless otherwise."""
        if value is None:
            monkeypatch.delenv("BANCO_FALABELLA_HEADLESS", raising=False)
        else:
            monkeypatch.setenv("BANCO_FALABELLA_HEADLESS", value)
        calls = self._install(monkeypatch, result=_session())

        asyncio.run(BancoFalabellaScraper().scrape_transactions())

        assert calls["headless"] == [expected]


class _OkBox:
    """A login control that is always visible and clickable."""

    def __init__(self):
        self.first = self

    def wait_for(self, **kwargs):
        pass

    def click(self, timeout=0):
        pass

    def count(self):
        return 1

    def press_sequentially(self, text, delay=0):
        pass


class _FailBox(_OkBox):
    def press_sequentially(self, text, delay=0):
        raise RuntimeError(f"failed typing {text!r}")


class _LoginFakePage:
    """A public page whose clave input fails typing (to check message hygiene)."""

    url = web.HOME_URL

    def goto(self, *args, **kwargs):
        pass

    def get_by_role(self, role, name=None, exact=False):
        pattern = (getattr(name, "pattern", None) or "").lower()
        if role == "textbox" and "clave" in pattern:
            return _FailBox()
        return _OkBox()

    def get_by_text(self, *args, **kwargs):
        return _OkBox()

    def locator(self, *args, **kwargs):
        return _OkBox()


class TestSecretsNeverSurface:
    def test_login_failure_drops_the_typed_credentials(self):
        """A Playwright error quoting the clave or RUT never reaches the run record."""
        with pytest.raises(BancoFalabellaLoginError) as info:
            web._login(_LoginFakePage(), "00.000.000-0", "synthetic-password")

        assert "synthetic-password" not in str(info.value)
        assert "00.000.000-0" not in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__


class TestUserAgent:
    @pytest.mark.parametrize(
        ("platform", "os_token"),
        [
            ("darwin", "Macintosh; Intel Mac OS X 10_15_7"),
            ("linux", "X11; Linux x86_64"),
            ("win32", "X11; Linux x86_64"),
        ],
    )
    def test_matches_running_engine_and_host(self, platform, os_token):
        """The UA keeps the engine's major version and host OS, without "Headless"."""
        ua = web.user_agent_for("141.0.7390.37", platform)

        assert f"({os_token})" in ua
        assert "Chrome/141.0.0.0" in ua
        assert "Headless" not in ua


class _NoMatch:
    """A locator that matches nothing, so `_click_text` falls back to its JS."""

    @property
    def first(self):
        return self

    def count(self):
        return 0


class _SurfacePage:
    """A page with scripted movements-table reads, on a fake clock (no browser)."""

    def __init__(self, reads, empty_state=False, backdrop=False, retry_shown=False, retry_lands=True):
        self.reads = list(reads)
        self.empty_state = empty_state
        self.backdrop = backdrop
        self.retry_shown = retry_shown
        self.retry_lands = retry_lands
        self.clock = 0.0
        self.scripts = []
        self.clicks = []

    def evaluate(self, script, *args):
        self.scripts.append(script)
        if script is web._MOVEMENTS_ROWS_JS:
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if script is web._EMPTY_STATE_JS:
            return self.empty_state
        if script is web._RETRY_SHOWN_JS:
            return self.retry_shown
        if script is web._DISMISS_OVERLAY_JS:
            return self.backdrop
        if script is web._CLICK_TEXT_JS:
            self.clicks.append(args[0])
        if script is web._CLICK_RETRY_JS:
            self.clicks.append("Reintentar")
            return self.retry_lands
        return False

    def get_by_text(self, *args, **kwargs):
        return _NoMatch()

    def get_by_role(self, *args, **kwargs):
        return _NoMatch()

    def wait_for_timeout(self, ms):
        self.clock += ms / 1000


def _table(*rows):
    return {"found": True, "rows": [list(row) for row in rows]}


_NO_TABLE = {"found": False, "rows": []}


class _ScriptedFeed:
    """A movements feed whose load state is scripted per poll (the last one sticks)."""

    def __init__(self, states):
        self.states = list(states)

    def mark(self):
        return 0

    def state(self, since):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class TestReadSurface:
    def _read(self, monkeypatch, page, states, previous=None):
        monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: page.clock))
        return web._read_surface(page, web._BILLED_TAB, _ScriptedFeed(states), previous)

    def test_rows_are_returned(self, monkeypatch):
        """A table with rows is read on the first poll."""
        page = _SurfacePage([_table(_row())])

        assert self._read(monkeypatch, page, [None]) == [_row()]

    def test_late_rows_beat_an_early_empty_table(self, monkeypatch):
        """A header-only table that fills in a poll later is not filed as empty."""
        page = _SurfacePage([_table(), _table(_row())])

        assert self._read(monkeypatch, page, [web._LOAD_DONE]) == [_row()]

    def test_empty_table_counts_once_its_load_answered(self, monkeypatch):
        """A header-only table after the load answered, held, is a confirmed empty tab."""
        page = _SurfacePage([_table()])

        result = self._read(monkeypatch, page, [web._LOAD_DONE])

        assert result == []
        assert page.scripts.count(web._MOVEMENTS_ROWS_JS) == web._EMPTY_CONFIRM_POLLS

    def test_empty_state_message_counts_once_its_load_answered(self, monkeypatch):
        """No table plus the component's "no movements" message is an empty tab."""
        page = _SurfacePage([_NO_TABLE], empty_state=True)

        assert self._read(monkeypatch, page, [web._LOAD_DONE]) == []

    @pytest.mark.parametrize(
        "previous",
        [
            pytest.param(None, id="first-load"),
            pytest.param([], id="stale-empty-table-from-the-other-tab"),
        ],
    )
    def test_empty_looking_table_during_the_load_is_not_empty(self, monkeypatch, previous):
        """Header-only table and "no movements" art while the load runs wait for its rows."""
        polls = web._EMPTY_CONFIRM_POLLS * 4
        page = _SurfacePage([_table()] * polls + [_table(_row())], empty_state=True)

        result = self._read(monkeypatch, page, [None] * polls, previous=previous)

        assert result == [_row()]

    def test_failed_load_is_retried_not_filed_empty(self, monkeypatch):
        """The header-only table a failed load leaves is retried via Reintentar."""
        page = _SurfacePage([_table(), _table(), _table(_row())], retry_shown=True)

        result = self._read(monkeypatch, page, [web._LOAD_FAILED, None])

        assert result == [_row()]
        assert page.clicks.count("Reintentar") == 1

    def test_load_that_keeps_failing_is_none(self, monkeypatch):
        """A tab whose every load fails is None (a warning upstream), never empty."""
        page = _SurfacePage([_table()], retry_shown=True)

        result = self._read(monkeypatch, page, [web._LOAD_FAILED])

        assert result is None
        assert page.clicks.count("Reintentar") == web._FAILED_LOAD_RETRIES

    def test_failed_step_without_an_error_view_keeps_waiting(self, monkeypatch):
        """A failed step the component rides out is neither retried nor empty."""
        page = _SurfacePage([_table()] * 3 + [_table(_row())])

        result = self._read(monkeypatch, page, [web._LOAD_FAILED] * 3 + [None])

        assert result == [_row()]
        assert "Reintentar" not in page.clicks

    def test_error_view_behind_a_2xx_is_not_empty(self, monkeypatch):
        """A header-only table next to Reintentar is a failure even if the call said 2xx."""
        page = _SurfacePage([_table()], retry_shown=True)

        result = self._read(monkeypatch, page, [web._LOAD_DONE])

        assert result is None

    def test_hung_load_runs_out_its_budgets(self, monkeypatch):
        """A load that never answers is retried once per budget, then None."""
        page = _SurfacePage([_table()])

        result = self._read(monkeypatch, page, [None])

        assert result is None
        assert page.clicks.count("Reintentar") == len(web._RENDER_TIMEOUTS_MS) - 1

    def test_previous_tab_rows_are_ignored(self, monkeypatch):
        """Rows still showing from the other tab are skipped until the new ones load."""
        stale = _row(descripcion="COMERCIO SINTETICO ANTERIOR")
        page = _SurfacePage([_table(stale), _table(stale), _table(_row())])

        assert self._read(monkeypatch, page, [None], previous=[stale]) == [_row()]

    def test_never_rendered_is_none(self, monkeypatch):
        """A surface with no table and no empty message is None (a warning upstream)."""
        page = _SurfacePage([_NO_TABLE])

        assert self._read(monkeypatch, page, [None]) is None


class _Request:
    """A Playwright request stand-in (hashable by identity, like the real one)."""

    def __init__(self, path, method="POST"):
        self.url = f"https://bff.example.invalid{path}"
        self.method = method


_CARD_CALL = "/movement-cc/v2.0/credit-card"
_MOVEMENTS_CALL = "/movement-cc/v2.0/credit-card/movements"


def _answer(feed, path, status=200, method="POST"):
    request = _Request(path, method)
    feed.on_request(request)
    feed.on_response(SimpleNamespace(request=request, status=status))
    feed.on_request_finished(request)


class _ChainPage(_SurfacePage):
    """A surface page whose Reintentar click starts a new load on the real feed.

    The error view lingers until the next poll; then the new load's request
    goes out, and its 2xx answer on the poll after, as the component's fetch
    does after a click.
    """

    def __init__(self, reads, feed, retry_shown=False):
        super().__init__(reads, retry_shown=retry_shown)
        self.feed = feed
        self.scheduled = []

    def evaluate(self, script, *args):
        if script is web._CLICK_RETRY_JS:
            request = _Request(_MOVEMENTS_CALL)
            self.scheduled += [
                lambda: (setattr(self, "retry_shown", False), self.feed.on_request(request)),
                lambda: (
                    self.feed.on_response(SimpleNamespace(request=request, status=200)),
                    self.feed.on_request_finished(request),
                ),
            ]
        return super().evaluate(script, *args)

    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        if self.scheduled:
            self.scheduled.pop(0)()


class TestReadSurfaceWithTheFeed:
    def _read(self, monkeypatch, page, feed, since=None):
        monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: page.clock))
        return web._read_surface(page, web._UNBILLED_TAB, feed, since=since)

    def test_retried_load_is_judged_on_its_own_answer(self, monkeypatch):
        """After Reintentar, the earlier failure and its lingering view no longer count."""
        feed = web._MovementsFeed()
        since = feed.mark()
        _answer(feed, _MOVEMENTS_CALL, 500)
        page = _ChainPage([_table()], feed, retry_shown=True)

        result = self._read(monkeypatch, page, feed, since=since)

        assert result == []
        assert page.clicks.count("Reintentar") == 1

    def test_load_answered_before_the_read_counts_from_the_callers_mark(self, monkeypatch):
        """The unbilled load that answered while the card detail opened is not missed."""
        feed = web._MovementsFeed()
        since = feed.mark()
        _answer(feed, _MOVEMENTS_CALL)
        page = _ChainPage([_table()], feed)

        result = self._read(monkeypatch, page, feed, since=since)

        assert result == []
        assert "Reintentar" not in page.clicks

    def test_slow_load_still_counts_when_no_reintentar_showed(self, monkeypatch):
        """A load that outlives its budget, with no Reintentar to click, may still answer."""
        feed = web._MovementsFeed()
        since = feed.mark()
        slow = _Request(_MOVEMENTS_CALL)
        feed.on_request(slow)
        page = _SlowAnswerPage([_table()], feed, slow, answer_at_s=15)

        result = self._read(monkeypatch, page, feed, since=since)

        assert result == []


class _SlowAnswerPage(_SurfacePage):
    """A surface page with no Reintentar control whose pending call answers 2xx late."""

    def __init__(self, reads, feed, request, answer_at_s):
        super().__init__(reads, retry_lands=False)
        self.feed = feed
        self.request = request
        self.answer_at_s = answer_at_s

    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        if self.request is not None and self.clock >= self.answer_at_s:
            self.feed.on_response(SimpleNamespace(request=self.request, status=200))
            self.feed.on_request_finished(self.request)
            self.request = None


class TestMovementsFeed:
    def test_load_is_done_once_movements_answers(self):
        """A chain that ends in a 2xx /movements call is a finished load."""
        feed = web._MovementsFeed()

        _answer(feed, _CARD_CALL)
        _answer(feed, _MOVEMENTS_CALL)

        assert feed.state(0) == web._LOAD_DONE

    def test_load_is_under_way_mid_chain(self):
        """A settled earlier step, with nothing pending, is no outcome yet."""
        feed = web._MovementsFeed()

        _answer(feed, _CARD_CALL)

        assert feed.state(0) is None

    def test_load_is_under_way_while_a_call_is_pending(self):
        """A /movements call that has not finished yet is no outcome yet."""
        feed = web._MovementsFeed()
        request = _Request(_MOVEMENTS_CALL)
        feed.on_request(request)

        feed.on_response(SimpleNamespace(request=request, status=200))

        assert feed.state(0) is None

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            pytest.param(_CARD_CALL, 500, id="card-call-500"),
            pytest.param(_MOVEMENTS_CALL, 500, id="movements-call-500"),
            pytest.param(_MOVEMENTS_CALL, 403, id="movements-call-403"),
        ],
    )
    def test_error_status_fails_the_load(self, path, status):
        """Any call of the chain answering an error status fails the load."""
        feed = web._MovementsFeed()

        _answer(feed, path, status)

        assert feed.state(0) == web._LOAD_FAILED

    def test_network_failure_fails_the_load(self):
        """A call that never got a response fails the load."""
        feed = web._MovementsFeed()
        request = _Request(_MOVEMENTS_CALL)
        feed.on_request(request)

        feed.on_request_failed(request)

        assert feed.state(0) == web._LOAD_FAILED

    def test_body_failure_after_a_2xx_status_fails_the_load(self):
        """A call whose body dies after a 2xx status line is a failure, not a finished load."""
        feed = web._MovementsFeed()
        request = _Request(_MOVEMENTS_CALL)
        feed.on_request(request)
        feed.on_response(SimpleNamespace(request=request, status=200))

        feed.on_request_failed(request)

        assert feed.state(0) == web._LOAD_FAILED

    def test_mark_skips_earlier_loads(self):
        """Only calls started after the mark count."""
        feed = web._MovementsFeed()
        _answer(feed, _MOVEMENTS_CALL)

        since = feed.mark()

        assert feed.state(since) is None

    def test_call_left_hanging_before_the_mark_is_ignored(self):
        """A call an earlier load never finished neither blocks nor decides the next one."""
        feed = web._MovementsFeed()
        hung = _Request(_MOVEMENTS_CALL)
        feed.on_request(hung)
        since = feed.mark()

        _answer(feed, _MOVEMENTS_CALL)
        feed.on_request_failed(hung)

        assert feed.state(since) == web._LOAD_DONE

    def test_redirect_is_a_step_not_a_failure(self):
        """A 3xx hop leaves the load under way until the redirected call answers."""
        feed = web._MovementsFeed()

        _answer(feed, _MOVEMENTS_CALL, 302)

        assert feed.state(0) is None

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            pytest.param("/credit-card-banking-statement/billing-date/list", "POST", id="other-bff"),
            pytest.param(_MOVEMENTS_CALL, "OPTIONS", id="cors-preflight"),
        ],
    )
    def test_calls_that_are_not_a_load_step_are_ignored(self, path, method):
        """Calls outside /movement-cc/ and CORS preflights neither block nor decide a load."""
        feed = web._MovementsFeed()

        _answer(feed, path, 500 if method == "POST" else 204, method)

        assert feed.state(0) is None


class _CardLocator:
    def __init__(self, page, selector, matches):
        self.page = page
        self.selector = selector
        self.matches = matches

    @property
    def first(self):
        return self

    def count(self):
        return self.matches

    def wait_for(self, **kwargs):
        pass

    def click(self, **kwargs):
        self.page.clicked.append(self.selector)


class _DashboardPage(_SurfacePage):
    """A dashboard whose credit-cards grid may or may not hold the product card."""

    def __init__(self, grid_matches):
        super().__init__([_NO_TABLE])
        self.grid_matches = grid_matches
        self.clicked = []

    def locator(self, selector):
        matches = self.grid_matches if "grid-credit-cards" in selector else 1
        return _CardLocator(self, selector, matches)

    def evaluate(self, script, *args):
        if script is web._CARD_DETAIL_READY_JS:
            return True
        return super().evaluate(script, *args)


class TestOpenCardDetail:
    @pytest.mark.parametrize(
        ("grid_matches", "expected"),
        [
            pytest.param(1, "div.grid-container.grid-credit-cards a.div-product", id="grid-card"),
            pytest.param(0, "a.div-product", id="markup-drift-fallback"),
        ],
    )
    def test_opens_the_credit_card_from_the_grid(self, grid_matches, expected):
        """The card inside the credit-cards grid is clicked, else any product card."""
        page = _DashboardPage(grid_matches)

        assert web._open_card_detail(page) is True
        assert page.clicked == [expected]


class TestDismissOverlays:
    def test_bounded_and_answers_the_idle_modal_first(self):
        """The idle modal is checked first, then at most three backdrop clicks."""
        page = _SurfacePage([_NO_TABLE], backdrop=True)

        web._dismiss_overlays(page)

        assert page.scripts[0] is web._IDLE_MODAL_JS
        assert page.scripts.count(web._DISMISS_OVERLAY_JS) == 3
