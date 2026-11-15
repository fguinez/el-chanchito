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


def test_a_session_without_a_marker_is_adopted(tmp_path):
    path = _session(tmp_path)
    assert session_account.belongs_to(path, A)
    assert session_account.marker_path(path).exists()
    assert not session_account.belongs_to(path, B)


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
