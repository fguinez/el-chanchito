"""Tests for the Banco Santander scraper: parsers, ids, and the two legs.

Payloads are written from scratch to mirror the shape of the SPA's JSON
(`cruceProductosOnline` and the transactions API); every identifier, figure
and description is synthetic. No test launches a browser: the scraper legs run
against a faked `fetch_session`.
"""

from datetime import date
from decimal import Decimal

import pytest

from scrapers.backends import santander_web as web
from scrapers.backends.santander_web import (
    SantanderAccount,
    SantanderMovement,
    SantanderPayloadError,
)

# Canonical synthetic amounts as the API pads them: 18-char balances and
# 15-char movement amounts, both with two implied decimals.
BALANCE_2_500_000 = "000000000250000000"
AMOUNT_999_999 = "000000099999900"
AMOUNT_1_000_000 = "000000100000000"


def _account_row(**overrides) -> dict:
    row = {
        "NUMEROCONTRATO": "000000000001",
        "PRODUCTO": "00",
        "SUBPRODUCTO": "0000",
        "MONTODISPONIBLE": BALANCE_2_500_000,
        "MONTOUTILIZADO": "000000000000000000",
        "GLOSACORTA": "CTA VISTA SINTETICA",
        "OFICINACONTRATO": "0000",
        "CUPO": "000000000000000000",
        "GLOSAESTADO": "ACTIVA",
        "NUMEROPAN": "",
        "ESTADOOPERACION": "00",
        "ESTADORELACION": "00",
        "CODIGOMONEDA": "CLP",
        "AGRUPACIONCOMERCIAL": "",
        "CALIDADPARTICIPACION": "00",
    }
    row.update(overrides)
    return row


def _products_payload(rows) -> dict:
    return {
        "METADATA": {"STATUS": "0", "DESCRIPCION": "OK"},
        "DATA": {
            "OUTPUT": {
                "INFO": {"CODERR": "0000", "DESERR": "", "MSGUSUARIO": ""},
                "ESCALARES": {"NOMBRE": "Persona Sintetica"},
                "MATRICES": {"MATRIZCAPTACIONES": {"e1": rows}},
            }
        },
    }


def _movement_row(**overrides) -> dict:
    row = {
        "accountingDate": "2026-07-15T00:00:00",
        "transactionDate": "2026-07-14T00:00:00",
        "operationTime": "1200",
        "newBalance": BALANCE_2_500_000,
        "codeOperationMovement": "0000",
        "movementAmount": AMOUNT_999_999 + "-",
        "observation": "0000000000 COMERCIO SINTETICO",
        "expandedCode": "COMPRA SINTETICA",
        "movementNumber": "000000001",
        "chargePaymentFlag": "D",
    }
    row.update(overrides)
    return row


def _movements_payload(rows) -> dict:
    return {
        "additionalInfo": [
            {"key": "code-ms", "value": "0"},
            {"key": "message-ms", "value": "OK"},
        ],
        "movements": rows,
    }


def _movement(**overrides) -> SantanderMovement:
    movement, _ = web.parse_movement(_movement_row(**overrides))
    assert movement is not None
    return movement


class TestParseCents:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(BALANCE_2_500_000, Decimal("2500000.00"), id="padded-balance"),
            pytest.param(AMOUNT_999_999, Decimal("999999.00"), id="padded-amount"),
            pytest.param(AMOUNT_999_999 + "-", Decimal("-999999.00"), id="trailing-minus"),
            pytest.param("-" + AMOUNT_999_999, Decimal("-999999.00"), id="leading-minus"),
            pytest.param("000000000000123456", Decimal("1234.56"), id="cents"),
            pytest.param("000000000000000000", Decimal("0.00"), id="zero"),
        ],
    )
    def test_reads_implied_cents(self, raw, expected):
        """The last two digits are cents and a "-" on either side is money out."""
        assert web.parse_cents(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("", id="blank"),
            pytest.param(None, id="none"),
            pytest.param("12.50", id="decimal-point"),
            pytest.param("ABC", id="letters"),
            pytest.param("-0001-", id="two-signs"),
        ],
    )
    def test_rejects_garbage(self, raw):
        """Anything but padded digits with one optional sign is unparseable."""
        assert web.parse_cents(raw) is None


class TestParseIsoDate:
    def test_takes_the_printed_date(self):
        """The time half is ignored, never shifted by a time zone."""
        assert web.parse_iso_date("2026-07-14T23:59:59Z") == date(2026, 7, 14)

    def test_bare_date(self):
        """A plain YYYY-MM-DD parses too."""
        assert web.parse_iso_date("2026-07-14") == date(2026, 7, 14)

    @pytest.mark.parametrize("raw", ["", None, "14/07/2026", "2026-13-01"])
    def test_rejects_other_shapes(self, raw):
        """Other formats and impossible dates are None."""
        assert web.parse_iso_date(raw) is None


class TestParseProducts:
    def test_clp_vista(self):
        """A CLP cuenta vista becomes one account with its balance in pesos."""
        parsed = web.parse_products(_products_payload([_account_row()]))

        assert parsed.accounts == [
            SantanderAccount(
                contract="000000000001",
                office="0000",
                glosa="CTA VISTA SINTETICA",
                currency="CLP",
                balance=Decimal("2500000"),
            )
        ]
        assert parsed.ignored == 0

    def test_single_row_as_dict(self):
        """A lone `e1` item may arrive as a bare dict instead of a list."""
        parsed = web.parse_products(_products_payload(_account_row()))

        assert len(parsed.accounts) == 1

    def test_account_id_and_display_number(self):
        """The movements accountId is office + contract; display drops the padding."""
        account = web.parse_products(_products_payload([_account_row()])).accounts[0]

        assert account.account_id == "0000000000000001"
        assert account.display_number == "1"

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"CODIGOMONEDA": "USD"}, id="usd"),
            pytest.param({"GLOSACORTA": "CTA CTE SINTETICA"}, id="cuenta-corriente"),
            pytest.param({"GLOSACORTA": "LINEA SINTETICA"}, id="linea"),
        ],
    )
    def test_other_products_are_ignored(self, overrides):
        """Rows that are not a CLP vista are counted and dropped."""
        rows = [_account_row(), _account_row(NUMEROCONTRATO="000000000002", **overrides)]

        parsed = web.parse_products(_products_payload(rows))

        assert [a.contract for a in parsed.accounts] == ["000000000001"]
        assert parsed.ignored == 1

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"MONTODISPONIBLE": ""}, id="no-balance"),
            pytest.param({"MONTODISPONIBLE": "000000000099999950"}, id="clp-with-cents"),
            pytest.param({"NUMEROCONTRATO": ""}, id="no-contract"),
            pytest.param({"OFICINACONTRATO": "XX"}, id="bad-office"),
        ],
    )
    def test_unusable_vista_is_invalid(self, overrides):
        """A vista row without a usable contract or whole balance is skipped."""
        parsed = web.parse_products(_products_payload([_account_row(**overrides)]))

        assert parsed.accounts == []
        assert parsed.invalid == 1

    def test_several_vistas_in_a_stable_order(self):
        """Several vistas come back lowest contract first, whatever the API order."""
        rows = [_account_row(NUMEROCONTRATO="000000000002"), _account_row()]

        parsed = web.parse_products(_products_payload(rows))

        assert [a.contract for a in parsed.accounts] == ["000000000001", "000000000002"]

    def test_no_captaciones_is_empty(self):
        """A payload without the matrix has no accounts, not a shape error."""
        payload = _products_payload([])
        del payload["DATA"]["OUTPUT"]["MATRICES"]

        assert web.parse_products(payload).accounts == []

    @pytest.mark.parametrize("payload", [None, {}, {"DATA": {}}, {"DATA": {"OUTPUT": []}}])
    def test_unrecognisable_shape_raises(self, payload):
        """Without DATA.OUTPUT the product list cannot be trusted at all."""
        with pytest.raises(SantanderPayloadError):
            web.parse_products(payload)


class TestParseMovements:
    def test_debit(self):
        """A trailing "-" with flag D is money out, dated by transactionDate."""
        movement = _movement()

        assert movement.amount == Decimal("-999999")
        assert movement.transaction_date == date(2026, 7, 14)
        assert movement.accounting_date == date(2026, 7, 15)
        assert movement.description == "0000000000 COMERCIO SINTETICO"
        assert movement.movement_number == "1"
        assert movement.new_balance == Decimal("2500000.00")

    def test_credit(self):
        """No trailing "-" with flag H is money in."""
        movement = _movement(movementAmount=AMOUNT_1_000_000, chargePaymentFlag="H")

        assert movement.amount == Decimal("1000000")

    def test_description_whitespace_collapsed(self):
        """The bank's text is kept, with runs of spaces collapsed."""
        movement = _movement(observation="  0000000000   COMERCIO   SINTETICO ")

        assert movement.description == "0000000000 COMERCIO SINTETICO"

    def test_description_falls_back_to_expanded_code(self):
        """An empty observation falls back to expandedCode."""
        assert _movement(observation="").description == "COMPRA SINTETICA"

    def test_accounting_date_stands_in_for_a_missing_transaction_date(self):
        """A row with only a posting date is dated by it."""
        movement = _movement(transactionDate="")

        assert movement.transaction_date == date(2026, 7, 15)

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            pytest.param({"movementAmount": AMOUNT_999_999, "chargePaymentFlag": "D"}, web.SKIP_SIGN_MISMATCH, id="debit-without-minus"),
            pytest.param({"movementAmount": AMOUNT_999_999 + "-", "chargePaymentFlag": "H"}, web.SKIP_SIGN_MISMATCH, id="credit-with-minus"),
            pytest.param({"movementAmount": "000000000000000"}, web.SKIP_ZERO, id="zero"),
            pytest.param({"movementAmount": ""}, web.SKIP_INVALID, id="no-amount"),
            pytest.param({"movementAmount": "ABC"}, web.SKIP_INVALID, id="unparseable"),
            pytest.param({"movementAmount": "000000099999950-"}, web.SKIP_INVALID, id="clp-with-cents"),
            pytest.param({"transactionDate": "", "accountingDate": ""}, web.SKIP_INVALID, id="no-date"),
        ],
    )
    def test_skipped_rows(self, overrides, reason):
        """Bad rows are skipped with the reason they were."""
        movement, skipped_because = web.parse_movement(_movement_row(**overrides))

        assert movement is None
        assert skipped_because == reason

    def test_counts_skips_and_mismatches(self):
        """parse_movements keeps good rows and counts each kind of skip."""
        rows = [
            _movement_row(),
            _movement_row(movementNumber="000000002", movementAmount="000000000000000"),
            _movement_row(movementNumber="000000003", chargePaymentFlag="H"),
            "not-a-row",
        ]

        parsed = web.parse_movements(_movements_payload(rows))

        assert len(parsed.movements) == 1
        assert parsed.zero == 1
        assert parsed.skipped == 1
        assert parsed.sign_mismatches == 1

    def test_empty_list_is_valid(self):
        """An account with no movements in the window is not an error."""
        assert web.parse_movements(_movements_payload([])).movements == []

    @pytest.mark.parametrize("payload", [None, {}, {"movements": None}, {"movements": {}}])
    def test_unrecognisable_shape_raises(self, payload):
        """Without a movements list the payload cannot be trusted."""
        with pytest.raises(SantanderPayloadError):
            web.parse_movements(payload)


class TestExternalId:
    def test_movement_number_id(self):
        """The bank's movement number, unpadded, scoped by the posting date."""
        assert web.external_id_for(_movement()) == "san_mov_2026-07-15_1"

    def test_no_posting_date_falls_back_to_the_fingerprint(self):
        """The occurrence date never stands in for the posting date in a san_mov_ id."""
        external_id = web.external_id_for(_movement(accountingDate=""))

        assert external_id.startswith("san_fp_")

    def test_stable(self):
        """The same movement yields the same id on every parse."""
        assert web.external_id_for(_movement()) == web.external_id_for(_movement())

    def test_distinct_numbers_distinct_ids(self):
        """Two movements with different numbers never share an id."""
        a = web.external_id_for(_movement(movementNumber="000000001"))
        b = web.external_id_for(_movement(movementNumber="000000002"))

        assert a != b

    @pytest.mark.parametrize("number", ["", "000000000", "ABC", None])
    def test_fingerprint_fallback(self, number):
        """Without a usable movement number the id is a description-free hash."""
        external_id = web.external_id_for(_movement(movementNumber=number))

        assert external_id.startswith("san_fp_")
        assert len(external_id) == len("san_fp_") + 16

    @pytest.mark.parametrize("number", ["000000001", ""])
    def test_description_rewrite_keeps_the_id(self, number):
        """Rewording observation or expandedCode never re-keys a movement."""
        before = _movement(movementNumber=number)
        after = _movement(
            movementNumber=number,
            observation="1111111111 OTRO COMERCIO SINTETICO",
            expandedCode="OTRO TEXTO",
        )

        assert web.external_id_for(before) == web.external_id_for(after)

    def test_fingerprint_separates_same_day_same_amount(self):
        """The running balance keeps two identical same-day charges apart."""
        a = _movement(movementNumber="", newBalance=BALANCE_2_500_000)
        b = _movement(movementNumber="", newBalance="000000000099999900")

        assert web.external_id_for(a) != web.external_id_for(b)


class TestLatestBalance:
    def test_picks_the_latest_posted_movement(self):
        """The newest posting date, then the highest number, wins."""
        older = _movement(accountingDate="2026-07-01", movementNumber="000000009", newBalance="000000000100000000")
        newer = _movement(accountingDate="2026-07-15", movementNumber="000000002", newBalance=BALANCE_2_500_000)

        assert web.latest_balance([older, newer]) == Decimal("2500000.00")

    def test_none_without_balances(self):
        """No reported balance means nothing to compare."""
        assert web.latest_balance([_movement(newBalance="")]) is None
        assert web.latest_balance([]) is None


class TestTransactionsRequestBody:
    def test_body(self):
        """The replay body mirrors the SPA's: office + contract, CLP, a window."""
        account = web.parse_products(_products_payload([_account_row()])).accounts[0]

        body = web.transactions_request_body(account, date(2026, 5, 7), date(2026, 7, 15))

        assert body == {
            "accountId": "0000000000000001",
            "currency": "CLP",
            "commercialGroup": "",
            "openingDate": "2026-05-07",
            "closingDate": "2026-07-15",
        }
