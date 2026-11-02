"""Tests for the Tarjeta Lider Bci billing read: rate, current billing, statements.

Every figure, date and identifier is synthetic (canonical CLP set, a 1000,0000
rate, fabricated dates); no real portal capture or statement appears here.
"""

import base64
from datetime import date
from decimal import Decimal

import pytest

from product_model import CreditCardMetrics
from scrapers.backends import bci_lider_billing as billing
from scrapers.backends import bci_lider_web as web
from scrapers.backends.bci_lider_billing import (
    CurrentBilling,
    LiderStatement,
    billing_from_result,
    billing_metrics,
    current_billing_from_data,
    exchange_rate_from_rows,
    parse_rate,
    statement_from_text,
)
from scrapers.backends.bci_lider_web import BciLiderCardResult
from scrapers.base import ScrapedProduct

# A synthetic statement as pypdf's layout mode renders it: header, previous
# period, the movements (with a total line, an installment row, a 0 fee and a
# payment), then the second page's totals and the next period.
STATEMENT_TEXT = """
     SYNTHETIC HOLDER
                                                   Número tarjeta XXXXXXXXXXXX1234
                                                   Fecha Estado de Cuenta 11/03/2026        Página 1 de 2
                                   Cupo Total        Cupo Utilizado      Cupo Disponible
 Cupo Total                       $ 2.500.000        $ 1.000.000          $ 1.500.000
                                                                       Desde            Hasta
                                                    Período Facturado   11/02/2026       10/03/2026
                                                    Pagar Hasta                          21/03/2026
 1. Período Anterior                                       Desde               Hasta
 Período de Facturación Anterior                       11/01/2026          10/02/2026
 Monto Pagado Período Anterior                                    $ -999.999
 2. Período Actual
                                  LIDER
SANTIAGO              01/03/2026   SYNTHETIC STORE ONE (T)                  $          35.000
                                                                                     $          35.000
                                  OTROS COMERCIOS
SANTIAGO CL           03/03/2026   SYNTHETIC   *STORE TWO (A)     $     999.999    $   999.999   01/03   $   333.333
                      11/03/2026   -100% DCTO COM ADM|MANTENCION (T)        $               0
                      05/03/2026   PAGO                                     $        -999.999
                                                   Fecha Estado de Cuenta 11/03/2026        Página 2 de 2
Monto Total Facturado                 $ 1.000.000          Opción de Pago
Monto Mínimo a Pagar                  $ 99.999             Cuotas
Próximo Período de Facturación          11/03/2026          10/04/2026
"""


def _statement(issued_on, period_end, **fields):
    return LiderStatement(
        issued_on=issued_on,
        period_start=None,
        period_end=period_end,
        payment_due_date=fields.get("payment_due_date"),
        billed_amount=fields.get("billed_amount"),
        minimum_payment=fields.get("minimum_payment"),
        next_period_end=fields.get("next_period_end"),
        movements=fields.get("movements", []),
    )


def _current(**fields):
    values = dict(
        billing_date=date(2026, 3, 11),
        payment_due_date=date(2026, 3, 21),
        total_to_pay=1000000.0,
        month_charges=999999.0,
        minimum_payment=99999.0,
    )
    values.update(fields)
    return CurrentBilling(**values)


class TestRate:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("1000.0000", Decimal("1000.0000")),
            ("999.99", Decimal("999.99")),
            (1000, Decimal("1000")),
            ("1000,0000", None),
            ("1e3", None),
            ("0", None),
            ("", None),
            (True, None),
            (None, None),
        ],
    )
    def test_parse_rate(self, raw, expected):
        assert parse_rate(raw) == expected

    def test_the_newest_dated_row_is_the_rate(self):
        """Order does not matter, and a row that does not parse is skipped."""
        # Act
        rate = exchange_rate_from_rows(
            [
                {"date": "15/01/2026", "value": "999.9999"},
                {"date": "16/01/2026", "value": "1000.0000"},
                {"date": "17/01/2026", "value": "1000,5"},
                {"date": "not a date", "value": "999.0000"},
                "garbage",
            ]
        )

        # Assert
        assert rate is not None
        assert rate.rate == Decimal("1000.0000")
        assert rate.reported_as_of == date(2026, 1, 16)
        assert (rate.institution, rate.base_currency, rate.quote_currency) == (
            "bci_lider",
            "USD",
            "CLP",
        )
        assert (rate.product.kind, rate.product.currency, rate.product.external_ref) == (
            "credit_card",
            "USD",
            None,
        )

    @pytest.mark.parametrize("rows", [[], None, [{"date": "16/01/2026", "value": "x"}]])
    def test_no_usable_row_is_no_rate(self, rows):
        assert exchange_rate_from_rows(rows) is None


class TestCurrentBilling:
    def test_reads_every_field(self):
        current = current_billing_from_data(
            {
                "monthCharges": 999999,
                "totalToPay": 1000000,
                "minimumPayment": 99999,
                "billingDate": "11/03/2026",
                "payUntil": "21/03/2026",
            }
        )

        assert current == _current()

    def test_nothing_usable_is_none(self):
        assert current_billing_from_data({"billingDate": "soon", "totalToPay": "n/a"}) is None
        assert current_billing_from_data(None) is None


class TestStatementText:
    def test_reads_the_figures(self):
        statement = statement_from_text(STATEMENT_TEXT)

        assert statement is not None
        assert statement.issued_on == date(2026, 3, 11)
        assert (statement.period_start, statement.period_end) == (
            date(2026, 2, 11),
            date(2026, 3, 10),
        )
        assert statement.closing_date == date(2026, 3, 10)
        assert statement.payment_due_date == date(2026, 3, 21)
        assert statement.billed_amount == 1000000.0
        assert statement.minimum_payment == 99999.0
        assert statement.next_period_end == date(2026, 4, 10)

    def test_reads_the_movements_signed_like_the_scraper(self):
        """A charge is negative, a payment positive; a 0 fee line and the
        total lines are not movements; an installment row keeps its first
        (operation) amount."""
        statement = statement_from_text(STATEMENT_TEXT)

        assert statement.movements == [
            (date(2026, 3, 1), -35000),
            (date(2026, 3, 3), -999999),
            (date(2026, 3, 5), 999999),
        ]

    def test_without_a_period_the_closing_is_the_day_before_the_issue(self):
        text = "Fecha Estado de Cuenta 01/03/2026\n2. Período Actual\n"

        statement = statement_from_text(text)

        assert statement.closing_date == date(2026, 2, 28)
        assert statement.movements == []

    def test_rows_after_the_totals_block_are_not_movements(self):
        """A dated "$" line after "Monto Total Facturado" (a payment plan,
        an ad) is not billed on the statement."""
        text = STATEMENT_TEXT + (
            "                      12/03/2026   CUOTA FLEXIBLE SINTETICA   $   999.999\n"
        )

        statement = statement_from_text(text)

        assert (date(2026, 3, 12), -999999) not in statement.movements
        assert len(statement.movements) == 3

    def test_a_minus_before_the_dollar_sign_is_a_credit(self):
        text = (
            "Fecha Estado de Cuenta 11/03/2026\n2. Período Actual\n"
            "                      05/03/2026   PAGO          -$   999.999\n"
        )

        statement = statement_from_text(text)

        assert statement.movements == [(date(2026, 3, 5), 999999)]

    @pytest.mark.parametrize("text", ["", None, "Detalle sin fechas"])
    def test_not_a_statement(self, text):
        assert statement_from_text(text) is None


class TestBillingMetrics:
    def test_closing_dates_from_the_matching_statement(self):
        """The statement the portal calls its last one gives the closing dates;
        the portal's own figures win for the amounts and due date."""
        statement = _statement(
            date(2026, 3, 11),
            date(2026, 3, 10),
            payment_due_date=date(2026, 3, 20),
            billed_amount=2500000.0,
            minimum_payment=999.0,
            next_period_end=date(2026, 4, 10),
        )

        fields = billing_metrics(_current(), [statement])

        assert fields == {
            "statement_date": date(2026, 3, 10),
            "next_statement_date": date(2026, 4, 10),
            "payment_due_date": date(2026, 3, 21),
            "billed_amount": 1000000.0,
            "minimum_payment": 99999.0,
        }

    def test_the_newest_statement_is_the_latest(self):
        older = _statement(date(2026, 2, 11), date(2026, 2, 10))
        newer = _statement(date(2026, 3, 11), date(2026, 3, 10))

        fields = billing_metrics(_current(), [newer, older])

        assert fields["statement_date"] == date(2026, 3, 10)

    def test_a_statement_of_another_period_is_ignored(self):
        """Without the matching PDF the day before the portal's billing date
        stands in: the billed period ends the day before it is issued."""
        older = _statement(
            date(2026, 2, 11), date(2026, 2, 10), next_period_end=date(2026, 3, 10)
        )

        fields = billing_metrics(_current(), [older])

        assert fields["statement_date"] == date(2026, 3, 10)
        assert "next_statement_date" not in fields

    def test_a_statement_without_an_issue_date_matches_by_its_period(self):
        statement = _statement(None, date(2026, 3, 10), next_period_end=date(2026, 4, 10))

        fields = billing_metrics(_current(), [statement])

        assert fields["statement_date"] == date(2026, 3, 10)
        assert fields["next_statement_date"] == date(2026, 4, 10)

    def test_portal_figures_alone(self):
        fields = billing_metrics(_current(billing_date=date(2026, 3, 1)), [])

        assert fields["statement_date"] == date(2026, 2, 28)

    def test_statement_alone(self):
        statement = statement_from_text(STATEMENT_TEXT)

        fields = billing_metrics(None, [statement])

        assert fields == {
            "statement_date": date(2026, 3, 10),
            "next_statement_date": date(2026, 4, 10),
            "payment_due_date": date(2026, 3, 21),
            "billed_amount": 1000000.0,
            "minimum_payment": 99999.0,
        }

    def test_month_charges_stand_in_for_a_missing_total(self):
        fields = billing_metrics(_current(total_to_pay=None), [])

        assert fields["billed_amount"] == 999999.0

    def test_nothing_known(self):
        assert billing_metrics(None, []) == {}


def _encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.fixture
def text_pdfs(monkeypatch):
    """A "PDF" here is its text, base64-encoded: no real PDF is needed."""
    monkeypatch.setattr(billing, "statement_text_from_pdf", lambda data: data.decode())


def _result(**parts):
    result = {
        "status": "ok",
        "rate": {"status": "ok", "data": [{"date": "16/01/2026", "value": "1000.0000"}]},
        "billing": {
            "status": "ok",
            "data": {
                "monthCharges": 999999,
                "totalToPay": 1000000,
                "minimumPayment": 99999,
                "billingDate": "11/03/2026",
                "payUntil": "21/03/2026",
            },
        },
        "periods": {"status": "ok"},
        "statements": [{"periodo": "202603", "status": "ok", "pdf": _encoded(STATEMENT_TEXT)}],
    }
    result.update(parts)
    return result


class TestBillingFromResult:
    def test_reads_every_part(self, text_pdfs):
        read = billing_from_result(_result())

        assert [rate.rate for rate in read.exchange_rates] == [Decimal("1000.0000")]
        assert read.current == _current()
        assert [s.closing_date for s in read.statements] == [date(2026, 3, 10)]
        assert read.warnings == []

    def test_a_failed_part_is_a_warning_with_its_status_only(self, text_pdfs):
        read = billing_from_result(
            _result(
                rate={"status": "http", "code": 401, "body": "SECRET"},
                billing={"status": "timeout"},
                periods={"status": "error"},
                statements=[],
            )
        )

        assert read.exchange_rates == []
        assert read.current is None
        assert read.warnings == [
            "BciLider: no USD rate (http 401)",
            "BciLider: no current billing (timeout)",
            "BciLider: no statement list (error)",
        ]
        assert not any("SECRET" in warning for warning in read.warnings)

    def test_an_unreadable_statement_is_counted(self, text_pdfs):
        read = billing_from_result(
            _result(
                statements=[
                    {"periodo": "202603", "status": "ok", "pdf": _encoded(STATEMENT_TEXT)},
                    {"periodo": "202602", "status": "ok", "pdf": "not base64!"},
                    {"periodo": "202601", "status": "http", "code": 500, "pdf": None},
                ]
            )
        )

        assert len(read.statements) == 1
        assert read.warnings == ["BciLider: 2 statement(s) not read"]

    def test_a_statement_skipped_by_the_deadline_is_counted(self, text_pdfs):
        """The in-page script skips the PDFs left once its total budget is spent."""
        read = billing_from_result(
            _result(
                statements=[
                    {"periodo": "202603", "status": "ok", "pdf": _encoded(STATEMENT_TEXT)},
                    {"periodo": "202602", "status": "skipped", "pdf": None},
                ]
            )
        )

        assert len(read.statements) == 1
        assert read.warnings == ["BciLider: 1 statement(s) not read"]

    def test_statements_with_no_movements_at_all_are_a_warning(self, text_pdfs):
        """Every statement parsing to zero movements means the layout moved."""
        empty = "Fecha Estado de Cuenta 11/03/2026\nMonto Total Facturado $ 1.000.000\n"
        read = billing_from_result(
            _result(statements=[{"periodo": "202603", "status": "ok", "pdf": _encoded(empty)}])
        )

        assert len(read.statements) == 1
        assert read.warnings == ["BciLider: no movements read from any statement"]

    def test_an_oversized_statement_is_not_parsed(self, text_pdfs, monkeypatch):
        monkeypatch.setattr(billing, "MAX_PDF_BASE64_CHARS", 8)
        read = billing_from_result(_result())

        assert read.statements == []
        assert read.warnings == ["BciLider: 1 statement(s) not read"]

    def test_the_script_has_a_total_deadline_and_a_cap(self):
        assert "deadline" in billing._BILLING_JS
        assert billing.MAX_STATEMENTS == 8

    @pytest.mark.parametrize(
        "result, reason",
        [
            ({"status": "off_portal"}, "off_portal"),
            ({"status": "no_session"}, "no_session"),
            ({"status": "weird"}, "no result"),
            (None, "no result"),
        ],
    )
    def test_no_session_reads_nothing(self, result, reason):
        read = billing_from_result(result)

        assert (read.exchange_rates, read.current, read.statements) == ([], None, [])
        assert read.warnings == [f"BciLider: billing not read ({reason})"]


class _BillingPage:
    """A portal tab whose in-page billing script answers `result`."""

    def __init__(self, result):
        self.result = result

    def evaluate(self, script, arg=None):
        assert "estadoCuentaPDF" in script
        return self.result


def _card(currency):
    return ScrapedProduct(
        institution="bci_lider",
        kind="credit_card",
        currency=currency,
        metrics=CreditCardMetrics(available=1500000, limit=2500000, owed=1000000),
    )


class TestReadBillingInto:
    def test_fills_the_clp_card_and_carries_the_rate(self, text_pdfs):
        # Arrange
        result = BciLiderCardResult(products=[_card("CLP"), _card("USD")])

        # Act
        web._read_billing_into(_BillingPage(_result()), result)

        # Assert
        clp, usd = result.products
        assert clp.metrics.statement_date == date(2026, 3, 10)
        assert clp.metrics.next_statement_date == date(2026, 4, 10)
        assert clp.metrics.payment_due_date == date(2026, 3, 21)
        assert clp.metrics.billed_amount == 1000000.0
        assert clp.metrics.minimum_payment == 99999.0
        assert clp.metrics.available == 1500000
        assert usd.metrics.statement_date is None
        assert [rate.product.currency for rate in result.exchange_rates] == ["USD"]
        assert len(result.statements) == 1

    def test_a_failing_script_is_a_warning(self):
        # Arrange
        class _Broken:
            def evaluate(self, script, arg=None):
                raise RuntimeError("target closed")

        result = BciLiderCardResult(products=[_card("CLP")])

        # Act
        web._read_billing_into(_Broken(), result)

        # Assert
        assert result.products[0].metrics.statement_date is None
        assert result.warnings == ["BciLider: billing script failed: RuntimeError"]
