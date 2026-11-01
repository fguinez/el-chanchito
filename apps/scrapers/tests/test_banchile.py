"""Tests for the BanChile scraper: the external_id scheme and the two legs.

Every movement below is built from a synthetic payload row of the shape the
portal returns, so the tests exercise the same path a real scrape does: raw
dict -> `banchile_movements` pure helper -> `ScrapedTransaction`. Every
identifier, amount, account and operation id is fabricated (see the repo's
personal-data policy).
"""

import asyncio
import os
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault("BANCHILE_RUT", "test")
os.environ.setdefault("BANCHILE_PASSWORD", "test")

from product_model import (
    CheckingMetrics,
    CreditCardAttributes,
    CreditCardMetrics,
    LineOfCreditMetrics,
)

from db.writer import upsert_transactions
from scrapers.backends.banchile_movements import (
    BanChileSessionResult,
    BanChileStatement,
    billed_leg_from_statements,
    operation_id_from_detail,
    operation_id_from_glosa,
    parse_billed_movements,
    parse_cartola_movement,
    parse_international_billed_movements,
    parse_unbilled_movement,
)
from scrapers.backends.banchile_web import BalanceFetchResult
from scrapers.base import (
    ProductRef,
    ScrapedExchangeRate,
    ScrapedProduct,
    TransactionLocation,
)
from scrapers.institutions import banchile as banchile_mod
from scrapers.institutions.banchile import (
    BanChileScraper,
    external_id_for,
    legacy_external_id_for,
    with_statements,
)

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


def _unbilled_row(
    monto=999999, comercio="COMERCIO SINTETICO", tbk="000000000001", origen="NAC"
):
    return {
        "origenTransaccion": origen,
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


def _card(currency="CLP", last4=CARD_LAST4, available=1000000):
    """One BanChile card product as the card surface reads it (synthetic)."""
    return ScrapedProduct(
        institution="banchile",
        kind="credit_card",
        currency=currency,
        attributes=CreditCardAttributes(last4=last4),
        metrics=CreditCardMetrics(available=available, limit=2500000, owed=999999),
    )


def _statement(currency="CLP", **overrides):
    """A synthetic statement summary; every date and figure is invented."""
    fields = {
        "statement_date": date(2026, 8, 5),
        "next_statement_date": date(2026, 9, 4),
        "payment_due_date": date(2026, 8, 19),
        "billed_amount": 999999.0 if currency == "CLP" else 1234.56,
        "minimum_payment": 99999.0 if currency == "CLP" else None,
    }
    fields.update(overrides)
    return BanChileStatement(currency=currency, **fields)


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

        The bank's composite `id` collides for them (it repeats across the
        observed window) but the operation id does not, and `saldo` separates
        them when there is no operation id.
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


class TestCardBilling:
    """V044: the leg a card movement came from is its billing status."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def _billed(self, fecha="2026-08-05"):
        (movement,) = parse_billed_movements(_billed_payload([_billed_row()]), fecha)
        return self.scraper._movement_to_transaction(movement)

    def test_an_unbilled_charge_is_unbilled_with_no_statement(self):
        txn = self.scraper._movement_to_transaction(
            parse_unbilled_movement(_unbilled_row())
        )
        assert (txn.billing_status, txn.statement_date) == ("unbilled", None)

    def test_a_usd_unbilled_charge_is_unbilled_too(self):
        txn = self.scraper._movement_to_transaction(
            parse_unbilled_movement(_unbilled_row(monto=12.34, origen="INT"))
        )
        assert txn.currency == "USD"
        assert (txn.billing_status, txn.statement_date) == ("unbilled", None)

    def test_a_billed_row_carries_its_statement_date(self):
        txn = self._billed("2026-08-05")
        assert (txn.billing_status, txn.statement_date) == ("billed", date(2026, 8, 5))

    def test_a_usd_billed_row_carries_its_statement_date(self):
        (movement,) = parse_international_billed_movements(
            _intl_payload([_intl_row()]), "2026-08-05"
        )
        txn = self.scraper._movement_to_transaction(movement)
        assert txn.currency == "USD"
        assert (txn.billing_status, txn.statement_date) == ("billed", date(2026, 8, 5))

    def test_a_blank_or_garbled_statement_date_is_still_billed(self):
        """The leg says billed; only the statement it came from is unknown."""
        for fecha in ("", "05/08/2026", "2026-13-45"):
            txn = self._billed(fecha)
            assert (txn.billing_status, txn.statement_date) == ("billed", None)

    @pytest.mark.parametrize(
        ("cuotas", "dated"),
        [
            pytest.param("01/01", True, id="single-payment"),
            pytest.param("01/12", True, id="first-installment"),
            pytest.param("", True, id="no-counter"),
            pytest.param("03/12", False, id="later-installment"),
            pytest.param(" 12 / 12 ", False, id="last-installment-spaced"),
        ],
    )
    def test_a_later_installment_is_billed_without_a_date(self, cuotas, dated):
        """An earlier statement billed it first; this one's date would stick."""
        row = dict(_billed_row(), cuotas=cuotas)
        (movement,) = parse_billed_movements(_billed_payload([row]), "2026-08-05")

        txn = self.scraper._movement_to_transaction(movement)

        assert txn.billing_status == "billed"
        assert txn.statement_date == (date(2026, 8, 5) if dated else None)
        assert txn.external_id == "bch_ref_200812345678"

    def test_a_checking_movement_reports_no_billing(self):
        txn = self.scraper._movement_to_transaction(
            parse_cartola_movement(_cartola_row(), "12345678901")
        )
        assert (txn.billing_status, txn.statement_date) == (None, None)

    def test_the_billing_status_never_changes_the_key(self):
        """Keys are byte-identical to what they were before V044."""
        assert self._billed("2026-08-05").external_id == "bch_ref_200812345678"


class TestBillingOnPostgres:
    """End to end against the writer: scrape output -> stored billing columns."""

    def _scrape(self, statements):
        scraper = BanChileScraper()
        read = billed_leg_from_statements(statements, parse_billed_movements, "CLP")
        return [scraper._movement_to_transaction(m) for m in read.movements]

    def _stored(self, conn):
        return conn.execute(
            "SELECT billing_status, statement_date, updated_at FROM transactions "
            "WHERE external_id = 'bch_ref_200812345678'"
        ).fetchall()

    def test_an_installment_keeps_the_statement_that_billed_it_first(self, db_conn):
        """Read newest first, as the SPA loads them; the older statement sticks."""
        payload = _billed_payload([_billed_row(referencia="2008 12345678")])
        statements = [("2026-08-05", payload), ("2026-07-05", payload)]

        assert upsert_transactions(self._scrape(statements)) == 1

        (row,) = self._stored(db_conn)
        assert (row["billing_status"], row["statement_date"]) == ("billed", date(2026, 7, 5))

    def test_a_wrong_replay_never_pins_its_date(self, db_conn):
        """The older replay answered with the newer statement: the newer date sticks."""
        row = _billed_row(referencia="2008 12345678")
        newest = dict(_billed_payload([row]), resumen={"fechaFacturacionActual": "05/08/2026"})
        statements = [("2026-08-05", newest), ("2026-07-05", newest)]

        upsert_transactions(self._scrape(statements))

        (row,) = self._stored(db_conn)
        assert (row["billing_status"], row["statement_date"]) == ("billed", date(2026, 8, 5))

    def test_a_second_run_changes_nothing(self, db_conn):
        payload = _billed_payload([_billed_row(referencia="2008 12345678")])
        statements = [("2026-08-05", payload), ("2026-07-05", payload)]
        upsert_transactions(self._scrape(statements))
        before = self._stored(db_conn)

        assert upsert_transactions(self._scrape(statements)) == 0
        assert self._stored(db_conn) == before


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

    def test_the_sessions_rate_reaches_the_products_leg(self, monkeypatch):
        rate = ScrapedExchangeRate(
            institution="banchile",
            base_currency="USD",
            rate="950.00",
            product=ProductRef(kind="credit_card", currency="USD"),
            source="Pago internacional de tarjeta",
        )
        self._session(monkeypatch, BanChileSessionResult(exchange_rates=[rate]))

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert result.exchange_rates == [rate]
        assert result.products == []
        assert result.warnings == []

    def test_a_failed_rate_surface_becomes_a_warning(self, monkeypatch):
        self._session(
            monkeypatch, BanChileSessionResult(failed_surfaces=("tipo de cambio",))
        )

        asyncio.run(self.scraper.scrape_transactions())
        result = asyncio.run(self.scraper.scrape_products())

        assert result.exchange_rates == []
        assert result.warnings == [
            "BanChile: tipo de cambio surface failed after 3 attempts"
        ]

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
        # The balance-only fallback does not read the rate.
        assert result.exchange_rates == []

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

    def test_the_balance_only_fallback_reads_no_statement(self, monkeypatch):
        """Its card keeps the cupo figures alone; no statement field is set."""
        async def fake_fetch(rut, password):
            return BalanceFetchResult(products=[_card()], failed_surfaces=())

        monkeypatch.setattr(banchile_mod, "fetch_balances", fake_fetch)

        (card,) = asyncio.run(self.scraper.scrape_products()).products

        assert card.metrics.statement_date is None
        assert card.metrics.billed_amount is None
        assert card.attributes.statement_day is None


class TestStatementsReachTheCards:
    """The newest statement of each currency lands on that currency's card."""

    def test_each_currency_gets_its_own_statement(self):
        products = [_card("CLP"), _card("USD", available=1234.56)]
        statements = [
            _statement("USD", billed_amount=1234.56, statement_date=date(2026, 8, 6)),
            _statement("CLP"),
        ]

        clp, usd = with_statements(products, statements)

        assert clp.metrics.statement_date == date(2026, 8, 5)
        assert clp.metrics.next_statement_date == date(2026, 9, 4)
        assert clp.metrics.payment_due_date == date(2026, 8, 19)
        assert clp.metrics.billed_amount == 999999
        assert clp.metrics.minimum_payment == 99999
        assert usd.metrics.statement_date == date(2026, 8, 6)
        assert usd.metrics.billed_amount == 1234.56
        assert usd.metrics.minimum_payment is None

    def test_the_cupo_figures_and_last4_survive(self):
        (card,) = with_statements([_card()], [_statement()])

        assert card.metrics.available == 1000000
        assert card.metrics.limit == 2500000
        assert card.metrics.owed == 999999
        assert card.attributes.last4 == CARD_LAST4

    def test_the_statement_and_due_days_come_from_its_dates(self):
        (card,) = with_statements(
            [_card()],
            [
                _statement(
                    statement_date=date(2026, 8, 31),
                    payment_due_date=date(2026, 9, 14),
                )
            ],
        )
        assert (card.attributes.statement_day, card.attributes.due_day) == (31, 14)

    def test_no_due_date_leaves_the_due_day_alone(self):
        """None, which the writer's attribute merge reads as "keep the stored one"."""
        (card,) = with_statements([_card()], [_statement(payment_due_date=None)])
        assert (card.attributes.statement_day, card.attributes.due_day) == (5, None)

    def test_a_card_without_attributes_gains_the_days(self):
        bare = _card().model_copy(update={"attributes": None})
        (card,) = with_statements([bare], [_statement()])
        assert card.attributes.statement_day == 5
        assert card.attributes.last4 is None

    def test_other_products_are_untouched(self):
        linea = ScrapedProduct(
            institution="banchile",
            kind="line_of_credit",
            metrics=LineOfCreditMetrics(available=1000000, limit=2500000),
        )
        checking = ScrapedProduct(
            institution="banchile", kind="checking", metrics=CheckingMetrics(balance=1)
        )
        assert with_statements([linea, checking], [_statement()]) == [linea, checking]

    def test_a_statement_with_no_card_of_its_currency_is_dropped(self):
        """The USD card surface failed: its statement has nowhere to go."""
        assert with_statements([_card("CLP")], [_statement("USD")]) == [_card("CLP")]

    def test_two_cards_of_one_currency_take_no_statement(self):
        """Which card the statement belongs to is unknown, so neither gets it."""
        cards = [_card(last4="1234"), _card(last4="0000")]
        assert with_statements(cards, [_statement()]) == cards

    def test_the_shared_session_folds_them_in(self, monkeypatch):
        scraper = BanChileScraper()

        async def fake_fetch(rut, password):
            return BanChileSessionResult(products=[_card()], statements=[_statement()])

        monkeypatch.setattr(banchile_mod, "fetch_session", fake_fetch)

        asyncio.run(scraper.scrape_transactions())
        (card,) = asyncio.run(scraper.scrape_products()).products

        assert card.metrics.statement_date == date(2026, 8, 5)
        assert card.attributes.statement_day == 5


class TestLoginCooldownSkip:
    """A login the cooldown refused is a quiet skip: one warning, no traceback."""

    SKIP = "full login skipped to avoid the bank's login throttling (allowed again in 540s)"

    def setup_method(self):
        self.scraper = BanChileScraper()

    async def _refused(self, rut, password):
        raise banchile_mod.LoginCooldownError(self.SKIP)

    def _warnings_logged(self, caplog):
        records = [r for r in caplog.records if r.name == banchile_mod.logger.name]
        assert all(r.exc_info is None for r in records)
        return [r.levelname for r in records]

    def test_transactions_leg_still_fails_the_run(self, monkeypatch, caplog):
        monkeypatch.setattr(banchile_mod, "fetch_session", self._refused)

        try:
            asyncio.run(self.scraper.scrape_transactions())
        except banchile_mod.LoginCooldownError as exc:
            assert str(exc) == self.SKIP
        else:
            raise AssertionError("a refused login must still fail the run")

        assert self._warnings_logged(caplog) == ["WARNING"]

    def test_products_leg_turns_it_into_a_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(banchile_mod, "fetch_balances", self._refused)

        result = asyncio.run(self.scraper.scrape_products())

        assert result.products == []
        assert result.warnings == [f"BanChile: {self.SKIP}"]
        assert self._warnings_logged(caplog) == ["WARNING"]


# --- Issue #79: currency routing, identity and the misrouted rows -------------


def _intl_payload(rows, section="seccionCompras"):
    return {section: {"transaccionesTarjetas": rows}}


def _intl_row(numero_documento="2008 " + "1" * 23, monto=1234.56, **extra):
    """A synthetic USD statement row."""
    return {
        "numeroDocumento": numero_documento,
        "fechaTransaccionString": "20/08/2026",
        "descripcion": "COMERCIO SINTETICO",
        "montoDolar": monto,
        "tituloTarjeta": f"****{CARD_LAST4}",
        "totales": False,
        "grupo": "generico",
        **extra,
    }


class TestClpKeysAreUnchanged:
    """Pinned with the code as it was before the currency existed (issue #79).

    Existing rows are keyed on these, so a CLP movement must never re-key.
    """

    def test_checking(self):
        assert external_id_for(parse_cartola_movement(_cartola_row())) == (
            "bch_fp_9d6fd81f7f7ea522"
        )
        assert external_id_for(
            parse_cartola_movement(_cartola_row(), "12345678901")
        ) == "bch_op_12345678901"

    def test_unbilled(self):
        assert external_id_for(parse_unbilled_movement(_unbilled_row(monto=999999.0))) == (
            "bch_fp_1197be75ceed32e2"
        )
        assert external_id_for(
            parse_unbilled_movement(_unbilled_row(monto=-2500000.0))
        ) == "bch_fp_9532482b5aba64d1"

    def test_billed(self):
        def key(row):
            return external_id_for(
                parse_billed_movements(_billed_payload([row]), "2026-08-25")[0]
            )

        assert key(_billed_row()) == "bch_ref_200812345678"
        assert key(
            _billed_row(referencia="2008 00000000", monto=2500000, grupo="pagos")
        ) == "bch_fp_1c5496267f26cc60"


class TestUsdMapping:
    def setup_method(self):
        self.scraper = BanChileScraper()

    def test_a_usd_charge_reaches_the_usd_product(self):
        txn = self.scraper._movement_to_transaction(
            parse_unbilled_movement(_unbilled_row(monto=1234.56, origen="INT"))
        )
        assert txn.currency == "USD"
        assert txn.amount == Decimal("-1234.56")
        assert txn.product_kind == "credit_card"

    def test_clp_transactions_stay_clp(self):
        txn = self.scraper._movement_to_transaction(
            parse_unbilled_movement(_unbilled_row())
        )
        assert (txn.currency, txn.amount) == ("CLP", -999999)

    def test_a_usd_key_never_equals_a_clp_one(self):
        """Even for one row read under both currencies, the keys differ."""
        clp = parse_unbilled_movement(_unbilled_row(monto=12.0))
        usd = parse_unbilled_movement(_unbilled_row(monto=12.0, origen="INT"))
        assert external_id_for(clp) != external_id_for(usd)
        assert external_id_for(usd).startswith("bch_fp_")

    def test_a_usd_statement_location_reaches_the_transaction(self):
        """V025: `ciudadComercio` and `pais` ride onto the USD card's row."""
        (movement,) = parse_international_billed_movements(
            _intl_payload(
                [_intl_row(ciudadComercio=" CIUDAD SINTETICA ", pais="PAIS SINTETICO")]
            )
        )

        txn = self.scraper._movement_to_transaction(movement)

        assert (txn.currency, txn.product_kind) == ("USD", "credit_card")
        assert txn.location == TransactionLocation(
            city="CIUDAD SINTETICA", country="PAIS SINTETICO"
        )

    def test_a_row_without_location_maps_to_none(self):
        (movement,) = parse_international_billed_movements(_intl_payload([_intl_row()]))
        assert self.scraper._movement_to_transaction(movement).location is None

    def test_a_usd_purchase_is_keyed_on_its_reference(self):
        (movement,) = parse_international_billed_movements(_intl_payload([_intl_row()]))
        assert external_id_for(movement) == "bch_ref_2008" + "1" * 23

    def test_the_legacy_key_is_what_the_clp_only_code_stored(self):
        """Pinned with that code: the same synthetic row read as CLP."""
        movement = parse_unbilled_movement(_unbilled_row(monto=1234.56, origen="INT"))
        assert legacy_external_id_for(movement) == "bch_fp_109da42d5aba2816"
        assert movement.legacy_amount == -1235

        half = parse_unbilled_movement(_unbilled_row(monto=12.5, origen="INT"))
        assert legacy_external_id_for(half) == "bch_fp_b2074085fb82a9a7"
        assert half.legacy_amount == -12

    def test_clp_movements_have_no_legacy_key(self):
        assert legacy_external_id_for(parse_unbilled_movement(_unbilled_row())) is None


class TestSupersededTransactions:
    """Which CLP-card rows a scrape names as the old twins of its USD charges."""

    def setup_method(self):
        self.scraper = BanChileScraper()

    def _scrape(self, monkeypatch, movements, failed=()):
        async def fake_fetch(rut, password):
            return BanChileSessionResult(movements=movements, failed_surfaces=failed)

        monkeypatch.setattr(banchile_mod, "fetch_session", fake_fetch)
        asyncio.run(self.scraper.scrape_transactions())
        return self.scraper.superseded_transactions()

    # A synthetic statement date, read by both card legs unless a test says not.
    _STATEMENT = "2026-09-05"

    def _movements(self, usd_statement=_STATEMENT):
        return [
            parse_unbilled_movement(_unbilled_row()),
            parse_unbilled_movement(_unbilled_row(monto=1234.56, origen="INT")),
            *parse_international_billed_movements(
                _intl_payload([_intl_row(monto=12.5)]), usd_statement
            ),
            *parse_billed_movements(_billed_payload([_billed_row()]), self._STATEMENT),
        ]

    def test_one_per_usd_card_movement(self, monkeypatch):
        movements = self._movements()
        exact, loose = self._scrape(monkeypatch, movements)

        assert [exact.replacement_external_id, loose.replacement_external_id] == [
            external_id_for(movements[1]),
            external_id_for(movements[2]),
        ]

        assert (exact.external_id, exact.amount) == ("bch_fp_109da42d5aba2816", -1235)
        assert (loose.external_id, loose.amount) == (None, -12)
        for item in (exact, loose):
            assert (item.institution, item.product_kind, item.currency) == (
                "banchile",
                "credit_card",
                "CLP",
            )
            assert item.external_id_prefix == "bch_fp_"
            assert item.transaction_date == date(2026, 8, 20)
            assert item.created_before_migration == "V023__transactions_amount_numeric.sql"

    def test_the_loose_match_needs_both_clp_card_legs(self, monkeypatch):
        """Otherwise a genuine CLP row of the window might not be protected."""
        (exact,) = self._scrape(
            monkeypatch, self._movements(), failed=("tarjeta facturados",)
        )
        assert exact.external_id == "bch_fp_109da42d5aba2816"

    def test_the_loose_match_needs_the_statement_read_on_the_national_leg(
        self, monkeypatch
    ):
        """The legs pick their statements independently: an older USD one,
        outside every national statement read, has no CLP rows protecting it."""
        (exact,) = self._scrape(
            monkeypatch, self._movements(usd_statement="2026-07-05")
        )
        assert exact.external_id == "bch_fp_109da42d5aba2816"

    def test_a_national_statement_with_no_rows_offers_no_loose_match(
        self, monkeypatch
    ):
        """Nothing shows that statement was read, so nothing is assumed."""
        without_national_rows = self._movements()[:3]
        (exact,) = self._scrape(monkeypatch, without_national_rows)
        assert exact.external_id == "bch_fp_109da42d5aba2816"

    def test_none_without_usd_card_movements(self, monkeypatch):
        movements = [parse_unbilled_movement(_unbilled_row())]
        assert self._scrape(monkeypatch, movements) == []


class TestVanishedWindows:
    """Which card history windows a scrape listed in full (fix C)."""

    _NEWEST = "2026-09-05"
    _OLDER = "2026-08-05"
    _TODAY = date(2026, 9, 20)

    def setup_method(self):
        self.scraper = BanChileScraper()

    def _statements(self, **overrides):
        """Newest summaries of both currencies, current as of `_TODAY`."""
        fields = {
            "statement_date": date(2026, 9, 5),
            "next_statement_date": date(2026, 10, 5),
        }
        fields.update(overrides)
        return [_statement("CLP", **fields), _statement("USD", **fields)]

    def _movements(self):
        """Unbilled rows of both currencies plus two CLP statements and one USD."""
        return [
            parse_unbilled_movement(_unbilled_row()),
            parse_unbilled_movement(_unbilled_row(monto=1234.56, origen="INT")),
            *parse_billed_movements(_billed_payload([_billed_row()]), self._OLDER),
            *parse_billed_movements(
                _billed_payload([_billed_row(referencia="0509 12345679")]), self._NEWEST
            ),
            *parse_international_billed_movements(
                _intl_payload([_intl_row(monto=12.5)]), self._NEWEST
            ),
        ]

    def _windows(self, movements=None, failed=(), statements=None):
        return self.scraper._vanished_for(
            self._movements() if movements is None else movements,
            failed,
            self._statements() if statements is None else statements,
            today=self._TODAY,
        )

    def _session(self, monkeypatch):
        async def fake_fetch(rut, password):
            return BanChileSessionResult(
                movements=self._movements(),
                statements=self._statements(next_statement_date=date(2099, 1, 5)),
            )

        monkeypatch.setattr(banchile_mod, "fetch_session", fake_fetch)

    def test_a_full_read_offers_both_windows(self):
        """After the newest statement anything pending; before it, only unbilled."""
        # Arrange / Act
        windows = self._windows()

        # Assert
        assert [(w.currency, w.after, w.include_unreported) for w in windows] == [
            ("CLP", date(2026, 9, 5), True),
            ("CLP", date(2026, 8, 5), False),
            ("USD", date(2026, 9, 5), True),
        ]
        for window in windows:
            assert (window.institution, window.product_kind) == ("banchile", "credit_card")
            assert window.external_id_prefix == "bch_fp_"

    @pytest.mark.parametrize(
        ("failed", "currencies"),
        [
            pytest.param(("tarjeta no facturados",), [], id="unbilled-failed"),
            pytest.param(("tarjeta facturados",), ["USD"], id="clp-statement-failed"),
            pytest.param(("tarjeta facturados USD",), ["CLP", "CLP"], id="usd-statement-failed"),
        ],
    )
    def test_a_failed_surface_withholds_its_windows(self, failed, currencies):
        """A leg that did not load cannot vouch for what it would have listed."""
        # Arrange / Act
        windows = self._windows(failed=failed)

        # Assert
        assert [w.currency for w in windows] == currencies

    def test_an_empty_unbilled_list_offers_nothing(self):
        """An empty reading cannot be told from a portal that answered nothing."""
        # Arrange
        movements = [m for m in self._movements() if m.source != "card_unbilled"]

        # Act
        windows = self._windows(movements=movements)

        # Assert
        assert windows == []

    def test_a_statement_past_its_next_closing_is_stale(self):
        """A newer statement exists that this scrape did not read."""
        # Arrange
        statements = self._statements(next_statement_date=self._TODAY)

        # Act
        windows = self._windows(statements=statements)

        # Assert
        assert windows == []

    def test_no_statement_summary_offers_nothing(self):
        """Without the summary the cycle boundary is unknown."""
        # Arrange / Act
        windows = self._windows(statements=[])

        # Assert
        assert windows == []

    def test_an_unconfirmed_statement_withholds_its_currency(self):
        """`billing_unknown` rows mean a statement answered for another period."""
        # Arrange
        movements = self._movements()
        movements[2] = replace(movements[2], billing_unknown=True, statement_date="")

        # Act
        windows = self._windows(movements=movements)

        # Assert
        assert [w.currency for w in windows] == ["USD"]

    def test_scrape_transactions_records_them(self, monkeypatch):
        """The windows of the last scrape are what `vanished_windows` returns."""
        # Arrange
        self._session(monkeypatch)

        # Act
        asyncio.run(self.scraper.scrape_transactions())

        # Assert
        assert [w.currency for w in self.scraper.vanished_windows()] == ["CLP", "CLP", "USD"]

    def test_a_movement_that_failed_to_convert_withholds_every_window(self, monkeypatch):
        """Its stored row would look vanished though the bank still lists it."""
        # Arrange
        self._session(monkeypatch)
        convert = self.scraper._movement_to_transaction
        calls = []

        def flaky(movement):
            calls.append(movement)
            if len(calls) == 1:
                raise ValueError("synthetic conversion failure")
            return convert(movement)

        monkeypatch.setattr(self.scraper, "_movement_to_transaction", flaky)

        # Act
        asyncio.run(self.scraper.scrape_transactions())

        # Assert
        assert self.scraper.vanished_windows() == []
