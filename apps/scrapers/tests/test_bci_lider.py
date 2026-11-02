"""Tests for the BCI Lider scraper: parsing, conversion, and one-login caching.

All fixtures are synthetic (canonical scheme: card last4 1234, obviously-fake
CLP/USD amounts); no real portal capture appears here.
"""

import asyncio
import os
import signal
import subprocess
from datetime import date

import pytest

os.environ.setdefault("LIDER_BCI_RUT", "00000000-0")
os.environ.setdefault("LIDER_BCI_PASSWORD", "test")

from scrapers.backends import bci_lider_web as web
from scrapers.backends.bci_lider_billing import LiderStatement
from scrapers.backends.bci_lider_web import BciLiderCardResult
from scrapers.base import ProductRef, ScrapedExchangeRate, ScrapedProduct
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
        saldos = web.card_saldos_from_text(
            "Nacional $2.500.000 $1.000.000 $1.500.000"
        )
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
        return {"date": date(2026, 7, day), "description": description, "cuotas": None, "amount": amount}

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
        facturado = self.scraper._movement_to_transaction(
            self._mov(description="STORE A,SANTIAGO")
        )

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
            movements=[{"date": date(2026, 7, 15), "description": "S", "cuotas": None, "amount": -1000}],
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
        unbilled = {"date": date(2026, 7, 15), "description": "STORE A", "cuotas": None,
                    "amount": -35000, "billed": False}
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
    OLDER = _lider_statement(
        date(2026, 2, 11), date(2026, 2, 10), [(date(2026, 2, 3), -999999)]
    )
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
            {"date": date(2026, 2, 3), "description": "CUOTAS", "cuotas": "02/03",
             "amount": -999999, "billed": False},
            {"date": date(2026, 3, 1), "description": "STORE", "cuotas": None,
             "amount": -35000, "billed": None},
            {"date": date(2026, 3, 12), "description": "NEW", "cuotas": None,
             "amount": -35000, "billed": False},
        ]

        # Act
        installment, charge, new = self._scrape(
            monkeypatch, movements, [self.NEWER, self.OLDER]
        )

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
            bci_lider_mod.movement_external_id(day, amount)
            for day, amount in self.NEWER.movements
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


class _FakeChrome:
    """A launched Chrome stand-in: records waits, exits when told to."""

    pid = 4242

    def __init__(self, exits_after: int = 1):
        self.waits = 0
        self.exits_after = exits_after
        self.signals = []

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits < self.exits_after:
            raise subprocess.TimeoutExpired("chrome", timeout)
        return 0

    def send_signal(self, sig):
        self.signals.append(sig)


class TestChromeDiscovery:
    def _isolate(self, monkeypatch, platform, windows=False, on_path=(), exists=()):
        monkeypatch.delenv("LIDER_BCI_CHROME_PATH", raising=False)
        monkeypatch.setattr(web.sys, "platform", platform)
        monkeypatch.setattr(web, "_is_windows", lambda: windows)
        monkeypatch.setattr(
            web.shutil, "which", lambda name: f"/usr/bin/{name}" if name in on_path else None
        )
        monkeypatch.setattr(web.os.path, "exists", lambda path: path in exists)

    def test_override_wins(self, monkeypatch):
        monkeypatch.setenv("LIDER_BCI_CHROME_PATH", "/opt/synthetic/chrome")

        assert web._chrome_executable() == "/opt/synthetic/chrome"

    @pytest.mark.parametrize(
        "name", ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]
    )
    def test_linux_names_on_path(self, monkeypatch, name):
        self._isolate(monkeypatch, "linux", on_path={name}, exists={f"/usr/bin/{name}"})

        assert web._chrome_executable() == f"/usr/bin/{name}"

    def test_macos_applications(self, monkeypatch):
        app = os.path.join("/Applications", "Google Chrome.app/Contents/MacOS/Google Chrome")
        self._isolate(monkeypatch, "darwin", exists={app})

        assert web._chrome_executable() == app

    @pytest.mark.parametrize(
        ("env_var", "suffix"),
        [
            ("PROGRAMFILES", ("Google", "Chrome", "Application", "chrome.exe")),
            ("PROGRAMFILES(X86)", ("Google", "Chrome", "Application", "chrome.exe")),
            ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
            ("LOCALAPPDATA", ("Chromium", "Application", "chrome.exe")),
        ],
    )
    def test_windows_install_dirs(self, monkeypatch, env_var, suffix):
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            monkeypatch.setenv(var, f"/synthetic/{var}")
        expected = os.path.join(f"/synthetic/{env_var}", *suffix)
        self._isolate(monkeypatch, "win32", windows=True, exists={expected})

        assert web._chrome_executable() == expected

    @pytest.mark.parametrize(
        ("platform", "windows", "hint"),
        [
            ("darwin", False, "/Applications"),
            ("win32", True, "%LocalAppData%"),
            ("linux", False, "apt install chromium"),
        ],
    )
    def test_not_found_names_the_override_and_an_os_hint(self, monkeypatch, platform, windows, hint):
        self._isolate(monkeypatch, platform, windows=windows)

        with pytest.raises(web.BciLiderWebError) as info:
            web._chrome_executable()

        assert "LIDER_BCI_CHROME_PATH" in str(info.value)
        assert hint in str(info.value)


class TestChromeLifecycle:
    def _capture_popen(self, monkeypatch):
        calls = []
        monkeypatch.setattr(web, "_chrome_executable", lambda: "/synthetic/chrome")
        monkeypatch.setattr(
            web.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or "proc"
        )
        return calls

    def test_posix_launch_starts_a_new_session(self, monkeypatch):
        monkeypatch.setattr(web, "_is_windows", lambda: False)
        calls = self._capture_popen(monkeypatch)

        assert web._launch_real_chrome(9222, "https://example.invalid/") == "proc"

        (args, kwargs), = calls
        assert args[0] == "/synthetic/chrome"
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs

    def test_windows_launch_uses_a_new_process_group(self, monkeypatch):
        monkeypatch.setattr(web, "_is_windows", lambda: True)
        calls = self._capture_popen(monkeypatch)

        web._launch_real_chrome(9222, "https://example.invalid/", offscreen=True)

        (args, kwargs), = calls
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        assert "start_new_session" not in kwargs
        assert "--window-position=-32000,-32000" in args

    @pytest.mark.skipif(os.name == "nt", reason="SIGKILL does not exist on Windows")
    def test_posix_teardown_signals_the_process_group(self, monkeypatch):
        monkeypatch.setattr(web, "_is_windows", lambda: False)
        sent = []
        monkeypatch.setattr(web.os, "getpgid", lambda pid: 777, raising=False)
        monkeypatch.setattr(web.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)
        proc = _FakeChrome(exits_after=2)

        web._terminate_chrome(proc)

        assert sent == [(777, signal.SIGTERM), (777, web.signal.SIGKILL)]
        assert proc.waits == 2

    def test_windows_teardown_never_touches_posix_only_calls(self, monkeypatch):
        """Simulates Windows: no killpg/getpgid/SIGKILL, so any use would raise."""
        monkeypatch.setattr(web, "_is_windows", lambda: True)
        monkeypatch.delattr(web.os, "killpg", raising=False)
        monkeypatch.delattr(web.os, "getpgid", raising=False)
        monkeypatch.delattr(web.signal, "SIGKILL", raising=False)
        runs = []
        monkeypatch.setattr(web.subprocess, "run", lambda args, **kwargs: runs.append(args))
        proc = _FakeChrome(exits_after=1)

        web._terminate_chrome(proc)

        assert runs == [["taskkill", "/PID", "4242", "/T"]]
        assert proc.waits == 1

    def test_windows_teardown_forces_the_tree_when_chrome_lingers(self, monkeypatch):
        monkeypatch.setattr(web, "_is_windows", lambda: True)
        runs = []
        monkeypatch.setattr(web.subprocess, "run", lambda args, **kwargs: runs.append(args))
        proc = _FakeChrome(exits_after=2)

        web._terminate_chrome(proc)

        assert runs == [
            ["taskkill", "/PID", "4242", "/T"],
            ["taskkill", "/PID", "4242", "/T", "/F"],
        ]
        assert proc.waits == 2

    def test_windows_teardown_survives_a_taskkill_error(self, monkeypatch):
        monkeypatch.setattr(web, "_is_windows", lambda: True)

        def boom(args, **kwargs):
            raise FileNotFoundError("taskkill")

        monkeypatch.setattr(web.subprocess, "run", boom)
        proc = _FakeChrome(exits_after=3)

        web._terminate_chrome(proc)  # logs, never raises

        assert proc.waits == 2
