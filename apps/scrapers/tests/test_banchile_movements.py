"""Tests for the Banco de Chile movements backend (issue #57).

Never launches a browser or hits the real bank: the pure helpers are fed
synthetic payload dicts shaped like the ones the portal returns, and the one
surface exercised end to end is driven with a fake page whose click emits a
canned response. Every identifier, amount, account and reference below is
fabricated (see the repo's personal-data policy).
"""

import datetime
import time
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from scrapers.backends import banchile_movements as movements_mod
from scrapers.backends import banchile_web as banchile_web_mod
from scrapers.backends.banchile_movements import (
    BanChileMovement,
    card_location,
    merge_locations,
    _read_card_billed,
    _read_card_billed_usd,
    _read_card_unbilled,
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
    read_session,
    statement_dates,
)
from scrapers.backends.banchile_web import BalanceFetchResult, LoginCooldownError
from scrapers.base import (
    ProductRef,
    ScrapedExchangeRate,
    ScrapedProduct,
    TransactionLocation,
)
from scrapers.institutions.banchile import external_id_for

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
    ciudad="CIUDAD SINTETICA",
    pais="PAIS SINTETICO",
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
        # The portal puts the country's NAME under this key.
        "codigoPaisComercio": pais,
        "ciudad": ciudad,
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
    ciudad="CIUDAD SINTETICA",
):
    return {
        "numReferencia": referencia,
        "nombreTarjeta": f"VISA ****{CARD_LAST4}",
        "fechaTransaccion": 1755000000000,
        "fechaTransaccionString": fecha,
        "montoTransaccion": monto,
        "descripcion": descripcion,
        "ciudad": ciudad,
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


class TestSharedSession:
    """`_session_sync` authenticates through banchile_web's session cache."""

    def test_both_reads_run_inside_the_cached_session(self, monkeypatch):
        events = []
        page = MagicMock()

        @contextmanager
        def authenticated_session(browser, rut, password):
            events.append("authenticated")
            yield page
            events.append("session saved")

        @contextmanager
        def sync_playwright():
            yield MagicMock()

        def read_products(p):
            events.append("products")
            return BalanceFetchResult(products=[], failed_surfaces=("card",))

        def read_rate(p):
            events.append("rate")
            return [], ()

        def read_movements(p):
            events.append("movements")
            return [], ("tarjeta facturados",)

        monkeypatch.setattr("playwright.sync_api.sync_playwright", sync_playwright)
        monkeypatch.setattr(movements_mod, "_authenticated_session", authenticated_session)
        monkeypatch.setattr(movements_mod, "_dismiss_popup", lambda p: None)
        monkeypatch.setattr(movements_mod, "_read_all_surfaces", read_products)
        monkeypatch.setattr(movements_mod, "read_exchange_rate_surface", read_rate)
        monkeypatch.setattr(movements_mod, "read_movement_surfaces", read_movements)

        result = movements_mod._session_sync("11.111.111-1", "synthetic-password", True)

        # The post-read save sees the cookies every read may have rotated.
        assert events == [
            "authenticated",
            "products",
            "rate",
            "movements",
            "session saved",
        ]
        assert result.failed_surfaces == ("card", "tarjeta facturados")

    def test_a_certain_login_is_refused_before_launching_chromium(
        self, tmp_path, monkeypatch
    ):
        """A login attempt just now and no cached session: nothing to launch."""
        monkeypatch.setenv("BANCHILE_SESSION_FILE", str(tmp_path / ".banchile_session.json"))
        monkeypatch.delenv("BANCHILE_LOGIN_COOLDOWN_MINUTES", raising=False)
        monkeypatch.setattr(banchile_web_mod, "_last_login_attempt", time.monotonic())
        sync_playwright = MagicMock()
        monkeypatch.setattr("playwright.sync_api.sync_playwright", sync_playwright)

        with pytest.raises(LoginCooldownError):
            movements_mod._session_sync("11.111.111-1", "synthetic-password", True)

        sync_playwright.assert_not_called()


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
    ciudad="CIUDAD SINTETICA",
    pais="PAIS SINTETICO",
):
    """One USD statement row, every value synthetic."""
    return {
        "numeroDocumento": numero_documento,
        "fechaTransaccion": None if totales else 1755000000000,
        "fechaTransaccionString": None if totales else fecha,
        "descripcion": descripcion,
        "codigoPais": None,
        "ciudadComercio": ciudad,
        "pais": pais,
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
            "2008",
            "seccionPagos",
        )
        assert second.fingerprint[1] == "-12.30"
        assert second.fingerprint[4] == "2108 00000000"

    def test_a_payment_and_a_credit_alike_in_all_else_both_survive(self):
        """Same date, signed amount and card, neither with a usable reference."""
        payload = _intl_payload(
            pagos=[_intl_row(numero_documento="2008 00000000", monto=-12.3, grupo="pagos")],
            comisiones=[_intl_row(numero_documento="2008", monto=-12.3)],
        )

        parsed = dedupe_movements(parse_international_billed_movements(payload, "2026-08-25"))

        assert len(parsed) == 2
        assert {m.operation_id for m in parsed} == {None}
        assert len({external_id_for(m) for m in parsed}) == 2

    def test_the_section_tells_apart_rows_with_the_same_document(self):
        payload = _intl_payload(
            pagos=[_intl_row(numero_documento="2008", monto=-12.3, grupo="pagos")],
            comisiones=[_intl_row(numero_documento="2008", monto=-12.3)],
        )

        parsed = dedupe_movements(parse_international_billed_movements(payload, "2026-08-25"))

        assert [m.fingerprint[5] for m in parsed] == ["seccionPagos", "seccionComisiones"]

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
                ("tarjeta no facturados", lambda page, attempt: []),
            ],
        )

        movements, failed = read_movement_surfaces(MagicMock())

        assert movements == [clp]
        assert failed == ("tarjeta facturados USD",)


# Synthetic place: no real city or country.
_PLACE = TransactionLocation(city="CIUDAD SINTETICA", country="PAIS SINTETICO")


class TestCardLocation:
    """V025: the card legs carry the merchant's city and country."""

    def test_an_unbilled_row_reads_ciudad_and_the_country_name(self):
        assert parse_unbilled_movement(_unbilled_row()).location == _PLACE

    def test_a_usd_unbilled_row_carries_it_too(self):
        movement = parse_unbilled_movement(_unbilled_row(monto=12.34, origen="INT"))
        assert movement.location == _PLACE

    def test_a_national_billed_row_has_a_city_and_no_country(self):
        (movement,) = parse_billed_movements(_billed_payload([_billed_row()]))
        assert movement.location == TransactionLocation(city="CIUDAD SINTETICA")

    def test_a_usd_statement_row_reads_ciudad_comercio_and_pais(self):
        (movement,) = parse_international_billed_movements(
            _intl_payload(compras=[_intl_row()])
        )
        assert movement.location == _PLACE

    def test_codigo_pais_is_only_a_fallback(self):
        row = _intl_row(pais="")
        row["codigoPais"] = "XX"
        (movement,) = parse_international_billed_movements(_intl_payload(compras=[row]))
        assert movement.location.country == "XX"

    def test_blank_fields_mean_no_location(self):
        """Payment rows leave both fields empty."""
        (movement,) = parse_international_billed_movements(
            _intl_payload(pagos=[_intl_row(monto=-1234.56, ciudad="", pais=" ")])
        )
        assert movement.location is None
        assert parse_unbilled_movement(_unbilled_row(ciudad=None, pais="")).location is None

    def test_values_are_trimmed_and_not_recased(self):
        location = card_location("  Ciudad Sintetica ", "PAIS SINTETICO ")
        assert location == TransactionLocation(
            city="Ciudad Sintetica", country="PAIS SINTETICO"
        )

    def test_an_unstorable_value_drops_the_location_not_the_movement(self):
        movement = parse_unbilled_movement(_unbilled_row(ciudad="X" * 500))
        assert movement is not None
        assert movement.location is None

    def test_location_never_reaches_an_identity(self):
        here = parse_unbilled_movement(_unbilled_row())
        there = parse_unbilled_movement(_unbilled_row(ciudad="OTRA", pais="OTRO"))
        assert external_id_for(here) == external_id_for(there)

    def test_a_dropped_unbilled_twin_hands_over_its_location(self):
        unbilled = parse_unbilled_movement(_unbilled_row(monto=1234.56, origen="INT"))
        (billed,) = parse_international_billed_movements(
            _intl_payload(compras=[_intl_row(ciudad="", pais="")])
        )

        (kept,) = drop_unbilled_duplicates([unbilled, billed])

        assert kept.source == "card_billed"
        assert kept.location == _PLACE

    def test_a_dropped_twin_fills_the_billed_rows_gaps_field_by_field(self):
        """The billed leg wins per field; the unbilled one only fills gaps."""
        unbilled = parse_unbilled_movement(
            _unbilled_row(
                monto=1234.56, origen="INT", ciudad="OTRA", pais="PAIS SINTETICO"
            )
        )
        (billed,) = parse_international_billed_movements(
            _intl_payload(compras=[_intl_row(ciudad="CIUDAD SINTETICA", pais="")])
        )

        (kept,) = drop_unbilled_duplicates([unbilled, billed])

        assert kept.location == _PLACE

    def test_a_billed_location_wins_over_its_unbilled_twin(self):
        unbilled = parse_unbilled_movement(
            _unbilled_row(monto=1234.56, origen="INT", ciudad="OTRA")
        )
        (billed,) = parse_international_billed_movements(
            _intl_payload(compras=[_intl_row()])
        )

        (kept,) = drop_unbilled_duplicates([unbilled, billed])

        assert kept.location == _PLACE


class TestMergeLocations:
    """Field-by-field merge; coordinates travel as a pair."""

    def test_either_side_missing(self):
        assert merge_locations(None, None) is None
        assert merge_locations(_PLACE, None) == _PLACE
        assert merge_locations(None, _PLACE) == _PLACE

    def test_primary_text_wins_and_gaps_are_filled(self):
        merged = merge_locations(
            TransactionLocation(city="CIUDAD SINTETICA"),
            TransactionLocation(name="TIENDA", city="OTRA", country="PAIS SINTETICO"),
        )
        assert merged == TransactionLocation(
            name="TIENDA", city="CIUDAD SINTETICA", country="PAIS SINTETICO"
        )

    def test_coordinates_are_never_mixed(self):
        primary = TransactionLocation(latitude=-33.0, longitude=-70.0)
        fallback = TransactionLocation(latitude=-34.0, longitude=-71.0)
        assert merge_locations(primary, fallback) == primary
        merged = merge_locations(TransactionLocation(city="X"), fallback)
        assert (merged.latitude, merged.longitude) == (-34.0, -71.0)


class TestReadSession:
    """One page, every read: products, then the USD card rate, then movements."""

    def _patch(self, monkeypatch, calls, rates=(), rate_failed=()):
        product = ScrapedProduct(
            institution="banchile", kind="credit_card", currency="USD"
        )
        movement = BanChileMovement(
            source="card_billed",
            product_kind="credit_card",
            description="COMERCIO SINTETICO",
            amount=Decimal("-1234.56"),
            transaction_date=datetime.date(2026, 8, 20),
            currency="USD",
        )

        def products(page):
            calls.append("products")
            return BalanceFetchResult(products=[product], failed_surfaces=("línea",))

        def rate(page):
            calls.append("rate")
            return list(rates), tuple(rate_failed)

        def movements(page):
            calls.append("movements")
            return [movement], ("movimientos",)

        monkeypatch.setattr(movements_mod, "_read_all_surfaces", products)
        monkeypatch.setattr(movements_mod, "read_exchange_rate_surface", rate)
        monkeypatch.setattr(movements_mod, "read_movement_surfaces", movements)
        return product, movement

    def test_the_rate_rides_its_own_channel(self, monkeypatch):
        calls = []
        rate = ScrapedExchangeRate(
            institution="banchile",
            base_currency="USD",
            rate="950.00",
            product=ProductRef(kind="credit_card", currency="USD"),
            source="Pago internacional de tarjeta",
        )
        product, movement = self._patch(monkeypatch, calls, rates=[rate])

        result = read_session(MagicMock())

        assert calls == ["products", "rate", "movements"]
        assert result.products == [product]
        assert result.exchange_rates == [rate]
        assert result.movements == [movement]
        assert result.failed_surfaces == ("línea", "movimientos")

    def test_a_failed_rate_is_a_failed_surface_and_costs_nothing_else(
        self, monkeypatch
    ):
        calls = []
        product, movement = self._patch(
            monkeypatch, calls, rate_failed=("tipo de cambio",)
        )

        result = read_session(MagicMock())

        assert result.exchange_rates == []
        assert result.products == [product]
        assert result.movements == [movement]
        assert result.failed_surfaces == ("línea", "tipo de cambio", "movimientos")


# --- The account dialog, step by step (issue #82) -----------------------------
#
# Every label, account number and payload below is synthetic.

_USD_RADIO = "Cuenta Corriente 00-000-00000-02 (USD)"
_CLP_RADIO = "Cuenta Corriente 00-000-00000-01 (CLP)"


class TestCartolaMismatch:
    def test_the_currency_must_match(self):
        assert movements_mod.cartola_mismatch({"moneda": "USD"}, "USD") == ""
        assert movements_mod.cartola_mismatch({"moneda": "clp"}, "USD") == (
            "stale cartola (moneda CLP)"
        )

    def test_an_account_already_read_is_stale(self):
        payload = {"moneda": "CLP", "movimientos": [_cartola_row()]}
        assert movements_mod.cartola_mismatch(payload, "CLP", {ACCOUNT}) == (
            "stale cartola (an account already read)"
        )
        assert movements_mod.cartola_mismatch(payload, "CLP", set()) == ""

    def test_a_payload_without_moneda_is_not_judged_on_it(self):
        assert movements_mod.cartola_mismatch({"movimientos": []}, "USD") == ""


class TestCurrencyHelpers:
    def test_option_index(self):
        options = ["(USD)", "(CLP)"]
        assert movements_mod.matching_option_index(options, "USD") == 0
        assert movements_mod.matching_option_index(options, "CLP") == 1
        assert movements_mod.matching_option_index(["(CLP)"], "USD") is None
        assert movements_mod.matching_option_index([], "CLP") is None

    def test_usd_radios_are_found_by_label(self):
        labels = [_CLP_RADIO, _USD_RADIO]
        assert movements_mod.matching_radio_indices(labels, "USD") == [1]
        assert movements_mod.matching_radio_indices(labels[:1], "USD") == []

    def test_clp_radios_keep_their_positional_pick(self):
        labels = ["Cuenta Corriente 00-000-00000-01", "Cuenta Vista 00-000-00000-03"]
        assert movements_mod.matching_radio_indices(labels, "CLP") == [0, 1]


_REOPEN = "role=button[name=SELECCIONAR OTRA CUENTA]"


class _DialogLocator:
    """One selector on the fake account dialog (see `_FakeDialogPage`)."""

    def __init__(self, page, selector, index=0):
        self.page = page
        self.selector = selector
        self.index = index

    def _items(self):
        page = self.page
        if self.selector == movements_mod._CURRENCY_SELECT_SELECTOR:
            return ["select"] if page.dialog_open and page.select_visible else []
        if self.selector == movements_mod._CURRENCY_OPTION_SELECTOR:
            return list(page.options) if page.select_open else []
        if self.selector == movements_mod._ACCOUNT_RADIO_SELECTOR:
            return list(page.radios.get(page.chosen, [])) if page.dialog_open else []
        if self.selector == movements_mod._MODAL_ACCEPT_SELECTOR:
            if not page.dialog_open or page.chosen in page.accept_fails_for:
                return []
            return ["accept"]
        if self.selector == _REOPEN:
            shown = page.on_movements and page.remembered and not page.dialog_open
            return ["reopen"] if shown else []
        if self.selector.endswith(" label") or self.selector.startswith("radio-label:"):
            return ["label"]
        return []

    @property
    def first(self):
        return _DialogLocator(self.page, self.selector, 0)

    def nth(self, index):
        return _DialogLocator(self.page, self.selector, index)

    def count(self):
        return len(self._items())

    def is_visible(self):
        return self.index < len(self._items())

    def wait_for(self, **kwargs):
        if self.index >= len(self._items()):
            raise RuntimeError("not visible")

    def inner_text(self):
        return self._items()[self.index]

    def get_attribute(self, name):
        return f"mat-radio-{self.index}"

    def locator(self, selector):
        return _DialogLocator(self.page, f"radio-label:{self.index}")

    def filter(self, visible=None):
        return self

    def click(self, **kwargs):
        page = self.page
        if self.selector == movements_mod._CURRENCY_SELECT_SELECTOR:
            page.select_open = True
        elif self.selector == movements_mod._CURRENCY_OPTION_SELECTOR:
            page.chosen = "USD" if "USD" in self.inner_text() else "CLP"
            page.select_open = False
        elif self.selector.startswith("radio-label:"):
            page.picked = int(self.selector.split(":")[1])
        elif self.selector.startswith("#mat-radio-"):
            page.picked = int(self.selector.split()[0].rsplit("-", 1)[1])
        elif self.selector == movements_mod._MODAL_ACCEPT_SELECTOR:
            page.accept()
        elif self.selector == _REOPEN:
            page.click_reopen()
        else:
            raise RuntimeError("not clickable")


class _FakeClock:
    """Stands in for `time` in the movements module: only the page's waits
    move it, so every budget is real but no test ever sleeps."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


_CLOCK = _FakeClock()


def _use_fake_clock(monkeypatch):
    _CLOCK.now = 1000.0
    monkeypatch.setattr(movements_mod, "time", _CLOCK)


class _FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        if key != "Escape":
            return
        if self.page.select_open:
            self.page.select_open = False
        elif self.page.dialog_open:
            self.page.dialog_open = False


class _FakeDialogPage:
    """The movements route and its "Seleccione una cuenta" dialog, all synthetic.

    `radios` maps a currency to the radio labels it lists, `cartolas` maps it
    to the `getCartola` payload Aceptar makes the SPA load (or to a list of
    them, one per radio). The select defaults to USD, as on the portal. Like
    the SPA, the page remembers the account once one is accepted: the route
    then shows that cartola and a "SELECCIONAR OTRA CUENTA" button instead of
    the dialog.
    """

    def __init__(
        self,
        options=("(USD)", "(CLP)"),
        radios=None,
        cartolas=None,
        accept_fails_for=(),
        cartola_fires=True,
        select_visible=True,
        remembered=False,
        accept_answers_remembered=False,
        auto_fetch_polls=0,
        reopen_ready_polls=0,
        dialog_delay_polls=0,
    ):
        self.url = "https://portalpersonas.bancochile.cl/mibancochile-web/#/home"
        self.options = options
        self.radios = radios if radios is not None else {"CLP": [_CLP_RADIO], "USD": [_USD_RADIO]}
        self.cartolas = cartolas or {}
        self.accept_fails_for = set(accept_fails_for)
        self.cartola_fires = cartola_fires
        self.select_visible = select_visible
        self.remembered = remembered
        self.on_movements = False
        self.dialog_open = False
        self.select_open = False
        self.chosen = "USD"
        self.picked = None
        self.reopen_clicks = 0
        self.remembered_as = None
        self.pending = []
        self.accept_answers_remembered = accept_answers_remembered
        # The remembered view: its own cartola arrives `auto_fetch_polls`
        # waits after the route, its button only works once that fetch landed
        # and `reopen_ready_polls` waits passed, and the dialog then takes
        # `dialog_delay_polls` waits to show.
        self.auto_fetch_polls = auto_fetch_polls
        self.reopen_ready_polls = reopen_ready_polls
        self.dialog_delay_polls = dialog_delay_polls
        self.polls_since_route = 0
        self.auto_fetch_in = None
        self.dialog_opens_in = None
        self.ignored_reopen_clicks = 0
        self.handlers = {}
        self.keyboard = _FakeKeyboard(self)

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def accept(self):
        if not self.cartola_fires or self.picked is None:
            return
        self.dialog_open = False
        self.remembered = True
        if self.accept_answers_remembered and self.remembered_as is not None:
            self.pending.append(self.remembered_as)
            return
        self.remembered_as = (self.chosen, self.picked)
        # The response arrives later, while the scraper waits, as on the portal.
        self.pending.append((self.chosen, self.picked))

    def fire_cartola(self, currency, picked):
        payload = self.cartolas.get(currency, {"movimientos": []})
        if isinstance(payload, list):
            payload = payload[picked]
        payload = {"moneda": currency, **payload}  # every live payload has one
        response = _FakeResponse(
            "https://portalpersonas.bancochile.cl/x/movimientos/getCartola", payload
        )
        for handler in list(self.handlers.get("response", [])):
            handler(response)

    def locator(self, selector):
        return _DialogLocator(self, selector)

    def evaluate(self, script, arg=None):
        if arg == movements_mod._MOVIMIENTOS_ROUTE:
            self.on_movements = True
            self.dialog_open = not self.remembered
            self.polls_since_route = 0
            if self.remembered and self.remembered_as is not None:
                # The SPA fetches the remembered account's cartola by itself.
                self.auto_fetch_in = self.auto_fetch_polls
                self._tick_auto_fetch(advance=False)
        return None

    def get_by_role(self, role, name=None):
        assert role == "button" and name.search("SELECCIONAR OTRA CUENTA")
        return _DialogLocator(self, _REOPEN)

    def _tick_auto_fetch(self, advance=True):
        if self.auto_fetch_in is None:
            return
        if advance:
            self.auto_fetch_in -= 1
        if self.auto_fetch_in <= 0:
            self.auto_fetch_in = None
            self.fire_cartola(*self.remembered_as)

    def click_reopen(self):
        self.reopen_clicks += 1
        settled = self.auto_fetch_in is None
        if not settled or self.polls_since_route < self.reopen_ready_polls:
            self.ignored_reopen_clicks += 1  # the view is still rendering
            return
        self.chosen = "USD"  # the dialog opens on its USD default again
        self.dialog_opens_in = self.dialog_delay_polls
        self._tick_dialog(advance=False)

    def _tick_dialog(self, advance=True):
        if self.dialog_opens_in is None:
            return
        if advance:
            self.dialog_opens_in -= 1
        if self.dialog_opens_in <= 0:
            self.dialog_opens_in = None
            self.dialog_open = True

    def wait_for_timeout(self, ms):
        _CLOCK.now += ms / 1000
        self.polls_since_route += 1
        self._tick_auto_fetch()
        self._tick_dialog()
        pending, self.pending = self.pending, []
        for currency, picked in pending:
            self.fire_cartola(currency, picked)

    def goto(self, url, **kwargs):
        self.url = url
        self.auto_fetch_in = None
        self.dialog_opens_in = None
        self.on_movements = False
        self.dialog_open = False
        self.select_open = False


def _remembered_page(**kwargs):
    """A page whose SPA already remembers the (synthetic) CLP account."""
    page = _FakeDialogPage(cartolas={"CLP": {"movimientos": [_cartola_row()]}}, **kwargs)
    page.remembered = True
    page.remembered_as = ("CLP", 0)
    return page


def _read_usd_dialog(page, attempt=0):
    return movements_mod._read_checking_movements(page, attempt, currency="USD")


class TestDialogSteps:
    @pytest.fixture(autouse=True)
    def _fake_time(self, monkeypatch):
        _use_fake_clock(monkeypatch)

    def test_the_clp_drive_still_picks_clp_by_position(self):
        page = _FakeDialogPage(cartolas={"CLP": {"movimientos": [_cartola_row()]}})

        found = movements_mod._read_checking_movements(page, attempt=0)

        assert page.chosen == "CLP"
        assert page.picked == 0
        assert [(m.currency, m.amount) for m in found] == [("CLP", Decimal(1000000))]

    def test_a_remembered_account_reopens_the_dialog_with_its_button(self):
        """Once an account is remembered the route shows no dialog by itself."""
        page = _FakeDialogPage(
            remembered=True, cartolas={"CLP": {"movimientos": [_cartola_row()]}}
        )

        found = movements_mod._read_checking_movements(page, attempt=0)

        assert page.reopen_clicks == 1
        assert [m.currency for m in found] == ["CLP"]

    def test_a_second_clp_account_is_read_through_the_reopen_button(self):
        second = dict(_cartola_row(saldo="999.999"), numeroCuenta="00-000-00000-03")
        second["id"] = "CTD00-000-00000-03:20260820 12:41:33:1000000:abono:1"
        page = _FakeDialogPage(
            radios={"CLP": [_CLP_RADIO, "Cuenta Vista 00-000-00000-03 (CLP)"]},
            cartolas={
                "CLP": [{"movimientos": [_cartola_row()]}, {"movimientos": [second]}]
            },
        )

        found = movements_mod._read_checking_movements(page, attempt=0)

        assert page.reopen_clicks == 1  # the first drive found the dialog open
        assert [m.fingerprint[1] for m in found] == ["2.500.000", "999.999"]

    def test_the_usd_drive_after_the_clp_one_reopens_the_dialog(self):
        page = _FakeDialogPage(cartolas={"CLP": {"movimientos": [_cartola_row()]}})

        assert len(movements_mod._read_checking_movements(page, attempt=0)) == 1
        assert _read_usd_dialog(page) == []
        assert (page.reopen_clicks, page.chosen, page.picked) == (1, "USD", 0)

    def test_no_account_leaves_no_panel_open(self):
        """The currency panel stayed open behind a no-account return live."""
        page = _FakeDialogPage(options=("(CLP)",))

        assert _read_usd_dialog(page) == []
        assert (page.select_open, page.dialog_open, page.on_movements) == (
            False,
            False,
            False,
        )

    @pytest.mark.parametrize(
        "page_kwargs",
        [{}, {"accept_fails_for": {"USD"}}, {"cartola_fires": False}],
        ids=["read", "accept-failed", "cartola-wait-failed"],
    )
    def test_every_usd_outcome_leaves_the_page_on_home(self, page_kwargs):
        """The card legs start from the dashboard and must find it uncovered."""
        page = _FakeDialogPage(**page_kwargs)

        _read_usd_dialog(page)

        assert not (page.select_open or page.dialog_open or page.on_movements)
        assert page.url == banchile_web_mod._PORTAL_HOME

    def test_a_clp_cartola_is_never_read_as_the_usd_one(self, caplog):
        """Aceptar answered only by the remembered CLP account's cartola."""
        page = _FakeDialogPage(cartolas={"CLP": {"movimientos": [_cartola_row()]}})
        assert len(movements_mod._read_checking_movements(page, attempt=0)) == 1
        page.accept_answers_remembered = True

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            assert _read_usd_dialog(page) is None

        assert "failed at the cartola wait step" in caplog.text
        assert "stale cartola (moneda CLP)" in caplog.text

    def test_the_reopen_click_waits_for_the_remembered_cartola(self):
        """Clicking while the remembered view still fetches does nothing."""
        page = _remembered_page(auto_fetch_polls=6)

        assert _read_usd_dialog(page) == []
        assert (page.reopen_clicks, page.ignored_reopen_clicks) == (1, 0)

    def test_an_ignored_reopen_click_is_retried(self):
        page = _remembered_page(reopen_ready_polls=8)

        assert _read_usd_dialog(page) == []
        assert (page.reopen_clicks, page.ignored_reopen_clicks) == (2, 1)

    def test_a_slow_dialog_is_waited_for_not_clicked_again(self):
        page = _remembered_page(dialog_delay_polls=4)

        assert _read_usd_dialog(page) == []
        assert page.reopen_clicks == 1

    def test_a_button_that_never_opens_the_dialog_fails_the_step(self, caplog):
        page = _remembered_page(reopen_ready_polls=10**6)

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            assert _read_usd_dialog(page) is None

        assert page.reopen_clicks == movements_mod._REOPEN_CLICKS
        assert (
            "failed at the dialog open step (account 1, attempt 1): reopen button "
            "did not open the dialog after 3 click(s)"
        ) in caplog.text

    def test_the_usd_radio_is_found_by_its_label_not_its_position(self):
        page = _FakeDialogPage(radios={"USD": [_CLP_RADIO, _USD_RADIO]})

        assert _read_usd_dialog(page) == []
        assert (page.chosen, page.picked) == ("USD", 1)

    def test_option_text_is_never_logged(self, caplog):
        page = _FakeDialogPage(options=("(USD) sintetico", "(CLP) sintetico"))

        with caplog.at_level("DEBUG", logger=movements_mod.__name__):
            _read_usd_dialog(page)

        assert "2 currency option(s); recognised: ['CLP', 'USD']" in caplog.text
        assert "sintetico" not in caplog.text

    def test_no_usd_option_means_no_account(self, caplog):
        page = _FakeDialogPage(options=("(CLP)",))

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            found = _read_usd_dialog(page)

        assert found == []
        assert "no USD checking account (no USD option)" in caplog.text

    def test_a_list_with_no_recognised_currency_is_a_failure(self, caplog):
        """Blank or relabelled options are a slow render, not a missing account."""
        page = _FakeDialogPage(options=("",))

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            assert _read_usd_dialog(page) is None

        assert "no currency option recognised" in caplog.text

    def test_a_usd_option_whose_radios_never_render_is_a_failure(self, caplog):
        """The option promised an account; a radio wait that times out is a
        broken drive, not a missing account."""
        page = _FakeDialogPage(radios={"USD": []})

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            assert _read_usd_dialog(page) is None

        assert "failed at the account radio step" in caplog.text
        assert "no account radio rendered" in caplog.text

    def test_a_usd_option_with_only_clp_radios_is_a_failure(self, caplog):
        page = _FakeDialogPage(radios={"USD": [_CLP_RADIO]})

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            assert _read_usd_dialog(page) is None

        assert "1 account radio(s), none labelled (USD)" in caplog.text

    def test_a_missing_clp_option_is_still_a_failure(self, caplog):
        """Every customer has a CLP account, so its absence is a broken drive."""
        page = _FakeDialogPage(options=("(USD)",))

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            assert movements_mod._read_checking_movements(page, attempt=0) is None

        assert "CLP cartola dialog failed at the currency select step" in caplog.text

    @pytest.mark.parametrize(
        ("page_kwargs", "step"),
        [
            ({"select_visible": False}, "dialog open"),
            ({"options": ()}, "currency select"),
            ({"accept_fails_for": {"USD"}}, "accept"),
            ({"cartola_fires": False}, "cartola wait"),
        ],
        ids=["select-hidden", "list-empty", "accept", "cartola-wait"],
    )
    def test_a_failing_step_is_named_and_is_a_failure(self, caplog, page_kwargs, step):
        page = _FakeDialogPage(**page_kwargs)

        with caplog.at_level("INFO", logger=movements_mod.__name__):
            found = _read_usd_dialog(page, attempt=1)

        assert found is None
        assert f"USD cartola dialog failed at the {step} step (account 1, attempt 2)" in (
            caplog.text
        )


# --- USD checking cartola (issue #82) ------------------------------------------
#
# The USD row shape is pending live verification: the one USD account observed
# had no movements. Every row, account number and amount below is synthetic,
# built to cover the shapes the parser tolerates.


def _usd_cartola_row(monto=1234.56, saldo=2345.67, tipo="cargo"):
    """A synthetic USD `getCartola` row: the CLP shape, amounts with cents."""
    row = _cartola_row(tipo=tipo, fecha="20260820 09:15:00")
    row["monto"] = monto
    row["saldo"] = saldo
    row["id"] = f"CEX{ACCOUNT}:20260820 09:15:00:{monto}:{tipo}:1"
    return row


def _usd_cartola(rows):
    return {"moneda": "USD", "movimientos": rows, "pagina": [{"masPaginas": "N"}]}


class TestUsdAmountParsing:
    def test_formats(self):
        parse = movements_mod._usd_from_text
        assert parse("1.234,56") == Decimal("1234.56")
        assert parse("1,234.56") == Decimal("1234.56")
        assert parse("1234,56") == Decimal("1234.56")
        assert parse("1234.56") == Decimal("1234.56")
        assert parse("USD 1.234,56") == Decimal("1234.56")
        assert parse("USD 12,3") == Decimal("12.30")
        assert parse("1.234") == Decimal("1234.00")
        assert parse(1234.56) == Decimal("1234.56")
        assert parse(1234) == Decimal("1234.00")

    def test_a_lone_comma_is_always_the_decimal_separator(self, caplog):
        """Chilean format, even when three digits follow it (ambiguous, so logged)."""
        with caplog.at_level("WARNING", logger=movements_mod.__name__):
            assert movements_mod._usd_from_text("1,234") == Decimal("1.23")
        assert "three digits after a lone comma" in caplog.text

    def test_unparseable(self):
        parse = movements_mod._usd_from_text
        assert parse("") is None
        assert parse(None) is None
        assert parse("sin dato") is None
        assert parse(True) is None


class TestUsdCartolaRows:
    def test_a_cargo_keeps_its_cents(self):
        movement = parse_cartola_movement(_usd_cartola_row(monto=1234.56), currency="USD")
        assert movement.currency == "USD"
        assert movement.amount == Decimal("-1234.56")
        assert movement.product_kind == "checking"
        assert movement.transaction_date == datetime.date(2026, 8, 20)

    def test_an_abono_is_income(self):
        movement = parse_cartola_movement(
            _usd_cartola_row(monto="1.234,56", tipo="abono"), currency="USD"
        )
        assert movement.amount == Decimal("1234.56")

    def test_cents_are_never_rounded_away(self):
        movement = parse_cartola_movement(_usd_cartola_row(monto=0.99), currency="USD")
        assert movement.amount == Decimal("-0.99")

    def test_number_and_string_spellings_share_one_identity(self):
        as_number = _usd_cartola_row(saldo=2345.67)
        as_string = dict(as_number, saldo="2.345,67")
        first = parse_cartola_movement(as_number, currency="USD")
        second = parse_cartola_movement(as_string, currency="USD")
        assert first.fingerprint == second.fingerprint == (as_number["id"], "2345.67")
        assert external_id_for(first) == external_id_for(second)

    def test_rows_without_a_saldo_are_kept_and_warned_about_once(self, caplog):
        rows = [_usd_cartola_row(monto=1234.56), _usd_cartola_row(monto=0.99)]
        for row in rows:
            row["saldo"] = None

        with caplog.at_level("WARNING", logger=movements_mod.__name__):
            found, _ = movements_mod._movements_from_cartola(
                MagicMock(), _usd_cartola(rows), 0, "USD"
            )

        assert [m.fingerprint for m in found] == [(row["id"], "") for row in rows]
        assert caplog.text.count("no parseable saldo") == 1
        assert "2 USD cartola row(s) have no parseable saldo" in caplog.text

    def test_the_operation_id_scheme_is_the_clp_one(self):
        movement = parse_cartola_movement(
            _usd_cartola_row(), operation_id="TEF_IPE 0000123", currency="USD"
        )
        assert external_id_for(movement) == "bch_op_TEFIPE0000123"

    def test_unreadable_rows_are_dropped(self):
        assert parse_cartola_movement(_usd_cartola_row(monto="sin dato"), currency="USD") is None
        assert parse_cartola_movement(_usd_cartola_row(tipo="reverso"), currency="USD") is None
        row = _usd_cartola_row()
        row["id"] = ""
        row["saldo"] = None
        assert parse_cartola_movement(row, currency="USD") is None


class TestClpCartolaKeysAreUnchanged:
    """Pinned against main before issue #82: no CLP checking key may move."""

    def test_fingerprint_key(self):
        movement = parse_cartola_movement(_cartola_row())
        assert movement.currency == "CLP"
        assert movement.amount == Decimal(1000000)
        assert external_id_for(movement) == "bch_fp_9d6fd81f7f7ea522"

    def test_operation_id_key(self):
        movement = parse_cartola_movement(
            _cartola_row(tipo="cargo", monto="999.999"), operation_id="TEF_IPE 0000123"
        )
        assert external_id_for(movement) == "bch_op_TEFIPE0000123"


class TestUsdAndClpNeverCollide:
    def test_the_same_row_keys_differently_per_currency(self):
        """A USD fingerprint is hashed with the currency in front."""
        row = _cartola_row(monto="1.000.000", saldo="2.500.000")
        clp = parse_cartola_movement(row)
        usd = parse_cartola_movement(row, currency="USD")
        assert external_id_for(clp) != external_id_for(usd)
        assert external_id_for(usd).startswith("bch_fp_")

    def test_a_shared_operation_id_stays_two_movements(self):
        """Both read `bch_op_<id>`, but on different products (currency routes
        them, and `external_id` is unique per product), and one scrape keeps
        both."""
        clp = parse_cartola_movement(_cartola_row(), operation_id="12345678901")
        usd = parse_cartola_movement(
            _usd_cartola_row(), operation_id="12345678901", currency="USD"
        )
        assert (clp.currency, usd.currency) == ("CLP", "USD")
        assert dedupe_movements([clp, usd]) == [clp, usd]


class TestUsdCheckingSurface:
    @pytest.fixture(autouse=True)
    def _fake_time(self, monkeypatch):
        _use_fake_clock(monkeypatch)

    def test_reads_the_usd_cartola_with_cents(self):
        page = _FakeDialogPage(cartolas={"USD": _usd_cartola([_usd_cartola_row()])})

        found = movements_mod._read_usd_checking_movements(page, attempt=0)

        assert [(m.currency, m.amount) for m in found] == [("USD", Decimal("-1234.56"))]

    def test_an_empty_cartola_is_a_reading_not_a_failure(self):
        page = _FakeDialogPage(cartolas={"USD": _usd_cartola([])})

        assert movements_mod._read_usd_checking_movements(page, attempt=0) == []

    def test_no_usd_account_is_not_a_failed_surface(self, monkeypatch):
        monkeypatch.setattr(
            movements_mod,
            "_MOVEMENT_READERS",
            [("movimientos USD", movements_mod._read_usd_checking_movements)],
        )

        movements, failed = read_movement_surfaces(_FakeDialogPage(options=("(CLP)",)))

        assert (movements, failed) == ([], ())

    def test_a_failed_usd_cartola_never_costs_the_clp_one(self, monkeypatch):
        page = _FakeDialogPage(
            cartolas={"CLP": {"movimientos": [_cartola_row()]}},
            accept_fails_for={"USD"},
        )
        monkeypatch.setattr(
            movements_mod,
            "_MOVEMENT_READERS",
            [
                ("movimientos", movements_mod._read_checking_movements),
                ("movimientos USD", movements_mod._read_usd_checking_movements),
            ],
        )

        movements, failed = read_movement_surfaces(page)

        assert failed == ("movimientos USD",)
        assert [(m.currency, external_id_for(m)) for m in movements] == [
            ("CLP", "bch_fp_9d6fd81f7f7ea522")
        ]

    def test_it_is_registered_right_after_the_clp_cartola(self):
        labels = [label for label, _ in movements_mod._MOVEMENT_READERS]
        assert labels[:2] == ["movimientos", "movimientos USD"]
