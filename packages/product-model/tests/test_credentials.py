"""The credential registry is consistent and covers every credential shape."""

import re

import pytest

from product_model import (
    CREDENTIAL_SPECS,
    CREDENTIALS_BY_SLUG,
    CredentialFieldKind,
    CredentialSpecError,
    credential_env_vars,
    validate_credential_specs,
)
from product_model import credentials as credentials_module


def test_registry_validates():
    validate_credential_specs()


def test_env_vars_are_unique():
    env_vars = credential_env_vars()
    assert len(env_vars) == len(set(env_vars))


def test_every_shape_is_covered():
    shapes = {
        slug: tuple(f.kind for f in CREDENTIALS_BY_SLUG[slug].fields)
        for slug in CREDENTIALS_BY_SLUG
    }
    identifier, secret = CredentialFieldKind.IDENTIFIER, CredentialFieldKind.SECRET
    assert shapes["banchile"] == (identifier, secret)
    assert shapes["buda"] == (secret, secret)
    assert shapes["skipo"] == (secret,)
    assert shapes["email"] == (identifier, identifier, secret)
    assert shapes["mercadopago"] == ()


def test_patterns_and_defaults_agree():
    for spec in CREDENTIAL_SPECS:
        for f in spec.fields:
            if f.pattern and f.default:
                assert re.fullmatch(f.pattern, f.default)


def test_banco_falabella_clave_is_six_digits():
    clave = next(f for f in CREDENTIALS_BY_SLUG["banco_falabella"].fields if f.key == "password")
    assert re.fullmatch(clave.pattern, "123456")
    assert not re.fullmatch(clave.pattern, "12345")
    assert not re.fullmatch(clave.pattern, "1234567")


def test_duplicate_env_var_is_rejected(monkeypatch):
    spec = CREDENTIALS_BY_SLUG["banchile"]
    clone = type(spec)(
        slug="clone",
        label_es="Clone",
        logo_slug="banchile",
        scraper_slugs=(),
        fields=spec.fields,
    )
    monkeypatch.setattr(credentials_module, "CREDENTIAL_SPECS", (*CREDENTIAL_SPECS, clone))
    with pytest.raises(CredentialSpecError):
        credentials_module.validate_credential_specs()


def test_slug_charset_is_enforced(monkeypatch):
    spec = CREDENTIALS_BY_SLUG["skipo"]
    bad = type(spec)(
        slug="Bad|slug",
        label_es="Bad",
        logo_slug="skipo",
        scraper_slugs=(),
        fields=(),
    )
    monkeypatch.setattr(credentials_module, "CREDENTIAL_SPECS", (*CREDENTIAL_SPECS, bad))
    with pytest.raises(CredentialSpecError):
        credentials_module.validate_credential_specs()


def test_max_length_fits_the_sealed_column(monkeypatch):
    from product_model.credentials import HPKE_OVERHEAD_BYTES, MAX_FIELD_LENGTH, MAX_SEALED_BYTES

    assert MAX_FIELD_LENGTH * 4 + HPKE_OVERHEAD_BYTES <= MAX_SEALED_BYTES
    for spec in CREDENTIAL_SPECS:
        for f in spec.fields:
            assert f.max_length * 4 + HPKE_OVERHEAD_BYTES <= MAX_SEALED_BYTES
    spec = CREDENTIALS_BY_SLUG["skipo"]
    too_long = type(spec)(
        slug="too_long",
        label_es="Too long",
        logo_slug="skipo",
        scraper_slugs=(),
        fields=(
            type(spec.fields[0])(
                **{
                    **_field_kwargs(spec.fields[0]),
                    "env_var": "TOO_LONG",
                    "max_length": MAX_FIELD_LENGTH + 1,
                }
            ),
        ),
    )
    monkeypatch.setattr(credentials_module, "CREDENTIAL_SPECS", (*CREDENTIAL_SPECS, too_long))
    with pytest.raises(CredentialSpecError):
        credentials_module.validate_credential_specs()


def _field_kwargs(f):
    from dataclasses import fields

    return {x.name: getattr(f, x.name) for x in fields(f)}


def test_imap_password_binds_its_destination():
    from product_model import binding_groups

    assert binding_groups(CREDENTIALS_BY_SLUG["email"]) == [
        ("imap_password", "imap_host", "imap_user")
    ]
    host = CREDENTIALS_BY_SLUG["email"].fields[0]
    assert "imap.gmail.com" in host.allowed_values
    assert host.allow_custom_env == "EMAIL_IMAP_ALLOW_CUSTOM_HOST"
