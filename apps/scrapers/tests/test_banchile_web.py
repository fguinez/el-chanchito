"""Tests for the self-contained Banco de Chile web balance backend.

These never launch a browser or hit the real bank: the pure parsing helpers
are exercised directly, and `balances_from_page` is driven with a fake page
whose `evaluate` returns canned page text.
"""

from datetime import date
from unittest.mock import MagicMock

from scrapers.backends.banchile_web import (
    _balance_from_text,
    _merge_balances,
    _usd_checking_from_text,
    balances_by_kind,
    balances_from_page,
    card_balances_from_text,
    card_saldos_from_text,
    linea_balances_from_text,
    linea_saldo_from_text,
    parse_amount,
    parse_clp,
)
from scrapers.base import ScrapedBalance


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
# every identifier and figure is fabricated. CLP available/límite = 3.600.000 /
# 4.000.000; USD = 1.950,00 / 2.000,00.
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
            "CLP": {"available": 3600000.0, "limit": 4000000.0},
            "USD": {"available": 1950.0, "limit": 2000.0},
        }

    def test_disponible_paired_with_own_currency_cupo(self):
        # The CLP "Disponible" must never pick up the USD "Cupo total" (or vice
        # versa): debt = límite − available would be nonsense across currencies.
        # (The "Cupo disponible avance" line must not be read as the límite.)
        result = card_saldos_from_text(CARD_DETAIL)
        assert result["CLP"] == {"available": 3600000.0, "limit": 4000000.0}
        assert result["USD"] == {"available": 1950.0, "limit": 2000.0}

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


# Synthetic depósito-a-plazo / fondos-mutuos blocks: no live fixture exists for
# these, so they mirror the uniform "<header> … $amount" dashboard layout. All
# figures are fabricated; the three deposits sum to 2.400.000.
DEPOSITS_BLOCK = (
    "Depósito a Plazo\n001-234\n$ 700.000\n"
    "Depósito a Plazo\n001-235\n$ 800.000\n"
    "Depósito a Plazo\n001-236\n$ 900.000\n"
)
FUNDS_BLOCK = "Fondos Mutuos\nFM Estrategia Activa\n$ 500.000\n"
FULL_DASHBOARD = REAL_DASHBOARD + DEPOSITS_BLOCK + FUNDS_BLOCK


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

    def test_term_deposits_are_summed(self):
        assert balances_by_kind(DEPOSITS_BLOCK) == {"term_deposit": 2400000}

    def test_fondos_mutuos_investment(self):
        assert balances_by_kind(FUNDS_BLOCK) == {"investment": 500000}

    def test_full_dashboard_all_four_kinds(self):
        assert balances_by_kind(FULL_DASHBOARD) == {
            "checking": 2500000,
            "credit_card": 999999,
            "term_deposit": 2400000,
            "investment": 500000,
        }

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
    def test_returns_checking_scraped_balance(self):
        balances = balances_from_page(_fake_page("Saldo Disponible $3.210.000"))
        assert len(balances) == 1
        bal = balances[0]
        assert bal.institution == "banchile"
        assert bal.product_kind == "checking"
        assert bal.currency == "CLP"
        assert bal.balance == 3210000
        assert bal.as_of == date.today()

    def test_no_balance_returns_empty(self):
        assert balances_from_page(_fake_page("nada por aquí")) == []

    def test_evaluate_failure_returns_empty(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("page closed")
        assert balances_from_page(page) == []

    def test_emits_every_kind_found_in_source_order(self):
        # CLP checking / depósito / fondos in source order, then the USD cuenta
        # corriente (USD 0,00). The dashboard credit_card figure is a placeholder
        # ($999.999) and is deliberately NOT emitted — the card is sourced from
        # its detail page instead (see test below).
        balances = balances_from_page(_fake_page(FULL_DASHBOARD))
        assert [(b.product_kind, b.currency, b.balance) for b in balances] == [
            ("checking", "CLP", 2500000),
            ("term_deposit", "CLP", 2400000),
            ("investment", "CLP", 500000),
            ("checking", "USD", 0.0),
        ]
        assert all(b.institution == "banchile" for b in balances)
        assert all(b.as_of == date.today() for b in balances)

    def test_dashboard_never_emits_the_card_placeholder(self):
        # Live QA showed the dashboard card "Disponible" is a static placeholder
        # that never matches the real available cupo, so it must not be written.
        kinds = {b.product_kind for b in balances_from_page(_fake_page(FULL_DASHBOARD))}
        assert "credit_card" not in kinds

    def test_stray_tarjeta_mention_yields_only_checking(self):
        # A "Tarjeta" that isn't the "Tarjeta de Crédito" product header must
        # not become a credit_card balance.
        text = "Saldo Disponible $1.000.000\nCupo Disponible Tarjeta $500.000"
        balances = balances_from_page(_fake_page(text))
        assert [b.product_kind for b in balances] == ["checking"]


class TestUsdChecking:
    def test_usd_cuenta_corriente_emitted_from_dashboard(self):
        text = "Cuenta Corriente\n00-000\nDisponible\nUSD 1.234,56\n"
        balances = balances_from_page(_fake_page(text))
        assert [(b.product_kind, b.currency, b.balance) for b in balances] == [
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
        assert [(b.product_kind, b.currency, b.balance) for b in balances] == [
            ("checking", "CLP", 2000000),
            ("checking", "USD", 500.0),
        ]

    def test_no_usd_checking(self):
        assert _usd_checking_from_text("Saldo Disponible $1.000.000") is None


class TestCardBalancesFromText:
    def test_emits_clp_and_usd_with_limits(self):
        balances = card_balances_from_text(CARD_DETAIL)
        assert [
            (b.product_kind, b.currency, b.balance, b.credit_limit) for b in balances
        ] == [
            ("credit_card", "CLP", 3600000.0, 4000000.0),
            ("credit_card", "USD", 1950.0, 2000.0),
        ]
        assert all(b.institution == "banchile" for b in balances)

    def test_empty_page_no_balances(self):
        assert card_balances_from_text("nada") == []


class TestLineaSaldo:
    def test_reads_available_and_authorized(self):
        text = (
            "Monto autorizado\n$ 100.000\n"
            "Saldo disponible\n$ 100.000\n"
            "Monto utilizado\n$ 0\n"
        )
        assert linea_saldo_from_text(text) == {"available": 100000.0, "limit": 100000.0}

    def test_used_line_debt_is_limit_minus_available(self):
        text = "Monto autorizado\n$ 500.000\nSaldo disponible\n$ 200.000\n"
        entry = linea_saldo_from_text(text)
        assert entry == {"available": 200000.0, "limit": 500000.0}
        # net worth computes debt = limit − available = 300.000 (the utilizado).
        assert entry["limit"] - entry["available"] == 300000.0

    def test_none_when_no_disponible(self):
        assert linea_saldo_from_text("Monto autorizado\n$ 100.000") is None
        assert linea_saldo_from_text("") is None


class TestLineaBalances:
    def test_emits_line_of_credit_with_limit(self):
        text = "Monto autorizado\n$ 100.000\nSaldo disponible\n$ 80.000\n"
        balances = linea_balances_from_text(text)
        assert [
            (b.product_kind, b.currency, b.balance, b.credit_limit) for b in balances
        ] == [("line_of_credit", "CLP", 80000.0, 100000.0)]

    def test_no_cupo_means_no_emission(self):
        # Without the authorized cupo, storing "available" would be counted as
        # debt — so nothing is emitted (issue #30).
        assert linea_balances_from_text("Saldo disponible\n$ 80.000") == []


class TestMergeBalances:
    def _bal(self, kind, currency, balance, limit=None):
        return ScrapedBalance(
            institution="banchile",
            product_kind=kind,
            balance=balance,
            as_of=date.today(),
            currency=currency,
            credit_limit=limit,
        )

    def test_detail_supersedes_dashboard_for_same_product(self):
        base = [self._bal("credit_card", "CLP", 999999)]
        extra = [self._bal("credit_card", "CLP", 3550000.0, 4000000.0)]
        merged = _merge_balances(base, extra)
        assert len(merged) == 1
        assert merged[0].balance == 3550000.0
        assert merged[0].credit_limit == 4000000.0

    def test_keeps_distinct_products_and_appends(self):
        base = [
            self._bal("checking", "CLP", 2500000),
            self._bal("credit_card", "CLP", 999999),
        ]
        extra = [
            self._bal("credit_card", "CLP", 3550000.0, 4000000.0),
            self._bal("credit_card", "USD", 2345.67, 2400.0),
        ]
        merged = _merge_balances(base, extra)
        assert [(b.product_kind, b.currency) for b in merged] == [
            ("checking", "CLP"),
            ("credit_card", "CLP"),
            ("credit_card", "USD"),
        ]

    def test_empty_extra_returns_base(self):
        base = [self._bal("checking", "CLP", 100)]
        assert _merge_balances(base, []) is base
