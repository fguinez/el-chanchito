"""Tests for the Banco de Chile movements backend (issue #57).

Never launches a browser or hits the real bank: the pure helpers are fed
synthetic payload dicts shaped like the ones the portal returns, and the one
surface exercised end to end is driven with a fake page whose click emits a
canned response. Every identifier, amount, account and reference below is
fabricated (see the repo's personal-data policy).
"""

import datetime

from scrapers.backends import banchile_movements as movements_mod
from scrapers.backends.banchile_movements import (
    BanChileMovement,
    _read_card_unbilled,
    billed_reference,
    cartola_has_more_pages,
    dedupe_movements,
    detail_glosa_body,
    needs_detail_glosa,
    operation_id_from_detail,
    operation_id_from_glosa,
    parse_billed_movements,
    parse_cartola_movement,
    parse_unbilled_movement,
    parse_unbilled_movements,
    statement_dates,
)

# --- Synthetic payload builders ------------------------------------------------

ACCOUNT = "00-000-00000-01"
CARD_LAST4 = "1234"


def _cartola_row(
    descripcion="TRANSFERENCIA DE TERCERO",
    monto="1.000.000",
    tipo="abono",
    fecha="20260820 12:41:33",
    saldo="2.500.000",
    detalle_glosa=None,
    token="glosa-token-1",
):
    """One `getCartola` movement, with the bank's composite `id`."""
    return {
        "descripcion": descripcion,
        "monto": monto,
        "saldo": saldo,
        "nombreCuenta": "CUENTA CORRIENTE",
        "numeroCuenta": ACCOUNT,
        "idCuenta": "1",
        "canal": "INTERNET",
        "tipo": tipo,
        "fecha": fecha,
        "fechaContable": "20/08/2026",
        "id": f"CTD{ACCOUNT}:{fecha}:{monto.replace('.', '')}:{tipo}:1",
        "numeroDocumento": "",
        "fechaContableMovimiento": 1755000000000,
        "detalleGlosa": detalle_glosa if detalle_glosa is not None else [],
        "infoDataGlosaAdicional": token,
    }


def _unbilled_row(
    monto=999999,
    fecha="20/08/2026",
    hora="12:41:33",
    fecha_auth="20/08/2026",
    comercio="COMERCIO SINTETICO",
    tbk="000000000001",
):
    return {
        "origenTransaccion": "PRESENCIAL",
        "fechaTransaccion": 1755000000000,
        "fechaTransaccionString": fecha,
        "montoCompra": monto,
        "glosaTransaccion": "COMPRA NACIONAL",
        "codigoComercioTBK": tbk,
        "codigoComercioINT": "000001",
        "nombreComercio": comercio,
        "rubroComercio": "RETAIL",
        "codigoPaisComercio": "CL",
        "ciudad": "SANTIAGO",
        "fechaAutorizacion": 1755000000000,
        "horaAutorizacion": hora,
        "numeroTarjeta": CARD_LAST4,
        "descripcionTransaccion": "COMPRA",
        "numeroCuotas": 0,
        "numeroTotalCuotas": 0,
        "tipoTarjeta": "CREDITO",
        "fechaAutorizacionString": fecha_auth,
        "montoCompraString": "$ 999.999",
        "numeroTarjetaCompleto": None,
        "infoDataGlosaAdicional": "unbilled-token-1",
    }


def _billed_row(
    referencia="2008 12345678",
    monto=999999,
    fecha="20/08/2026",
    descripcion="COMERCIO SINTETICO",
    grupo="avancesCompras",
    totales=False,
):
    return {
        "numReferencia": referencia,
        "nombreTarjeta": f"VISA ****{CARD_LAST4}",
        "fechaTransaccion": 1755000000000,
        "fechaTransaccionString": fecha,
        "montoTransaccion": monto,
        "descripcion": descripcion,
        "ciudad": "SANTIAGO",
        "cuotas": "",
        "comercio": descripcion,
        "rubro": "RETAIL",
        "totales": totales,
        "grupo": grupo,
        "idMovimiento": "b64-blob-1",
        "idComprobante": "b64-blob-2",
    }


def _billed_payload(rows):
    return {
        "existeEstadoCuenta": True,
        "seccionOperaciones": {
            "totalTransacciones": len(rows),
            "numeroDeTransacciones": len(rows),
            "transaccionesTarjetas": rows,
        },
    }


class TestOperationIdFromGlosa:
    def test_reads_the_id_line(self):
        glosa = [
            "Rut Destinatario: 00.000.000-0",
            "Id transaccion: 12345678901",
            "Comentario: sin comentario",
        ]
        assert operation_id_from_glosa(glosa) == "12345678901"

    def test_tolerates_the_accent_and_casing(self):
        assert operation_id_from_glosa(["ID TRANSACCIÓN: TEF_IPE00000001"]) == (
            "TEF_IPE00000001"
        )

    def test_free_form_fija_glosa_yields_nothing(self):
        """The SPA's `detalleGlosa[0] == "Fija"` shape has no label/value lines."""
        assert operation_id_from_glosa(["Fija", "Cargo mensual del plan"]) is None

    def test_glosa_without_an_id_line(self):
        """Two of the 42 movements observed had a glosa but no id in it."""
        assert operation_id_from_glosa(["Banco: BANCO SINTETICO"]) is None

    def test_empty_and_missing(self):
        assert operation_id_from_glosa([]) is None
        assert operation_id_from_glosa(None) is None

    def test_never_reads_a_personal_field(self):
        """Only the id line is ever extracted, never a name, RUT or account."""
        glosa = ["Nombre Destinatario: PERSONA SINTETICA", "Cuenta Origen: 00-000-00000-01"]
        assert operation_id_from_glosa(glosa) is None


class TestOperationIdFromDetail:
    def test_reads_transaccion_id(self):
        assert operation_id_from_detail({"transaccionId": "TEFMBCO00000001"}) == (
            "TEFMBCO00000001"
        )

    def test_error_bodies_mean_no_id(self):
        """501 ("Glosa aun no implementada") and 503 are not failures."""
        body = {"codigo": "BFERR_505", "mensaje": "Glosa aun no implementada", "httpStatus": 501}
        assert operation_id_from_detail(body) is None
        assert operation_id_from_detail(None) is None

    def test_blank_id_is_absent(self):
        assert operation_id_from_detail({"transaccionId": "   "}) is None


class TestNeedsDetailGlosa:
    def test_empty_glosa_needs_the_call(self):
        assert needs_detail_glosa(_cartola_row()) is True

    def test_inline_glosa_does_not(self):
        """Those answer 501; asking skipped ~26 of 42 calls in the live window."""
        row = _cartola_row(detalle_glosa=["Id transaccion: 12345678901"])
        assert needs_detail_glosa(row) is False

    def test_missing_token_or_account_cannot_be_asked(self):
        row = _cartola_row(token="")
        assert needs_detail_glosa(row) is False
        row = _cartola_row()
        row["numeroCuenta"] = ""
        assert needs_detail_glosa(row) is False

    def test_body_carries_only_the_token_and_the_account(self):
        assert detail_glosa_body(_cartola_row()) == {
            "infoDataGlosaAdicional": "glosa-token-1",
            "numeroCuenta": ACCOUNT,
        }


class TestParseCartolaMovement:
    def test_abono_is_income(self):
        movement = parse_cartola_movement(_cartola_row(tipo="abono", monto="1.000.000"))
        assert movement.amount == 1000000
        assert movement.product_kind == "checking"
        assert movement.transaction_date == datetime.date(2026, 8, 20)

    def test_cargo_is_an_expense(self):
        movement = parse_cartola_movement(_cartola_row(tipo="cargo", monto="999.999"))
        assert movement.amount == -999999

    def test_unknown_tipo_is_skipped_not_guessed(self):
        """`monto` is unsigned, so an unreadable direction must import nothing."""
        assert parse_cartola_movement(_cartola_row(tipo="reverso")) is None

    def test_falls_back_to_fecha_contable(self):
        row = _cartola_row()
        row["fecha"] = ""
        assert parse_cartola_movement(row).transaction_date == datetime.date(2026, 8, 20)

    def test_fingerprint_is_the_composite_id_plus_saldo(self):
        row = _cartola_row(saldo="2.500.000")
        movement = parse_cartola_movement(row)
        assert movement.fingerprint == (row["id"], "2.500.000")
        assert movement.operation_id is None

    def test_operation_id_rides_along(self):
        movement = parse_cartola_movement(_cartola_row(), operation_id="12345678901")
        assert movement.operation_id == "12345678901"

    def test_nothing_to_key_on_is_dropped(self):
        row = _cartola_row()
        row["id"] = ""
        row["saldo"] = ""
        assert parse_cartola_movement(row) is None

    def test_unparseable_amount_is_dropped(self):
        assert parse_cartola_movement(_cartola_row(monto="sin dato")) is None


class TestCartolaPaging:
    def test_more_pages_is_reported(self):
        payload = {"movimientos": [], "pagina": [{"masPaginas": True}]}
        assert cartola_has_more_pages(payload) is True

    def test_single_page(self):
        payload = {"movimientos": [], "pagina": [{"masPaginas": False}]}
        assert cartola_has_more_pages(payload) is False
        assert cartola_has_more_pages({"movimientos": []}) is False


class TestParseUnbilled:
    def test_charge_is_negative(self):
        movement = parse_unbilled_movement(_unbilled_row(monto=999999))
        assert movement.amount == -999999
        assert movement.product_kind == "credit_card"
        assert movement.transaction_date == datetime.date(2026, 8, 20)

    def test_negative_monto_is_a_payment(self):
        """The SPA maps a negative `montoCompra` to a `montoPago`."""
        assert parse_unbilled_movement(_unbilled_row(monto=-2500000)).amount == 2500000

    def test_no_operation_id_on_this_leg(self):
        assert parse_unbilled_movement(_unbilled_row()).operation_id is None

    def test_fingerprint_is_description_free(self):
        movement = parse_unbilled_movement(_unbilled_row())
        assert movement.fingerprint == (
            "20/08/2026",
            "20/08/2026",
            "12:41:33",
            "999999",
            CARD_LAST4,
            "000000000001",
        )
        assert "COMERCIO" not in "|".join(movement.fingerprint)

    def test_whole_number_amount_renders_the_same_either_way(self):
        """A cosmetic 999999 vs 999999.0 in the payload must not re-key a row."""
        as_int = parse_unbilled_movement(_unbilled_row(monto=999999))
        as_float = parse_unbilled_movement(_unbilled_row(monto=999999.0))
        assert as_int.fingerprint == as_float.fingerprint

    def test_missing_date_is_dropped(self):
        assert parse_unbilled_movement(_unbilled_row(fecha="")) is None

    def test_payload_list(self):
        payload = {"listaMovNoFactur": [_unbilled_row(), _unbilled_row(monto=2500000)]}
        assert len(parse_unbilled_movements(payload)) == 2
        assert parse_unbilled_movements({}) == []


class TestBilledReference:
    def test_reads_the_reference(self):
        assert billed_reference("2008 12345678") == "2008 12345678"

    def test_all_zero_suffix_is_no_reference(self):
        """Observed on a payment row; keying on it would merge every such row."""
        assert billed_reference("2008 00000000") is None
        assert billed_reference("000000000000") is None

    def test_blank_and_missing(self):
        assert billed_reference("") is None
        assert billed_reference(None) is None


class TestParseBilled:
    def test_totales_rows_are_dropped(self):
        rows = [
            _billed_row(referencia="2008 12345678"),
            _billed_row(referencia="", totales=True, descripcion="TOTAL COMPRAS"),
            _billed_row(referencia="2108 12345679"),
        ]
        parsed = parse_billed_movements(_billed_payload(rows))
        assert [m.operation_id for m in parsed] == ["2008 12345678", "2108 12345679"]

    def test_reference_is_the_operation_id(self):
        parsed = parse_billed_movements(_billed_payload([_billed_row()]))
        assert parsed[0].operation_id == "2008 12345678"
        assert parsed[0].amount == -999999

    def test_all_zero_reference_falls_back_to_a_fingerprint(self):
        rows = [_billed_row(referencia="2008 00000000", monto=-2500000, grupo="pagos")]
        parsed = parse_billed_movements(_billed_payload(rows), "2026-08-25")
        assert parsed[0].operation_id is None
        assert parsed[0].fingerprint == (
            "20/08/2026",
            "-2500000",
            f"VISA ****{CARD_LAST4}",
            "2026-08-25",
        )
        assert parsed[0].amount == 2500000

    def test_fingerprint_excludes_the_merchant_and_the_group(self):
        rows = [_billed_row(referencia="2008 00000000", descripcion="COMERCIO SINTETICO")]
        renamed = [_billed_row(referencia="2008 00000000", descripcion="COMERCIO SINTETICO S.A.")]
        assert parse_billed_movements(_billed_payload(rows))[0].fingerprint == (
            parse_billed_movements(_billed_payload(renamed))[0].fingerprint
        )

    def test_empty_section(self):
        assert parse_billed_movements({"seccionOperaciones": {}}) == []
        assert parse_billed_movements(None) == []


class TestStatementDates:
    def test_newest_first_and_bounded(self):
        payload = {
            "listaNacional": [
                {"fechaFacturacion": "2026-08-05", "existeEstadoCuentaNacional": True},
                {"fechaFacturacion": "2026-07-05", "existeEstadoCuentaNacional": True},
                {"fechaFacturacion": "2026-06-05", "existeEstadoCuentaNacional": True},
            ]
        }
        assert statement_dates(payload, 2) == ["2026-08-05", "2026-07-05"]

    def test_periods_without_a_statement_are_skipped(self):
        payload = {
            "listaNacional": [
                {"fechaFacturacion": "2026-08-05", "existeEstadoCuentaNacional": False},
                {"fechaFacturacion": "2026-07-05", "existeEstadoCuentaNacional": True},
            ]
        }
        assert statement_dates(payload, 2) == ["2026-07-05"]

    def test_empty(self):
        assert statement_dates({}, 2) == []
        assert statement_dates(None, 2) == []


class TestDedupeMovements:
    def _movement(self, operation_id=None, fingerprint=("a", "b")):
        return BanChileMovement(
            source="checking",
            product_kind="checking",
            description="TRANSFERENCIA DE TERCERO",
            amount=1000000,
            transaction_date=datetime.date(2026, 8, 20),
            operation_id=operation_id,
            fingerprint=fingerprint,
        )

    def test_identical_reads_collapse(self):
        assert len(dedupe_movements([self._movement(), self._movement()])) == 1

    def test_distinct_identities_survive(self):
        pair = [self._movement(operation_id="1"), self._movement(operation_id="2")]
        assert len(dedupe_movements(pair)) == 2

    def test_distinct_fingerprints_survive(self):
        pair = [self._movement(fingerprint=("a", "1")), self._movement(fingerprint=("a", "2"))]
        assert len(dedupe_movements(pair)) == 2


# --- One surface, end to end, on a fake page ----------------------------------


class _FakeResponse:
    def __init__(self, url, payload, status=200):
        self.url = url
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeLocator:
    def __init__(self, page, matches):
        self.page = page
        self.matches = matches

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    def count(self):
        return 1 if self.matches else 0

    def wait_for(self, **kwargs):
        if not self.matches:
            raise RuntimeError("not visible")

    def click(self):
        if not self.matches:
            raise RuntimeError("not clickable")
        self.page.fire_card_response()


class _FakeCardPage:
    """The card page: clicking the shortcut makes the SPA load its movements."""

    def __init__(self, payload, url_fragment="movimientos-no-facturados"):
        self.url = "https://portalpersonas.bancochile.cl/mibancochile-web/#/home"
        self.payload = payload
        self.url_fragment = url_fragment
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def fire_card_response(self):
        response = _FakeResponse(
            f"https://portalpersonas.bancochile.cl/x/{self.url_fragment}", self.payload
        )
        for handler in list(self.handlers.get("response", [])):
            handler(response)

    def locator(self, selector):
        return _FakeLocator(self, "SALDOS Y MOV.TARJETAS" in selector)

    def evaluate(self, script, arg=None):
        return ""

    def wait_for_timeout(self, ms):
        return None

    def goto(self, url, **kwargs):
        self.url = url


class TestUnbilledSurface:
    def test_captures_the_spas_own_response(self):
        """The unbilled leg composes nothing: it reads what the card page loads."""
        page = _FakeCardPage({"listaMovNoFactur": [_unbilled_row(), _unbilled_row(monto=2500000)]})

        found = _read_card_unbilled(page, attempt=0)

        assert [m.amount for m in found] == [-999999, -2500000]

    def test_a_page_that_never_loads_yields_nothing(self, monkeypatch):
        """A surface that stays empty is reported by the caller, never raised."""
        monkeypatch.setattr(movements_mod, "_RENDER_TIMEOUTS_MS", (0,))
        page = _FakeCardPage({"listaMovNoFactur": []}, url_fragment="otra-cosa")

        assert _read_card_unbilled(page, attempt=0) == []
