"""Tests for the needs-sign-in marker: a file next to a session, best-effort only."""

import logging
import os
import stat
from datetime import datetime, timedelta

import pytest

from scrapers import sign_in_marker


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "profile.needs-sign-in"


class TestMark:
    def test_writes_the_current_utc_time(self, marker):
        """The marker says when the session was found lost, in ISO UTC."""
        before = datetime.now().astimezone()

        sign_in_marker.mark(marker)

        written = datetime.fromisoformat(marker.read_text())
        assert written.utcoffset() == timedelta(0)
        assert written >= before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_is_readable_by_its_owner_only(self, marker):
        """0600, like the session files it sits next to."""
        sign_in_marker.mark(marker)

        assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    def test_replaces_an_older_marker_and_leaves_no_temp_file(self, marker):
        """Swapped in whole: only the marker is left in the directory."""
        marker.write_text("2026-01-01T00:00:00+00:00")

        sign_in_marker.mark(marker)

        assert marker.read_text() != "2026-01-01T00:00:00+00:00"
        assert [p.name for p in marker.parent.iterdir()] == [marker.name]

    def test_an_unwritable_place_only_warns(self, tmp_path, caplog):
        """A filesystem error never escapes: the caller's own error matters more."""
        marker = tmp_path / "missing-dir" / "profile.needs-sign-in"

        with caplog.at_level(logging.WARNING, logger="scrapers.sign_in_marker"):
            sign_in_marker.mark(marker)

        assert not marker.exists()
        assert "Could not write the sign-in marker" in caplog.text


class TestMarkedSince:
    def test_no_marker_is_none(self, marker):
        """Nothing written, nothing to skip."""
        assert sign_in_marker.marked_since(marker) is None

    def test_returns_what_mark_wrote(self, marker):
        """The time comes back as written, for the caller's message."""
        sign_in_marker.mark(marker)

        assert sign_in_marker.marked_since(marker) == marker.read_text()

    @pytest.mark.parametrize("content", ["", "  \n"], ids=["empty", "blank"])
    def test_an_empty_marker_still_counts(self, marker, content):
        """Present but without a time: still marked, with an empty time."""
        marker.write_text(content)

        assert sign_in_marker.marked_since(marker) == ""

    def test_an_unreadable_marker_still_counts(self, marker, caplog):
        """When in doubt keep skipping: only a sign-in clears the marker."""
        marker.mkdir()

        with caplog.at_level(logging.WARNING, logger="scrapers.sign_in_marker"):
            since = sign_in_marker.marked_since(marker)

        assert since == ""
        assert "Could not read the sign-in marker" in caplog.text


class TestClear:
    def test_removes_the_marker(self, marker):
        """After a sign-in, runs go ahead again."""
        sign_in_marker.mark(marker)

        sign_in_marker.clear(marker)

        assert sign_in_marker.marked_since(marker) is None

    def test_no_marker_is_fine(self, marker):
        """Clearing twice (or never marked) is not an error."""
        sign_in_marker.clear(marker)

        assert not marker.exists()

    def test_a_marker_that_cannot_be_removed_only_warns(self, marker, caplog):
        """A filesystem error never escapes from a successful run."""
        marker.mkdir()

        with caplog.at_level(logging.WARNING, logger="scrapers.sign_in_marker"):
            sign_in_marker.clear(marker)

        assert "Could not remove the sign-in marker" in caplog.text
