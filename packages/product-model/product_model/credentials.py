"""Which credentials each institution's scraper needs.

One spec per institution card in the dashboard's Credenciales view. The web
renders its forms and validates submissions from the generated
`CREDENTIAL_SPECS`; the scraper service overlays each stored field onto the
environment variable `env_var` names, the same names `build_scrapers()` and
the scrapers have always read, so setups that export them keep working.

Every field is sealed before it is stored, identifiers (RUT, e-mail) included,
and no API ever returns a stored value: a spec only describes the form.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class CredentialFieldKind(StrEnum):
    """How a field is entered: identifiers in a text input, secrets masked."""

    IDENTIFIER = "identifier"
    SECRET = "secret"


class CredentialInputMode(StrEnum):
    """The `inputmode` hint for a field's input."""

    TEXT = "text"
    EMAIL = "email"
    NUMERIC = "numeric"


# Slugs and field keys are part of each ciphertext's sealed context, so they
# are kept to a charset that can never be confused with its separators.
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Generous for any password or API secret, small enough to bound a request.
DEFAULT_MAX_LENGTH = 256


@dataclass(frozen=True, slots=True)
class CredentialField:
    """One value an institution needs.

    `pattern` is a full-match regular expression in the subset Python and
    JavaScript read the same way; the web enforces it on save. `default` is a
    suggestion shown in the form, never stored unless submitted.
    `fallback_env_vars` are legacy names the scraper still reads for the same
    value (they count as the local fallback, never written).
    """

    key: str
    label_es: str
    env_var: str
    kind: CredentialFieldKind
    required: bool = True
    input_mode: CredentialInputMode = CredentialInputMode.TEXT
    placeholder: str | None = None
    pattern: str | None = None
    pattern_hint_es: str | None = None
    default: str | None = None
    help_es: str | None = None
    max_length: int = DEFAULT_MAX_LENGTH
    fallback_env_vars: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstitutionCredentials:
    """The credentials card of one institution.

    `slug` keys the stored rows; `scraper_slugs` are the scrapers these
    credentials enable (the e-mail inbox feeds Tenpo); `logo_slug` picks the
    logo. An institution with no fields (Mercado Pago) is shown with its
    `note_es` only: there is nothing to store.
    """

    slug: str
    label_es: str
    fields: tuple[CredentialField, ...]
    scraper_slugs: tuple[str, ...]
    logo_slug: str
    note_es: str | None = None


def _rut(env_var: str) -> CredentialField:
    return CredentialField(
        key="rut",
        label_es="RUT",
        env_var=env_var,
        kind=CredentialFieldKind.IDENTIFIER,
        placeholder="11.111.111-1",
        help_es="Tal como lo escribes al entrar al sitio.",
        max_length=16,
    )


def _clave(env_var: str, **overrides) -> CredentialField:
    return CredentialField(
        **{
            "key": "password",
            "label_es": "Clave",
            "env_var": env_var,
            "kind": CredentialFieldKind.SECRET,
            **overrides,
        }
    )


CREDENTIAL_SPECS: tuple[InstitutionCredentials, ...] = (
    InstitutionCredentials(
        slug="banchile",
        label_es="Banco de Chile",
        logo_slug="banchile",
        scraper_slugs=("banchile",),
        fields=(_rut("BANCHILE_RUT"), _clave("BANCHILE_PASSWORD")),
    ),
    InstitutionCredentials(
        slug="bci_lider",
        label_es="Tarjeta Lider Bci",
        logo_slug="bci_lider",
        scraper_slugs=("bci_lider",),
        fields=(_rut("LIDER_BCI_RUT"), _clave("LIDER_BCI_PASSWORD")),
        note_es=(
            "Abre un Chrome real en cada lectura (el sitio bloquea navegadores "
            "automatizados), así que necesita un equipo con pantalla."
        ),
    ),
    InstitutionCredentials(
        slug="santander",
        label_es="Banco Santander",
        logo_slug="santander",
        scraper_slugs=("santander",),
        fields=(_rut("SANTANDER_RUT"), _clave("SANTANDER_PASSWORD")),
    ),
    InstitutionCredentials(
        slug="banco_falabella",
        label_es="Banco Falabella",
        logo_slug="banco_falabella",
        scraper_slugs=("banco_falabella",),
        fields=(
            _rut("BANCO_FALABELLA_RUT"),
            _clave(
                "BANCO_FALABELLA_PASSWORD",
                label_es="Clave de 6 dígitos",
                input_mode=CredentialInputMode.NUMERIC,
                pattern="[0-9]{6}",
                pattern_hint_es="debe tener exactamente 6 dígitos.",
                max_length=6,
            ),
        ),
    ),
    InstitutionCredentials(
        slug="fintual",
        label_es="Fintual",
        logo_slug="fintual",
        scraper_slugs=("fintual",),
        fields=(
            CredentialField(
                key="email",
                label_es="Correo",
                env_var="FINTUAL_EMAIL",
                kind=CredentialFieldKind.IDENTIFIER,
                input_mode=CredentialInputMode.EMAIL,
                placeholder="usuario@example.com",
                pattern="[^\\s@]+@[^\\s@]+",
                pattern_hint_es="debe ser un correo.",
            ),
            _clave("FINTUAL_PASSWORD", label_es="Contraseña", fallback_env_vars=("FINTUAL_TOKEN",)),
        ),
        note_es=(
            "La primera vez Fintual pide un código que llega por correo: corre "
            "`make fintual-login` una vez para guardar la sesión."
        ),
    ),
    InstitutionCredentials(
        slug="buda",
        label_es="Buda",
        logo_slug="buda",
        scraper_slugs=("buda",),
        fields=(
            CredentialField(
                key="api_key",
                label_es="API key",
                env_var="BUDA_API_KEY",
                kind=CredentialFieldKind.SECRET,
            ),
            CredentialField(
                key="api_secret",
                label_es="API secret",
                env_var="BUDA_API_SECRET",
                kind=CredentialFieldKind.SECRET,
            ),
        ),
        note_es="Crea una API key de solo lectura en Buda.",
    ),
    InstitutionCredentials(
        slug="skipo",
        label_es="Skipo",
        logo_slug="skipo",
        scraper_slugs=("skipo",),
        fields=(
            CredentialField(
                key="api_key",
                label_es="API key",
                env_var="SKIPO_API_KEY",
                kind=CredentialFieldKind.SECRET,
                help_es="Usa una key de solo lectura.",
            ),
        ),
    ),
    InstitutionCredentials(
        slug="email",
        label_es="Correo (Tenpo)",
        logo_slug="tenpo",
        scraper_slugs=("tenpo",),
        fields=(
            CredentialField(
                key="imap_host",
                label_es="Servidor IMAP",
                env_var="EMAIL_IMAP_HOST",
                kind=CredentialFieldKind.IDENTIFIER,
                placeholder="imap.gmail.com",
                default="imap.gmail.com",
                pattern="[A-Za-z0-9.-]+",
                pattern_hint_es="solo el nombre del servidor, sin puerto ni protocolo.",
                max_length=253,
            ),
            CredentialField(
                key="imap_user",
                label_es="Usuario",
                env_var="EMAIL_IMAP_USER",
                kind=CredentialFieldKind.IDENTIFIER,
                input_mode=CredentialInputMode.EMAIL,
                placeholder="usuario@example.com",
            ),
            CredentialField(
                key="imap_password",
                label_es="Contraseña de aplicación",
                env_var="EMAIL_IMAP_PASSWORD",
                kind=CredentialFieldKind.SECRET,
                help_es="En Gmail, una contraseña de aplicación, no la de tu cuenta.",
            ),
        ),
        note_es="Lee las notificaciones de Tenpo desde tu bandeja de entrada.",
    ),
    InstitutionCredentials(
        slug="mercadopago",
        label_es="Mercado Pago",
        logo_slug="mercadopago",
        scraper_slugs=("mercadopago",),
        fields=(),
        note_es=(
            "No hay credenciales que guardar: inicias sesión a mano en un Chrome "
            "dedicado con `make mercadopago-login`, y se activa con "
            "MERCADOPAGO_ENABLED=1 en .env."
        ),
    ),
)

CREDENTIALS_BY_SLUG: dict[str, InstitutionCredentials] = {
    spec.slug: spec for spec in CREDENTIAL_SPECS
}


class CredentialSpecError(ValueError):
    """The credential registry is inconsistent."""


def credential_env_vars() -> tuple[str, ...]:
    """Every env var a stored credential may set, in registry order."""
    return tuple(f.env_var for spec in CREDENTIAL_SPECS for f in spec.fields)


def validate_credential_specs() -> None:
    """Raise CredentialSpecError on a duplicate or malformed key, env var or pattern."""
    seen_slugs: set[str] = set()
    seen_envs: set[str] = set()
    for spec in CREDENTIAL_SPECS:
        if not _KEY_RE.match(spec.slug) or spec.slug in seen_slugs:
            raise CredentialSpecError(f"bad or duplicate credential slug {spec.slug!r}")
        seen_slugs.add(spec.slug)
        keys: set[str] = set()
        for f in spec.fields:
            where = f"{spec.slug}.{f.key}"
            if not _KEY_RE.match(f.key) or f.key in keys:
                raise CredentialSpecError(f"bad or duplicate field key {where}")
            keys.add(f.key)
            for env in (f.env_var, *f.fallback_env_vars):
                if not _ENV_RE.match(env) or env in seen_envs:
                    raise CredentialSpecError(f"bad or duplicate env var {env!r} ({where})")
                seen_envs.add(env)
            if not 1 <= f.max_length <= 1024:
                raise CredentialSpecError(f"{where}: max_length out of range")
            if f.pattern is not None:
                re.compile(f.pattern)
            if f.default is not None and f.pattern and not re.fullmatch(f.pattern, f.default):
                raise CredentialSpecError(f"{where}: default does not match pattern")
