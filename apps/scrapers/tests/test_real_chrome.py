"""Tests for the generic real-Chrome helpers (`backends/real_chrome.py`).

No Chrome and no Playwright is ever started: `subprocess.Popen`, the signals
and the CDP connection are replaced by fakes. Every path, port and URL is
synthetic.
"""

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
