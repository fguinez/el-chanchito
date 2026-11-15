"""A cached session is reused only by the account that saved it. Every
identifier here is synthetic."""

import os
import stat

import pytest

from scrapers import session_account
from scrapers.institutions.fintual import FintualScraper

A, B = "user@example.com", "other@example.com"


def _session(tmp_path):
    path = tmp_path / ".fintual_session.json"
    path.write_text("{}")
    return path


def test_marker_matches_its_account_only(tmp_path):
    path = _session(tmp_path)
    session_account.write_account(path, A)
    marker = session_account.marker_path(path)
    assert A not in marker.read_text()
    assert session_account.belongs_to(path, A)
    assert not session_account.belongs_to(path, B)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_marker_is_owner_only(tmp_path):
    path = _session(tmp_path)
    old = os.umask(0)
    try:
        session_account.write_account(path, A)
    finally:
        os.umask(old)
    assert stat.S_IMODE(session_account.marker_path(path).stat().st_mode) == 0o600


def test_a_session_without_a_marker_is_not_adopted(tmp_path):
    """Saved before markers: nothing says whose it is, so no account gets it."""
    path = _session(tmp_path)
    assert not session_account.belongs_to(path, A)
    assert not session_account.belongs_to(path, B)
    assert not session_account.marker_path(path).exists()


def test_no_session_and_no_marker_is_nothing_to_refuse(tmp_path):
    assert session_account.belongs_to(tmp_path / "missing.json", A)


def test_a_saved_session_then_belongs_to_its_account(tmp_path):
    path = _session(tmp_path)
    session_account.write_account(path, A)
    assert session_account.belongs_to(path, A)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("11.111.111-1", "11111111-1"),
        ("11111111-1", "11111111-1"),
        (" 11 111 111 - 1 ", "11111111-1"),
        ("111111111", "11111111-1"),
        ("11.111.111-k", "11111111-K"),
        ("011.111.111-1", "11111111-1"),
        (" User@Example.COM ", "user@example.com"),
        ("not-a-rut", "not-a-rut"),
    ],
)
def test_normalize(raw, canonical):
    assert session_account.normalize(raw) == canonical


def test_a_rut_with_or_without_dots_is_one_account(tmp_path):
    path = _session(tmp_path)
    session_account.write_account(path, "11.111.111-k")
    assert session_account.belongs_to(path, "11111111-K")
    assert session_account.belongs_to(path, "11111111k")
    assert not session_account.belongs_to(path, "22.222.222-2")


def test_an_email_in_another_case_is_one_account(tmp_path):
    path = _session(tmp_path)
    session_account.write_account(path, A)
    assert session_account.belongs_to(path, " USER@example.com")


def _legacy_marker(path, identifier):
    salt = bytes(16)
    digest = session_account._legacy_digest(salt, identifier)
    session_account.marker_path(path).write_text(f"{salt.hex()}:{digest}\n")


def test_a_legacy_marker_still_matches_and_is_rewritten(tmp_path):
    path = _session(tmp_path)
    _legacy_marker(path, "11.111.111-1")
    marker = session_account.marker_path(path)
    before = marker.read_text()
    assert session_account.belongs_to(path, "11.111.111-1")
    assert marker.read_text() != before
    # Rewritten in the normalized form: any spelling of the RUT matches now.
    assert session_account.belongs_to(path, "11111111-1")


def test_a_legacy_marker_of_another_account_does_not_match(tmp_path):
    path = _session(tmp_path)
    _legacy_marker(path, "11.111.111-1")
    marker = session_account.marker_path(path)
    before = marker.read_text()
    assert not session_account.belongs_to(path, "22.222.222-2")
    assert marker.read_text() == before


def test_a_corrupt_marker_does_not_match(tmp_path):
    path = _session(tmp_path)
    session_account.marker_path(path).write_text("garbage")
    assert not session_account.belongs_to(path, A)


def test_fintual_discards_another_accounts_session(tmp_path, monkeypatch):
    """Covers an account change across restarts, a scraper removed and
    re-added, and an old run writing its session back after a change."""
    path = tmp_path / ".fintual_session.json"
    monkeypatch.setenv("FINTUAL_SESSION_FILE", str(path))
    monkeypatch.setenv("FINTUAL_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINTUAL_EMAIL", A)
    path.write_text('{"synthetic": "cookie"}')
    session_account.write_account(path, A)

    monkeypatch.setenv("FINTUAL_EMAIL", B)
    scraper = FintualScraper()

    class Client:
        def __init__(self):
            self.cookies = self

        def set(self, *args, **kwargs):
            raise AssertionError("another account's cookies were loaded")

    assert scraper._load_session(Client()) is False
    assert not path.exists()
    assert not session_account.marker_path(path).exists()


def _fintual(tmp_path, monkeypatch, *, imap_user):
    path = tmp_path / ".fintual_session.json"
    monkeypatch.setenv("FINTUAL_SESSION_FILE", str(path))
    monkeypatch.setenv("FINTUAL_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINTUAL_EMAIL", A)
    if imap_user:
        monkeypatch.setenv("EMAIL_IMAP_USER", imap_user)
    else:
        monkeypatch.delenv("EMAIL_IMAP_USER", raising=False)
    path.write_text('{"synthetic": "cookie"}')  # cached before markers existed
    return path, FintualScraper()


class _NoCookies:
    def __init__(self):
        self.cookies = self

    def set(self, *args, **kwargs):
        raise AssertionError("an unmarked session's cookies were loaded")


def test_fintual_unmarked_session_signs_in_again_via_imap(tmp_path, monkeypatch):
    """The re-login after discarding an unmarked session is unattended when
    the IMAP mailbox is the Fintual account."""
    import asyncio

    path, scraper = _fintual(tmp_path, monkeypatch, imap_user=A)
    signed_in = []

    async def fake_auto_login():
        signed_in.append(True)

    scraper._auto_login = fake_auto_login
    assert asyncio.run(scraper._ensure_session(_NoCookies())) is True
    assert signed_in == [True]
    assert not path.exists()


def test_fintual_unmarked_session_without_imap_asks_for_a_sign_in(tmp_path, monkeypatch):
    import asyncio

    from scrapers.base import SignInRequired

    path, scraper = _fintual(tmp_path, monkeypatch, imap_user=None)
    with pytest.raises(SignInRequired, match="make fintual-login"):
        asyncio.run(scraper._ensure_session(_NoCookies()))
    assert not path.exists()
