"""Tests for the generic real-Chrome helpers (`backends/real_chrome.py`).

No Chrome and no Playwright is ever started: `subprocess.Popen`, the signals
and the CDP connection are replaced by fakes. Every path, port and URL is
synthetic.
"""

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from scrapers.backends import real_chrome

_ENV = "SYNTHETIC_CHROME_PATH"


class _FakeChrome:
    """A launched Chrome stand-in: records waits, exits when told to."""

    pid = 4242

    def __init__(self, exits_after: int = 1, exited: bool = False, events=None):
        self.waits = 0
        self.exits_after = exits_after
        self.exited = exited
        self.events = events if events is not None else []

    def poll(self):
        return 0 if self.exited else None

    def send_signal(self, sig):
        self.events.append(("browser", sig))

    def wait(self, timeout=None):
        self.waits += 1
        self.events.append(("wait", timeout))
        if self.waits < self.exits_after:
            raise subprocess.TimeoutExpired("chrome", timeout)
        return 0


class _FakePage:
    def __init__(self, url):
        self._url = url

    @property
    def url(self):
        if self._url is None:
            raise RuntimeError("page closed")
        return self._url


class _FakeContext:
    def __init__(self, urls):
        self.pages = [_FakePage(url) for url in urls]


class _FakeBrowser:
    def __init__(self, *contexts):
        self.contexts = [_FakeContext(urls) for urls in contexts]


class _FakeChromium:
    """`connect_over_cdp` that fails `failures` times, then returns a browser."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls: list[dict] = []

    def connect_over_cdp(self, cdp_url, **kwargs):
        self.calls.append({"cdp_url": cdp_url, **kwargs})
        if len(self.calls) <= self.failures:
            raise ConnectionError("connection refused at http://127.0.0.1:1/devtools/synthetic-id")
        return "browser"


class _FakePlaywright:
    def __init__(self, failures: int = 0):
        self.chromium = _FakeChromium(failures)


class _Launched:
    """What `record_managed_launch` reads from a launched process."""

    pid = 4242


class _Clock:
    """A monotonic clock that only moves when `sleep` is called."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestChromeDiscovery:
    def _isolate(self, monkeypatch, platform, windows=False, on_path=(), exists=()):
        monkeypatch.delenv(_ENV, raising=False)
        monkeypatch.setattr(real_chrome.sys, "platform", platform)
        monkeypatch.setattr(real_chrome, "is_windows", lambda: windows)
        monkeypatch.setattr(
            real_chrome.shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if name in on_path else None,
        )
        monkeypatch.setattr(real_chrome.os.path, "exists", lambda path: path in exists)

    def test_override_wins(self, monkeypatch):
        """The caller's env var names the binary without looking anywhere."""
        monkeypatch.setenv(_ENV, "/opt/synthetic/chrome")

        assert real_chrome.find_chrome(_ENV) == "/opt/synthetic/chrome"

    @pytest.mark.parametrize(
        "name", ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]
    )
    def test_linux_names_on_path(self, monkeypatch, name):
        """Any of the usual Linux binary names on PATH is accepted."""
        self._isolate(monkeypatch, "linux", on_path={name}, exists={f"/usr/bin/{name}"})

        assert real_chrome.find_chrome(_ENV) == f"/usr/bin/{name}"

    def test_macos_applications(self, monkeypatch):
        """macOS looks under /Applications."""
        app = os.path.join("/Applications", "Google Chrome.app/Contents/MacOS/Google Chrome")
        self._isolate(monkeypatch, "darwin", exists={app})

        assert real_chrome.find_chrome(_ENV) == app

    @pytest.mark.parametrize(
        ("env_var", "suffix"),
        [
            ("PROGRAMFILES", ("Google", "Chrome", "Application", "chrome.exe")),
            ("PROGRAMFILES(X86)", ("Google", "Chrome", "Application", "chrome.exe")),
            ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
            ("LOCALAPPDATA", ("Chromium", "Application", "chrome.exe")),
        ],
    )
    def test_windows_install_dirs(self, monkeypatch, env_var, suffix):
        """Windows looks under each install base folder."""
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            monkeypatch.setenv(var, f"/synthetic/{var}")
        expected = os.path.join(f"/synthetic/{env_var}", *suffix)
        self._isolate(monkeypatch, "win32", windows=True, exists={expected})

        assert real_chrome.find_chrome(_ENV) == expected

    @pytest.mark.parametrize(
        ("platform", "windows", "hint"),
        [
            ("darwin", False, "/Applications"),
            ("win32", True, "%LocalAppData%"),
            ("linux", False, "apt install chromium"),
        ],
    )
    def test_not_found_names_the_override_and_an_os_hint(self, monkeypatch, platform, windows, hint):
        """The error says how to install Chrome and which env var overrides it."""
        self._isolate(monkeypatch, platform, windows=windows)

        with pytest.raises(real_chrome.ChromeNotFoundError) as info:
            real_chrome.find_chrome(_ENV)

        assert _ENV in str(info.value)
        assert hint in str(info.value)


class TestLaunch:
    def _capture_popen(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            real_chrome.subprocess,
            "Popen",
            lambda args, **kwargs: calls.append((args, kwargs)) or "proc",
        )
        return calls

    def _launch(self, **kwargs):
        return real_chrome.launch_chrome(
            "/synthetic/chrome",
            port=9333,
            profile_dir=Path("/synthetic/profile"),
            url="https://example.invalid/",
            **kwargs,
        )

    def test_arguments_come_from_the_caller(self, monkeypatch):
        """Port, profile and URL are the caller's; the window is visible by default."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: False)
        calls = self._capture_popen(monkeypatch)

        proc = self._launch()

        (args, kwargs), = calls
        assert proc == "proc"
        assert args[0] == "/synthetic/chrome"
        assert "--remote-debugging-port=9333" in args
        assert f"--user-data-dir={Path('/synthetic/profile')}" in args
        assert args[-1] == "https://example.invalid/"
        assert not any(arg.startswith("--window-position") for arg in args)

    def test_posix_launch_starts_a_new_session(self, monkeypatch):
        """POSIX: a new session, so one killpg reaches the whole tree."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: False)
        calls = self._capture_popen(monkeypatch)

        self._launch()

        (_, kwargs), = calls
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs

    def test_windows_launch_uses_a_new_process_group(self, monkeypatch):
        """Windows: a new process group, and off-screen parks the window."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: True)
        calls = self._capture_popen(monkeypatch)

        self._launch(offscreen=True)

        (args, kwargs), = calls
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        assert "start_new_session" not in kwargs
        assert "--window-position=-32000,-32000" in args


class TestLaunchFailure:
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(FileNotFoundError(2, "No such file", "/synthetic/secret/chrome"), id="missing"),
            pytest.param(PermissionError(13, "Permission denied", "/synthetic/secret/chrome"), id="not-executable"),
        ],
    )
    def test_a_binary_that_cannot_start_raises_without_its_path(self, monkeypatch, error):
        """The OSError names the path; the error that leaves this module does not."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: False)

        def boom(args, **kwargs):
            raise error

        monkeypatch.setattr(real_chrome.subprocess, "Popen", boom)

        with pytest.raises(real_chrome.ChromeLaunchError) as info:
            real_chrome.launch_chrome(
                "/synthetic/secret/chrome",
                port=9333,
                profile_dir=Path("/synthetic/profile"),
                url="https://example.invalid/",
            )

        assert "/synthetic" not in str(info.value)
        assert type(error).__name__ in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None


class _FakeProcess:
    """A launched process stand-in that ends after `alive_polls` polls."""

    def __init__(self, alive_polls: int):
        self.alive_polls = alive_polls
        self.polls = 0

    def poll(self):
        self.polls += 1
        return None if self.polls <= self.alive_polls else 0


class TestConnect:
    def _patch_clock(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr(real_chrome.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(real_chrome.time, "sleep", clock.sleep)
        return clock

    def test_every_attempt_carries_a_timeout(self, monkeypatch):
        """A port that opens late is retried, each attempt bounded."""
        self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=2)

        browser = real_chrome.connect_over_cdp(
            playwright, "http://127.0.0.1:9333", deadline=30.0, attempt_timeout_ms=1234
        )

        assert browser == "browser"
        assert len(playwright.chromium.calls) == 3
        assert all(call["timeout"] == 1234 for call in playwright.chromium.calls)

    def test_gives_up_at_the_deadline_without_leaking_the_url(self, monkeypatch):
        """The error names neither the CDP URL nor the underlying message."""
        self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=99)

        with pytest.raises(real_chrome.ChromeUnreachableError) as info:
            real_chrome.connect_over_cdp(
                playwright, "http://127.0.0.1:9333/devtools/synthetic-id", deadline=3.0
            )

        assert len(playwright.chromium.calls) == 3
        assert "ConnectionError" in str(info.value)
        error = info.value
        while error is not None:
            assert "127.0.0.1" not in str(error)
            assert "synthetic-id" not in str(error)
            error = error.__cause__ or error.__context__
        assert info.value.__cause__ is None and info.value.__context__ is None

    def test_a_launch_that_already_ended_fails_fast(self, monkeypatch):
        """Handed off to another Chrome on the profile: the port never opens."""
        clock = self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=99)

        with pytest.raises(real_chrome.ChromeExitedError) as info:
            real_chrome.connect_over_cdp(
                playwright, "http://127.0.0.1:9333", deadline=30.0, process=_FakeProcess(0)
            )

        assert len(playwright.chromium.calls) == 1
        assert clock.now == 0
        assert "127.0.0.1" not in str(info.value)
        assert info.value.__cause__ is None and info.value.__context__ is None

    def test_a_launch_that_ends_while_waiting_stops_the_retries(self, monkeypatch):
        """The exit is noticed on the next failed attempt, not at the deadline."""
        clock = self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=99)

        with pytest.raises(real_chrome.ChromeExitedError):
            real_chrome.connect_over_cdp(
                playwright, "http://127.0.0.1:9333", deadline=30.0, process=_FakeProcess(2)
            )

        assert len(playwright.chromium.calls) == 3
        assert clock.now == 2

    def test_a_running_launch_is_waited_for_as_before(self, monkeypatch):
        """A Chrome that is still starting gets its retries."""
        self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=2)

        browser = real_chrome.connect_over_cdp(
            playwright, "http://127.0.0.1:9333", deadline=30.0, process=_FakeProcess(99)
        )

        assert browser == "browser"

    def test_a_port_that_answers_wins_over_an_ended_launcher(self, monkeypatch):
        """Only a failed attempt consults the process: an answer is an answer."""
        self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=0)
        process = _FakeProcess(0)

        browser = real_chrome.connect_over_cdp(
            playwright, "http://127.0.0.1:9333", deadline=30.0, process=process
        )

        assert browser == "browser"
        assert process.polls == 0

    def test_one_attempt_can_be_given_the_whole_budget(self, monkeypatch):
        """A caller that knows the port is up passes a deadline of now."""
        clock = self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=99)

        with pytest.raises(real_chrome.ChromeUnreachableError):
            real_chrome.connect_over_cdp(
                playwright, "http://127.0.0.1:9333", deadline=0.0, attempt_timeout_ms=30_000
            )

        assert [call["timeout"] for call in playwright.chromium.calls] == [30_000]
        assert clock.now == 0

    def test_a_past_deadline_still_tries_once(self, monkeypatch):
        """A spent budget never skips the only attempt."""
        self._patch_clock(monkeypatch)
        playwright = _FakePlaywright(failures=0)

        assert (
            real_chrome.connect_over_cdp(playwright, "http://127.0.0.1:9333", deadline=-1.0)
            == "browser"
        )


class TestFindPage:
    def test_first_matching_tab_across_contexts(self):
        """Tabs are searched in every context; a tab whose URL raises is skipped."""
        browser = _FakeBrowser(
            ["about:blank", None],
            ["https://other.example.invalid/", "https://portal.example.invalid/a"],
        )

        page = real_chrome.find_page(browser, lambda url: "portal.example" in url)

        assert page.url == "https://portal.example.invalid/a"

    def test_no_match(self):
        """No tab satisfies the predicate: None."""
        browser = _FakeBrowser(["about:blank"])

        assert real_chrome.find_page(browser, lambda url: url.startswith("https://")) is None

class TestTargetSummary:
    """Chrome's own `/json/list`, faked: no request is ever made."""

    def _serve(self, monkeypatch, payload):
        asked = []

        def fake_get(url, timeout_s):
            asked.append((url, timeout_s))
            if isinstance(payload, Exception):
                raise payload
            return payload

        monkeypatch.setattr(real_chrome, "_http_get_json", fake_get)
        return asked

    @pytest.mark.parametrize(
        ("cdp_url", "endpoint"),
        [
            pytest.param("http://127.0.0.1:9333", "http://127.0.0.1:9333/json/list", id="http"),
            pytest.param("http://localhost:9333/", "http://localhost:9333/json/list", id="trailing-slash"),
            pytest.param(
                "ws://127.0.0.1:9333/devtools/browser/synthetic-id",
                "http://127.0.0.1:9333/json/list",
                id="websocket",
            ),
            pytest.param("wss://chrome.example.invalid/x", "https://chrome.example.invalid/json/list", id="wss"),
        ],
    )
    def test_asks_the_debug_port_the_cdp_url_names(self, monkeypatch, cdp_url, endpoint):
        """The target list lives on the same host and port, over plain HTTP."""
        asked = self._serve(monkeypatch, [])

        real_chrome.summarize_targets(cdp_url, lambda url: False, timeout_s=1.5)

        assert asked == [(endpoint, 1.5)]

    def test_counts_types_and_matching_pages_only(self, monkeypatch):
        """Only `page` targets are matched; a worker on the same host is not a tab."""
        self._serve(
            monkeypatch,
            [
                {"type": "page", "url": "https://portal.example.invalid/a"},
                {"type": "page", "url": "https://other.example.invalid/"},
                {"type": "service_worker", "url": "https://portal.example.invalid/sw.js"},
                {"type": "browser_ui", "url": "chrome://synthetic/"},
            ],
        )

        summary = real_chrome.summarize_targets(
            "http://127.0.0.1:9333", lambda url: "portal.example" in url
        )

        assert summary.types == {"page": 2, "service_worker": 1, "browser_ui": 1}
        assert (summary.matching_pages, summary.other_web_pages) == (1, 1)

    @pytest.mark.parametrize(
        ("urls", "expected"),
        [
            pytest.param(["https://portal.example.invalid/a"], (1, 0), id="on-the-portal"),
            pytest.param(["https://other.example.invalid/login"], (0, 1), id="on-another-site"),
            pytest.param(["http://other.example.invalid/"], (0, 1), id="another-site-over-http"),
            pytest.param(["about:blank"], (0, 0), id="not-navigated"),
            pytest.param(["chrome://newtab/"], (0, 0), id="browser-page"),
            pytest.param([""], (0, 0), id="empty-url"),
            pytest.param([], (0, 0), id="no-tab"),
            pytest.param(
                ["https://portal.example.invalid/a", "https://other.example.invalid/"],
                (1, 1),
                id="both",
            ),
        ],
    )
    def test_page_targets_are_sorted_into_matching_and_other_web_pages(
        self, monkeypatch, urls, expected
    ):
        """Only a web page on another site counts as 'elsewhere'."""
        self._serve(monkeypatch, [{"type": "page", "url": url} for url in urls])

        summary = real_chrome.summarize_targets(
            "http://127.0.0.1:9333", lambda url: "portal.example" in url
        )

        assert (summary.matching_pages, summary.other_web_pages) == expected

    def test_the_key_changes_when_a_matching_tab_moves_and_only_then(self, monkeypatch):
        """An opaque digest: same tabs, same key, whatever else is open."""

        def key(*urls):
            self._serve(monkeypatch, [{"type": "page", "url": url} for url in urls])
            return real_chrome.summarize_targets(
                "http://127.0.0.1:9333", lambda url: "portal.example" in url
            ).matching_key

        first = key("https://portal.example.invalid/a")

        assert first == key("https://portal.example.invalid/a", "https://other.example.invalid/")
        assert first != key("https://portal.example.invalid/b")
        assert key("https://other.example.invalid/") == ""
        assert "portal" not in first and len(first) == 64

    def test_nothing_from_a_target_but_its_type_gets_out(self, monkeypatch):
        """No URL or title in the summary; an odd type label is not echoed."""
        self._serve(
            monkeypatch,
            [
                {"type": "page", "url": "https://portal.example.invalid/?id=synthetic", "title": "Persona"},
                {"type": "Persona Sintética <b>", "url": "https://portal.example.invalid/"},
                "synthetic",
                {"url": "https://portal.example.invalid/"},
            ],
        )

        summary = real_chrome.summarize_targets("http://127.0.0.1:9333", lambda url: True)

        assert summary.types == {"page": 1, "other": 2}
        assert summary.matching_pages == 1
        assert "synthetic" not in repr(summary)
        assert "Persona" not in repr(summary)
        assert "portal.example" not in repr(summary)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(ConnectionRefusedError("refused"), id="port-closed"),
            pytest.param(TimeoutError("timed out"), id="timeout"),
            pytest.param(ValueError("not json"), id="not-json"),
            pytest.param({"synthetic": 1}, id="not-a-list"),
        ],
    )
    def test_an_endpoint_that_does_not_answer_is_none(self, monkeypatch, payload):
        """Diagnostics only: it never raises."""
        self._serve(monkeypatch, payload)

        assert real_chrome.summarize_targets("http://127.0.0.1:9333", lambda url: True) is None

    @pytest.mark.parametrize("cdp_url", ["", "synthetic", "file:///synthetic", "http://"])
    def test_a_cdp_url_without_a_host_is_not_asked(self, monkeypatch, cdp_url):
        """Nothing is requested when there is no debug port to ask."""
        asked = self._serve(monkeypatch, [])

        assert real_chrome.summarize_targets(cdp_url, lambda url: True) is None
        assert asked == []

    def test_a_matcher_that_raises_counts_as_no_match(self, monkeypatch):
        """A caller's predicate cannot break the summary."""
        self._serve(monkeypatch, [{"type": "page", "url": "https://portal.example.invalid/"}])

        def boom(url):
            raise ValueError("synthetic")

        summary = real_chrome.summarize_targets("http://127.0.0.1:9333", boom)

        assert (summary.types, summary.matching_pages) == ({"page": 1}, 0)


class TestTerminate:
    def _posix(self, monkeypatch, events):
        monkeypatch.setattr(real_chrome, "is_windows", lambda: False)
        monkeypatch.setattr(real_chrome.os, "getpgid", lambda pid: 777, raising=False)
        monkeypatch.setattr(
            real_chrome.os,
            "killpg",
            lambda pgid, sig: events.append(("group", pgid, sig)),
            raising=False,
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_posix_teardown_asks_the_browser_alone_and_stops_there(self, monkeypatch):
        """The browser exits on its own SIGTERM: its children are never signalled,
        so the network service can flush the cookie store."""
        events = []
        self._posix(monkeypatch, events)
        proc = _FakeChrome(exits_after=1, events=events)

        real_chrome.terminate_chrome(proc)

        assert events == [
            ("browser", signal.SIGTERM),
            ("wait", real_chrome._BROWSER_EXIT_WAIT_S),
        ]

    @pytest.mark.skipif(os.name == "nt", reason="SIGKILL does not exist on Windows")
    def test_posix_teardown_signals_the_group_only_after_the_browser_lingered(self, monkeypatch):
        """Order matters: browser SIGTERM, a wait, then the group, gently first."""
        events = []
        self._posix(monkeypatch, events)
        proc = _FakeChrome(exits_after=2, events=events)

        real_chrome.terminate_chrome(proc)

        assert events == [
            ("browser", signal.SIGTERM),
            ("wait", real_chrome._BROWSER_EXIT_WAIT_S),
            ("group", 777, signal.SIGTERM),
            ("wait", real_chrome._GROUP_EXIT_WAIT_S),
        ]

    @pytest.mark.skipif(os.name == "nt", reason="SIGKILL does not exist on Windows")
    def test_posix_teardown_kills_the_group_last(self, monkeypatch):
        """SIGKILL to the whole group is the last resort, so no child is orphaned."""
        events = []
        self._posix(monkeypatch, events)
        proc = _FakeChrome(exits_after=3, events=events)

        real_chrome.terminate_chrome(proc)

        assert events == [
            ("browser", signal.SIGTERM),
            ("wait", real_chrome._BROWSER_EXIT_WAIT_S),
            ("group", 777, signal.SIGTERM),
            ("wait", real_chrome._GROUP_EXIT_WAIT_S),
            ("group", 777, real_chrome.signal.SIGKILL),
            ("wait", real_chrome._GROUP_EXIT_WAIT_S),
        ]

    @pytest.mark.skipif(os.name == "nt", reason="SIGKILL does not exist on Windows")
    def test_posix_teardown_never_raises_when_chrome_will_not_die(self, monkeypatch, caplog):
        """Even SIGKILL not ending it is logged, not raised."""
        events = []
        self._posix(monkeypatch, events)
        proc = _FakeChrome(exits_after=99, events=events)

        real_chrome.terminate_chrome(proc)

        assert proc.waits == 3
        assert "did not exit after SIGKILL" in caplog.text

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_posix_teardown_without_a_group_falls_back_to_the_process(self, monkeypatch):
        """No process group to ask: the signals go to the process itself."""
        events = []
        monkeypatch.setattr(real_chrome, "is_windows", lambda: False)

        def no_group(pid):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(real_chrome.os, "getpgid", no_group, raising=False)
        proc = _FakeChrome(exits_after=2, events=events)

        real_chrome.terminate_chrome(proc)

        assert [event[:2] for event in events if event[0] == "browser"] == [
            ("browser", signal.SIGTERM),
            ("browser", signal.SIGTERM),
        ]

    @pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
    def test_a_chrome_that_already_exited_is_left_alone(self, monkeypatch, windows):
        """Its pid may be someone else's by now: nothing is signalled or killed."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: windows)
        touched = []
        monkeypatch.setattr(real_chrome.os, "getpgid", touched.append, raising=False)
        monkeypatch.setattr(
            real_chrome.os, "killpg", lambda pgid, sig: touched.append(pgid), raising=False
        )
        monkeypatch.setattr(
            real_chrome.subprocess, "run", lambda args, **kwargs: touched.append(args)
        )
        proc = _FakeChrome(exited=True)

        real_chrome.terminate_chrome(proc)

        assert touched == []
        assert proc.waits == 0

    def test_windows_teardown_never_touches_posix_only_calls(self, monkeypatch):
        """Simulates Windows: no killpg/getpgid/SIGKILL, so any use would raise."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: True)
        monkeypatch.delattr(real_chrome.os, "killpg", raising=False)
        monkeypatch.delattr(real_chrome.os, "getpgid", raising=False)
        monkeypatch.delattr(real_chrome.signal, "SIGKILL", raising=False)
        runs = []
        monkeypatch.setattr(
            real_chrome.subprocess, "run", lambda args, **kwargs: runs.append(args)
        )
        proc = _FakeChrome(exits_after=1)

        real_chrome.terminate_chrome(proc)

        assert runs == [["taskkill", "/PID", "4242", "/T"]]
        assert proc.waits == 1

    def test_windows_teardown_forces_the_tree_when_chrome_lingers(self, monkeypatch):
        """taskkill /F follows when the polite one did not end Chrome."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: True)
        runs = []
        monkeypatch.setattr(
            real_chrome.subprocess, "run", lambda args, **kwargs: runs.append(args)
        )
        proc = _FakeChrome(exits_after=2)

        real_chrome.terminate_chrome(proc)

        assert runs == [
            ["taskkill", "/PID", "4242", "/T"],
            ["taskkill", "/PID", "4242", "/T", "/F"],
        ]
        assert proc.waits == 2

    def test_windows_teardown_survives_a_taskkill_error(self, monkeypatch):
        """A missing taskkill is logged, never raised."""
        monkeypatch.setattr(real_chrome, "is_windows", lambda: True)

        def boom(args, **kwargs):
            raise FileNotFoundError("taskkill")

        monkeypatch.setattr(real_chrome.subprocess, "run", boom)
        proc = _FakeChrome(exits_after=3)

        real_chrome.terminate_chrome(proc)

        assert proc.waits == 2


_PORT = 9223


def _argv(port=_PORT, profile="/synthetic/profile"):
    return [
        "/synthetic/chrome",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
    ]


_OWNER = 5151


class _Processes:
    """The fake Chrome (pid 4242) and the run that launched it (pid 5151); nothing is signalled.

    Replaces `os.kill`, `os.killpg`, `os.getpgid`, `os.waitpid` and the
    platform's status reader; any other pid is gone.
    """

    def __init__(
        self,
        monkeypatch,
        *,
        state="alive",
        pgid=4242,
        dies_after=1,
        owner_alive=False,
        ppid=1,
        zombie_after=None,
    ):
        self.state = state
        self.pgid = pgid
        self.dies_after = dies_after
        self.owner_alive = owner_alive
        self.ppid = ppid
        self.zombie_after = zombie_after
        self.zombie = False
        self.signals: list[tuple] = []
        monkeypatch.setattr(real_chrome.os, "kill", self.kill)
        monkeypatch.setattr(real_chrome.os, "killpg", self.killpg, raising=False)
        monkeypatch.setattr(real_chrome.os, "getpgid", lambda pid: self.pgid, raising=False)
        monkeypatch.setattr(real_chrome.os, "waitpid", self.waitpid, raising=False)
        monkeypatch.setattr(real_chrome, "_status_reader", lambda platform: self.read_status)

    def _signalled(self, event):
        self.signals.append(event)
        if self.zombie_after is not None and len(self.signals) >= self.zombie_after:
            self.zombie = True
        elif len(self.signals) >= self.dies_after:
            self.state = "gone"

    def kill(self, pid, sig):
        if pid == _OWNER:
            if not self.owner_alive:
                raise ProcessLookupError(pid)
            return
        if pid != 4242 or self.state == "gone":
            raise ProcessLookupError(pid)
        if self.state == "foreign":
            raise PermissionError(pid)
        if sig != 0:
            self._signalled(("browser", pid, sig))

    def killpg(self, pgid, sig):
        if self.state == "gone":
            raise ProcessLookupError(pgid)
        self._signalled(("group", pgid, sig))

    def waitpid(self, pid, options):
        raise ChildProcessError(pid)

    def read_status(self, pid):
        return real_chrome.ProcessStatus(ppid=self.ppid, state="Z" if self.zombie else "S")


class TestOrphanReclaim:
    """The pidfile in a synthetic profile under tmp_path; no process is ever touched."""

    def _profile(self, tmp_path):
        profile = tmp_path / "profile"
        profile.mkdir()
        return profile

    def _record(self, profile, pid=4242, port=_PORT, owner=_OWNER):
        class _Proc:
            pass

        proc = _Proc()
        proc.pid = pid
        real_chrome.record_managed_launch(proc, port=port, profile_dir=profile)
        pidfile = real_chrome.managed_pidfile(profile)
        record = json.loads(pidfile.read_text())
        if owner is None:
            del record["owner_pid"]
        else:
            record["owner_pid"] = owner
        pidfile.write_text(json.dumps(record))

    def _reclaim(self, profile, cmdline=None, platform="linux", port=_PORT):
        argv = _argv(port=port, profile=profile) if cmdline is None else cmdline
        return real_chrome.reclaim_orphaned_chrome(
            port=port, profile_dir=profile, platform=platform, read_cmdline=lambda pid: argv
        )

    def _patch_clock(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr(real_chrome.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(real_chrome.time, "sleep", clock.sleep)
        return clock

    def test_record_then_forget_round_trip(self, tmp_path):
        """The record names the pid, port, owner and resolved profile; forgetting removes it."""
        profile = self._profile(tmp_path)

        real_chrome.record_managed_launch(_Launched(), port=_PORT, profile_dir=profile)
        record = json.loads(real_chrome.managed_pidfile(profile).read_text())
        real_chrome.forget_managed_launch(profile, 4242)

        assert (record["pid"], record["port"]) == (4242, _PORT)
        assert record["owner_pid"] == os.getpid()
        assert record["profile_dir"] == str(profile.resolve())
        assert isinstance(record["launched_at"], float)
        assert not real_chrome.managed_pidfile(profile).exists()
        assert list(profile.iterdir()) == []

    def test_the_pidfile_lives_inside_the_profile(self, tmp_path):
        """Inside the gitignored profile, under a name Chrome never writes."""
        profile = self._profile(tmp_path)

        pidfile = real_chrome.managed_pidfile(profile)

        assert pidfile.parent == profile
        assert pidfile.name == "el-chanchito-managed-chrome.json"

    def test_forget_ignores_another_pid(self, tmp_path):
        """A newer launch's record survives an older run clearing up."""
        profile = self._profile(tmp_path)
        self._record(profile, pid=5151)

        real_chrome.forget_managed_launch(profile, 4242)

        assert json.loads(real_chrome.managed_pidfile(profile).read_text())["pid"] == 5151

    def test_record_never_raises(self, tmp_path, caplog):
        """A profile that cannot hold the file only logs, without the path."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("synthetic")

        real_chrome.record_managed_launch(_Launched(), port=_PORT, profile_dir=blocker / "profile")

        assert "Could not record" in caplog.text
        assert str(tmp_path) not in caplog.text

    def test_no_pidfile_does_nothing(self, monkeypatch, tmp_path):
        """No record: no process is even looked at."""
        processes = _Processes(monkeypatch)
        profile = self._profile(tmp_path)

        assert self._reclaim(profile) is False
        assert processes.signals == []

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("{not json", id="not-json"),
            pytest.param("[]", id="not-an-object"),
            pytest.param('{"pid": "4242", "port": 9223, "profile_dir": "/x"}', id="pid-not-int"),
            pytest.param('{"pid": true, "port": 9223, "profile_dir": "/x"}', id="pid-bool"),
            pytest.param('{"pid": 0, "port": 9223, "profile_dir": "/x"}', id="pid-zero"),
            pytest.param('{"pid": 4242, "profile_dir": "/x"}', id="no-port"),
        ],
    )
    def test_a_corrupt_pidfile_is_removed_and_nothing_killed(self, monkeypatch, tmp_path, content):
        """A record that cannot be trusted is dropped."""
        processes = _Processes(monkeypatch)
        profile = self._profile(tmp_path)
        real_chrome.managed_pidfile(profile).write_text(content)

        assert self._reclaim(profile) is False
        assert not real_chrome.managed_pidfile(profile).exists()
        assert processes.signals == []

    def test_a_dead_pid_drops_the_record(self, monkeypatch, tmp_path):
        """The Chrome already ended: only the stale file goes."""
        processes = _Processes(monkeypatch, state="gone")
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is False
        assert not real_chrome.managed_pidfile(profile).exists()
        assert processes.signals == []

    @pytest.mark.parametrize(
        "cmdline",
        [
            pytest.param(lambda profile: ["/usr/bin/synthetic-editor", "notes.txt"], id="another-program"),
            pytest.param(lambda profile: _argv(port=9333, profile=profile), id="other-port"),
            pytest.param(lambda profile: _argv(profile=f"{profile}-2"), id="other-profile"),
            pytest.param(lambda profile: _argv(profile=profile)[:2], id="no-profile-arg"),
            pytest.param(lambda profile: [], id="empty"),
        ],
    )
    def test_an_alive_pid_that_is_not_the_managed_chrome_is_left_alone(
        self, monkeypatch, tmp_path, cmdline
    ):
        """A reused pid is never signalled; the stale record is dropped."""
        processes = _Processes(monkeypatch)
        profile = self._profile(tmp_path)
        self._record(profile)

        reclaimed = real_chrome.reclaim_orphaned_chrome(
            port=_PORT,
            profile_dir=profile,
            platform="linux",
            read_cmdline=lambda pid: cmdline(profile),
        )

        assert reclaimed is False
        assert processes.signals == []
        assert not real_chrome.managed_pidfile(profile).exists()

    def test_an_unreadable_command_line_keeps_the_record(self, monkeypatch, tmp_path):
        """A slow or failing `ps` proves nothing: nothing is killed and the next run retries."""
        processes = _Processes(monkeypatch)
        profile = self._profile(tmp_path)
        self._record(profile)

        reclaimed = real_chrome.reclaim_orphaned_chrome(
            port=_PORT, profile_dir=profile, platform="linux", read_cmdline=lambda pid: None
        )

        assert reclaimed is False
        assert processes.signals == []
        assert real_chrome.managed_pidfile(profile).exists()

    def test_an_alive_pid_outside_its_own_group_is_left_alone(self, monkeypatch, tmp_path):
        """Matching arguments are not enough: it must still lead the session we started."""
        processes = _Processes(monkeypatch, pgid=1)
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is False
        assert processes.signals == []
        assert not real_chrome.managed_pidfile(profile).exists()

    def test_a_pid_we_may_not_signal_is_left_alone(self, monkeypatch, tmp_path):
        """PermissionError means someone else's process now holds that pid."""
        processes = _Processes(monkeypatch, state="foreign")
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is False
        assert processes.signals == []

    def test_a_record_for_another_port_is_not_acted_on(self, monkeypatch, tmp_path):
        """The record, the command line and the current call must all agree."""
        processes = _Processes(monkeypatch)
        profile = self._profile(tmp_path)
        self._record(profile, port=9333)

        assert self._reclaim(profile) is False
        assert processes.signals == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_a_verified_orphan_that_exits_on_its_own_sigterm(self, monkeypatch, tmp_path):
        """The browser alone is asked first, as in `terminate_chrome`."""
        self._patch_clock(monkeypatch)
        processes = _Processes(monkeypatch, dies_after=1)
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is True
        assert processes.signals == [("browser", 4242, signal.SIGTERM)]
        assert not real_chrome.managed_pidfile(profile).exists()

    @pytest.mark.skipif(os.name == "nt", reason="SIGKILL does not exist on Windows")
    def test_a_verified_orphan_escalates_to_its_group(self, monkeypatch, tmp_path):
        """SIGTERM, the browser wait, then the group: SIGTERM, then SIGKILL."""
        clock = self._patch_clock(monkeypatch)
        processes = _Processes(monkeypatch, dies_after=3)
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is True
        assert processes.signals == [
            ("browser", 4242, signal.SIGTERM),
            ("group", 4242, signal.SIGTERM),
            ("group", 4242, signal.SIGKILL),
        ]
        assert clock.now >= real_chrome._BROWSER_EXIT_WAIT_S + real_chrome._GROUP_EXIT_WAIT_S
        assert not real_chrome.managed_pidfile(profile).exists()

    @pytest.mark.skipif(os.name == "nt", reason="SIGKILL does not exist on Windows")
    def test_an_orphan_that_survives_sigkill_keeps_its_record(self, monkeypatch, tmp_path, caplog):
        """Logged, not raised; the next run tries again."""
        self._patch_clock(monkeypatch)
        _Processes(monkeypatch, dies_after=99)
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is False
        assert "did not exit after SIGKILL" in caplog.text
        assert real_chrome.managed_pidfile(profile).exists()

    @pytest.mark.parametrize(
        ("owner", "ppid"),
        [
            pytest.param(_OWNER, _OWNER, id="sign-in-window-still-open"),
            pytest.param(os.getpid(), os.getpid(), id="our-own-drive"),
            pytest.param(_OWNER, None, id="parent-unreadable"),
        ],
    )
    def test_a_chrome_its_live_owner_still_parents_is_not_an_orphan(
        self, monkeypatch, tmp_path, owner, ppid
    ):
        """Its launching run is alive: nothing is killed and the record stays."""
        processes = _Processes(monkeypatch, owner_alive=True, ppid=ppid)
        if ppid is None:
            monkeypatch.setattr(real_chrome, "_status_reader", lambda platform: lambda pid: None)
        profile = self._profile(tmp_path)
        self._record(profile, owner=owner)

        assert self._reclaim(profile) is False
        assert processes.signals == []
        assert real_chrome.managed_pidfile(profile).exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    @pytest.mark.parametrize(
        ("owner_alive", "ppid", "owner"),
        [
            pytest.param(False, _OWNER, _OWNER, id="owner-dead"),
            pytest.param(True, 1, _OWNER, id="reparented-to-init"),
            pytest.param(True, 1, os.getpid(), id="reparented-from-our-pid"),
            pytest.param(False, 1, None, id="record-without-owner"),
        ],
    )
    def test_a_chrome_without_its_owner_is_reclaimed(
        self, monkeypatch, tmp_path, owner_alive, ppid, owner
    ):
        """A dead owner, or one that is no longer the parent, leaves a true orphan."""
        self._patch_clock(monkeypatch)
        processes = _Processes(monkeypatch, owner_alive=owner_alive, ppid=ppid)
        profile = self._profile(tmp_path)
        self._record(profile, owner=owner)

        assert self._reclaim(profile) is True
        assert processes.signals == [("browser", 4242, signal.SIGTERM)]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_an_orphan_on_a_since_changed_port_is_still_reclaimed(self, monkeypatch, tmp_path):
        """The command line is checked against the recorded port, not today's."""
        self._patch_clock(monkeypatch)
        processes = _Processes(monkeypatch)
        profile = self._profile(tmp_path)
        self._record(profile, port=9333)

        reclaimed = real_chrome.reclaim_orphaned_chrome(
            port=_PORT,
            profile_dir=profile,
            platform="linux",
            read_cmdline=lambda pid: _argv(port=9333, profile=profile),
        )

        assert reclaimed is True
        assert processes.signals == [("browser", 4242, signal.SIGTERM)]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_a_zombie_counts_as_gone_without_escalating(self, monkeypatch, tmp_path, caplog):
        """An exited but unreaped Chrome ends the wait at once, with no false warning."""
        clock = self._patch_clock(monkeypatch)
        processes = _Processes(monkeypatch, zombie_after=1)
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is True
        assert processes.signals == [("browser", 4242, signal.SIGTERM)]
        assert clock.now == 0
        assert "did not exit" not in caplog.text

    def test_a_zombie_record_is_dropped_without_signals(self, monkeypatch, tmp_path):
        """Already a zombie when found: nothing to stop."""
        processes = _Processes(monkeypatch)
        processes.zombie = True
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is False
        assert processes.signals == []
        assert not real_chrome.managed_pidfile(profile).exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_our_own_exited_child_is_reaped_as_gone(self, monkeypatch, tmp_path):
        """`waitpid` reaping it is as good as the pid being gone."""
        self._patch_clock(monkeypatch)
        processes = _Processes(monkeypatch, dies_after=99)
        monkeypatch.setattr(
            real_chrome.os,
            "waitpid",
            lambda pid, options: (pid, 0) if processes.signals else (0, 0),
            raising=False,
        )
        profile = self._profile(tmp_path)
        self._record(profile)

        assert self._reclaim(profile) is True
        assert processes.signals == [("browser", 4242, signal.SIGTERM)]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    def test_a_reclaim_logs_what_it_stopped_without_paths(self, monkeypatch, tmp_path, caplog):
        """An info line names the pid and port, never the profile."""
        caplog.set_level("INFO", logger=real_chrome.logger.name)
        self._patch_clock(monkeypatch)
        _Processes(monkeypatch)
        profile = self._profile(tmp_path)
        self._record(profile)

        self._reclaim(profile)

        assert "Reclaimed an orphaned managed Chrome (pid 4242, debug port 9223)" in caplog.text
        assert str(tmp_path) not in caplog.text

    @pytest.mark.parametrize("platform", ["win32", "cygwin"])
    def test_windows_is_skipped_and_keeps_the_record(self, monkeypatch, tmp_path, platform):
        """Only macOS, Linux and WSL2 reclaim; nothing is looked at elsewhere."""
        monkeypatch.delattr(real_chrome.os, "killpg", raising=False)
        monkeypatch.delattr(real_chrome.os, "getpgid", raising=False)
        profile = self._profile(tmp_path)
        self._record(profile)

        def never(pid):
            raise AssertionError("read the command line on Windows")

        reclaimed = real_chrome.reclaim_orphaned_chrome(
            port=_PORT, profile_dir=profile, platform=platform, read_cmdline=never
        )

        assert reclaimed is False
        assert real_chrome.managed_pidfile(profile).exists()

    @pytest.mark.parametrize(
        ("platform", "reader"),
        [
            ("linux", real_chrome.read_proc_cmdline),
            ("darwin", real_chrome.read_ps_command),
            ("freebsd14", None),
        ],
    )
    def test_the_reader_follows_the_platform(self, platform, reader):
        """/proc on Linux (and WSL2), `ps` on macOS, nothing elsewhere."""
        assert real_chrome._cmdline_reader(platform) is reader


class TestCmdlineReading:
    def _write(self, tmp_path, argv):
        (tmp_path / "4242").mkdir()
        (tmp_path / "4242" / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def test_proc_cmdline_splits_nul_separated_argv(self, tmp_path):
        """Each NUL-terminated field is one argument, spaces included."""
        argv = ["/synthetic/chrome", "--user-data-dir=/synthetic/My Profile", "--no-first-run"]
        self._write(tmp_path, argv)

        assert real_chrome.read_proc_cmdline(4242, proc_root=tmp_path) == argv

    @pytest.mark.parametrize(
        ("profile", "expected"),
        [
            pytest.param("/synthetic/profile", True, id="exact"),
            pytest.param("/synthetic/profile-2", False, id="profile-prefix"),
        ],
    )
    def test_proc_cmdline_rewritten_title_is_matched_as_one_line(self, tmp_path, profile, expected):
        """Chrome's rewritten title is one space-joined field, still matched by whole argument."""
        (tmp_path / "4242").mkdir()
        title = " ".join(_argv(profile=profile)).encode() + b"\0\0\0"
        (tmp_path / "4242" / "cmdline").write_bytes(title)
        cmdline = real_chrome.read_proc_cmdline(4242, proc_root=tmp_path)

        assert isinstance(cmdline, str)
        matched = all(
            real_chrome._has_whole_arg(cmdline, arg)
            for arg in ("--remote-debugging-port=9223", "--user-data-dir=/synthetic/profile")
        )
        assert matched is expected

    def test_proc_cmdline_of_a_missing_process_is_none(self, tmp_path):
        """A pid with no /proc entry cannot be verified."""
        assert real_chrome.read_proc_cmdline(4242, proc_root=tmp_path) is None

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(_argv(), True, id="exact"),
            pytest.param(_argv(port=92230), False, id="port-prefix"),
            pytest.param(_argv(profile="/synthetic/profile-2"), False, id="profile-prefix"),
            pytest.param(_argv(profile="/synthetic/profile/nested"), False, id="profile-parent"),
        ],
    )
    def test_proc_argv_matches_whole_arguments_only(self, tmp_path, argv, expected):
        """9223 is not 92230, and a profile path is not a prefix of another."""
        self._write(tmp_path, argv)
        cmdline = real_chrome.read_proc_cmdline(4242, proc_root=tmp_path)

        matched = all(
            real_chrome._has_whole_arg(cmdline, arg)
            for arg in ("--remote-debugging-port=9223", "--user-data-dir=/synthetic/profile")
        )

        assert matched is expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param("/synthetic/chrome --user-data-dir=/synthetic/My Profile --x", True, id="space-in-path"),
            pytest.param("/synthetic/chrome --user-data-dir=/synthetic/My Profile", True, id="last-arg"),
            pytest.param("/synthetic/chrome --user-data-dir=/synthetic/My Profile-2", False, id="prefix"),
            pytest.param("/synthetic/chrome --user-data-dir=/synthetic/My Profile 2 --x", False, id="longer-path-with-space"),
            pytest.param("/synthetic/chrome --user-data-dir=/synthetic/My Profile-2 --user-data-dir=/synthetic/My Profile", True, id="second-occurrence"),
        ],
    )
    def test_a_ps_line_matches_arguments_between_spaces(self, line, expected):
        """macOS `ps` loses argument boundaries; a match must end at a space or the line."""
        assert real_chrome._has_whole_arg(line, "--user-data-dir=/synthetic/My Profile") is expected

    @pytest.mark.parametrize(
        ("stat", "expected"),
        [
            pytest.param("4242 (chrome) S 1 4242 4242 0", (1, "S"), id="sleeping"),
            pytest.param("4242 (Chrome Helper) Z 5151 4242 4242 0", (5151, "Z"), id="zombie-spaces"),
            pytest.param("4242 (a) b) R 77 4242 4242 0", (77, "R"), id="paren-in-name"),
        ],
    )
    def test_proc_stat_gives_parent_and_state(self, tmp_path, stat, expected):
        """Fields are read after the last ')', so the command name cannot shift them."""
        (tmp_path / "4242").mkdir()
        (tmp_path / "4242" / "stat").write_text(stat)

        status = real_chrome.read_proc_status(4242, proc_root=tmp_path)

        assert (status.ppid, status.state) == expected

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            pytest.param(" 5151 Ss\n", (5151, "Ss"), id="alive"),
            pytest.param("1 Z+\n", (1, "Z+"), id="zombie"),
            pytest.param("", None, id="unknown-pid"),
        ],
    )
    def test_ps_status_gives_parent_and_state(self, monkeypatch, stdout, expected):
        """macOS: `ps -o ppid=,stat=`, None when there is no such pid."""
        monkeypatch.setattr(
            real_chrome.subprocess,
            "run",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=""),
        )

        status = real_chrome.read_ps_status(4242)

        assert (None if status is None else (status.ppid, status.state)) == expected

    def test_ps_reader_returns_none_when_ps_fails(self, monkeypatch):
        """A pid `ps` does not know cannot be verified."""
        monkeypatch.setattr(
            real_chrome.subprocess,
            "run",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr=""),
        )

        assert real_chrome.read_ps_command(4242) is None

    def test_ps_reader_asks_for_the_full_untruncated_command(self, monkeypatch):
        """`-ww` keeps a long profile path whole; the call is bounded."""
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout=" /synthetic/chrome --x\n", stderr="")

        monkeypatch.setattr(real_chrome.subprocess, "run", fake_run)

        line = real_chrome.read_ps_command(4242)

        (args, kwargs), = calls
        assert line == "/synthetic/chrome --x"
        assert args == ["ps", "-ww", "-o", "command=", "-p", "4242"]
        assert kwargs["timeout"] == real_chrome._PS_TIMEOUT_S
