"""Tests for the self-contained Banco de Chile web balance backend.

These never launch a browser or hit the real bank: the pure parsing helpers
are exercised directly, and `balances_from_page` is driven with a fake page
whose `evaluate` returns canned page text.
"""

from unittest.mock import MagicMock

from product_model import CheckingMetrics, CreditCardMetrics

from scrapers.backends.banchile_web import (
    _balance_from_text,
    _merge_balances,
    _read_all_surfaces,
    _usd_checking_from_text,
    balances_by_kind,
    balances_from_page,
    card_balances_from_text,
    card_last4_from_text,
    card_saldos_from_text,
    inversiones_balances_from_text,
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


# Synthetic "Resumen de Inversión" page. The STRUCTURE mirrors the real page —
# SALDO TOTAL broken into "En activos financieros" (fondos mutuos) and "En
# depósitos y ahorros" (depósitos a plazo) — with figures from a live read:
# fondos = 1.000.000, depósitos = 2.000.000 (SALDO TOTAL 3.000.000).
INVERSION_RESUMEN = """Resumen de Inversión

SALDO TOTAL
$ 3.000.000

En activos financieros
$ 1.000.000

En depósitos y ahorros
$ 2.000.000
"""


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


class TestInversionesBalances:
    def test_reads_term_deposit_and_investment(self):
        # The resumen breakdown: "En depósitos y ahorros" -> term_deposit
        # (balance metric), "En activos financieros" -> investment (nav metric).
        balances = inversiones_balances_from_text(INVERSION_RESUMEN)
        assert [
            (b.kind, b.currency, b.metrics.headline()) for b in balances
        ] == [
            ("term_deposit", "CLP", 2000000),
            ("investment", "CLP", 1000000),
        ]
        assert all(b.institution == "banchile" for b in balances)

    def test_saldo_total_headline_is_never_grabbed(self):
        # Each label anchors on its own nearest "$"; the SALDO TOTAL headline
        # ($ 3.000.000) must not be read as either kind.
        amounts = {
            b.metrics.headline()
            for b in inversiones_balances_from_text(INVERSION_RESUMEN)
        }
        assert 3000000 not in amounts

    def test_only_deposits_present(self):
        balances = inversiones_balances_from_text("En depósitos y ahorros\n$ 2.000.000\n")
        assert [(b.kind, b.metrics.balance) for b in balances] == [
            ("term_deposit", 2000000)
        ]

    def test_only_funds_present(self):
        balances = inversiones_balances_from_text("En activos financieros\n$ 1.000.000\n")
        assert [(b.kind, b.metrics.nav) for b in balances] == [
            ("investment", 1000000)
        ]

    def test_dashboard_text_yields_nothing(self):
        # The dashboard carries no "activos financieros"/"depósitos y ahorros"
        # labels, so this reader finds nothing there.
        assert inversiones_balances_from_text(REAL_DASHBOARD) == []

    def test_empty_and_none(self):
        assert inversiones_balances_from_text("") == []
        assert inversiones_balances_from_text(None) == []


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
        extra = [self._card("CLP", 3550000.0, 4000000.0)]
        merged = _merge_balances(base, extra)
        assert len(merged) == 1
        assert merged[0].metrics.available == 3550000.0
        assert merged[0].metrics.limit == 4000000.0

    def test_keeps_distinct_products_and_appends(self):
        base = [
            self._checking("CLP", 2500000),
            self._card("CLP", 999999),
        ]
        extra = [
            self._card("CLP", 3550000.0, 4000000.0),
            self._card("USD", 2345.67, 2400.0),
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


# Synthetic línea detail text — the labels mirror the real "Saldos y movimientos
# de la línea" page; every figure is fabricated (autorizado 100.000 /
# disponible 80.000 / utilizado 20.000).
LINEA_DETAIL = (
    "Monto autorizado\n$ 100.000\n"
    "Saldo disponible\n$ 80.000\n"
    "Monto utilizado\n$ 20.000\n"
)


def _fake_portal_page():
    """Stateful portal stand-in whose visible text follows the SPA route.

    The dashboard is the initial (and post-recovery) text, the card shortcut
    click opens the card detail, and a hash assignment opens the routed page —
    so `_read_all_surfaces` can be driven attempt by attempt with no browser.
    """
    page = MagicMock()
    page.url = (
        "https://portalpersonas.bancochile.cl/mibancochile-web/front/persona/"
        "index.html#/home"
    )
    state = {"text": REAL_DASHBOARD}

    def evaluate(js, *args):
        if args:
            state["text"] = LINEA_DETAIL if "linea" in args[0] else INVERSION_RESUMEN
            return None
        return state["text"]

    def click():
        state["text"] = CARD_DETAIL

    def goto(url, **kwargs):
        state["text"] = REAL_DASHBOARD

    page.evaluate.side_effect = evaluate
    page.locator.return_value.first.click.side_effect = click
    page.goto.side_effect = goto
    return page


class TestSurfaceRetries:
    def test_all_surfaces_parse_without_retries(self):
        """Every surface renders on attempt 1: no pauses, no recoveries."""
        page = _fake_portal_page()

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ()
        assert [(b.kind, b.currency) for b in result.products] == [
            ("checking", "CLP"),
            ("checking", "USD"),
            ("credit_card", "CLP"),
            ("credit_card", "USD"),
            ("line_of_credit", "CLP"),
            ("term_deposit", "CLP"),
            ("investment", "CLP"),
        ]
        page.goto.assert_not_called()
        page.wait_for_timeout.assert_not_called()

    def test_card_link_miss_recovers_on_second_attempt(self):
        """A card shortcut that misses once parses fine on the retry."""
        page = _fake_portal_page()
        link = page.locator.return_value.first
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
        link = page.locator.return_value.first
        link.wait_for.side_effect = RuntimeError("selector timeout")

        result = _read_all_surfaces(page)

        assert result.failed_surfaces == ("card",)
        assert [(b.kind, b.currency) for b in result.products] == [
            ("checking", "CLP"),
            ("checking", "USD"),
            ("line_of_credit", "CLP"),
            ("term_deposit", "CLP"),
            ("investment", "CLP"),
        ]
        assert link.wait_for.call_count == 9
        assert page.goto.call_count == 2
        assert [c.args[0] for c in page.wait_for_timeout.call_args_list] == [2000, 4000]
