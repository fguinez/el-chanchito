"""Tests for the BanChile scraper: the external_id scheme and the two legs.

Every movement below is built from a synthetic payload row of the shape the
portal returns, so the tests exercise the same path a real scrape does: raw
dict -> `banchile_movements` pure helper -> `ScrapedTransaction`. Every
identifier, amount, account and operation id is fabricated (see the repo's
personal-data policy).
"""

import asyncio
import os
from datetime import date

os.environ.setdefault("BANCHILE_RUT", "test")
os.environ.setdefault("BANCHILE_PASSWORD", "test")

from product_model import CheckingMetrics

from scrapers.backends.banchile_movements import (
    BanChileSessionResult,
    operation_id_from_detail,
    operation_id_from_glosa,
    parse_billed_movements,
    parse_cartola_movement,
    parse_unbilled_movement,
)
from scrapers.backends.banchile_web import BalanceFetchResult
from scrapers.base import ScrapedProduct
from scrapers.institutions import banchile as banchile_mod
from scrapers.institutions.banchile import BanChileScraper

ACCOUNT = "00-000-00000-01"
CARD_LAST4 = "1234"


def _cartola_row(
    descripcion="TRANSFERENCIA DE TERCERO",
    monto="1.000.000",
    tipo="abono",
    fecha="20260820 12:41:33",
    saldo="2.500.000",
    detalle_glosa=None,
):
    return {
        "descripcion": descripcion,
        "monto": monto,
        "saldo": saldo,
        "numeroCuenta": ACCOUNT,
        "tipo": tipo,
        "fecha": fecha,
        "fechaContable": "20/08/2026",
        "id": f"CTD{ACCOUNT}:{fecha}:{monto.replace('.', '')}:{tipo}:1",
        "detalleGlosa": detalle_glosa if detalle_glosa is not None else [],
        "infoDataGlosaAdicional": "glosa-token-1",
    }


def _unbilled_row(monto=999999, comercio="COMERCIO SINTETICO", tbk="000000000001"):
    return {
        "fechaTransaccionString": "20/08/2026",
        "fechaAutorizacionString": "20/08/2026",
        "horaAutorizacion": "12:41:33",
        "montoCompra": monto,
        "numeroTarjeta": CARD_LAST4,
        "codigoComercioTBK": tbk,
        "nombreComercio": comercio,
    }


def _billed_payload(rows):
    return {"seccionOperaciones": {"transaccionesTarjetas": rows}}


def _billed_row(
    referencia="2008 12345678",
    monto=999999,
    descripcion="COMERCIO SINTETICO",
    grupo="avancesCompras",
):
    return {
        "numReferencia": referencia,
        "nombreTarjeta": f"VISA ****{CARD_LAST4}",
        "fechaTransaccionString": "20/08/2026",
        "montoTransaccion": monto,
        "descripcion": descripcion,
        "totales": False,
        "grupo": grupo,
    }


class TestCheckingMapping:
    """A `getCartola` row becomes a checking transaction keyed on its id."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def _convert(self, row, operation_id=None):
        return self.scraper._movement_to_transaction(
            parse_cartola_movement(row, operation_id)
        )

    def test_inline_glosa_id_becomes_the_key(self):
        """A transfer carries its "ID Transacción" in `detalleGlosa`."""
        row = _cartola_row(
            detalle_glosa=["Banco: BANCO SINTETICO", "Id transaccion: 12345678901"]
        )
        operation_id = operation_id_from_glosa(row["detalleGlosa"])

        txn = self._convert(row, operation_id)

        assert txn.external_id == "bch_op_12345678901"
        assert txn.institution == "banchile"
        assert txn.product_kind == "checking"
        assert txn.amount == 1000000
        assert txn.transaction_date == date(2026, 8, 20)
        assert txn.scheduled_month == date(2026, 8, 1)

    def test_detail_payload_id_becomes_the_key(self):
        """A movement with an empty glosa gets its id from the extra request."""
        operation_id = operation_id_from_detail({"transaccionId": "TEF_IPE00000001"})

        txn = self._convert(_cartola_row(), operation_id)

        # Punctuation is dropped so a cosmetic drift can't re-key the movement.
        assert txn.external_id == "bch_op_TEFIPE00000001"

    def test_both_dates_reach_the_transaction(self):
        """`transaction_date` is when it happened, `accounting_date` when posted.

        `scheduled_month` follows the occurrence date, which is intended: the
        month a movement belongs to is the month it happened in.
        """
        row = _cartola_row(fecha="20260821 15:48:28")
        row["fechaContable"] = "24/08/2026"

        txn = self._convert(row, "12345678901")

        assert txn.transaction_date == date(2026, 8, 21)
        assert txn.accounting_date == date(2026, 8, 24)
        assert txn.scheduled_month == date(2026, 8, 1)

    def test_the_dates_never_reach_the_key(self):
        """Keying on a date is the pre-#57 scheme this replaced."""
        row = _cartola_row(fecha="20260821 15:48:28")
        row["fechaContable"] = "24/08/2026"
        reposted = _cartola_row(fecha="20260821 15:48:28")
        reposted["fechaContable"] = "25/08/2026"

        assert self._convert(row).external_id == self._convert(reposted).external_id

    def test_cargo_is_an_expense(self):
        txn = self._convert(_cartola_row(tipo="cargo", monto="999.999"), "12345678902")
        assert txn.amount == -999999

    def test_no_id_falls_back_to_a_fingerprint(self):
        """The ~7 percent the bank answers no glosa for still get a stable key."""
        txn = self._convert(_cartola_row())
        assert txn.external_id.startswith("bch_fp_")

    def test_fingerprint_is_stable_across_scrapes(self):
        assert self._convert(_cartola_row()).external_id == (
            self._convert(_cartola_row()).external_id
        )

    def test_description_change_does_not_re_key(self):
        """The V017 bug class: a reworded movement must keep its id."""
        first = self._convert(_cartola_row(descripcion="TRANSFERENCIA DE TERCERO"))
        renamed = self._convert(_cartola_row(descripcion="TRANSFERENCIA DE UN TERCERO"))
        assert first.external_id == renamed.external_id

    def test_description_change_does_not_re_key_an_id_keyed_movement(self):
        first = self._convert(_cartola_row(descripcion="COMPRA A"), "12345678903")
        renamed = self._convert(_cartola_row(descripcion="COMPRA B"), "12345678903")
        assert first.external_id == renamed.external_id == "bch_op_12345678903"

    def test_same_second_siblings_get_their_own_ids(self):
        """The #55 case: batch credits sharing everything the list exposes.

        The bank's composite `id` collides for them (37 distinct values for 42
        movements in the observed window) but the operation id does not, and
        `saldo` separates them when there is no operation id.
        """
        rows = [_cartola_row(saldo=f"{n}.500.000") for n in range(1, 6)]
        ids = [self._convert(row).external_id for row in rows]
        assert len(set(ids)) == 5

        with_operation_ids = [
            self._convert(_cartola_row(), f"1234567890{n}").external_id for n in range(5)
        ]
        assert len(set(with_operation_ids)) == 5

    def test_ids_do_not_depend_on_order_or_multiplicity(self):
        """PR #55's occurrence counter is gone: nothing is positional."""
        rows = [_cartola_row(saldo=f"{n}.500.000") for n in range(1, 4)]
        forwards = [self._convert(row).external_id for row in rows]
        backwards = [self._convert(row).external_id for row in reversed(rows)]
        with_a_repeat = [
            self._convert(row).external_id for row in rows + [rows[0], rows[1]]
        ]

        assert forwards == list(reversed(backwards))
        assert with_a_repeat[:3] == forwards
        assert with_a_repeat[3:] == forwards[:2]


class TestCardMapping:
    """The card's two legs: a fingerprint while unbilled, the reference after."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def _unbilled(self, **kwargs):
        return self.scraper._movement_to_transaction(
            parse_unbilled_movement(_unbilled_row(**kwargs))
        )

    def _billed(self, **kwargs):
        movements = parse_billed_movements(_billed_payload([_billed_row(**kwargs)]))
        return self.scraper._movement_to_transaction(movements[0])

    def test_the_card_legs_report_no_posting_date(self):
        """Both legs date a charge by when it happened; nothing posts it."""
        assert self._unbilled().accounting_date is None
        assert self._billed().accounting_date is None
        assert self._billed().transaction_date == date(2026, 8, 20)

    def test_a_billed_payment_is_income(self):
        """`montoTransaccion` is unsigned on this leg; `grupo` is the direction."""
        txn = self._billed(monto=2500000, grupo="pagos", referencia="2008 00000000")
        assert txn.amount == 2500000

    def test_unbilled_charge_is_negative_and_fingerprinted(self):
        txn = self._unbilled()
        assert txn.product_kind == "credit_card"
        assert txn.amount == -999999
        assert txn.external_id.startswith("bch_fp_")

    def test_unbilled_merchant_rename_keeps_the_id(self):
        """Issue #56's second trigger: the portal rewrites the merchant name."""
        assert self._unbilled(comercio="COMERCIO SINTETICO").external_id == (
            self._unbilled(comercio="COMERCIO SINTETICO S.A. SANTIAGO").external_id
        )

    def test_distinct_unbilled_charges_stay_distinct(self):
        assert self._unbilled(tbk="000000000001").external_id != (
            self._unbilled(tbk="000000000002").external_id
        )

    def test_billed_row_is_keyed_on_its_reference(self):
        txn = self._billed()
        assert txn.external_id == "bch_ref_200812345678"
        assert txn.amount == -999999

    def test_billed_reference_survives_a_rename(self):
        assert self._billed(descripcion="COMERCIO SINTETICO").external_id == (
            self._billed(descripcion="COMERCIO SINTETICO S.A.").external_id
        )

    def test_billed_totales_row_never_becomes_a_transaction(self):
        rows = [_billed_row(), dict(_billed_row(referencia=""), totales=True)]
        assert len(parse_billed_movements(_billed_payload(rows))) == 1

    def test_all_zero_reference_falls_back_to_a_fingerprint(self):
        """Every payment row shares "...00000000"; keying on it would merge them."""
        txn = self._billed(referencia="2008 00000000", monto=-2500000, grupo="pagos")
        assert txn.external_id.startswith("bch_fp_")
        assert txn.amount == 2500000

    def test_the_two_card_legs_key_differently_by_construction(self):
        """They share no identity field, which is why the writer adopts (#56)."""
        assert self._unbilled().external_id != self._billed().external_id


class TestScrapeTransactions:
    """The transactions leg opens the shared session and caches its products."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def _session(self, monkeypatch, result):
        async def fake_fetch(rut, password):
            assert (rut, password) == (self.scraper.rut, self.scraper.password)
            return result

        monkeypatch.setattr(banchile_mod, "fetch_session", fake_fetch)

    def test_converts_every_movement(self, monkeypatch):
        movements = [
            parse_cartola_movement(_cartola_row(), "12345678901"),
            parse_unbilled_movement(_unbilled_row()),
        ]
        self._session(monkeypatch, BanChileSessionResult(movements=movements))

        txns = asyncio.run(self.scraper.scrape_transactions())

        assert [t.product_kind for t in txns] == ["checking", "credit_card"]

    def test_products_are_served_from_the_same_session(self, monkeypatch):
        """One login per run (#28): the products leg must not open a second."""
        products = [
            ScrapedProduct(
                institution="banchile",
                kind="checking",
                metrics=CheckingMetrics(balance=2500000),
            )
        ]
        self._session(
            monkeypatch, BanChileSessionResult(products=products, failed_surfaces=("card",))
        )

        async def never(rut, password):
            raise AssertionError("the products leg opened a second session")

        monkeypatch.setattr(banchile_mod, "fetch_balances", never)

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == products
        assert result.warnings == ["BanChile: card surface failed after 3 attempts"]

    def test_movement_surface_failures_become_warnings(self, monkeypatch):
        """A movements surface that stayed empty is reported, not swallowed."""
        self._session(
            monkeypatch,
            BanChileSessionResult(failed_surfaces=("movimientos", "tarjeta facturados")),
        )

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert result.warnings == [
            "BanChile: movimientos surface failed after 3 attempts",
            "BanChile: tarjeta facturados surface failed after 3 attempts",
        ]

    def test_session_crash_is_raised(self, monkeypatch):
        async def boom(rut, password):
            raise RuntimeError("login failed")

        monkeypatch.setattr(banchile_mod, "fetch_session", boom)

        try:
            asyncio.run(self.scraper.scrape_transactions())
        except RuntimeError as exc:
            assert str(exc) == "login failed"
        else:
            raise AssertionError("the transactions leg must fail the run")


class TestScrapeProducts:
    """The products leg: cached half first, its own balance-only login after."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def test_falls_back_to_its_own_session(self, monkeypatch):
        """A crashed shared session must not cost the balances too."""
        expected = [
            ScrapedProduct(
                institution="banchile",
                kind="checking",
                metrics=CheckingMetrics(balance=1234567),
            )
        ]

        async def fake_fetch(rut, password):
            return BalanceFetchResult(products=expected, failed_surfaces=())

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == expected
        assert result.warnings == []

    def test_failed_surfaces_become_warnings(self, monkeypatch):
        async def fake_fetch(rut, password):
            return BalanceFetchResult(products=[], failed_surfaces=("card", "línea"))

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.warnings == [
            "BanChile: card surface failed after 3 attempts",
            "BanChile: línea surface failed after 3 attempts",
        ]

    def test_per_holding_surface_labels_reach_warnings(self, monkeypatch):
        """The issue #36 surfaces warn as depósitos/fondos (not 'inversiones')."""
        async def fake_fetch(rut, password):
            return BalanceFetchResult(
                products=[], failed_surfaces=("depósitos", "fondos")
            )

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.warnings == [
            "BanChile: depósitos surface failed after 3 attempts",
            "BanChile: fondos surface failed after 3 attempts",
        ]

    def test_backend_failure_is_swallowed(self, monkeypatch):
        """A heavy, flaky login must not fail the whole run."""
        async def boom(rut, password):
            raise RuntimeError("login failed")

        monkeypatch.setattr(banchile_mod, "fetch_balances", boom)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == []
        assert result.warnings == ["BanChile: product scrape crashed: login failed"]
