"""Tests for the self-contained Banco de Chile web balance backend.

These never launch a browser or hit the real bank: the pure parsing helpers
are exercised directly, and `balances_from_page` is driven with a fake page
whose `evaluate` returns canned page text.
"""

import datetime
from unittest.mock import MagicMock

from product_model import CheckingMetrics, CreditCardMetrics

from scrapers.backends import banchile_web as banchile_web_mod
from scrapers.backends.banchile_web import (
    _balance_from_text,
    _merge_balances,
    _parse_date_ddmmyyyy,
    _parse_pct,
    _parse_term_days,
    _read_all_surfaces,
    _usd_checking_from_text,
    balances_by_kind,
    balances_from_page,
    build_depositos_products,
    build_fondos_products,
    card_balances_from_text,
    card_last4_from_text,
    card_saldos_from_text,
    deposito_aside_from_text,
    deposito_cards_from_text,
    depositos_header_from_text,
    fondo_aside_from_text,
    fondo_cards_from_text,
    fondos_header_from_text,
    linea_balances_from_text,
    linea_saldo_from_text,
    parse_amount,
    parse_clp,
)
from scrapers.base import ScrapedProduct


class TestParseClp:
    def test_plain_thousands(self):
        assert parse_clp("1.234.567") == 1234567

    def test_with_peso_sign_and_space(self):
        assert parse_clp("$ 1.234.567") == 1234567

    def test_with_currency_code(self):
        assert parse_clp("CLP 45.000") == 45000

    def test_comma_decimals_rounded(self):
        assert parse_clp("1.234.567,49") == 1234567
        assert parse_clp("1.234.567,50") == 1234568

    def test_negative(self):
        assert parse_clp("-$12.000") == -12000

    def test_zero_is_valid(self):
        assert parse_clp("$0") == 0

    def test_none_and_empty(self):
        assert parse_clp(None) is None
        assert parse_clp("") is None
        assert parse_clp("   ") is None

    def test_no_digits(self):
        assert parse_clp("sin dato") is None


class TestParseAmount:
    def test_keeps_usd_decimals(self):
        assert parse_amount("USD 1.950,91") == 1950.91

    def test_usd_whole(self):
        assert parse_amount("USD 2.000,00") == 2000.0

    def test_clp_dollar_sign(self):
        assert parse_amount("$ 4.000.000") == 4000000.0

    def test_none_and_no_digits(self):
        assert parse_amount(None) is None
        assert parse_amount("USD") is None


# Credit-card detail page ("Saldos y movimientos no facturados"): the real page
# STRUCTURE and labels confirmed by live QA — a CLP "Nacional" and a USD
# "Internacional" section, each with Utilizado / Disponible / Cupo total — but
# every identifier and figure is fabricated. CLP utilizado/available/límite =
# 400.000 / 3.600.000 / 4.000.000; USD = 50,00 / 1.950,00 / 2.000,00; the
# masked card number's tail ("****0000") is the last4.
CARD_DETAIL = """Titular Visa Signature ****0000 - Estado: Activa

Saldos y movimientos no facturados
Movimientos facturados

Nacional, al 01/01/2026

Utilizado
$ 400.000
Disponible
$ 3.600.000
Cupo total
$ 4.000.000
Cupo disponible avance
$ 3.600.000

Internacional, al 01/01/2026

Utilizado
USD 50,00
Disponible
USD 1.950,00
Cupo total
USD 2.000,00
Cupo disponible avance
USD 1.950,00
"""


class TestCardSaldosFromText:
    def test_reads_both_currencies(self):
        assert card_saldos_from_text(CARD_DETAIL) == {
            "CLP": {"available": 3600000.0, "limit": 4000000.0, "owed": 400000.0},
            "USD": {"available": 1950.0, "limit": 2000.0, "owed": 50.0},
        }

    def test_disponible_paired_with_own_currency_cupo(self):
        # The CLP figures must never pick up the USD "Cupo total"/"Utilizado"
        # (or vice versa): debt would be nonsense across currencies. (The "Cupo
        # disponible avance" line must not be read as the límite.)
        result = card_saldos_from_text(CARD_DETAIL)
        assert result["CLP"] == {
            "available": 3600000.0,
            "limit": 4000000.0,
            "owed": 400000.0,
        }
        assert result["USD"] == {"available": 1950.0, "limit": 2000.0, "owed": 50.0}

    def test_clp_only_page(self):
        text = "Nacional\nDisponible\n$ 4.000.000\nCupo total\n$ 5.000.000\n"
        assert card_saldos_from_text(text) == {
            "CLP": {"available": 4000000.0, "limit": 5000000.0}
        }

    def test_unparseable_section_is_omitted(self):
        # No CLP "$" figure to read -> the currency is dropped, not guessed.
        assert card_saldos_from_text("Nacional\nDisponible\nsin dato") == {}

    def test_empty_and_none(self):
        assert card_saldos_from_text("") == {}
        assert card_saldos_from_text(None) == {}


class TestCardLast4:
    def test_reads_masked_tail(self):
        assert card_last4_from_text(CARD_DETAIL) == "0000"

    def test_dashboard_bullet_mask_is_ignored(self):
        # The dashboard masks with bullets ("•••• 1234"); only the detail page's
        # "****0000" form is trusted for the last4.
        assert card_last4_from_text("•••• 1234") is None

    def test_none_when_absent(self):
        assert card_last4_from_text("Nacional\nDisponible\n$ 1.000") is None
        assert card_last4_from_text("") is None
        assert card_last4_from_text(None) is None


# Synthetic "Mis Productos" dashboard dump — the *structure* mirrors a real
# post-login page, but every account number and figure is fabricated. It holds a
# CLP checking account, a credit line, a USD checking account and a credit card;
# only the first ($2.500.000) is the CLP checking balance.
REAL_DASHBOARD = """SALDOS Y MOV. CUENTAS
Cuentas
Cuenta Corriente

00-000-00000-01

Disponible

$ 2.500.000

Línea de Crédito

00-000-00000-02

PAGAR

Disponible

$ 300.000

Cuenta Corriente

00-000-00000-03

Disponible

USD 0,00
Tarjetas de Crédito
•••• 1234

TITULAR VISA SIGNATURE

Disponible
$ 999.999
USD 1.234,00
"""


class TestBalanceFromText:
    def test_real_dashboard_picks_clp_checking_only(self):
        # The headline case: only the CLP "Cuenta Corriente" disponible counts.
        # Not the credit line ($300.000), the USD account (USD 0,00), or the
        # credit-card cupo ($999.999).
        assert _balance_from_text(REAL_DASHBOARD) == 2500000

    def test_sums_multiple_clp_checking_accounts(self):
        text = (
            "Cuenta Corriente\n11-111\nDisponible\n$ 1.000.000\n"
            "Cuenta Corriente\n22-222\nDisponible\n$ 500.000\n"
        )
        assert _balance_from_text(text) == 1500000

    def test_skips_usd_cuenta_corriente(self):
        text = "Cuenta Corriente\n00-000\nDisponible\nUSD 0,00"
        assert _balance_from_text(text) is None

    def test_ignores_credit_line_and_card(self):
        text = (
            "Línea de Crédito\n00-000\nPAGAR\nDisponible\n$ 300.000\n"
            "Tarjeta de Crédito\n1234\nDisponible\n$ 999.999"
        )
        assert _balance_from_text(text) is None

    def test_dedicated_saldos_page_fallback(self):
        assert _balance_from_text("Saldo Disponible: $1.234.567") == 1234567

    def test_fallback_prefers_disponible_over_contable(self):
        text = "Saldo Contable $9.999.999 Saldo Disponible $1.000.000"
        assert _balance_from_text(text) == 1000000

    def test_fallback_to_contable(self):
        assert _balance_from_text("Saldo Contable: $750.000") == 750000

    def test_amount_without_dollar_sign_is_ignored(self):
        # Deliberate false-negative: without a "$" we'd rather record nothing
        # than risk grabbing an unrelated number.
        assert _balance_from_text("Cuenta Corriente\n12-345\nDisponible\n1.234.567") is None

    def test_no_balance_returns_none(self):
        assert _balance_from_text("Bienvenido a Banco de Chile") is None

    def test_empty_returns_none(self):
        assert _balance_from_text("") is None
        assert _balance_from_text(None) is None


class TestBalancesByKind:
    def test_real_dashboard_checking_and_card(self):
        # The CLP cuenta corriente disponible and the card's CLP cupo — never
        # the línea de crédito ($300.000), the USD cuenta corriente, or the
        # card's USD cupo.
        assert balances_by_kind(REAL_DASHBOARD) == {
            "checking": 2500000,
            "credit_card": 999999,
        }

    def test_linea_de_credito_is_not_captured(self):
        # Available credit must not leak into any product (net-worth would count
        # it as debt). 300.000 appears nowhere in the result.
        assert 300000 not in balances_by_kind(REAL_DASHBOARD).values()

    def test_dashboard_never_reports_investments(self):
        # Depósitos a plazo / fondos mutuos aren't on the "Mis Productos"
        # dashboard — they're read from the inversiones resumen page — so no
        # matter what investment-looking text the dashboard carries, this never
        # reports those kinds.
        text = REAL_DASHBOARD + "Depósito a Plazo\n001\n$ 900.000\nFondos Mutuos\nX\n$ 500.000\n"
        result = balances_by_kind(text)
        assert result == {"checking": 2500000, "credit_card": 999999}
        assert "term_deposit" not in result
        assert "investment" not in result

    def test_tarjeta_without_de_credito_header_is_ignored(self):
        # A stray "Tarjeta" mention (not the "Tarjeta de Crédito" product header)
        # must not be read as a card balance.
        assert balances_by_kind("Cupo Disponible Tarjeta $500.000") == {}

    def test_empty_and_none(self):
        assert balances_by_kind("") == {}
        assert balances_by_kind(None) == {}


def _fake_page(text):
    page = MagicMock()
    page.evaluate.return_value = text
    return page


class TestBalancesFromPage:
    def test_returns_checking_scraped_product(self):
        balances = balances_from_page(_fake_page("Saldo Disponible $3.210.000"))
        assert len(balances) == 1
        bal = balances[0]
        assert bal.institution == "banchile"
        assert bal.kind == "checking"
        assert bal.currency == "CLP"
        assert bal.metrics == CheckingMetrics(balance=3210000)

    def test_no_balance_returns_empty(self):
        assert balances_from_page(_fake_page("nada por aquí")) == []

    def test_evaluate_failure_returns_empty(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("page closed")
        assert balances_from_page(page) == []

    def test_emits_clp_then_usd_checking(self):
        # The dashboard only carries checking: the CLP cuenta corriente, then the
        # USD one (USD 0,00). The credit_card figure is a placeholder ($999.999)
        # and is deliberately NOT emitted — the card is sourced from its detail
        # page. Depósitos/fondos aren't on the dashboard at all (inversiones page).
        balances = balances_from_page(_fake_page(REAL_DASHBOARD))
        assert [(b.kind, b.currency, b.metrics.balance) for b in balances] == [
            ("checking", "CLP", 2500000),
            ("checking", "USD", 0.0),
        ]
        assert all(b.institution == "banchile" for b in balances)

    def test_dashboard_never_emits_the_card_placeholder(self):
        # Live QA showed the dashboard card "Disponible" is a static placeholder
        # that never matches the real available cupo, so it must not be written.
        kinds = {b.kind for b in balances_from_page(_fake_page(REAL_DASHBOARD))}
        assert "credit_card" not in kinds

    def test_stray_tarjeta_mention_yields_only_checking(self):
        # A "Tarjeta" that isn't the "Tarjeta de Crédito" product header must
        # not become a credit_card balance.
        text = "Saldo Disponible $1.000.000\nCupo Disponible Tarjeta $500.000"
        balances = balances_from_page(_fake_page(text))
        assert [b.kind for b in balances] == ["checking"]


class TestUsdChecking:
    def test_usd_cuenta_corriente_emitted_from_dashboard(self):
        text = "Cuenta Corriente\n00-000\nDisponible\nUSD 1.234,56\n"
        balances = balances_from_page(_fake_page(text))
        assert [(b.kind, b.currency, b.metrics.balance) for b in balances] == [
            ("checking", "USD", 1234.56)
        ]

    def test_zero_usd_checking_still_emitted(self):
        # A real (empty) USD account shows USD 0,00 — recording it is correct.
        assert _usd_checking_from_text(REAL_DASHBOARD) == 0.0

    def test_clp_and_usd_checking_coexist(self):
        text = (
            "Cuenta Corriente\n11-111\nDisponible\n$ 2.000.000\n"
            "Cuenta Corriente\n22-222\nDisponible\nUSD 500,00\n"
        )
        balances = balances_from_page(_fake_page(text))
        assert [(b.kind, b.currency, b.metrics.balance) for b in balances] == [
            ("checking", "CLP", 2000000),
            ("checking", "USD", 500.0),
        ]

    def test_no_usd_checking(self):
        assert _usd_checking_from_text("Saldo Disponible $1.000.000") is None


class TestCardBalancesFromText:
    def test_emits_clp_and_usd_with_limits_and_owed(self):
        balances = card_balances_from_text(CARD_DETAIL)
        assert [
            (b.kind, b.currency, b.metrics.available, b.metrics.limit, b.metrics.owed)
            for b in balances
        ] == [
            ("credit_card", "CLP", 3600000.0, 4000000.0, 400000.0),
            ("credit_card", "USD", 1950.0, 2000.0, 50.0),
        ]
        assert all(b.institution == "banchile" for b in balances)
        # One shared plastic: both currency slices carry the same masked tail.
        assert [b.attributes.last4 for b in balances] == ["0000", "0000"]

    def test_missing_utilizado_degrades_gracefully(self):
        # A relabeled/absent "Utilizado" must not cost the available/límite:
        # owed just stays None (net worth falls back to límite − available).
        balances = card_balances_from_text(CARD_DETAIL.replace("Utilizado", "Ocupado"))
        assert [
            (b.currency, b.metrics.available, b.metrics.limit, b.metrics.owed)
            for b in balances
        ] == [
            ("CLP", 3600000.0, 4000000.0, None),
            ("USD", 1950.0, 2000.0, None),
        ]

    def test_no_masked_number_no_attributes(self):
        # Without the "****0000" header the card still emits — attributes are
        # simply not attached (never a guessed/empty last4).
        text = "Nacional\nDisponible\n$ 4.000.000\nCupo total\n$ 5.000.000\n"
        balances = card_balances_from_text(text)
        assert [(b.currency, b.attributes) for b in balances] == [("CLP", None)]

    def test_empty_page_no_balances(self):
        assert card_balances_from_text("nada") == []


class TestLineaSaldo:
    def test_reads_available_authorized_and_utilizado(self):
        text = (
            "Monto autorizado\n$ 100.000\n"
            "Saldo disponible\n$ 100.000\n"
            "Monto utilizado\n$ 0\n"
        )
        # An untouched line reports "Monto utilizado $ 0" — owed 0 is a real
        # figure worth recording, not a missing one.
        assert linea_saldo_from_text(text) == {
            "available": 100000.0,
            "limit": 100000.0,
            "owed": 0.0,
        }

    def test_used_line_reads_monto_utilizado(self):
        text = (
            "Monto autorizado\n$ 500.000\n"
            "Saldo disponible\n$ 200.000\n"
            "Monto utilizado\n$ 300.000\n"
        )
        assert linea_saldo_from_text(text) == {
            "available": 200000.0,
            "limit": 500000.0,
            "owed": 300000.0,
        }

    def test_missing_utilizado_still_yields_available_and_limit(self):
        # A relabeled/absent "Monto utilizado" degrades gracefully: no "owed"
        # key, and net worth falls back to limit − available = 300.000.
        text = "Monto autorizado\n$ 500.000\nSaldo disponible\n$ 200.000\n"
        entry = linea_saldo_from_text(text)
        assert entry == {"available": 200000.0, "limit": 500000.0}
        assert entry["limit"] - entry["available"] == 300000.0

    def test_none_when_no_disponible(self):
        assert linea_saldo_from_text("Monto autorizado\n$ 100.000") is None
        assert linea_saldo_from_text("") is None


class TestLineaBalances:
    def test_monto_utilizado_becomes_owed(self):
        text = (
            "Monto autorizado\n$ 100.000\n"
            "Saldo disponible\n$ 80.000\n"
            "Monto utilizado\n$ 20.000\n"
        )
        balances = linea_balances_from_text(text)
        assert [
            (b.kind, b.currency, b.metrics.available, b.metrics.limit, b.metrics.owed)
            for b in balances
        ] == [("line_of_credit", "CLP", 80000.0, 100000.0, 20000.0)]

    def test_emits_without_utilizado_owed_none(self):
        # "Monto utilizado" missing -> the línea still emits with its available
        # and límite; owed stays None (net worth falls back to límite − available).
        text = "Monto autorizado\n$ 100.000\nSaldo disponible\n$ 80.000\n"
        balances = linea_balances_from_text(text)
        assert [
            (b.kind, b.currency, b.metrics.available, b.metrics.limit, b.metrics.owed)
            for b in balances
        ] == [("line_of_credit", "CLP", 80000.0, 100000.0, None)]

    def test_no_cupo_means_no_emission(self):
        # Without the authorized cupo, storing "available" would be counted as
        # debt — so nothing is emitted (issue #30), even with a utilizado.
        assert linea_balances_from_text("Saldo disponible\n$ 80.000") == []
        assert (
            linea_balances_from_text(
                "Saldo disponible\n$ 80.000\nMonto utilizado\n$ 20.000\n"
            )
            == []
        )


# --- Issue #36 fixtures: per-holding depósitos / fondos listings ---------------
# The structure mirrors the real listing pages (headers, DISPONIBLE PARA
# INVERTIR / INDICADORES noise, card stacks, cross-sell and glossary blocks) but
# every figure, date, identifier and name is fabricated: deposit numbers are
# 17-digit zero runs, amounts come from the canonical synthetic set, the account
# number is 00-000-00000-01, all dates are in 2026, fund names are "Banchile
# Fondo Sintético X" and the account holder is an obviously fake placeholder.
# Deposit Monto Inicial values (aside-only): 2.500.000 + 1.000.000 + 999.999,
# which is why the header total is 4.499.999.
DEPOSITOS_LISTADO = """Mis Productos
Inversiones
Depósito a plazo
SALDO TOTAL DE MIS DEPÓSITOS

|

OCULTAR SALDOS

CLP $ 4.499.999

al día de hoy

Pesos

Saldo Depósitos en Pesos

$ 4.499.999

Cantidad

3

Próximo a vencer

01-08-2026
DISPONIBLE PARA INVERTIR

Cuenta Corriente

00-000-00000-01

$ 2.500.000

Simula tu depósito a plazo

SIMULAR
INDICADORES ECONÓMICOS

DÓLAR

$ 999,99

UF

$ 39.999,99
INDICADORES DE MERCADOS
MIS DEPÓSITOS A PLAZO
CARTOLA HISTÓRICA

ACTIVO

Depósito a Plazo

Tipo de depósito

Renovable

Vencimiento

01/08/2026

Monto a recibir

$ 2.600.000
VER DETALLE
OPERAR

ACTIVO

Depósito a Plazo

Tipo de depósito

Renovable

Vencimiento

15/09/2026

Monto a recibir

$ 1.100.000
VER DETALLE
OPERAR

ACTIVO

Depósito a Plazo

Tipo de depósito

Renovable

Vencimiento

30/12/2026

Monto a recibir

$ 1.111.111
VER DETALLE
OPERAR

Te puede interesar:

Fondos Mutuos

ETFs

Monto Inversión: Para Depósitos Renovables es el monto de última renovación.
Monto a Recibir: Monto a recibir al cumplirse la fecha de vencimiento.
"""

# One detail aside per deposit, in card order; the label stack mirrors the real
# "Detalle del Depósito a Plazo" aside. Every figure/date/id is fabricated.
DEPOSITO_ASIDE_1 = """Detalle del Depósito a Plazo
Depósito a plazo

N° Depósito 00000000000000001

Monto a recibir

$ 2.600.000

ACTIVO

Fecha de vencimiento

01/08/2026

Monto Inicial

$ 2.500.000

Tipo de depósito

Renovable

Tipo de moneda

CLP

Plazo

30 días

Tasa base (360 días)

0,40%

Tasa período

0,45%

Fecha emisión

01/07/2026
"""

DEPOSITO_ASIDE_2 = """Detalle del Depósito a Plazo
Depósito a plazo

N° Depósito 00000000000000002

Monto a recibir

$ 1.100.000

ACTIVO

Fecha de vencimiento

15/09/2026

Monto Inicial

$ 1.000.000

Tipo de depósito

Renovable

Tipo de moneda

CLP

Plazo

60 días

Tasa base (360 días)

0,45%

Tasa período

0,50%

Fecha emisión

15/07/2026
"""

DEPOSITO_ASIDE_3 = """Detalle del Depósito a Plazo
Depósito a plazo

N° Depósito 00000000000000003

Monto a recibir

$ 1.111.111

ACTIVO

Fecha de vencimiento

30/12/2026

Monto Inicial

$ 999.999

Tipo de depósito

Renovable

Tipo de moneda

CLP

Plazo

90 días

Tasa base (360 días)

0,50%

Tasa período

0,55%

Fecha emisión

01/10/2026
"""

# The fondos listing. The FONDO RECOMENDADO marketing block ABOVE the holdings
# carries a fund name plus a near-miss "VER DETALLES" button, and the "Cuentas
# de Inversión:" line carries the (fake) account holder's full name; parsers
# must skip both. Card "Mi saldo" values sum to the header total: 1.099.999 +
# 450.000 + 250.001 = 1.800.000.
FONDOS_LISTADO = """Mis Productos
Inversiones
Fondos Mutuos

Cuentas de Inversión: 1 - NOMBRE APELLIDO SINTÉTICO

SALDO TOTAL EN FONDOS MUTUOS

|

OCULTAR SALDOS

$ 1.800.000

Al 16 de julio 2026

Variación este mes

$ -50.000
DISPONIBLE PARA INVERTIR

Cuenta Corriente

00-000-00000-01

$ 2.500.000

Caja Banchile

$ 0

RECOMENDACIÓN

FONDO RECOMENDADO SEGÚN TU PERFIL

Fondo Sintético Recomendado
ACERCA DEL FONDO
Tu perfil de inversionista elegido es: Moderado
VER DETALLES
MIS FONDOS MUTUOS

La información de tus fondos está actualizada al día de ayer. Los gráficos muestran el valor cuota de los últimos 30 días de cada instrumento.

Banchile Fondo Sintético A

Serie Digital

Lo que he invertido:

$ 1.000.000

Variación histórica:

 $ 99.999

Mi saldo

$ 1.099.999
VER DETALLE
OPERAR

Banchile Fondo Sintético B

Serie Digital

Lo que he invertido:

$ 500.000

Variación histórica:

 $ -50.000

Mi saldo

$ 450.000
VER DETALLE
OPERAR

Banchile Fondo Sintético C

Serie L

Lo que he invertido:

$ 250.000

Variación histórica:

 $ 1

Mi saldo

$ 250.001
VER DETALLE
OPERAR

Revisa nuestra completa oferta de Fondos Mutuos

VER TODA LA OFERTA

Te puede interesar:
"""

# One "Acerca del fondo" aside per fund, in card order. The yesterday/today
# saldo pairs, cuotas and Valor Cuota lines mirror the real aside; only the
# Var. Diaria / Var. 30 días / Acumulada año percentages are consumed (signed,
# comma decimals). Every figure is fabricated.
FONDO_ASIDE_A = """Acerca del fondo
Banchile Fondo Sintético A
VER FICHA
Como cerró mi fondo ayer:

Mi saldo

$ 1.099.998

N° de cuotas:

100,0000

Valor cuota:

$ 1.099,9980

 Como está hoy:

Mi saldo

$ 1.099.999

N° de cuotas:

100,0000

Descripción del fondo

Fondo sintético de ejemplo. - Riesgo Medio (R4)

Moneda
PESO
Riesgo

Riesgo 4 - MEDIO

Serie: Digital

Fecha:

Valor Cuota: $ 1.099,9980

Var. Diaria

-0,1234%

Var. 30 días

+1,2345%

Acumulada año

+12,34%

Conoce otras series del fondo:

VER OTRAS SERIES
REINVERTIR
RESCATAR
APORTAR
"""

FONDO_ASIDE_B = """Acerca del fondo
Banchile Fondo Sintético B
VER FICHA
Como cerró mi fondo ayer:

Mi saldo

$ 449.999

Serie: Digital

Fecha:

Var. Diaria

-0,5678%

Var. 30 días

-2,3456%

Acumulada año

-5,67%

REINVERTIR
RESCATAR
APORTAR
"""

FONDO_ASIDE_C = """Acerca del fondo
Banchile Fondo Sintético C
VER FICHA
Como cerró mi fondo ayer:

Mi saldo

$ 250.000

Serie: L

Fecha:

Var. Diaria

+0,0001%

Var. 30 días

+0,1000%

Acumulada año

+1,00%

REINVERTIR
RESCATAR
APORTAR
"""


class TestParseUtilities:
    def test_pct_plain(self):
        assert _parse_pct("0,45%") == 0.45

    def test_pct_signed_four_decimals(self):
        # The fund aside always prints the sign, with four comma decimals.
        assert _parse_pct("+1,2345%") == 1.2345
        assert _parse_pct("-0,1234%") == -0.1234

    def test_pct_space_before_percent(self):
        # The resumen tiles / evolution rows put a space before "%".
        assert _parse_pct("12,34 %") == 12.34

    def test_pct_missing(self):
        assert _parse_pct(None) is None
        assert _parse_pct("sin dato") is None

    def test_date_ddmmyyyy(self):
        assert _parse_date_ddmmyyyy("01/08/2026") == datetime.date(2026, 8, 1)

    def test_date_invalid_or_missing(self):
        assert _parse_date_ddmmyyyy("99/99/2026") is None
        assert _parse_date_ddmmyyyy("sin fecha") is None
        assert _parse_date_ddmmyyyy(None) is None

    def test_term_days(self):
        assert _parse_term_days("30 días") == 30

    def test_term_days_missing(self):
        assert _parse_term_days("un mes") is None
        assert _parse_term_days(None) is None


class TestDepositosHeader:
    def test_reads_total_and_cantidad(self):
        assert depositos_header_from_text(DEPOSITOS_LISTADO) == {
            "total": 4499999,
            "cantidad": 3,
        }

    def test_missing_labels_yield_none(self):
        assert depositos_header_from_text("Depósito a plazo") == {
            "total": None,
            "cantidad": None,
        }
        assert depositos_header_from_text("") == {"total": None, "cantidad": None}
        assert depositos_header_from_text(None) == {"total": None, "cantidad": None}


class TestDepositoCards:
    def test_parses_all_cards(self):
        cards = deposito_cards_from_text(DEPOSITOS_LISTADO)
        assert [
            (c["tipo_deposito"], c["vencimiento"], c["monto_a_recibir"])
            for c in cards
        ] == [
            ("Renovable", datetime.date(2026, 8, 1), 2600000),
            ("Renovable", datetime.date(2026, 9, 15), 1100000),
            ("Renovable", datetime.date(2026, 12, 30), 1111111),
        ]

    def test_noise_above_the_anchor_is_not_a_card(self):
        # DISPONIBLE PARA INVERTIR ($2.500.000) and the INDICADORES figures sit
        # before "MIS DEPÓSITOS A PLAZO" and must never count as deposits.
        amounts = {c["monto_a_recibir"] for c in deposito_cards_from_text(DEPOSITOS_LISTADO)}
        assert 2500000 not in amounts
        assert len(deposito_cards_from_text(DEPOSITOS_LISTADO)) == 3

    def test_glossary_below_the_end_marker_is_not_a_card(self):
        # The glossary re-uses the card labels ("Monto a Recibir: ...") after
        # "Te puede interesar:"; it must not add a fourth card.
        assert len(deposito_cards_from_text(DEPOSITOS_LISTADO)) == 3

    def test_missing_anchor_yields_no_cards(self):
        assert deposito_cards_from_text("Depósito a Plazo\nMonto a recibir\n$ 1") == []
        assert deposito_cards_from_text("") == []
        assert deposito_cards_from_text(None) == []


class TestDepositoAside:
    def test_parses_every_field(self):
        assert deposito_aside_from_text(DEPOSITO_ASIDE_1) == {
            "numero": "00000000000000001",
            "monto_inicial": 2500000,
            "monto_a_recibir": 2600000,
            "fecha_vencimiento": datetime.date(2026, 8, 1),
            "fecha_emision": datetime.date(2026, 7, 1),
            "tipo_deposito": "Renovable",
            "tipo_moneda": "CLP",
            "plazo_dias": 30,
            "tasa_periodo_pct": 0.45,
        }

    def test_tasa_periodo_not_confused_with_tasa_base(self):
        # The aside prints "Tasa base (360 días)" right above "Tasa período";
        # only the período rate feeds interest_rate_pct.
        assert deposito_aside_from_text(DEPOSITO_ASIDE_1)["tasa_periodo_pct"] == 0.45

    def test_each_missing_label_yields_none(self):
        # Corrupt one label at a time (existing-fixture style): the field goes
        # None and every other field still parses.
        cases = {
            "N° Depósito": "numero",
            "Monto Inicial": "monto_inicial",
            "Monto a recibir": "monto_a_recibir",
            "Fecha de vencimiento": "fecha_vencimiento",
            "Fecha emisión": "fecha_emision",
            "Tipo de depósito": "tipo_deposito",
            "Tipo de moneda": "tipo_moneda",
            "Plazo": "plazo_dias",
            "Tasa período": "tasa_periodo_pct",
        }
        for label, field in cases.items():
            # Corrupt the label only where it leads its own line, so replacing
            # "Plazo" cannot also clobber the "...a Plazo" aside anchor.
            aside = deposito_aside_from_text(
                DEPOSITO_ASIDE_1.replace(f"\n{label}", "\nEtiqueta Desconocida")
            )
            assert aside[field] is None, label
            for other_field in set(cases.values()) - {field}:
                assert aside[other_field] is not None, (label, other_field)

    def test_last_aside_block_wins(self):
        # If aside text accumulates in body.innerText, only the freshest
        # (last) block must be read.
        aside = deposito_aside_from_text(
            DEPOSITOS_LISTADO + DEPOSITO_ASIDE_1 + DEPOSITO_ASIDE_2
        )
        assert aside["numero"] == "00000000000000002"
        assert aside["monto_inicial"] == 1000000

    def test_no_aside_block_returns_none(self):
        assert deposito_aside_from_text(DEPOSITOS_LISTADO) is None
        assert deposito_aside_from_text("") is None
        assert deposito_aside_from_text(None) is None


def _deposito_asides():
    return [
        deposito_aside_from_text(DEPOSITO_ASIDE_1),
        deposito_aside_from_text(DEPOSITO_ASIDE_2),
        deposito_aside_from_text(DEPOSITO_ASIDE_3),
    ]


class TestBuildDepositosProducts:
    def test_happy_path_builds_typed_products(self):
        products = build_depositos_products(DEPOSITOS_LISTADO, _deposito_asides(), 0)
        assert [
            (p.kind, p.currency, p.external_ref, p.name, p.metrics.balance)
            for p in products
        ] == [
            ("term_deposit", "CLP", "00000000000000001", "Depósito a Plazo 0001", 2500000),
            ("term_deposit", "CLP", "00000000000000002", "Depósito a Plazo 0002", 1000000),
            ("term_deposit", "CLP", "00000000000000003", "Depósito a Plazo 0003", 999999),
        ]
        assert all(p.institution == "banchile" for p in products)
        first = products[0].attributes
        assert first.principal == 2500000
        assert first.maturity_value == 2600000
        assert first.maturity_date == datetime.date(2026, 8, 1)
        assert first.issue_date == datetime.date(2026, 7, 1)
        assert first.interest_rate_pct == 0.45
        assert first.term_days == 30
        assert first.deposit_type == "Renovable"

    def test_balance_is_monto_inicial_never_monto_a_recibir(self):
        # Convention: the portal shows no per-deposit current value, and the
        # page total equals the sum of Monto Inicial, so balance = principal.
        # The future "Monto a recibir" must never be the balance.
        products = build_depositos_products(DEPOSITOS_LISTADO, _deposito_asides(), 0)
        assert [p.metrics.balance for p in products] == [2500000, 1000000, 999999]
        assert 2600000 not in {p.metrics.balance for p in products}

    def test_missing_aside_fails_non_final_attempt(self):
        # Only 2 asides for 3 cards (Cantidad 3): retryable, so [].
        assert build_depositos_products(DEPOSITOS_LISTADO, _deposito_asides()[:2], 0) == []

    def test_cantidad_mismatch_fails_non_final_attempt(self):
        # The header says 2 but three cards parsed: inconsistent page, retry.
        listado = DEPOSITOS_LISTADO.replace("Cantidad\n\n3", "Cantidad\n\n2")
        assert build_depositos_products(listado, _deposito_asides(), 0) == []

    def test_duplicate_numero_fails_non_final_attempt(self):
        asides = _deposito_asides()
        asides[2]["numero"] = asides[0]["numero"]
        assert build_depositos_products(DEPOSITOS_LISTADO, asides, 0) == []

    def test_incomplete_aside_fails_non_final_attempt(self):
        asides = _deposito_asides()
        asides[1]["monto_inicial"] = None
        assert build_depositos_products(DEPOSITOS_LISTADO, asides, 0) == []

    def test_final_attempt_emits_partial_complete_holdings(self):
        # Final attempt relaxes the count checks: two complete asides (of three
        # cards) still become two products.
        products = build_depositos_products(DEPOSITOS_LISTADO, _deposito_asides()[:2], 2)
        assert [p.external_ref for p in products] == [
            "00000000000000001",
            "00000000000000002",
        ]

    def test_final_attempt_incomplete_aside_falls_back_to_rollup(self):
        # An aside without its Monto Inicial is money we can't attribute: the
        # roll-up (which covers ALL deposits) is safer than a partial emit.
        asides = _deposito_asides()
        asides[1]["monto_inicial"] = None
        products = build_depositos_products(DEPOSITOS_LISTADO, asides, 2)
        assert [(p.kind, p.external_ref, p.name, p.metrics.balance) for p in products] == [
            ("term_deposit", None, None, 4499999)
        ]

    def test_final_attempt_no_asides_falls_back_to_rollup(self):
        # The legacy summed shape: no external_ref, no name, header total.
        products = build_depositos_products(DEPOSITOS_LISTADO, [], 2)
        assert [(p.kind, p.external_ref, p.name, p.metrics.balance) for p in products] == [
            ("term_deposit", None, None, 4499999)
        ]

    def test_never_mixes_rollup_and_per_holding(self):
        # Whatever the input, the result is either all-ref'd or one None-ref.
        for asides in ([], _deposito_asides()[:1], _deposito_asides()):
            for attempt in (0, 2):
                products = build_depositos_products(DEPOSITOS_LISTADO, asides, attempt)
                refs = {p.external_ref is None for p in products}
                assert len(refs) <= 1

    def test_total_failure_returns_empty(self):
        assert build_depositos_products("nada por aquí", [], 2) == []
        assert build_depositos_products(None, [], 2) == []


class TestFondosHeader:
    def test_reads_total(self):
        assert fondos_header_from_text(FONDOS_LISTADO) == 1800000

    def test_missing_yields_none(self):
        assert fondos_header_from_text("Fondos Mutuos") is None
        assert fondos_header_from_text("") is None
        assert fondos_header_from_text(None) is None


class TestFondoCards:
    def test_parses_all_cards(self):
        cards = fondo_cards_from_text(FONDOS_LISTADO)
        assert [
            (c["name"], c["serie"], c["invertido"], c["variacion"], c["saldo"])
            for c in cards
        ] == [
            ("Banchile Fondo Sintético A", "Digital", 1000000, 99999, 1099999),
            ("Banchile Fondo Sintético B", "Digital", 500000, -50000, 450000),
            ("Banchile Fondo Sintético C", "L", 250000, 1, 250001),
        ]

    def test_serie_variants_word_and_single_letter(self):
        series = [c["serie"] for c in fondo_cards_from_text(FONDOS_LISTADO)]
        assert series == ["Digital", "Digital", "L"]

    def test_signed_variacion_historica(self):
        # The page prints a leading space (positive) or "$ -" (negative).
        variaciones = [c["variacion"] for c in fondo_cards_from_text(FONDOS_LISTADO)]
        assert variaciones == [99999, -50000, 1]

    def test_marketing_fund_above_anchor_is_not_a_card(self):
        # The FONDO RECOMENDADO block sits above "MIS FONDOS MUTUOS" and its
        # fund name must never become a holding.
        names = {c["name"] for c in fondo_cards_from_text(FONDOS_LISTADO)}
        assert "Fondo Sintético Recomendado" not in names

    def test_missing_anchor_yields_no_cards(self):
        assert fondo_cards_from_text("Serie L\nMi saldo\n$ 1") == []
        assert fondo_cards_from_text("") == []
        assert fondo_cards_from_text(None) == []


class TestFondoAside:
    def test_parses_signed_four_decimal_vars(self):
        assert fondo_aside_from_text(FONDO_ASIDE_A) == {
            "fund_name": "Banchile Fondo Sintético A",
            "serie": "Digital",
            "var_daily_pct": -0.1234,
            "var_30d_pct": 1.2345,
            "var_ytd_pct": 12.34,
        }

    def test_last_aside_block_wins(self):
        aside = fondo_aside_from_text(FONDOS_LISTADO + FONDO_ASIDE_A + FONDO_ASIDE_B)
        assert aside["fund_name"] == "Banchile Fondo Sintético B"
        assert aside["var_daily_pct"] == -0.5678

    def test_marketing_block_is_not_an_aside(self):
        # The listing's all-caps "ACERCA DEL FONDO" marketing button must not
        # anchor an aside block (the real aside title is "Acerca del fondo").
        assert fondo_aside_from_text(FONDOS_LISTADO) is None

    def test_missing_fields_yield_none(self):
        aside = fondo_aside_from_text("Acerca del fondo\nBanchile Fondo Sintético A\n")
        assert aside["fund_name"] == "Banchile Fondo Sintético A"
        assert aside["serie"] is None
        assert aside["var_daily_pct"] is None
        assert aside["var_30d_pct"] is None
        assert aside["var_ytd_pct"] is None

    def test_no_aside_returns_none(self):
        assert fondo_aside_from_text("") is None
        assert fondo_aside_from_text(None) is None


def _fondo_asides():
    return [
        fondo_aside_from_text(FONDO_ASIDE_A),
        fondo_aside_from_text(FONDO_ASIDE_B),
        fondo_aside_from_text(FONDO_ASIDE_C),
    ]


class TestBuildFondosProducts:
    def test_happy_path_builds_enriched_products(self):
        products = build_fondos_products(FONDOS_LISTADO, _fondo_asides(), 0)
        assert [
            (p.kind, p.currency, p.external_ref, p.name) for p in products
        ] == [
            ("investment", "CLP", "Banchile Fondo Sintético A|Digital", "Banchile Fondo Sintético A"),
            ("investment", "CLP", "Banchile Fondo Sintético B|Digital", "Banchile Fondo Sintético B"),
            ("investment", "CLP", "Banchile Fondo Sintético C|L", "Banchile Fondo Sintético C"),
        ]
        assert all(p.institution == "banchile" for p in products)
        assert [p.attributes.fund_name for p in products] == [
            "Banchile Fondo Sintético A",
            "Banchile Fondo Sintético B",
            "Banchile Fondo Sintético C",
        ]
        a, b, c = (p.metrics for p in products)
        assert (a.nav, a.deposited, a.profit) == (1099999, 1000000, 99999)
        assert (a.var_daily_pct, a.var_30d_pct, a.var_ytd_pct) == (-0.1234, 1.2345, 12.34)
        assert (b.nav, b.deposited, b.profit) == (450000, 500000, -50000)
        assert (b.var_daily_pct, b.var_30d_pct, b.var_ytd_pct) == (-0.5678, -2.3456, -5.67)
        assert (c.var_daily_pct, c.var_30d_pct, c.var_ytd_pct) == (0.0001, 0.1, 1.0)

    def test_nav_comes_from_the_card_not_the_aside(self):
        # The aside shows a "hoy" saldo that can differ from the card's; the
        # card's "Mi saldo" wins so all funds share one freshness basis.
        products = build_fondos_products(FONDOS_LISTADO, _fondo_asides(), 0)
        assert products[0].metrics.nav == 1099999  # card figure, not 1099998

    def test_missing_asides_leave_vars_none(self):
        products = build_fondos_products(FONDOS_LISTADO, [], 0)
        assert len(products) == 3
        for p in products:
            assert p.metrics.var_daily_pct is None
            assert p.metrics.var_30d_pct is None
            assert p.metrics.var_ytd_pct is None
        # nav/deposited/profit still come from the cards.
        assert products[0].metrics.nav == 1099999

    def test_mismatched_aside_is_ignored(self):
        # An aside whose name+serie matches no card must not enrich anything.
        aside = fondo_aside_from_text(FONDO_ASIDE_A)
        aside["serie"] = "Otra"
        products = build_fondos_products(FONDOS_LISTADO, [aside], 0)
        assert products[0].metrics.var_daily_pct is None

    def test_sum_mismatch_fails_non_final_attempt(self):
        listado = FONDOS_LISTADO.replace("$ 1.800.000", "$ 9.999.999")
        assert build_fondos_products(listado, _fondo_asides(), 0) == []

    def test_sum_mismatch_still_emits_on_final_attempt(self):
        listado = FONDOS_LISTADO.replace("$ 1.800.000", "$ 9.999.999")
        products = build_fondos_products(listado, _fondo_asides(), 2)
        assert len(products) == 3

    def test_missing_header_fails_non_final_attempt(self):
        # Without the header total the sum can't be verified: retry.
        listado = FONDOS_LISTADO.replace("SALDO TOTAL EN FONDOS MUTUOS", "SALDO")
        assert build_fondos_products(listado, _fondo_asides(), 0) == []

    def test_final_attempt_no_cards_falls_back_to_rollup(self):
        # Cut the holdings section off: only the header total remains, so the
        # legacy summed shape (no external_ref/name) is emitted.
        listado = FONDOS_LISTADO.split("MIS FONDOS MUTUOS")[0]
        products = build_fondos_products(listado, [], 2)
        assert [(p.kind, p.external_ref, p.name, p.metrics.nav) for p in products] == [
            ("investment", None, None, 1800000)
        ]

    def test_no_cards_fails_non_final_attempt(self):
        listado = FONDOS_LISTADO.split("MIS FONDOS MUTUOS")[0]
        assert build_fondos_products(listado, [], 0) == []

    def test_holder_name_never_reaches_a_product(self):
        # The "Cuentas de Inversión:" line carries the customer's full name
        # (here a fake placeholder); it must never leak into any field.
        products = build_fondos_products(FONDOS_LISTADO, _fondo_asides(), 0)
        for p in products:
            assert "APELLIDO" not in (p.external_ref or "")
            assert "APELLIDO" not in (p.name or "")
            assert "APELLIDO" not in (p.attributes.fund_name or "")

    def test_total_failure_returns_empty(self):
        assert build_fondos_products("nada por aquí", [], 2) == []
        assert build_fondos_products(None, [], 2) == []


class TestMergeBalances:
    def _checking(self, currency, balance):
        return ScrapedProduct(
            institution="banchile",
            kind="checking",
            currency=currency,
            metrics=CheckingMetrics(balance=balance),
        )

    def _card(self, currency, available, limit=None):
        return ScrapedProduct(
            institution="banchile",
            kind="credit_card",
            currency=currency,
            metrics=CreditCardMetrics(available=available, limit=limit),
        )

    def test_detail_supersedes_dashboard_for_same_product(self):
        base = [self._card("CLP", 999999)]
        extra = [self._card("CLP", 3600000.0, 4000000.0)]
        merged = _merge_balances(base, extra)
        assert len(merged) == 1
        assert merged[0].metrics.available == 3600000.0
        assert merged[0].metrics.limit == 4000000.0

    def test_keeps_distinct_products_and_appends(self):
        base = [
            self._checking("CLP", 2500000),
            self._card("CLP", 999999),
        ]
        extra = [
            self._card("CLP", 3600000.0, 4000000.0),
            self._card("USD", 1950.0, 2000.0),
        ]
        merged = _merge_balances(base, extra)
        assert [(b.kind, b.currency) for b in merged] == [
            ("checking", "CLP"),
            ("credit_card", "CLP"),
            ("credit_card", "USD"),
        ]

    def test_empty_extra_returns_base(self):
        base = [self._checking("CLP", 100)]
        assert _merge_balances(base, []) is base

    def test_per_holding_products_do_not_clobber_each_other(self):
        # Same (kind, currency) but distinct external_refs: all must survive a
        # merge, and only the ref that reappears is superseded.
        def deposit(ref, balance):
            from product_model import TermDepositMetrics

            return ScrapedProduct(
                institution="banchile",
                kind="term_deposit",
                currency="CLP",
                external_ref=ref,
                metrics=TermDepositMetrics(balance=balance),
            )

        base = [deposit("00000000000000001", 2500000), deposit("00000000000000002", 1000000)]
        extra = [deposit("00000000000000002", 999999)]
        merged = _merge_balances(base, extra)
        assert [(b.external_ref, b.metrics.balance) for b in merged] == [
            ("00000000000000001", 2500000),
            ("00000000000000002", 999999),
        ]


# Synthetic línea detail text — the labels mirror the real "Saldos y movimientos
# de la línea" page; every figure is fabricated (autorizado 100.000 /
# disponible 80.000 / utilizado 20.000).
LINEA_DETAIL = (
    "Monto autorizado\n$ 100.000\n"
    "Saldo disponible\n$ 80.000\n"
    "Monto utilizado\n$ 20.000\n"
)


def _fake_portal_page(fondos_texts=None):
    """Stateful portal stand-in whose visible text follows the SPA route.

    The dashboard is the initial (and post-recovery) text, the card shortcut
    click opens the card detail, a hash assignment opens the routed page, and
    each listing's VER DETALLE buttons APPEND their holding's aside text to the
    page text (the behavior observed live) until the aside close control clears
    it — so `_read_all_surfaces` can be driven attempt by attempt, aside by
    aside, with no browser.

    Failure injection handles: `page.card_link` is the card shortcut element,
    `page.detail_buttons["depósitos"|"fondos"][i]` the per-card VER DETALLE
    elements (make `.wait_for` raise to simulate a missing control, or clear
    `.click.side_effect` for a click that opens nothing), and `page.close_button`
    the aside close control. `fondos_texts` serves successive texts per fondos
    navigation (the last one repeats), for pages that render wrong once.
    """
    page = MagicMock()
    page.url = (
        "https://portalpersonas.bancochile.cl/mibancochile-web/front/persona/"
        "index.html#/home"
    )
    state = {"route": "home", "asides": [], "fondos_visits": 0}
    deposito_asides = (DEPOSITO_ASIDE_1, DEPOSITO_ASIDE_2, DEPOSITO_ASIDE_3)
    fondo_asides = (FONDO_ASIDE_A, FONDO_ASIDE_B, FONDO_ASIDE_C)

    def base_text():
        if state["route"] == "fondos":
            if fondos_texts:
                return fondos_texts[min(state["fondos_visits"] - 1, len(fondos_texts) - 1)]
            return FONDOS_LISTADO
        return {
            "home": REAL_DASHBOARD,
            "card": CARD_DETAIL,
            "linea": LINEA_DETAIL,
            "depósitos": DEPOSITOS_LISTADO,
        }[state["route"]]

    def evaluate(js, *args):
        if args and isinstance(args[0], str):  # hash assignment (SPA navigation)
            state["asides"] = []
            if "linea" in args[0]:
                state["route"] = "linea"
            elif "depositos-a-plazo" in args[0]:
                state["route"] = "depósitos"
            elif "fondos-mutuos" in args[0]:
                state["route"] = "fondos"
                state["fondos_visits"] += 1
            return None
        if args:  # JS-click fallback on an element handle: no-op
            return None
        return base_text() + "".join(state["asides"])

    def _detail_button(route, index):
        button = MagicMock()
        aside_text = (deposito_asides if route == "depósitos" else fondo_asides)[index]

        def click():
            state["asides"].append(aside_text)

        button.click.side_effect = click
        return button

    detail_buttons = {
        route: [_detail_button(route, i) for i in range(3)]
        for route in ("depósitos", "fondos")
    }

    card_link = MagicMock()
    card_link.click.side_effect = lambda: state.update(route="card")
    close_button = MagicMock()
    close_button.click.side_effect = lambda: state.update(asides=[])

    def locator(selector):
        loc = MagicMock()
        if "ver-detalle" in selector or "Ver detalle" in selector:
            buttons = detail_buttons.get(state["route"], [])

            def nth(index):
                if index < len(buttons):
                    return buttons[index]
                missing = MagicMock()
                missing.wait_for.side_effect = RuntimeError("no such card")
                return missing

            loc.nth.side_effect = nth
        elif "SALDOS" in selector or "tarjeta-credito" in selector:
            loc.first = card_link  # the dashboard "SALDOS Y MOV. TARJETAS" shortcut
        else:  # every aside close control (modal-close / Cerrar / aria-label)
            loc.first = close_button
        return loc

    def goto(url, **kwargs):
        state["route"] = "home"
        state["asides"] = []

    page.evaluate.side_effect = evaluate
    page.locator.side_effect = locator
    page.goto.side_effect = goto
    page.card_link = card_link
    page.close_button = close_button
    page.detail_buttons = detail_buttons
    return page


# The full per-holding product list a clean run of the fake portal yields.
_ALL_SURFACE_PRODUCTS = [
    ("checking", "CLP", None),
    ("checking", "USD", None),
    ("credit_card", "CLP", None),
    ("credit_card", "USD", None),
    ("line_of_credit", "CLP", None),
    ("term_deposit", "CLP", "00000000000000001"),
    ("term_deposit", "CLP", "00000000000000002"),
    ("term_deposit", "CLP", "00000000000000003"),
    ("investment", "CLP", "Banchile Fondo Sintético A|Digital"),
    ("investment", "CLP", "Banchile Fondo Sintético B|Digital"),
    ("investment", "CLP", "Banchile Fondo Sintético C|L"),
]


class TestSurfaceRetries:
    def test_all_surfaces_parse_without_retries(self):
        """Every surface renders on attempt 1: no pauses, no recoveries."""
        page = _fake_portal_page()

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        assert [
            (b.kind, b.currency, b.external_ref) for b in result.products
        ] == _ALL_SURFACE_PRODUCTS
        page.goto.assert_not_called()
        page.wait_for_timeout.assert_not_called()

    def test_per_holding_products_carry_aside_data(self):
        """The deposit asides and fund asides feed the typed payloads."""
        page = _fake_portal_page()

        result = _read_all_surfaces(page)

        deposits = [b for b in result.products if b.kind == "term_deposit"]
        assert [b.metrics.balance for b in deposits] == [2500000, 1000000, 999999]
        assert deposits[0].attributes.principal == 2500000
        assert deposits[0].attributes.maturity_value == 2600000
        funds = [b for b in result.products if b.kind == "investment"]
        assert [b.metrics.nav for b in funds] == [1099999, 450000, 250001]
        assert funds[0].metrics.var_daily_pct == -0.1234
        assert funds[1].metrics.var_ytd_pct == -5.67

    def test_asides_that_accumulate_without_closing_still_parse(self):
        """If the close control is gone (and Escape does nothing), aside text
        accumulates; parsing the LAST block keeps every holding correct."""
        page = _fake_portal_page()
        page.close_button.wait_for.side_effect = RuntimeError("no close control")

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        assert [
            (b.kind, b.currency, b.external_ref) for b in result.products
        ] == _ALL_SURFACE_PRODUCTS

    def test_card_link_miss_recovers_on_second_attempt(self):
        """A card shortcut that misses once parses fine on the retry."""
        page = _fake_portal_page()
        link = page.card_link
        link.wait_for.side_effect = [RuntimeError("selector timeout")] * 3 + [None]

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        cards = [b for b in result.products if b.kind == "credit_card"]
        assert [
            (b.currency, b.metrics.available, b.metrics.limit, b.metrics.owed)
            for b in cards
        ] == [
            ("CLP", 3600000.0, 4000000.0, 400000.0),
            ("USD", 1950.0, 2000.0, 50.0),
        ]
        assert page.goto.call_count == 1
        assert [c.args[0] for c in page.wait_for_timeout.call_args_list] == [2000]

    def test_card_exhausts_bounded_attempts_and_is_reported_failed(self):
        """A card shortcut that never appears fails after exactly 3 attempts."""
        page = _fake_portal_page()
        link = page.card_link
        link.wait_for.side_effect = RuntimeError("selector timeout")

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ("card",)
        assert [(b.kind, b.currency, b.external_ref) for b in result.products] == [
            p for p in _ALL_SURFACE_PRODUCTS if p[0] != "credit_card"
        ]
        assert link.wait_for.call_count == 9
        assert page.goto.call_count == 2
        assert [c.args[0] for c in page.wait_for_timeout.call_args_list] == [2000, 4000]

    def test_extra_attempts_reuse_the_last_configured_budgets(self, monkeypatch):
        """Bumping _SURFACE_ATTEMPTS past the budget tuples clamps to their last values."""
        page = _fake_portal_page()
        link = page.card_link
        link.wait_for.side_effect = RuntimeError("selector timeout")
        monkeypatch.setattr(banchile_web_mod, "_SURFACE_ATTEMPTS", 4)

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ("card",)
        assert link.wait_for.call_count == 12
        assert page.goto.call_count == 3
        assert [c.args[0] for c in page.wait_for_timeout.call_args_list] == [2000, 4000, 4000]

    def test_flaky_deposito_aside_recovers_on_second_attempt(self):
        """One VER DETALLE that misses on attempt 1 heals on the retry."""
        page = _fake_portal_page()
        button = page.detail_buttons["depósitos"][0]
        # _click_nth tries 2 selectors per round and the rounds loop breaks on
        # a failed click, so attempt 1 consumes exactly 2 wait_for calls.
        button.wait_for.side_effect = [RuntimeError("selector timeout")] * 2 + [None] * 10

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        deposits = [b for b in result.products if b.kind == "term_deposit"]
        assert [b.external_ref for b in deposits] == [
            "00000000000000001",
            "00000000000000002",
            "00000000000000003",
        ]
        assert page.goto.call_count == 1
        assert [c.args[0] for c in page.wait_for_timeout.call_args_list] == [2000]

    def test_fondos_bad_total_recovers_on_second_attempt(self):
        """A fondos render whose cards do not add up is retried, then heals."""
        page = _fake_portal_page(
            fondos_texts=[
                FONDOS_LISTADO.replace("$ 1.800.000", "$ 9.999.999"),
                FONDOS_LISTADO,
            ]
        )

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        funds = [b for b in result.products if b.kind == "investment"]
        assert len(funds) == 3
        assert page.goto.call_count == 1

    def test_depositos_fall_back_to_rollup_on_final_attempt(self):
        """Asides that never open end in the legacy summed roll-up, not a loss."""
        page = _fake_portal_page()
        for button in page.detail_buttons["depósitos"]:
            button.wait_for.side_effect = RuntimeError("selector timeout")

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        deposits = [b for b in result.products if b.kind == "term_deposit"]
        assert [(b.external_ref, b.name, b.metrics.balance) for b in deposits] == [
            (None, None, 4499999)
        ]
        # Two retries before the fallback engaged (plus none for other surfaces).
        assert page.goto.call_count == 2
        assert [c.args[0] for c in page.wait_for_timeout.call_args_list] == [2000, 4000]

    def test_broken_aside_click_degrades_to_partial_holdings(self, monkeypatch):
        """A VER DETALLE whose click opens nothing costs only that holding."""
        page = _fake_portal_page()
        # Button 1 exists (wait_for passes) but its click opens no aside.
        page.detail_buttons["depósitos"][1].click.side_effect = None
        # Zero poll budget: the stale read returns immediately instead of
        # burning the real-time aside budget in a MagicMock loop.
        monkeypatch.setattr(banchile_web_mod, "_ASIDE_RENDER_TIMEOUTS_MS", (0, 0, 0))

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        deposits = [b for b in result.products if b.kind == "term_deposit"]
        # Final attempt emits the two complete holdings (see the builder's
        # degradation ladder); the broken one is dropped, never guessed.
        assert [b.external_ref for b in deposits] == [
            "00000000000000001",
            "00000000000000003",
        ]
