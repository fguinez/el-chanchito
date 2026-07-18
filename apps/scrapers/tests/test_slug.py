"""Tests for the product slug helpers.

`slugify` and `unique_slug` mint the create-only, institution-unique product
slug. The vectors below are shared verbatim with the TypeScript suite
(apps/web/src/lib/__tests__/slug.test.ts) so both creation paths provably
agree; change them in both places or not at all.
"""

import pytest

from db.slug import slugify, unique_slug


class TestSlugify:
    @pytest.mark.parametrize(
        ("name", "kind", "expected"),
        [
            ("Tarjeta de crédito ****1234", "credit_card", "tarjeta-de-credito-1234"),
            ("Cuenta Corriente", "checking", "cuenta-corriente"),
            ("Fondo Ñuñoa Ültra", "investment", "fondo-nunoa-ultra"),
            (
                "Depósito a Plazo Nº 00-000-00000-01",
                "term_deposit",
                "deposito-a-plazo-no-00-000-00000-01",
            ),
            ("$$$", "credit_card", "credit-card"),
            ("MACH - checking (USD)", "checking", "mach-checking-usd"),
        ],
    )
    def test_shared_vectors(self, name, kind, expected):
        """The cross-language vectors both implementations must satisfy."""
        assert slugify(name, kind) == expected

    def test_fallback_hyphenates_kind_underscores(self):
        """A name with nothing to keep falls back to the hyphenated kind."""
        assert slugify("***", "term_deposit") == "term-deposit"


class TestUniqueSlug:
    def test_free_base_is_returned_unsuffixed(self):
        """A slug nobody holds needs no suffix."""
        assert unique_slug("cuenta-corriente", set()) == "cuenta-corriente"

    def test_taken_base_gets_first_suffix(self):
        """The suffix series starts at -2 for the first duplicate."""
        assert unique_slug("x", {"x"}) == "x-2"

    def test_suffix_series_advances_past_taken_slots(self):
        """Each further duplicate takes the next free slot."""
        assert unique_slug("x", {"x", "x-2"}) == "x-3"

    def test_taken_suffix_does_not_block_free_base(self):
        """Only the base itself matters; a held -2 slot is irrelevant."""
        assert unique_slug("x", {"x-2"}) == "x"
