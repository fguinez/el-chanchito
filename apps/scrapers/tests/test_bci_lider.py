"""Tests for the BCI Lider scraper: parsing, conversion, and one-login caching.

All fixtures are synthetic (canonical scheme: card last4 1234, obviously-fake
CLP/USD amounts); no real portal capture appears here.
"""

import asyncio
import os
import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("LIDER_BCI_RUT", "00000000-0")
os.environ.setdefault("LIDER_BCI_PASSWORD", "test")

import main as main_mod
from db.writer import TransactionWriteResult, VanishedProductsResult
from scrapers.backends import bci_lider_web as web
from scrapers.backends.bci_lider_billing import LiderStatement
from scrapers.backends.bci_lider_web import BciLiderCardResult
from scrapers.base import ProductRef, ScrapedExchangeRate, ScrapedProduct, SignInRequired
from scrapers.institutions import bci_lider as bci_lider_mod
from scrapers.institutions.bci_lider import BciLiderScraper

# Synthetic "Saldos de tu tarjeta" page text (Autorizado/Utilizado/Disponible).
SALDOS_TEXT = """Lider Bci Tradicional
Tarjeta N° XXXX XXXX XXXX 1234
Saldos de tu tarjeta
Autorizado Utilizado Disponible
Nacional $2.500.000 $1.000.000 $1.500.000
Internacional US$1.234,56 US$234,56 US$1.000,00
* Valor del dólar hoy 17 julio 2026: $956,8575"""

# Synthetic "Movimientos" table region (Fecha / Descripción / Cuotas / Monto).
MOVEMENTS_TEXT = """Detalle de tus movimientos
Fecha Tienda / Descripción Cuotas Monto
15/07/2026 SYNTHETIC STORE ONE $35.000
14/07/2026 SYNTHETIC STORE TWO 03/12 $10.000
09/07/2026 SYNTHETIC ABONO PAGO -$5.000
08/07/2026 PAGO $-999.999
Mostrando Página 12"""

# The same page in the "Último periodo facturado" view (synthetic): an older
# charge, plus STORE ONE again with the city suffix billing adds.
BILLED_VIEW_TEXT = """Detalle de tus movimientos
Fecha Tienda / Descripción Cuotas Monto
15/07/2026 SYNTHETIC STORE ONE,SANTIAGO $35.000
25/06/2026 SYNTHETIC STORE THREE $999.999
Mostrando Página 1"""

# A second "Por facturar" pager page (synthetic).
PAGE_TWO_TEXT = """Detalle de tus movimientos
Fecha Tienda / Descripción Cuotas Monto
05/07/2026 SYNTHETIC STORE FOUR $999.999
Mostrando Página 2"""


class _ViewLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    @property
    def first(self):
        return self

    def _pager_page(self):
        """The 0-based pager page this text's button opens, if it is showing."""
        if not self.text.isdigit() or self.page.view != "unbilled":
            return None
        index = int(self.text) - 1
        return index if index < len(self.page.unbilled_pages) else None

    def wait_for(self, **kwargs):
        if self._pager_page() is None:
            raise TimeoutError("no such pager button")

    def click(self, timeout=None):
        if self._pager_page() is not None:
            self.page.page_index = self._pager_page()
            return
        if self.text != web._BILLED_VIEW:
            raise TimeoutError("not clickable")
        if self.page.switches:
            self.page.view = "billed"

    def evaluate(self, script, timeout=None):
        assert script == web._VIEW_RADIO_CHECKED_JS
        assert timeout, "every view check must be bounded"
        if self.page.radios is None:
            return None  # the text is not inside a radio label
        view = {web._UNBILLED_VIEW: "unbilled", web._BILLED_VIEW: "billed"}[self.text]
        return self.page.view == view


class _MovementsPage:
    """The Movimientos page: two period views behind their radios (synthetic)."""

    def __init__(
        self,
        switches=True,
        radios=True,
        billed_text=BILLED_VIEW_TEXT,
        stale_reads=0,
        unbilled_pages=(MOVEMENTS_TEXT,),
    ):
        self.view = "unbilled"
        self.switches = switches
        self.radios = True if radios else None
        self.billed_text = billed_text
        # Reads after the switch that still show "Por facturar" page 1.
        self.stale_reads = stale_reads
        self.unbilled_pages = list(unbilled_pages)
        self.page_index = 0

    def goto(self, url, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def get_by_text(self, text, exact=False):
        return _ViewLocator(self, text)

    def text(self):
        if self.view == "billed" and self.stale_reads:
            self.stale_reads -= 1
            return self.unbilled_pages[0]
        if self.view == "billed":
            return self.billed_text
        return self.unbilled_pages[self.page_index]


class TestAmountParsing:
    def test_clp(self):
        assert web.parse_clp("$1.000.000") == 1000000

    def test_clp_rounds_decimals(self):
        assert web.parse_clp("$956,8575") == 957

    def test_usd_keeps_decimals(self):
        assert web.parse_amount("US$1.234,56") == 1234.56

    def test_usd_zero(self):
        assert web.parse_amount("US$0,00") == 0.0

    def test_negative(self):
        assert web.parse_clp("-$5.000") == -5000

    def test_none_and_empty(self):
        assert web.parse_amount(None) is None
        assert web.parse_amount("sin dato") is None


class TestSaldosParsing:
    def test_clp_and_usd(self):
        saldos = web.card_saldos_from_text(SALDOS_TEXT)
        assert saldos["CLP"] == {"available": 1500000.0, "limit": 2500000.0, "owed": 1000000.0}
        assert saldos["USD"] == {"available": 1000.0, "limit": 1234.56, "owed": 234.56}

    def test_last4_and_name(self):
        assert web.card_last4_from_text(SALDOS_TEXT) == "1234"
        assert web.card_name_from_text(SALDOS_TEXT) == "Lider Bci Tradicional"

    def test_empty_text(self):
        assert web.card_saldos_from_text("") == {}
        assert web.card_last4_from_text("") is None

    def test_clp_not_read_as_usd(self):
        """A bare '$' row must not be picked up by the USD (US$) pattern."""
        saldos = web.card_saldos_from_text("Nacional $2.500.000 $1.000.000 $1.500.000")
        assert "CLP" in saldos and "USD" not in saldos


class TestCardProducts:
    def test_emits_clp_and_usd(self):
        products = web.card_products_from_text(SALDOS_TEXT)
        assert [p.currency for p in products] == ["CLP", "USD"]
        clp = products[0]
        assert clp.kind == "credit_card"
        assert clp.metrics.available == 1500000.0
        assert clp.metrics.limit == 2500000.0
        assert clp.metrics.owed == 1000000.0

    def test_last4_and_brand_attributes(self):
        clp = web.card_products_from_text(SALDOS_TEXT)[0]
        assert clp.attributes.last4 == "1234"
        assert clp.attributes.brand == "Lider Bci Tradicional"

    def test_no_balances_returns_empty(self):
        assert web.card_products_from_text("nothing here") == []


class TestMovementsParsing:
    def test_charge_is_negative(self):
        movements = web.movements_from_text(MOVEMENTS_TEXT)
        first = movements[0]
        assert first["date"] == date(2026, 7, 15)
        assert first["description"] == "SYNTHETIC STORE ONE"
        assert first["amount"] == -35000

    def test_installment_cuotas(self):
        second = web.movements_from_text(MOVEMENTS_TEXT)[1]
        assert second["cuotas"] == "03/12"
        assert second["amount"] == -10000

    def test_abono_is_positive(self):
        abono = web.movements_from_text(MOVEMENTS_TEXT)[2]
        assert abono["amount"] == 5000

    def test_abono_minus_after_peso_sign(self):
        """The portal prints a card payment as "$-999.999", not "-$999.999"."""
        payment = web.movements_from_text(MOVEMENTS_TEXT)[3]

        assert payment["description"] == "PAGO"
        assert payment["amount"] == 999999

    def test_region_excludes_pager_and_header(self):
        """Only real rows parse: the header and 'Mostrando Página' are excluded."""
        assert len(web.movements_from_text(MOVEMENTS_TEXT)) == 4

    def test_empty(self):
        assert web.movements_from_text("") == []
        assert web.movements_from_text("no movements here") == []


class TestPeriodViews:
    """V044: the period view a charge was read in is its billing status."""

    @pytest.fixture(autouse=True)
    def _page_text(self, monkeypatch):
        monkeypatch.setattr(web, "_inner_text", lambda page: page.text())
        monkeypatch.setattr(web, "_dismiss_popup", lambda page: None)

    def _billed_by_description(self, page):
        return {m["description"]: m["billed"] for m in web._read_movements(page)}

    def test_each_view_tags_its_rows(self):
        tags = self._billed_by_description(_MovementsPage())

        assert tags["SYNTHETIC STORE TWO"] is False
        assert tags["SYNTHETIC STORE THREE"] is True
        # Rewritten on billing, so the backend keeps both; the scraper folds them.
        assert tags["SYNTHETIC STORE ONE"] is False
        assert tags["SYNTHETIC STORE ONE,SANTIAGO"] is True

    def test_a_row_seen_again_in_the_billed_view_is_billed(self):
        """Same date, description and amount in both views: kept once, billed."""
        billed_text = BILLED_VIEW_TEXT.replace("STORE ONE,SANTIAGO", "STORE ONE")

        movements = web._read_movements(_MovementsPage(billed_text=billed_text))

        ones = [m["billed"] for m in movements if m["description"] == "SYNTHETIC STORE ONE"]
        assert ones == [True]

    def test_a_view_that_did_not_switch_tags_nothing_billed(self):
        """The click left "Por facturar" showing: its rows stay unbilled."""
        page = _MovementsPage(switches=False)

        movements = web._read_movements(page)

        assert {m["billed"] for m in movements} == {False}
        assert len(movements) == 4

    def test_a_radio_switched_over_a_stale_table_tags_nothing_billed(self):
        """The radio reads checked but the "Por facturar" rows never left."""
        movements = web._read_movements(_MovementsPage(stale_reads=10_000))

        assert {m["billed"] for m in movements} == {False}
        assert len(movements) == 4

    def test_a_table_that_re_renders_late_is_still_billed(self):
        """A few stale reads after the switch, then the billed rows show."""
        tags = self._billed_by_description(_MovementsPage(stale_reads=3))

        assert tags["SYNTHETIC STORE TWO"] is False
        assert tags["SYNTHETIC STORE THREE"] is True

    def test_a_pager_reset_to_page_one_is_not_the_billed_view(self):
        """The click briefly shows "Por facturar" page 1 again, which differs from
        page 2 but holds only rows already read: no sign the view switched."""
        page = _MovementsPage(stale_reads=3, unbilled_pages=(MOVEMENTS_TEXT, PAGE_TWO_TEXT))

        tags = self._billed_by_description(page)

        assert tags["SYNTHETIC STORE TWO"] is False
        assert tags["SYNTHETIC STORE FOUR"] is False
        assert tags["SYNTHETIC STORE THREE"] is True

    def test_unconfirmed_radios_tag_nothing(self):
        """When the views' radios can't be read, no row gets a status."""
        tags = self._billed_by_description(_MovementsPage(radios=False))

        assert set(tags.values()) == {None}
        assert "SYNTHETIC STORE THREE" in tags


class TestMovementConversion:
    def setup_method(self):
        self.scraper = BciLiderScraper()

    def _mov(self, description="SYNTHETIC STORE", amount=-35000, day=15):
        return {
            "date": date(2026, 7, day),
            "description": description,
            "cuotas": None,
            "amount": amount,
        }

    def test_basic_conversion(self):
        txn = self.scraper._movement_to_transaction(self._mov())
        assert txn.institution == "bci_lider"
        assert txn.product_kind == "credit_card"
        assert txn.amount == -35000
        assert txn.transaction_date == date(2026, 7, 15)

    def test_external_id_stable(self):
        a = self.scraper._movement_to_transaction(self._mov())
        b = self.scraper._movement_to_transaction(self._mov())
        assert a.external_id == b.external_id
        assert a.external_id.startswith("bcl_")

    def test_different_movements_different_ids(self):
        a = self.scraper._movement_to_transaction(self._mov(amount=-35000))
        b = self.scraper._movement_to_transaction(self._mov(amount=-10000))
        assert a.external_id != b.external_id

    def test_id_survives_a_description_rewrite(self):
        """Billing rewrites the store name, so the id must not depend on it."""
        por_facturar = self.scraper._movement_to_transaction(self._mov(description="STORE A"))
        facturado = self.scraper._movement_to_transaction(self._mov(description="STORE A,SANTIAGO"))

        assert por_facturar.external_id == facturado.external_id

    def test_the_view_is_the_billing_status(self):
        for billed, status in ((True, "billed"), (False, "unbilled"), (None, None)):
            txn = self.scraper._movement_to_transaction(dict(self._mov(), billed=billed))
            assert (txn.billing_status, txn.statement_date) == (status, None)

    def test_a_movement_without_a_view_reports_no_status(self):
        assert self.scraper._movement_to_transaction(self._mov()).billing_status is None


class TestOneLoginCaching:
    """The Turnstile-gated login must run once per cycle, feeding both legs."""

    def setup_method(self):
        self.scraper = BciLiderScraper()
        self.products = [
            ScrapedProduct(
                institution="bci_lider",
                kind="credit_card",
                currency="CLP",
                metrics=web.CreditCardMetrics(available=1500000.0, limit=2500000.0, owed=1000000.0),
            )
        ]

    def _install(self, monkeypatch, result=None, exc=None):
        calls = {"n": 0}

        async def fake_scrape_card(rut=None, password=None, cdp_url=None):
            calls["n"] += 1
            if exc is not None:
                raise exc
            return result

        monkeypatch.setattr(bci_lider_mod, "scrape_card", fake_scrape_card)
        return calls

    def test_transactions_then_products_logs_in_once(self, monkeypatch):
        result = BciLiderCardResult(
            products=self.products,
            movements=[
                {"date": date(2026, 7, 15), "description": "S", "cuotas": None, "amount": -1000}
            ],
        )
        calls = self._install(monkeypatch, result=result)

        txns = asyncio.run(self.scraper.scrape_transactions())
        prods = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert len(txns) == 1
        assert prods.products == self.products

    def test_products_alone_logs_in(self, monkeypatch):
        result = BciLiderCardResult(products=self.products, warnings=["w"])
        calls = self._install(monkeypatch, result=result)

        prods = asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1
        assert prods.products == self.products
        assert prods.warnings == ["w"]

    def test_failed_login_not_retried_by_products(self, monkeypatch):
        calls = self._install(monkeypatch, exc=RuntimeError("login failed"))

        with pytest.raises(RuntimeError):
            asyncio.run(self.scraper.scrape_transactions())
        with pytest.raises(RuntimeError):
            asyncio.run(self.scraper.scrape_products())

        assert calls["n"] == 1

    @pytest.mark.parametrize("billed_first", [True, False], ids=["billed-first", "unbilled-first"])
    def test_a_charge_in_both_views_is_written_once_billed(self, monkeypatch, billed_first):
        """Its rewritten description kept both copies in the backend; one key wins."""
        unbilled = {
            "date": date(2026, 7, 15),
            "description": "STORE A",
            "cuotas": None,
            "amount": -35000,
            "billed": False,
        }
        billed = dict(unbilled, description="STORE A,SANTIAGO", billed=True)
        other = dict(unbilled, date=date(2026, 7, 14), billed=False)
        movements = [billed, unbilled, other] if billed_first else [unbilled, billed, other]
        self._install(monkeypatch, result=BciLiderCardResult(movements=movements))

        first, second = asyncio.run(self.scraper.scrape_transactions())

        assert (first.billing_status, first.description) == ("billed", "STORE A,SANTIAGO")
        assert second.billing_status == "unbilled"

    def test_warnings_propagate(self, monkeypatch):
        result = BciLiderCardResult(products=[], warnings=["BciLider: no card balances parsed"])
        self._install(monkeypatch, result=result)

        asyncio.run(self.scraper.scrape_transactions())
        prods = asyncio.run(self.scraper.scrape_products())

        assert prods.warnings == ["BciLider: no card balances parsed"]


def _lider_statement(issued_on, period_end, movements):
    return LiderStatement(
        issued_on=issued_on,
        period_start=None,
        period_end=period_end,
        payment_due_date=None,
        billed_amount=None,
        minimum_payment=None,
        next_period_end=None,
        movements=movements,
    )


class TestStatements:
    """Past statements date the billing of the charges they list."""

    # Synthetic: an installment charge on both statements, a newer charge and
    # a payment on the newer one only.
    OLDER = _lider_statement(date(2026, 2, 11), date(2026, 2, 10), [(date(2026, 2, 3), -999999)])
    NEWER = _lider_statement(
        date(2026, 3, 11),
        date(2026, 3, 10),
        [(date(2026, 2, 3), -999999), (date(2026, 3, 1), -35000), (date(2026, 3, 5), 999999)],
    )

    def setup_method(self):
        self.scraper = BciLiderScraper()

    def _scrape(self, monkeypatch, movements, statements):
        async def fake_scrape_card(rut=None, password=None, cdp_url=None):
            return BciLiderCardResult(movements=movements, statements=statements)

        monkeypatch.setattr(bci_lider_mod, "scrape_card", fake_scrape_card)
        return asyncio.run(self.scraper.scrape_transactions())

    def test_a_listed_charge_is_billed_on_its_oldest_statement(self, monkeypatch):
        """Even one read in "Por facturar"; an unlisted one keeps its view."""
        # Arrange
        movements = [
            {
                "date": date(2026, 2, 3),
                "description": "CUOTAS",
                "cuotas": "02/03",
                "amount": -999999,
                "billed": False,
            },
            {
                "date": date(2026, 3, 1),
                "description": "STORE",
                "cuotas": None,
                "amount": -35000,
                "billed": None,
            },
            {
                "date": date(2026, 3, 12),
                "description": "NEW",
                "cuotas": None,
                "amount": -35000,
                "billed": False,
            },
        ]

        # Act
        installment, charge, new = self._scrape(monkeypatch, movements, [self.NEWER, self.OLDER])

        # Assert
        assert (installment.billing_status, installment.statement_date) == (
            "billed",
            date(2026, 2, 10),
        )
        assert (charge.billing_status, charge.statement_date) == ("billed", date(2026, 3, 10))
        assert (new.billing_status, new.statement_date) == ("unbilled", None)

    def test_billed_statements_carry_every_listed_key(self, monkeypatch):
        """Stored rows of older cycles are marked through them."""
        # Arrange
        self._scrape(monkeypatch, [], [self.OLDER, self.NEWER])

        # Act
        statements = self.scraper.billed_statements()

        # Assert
        by_date = {s.statement_date: s for s in statements}
        assert set(by_date) == {date(2026, 2, 10), date(2026, 3, 10)}
        newer = by_date[date(2026, 3, 10)]
        assert (newer.institution, newer.product_kind, newer.currency) == (
            "bci_lider",
            "credit_card",
            "CLP",
        )
        assert newer.external_ids == {
            bci_lider_mod.movement_external_id(day, amount) for day, amount in self.NEWER.movements
        }

    def test_the_key_matches_the_movement_key(self):
        txn = self.scraper._movement_to_transaction(
            {"date": date(2026, 3, 1), "description": "S", "cuotas": None, "amount": -35000}
        )

        assert txn.external_id == bci_lider_mod.movement_external_id(date(2026, 3, 1), -35000)

    def test_no_statements_without_a_scrape(self):
        assert self.scraper.billed_statements() == []

    def test_a_failed_scrape_clears_the_statements(self, monkeypatch):
        # Arrange
        self._scrape(monkeypatch, [], [self.NEWER])

        async def failing(rut=None, password=None, cdp_url=None):
            raise RuntimeError("login failed")

        monkeypatch.setattr(bci_lider_mod, "scrape_card", failing)

        # Act
        with pytest.raises(RuntimeError):
            asyncio.run(self.scraper.scrape_transactions())

        # Assert
        assert self.scraper.billed_statements() == []

    def test_the_products_leg_carries_the_rate(self, monkeypatch):
        # Arrange
        rate = ScrapedExchangeRate(
            institution="bci_lider",
            base_currency="USD",
            rate="1000.0000",
            product=ProductRef(kind="credit_card", currency="USD"),
            source="Valor del dólar de la tarjeta",
        )

        async def fake_scrape_card(rut=None, password=None, cdp_url=None):
            return BciLiderCardResult(exchange_rates=[rate])

        monkeypatch.setattr(bci_lider_mod, "scrape_card", fake_scrape_card)

        # Act
        asyncio.run(self.scraper.scrape_transactions())
        prods = asyncio.run(self.scraper.scrape_products())

        # Assert
        assert prods.exchange_rates == [rate]


class TestRealChromeWiring:
    """Lider's thin layer over `real_chrome` (whose own behaviour has its own tests)."""

    @pytest.mark.parametrize("offscreen", [True, False])
    def test_launch_passes_lider_settings_to_the_shared_helpers(self, monkeypatch, offscreen):
        """The override env, the dedicated profile, the port, the URL and offscreen go through."""
        found, launched = [], []
        monkeypatch.setattr(
            web.real_chrome, "find_chrome", lambda env: found.append(env) or "/synthetic/chrome"
        )
        monkeypatch.setattr(
            web.real_chrome,
            "launch_chrome",
            lambda exe, **kwargs: launched.append((exe, kwargs)) or "proc",
        )

        proc = web._launch_real_chrome(9222, "https://example.invalid/", offscreen=offscreen)

        assert proc == "proc"
        assert found == ["LIDER_BCI_CHROME_PATH"]
        assert launched == [
            (
                "/synthetic/chrome",
                {
                    "port": 9222,
                    "profile_dir": web._CHROME_PROFILE,
                    "url": "https://example.invalid/",
                    "offscreen": offscreen,
                },
            )
        ]

    def test_missing_chrome_is_a_lider_error_naming_the_override(self, monkeypatch):
        """No Chrome anywhere surfaces as BciLiderWebError pointing at LIDER_BCI_CHROME_PATH."""
        monkeypatch.delenv("LIDER_BCI_CHROME_PATH", raising=False)
        monkeypatch.setattr(web.real_chrome, "chrome_candidates", list)

        with pytest.raises(web.BciLiderWebError) as info:
            web._launch_real_chrome(9222, "https://example.invalid/")

        assert "LIDER_BCI_CHROME_PATH" in str(info.value)

    def test_unstartable_chrome_is_a_lider_error_without_the_path(self, monkeypatch):
        """A bad override names the setting, never the path it holds."""
        path = "/synthetic/missing/chrome"
        monkeypatch.setenv("LIDER_BCI_CHROME_PATH", path)

        def no_binary(args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", path)

        monkeypatch.setattr(web.real_chrome.subprocess, "Popen", no_binary)

        with pytest.raises(web.BciLiderWebError) as info:
            web._launch_real_chrome(9222, "https://example.invalid/")

        assert "LIDER_BCI_CHROME_PATH" in str(info.value)
        assert path not in str(info.value)
        assert info.value.__cause__ is None

    def test_unreachable_debug_port_is_a_lider_session_error(self, monkeypatch):
        """A connect that never succeeds keeps Lider's error type, URL and hint."""
        deadlines = []

        def unreachable(playwright, cdp_url, *, deadline, process=None):
            deadlines.append(deadline)
            raise web.real_chrome.ChromeUnreachableError("synthetic")

        monkeypatch.setattr(web.real_chrome, "connect_over_cdp", unreachable)

        with pytest.raises(web.BciLiderSessionError) as info:
            web._connect_cdp(object(), "http://localhost:9222", deadline=123.0)

        assert deadlines == [123.0]
        assert "http://localhost:9222" in str(info.value)
        assert "make bci-lider-login" in str(info.value)
        assert not isinstance(info.value, SignInRequired)

    def test_exited_chrome_is_a_lider_session_error_with_the_profile_hint(self, monkeypatch):
        """A launched Chrome that exits surfaces at once with the profile hint."""
        passed = []

        def exited(playwright, cdp_url, *, deadline, process=None):
            passed.append(process)
            raise web.real_chrome.ChromeExitedError("synthetic exit.")

        monkeypatch.setattr(web.real_chrome, "connect_over_cdp", exited)

        with pytest.raises(web.BciLiderSessionError) as info:
            web._connect_cdp(object(), "http://localhost:9222", deadline=123.0, process="proc")

        assert passed == ["proc"]
        assert "Lider profile" in str(info.value)

    def test_sign_in_chrome_that_exits_is_a_lider_web_error_with_the_profile_hint(
        self,
        monkeypatch,
    ):
        """The interactive sign-in watches the Chrome it launched and names the profile."""
        import contextlib

        import playwright.sync_api

        passed = []

        def exited(playwright, cdp_url, *, deadline, process=None):
            passed.append(process)
            raise web.real_chrome.ChromeExitedError("synthetic exit.")

        monkeypatch.setattr(web, "_launch_real_chrome", lambda port, url: "proc")
        monkeypatch.setattr(web.real_chrome, "connect_over_cdp", exited)
        monkeypatch.setattr(
            playwright.sync_api, "sync_playwright", lambda: contextlib.nullcontext(object())
        )

        with pytest.raises(web.BciLiderWebError) as info:
            web.save_login_session(timeout_s=60)

        assert passed == ["proc"]
        assert "Lider profile" in str(info.value)

    def test_managed_attempt_passes_its_chrome_to_the_scrape(self, monkeypatch):
        """The managed run hands the Chrome it launched to the CDP attach."""
        seen = []
        monkeypatch.setattr(web, "_launch_real_chrome", lambda port, url, offscreen: "proc")
        monkeypatch.setattr(web.real_chrome, "terminate_chrome", lambda proc, cdp_url=None: None)
        monkeypatch.setattr(
            web,
            "_scrape_over_cdp",
            lambda cdp_url, rut, password, process=None, profile=None: (
                seen.append((process, profile)) or "result"
            ),
        )

        result = web._scrape_managed_once(9222, "http://localhost:9222", None, None, offscreen=True)

        assert result == "result"
        assert seen == [("proc", web._CHROME_PROFILE)]

    def test_managed_attempt_terminates_its_chrome_even_on_failure(self, monkeypatch):
        """The Chrome a managed run launched is torn down by the shared helper, via CDP first."""
        terminated = []
        monkeypatch.setattr(web, "_launch_real_chrome", lambda port, url, offscreen: "proc")
        monkeypatch.setattr(
            web.real_chrome,
            "terminate_chrome",
            lambda proc, cdp_url=None: terminated.append((proc, cdp_url)),
        )

        def broken_scrape(cdp_url, rut, password, process=None, profile=None):
            raise web.BciLiderSessionError("synthetic")

        monkeypatch.setattr(web, "_scrape_over_cdp", broken_scrape)

        with pytest.raises(web.BciLiderSessionError):
            web._scrape_managed_once(9222, "http://localhost:9222", None, None, offscreen=True)

        assert terminated == [("proc", "http://localhost:9222")]


class _Locator:
    def __init__(self, visible):
        self.first = self
        self._visible = visible

    def is_visible(self):
        return self._visible


class _SignedOutPage:
    """A tab the portal keeps on /login: every navigation is a no-op."""

    url = "https://example.invalid/login"

    def __init__(self, login_form=True):
        self._login_form = login_form

    def goto(self, url, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        return _Locator(self._login_form)


class TestSignInRequired:
    """A portal that stays signed out needs a human; an unreachable Chrome does not."""

    def test_an_autofill_that_never_lands_needs_a_sign_in(self, monkeypatch):
        """The login timeout names the login command and is a SignInRequired."""
        monkeypatch.setattr(web, "_autofill_and_submit", lambda *args: None)

        with pytest.raises(web.BciLiderSignInRequired) as info:
            web._login_via_autofill(_SignedOutPage(), None, None, time.monotonic() - 1)

        assert isinstance(info.value, SignInRequired)
        assert "make bci-lider-login" in str(info.value)[:120]
        assert "LIDER_BCI_CDP_URL" in str(info.value)

    @pytest.mark.parametrize(
        ("submitted", "hint"),
        [(True, "clave may be wrong"), (False, "Cloudflare")],
        ids=["rejected", "never-submitted"],
    )
    def test_the_error_says_whether_the_clave_was_sent(self, monkeypatch, submitted, hint):
        """A submitted login that never lands blames the clave; an unsent one, Cloudflare."""
        monkeypatch.setattr(web, "_autofill_and_submit", lambda *args: submitted)

        with pytest.raises(web.BciLiderSignInRequired) as info:
            web._login_via_autofill(_SignedOutPage(), None, None, time.monotonic() - 1)

        assert info.value.submitted is submitted
        assert hint in str(info.value)
        assert "make bci-lider-login" in str(info.value)[:120]

    @pytest.mark.parametrize("clicked", [True, False], ids=["clicked", "click-failed"])
    def test_submitted_means_ingresar_was_clicked(self, monkeypatch, clicked):
        """The autofill only reports a submit when the Ingresar click went through."""
        monkeypatch.setattr(web, "_submit_enabled", lambda page: True)
        monkeypatch.setattr(web, "_click_first", lambda *args, **kwargs: clicked)
        page = SimpleNamespace(evaluate=lambda js: True, wait_for_timeout=lambda ms: None)

        submitted = web._autofill_and_submit(page, None, None, time.monotonic() + 60)

        assert submitted is clicked

    def test_a_rejected_clave_without_its_form_still_blames_the_clave(self, monkeypatch):
        """Once the clave went out, a page hiding the form is still a rejection."""
        monkeypatch.setattr(web, "_autofill_and_submit", lambda *args: True)

        with pytest.raises(web.BciLiderSignInRequired) as info:
            web._login_via_autofill(
                _SignedOutPage(login_form=False), None, None, time.monotonic() - 1
            )

        assert info.value.submitted is True

    def test_a_login_page_without_its_form_is_an_error(self, monkeypatch):
        """A maintenance or error page at /login is not a sign-in a human can fix."""
        monkeypatch.setattr(web, "_autofill_and_submit", lambda *args: None)

        with pytest.raises(web.BciLiderSessionError) as info:
            web._login_via_autofill(
                _SignedOutPage(login_form=False), None, None, time.monotonic() - 1
            )

        assert not isinstance(info.value, SignInRequired)

    def test_a_bounce_to_login_while_reading_needs_a_sign_in(self, monkeypatch):
        """It stays a BciLiderSessionError, so the mid-read retry still catches it."""
        monkeypatch.setattr(web, "_dismiss_popup", lambda page: None)

        with pytest.raises(web.BciLiderSignInRequired) as info:
            web._read_balances(_SignedOutPage())

        assert isinstance(info.value, web.BciLiderSessionError)

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            pytest.param(
                web.BciLiderSignInRequired("Could not sign in. Run `make bci-lider-login`."),
                "needs_sign_in",
                id="signed-out",
            ),
            pytest.param(
                web.BciLiderSessionError("Could not reach the Chrome debug port."),
                "error",
                id="chrome-unreachable",
            ),
        ],
    )
    def test_the_run_status_follows_the_failure(self, monkeypatch, exc, status):
        """Both legs share the one failure; only a sign-in one ends `needs_sign_in`."""
        calls = []

        async def failing(rut=None, password=None, cdp_url=None):
            calls.append(1)
            raise exc

        monkeypatch.setattr(bci_lider_mod, "scrape_card", failing)
        finish = MagicMock()
        monkeypatch.setattr(main_mod, "start_scraper_run", MagicMock(return_value="run-1"))
        monkeypatch.setattr(
            main_mod, "write_transactions", MagicMock(return_value=TransactionWriteResult())
        )
        monkeypatch.setattr(main_mod, "upsert_product", MagicMock())
        monkeypatch.setattr(
            main_mod, "retire_vanished_products", MagicMock(return_value=VanishedProductsResult())
        )
        monkeypatch.setattr(main_mod, "finish_scraper_run", finish)

        asyncio.run(main_mod.run_scraper(BciLiderScraper()))

        (_, recorded), _ = finish.call_args
        assert recorded == status
        assert calls == [1]


class TestManagedFallback:
    """The visible-window retry never resends a rejected clave nor hides a sign-in."""

    @pytest.fixture
    def attempts(self, monkeypatch):
        monkeypatch.setattr(web.real_chrome, "port_in_use", lambda port: False)
        calls = []
        outcomes = []

        def fake_once(port, cdp_url, rut, password, offscreen):
            calls.append(offscreen)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(web, "_scrape_managed_once", fake_once)
        return calls, outcomes

    def test_a_rejected_clave_is_not_retried(self, attempts):
        """A submitted sign-in the portal rejected propagates without a second attempt."""
        calls, outcomes = attempts
        rejected = web.BciLiderSignInRequired("synthetic", submitted=True)
        outcomes.append(rejected)

        with pytest.raises(web.BciLiderSignInRequired) as info:
            web._scrape_managed_sync(None, None)

        assert info.value is rejected
        assert calls == [True]

    def test_an_unsent_sign_in_retries_visibly(self, attempts):
        """A sign-in Cloudflare never let through is retried in a visible window."""
        calls, outcomes = attempts
        outcomes.extend([web.BciLiderSignInRequired("synthetic"), "result"])

        result = web._scrape_managed_sync(None, None)

        assert result == "result"
        assert calls == [True, False]

    def test_a_retry_failing_otherwise_keeps_the_sign_in_error(self, attempts):
        """A visible retry that dies for another reason still reports the sign-in."""
        calls, outcomes = attempts
        unsent = web.BciLiderSignInRequired("synthetic")
        outcomes.extend([unsent, web.BciLiderSessionError("Chrome exited")])

        with pytest.raises(web.BciLiderSignInRequired) as info:
            web._scrape_managed_sync(None, None)

        assert info.value is unsent
        assert calls == [True, False]

    def test_a_plain_failure_falls_back_to_a_visible_window(self, attempts):
        """Any other off-screen failure is retried once, and the retry's result wins."""
        calls, outcomes = attempts
        outcomes.extend([web.BciLiderSessionError("synthetic"), "result"])

        result = web._scrape_managed_sync(None, None)

        assert result == "result"
        assert calls == [True, False]


class TestAccountBinding:
    """Nothing is read from a portal tab of another account than LIDER_BCI_RUT.
    Every RUT here is synthetic."""

    RUT = "11.111.111-1"
    OTHER = "22.222.222-2"

    @pytest.fixture(autouse=True)
    def profile(self, tmp_path, monkeypatch):
        path = tmp_path / ".bci_lider_chrome_profile"
        monkeypatch.setattr(web, "_CHROME_PROFILE", path)
        return path

    @staticmethod
    def _make_profile(path, rut=None):
        (path / "Default").mkdir(parents=True)
        (path / "Default" / "Cookies").write_text("synthetic")
        if rut is not None:
            web.session_account.write_account(path, rut)

    def test_the_check_runs_in_the_page_with_the_canonical_rut(self):
        seen = []

        class Page:
            def evaluate(self, js, arg):
                seen.append(arg)
                return "match"

        assert web._account_check(Page(), "11.111.111-k") == "match"
        assert seen == [{"full": "11111111K", "body": "11111111"}]

    @pytest.mark.parametrize("answer", [None, "yes", RuntimeError("closed")])
    def test_an_unreadable_verdict_is_unknown(self, answer):
        class Page:
            def evaluate(self, js, arg):
                if isinstance(answer, Exception):
                    raise answer
                return answer

        assert web._account_check(Page(), self.RUT) == "unknown"

    @pytest.mark.parametrize(
        ("verdict", "signed_in_now"),
        [("match", False), ("match", True), ("unknown", True)],
    )
    @pytest.mark.parametrize("managed", [True, False], ids=["managed", "reuse"])
    def test_an_own_or_confirmed_tab_is_read(self, monkeypatch, verdict, signed_in_now, managed):
        monkeypatch.setattr(web, "_account_check", lambda page, rut: verdict)
        web._verify_account(object(), self.RUT, signed_in_now=signed_in_now, managed=managed)

    @pytest.mark.parametrize(
        ("verdict", "signed_in_now"),
        [("mismatch", False), ("mismatch", True), ("unknown", False)],
    )
    def test_reuse_mode_refuses_and_asks_for_a_sign_out(self, monkeypatch, verdict, signed_in_now):
        monkeypatch.setattr(web, "_account_check", lambda page, rut: verdict)
        with pytest.raises(web.BciLiderSignInRequired) as info:
            web._verify_account(object(), self.RUT, signed_in_now=signed_in_now, managed=False)
        message = str(info.value)
        assert "Sign out" in message and "make bci-lider-login" in message
        assert "11" not in message

    @pytest.mark.parametrize(
        ("verdict", "signed_in_now"),
        [("mismatch", False), ("mismatch", True), ("unknown", False)],
    )
    def test_managed_mode_raises_a_mismatch_to_reset(self, monkeypatch, verdict, signed_in_now):
        monkeypatch.setattr(web, "_account_check", lambda page, rut: verdict)
        with pytest.raises(web.BciLiderAccountMismatch) as info:
            web._verify_account(object(), self.RUT, signed_in_now=signed_in_now, managed=True)
        assert not isinstance(info.value, SignInRequired)

    def test_without_a_rut_there_is_nothing_to_compare(self, monkeypatch):
        def never(page, rut):
            raise AssertionError("checked without a RUT")

        monkeypatch.setattr(web, "_account_check", never)
        web._verify_account(object(), None, signed_in_now=False, managed=False)

    def test_a_profile_of_this_account_is_kept(self, profile):
        self._make_profile(profile, "11111111-1")
        web._prepare_profile(self.RUT)  # another spelling of the same RUT
        assert (profile / "Default" / "Cookies").exists()

    @pytest.mark.parametrize("owner", [OTHER, None], ids=["other-account", "unmarked"])
    def test_a_profile_not_tied_to_this_account_is_deleted(self, profile, owner, caplog):
        self._make_profile(profile, owner)
        with caplog.at_level("INFO"):
            web._prepare_profile(self.RUT)
        assert not profile.exists()
        assert not web.session_account.marker_path(profile).exists()
        assert "11" not in caplog.text

    def test_no_rut_leaves_the_profile_alone(self, profile):
        self._make_profile(profile, self.OTHER)
        web._prepare_profile(None)
        assert profile.exists()

    def test_a_profile_that_cannot_be_deleted_stops_the_run(self, profile, monkeypatch):
        self._make_profile(profile, self.OTHER)
        monkeypatch.setattr(web.session_account, "discard", lambda *args, **kwargs: None)
        with pytest.raises(web.BciLiderSessionError, match="could not reset"):
            web._prepare_profile(self.RUT)

    def test_the_managed_run_resets_another_accounts_profile_before_launch(
        self, profile, monkeypatch
    ):
        self._make_profile(profile, self.OTHER)
        monkeypatch.setattr(web.real_chrome, "port_in_use", lambda port: False)
        existed = []

        def fake_once(port, cdp_url, rut, password, offscreen):
            existed.append(profile.exists())
            return "result"

        monkeypatch.setattr(web, "_scrape_managed_once", fake_once)
        assert web._scrape_managed_sync(self.RUT, "synthetic-clave") == "result"
        assert existed == [False]

    def test_a_mismatched_tab_resets_the_profile_and_signs_in_afresh(self, profile, monkeypatch):
        monkeypatch.setattr(web.real_chrome, "port_in_use", lambda port: False)
        self._make_profile(profile, self.RUT)
        calls = []

        def fake_once(port, cdp_url, rut, password, offscreen):
            calls.append(profile.exists())
            if len(calls) == 1:
                raise web.BciLiderAccountMismatch("synthetic")
            return "result"

        monkeypatch.setattr(web, "_scrape_managed_once", fake_once)
        assert web._scrape_managed_sync(self.RUT, "synthetic-clave") == "result"
        assert calls == [True, False]

    def _drive(self, monkeypatch, *, verdict, signed_in_now, profile):
        import contextlib

        import playwright.sync_api

        reads = []
        monkeypatch.setattr(
            playwright.sync_api, "sync_playwright", lambda: contextlib.nullcontext(object())
        )
        monkeypatch.setattr(web, "_connect_cdp", lambda *args, **kwargs: object())
        monkeypatch.setattr(web, "_find_page_matching", lambda browser, needle: "page")
        monkeypatch.setattr(web, "_ensure_logged_in", lambda *args: signed_in_now)
        monkeypatch.setattr(web, "_account_check", lambda page, rut: verdict)
        monkeypatch.setattr(web, "_read_card", lambda page, result, browser: reads.append(page))
        web._scrape_over_cdp("http://localhost:9222", self.RUT, None, profile=profile)
        return reads

    def test_a_slow_bounce_to_login_signs_in_instead_of_refusing(self, monkeypatch):
        """A tab with no account yet that lands on /login signs in with the RUT."""
        page = SimpleNamespace(url="https://example.invalid/private-home")
        page.wait_for_timeout = lambda ms: setattr(page, "url", "https://example.invalid/login")
        logins = []
        monkeypatch.setattr(web, "_account_check", lambda page, rut: "unknown")
        monkeypatch.setattr(web, "_login_via_autofill", lambda *args: logins.append(args[1]))

        web._confirm_account(
            page, self.RUT, None, time.monotonic() + 60, signed_in_now=False, managed=False
        )

        assert logins == [self.RUT]

    def test_a_tab_that_never_shows_its_account_is_refused(self, monkeypatch):
        monkeypatch.setattr(web, "_ACCOUNT_SETTLE_S", 0)
        monkeypatch.setattr(web, "_account_check", lambda page, rut: "unknown")
        page = SimpleNamespace(url="https://example.invalid/private-home")
        with pytest.raises(web.BciLiderSignInRequired):
            web._confirm_account(
                page, self.RUT, None, time.monotonic() + 60, signed_in_now=False, managed=False
            )

    def test_a_verified_managed_tab_ties_the_profile(self, monkeypatch, profile):
        profile.mkdir()
        reads = self._drive(monkeypatch, verdict="match", signed_in_now=False, profile=profile)
        assert reads == ["page"]
        assert web.session_account.belongs_to(profile, "11111111-1")

    def test_a_reuse_tab_never_writes_a_profile_marker(self, monkeypatch, profile):
        reads = self._drive(monkeypatch, verdict="match", signed_in_now=False, profile=None)
        assert reads == ["page"]
        assert not web.session_account.marker_path(profile).exists()

    @pytest.mark.parametrize("managed", [True, False], ids=["managed", "reuse"])
    def test_a_tab_of_another_account_is_never_read(self, monkeypatch, profile, managed):
        profile.mkdir()
        reads = []
        monkeypatch.setattr(web, "_read_card", lambda *args: reads.append(1))
        with pytest.raises(web.BciLiderSessionError):
            self._drive(
                monkeypatch,
                verdict="mismatch",
                signed_in_now=False,
                profile=profile if managed else None,
            )
        assert not web.session_account.marker_path(profile).exists()
