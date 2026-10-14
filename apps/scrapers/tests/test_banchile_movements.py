"""Tests for the Banco de Chile movements backend (issue #57).

Never launches a browser or hits the real bank: the pure helpers are fed
synthetic payload dicts shaped like the ones the portal returns, and the one
surface exercised end to end is driven with a fake page whose click emits a
canned response. Every identifier, amount, account and reference below is
fabricated (see the repo's personal-data policy).
"""

import datetime
from decimal import Decimal
from unittest.mock import MagicMock

from scrapers.backends import banchile_movements as movements_mod
from scrapers.backends.banchile_movements import (
    BanChileMovement,
    _read_card_billed,
    _read_card_billed_usd,
    _read_card_unbilled,
    _usd_from_text,
    billed_reference,
    cartola_account_number,
    cartola_has_more_pages,
    dedupe_movements,
    drop_unbilled_duplicates,
    detail_glosa_body,
    international_statement_dates,
    is_international,
    needs_detail_glosa,
    operation_id_from_detail,
    operation_id_from_glosa,
    parse_billed_movements,
    parse_cartola_movement,
    parse_international_billed_movements,
    parse_unbilled_movement,
    parse_unbilled_movements,
    read_movement_surfaces,
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
        "fechaContable": "20/08/2026",  # same day as `fecha` in this fixture
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
    origen="NAC",
):
    """One unbilled row; `origen="INT"` makes it a USD row (synthetic)."""
    return {
        "origenTransaccion": origen,
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
        """A non-empty glosa with no id line is a real, recurring shape."""
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
        """Those answer 501; asking skipped most of the calls in the window."""
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

    def test_the_string_flag_is_compared(self):
        """"N" is a non-empty string, which is not "more pages"."""
        assert cartola_has_more_pages({"pagina": [{"masPaginas": "N"}]}) is False
        assert cartola_has_more_pages({"pagina": [{"masPaginas": "S"}]}) is True


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

    def test_a_page_that_never_loads_is_a_failure(self, monkeypatch):
        """None, so the caller retries and then reports the surface."""
        monkeypatch.setattr(movements_mod, "_RENDER_TIMEOUTS_MS", (0,))
        page = _FakeCardPage({"listaMovNoFactur": []}, url_fragment="otra-cosa")

        assert _read_card_unbilled(page, attempt=0) is None

    def test_a_card_with_no_unbilled_charges_is_not_a_failure(self):
        """Right after the statement is paid the list is legitimately empty."""
        page = _FakeCardPage({"listaMovNoFactur": []})

        assert _read_card_unbilled(page, attempt=0) == []


class TestCartolaDates:
    """`transaction_date` is when it happened; `accounting_date` when it posted."""

    def test_both_dates_are_kept(self):
        """The bank reports two and they usually differ; neither is discarded."""
        row = _cartola_row(fecha="20260821 15:48:28")
        row["fechaContable"] = "24/08/2026"

        movement = parse_cartola_movement(row)

        assert movement.transaction_date == datetime.date(2026, 8, 21)
        assert movement.accounting_date == datetime.date(2026, 8, 24)

    def test_a_missing_posting_date_leaves_it_null(self):
        row = _cartola_row(fecha="20260821 15:48:28")
        row["fechaContable"] = ""

        movement = parse_cartola_movement(row)

        assert movement.transaction_date == datetime.date(2026, 8, 21)
        assert movement.accounting_date is None

    def test_only_a_posting_date_fills_both(self):
        """An occurrence date is never invented; the known date fills in."""
        row = _cartola_row(fecha="")
        row["fechaContable"] = "24/08/2026"

        movement = parse_cartola_movement(row)

        assert movement.transaction_date == datetime.date(2026, 8, 24)
        assert movement.accounting_date == datetime.date(2026, 8, 24)

    def test_no_date_at_all_is_dropped(self):
        row = _cartola_row(fecha="")
        row["fechaContable"] = ""

        assert parse_cartola_movement(row) is None

    def test_the_card_legs_report_no_posting_date(self):
        """`fechaTransaccion` is when the charge happened; nothing posts it."""
        unbilled = parse_unbilled_movement(_unbilled_row())
        billed = parse_billed_movements(_billed_payload([_billed_row()]), "2026-08-25")[0]

        assert unbilled.transaction_date == datetime.date(2026, 8, 20)
        assert unbilled.accounting_date is None
        assert billed.transaction_date == datetime.date(2026, 8, 20)
        assert billed.accounting_date is None

    def test_neither_date_reaches_an_identity(self):
        """Dates are recorded data: keying on one is the bug #57 removed."""
        row = _cartola_row(fecha="20260821 15:48:28")
        row["fechaContable"] = "24/08/2026"
        shifted = _cartola_row(fecha="20260821 15:48:28")
        shifted["fechaContable"] = "25/08/2026"

        assert parse_cartola_movement(row).fingerprint == (
            parse_cartola_movement(shifted).fingerprint
        )
        assert len(dedupe_movements(
            [parse_cartola_movement(row), parse_cartola_movement(shifted)]
        )) == 1


class TestAllZeroOperationId:
    """A placeholder id must never become a key every movement shares."""

    def test_all_zero_inline_id_is_absent(self):
        assert operation_id_from_glosa(["Id transaccion: 00000000000"]) is None

    def test_all_zero_detail_id_is_absent(self):
        assert operation_id_from_detail({"transaccionId": "00000000000"}) is None

    def test_a_real_id_containing_zeros_survives(self):
        assert operation_id_from_detail({"transaccionId": "TEF_IPE00000001"}) == (
            "TEF_IPE00000001"
        )


class TestBilledSigns:
    """The billed leg does not sign its amounts; `grupo` carries the direction."""

    def _amount(self, **kwargs):
        rows = [_billed_row(**kwargs)]
        return parse_billed_movements(_billed_payload(rows))[0].amount

    def test_a_purchase_is_an_expense(self):
        assert self._amount(monto=999999, grupo="avancesCompras") == -999999

    def test_a_positive_pagos_row_is_income(self):
        """The statement's payment is an unsigned magnitude like every row.

        Negating it would import the month's card payment, typically the
        largest line on the statement, as a charge.
        """
        assert self._amount(monto=2500000, grupo="pagos", referencia="2008 00000000") == (
            2500000
        )

    def test_a_negative_pagos_row_is_still_income(self):
        assert self._amount(monto=-2500000, grupo="pagos") == 2500000

    def test_the_unbilled_leg_still_signs_its_own_amounts(self):
        """`montoCompra` genuinely carries negatives, so the sign is flipped."""
        assert parse_unbilled_movement(_unbilled_row(monto=999999)).amount == -999999
        assert parse_unbilled_movement(_unbilled_row(monto=-2500000)).amount == 2500000


class TestDropUnbilledDuplicates:
    """A charge visible on both card legs in one scrape must yield one row."""

    def _card(self, source, amount=-999999, operation_id=None, fingerprint=("a",)):
        return BanChileMovement(
            source=source,
            product_kind="credit_card",
            description="COMERCIO SINTETICO",
            amount=amount,
            transaction_date=datetime.date(2026, 8, 20),
            operation_id=operation_id,
            fingerprint=fingerprint,
        )

    def test_the_billed_row_wins(self):
        """It carries the bank's reference; the unbilled one has no id at all."""
        movements = [
            self._card("card_unbilled", fingerprint=("fp",)),
            self._card("card_billed", operation_id="2008 12345678"),
        ]

        kept = drop_unbilled_duplicates(movements)

        assert [m.source for m in kept] == ["card_billed"]

    def test_matching_is_one_to_one(self):
        """Two identical unbilled charges, one billed: one survives unbilled."""
        movements = [
            self._card("card_unbilled", fingerprint=("fp-1",)),
            self._card("card_unbilled", fingerprint=("fp-2",)),
            self._card("card_billed", operation_id="2008 12345678"),
        ]

        kept = drop_unbilled_duplicates(movements)

        assert [m.source for m in kept] == ["card_unbilled", "card_billed"]

    def test_a_different_amount_is_a_different_charge(self):
        movements = [
            self._card("card_unbilled", amount=-2500000, fingerprint=("fp",)),
            self._card("card_billed", operation_id="2008 12345678"),
        ]

        assert len(drop_unbilled_duplicates(movements)) == 2

    def test_checking_movements_are_never_touched(self):
        movements = [
            BanChileMovement(
                source="checking",
                product_kind="checking",
                description="TRANSFERENCIA DE TERCERO",
                amount=-999999,
                transaction_date=datetime.date(2026, 8, 20),
                fingerprint=("a", "b"),
            ),
            self._card("card_billed", operation_id="2008 12345678"),
        ]

        assert len(drop_unbilled_duplicates(movements)) == 2

    def test_without_a_billed_leg_nothing_is_dropped(self):
        movements = [self._card("card_unbilled", fingerprint=("fp",))]
        assert drop_unbilled_duplicates(movements) == movements


class TestCartolaAccountNumber:
    """What the account loop compares so it can't silently re-read an account."""

    def test_reads_it_off_the_movements(self):
        payload = {"movimientos": [_cartola_row()]}
        assert cartola_account_number(payload) == ACCOUNT

    def test_empty_payloads(self):
        assert cartola_account_number({"movimientos": []}) == ""
        assert cartola_account_number(None) == ""


# --- Issue #79: the USD side of the same products ------------------------------
#
# Every USD figure below is synthetic (1234.56, 12.30, ...), as are the card
# last-4 and every reference.


class TestUnbilledCurrency:
    """`origenTransaccion` is the only currency mark on the unbilled leg."""

    def test_an_int_row_is_usd_with_exact_cents(self):
        movement = parse_unbilled_movement(_unbilled_row(monto=1234.56, origen="INT"))

        assert movement.currency == "USD"
        assert movement.amount == Decimal("-1234.56")
        assert str(movement.amount) == "-1234.56"

    def test_a_nac_row_stays_whole_clp(self):
        movement = parse_unbilled_movement(_unbilled_row(monto=999999, origen="NAC"))

        assert movement.currency == "CLP"
        assert movement.amount == -999999

    def test_anything_but_int_stays_clp(self):
        """What every row was before #79; only "INT" is known to mean USD."""
        assert parse_unbilled_movement(_unbilled_row(origen="")).currency == "CLP"
        assert parse_unbilled_movement(_unbilled_row(origen="OTRO")).currency == "CLP"
        assert is_international(_unbilled_row(origen="int")) is True

    def test_the_merchant_currency_fields_are_ignored(self):
        """840 was reported on rows whose origin figure was in pesos."""
        row = _unbilled_row(monto=999999, origen="NAC")
        row["codigoMonedaOrigen"] = "840"
        assert parse_unbilled_movement(row).currency == "CLP"

    def test_a_usd_payment_is_income(self):
        """Same sign convention as the CLP rows: negative is a payment."""
        movement = parse_unbilled_movement(_unbilled_row(monto=-1234.56, origen="INT"))
        assert movement.amount == Decimal("1234.56")

    def test_float_noise_never_reaches_the_cents(self):
        movement = parse_unbilled_movement(
            _unbilled_row(monto=0.1 + 0.2, origen="INT")
        )
        assert movement.amount == Decimal("-0.30")

    def test_usd_fingerprint_renders_the_amount_canonically(self):
        """12.3, 12.30 and the same figure as a whole number never re-key."""
        tenths = parse_unbilled_movement(_unbilled_row(monto=12.3, origen="INT"))
        whole = parse_unbilled_movement(_unbilled_row(monto=12, origen="INT"))
        whole_float = parse_unbilled_movement(_unbilled_row(monto=12.0, origen="INT"))

        assert tenths.fingerprint[3] == "12.30"
        assert whole.fingerprint == whole_float.fingerprint
        assert whole.fingerprint[3] == "12.00"

    def test_a_usd_row_records_what_the_clp_only_code_stored(self):
        """Its key fields and its amount, rounded the way that code rounded."""
        movement = parse_unbilled_movement(_unbilled_row(monto=1234.56, origen="INT"))

        assert movement.legacy_fingerprint == (
            "20/08/2026",
            "20/08/2026",
            "12:41:33",
            "1234.56",
            CARD_LAST4,
            "000000000001",
        )
        assert movement.legacy_amount == -1235

    def test_legacy_rounding_is_bankers_rounding(self):
        """Python's round(12.5) is 12; the legacy rows hold exactly that."""
        assert parse_unbilled_movement(
            _unbilled_row(monto=12.5, origen="INT")
        ).legacy_amount == -12
        assert parse_unbilled_movement(
            _unbilled_row(monto=13.5, origen="INT")
        ).legacy_amount == -14

    def test_clp_rows_carry_no_legacy_fields(self):
        movement = parse_unbilled_movement(_unbilled_row())
        assert movement.legacy_fingerprint == ()
        assert movement.legacy_amount is None

    def test_one_list_holds_both_currencies(self):
        payload = {
            "listaMovNoFactur": [
                _unbilled_row(monto=999999),
                _unbilled_row(monto=1234.56, origen="INT"),
            ]
        }
        assert [m.currency for m in parse_unbilled_movements(payload)] == [
            "CLP",
            "USD",
        ]


class TestBilledReferenceShapes:
    def test_a_usd_purchase_reference_is_kept(self):
        reference = "2008 " + "1" * 23
        assert billed_reference(reference) == reference

    def test_a_bare_ddmm_is_no_reference(self):
        """A posting day on payment and commission rows; every one would share it."""
        assert billed_reference("2008") is None
        assert billed_reference(" 2008 ") is None


def _intl_row(
    numero_documento="2008 " + "1" * 23,
    monto=1234.56,
    fecha="20/08/2026",
    descripcion="COMERCIO SINTETICO",
    grupo="generico",
    totales=False,
):
    """One USD statement row, every value synthetic."""
    return {
        "numeroDocumento": numero_documento,
        "fechaTransaccion": None if totales else 1755000000000,
        "fechaTransaccionString": None if totales else fecha,
        "descripcion": descripcion,
        "codigoPais": None,
        "ciudadComercio": "CIUDAD SINTETICA",
        "montoDolar": monto,
        "montoMonedaOrigen": monto,
        "tituloTarjeta": f"****{CARD_LAST4}",
        "nombreTitular": "PERSONA SINTETICA",
        "totales": totales,
        "grupo": grupo,
        "idMovimiento": "session-token-1",
        "idComprobante": "session-token-2",
        "montoDolarTemp": monto,
    }


def _intl_section(rows, count=None):
    return {
        "totalTransacciones": 0,
        "numeroDeTransacciones": len(rows or []) + 1 if count is None else count,
        "mensajeSinTransacciones": None if rows else "Sin transacciones",
        "transaccionesTarjetas": rows,
    }


def _intl_payload(pagos=None, compras=None, comisiones=None):
    return {
        "existeEstadoCuenta": True,
        "resumen": {"montoFacturado": 1234.56},
        "seccionPagos": _intl_section(pagos),
        "seccionCompras": _intl_section(compras),
        "seccionComisiones": _intl_section(comisiones),
    }


class TestInternationalBilled:
    def test_every_section_is_read(self):
        payload = _intl_payload(
            pagos=[_intl_row(numero_documento="2008", monto=-1234.56, grupo="pagos")],
            compras=[_intl_row(monto=12.3)],
            comisiones=[_intl_row(numero_documento="2108", monto=-1.5)],
        )

        parsed = parse_international_billed_movements(payload, "2026-08-25")

        assert [m.amount for m in parsed] == [
            Decimal("1234.56"),
            Decimal("-12.30"),
            Decimal("1.50"),
        ]
        assert {m.currency for m in parsed} == {"USD"}
        assert {m.source for m in parsed} == {"card_billed"}
        assert {m.product_kind for m in parsed} == {"credit_card"}

    def test_the_sign_comes_from_the_amount_not_the_section(self):
        """A commissions row was a credit; `grupo` says nothing about direction."""
        payload = _intl_payload(
            comisiones=[_intl_row(numero_documento="2108", monto=-1.5)],
            compras=[_intl_row(monto=1.5, grupo="pagos")],
        )
        amounts = [m.amount for m in parse_international_billed_movements(payload)]
        assert sorted(amounts) == [Decimal("-1.50"), Decimal("1.50")]

    def test_a_null_section_is_empty_not_an_error(self):
        payload = _intl_payload(compras=[_intl_row()])
        assert payload["seccionComisiones"]["transaccionesTarjetas"] is None

        assert len(parse_international_billed_movements(payload)) == 1

    def test_missing_sections_and_bad_payloads(self):
        assert parse_international_billed_movements({"existeEstadoCuenta": False}) == []
        assert parse_international_billed_movements(None) == []

    def test_totales_rows_are_dropped(self):
        payload = _intl_payload(
            compras=[
                _intl_row(),
                _intl_row(numero_documento="", totales=True, grupo="TOTAL DE COMPRAS"),
            ]
        )
        assert len(parse_international_billed_movements(payload)) == 1

    def test_the_row_count_field_is_never_trusted(self):
        """`numeroDeTransacciones` was off by one even on empty sections."""
        payload = _intl_payload(compras=[_intl_row()])
        payload["seccionCompras"]["numeroDeTransacciones"] = 7
        assert len(parse_international_billed_movements(payload)) == 1

    def test_a_purchase_is_keyed_on_its_reference(self):
        (movement,) = parse_international_billed_movements(
            _intl_payload(compras=[_intl_row()])
        )
        assert movement.operation_id == "2008 " + "1" * 23

    def test_payments_fall_back_to_a_fingerprint(self):
        payload = _intl_payload(
            pagos=[
                _intl_row(numero_documento="2008", monto=-1234.56, grupo="pagos"),
                _intl_row(numero_documento="2108 00000000", monto=-12.3, grupo="pagos"),
            ]
        )
        first, second = parse_international_billed_movements(payload, "2026-08-25")

        assert first.operation_id is None and second.operation_id is None
        assert first.fingerprint == (
            "20/08/2026",
            "-1234.56",
            f"****{CARD_LAST4}",
            "2026-08-25",
        )
        assert second.fingerprint[1] == "-12.30"

    def test_no_date_comes_from_the_ddmm_prefix(self):
        row = _intl_row(numero_documento="0101 " + "1" * 23, fecha="20/08/2026")
        (movement,) = parse_international_billed_movements(_intl_payload(compras=[row]))
        assert movement.transaction_date == datetime.date(2026, 8, 20)
        assert movement.accounting_date is None

    def test_session_tokens_never_reach_the_identity(self):
        row = _intl_row(numero_documento="2008")
        other = dict(row, idMovimiento="session-token-9", idComprobante="token-9")
        parse = parse_international_billed_movements
        assert (
            parse(_intl_payload(pagos=[row]))[0].fingerprint
            == parse(_intl_payload(pagos=[other]))[0].fingerprint
        )

    def test_records_the_legacy_clp_amount(self):
        (movement,) = parse_international_billed_movements(
            _intl_payload(compras=[_intl_row(monto=1234.56)])
        )
        assert movement.legacy_amount == -1235
        assert movement.legacy_fingerprint == ()


class TestInternationalStatementDates:
    def test_reads_the_international_list_and_its_flag(self):
        payload = {
            "listaNacional": [
                {"fechaFacturacion": "2026-09-05", "existeEstadoCuentaNacional": "1"},
            ],
            "listaInternacional": [
                {"fechaFacturacion": "2026-08-05", "existeEstadoCuentaInternacional": "S"},
                {"fechaFacturacion": "2026-07-05", "existeEstadoCuentaInternacional": "N"},
                {"fechaFacturacion": "2026-06-05", "existeEstadoCuentaInternacional": "S"},
                {"fechaFacturacion": "2026-05-05", "existeEstadoCuentaInternacional": "S"},
            ],
        }
        assert international_statement_dates(payload, 2) == ["2026-08-05", "2026-06-05"]

    def test_empty(self):
        assert international_statement_dates({}, 2) == []
        assert international_statement_dates(None, 2) == []


class TestUsdDuplicates:
    """The USD legs share date and signed amount exactly; nothing else."""

    def _card(self, source, amount, currency="USD", **fields):
        return BanChileMovement(
            source=source,
            product_kind="credit_card",
            description="COMERCIO SINTETICO",
            amount=Decimal(amount),
            transaction_date=datetime.date(2026, 8, 20),
            currency=currency,
            **fields,
        )

    def test_a_usd_charge_on_both_legs_keeps_the_billed_row(self):
        movements = [
            self._card("card_unbilled", "-1234.56", fingerprint=("fp",)),
            self._card("card_billed", "-1234.56", operation_id="ref-1"),
        ]
        assert [m.source for m in drop_unbilled_duplicates(movements)] == ["card_billed"]

    def test_the_opposite_sign_is_a_different_movement(self):
        movements = [
            self._card("card_unbilled", "1234.56", fingerprint=("fp",)),
            self._card("card_billed", "-1234.56", operation_id="ref-1"),
        ]
        assert len(drop_unbilled_duplicates(movements)) == 2

    def test_currencies_never_match_each_other(self):
        movements = [
            self._card("card_unbilled", "-12", currency="CLP", fingerprint=("fp",)),
            self._card("card_billed", "-12.00", operation_id="ref-1"),
        ]
        assert len(drop_unbilled_duplicates(movements)) == 2

    def test_the_billed_row_inherits_the_legacy_key(self):
        """So the misrouted-row cleanup can still match the old row exactly."""
        movements = [
            self._card(
                "card_unbilled",
                "-1234.56",
                fingerprint=("fp",),
                legacy_fingerprint=("legacy",),
                legacy_amount=-1235,
            ),
            self._card("card_billed", "-1234.56", operation_id="ref-1", legacy_amount=-1235),
        ]
        (kept,) = drop_unbilled_duplicates(movements)
        assert kept.legacy_fingerprint == ("legacy",)
        assert kept.legacy_amount == -1235

    def test_dedupe_keeps_a_usd_and_a_clp_row_with_one_fingerprint(self):
        pair = [
            self._card("card_unbilled", "-12", currency="CLP", fingerprint=("fp",)),
            self._card("card_unbilled", "-12", fingerprint=("fp",)),
        ]
        assert len(dedupe_movements(pair)) == 2


class TestUsdCartola:
    """Row shape unverified (the USD account observed had no movements)."""

    def test_a_usd_cargo_keeps_its_cents(self):
        movement = parse_cartola_movement(
            _cartola_row(tipo="cargo", monto="1.234,56"), currency="USD"
        )
        assert movement.currency == "USD"
        assert movement.amount == Decimal("-1234.56")
        assert movement.product_kind == "checking"

    def test_the_identity_is_the_clp_one(self):
        row = _cartola_row(monto="1.234,56")
        assert parse_cartola_movement(row, currency="USD").fingerprint == (
            row["id"],
            "2.500.000",
        )

    def test_the_clp_path_still_rounds_to_whole_pesos(self):
        movement = parse_cartola_movement(_cartola_row(monto="1.000.000"))
        assert movement.currency == "CLP"
        assert movement.amount == 1000000

    def test_amount_formats(self):
        assert _usd_from_text("1.234,56") == Decimal("1234.56")
        assert _usd_from_text("1,234.56") == Decimal("1234.56")
        assert _usd_from_text("1234,56") == Decimal("1234.56")
        assert _usd_from_text("1234.56") == Decimal("1234.56")
        assert _usd_from_text("USD 12,3") == Decimal("12.30")
        assert _usd_from_text("1.234") == Decimal("1234.00")
        assert _usd_from_text(1234.56) == Decimal("1234.56")
        assert _usd_from_text("") is None
        assert _usd_from_text("sin dato") is None

    def test_an_empty_cartola_is_a_reading_not_a_failure(self):
        """The observed USD account answered with no movements at all."""
        payload = {"moneda": "USD", "movimientos": [], "pagina": [{"masPaginas": "N"}]}

        found, budget = movements_mod._movements_from_cartola(
            MagicMock(), payload, 5, "USD"
        )

        assert (found, budget) == ([], 5)


class TestInternationalSurface:
    """The USD statement is its own capture; the national one never sees it."""

    _FRAGMENT = "tarjetas/estadocuenta/internacional/resumen-por-fecha"

    def test_captures_the_spas_international_statement(self):
        page = _FakeCardPage(_intl_payload(compras=[_intl_row()]), self._FRAGMENT)

        found = _read_card_billed_usd(page, attempt=0)

        assert [(m.currency, m.amount) for m in found] == [("USD", Decimal("-1234.56"))]

    def test_the_national_leg_does_not_capture_it(self, monkeypatch):
        """`estadocuenta/nacional` is not a substring of the international URL."""
        monkeypatch.setattr(movements_mod, "_RENDER_TIMEOUTS_MS", (0,))
        page = _FakeCardPage(_intl_payload(compras=[_intl_row()]), self._FRAGMENT)

        assert _read_card_billed(page, attempt=0) is None


class TestSurfaceIsolation:
    def test_a_failed_usd_surface_costs_nothing_else(self, monkeypatch):
        clp = BanChileMovement(
            source="card_billed",
            product_kind="credit_card",
            description="COMERCIO SINTETICO",
            amount=Decimal(-999999),
            transaction_date=datetime.date(2026, 8, 20),
            operation_id="2008 12345678",
        )
        monkeypatch.setattr(
            movements_mod,
            "_MOVEMENT_READERS",
            [
                ("tarjeta facturados", lambda page, attempt: [clp]),
                ("tarjeta facturados USD", lambda page, attempt: None),
                ("movimientos USD", lambda page, attempt: []),
            ],
        )

        movements, failed = read_movement_surfaces(MagicMock())

        assert movements == [clp]
        assert failed == ("tarjeta facturados USD",)
