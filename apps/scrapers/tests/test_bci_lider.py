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
from scrapers.backends.bci_lider_web import BciLiderCardResult
from scrapers.base import ScrapedProduct
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

    def test_warnings_propagate(self, monkeypatch):
        result = BciLiderCardResult(products=[], warnings=["BciLider: no card balances parsed"])
        self._install(monkeypatch, result=result)

        asyncio.run(self.scraper.scrape_transactions())
        prods = asyncio.run(self.scraper.scrape_products())

        assert prods.warnings == ["BciLider: no card balances parsed"]


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
