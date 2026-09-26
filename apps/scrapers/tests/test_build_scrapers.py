"""Tests for build_scrapers' env-var gating.

A scraper is enabled only when every credential it needs is present, so each
case clears all the variables first (main's import-time `load_dotenv()` may
have filled some from a developer's .env) and sets only its own. The
constructors just read env vars (no network, browser or file access), so the
real classes are built. Every value is synthetic.
"""

import pytest

from main import build_scrapers

_VALUES = {
    "FINTUAL_EMAIL": "user@example.com",
    "FINTUAL_PASSWORD": "synthetic-password",
    "FINTUAL_TOKEN": "synthetic-token",
    "BUDA_API_KEY": "synthetic-key",
    "BUDA_API_SECRET": "synthetic-secret",
    "BANCHILE_RUT": "00.000.000-0",
    "BANCHILE_PASSWORD": "synthetic-password",
    "LIDER_BCI_RUT": "00.000.000-0",
    "LIDER_BCI_PASSWORD": "synthetic-password",
    "EMAIL_IMAP_HOST": "imap.example.com",
    "EMAIL_IMAP_USER": "user@example.com",
    "EMAIL_IMAP_PASSWORD": "synthetic-password",
}
# Read by the constructors, not the gating; cleared so a stray value can't leak in.
_CONSTRUCTOR_VARS = ("FINTUAL_SESSION_FILE", "EMAIL_LOOKBACK_DAYS")

_EMAIL = ("EMAIL_IMAP_HOST", "EMAIL_IMAP_USER", "EMAIL_IMAP_PASSWORD")
_ALL = {"fintual", "buda", "banchile", "bci_lider", "mach", "mercadopago", "tenpo"}


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
        pytest.param(("BANCHILE_RUT", "BANCHILE_PASSWORD"), {"banchile"}, id="banchile"),
        pytest.param(("BANCHILE_RUT",), set(), id="banchile-rut-only"),
        pytest.param(("BANCHILE_PASSWORD",), set(), id="banchile-password-only"),
        pytest.param(("LIDER_BCI_RUT",), {"bci_lider"}, id="bci-lider-rut-only"),
        pytest.param(("LIDER_BCI_PASSWORD",), set(), id="bci-lider-password-only"),
        pytest.param(_EMAIL, {"mach", "mercadopago", "tenpo"}, id="email-trio"),
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


def test_empty_values_count_as_unset(monkeypatch):
    """docker-compose passes every unset credential as `${VAR:-}`, i.e. ""."""
    for var in _VALUES:
        monkeypatch.setenv(var, "")

    assert build_scrapers() == {}
