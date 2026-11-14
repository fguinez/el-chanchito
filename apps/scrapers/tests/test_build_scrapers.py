"""Tests for build_scrapers' env-var gating.

A scraper is enabled only when every credential it needs is present, so each
case clears all the variables first (main's import-time `load_dotenv()` may
have filled some from a developer's .env) and sets only its own. The
constructors just read env vars (no network, browser or file access), so the
real classes are built. Every value is synthetic.
"""

import pytest

from main import _SCHEDULES, build_scrapers

_VALUES = {
    "FINTUAL_EMAIL": "user@example.com",
    "FINTUAL_PASSWORD": "synthetic-password",
    "FINTUAL_TOKEN": "synthetic-token",
    "BUDA_API_KEY": "synthetic-key",
    "BUDA_API_SECRET": "synthetic-secret",
    "SKIPO_API_KEY": "synthetic-key",
    "BANCHILE_RUT": "00.000.000-0",
    "BANCHILE_PASSWORD": "synthetic-password",
    "LIDER_BCI_RUT": "00.000.000-0",
    "LIDER_BCI_PASSWORD": "synthetic-password",
    "SANTANDER_RUT": "00.000.000-0",
    "SANTANDER_PASSWORD": "synthetic-password",
    "BANCO_FALABELLA_RUT": "00.000.000-0",
    "BANCO_FALABELLA_PASSWORD": "synthetic-password",
    "MERCADOPAGO_ENABLED": "1",
    "EMAIL_IMAP_HOST": "imap.example.com",
    "EMAIL_IMAP_USER": "user@example.com",
    "EMAIL_IMAP_PASSWORD": "synthetic-password",
}
# Read by the constructors, not the gating; cleared so a stray value can't leak in.
_CONSTRUCTOR_VARS = (
    "FINTUAL_SESSION_FILE",
    "EMAIL_LOOKBACK_DAYS",
    "SANTANDER_HEADLESS",
    "BANCO_FALABELLA_HEADLESS",
    "MERCADOPAGO_MOVEMENT_PAGES",
)

_EMAIL = ("EMAIL_IMAP_HOST", "EMAIL_IMAP_USER", "EMAIL_IMAP_PASSWORD")
_ALL = {
    "fintual",
    "buda",
    "skipo",
    "banchile",
    "bci_lider",
    "santander",
    "banco_falabella",
    "mercadopago",
    "tenpo",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (*_VALUES, *_CONSTRUCTOR_VARS):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    ("present", "enabled"),
    [
        pytest.param((), set(), id="nothing-set"),
        pytest.param(("FINTUAL_EMAIL", "FINTUAL_PASSWORD"), {"fintual"}, id="fintual-password"),
        pytest.param(("FINTUAL_EMAIL", "FINTUAL_TOKEN"), {"fintual"}, id="fintual-token"),
        pytest.param(("FINTUAL_EMAIL",), set(), id="fintual-email-only"),
        pytest.param(("FINTUAL_PASSWORD", "FINTUAL_TOKEN"), set(), id="fintual-no-email"),
        pytest.param(("BUDA_API_KEY", "BUDA_API_SECRET"), {"buda"}, id="buda"),
        pytest.param(("BUDA_API_KEY",), set(), id="buda-key-only"),
        pytest.param(("BUDA_API_SECRET",), set(), id="buda-secret-only"),
        pytest.param(("SKIPO_API_KEY",), {"skipo"}, id="skipo"),
        pytest.param(("BANCHILE_RUT", "BANCHILE_PASSWORD"), {"banchile"}, id="banchile"),
        pytest.param(("BANCHILE_RUT",), set(), id="banchile-rut-only"),
        pytest.param(("BANCHILE_PASSWORD",), set(), id="banchile-password-only"),
        pytest.param(("LIDER_BCI_RUT",), {"bci_lider"}, id="bci-lider-rut-only"),
        pytest.param(("LIDER_BCI_PASSWORD",), set(), id="bci-lider-password-only"),
        pytest.param(("SANTANDER_RUT", "SANTANDER_PASSWORD"), {"santander"}, id="santander"),
        pytest.param(("SANTANDER_RUT",), set(), id="santander-rut-only"),
        pytest.param(("SANTANDER_PASSWORD",), set(), id="santander-password-only"),
        pytest.param(
            ("BANCO_FALABELLA_RUT", "BANCO_FALABELLA_PASSWORD"),
            {"banco_falabella"},
            id="banco-falabella",
        ),
        pytest.param(("BANCO_FALABELLA_RUT",), set(), id="banco-falabella-rut-only"),
        pytest.param(("BANCO_FALABELLA_PASSWORD",), set(), id="banco-falabella-password-only"),
        pytest.param(("MERCADOPAGO_ENABLED",), {"mercadopago"}, id="mercadopago"),
        pytest.param(_EMAIL, {"tenpo"}, id="email-scrapers"),
        pytest.param(_EMAIL[:2], set(), id="email-no-password"),
        pytest.param(_EMAIL[1:], set(), id="email-no-host"),
        pytest.param(_EMAIL[::2], set(), id="email-no-user"),
        pytest.param(tuple(_VALUES), _ALL, id="everything"),
    ],
)
def test_enabled_scrapers(monkeypatch, present, enabled):
    for var in present:
        monkeypatch.setenv(var, _VALUES[var])

    assert set(build_scrapers()) == enabled


@pytest.mark.parametrize("value", ["0", "false", "No", " off "])
def test_mercadopago_flag_can_be_switched_off(monkeypatch, value):
    """The opt-in flag reads 0/false/no/off as off, like the other flags."""
    monkeypatch.setenv("MERCADOPAGO_ENABLED", value)

    assert build_scrapers() == {}


def test_mercadopago_no_longer_rides_on_the_mailbox(monkeypatch):
    """Its e-mail leg is retired: IMAP credentials alone must not enable it."""
    for var in _EMAIL:
        monkeypatch.setenv(var, _VALUES[var])

    scrapers = build_scrapers()

    assert "mercadopago" not in scrapers


def test_mercadopago_is_a_web_scraper(monkeypatch):
    """One source per product: the wallet is read from the web, not from e-mail."""
    monkeypatch.setenv("MERCADOPAGO_ENABLED", "1")

    scraper = build_scrapers()["mercadopago"]

    assert (scraper.method, scraper.institution) == ("web", "mercadopago")


def test_mercadopago_runs_every_six_hours_off_the_mailbox_cadence():
    """No login per run, so more often than the daily browser logins."""
    schedule = _SCHEDULES["mercadopago"]

    assert schedule["hours"] == 6
    assert "minutes" not in schedule
    assert "email" not in schedule["label"].lower()


def test_every_scraper_that_can_be_enabled_has_a_schedule(monkeypatch):
    """Without one a scraper runs once at startup and can never be refreshed."""
    for var, value in _VALUES.items():
        monkeypatch.setenv(var, value)

    assert set(build_scrapers()) <= set(_SCHEDULES)


def test_empty_values_count_as_unset(monkeypatch):
    """docker-compose passes every unset credential as `${VAR:-}`, i.e. ""."""
    for var in _VALUES:
        monkeypatch.setenv(var, "")

    assert build_scrapers() == {}
