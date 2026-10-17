"""`alias_for_transaction` and the display-description COALESCE (V035).

These run the SQL that resolves an alias at read time, against a freshly
migrated PostgreSQL (see conftest.py). They are self-contained: the function
takes literals, so no transaction or product rows are needed, only
`description_aliases` rows. Skipped unless TEST_DATABASE_URL is set. Every
description, alias and figure is synthetic (see the repo's personal-data
policy).
"""

from datetime import datetime, timezone
from uuid import UUID

import pytest


def _insert_alias(
    conn,
    match_description,
    alias,
    *,
    match_type="exact",
    priority=0,
    created_at=None,
    alias_id=None,
):
    """Insert one alias, optionally pinning created_at/id for tie-break tests."""
    conn.execute(
        """
        INSERT INTO description_aliases
            (id, match_description, match_type, alias, priority, created_at)
        VALUES (
            COALESCE(%(id)s, gen_random_uuid()),
            %(match_description)s, %(match_type)s, %(alias)s, %(priority)s,
            COALESCE(%(created_at)s, now())
        )
        """,
        {
            "id": alias_id,
            "match_description": match_description,
            "match_type": match_type,
            "alias": alias,
            "priority": priority,
            "created_at": created_at,
        },
    )


def _alias_for(conn, description, amount=None, currency=None):
    row = conn.execute(
        "SELECT alias_for_transaction(%s, %s, %s) AS alias",
        (description, amount, currency),
    ).fetchone()
    return row["alias"]


def _resolve(conn, custom, description, amount=None, currency=None):
    """The exact production display expression (COALESCE custom -> alias -> raw)."""
    row = conn.execute(
        """
        SELECT COALESCE(
            %(custom)s,
            alias_for_transaction(%(desc)s, %(amt)s, %(cur)s),
            %(desc)s
        ) AS display
        """,
        {"custom": custom, "desc": description, "amt": amount, "cur": currency},
    ).fetchone()
    return row["display"]


class TestExactMatch:
    def test_exact_match_is_case_insensitive_and_trimmed(self, db_conn):
        _insert_alias(db_conn, "SPOTIFY AB", "Música", match_type="exact")
        # Different case and surrounding whitespace on the transaction side.
        assert _alias_for(db_conn, "  spotify ab  ") == "Música"
        assert _alias_for(db_conn, "SPOTIFY AB") == "Música"

    def test_exact_match_needs_the_whole_string(self, db_conn):
        _insert_alias(db_conn, "SPOTIFY", "Música", match_type="exact")
        # A superstring is not an exact match.
        assert _alias_for(db_conn, "SPOTIFY PREMIUM") is None


class TestSubstringMatch:
    def test_substring_match_returns_the_alias(self, db_conn):
        _insert_alias(db_conn, "uber", "Transporte", match_type="substring")
        assert _alias_for(db_conn, "UBER *TRIP 1234") == "Transporte"

    def test_non_substring_returns_null(self, db_conn):
        _insert_alias(db_conn, "uber", "Transporte", match_type="substring")
        assert _alias_for(db_conn, "COMPRA SINTETICA") is None

    def test_wildcards_are_literal_not_like_patterns(self, db_conn):
        # strpos is literal, so `%` and `_` match themselves, unlike LIKE.
        _insert_alias(db_conn, "50%_OFF", "Descuento", match_type="substring")
        assert _alias_for(db_conn, "PROMO 50%_OFF HOY") == "Descuento"
        # The same literal, with the wildcard positions filled in, must NOT match.
        assert _alias_for(db_conn, "50XYOFF") is None


class TestBlankNeverMatches:
    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_match_description_never_matches(self, db_conn, blank):
        _insert_alias(db_conn, blank, "Fantasma", match_type="substring")
        # A blank needle would otherwise be a substring of everything.
        assert _alias_for(db_conn, "CUALQUIER COSA") is None


class TestOrdering:
    def test_higher_priority_wins(self, db_conn):
        _insert_alias(db_conn, "compra", "Baja", match_type="substring", priority=0)
        _insert_alias(db_conn, "compra", "Alta", match_type="substring", priority=10)
        assert _alias_for(db_conn, "COMPRA WEB") == "Alta"

    def test_tie_breaks_on_older_created_at(self, db_conn):
        older = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 2, 1, tzinfo=timezone.utc)
        _insert_alias(
            db_conn, "compra", "Nueva", match_type="substring", created_at=newer
        )
        _insert_alias(
            db_conn, "compra", "Antigua", match_type="substring", created_at=older
        )
        assert _alias_for(db_conn, "COMPRA WEB") == "Antigua"

    def test_tie_breaks_on_lower_id_when_created_at_is_equal(self, db_conn):
        same = datetime(2026, 3, 1, tzinfo=timezone.utc)
        low = UUID("00000000-0000-4000-8000-000000000001")
        high = UUID("00000000-0000-4000-8000-0000000000ff")
        _insert_alias(
            db_conn,
            "compra",
            "AltaId",
            match_type="substring",
            created_at=same,
            alias_id=high,
        )
        _insert_alias(
            db_conn,
            "compra",
            "BajaId",
            match_type="substring",
            created_at=same,
            alias_id=low,
        )
        assert _alias_for(db_conn, "COMPRA WEB") == "BajaId"


class TestAmountCurrencyIgnored:
    def test_amount_and_currency_are_currently_ignored(self, db_conn):
        # Phase 3 (issue #103) will make these arguments filter matches; for now
        # the match keys on the description alone, so any amount/currency hits.
        _insert_alias(db_conn, "NETFLIX AB", "Streaming", match_type="exact")
        assert _alias_for(db_conn, "NETFLIX AB", amount=9990, currency="CLP") == (
            "Streaming"
        )
        assert _alias_for(db_conn, "NETFLIX AB", amount=123456, currency="USD") == (
            "Streaming"
        )
        assert _alias_for(db_conn, "NETFLIX AB", amount=None, currency=None) == (
            "Streaming"
        )


class TestResolutionPriority:
    def test_custom_description_wins_over_everything(self, db_conn):
        _insert_alias(db_conn, "COMPRA WEB", "Alias", match_type="exact")
        assert (
            _resolve(db_conn, "Personalizada", "COMPRA WEB", 2500000, "CLP")
            == "Personalizada"
        )

    def test_alias_wins_when_no_custom_description(self, db_conn):
        _insert_alias(db_conn, "COMPRA WEB", "Alias", match_type="exact")
        assert _resolve(db_conn, None, "COMPRA WEB", 1000000, "CLP") == "Alias"

    def test_raw_description_when_neither_applies(self, db_conn):
        # No alias inserted, no custom override: the scraped text shows through.
        assert (
            _resolve(db_conn, None, "COMPRA SINTETICA", 999999, "CLP")
            == "COMPRA SINTETICA"
        )
