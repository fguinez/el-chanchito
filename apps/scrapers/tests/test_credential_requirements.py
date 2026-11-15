"""`required` in the credential registry means exactly what build_scrapers
gates on: every required field set enables the scraper, and removing any one
disables it. Optional fields never gate. Every value is synthetic."""

import pytest

from main import build_scrapers
from product_model import CREDENTIAL_SPECS

_SYNTHETIC = {
    "rut": "11.111.111-1",
    "email": "user@example.com",
    "imap_host": "imap.gmail.com",
    "imap_user": "user@example.com",
    "password": "123456",
}

_SPECS = [spec for spec in CREDENTIAL_SPECS if spec.fields]


def _clear(monkeypatch):
    for spec in CREDENTIAL_SPECS:
        for f in spec.fields:
            for name in (f.env_var, *f.fallback_env_vars):
                monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MERCADOPAGO_ENABLED", raising=False)
    for name in ("FINTUAL_SESSION_FILE", "MERCADOPAGO_MOVEMENT_PAGES", "EMAIL_LOOKBACK_DAYS"):
        monkeypatch.delenv(name, raising=False)


def _set_required(monkeypatch, spec, skip=None):
    for f in spec.fields:
        if f.required and f.key != skip:
            monkeypatch.setenv(f.env_var, _SYNTHETIC.get(f.key, "synthetic-value"))


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.slug)
def test_required_fields_enable_the_scraper(monkeypatch, spec):
    _clear(monkeypatch)
    _set_required(monkeypatch, spec)
    assert set(spec.scraper_slugs) <= set(build_scrapers())


@pytest.mark.parametrize(
    ("spec", "key"),
    [(spec, f.key) for spec in _SPECS for f in spec.fields if f.required],
    ids=lambda v: v if isinstance(v, str) else v.slug,
)
def test_each_required_field_gates_the_scraper(monkeypatch, spec, key):
    _clear(monkeypatch)
    _set_required(monkeypatch, spec, skip=key)
    assert not set(spec.scraper_slugs) & set(build_scrapers())
