"""Tests for the Banco Santander scraper: parsers, ids, and the two legs.

Payloads are written from scratch to mirror the shape of the SPA's JSON
(`cruceProductosOnline` and the transactions API); every identifier, figure
and description is synthetic. No test launches a browser: the scraper legs run
against a faked `fetch_session`.
"""

import asyncio
import os
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault("SANTANDER_RUT", "00.000.000-0")
os.environ.setdefault("SANTANDER_PASSWORD", "synthetic-password")

from scrapers.backends import santander_web as web
from scrapers.backends.santander_web import (
    SantanderAccount,
    SantanderBlockedError,
    SantanderLoginError,
    SantanderMovement,
    SantanderPayloadError,
    SantanderSessionResult,
    SantanderStepUpError,
)
from scrapers.institutions import santander as santander_mod
from scrapers.institutions.santander import SantanderScraper

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


class TestFindMarker:
    @pytest.mark.parametrize(
        "text",
        [
            "Ingresa tu Clave Dinámica",
            "Autoriza con SANTANDER PASS",
            "Ingresa el código de verificación",
        ],
    )
    def test_step_up_texts_match_without_accents(self, text):
        """Step-up prompts are found whatever their case or accents."""
        assert web.find_marker(text, web._STEP_UP_MARKERS) is not None

    def test_ordinary_text_does_not_match(self):
        """The login form's own labels are not a step-up."""
        assert web.find_marker("RUT Clave Ingresar", web._STEP_UP_MARKERS) is None


class _FakeRequest:
    def __init__(self, url, headers=None, body=None, resource_type="xhr"):
        self.url = url
        self.headers = headers or {}
        self.post_data_json = body
        self.resource_type = resource_type


class _FakeResponse:
    def __init__(self, url, payload=None, status=200, request=None, frame=None):
        self.url = url
        self.status = status
        self._payload = payload
        self.request = request or _FakeRequest(url)
        self.frame = frame

    def json(self):
        return self._payload


_PRODUCTS_URL = "https://api-dsk.santander.cl/perdsk/datosCliente/cruceProductosOnline"
_SESSION_PARAMS_URL = "https://openbanking.santander.cl/party_authentication/session/v1/parameters"
_SYNTHETIC_HEADERS = {
    "Authorization": "Bearer synthetic-token",
    "x-client-code": "synthetic-client",
    "x-organization-code": "synthetic-org",
    "x-santander-client-id": "synthetic-id",
}


class TestSessionCapture:
    def setup_method(self):
        self.capture = web.SessionCapture()

    def test_captures_products(self):
        """A 200 products response is kept."""
        payload = _products_payload([_account_row()])

        self.capture.on_response(_FakeResponse(_PRODUCTS_URL, payload))

        assert self.capture.products == [payload]

    def test_movements_paired_with_their_account(self):
        """A movements payload is returned only for the account it was asked for."""
        request = _FakeRequest(web.MOVEMENTS_URL, body={"accountId": "0000000000000001"})
        payload = _movements_payload([_movement_row()])

        self.capture.on_response(_FakeResponse(web.MOVEMENTS_URL, payload, request=request))

        assert self.capture.movements_for("0000000000000001") == payload
        assert self.capture.movements_for("0000000000000002") is None

    def test_movements_without_a_readable_account_are_never_taken(self):
        """A payload whose request body can't be read is no account's movements."""
        payload = _movements_payload([_movement_row()])

        self.capture.on_response(_FakeResponse(web.MOVEMENTS_URL, payload))

        assert self.capture.movements_for("0000000000000001") is None

    def test_non_200_ignored(self):
        """An error response is not data."""
        self.capture.on_response(_FakeResponse(_PRODUCTS_URL, {"x": 1}, status=500))

        assert self.capture.products == []

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(
                _FakeResponse("https://banco.santander.cl/fueradelinea/style.css"),
                id="block-asset",
            ),
            pytest.param(
                _FakeResponse(
                    "https://banco.santander.cl/personas",
                    status=403,
                    request=_FakeRequest(
                        "https://banco.santander.cl/personas", resource_type="document"
                    ),
                ),
                id="403-document",
            ),
        ],
    )
    def test_block_page_detected(self, response):
        """The edge's block page or a refused document marks the session blocked."""
        self.capture.on_response(response)

        assert self.capture.blocked

    def test_403_in_a_third_party_iframe_is_not_a_block(self):
        """A refused document from another host in a sub-frame can't fail the run."""
        self.capture.main_frame = object()
        url = "https://ads.example.com/frame"

        self.capture.on_response(
            _FakeResponse(
                url, status=403, request=_FakeRequest(url, resource_type="document"), frame=object()
            )
        )

        assert not self.capture.blocked

    def test_403_main_frame_document_is_a_block_on_any_host(self):
        """The main frame being refused is a block whatever host served it."""
        main = object()
        self.capture.main_frame = main
        url = "https://edge.example.net/denied"

        self.capture.on_response(
            _FakeResponse(
                url, status=403, request=_FakeRequest(url, resource_type="document"), frame=main
            )
        )

        assert self.capture.blocked

    def test_403_xhr_is_not_a_block(self):
        """Only a refused document is the edge's block, not a failed API call."""
        self.capture.on_response(_FakeResponse(web.MOVEMENTS_URL, status=403))

        assert not self.capture.blocked

    def test_replay_headers_from_an_openbanking_request(self):
        """The SPA's own open-banking headers are kept, lower-cased, for a replay."""
        self.capture.on_request(_FakeRequest(_SESSION_PARAMS_URL, headers=_SYNTHETIC_HEADERS))

        assert self.capture.replay_headers() == {
            "authorization": "Bearer synthetic-token",
            "x-client-code": "synthetic-client",
            "x-organization-code": "synthetic-org",
            "x-santander-client-id": "synthetic-id",
        }

    def test_replay_needs_every_required_header(self):
        """Without x-organization-code there is no replay."""
        headers = {k: v for k, v in _SYNTHETIC_HEADERS.items() if k != "x-organization-code"}

        self.capture.on_request(_FakeRequest(_SESSION_PARAMS_URL, headers=headers))

        assert self.capture.replay_headers() is None

    def test_other_hosts_ignored(self):
        """Headers sent to other hosts are never captured."""
        self.capture.on_request(_FakeRequest("https://example.com/x", headers=_SYNTHETIC_HEADERS))

        assert self.capture.headers == {}

    def test_host_named_only_in_the_query_ignored(self):
        """A third-party URL that merely mentions the open-banking host is not it."""
        url = "https://example.com/x?next=https://openbanking.santander.cl/"

        self.capture.on_request(_FakeRequest(url, headers=_SYNTHETIC_HEADERS))

        assert self.capture.headers == {}

    def test_broken_response_never_raises(self):
        """A handler that fails must stay silent."""

        class Broken(_FakeResponse):
            def json(self):
                raise ValueError("not json")

        self.capture.on_response(Broken(_PRODUCTS_URL))

        assert self.capture.products == []


class _IdlePage:
    """A page that only waits; the test fills the capture."""

    url = "https://mibanco.santander.cl/UI.Web.HB/Private_new/frame/#/private/home/main/resumen"

    def wait_for_timeout(self, ms):
        pass


class TestReadSession:
    def setup_method(self):
        self.page = _IdlePage()
        self.capture = web.SessionCapture()
        self.today = date(2026, 7, 15)

    def _quick(self, monkeypatch, *, click=True, movements=None, replay=(0, None)):
        for name in ("_PRODUCTS_TIMEOUT_MS", "_MOVEMENTS_TIMEOUT_MS"):
            monkeypatch.setattr(web, name, 0)
        calls = {"replay": 0}

        def fake_open(page):
            if click and movements is not None:
                request = _FakeRequest(web.MOVEMENTS_URL, body={"accountId": "0000000000000001"})
                self.capture.on_response(
                    _FakeResponse(web.MOVEMENTS_URL, movements, request=request)
                )
            return click

        def fake_replay(page, capture, account, today):
            calls["replay"] += 1
            return replay

        monkeypatch.setattr(web, "_open_movements_view", fake_open)
        monkeypatch.setattr(web, "_replay_movements", fake_replay)
        return calls

    def test_vista_and_movements(self, monkeypatch):
        """The clicked card's movements come back with the vista."""
        self.capture.products.append(_products_payload([_account_row()]))
        calls = self._quick(monkeypatch, movements=_movements_payload([_movement_row()]))

        result = web.read_session(self.page, self.capture, self.today)

        assert result.account.balance == Decimal("2500000")
        assert [web.external_id_for(m) for m in result.movements] == ["san_mov_2026-07-15_1"]
        assert result.warnings == []
        assert calls["replay"] == 0

    def test_replay_fallback(self, monkeypatch):
        """No movements from the click path: one replay is tried and used."""
        self.capture.products.append(_products_payload([_account_row()]))
        calls = self._quick(
            monkeypatch, movements=None, replay=(200, _movements_payload([_movement_row()]))
        )

        result = web.read_session(self.page, self.capture, self.today)

        assert calls["replay"] == 1
        assert len(result.movements) == 1

    def test_missing_movements_is_partial(self, monkeypatch, caplog):
        """Without movements the vista is still returned, with a warning."""
        self.capture.products.append(_products_payload([_account_row()]))
        self._quick(monkeypatch, click=False, replay=(401, None))

        result = web.read_session(self.page, self.capture, self.today)

        assert result.account is not None
        assert result.movements == []
        assert result.warnings == ["Santander: movements did not load"]
        assert "status 401" in caplog.text

    def test_no_vista_is_a_warning(self, monkeypatch):
        """A product list without a CLP vista yields no account, with a warning."""
        self.capture.products.append(_products_payload([_account_row(CODIGOMONEDA="USD")]))
        self._quick(monkeypatch)

        result = web.read_session(self.page, self.capture, self.today)

        assert result.account is None
        assert result.warnings == ["Santander: no CLP cuenta vista in the product list"]

    def test_sign_mismatch_is_a_warning(self, monkeypatch):
        """Skipping a movement over a sign/flag disagreement makes the run partial."""
        self.capture.products.append(_products_payload([_account_row()]))
        rows = [_movement_row(), _movement_row(movementNumber="000000002", chargePaymentFlag="H")]
        self._quick(monkeypatch, movements=_movements_payload(rows))

        result = web.read_session(self.page, self.capture, self.today)

        assert len(result.movements) == 1
        assert len(result.warnings) == 1
        assert "sign" in result.warnings[0]

    def test_balance_disagreement_logged_without_figures(self, monkeypatch, caplog):
        """A running balance that disagrees is a warning naming no figure."""
        self.capture.products.append(_products_payload([_account_row()]))
        rows = [_movement_row(newBalance="000000000099999900")]
        self._quick(monkeypatch, movements=_movements_payload(rows))

        result = web.read_session(self.page, self.capture, self.today)

        assert result.account.balance == Decimal("2500000")
        assert "disagree" in caplog.text
        assert "2500000" not in caplog.text and "999999" not in caplog.text

    def test_balance_off_by_a_factor_of_100_is_a_warning(self, monkeypatch, caplog):
        """A running balance exactly 100 times off flags the cents reading."""
        self.capture.products.append(_products_payload([_account_row()]))
        rows = [_movement_row(newBalance="000000025000000000")]
        self._quick(monkeypatch, movements=_movements_payload(rows))

        result = web.read_session(self.page, self.capture, self.today)

        assert result.warnings == [
            "Santander balance unit looks inconsistent with the running balance; "
            "check the implied-cents reading"
        ]
        assert "2500000" not in caplog.text

    def test_unusable_movements_are_a_warning(self, monkeypatch):
        """Dateless or unreadable movements make the run partial; zero amounts don't."""
        self.capture.products.append(_products_payload([_account_row()]))
        rows = [
            _movement_row(),
            _movement_row(movementNumber="000000002", movementAmount="ABC"),
            _movement_row(movementNumber="000000003", movementAmount="000000000000000"),
        ]
        self._quick(monkeypatch, movements=_movements_payload(rows))

        result = web.read_session(self.page, self.capture, self.today)

        assert result.warnings == [
            "Santander: 1 movements skipped (no date or unreadable amount)"
        ]

    def test_another_accounts_movements_are_partial(self, monkeypatch):
        """Movements the SPA loaded for another account are never ingested."""
        self.capture.products.append(_products_payload([_account_row()]))
        self._quick(monkeypatch)
        request = _FakeRequest(web.MOVEMENTS_URL, body={"accountId": "0000000000000002"})
        self.capture.on_response(
            _FakeResponse(web.MOVEMENTS_URL, _movements_payload([_movement_row()]), request=request)
        )

        result = web.read_session(self.page, self.capture, self.today)

        assert result.movements == []
        assert result.warnings == ["Santander: movements did not load"]

    def test_no_products_raises(self, monkeypatch):
        """A dashboard that never loads the product list fails the leg."""
        self._quick(monkeypatch)

        with pytest.raises(SantanderPayloadError):
            web.read_session(self.page, self.capture, self.today)

    def test_block_raises(self, monkeypatch):
        """The block page showing up mid-session raises the typed error."""
        self.capture.blocked = True
        self._quick(monkeypatch)

        with pytest.raises(SantanderBlockedError):
            web.read_session(self.page, self.capture, self.today)


def _session_result(**overrides) -> SantanderSessionResult:
    account = web.parse_products(_products_payload([_account_row()])).accounts[0]
    movements = web.parse_movements(
        _movements_payload(
            [
                _movement_row(),
                _movement_row(
                    movementNumber="000000002",
                    movementAmount=AMOUNT_1_000_000,
                    chargePaymentFlag="H",
                    transactionDate="2026-06-30T00:00:00",
                    accountingDate="2026-07-01T00:00:00",
                ),
            ]
        )
    ).movements
    values = {"account": account, "movements": movements, "warnings": []}
    values.update(overrides)
    return SantanderSessionResult(**values)


class TestScraperLegs:
    def setup_method(self):
        self.scraper = SantanderScraper()

    def _install(self, monkeypatch, result=None, exc=None):
        calls = {"n": 0, "headless": []}

        async def fake_fetch_session(rut, password, *, headless=True):
            calls["n"] += 1
            calls["headless"].append(headless)
            if exc is not None:
                raise exc
            return result

        monkeypatch.setattr(santander_mod, "fetch_session", fake_fetch_session)
        return calls

    def test_one_drive_feeds_both_legs(self, monkeypatch):
        """Transactions then products log in exactly once."""
        calls = self._install(monkeypatch, result=_session_result())

        transactions = asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert len(transactions) == 2
        assert len(products.products) == 1

    def test_product_is_the_singleton_clp_vista(self, monkeypatch):
        """The vista is a singleton with a generic name and its display number."""
        self._install(monkeypatch, result=_session_result())

        asyncio.run(self.scraper.scrape_transactions())
        product = asyncio.run(self.scraper.scrape_products()).products[0]

        assert (product.institution, product.kind, product.currency) == (
            "santander",
            "vista",
            "CLP",
        )
        assert product.external_ref is None
        assert product.name == "Cuenta Vista"
        assert product.attributes.account_number == "1"
        assert product.metrics.balance == 2500000.0

    def test_transactions_land_on_the_vista(self, monkeypatch):
        """Every transaction matches the singleton and carries both dates."""
        self._install(monkeypatch, result=_session_result())

        debit, credit = asyncio.run(self.scraper.scrape_transactions())

        assert {(t.institution, t.product_kind, t.currency) for t in (debit, credit)} == {
            ("santander", "vista", "CLP")
        }
        assert debit.amount == Decimal("-999999")
        assert credit.amount == Decimal("1000000")
        assert debit.accounting_date == date(2026, 7, 15)
        assert debit.description == "0000000000 COMERCIO SINTETICO"
        assert debit.external_id == "san_mov_2026-07-15_1"

    def test_scheduled_month_follows_the_transaction_date(self, monkeypatch):
        """scheduled_month is the first of the occurrence month, not the posting one."""
        self._install(monkeypatch, result=_session_result())

        debit, credit = asyncio.run(self.scraper.scrape_transactions())

        assert debit.scheduled_month == date(2026, 7, 1)
        assert credit.scheduled_month == date(2026, 6, 1)

    def test_products_alone_drives_once(self, monkeypatch):
        """Called on its own, the products leg logs in itself."""
        calls = self._install(monkeypatch, result=_session_result(warnings=["w"]))

        products = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert products.warnings == ["w"]

    def test_cache_resets_between_cycles(self, monkeypatch):
        """Each cycle logs in once, never reusing the previous cycle's data."""
        calls = self._install(monkeypatch, result=_session_result())

        for _ in range(2):
            asyncio.run(self.scraper.scrape_transactions())
            asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 2

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(SantanderBlockedError("blocked"), id="blocked"),
            pytest.param(SantanderLoginError("login"), id="login"),
            pytest.param(SantanderStepUpError("step-up"), id="step-up"),
        ],
    )
    def test_failed_login_not_retried_by_products(self, monkeypatch, exc):
        """The products leg re-raises the cached error without a second login."""
        calls = self._install(monkeypatch, exc=exc)

        with pytest.raises(type(exc)):
            asyncio.run(self.scraper.scrape_transactions())
        with pytest.raises(type(exc)):
            asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1

    def test_partial_coverage_reaches_products_warnings(self, monkeypatch):
        """Movements that did not load surface as a products warning."""
        result = _session_result(movements=[], warnings=["Santander: movements did not load"])
        self._install(monkeypatch, result=result)

        transactions = asyncio.run(self.scraper.scrape_transactions())
        products = asyncio.run(self.scraper.scrape_products())

        assert transactions == []
        assert len(products.products) == 1
        assert products.warnings == ["Santander: movements did not load"]

    def test_no_vista_emits_no_product(self, monkeypatch):
        """Without a vista the products leg is empty, keeping its warning."""
        result = _session_result(account=None, movements=[], warnings=["no vista"])
        self._install(monkeypatch, result=result)

        products = asyncio.run(self.scraper.scrape_products())

        assert products.products == []
        assert products.warnings == ["no vista"]

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
        """SANTANDER_HEADLESS=0 turns the browser visible; headless otherwise."""
        if value is None:
            monkeypatch.delenv("SANTANDER_HEADLESS", raising=False)
        else:
            monkeypatch.setenv("SANTANDER_HEADLESS", value)
        calls = self._install(monkeypatch, result=_session_result())

        asyncio.run(SantanderScraper().scrape_transactions())

        assert calls["headless"] == [expected]


class _Control:
    """A login-page control that is always visible and clickable."""

    first = None

    def __init__(self):
        self.first = self

    def wait_for(self, **kwargs):
        pass

    def click(self, timeout=0):
        pass

    def count(self):
        return 1


class _TypingFailsBox(_Control):
    def press_sequentially(self, text, delay=0):
        raise RuntimeError(f"failed typing {text!r}")


class _LoginFrame:
    def get_by_role(self, role, name=None, exact=False):
        return _TypingFailsBox()


class _PublicPage:
    url = "https://banco.santander.cl/personas"

    def goto(self, *args, **kwargs):
        pass

    def get_by_role(self, *args, **kwargs):
        return _Control()

    def locator(self, *args):
        return _Control()

    def frame_locator(self, selector):
        return _LoginFrame()


class _TypingBox(_Control):
    def press_sequentially(self, text, delay=0):
        pass


class _TypingFrame:
    def get_by_role(self, role, name=None, exact=False):
        return _TypingBox()


class _TextFrame:
    def __init__(self, page):
        self.page = page

    def evaluate(self, script):
        return self.page.iframe_text


class _Handle:
    def __init__(self, page):
        self.page = page

    def content_frame(self):
        return _TextFrame(self.page)


class _IframeControl(_Control):
    def __init__(self, page):
        super().__init__()
        self.page = page

    def element_handle(self, timeout=0):
        return _Handle(self.page)


class _LoginPage(_PublicPage):
    """The public site with a login iframe whose text changes after submit."""

    IDLE_TEXT = "RUT Clave ¿Olvidaste tu RUT o clave? Ingresar Conoce Santander Pass"

    def __init__(self, capture, after_text=IDLE_TEXT, products=True):
        self.capture = capture
        self.after_text = after_text
        self.products = products
        self.iframe_text = self.IDLE_TEXT
        self.polls = 0

    def locator(self, selector):
        return _IframeControl(self)

    def frame_locator(self, selector):
        return _TypingFrame()

    def wait_for_timeout(self, ms):
        self.polls += 1
        self.iframe_text = self.after_text
        if self.products and self.polls >= 3:
            self.capture.products.append(_products_payload([_account_row()]))


class TestLoginOutcome:
    def setup_method(self):
        self.capture = web.SessionCapture()

    def _login(self, page):
        web._login(page, self.capture, "00.000.000-0", "synthetic-password")

    def test_idle_form_words_are_not_an_error(self, monkeypatch):
        """Marker words already on the idle form never count as a reaction."""
        monkeypatch.setattr(web, "_LOGIN_RESULT_TIMEOUT_MS", 60_000)
        page = _LoginPage(self.capture)

        self._login(page)

        assert self.capture.products

    def test_new_error_banner_raises(self, monkeypatch):
        """An error banner that appears after submit is a login error."""
        page = _LoginPage(
            self.capture, after_text=_LoginPage.IDLE_TEXT + " Clave incorrecta", products=False
        )
        monkeypatch.setattr(web.time, "monotonic", _ticking_clock())

        with pytest.raises(SantanderLoginError):
            self._login(page)

    def test_new_step_up_prompt_raises(self, monkeypatch):
        """A second-factor prompt that appears after submit is a step-up."""
        page = _LoginPage(
            self.capture,
            after_text=_LoginPage.IDLE_TEXT + " Ingresa tu clave dinámica",
            products=False,
        )
        monkeypatch.setattr(web.time, "monotonic", _ticking_clock())

        with pytest.raises(SantanderStepUpError):
            self._login(page)


def _ticking_clock():
    """A monotonic clock that moves two seconds per call, so text checks run."""
    ticks = iter(range(0, 10_000, 2))
    return lambda: next(ticks)


class TestNewMarkers:
    def test_only_new_markers_count(self):
        """A marker present before and after submit is not new."""
        before = "¿Olvidaste tu RUT o clave? Santander Pass"
        after = before + " Clave incorrecta"

        assert web.new_markers(before, after, web._LOGIN_ERROR_MARKERS) == ["clave incorrecta"]
        assert web.new_markers(before, after, web._STEP_UP_MARKERS) == []


class TestSecretsNeverSurface:
    def test_login_failure_while_typing_drops_playwright_message(self):
        """A Playwright error quoting the typed text never reaches the run record."""
        with pytest.raises(SantanderLoginError) as info:
            web._login(_PublicPage(), web.SessionCapture(), "00.000.000-0", "synthetic-password")

        assert "synthetic-password" not in str(info.value)
        assert "00.000.000-0" not in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__

    def test_blocked_page_raises_before_typing(self):
        """A block page on the public site raises before any credential is typed."""
        capture = web.SessionCapture()
        capture.blocked = True

        with pytest.raises(SantanderBlockedError) as info:
            web._login(_PublicPage(), capture, "00.000.000-0", "synthetic-password")

        assert "http" not in str(info.value)


class TestUserAgent:
    @pytest.mark.parametrize(
        ("platform", "os_token"),
        [
            ("darwin", "Macintosh; Intel Mac OS X 10_15_7"),
            ("linux", "X11; Linux x86_64"),
            ("win32", "Windows NT 10.0; Win64; x64"),
            ("freebsd14", "X11; Linux x86_64"),
        ],
    )
    def test_matches_running_engine_and_host(self, platform, os_token):
        """The UA keeps the engine's major version and host OS, without "Headless"."""
        ua = web.user_agent_for("141.0.7390.37", platform)

        assert f"({os_token})" in ua
        assert "Chrome/141.0.0.0" in ua
        assert "Headless" not in ua
