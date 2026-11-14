"""Generic plumbing for driving a genuine Chrome over CDP (acquisition rung 7).

Some portals only work in a real browser: a bot check that a
Playwright-launched Chromium fails, or a session bound to the browser a human
signed in with. For those the scraper starts an ordinary Chrome process (the
user's own binary, a dedicated persistent profile, a debug port) and Playwright
only attaches to it over CDP.

This module holds the parts of that which are the same for every institution:
finding the Chrome binary, launching it on a debug port, checking the port,
connecting, finding a tab, asking Chrome what it has open and tearing the
process tree down. Nothing here
knows an institution: ports, profile directories, URLs and env var names are
arguments. Mercado Pago (`backends/mercadopago_web.py`) is its only user today.
`backends/bci_lider_web.py` predates it and carries its own private copies of
these helpers; it should move onto this module in a change of its own.

Every call that talks to the browser takes a timeout or a deadline: an
unbounded Playwright call has hung the scraper service before.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# One `connect_over_cdp` attempt. A tab stuck loading can hold the connection
# for its whole timeout, so it stays well under any caller's deadline.
CONNECT_ATTEMPT_TIMEOUT_MS = 10_000
_CONNECT_RETRY_PAUSE_S = 1.0
# Teardown: how long the browser gets to exit by itself after its own
# SIGTERM, and how long after each signal to its whole process group.
_BROWSER_EXIT_WAIT_S = 10
_GROUP_EXIT_WAIT_S = 5
# Chrome's target list is a few kilobytes; more than this is not read.
_TARGET_LIST_MAX_BYTES = 1_000_000
_TARGET_TYPE_RE = re.compile(r"^[a-z_]{1,30}$")

# Chrome's own install dirs under each Windows base folder (Chromium installs
# per user, like Chrome's non-admin install).
_WINDOWS_CHROME_DIRS = (
    ("PROGRAMFILES", ("Google", "Chrome", "Application", "chrome.exe")),
    ("PROGRAMFILES(X86)", ("Google", "Chrome", "Application", "chrome.exe")),
    ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
    ("LOCALAPPDATA", ("Chromium", "Application", "chrome.exe")),
)
# Linux names on PATH. Google Chrome has no ARM64 Linux build, so on a
# Raspberry Pi this finds Debian's `chromium`.
_PATH_CHROME_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


class RealChromeError(RuntimeError):
    """A real-Chrome step failed. The message never carries a URL or a path."""


class ChromeNotFoundError(RealChromeError):
    """No Chrome or Chromium binary was found on this machine."""


class ChromeLaunchError(RealChromeError):
    """The Chrome binary could not be started (missing, or not executable)."""


class ChromeUnreachableError(RealChromeError):
    """No CDP connection to the Chrome debug port could be made in time."""


class ChromeExitedError(RealChromeError):
    """The launched Chrome process ended before its debug port answered."""


def is_windows() -> bool:
    return os.name == "nt"


def chrome_candidates() -> list[str]:
    """Where a real Chrome/Chromium usually lives on this OS, most preferred first."""
    candidates: list[str] = []
    if sys.platform == "darwin":
        for app in (
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Chromium.app/Contents/MacOS/Chromium",
        ):
            candidates.append(os.path.join("/Applications", app))
            candidates.append(os.path.join(os.path.expanduser("~/Applications"), app))
    if is_windows():
        for env_var, parts in _WINDOWS_CHROME_DIRS:
            base = os.environ.get(env_var)
            if base:
                candidates.append(os.path.join(base, *parts))
    for name in _PATH_CHROME_NAMES:
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return candidates


def chrome_install_hint() -> str:
    if sys.platform == "darwin":
        return "Install Google Chrome into /Applications."
    if is_windows():
        return (
            "Install Google Chrome (looked under %ProgramFiles%, %ProgramFiles(x86)% "
            "and %LocalAppData%)."
        )
    return (
        "Install google-chrome-stable, or Chromium (Debian/Raspberry Pi OS: "
        "`sudo apt install chromium`; Google Chrome has no ARM64 Linux build)."
    )


def find_chrome(override_env: str) -> str:
    """Path to a real Chrome/Chromium; the env var `override_env` wins when set."""
    override = os.environ.get(override_env)
    if override:
        return override
    for candidate in chrome_candidates():
        if os.path.exists(candidate):
            return candidate
    raise ChromeNotFoundError(
        "Could not find Google Chrome or Chromium. "
        f"{chrome_install_hint()} Or set {override_env} to its binary."
    )


def launch_chrome(
    executable: str,
    *,
    port: int,
    profile_dir: Path,
    url: str,
    offscreen: bool = False,
) -> subprocess.Popen:
    """Start a genuine Chrome with remote debugging on `port`, opening `url`.

    `profile_dir` is the persistent profile (cookies, the signed-in session).
    `offscreen` parks the window off-screen and stops Chrome throttling a
    window it thinks is hidden; it stays a real headed browser, so the machine
    still needs a display.

    If a Chrome is already running on `profile_dir`, this launch hands the URL
    to it and exits, and the debug port never opens: check `port_in_use`
    first, and pass the returned process to `connect_over_cdp`, which then
    fails fast instead of waiting for a port that will not open.

    Raises `ChromeLaunchError` when the binary cannot be started. Its message
    leaves the path out: a wrong override would otherwise travel with it.
    """
    args = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if offscreen:
        args += ["--window-position=-32000,-32000", "--disable-backgrounding-occluded-windows"]
    args.append(url)
    logger.info("Launching Chrome (debug port %s, offscreen=%s)", port, offscreen)
    # Chrome gets its own process group so `terminate_chrome` can tear down the
    # whole tree (renderers, GPU, zygote): a new session (one killpg) on POSIX,
    # a new process group (taskkill /T) on Windows, which has no killpg.
    if is_windows():
        group = {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        }
    else:
        group = {"start_new_session": True}
    try:
        return subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **group
        )
    except OSError as exc:
        failure = type(exc).__name__
    # Raised outside the handler, so the OSError (which names the path it
    # could not run) is neither the cause nor the context of this error.
    raise ChromeLaunchError(
        f"Could not start the Chrome binary ({failure}): it is missing or not executable."
    )


def port_in_use(port: int) -> bool:
    """True if something is already listening on 127.0.0.1:`port`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def has_exited(process: Optional[subprocess.Popen]) -> bool:
    """Whether a process `launch_chrome` returned has ended (never raises)."""
    if process is None:
        return False
    try:
        return process.poll() is not None
    except Exception:
        return False


def connect_over_cdp(
    playwright,
    cdp_url: str,
    *,
    deadline: float,
    attempt_timeout_ms: int = CONNECT_ATTEMPT_TIMEOUT_MS,
    process: Optional[subprocess.Popen] = None,
):
    """Connect to a Chrome debug port, retrying until `deadline` (monotonic).

    A Chrome just launched needs a few seconds to open its port, so a refused
    connection is retried; each attempt runs under `attempt_timeout_ms`. At
    least one attempt is made, so a caller that already knows the port is up
    passes a `deadline` of now and gives the single attempt its whole budget.
    Raises `ChromeUnreachableError` when none succeeds.

    `process` is the Chrome that `launch_chrome` started, when the caller
    owns one. If an attempt fails and that process has already ended, the
    port will never open (the launch handed off to a Chrome already running
    on the profile, or Chrome crashed, or its window was closed), so
    `ChromeExitedError` is raised at once instead of retrying to the deadline.

    Neither error is chained to the Playwright one, whose text carries the
    CDP URL, a browser id and a call log: only its type name is kept.
    """
    failure = "no attempt"
    while True:
        try:
            return playwright.chromium.connect_over_cdp(cdp_url, timeout=attempt_timeout_ms)
        except Exception as exc:
            failure = type(exc).__name__
        if has_exited(process):
            raise ChromeExitedError("The launched Chrome is no longer running.")
        if time.monotonic() + _CONNECT_RETRY_PAUSE_S >= deadline:
            break
        time.sleep(_CONNECT_RETRY_PAUSE_S)
    raise ChromeUnreachableError(f"Could not connect to the Chrome debug port ({failure}).")


def find_page(browser, matches: Callable[[str], bool]):
    """The first open tab whose URL satisfies `matches`, or None.

    Reads only what Playwright already holds locally (`contexts`, `pages`,
    `url`), so it never waits on the browser.
    """
    for context in browser.contexts:
        for page in context.pages:
            try:
                if matches(page.url):
                    return page
            except Exception:
                continue
    return None


@dataclass(frozen=True)
class TargetSummary:
    """What Chrome itself lists as open: counts only, never a URL.

    `types` counts the targets by type ("page", "service_worker", ...),
    `matching_pages` the `page` targets whose URL satisfied the caller's
    test, and `other_web_pages` the `page` targets on some other http(s)
    URL. `matching_key` is an opaque digest of the matching URLs: it changes
    when a matching tab moves to another URL, and says nothing else.
    """

    types: dict[str, int]
    matching_pages: int
    other_web_pages: int = 0
    matching_key: str = ""


def _targets_endpoint(cdp_url: str) -> Optional[str]:
    """Chrome's `/json/list` URL for the debug port `cdp_url` points at."""
    try:
        parts = urlsplit(cdp_url)
    except ValueError:
        return None
    scheme = {"http": "http", "https": "https", "ws": "http", "wss": "https"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        return None
    return f"{scheme}://{parts.netloc}/json/list"


def _http_get_json(url: str, timeout_s: float):
    """GET `url` and decode its JSON body, ignoring any proxy configuration."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout_s) as response:
        return json.loads(response.read(_TARGET_LIST_MAX_BYTES))


def summarize_targets(
    cdp_url: str, matches: Callable[[str], bool], *, timeout_s: float = 2.0
) -> Optional[TargetSummary]:
    """Ask Chrome's own HTTP endpoint what is open; None when it does not answer.

    Independent of any Playwright connection, and ahead of it: Chrome lists a
    tab with its real URL as soon as it navigates, while a Playwright
    connection made during the load may never learn it. So this is where to
    look for "is the tab there, and on which site", before attaching at all.

    Only counts and an opaque digest leave this function: the URLs are tested
    with `matches` and dropped, never returned or logged. Bounded by
    `timeout_s`; never raises.
    """
    endpoint = _targets_endpoint(cdp_url)
    if endpoint is None:
        return None
    try:
        targets = _http_get_json(endpoint, timeout_s)
    except Exception:
        return None
    if not isinstance(targets, list):
        return None
    types: dict[str, int] = {}
    matching: list[str] = []
    elsewhere = 0
    for target in targets:
        if not isinstance(target, dict):
            continue
        kind = target.get("type")
        # Chrome's own short labels; anything else is not echoed.
        label = kind if isinstance(kind, str) and _TARGET_TYPE_RE.match(kind) else "other"
        types[label] = types.get(label, 0) + 1
        url = target.get("url")
        if label != "page" or not isinstance(url, str):
            continue
        try:
            matched = bool(matches(url))
        except Exception:
            matched = False
        if matched:
            matching.append(url)
        elif url.startswith(("http://", "https://")):
            elsewhere += 1
    digest = hashlib.sha256("\n".join(sorted(matching)).encode()).hexdigest() if matching else ""
    return TargetSummary(
        types=types,
        matching_pages=len(matching),
        other_web_pages=elsewhere,
        matching_key=digest,
    )


def terminate_chrome(proc: subprocess.Popen) -> None:
    """Stop a Chrome started by `launch_chrome`, including its child processes.

    Asks first (SIGTERM to the browser process alone, or taskkill without
    /F), which lets Chrome flush its profile, then forces. Never raises. A Chrome that has already exited is
    left alone, so calling this twice is safe: once the process is reaped its
    pid may belong to something else.
    """
    try:
        if proc.poll() is not None:
            return
    except Exception:
        logger.warning("Could not tell whether the launched Chrome is running", exc_info=True)
    if is_windows():
        _terminate_windows(proc)
    else:
        _terminate_posix(proc)


def _reaped(proc: subprocess.Popen, timeout_s: float) -> bool:
    """Wait up to `timeout_s` for `proc` to exit; True once it has."""
    try:
        proc.wait(timeout=timeout_s)
    except Exception:
        return False
    return True


def _terminate_posix(proc: subprocess.Popen) -> None:
    """Ask the browser process alone to quit, and only then touch its group.

    Chrome's network service is a child process that owns the cookie store
    and writes it on a timer. A signal to the whole group ends that child
    together with the browser, before the browser has told it to flush, and
    a session cookie just set or rotated can be lost: exactly what a
    profile-held session depends on. So the browser process gets SIGTERM by
    itself, which makes it shut its children down in order, and it is given
    time to exit. Only a browser that does not exit has its whole group
    signalled (renderer, GPU and zygote children would otherwise be
    orphaned): SIGTERM, then SIGKILL. The process is reaped after each step,
    so no zombie lingers and the debug port and profile lock are released.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None

    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        logger.warning("Error signalling the launched Chrome", exc_info=True)
    if _reaped(proc, _BROWSER_EXIT_WAIT_S):
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass
        except Exception:
            logger.warning("Error signalling the launched Chrome's process group", exc_info=True)
        if _reaped(proc, _GROUP_EXIT_WAIT_S):
            return
    logger.warning("The launched Chrome did not exit after SIGKILL")


def _taskkill(pid: int, force: bool) -> None:
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        subprocess.run(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False
        )
    except Exception:
        logger.warning("taskkill failed for the launched Chrome", exc_info=True)


def _terminate_windows(proc: subprocess.Popen) -> None:
    """Close Chrome's process tree with taskkill: politely, then /F.

    Windows has no process-group signals; `taskkill /T` walks the tree from
    the PID Chrome was launched with, so its children go too, and the process
    is reaped so the debug port and profile lock are released.
    """
    _taskkill(proc.pid, force=False)
    try:
        proc.wait(timeout=10)
        return
    except Exception:
        pass
    _taskkill(proc.pid, force=True)
    try:
        proc.wait(timeout=5)
    except Exception:
        logger.warning("The launched Chrome did not exit after taskkill /F")
