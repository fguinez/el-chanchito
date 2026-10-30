"""Tests for the shared desktop Chrome user agent."""

import pytest

from scrapers.backends import banco_falabella_web, santander_web
from scrapers.backends.user_agent import os_token, user_agent_for


@pytest.mark.parametrize(
    ("platform", "token"),
    [
        ("darwin", "Macintosh; Intel Mac OS X 10_15_7"),
        ("win32", "Windows NT 10.0; Win64; x64"),
        ("cygwin", "Windows NT 10.0; Win64; x64"),
        ("linux", "X11; Linux x86_64"),
        ("freebsd14", "X11; Linux x86_64"),
    ],
)
def test_os_token_is_chromes_frozen_platform(platform, token):
    assert os_token(platform) == token


def test_linux_token_does_not_follow_the_cpu(monkeypatch):
    """Chrome freezes Linux to x86_64 even on aarch64 (a Raspberry Pi)."""
    import platform as stdlib_platform

    monkeypatch.setattr(stdlib_platform, "machine", lambda: "aarch64")

    assert "(X11; Linux x86_64)" in user_agent_for("141.0.7390.37", "linux")


def test_both_scrapers_share_the_helper():
    assert santander_web.user_agent_for is user_agent_for
    assert banco_falabella_web.user_agent_for is user_agent_for
