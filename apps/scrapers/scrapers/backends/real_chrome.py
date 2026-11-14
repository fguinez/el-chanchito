"""Generic plumbing for driving a genuine Chrome over CDP (acquisition rung 7).

Some portals only work in a real browser: a bot check that a
Playwright-launched Chromium fails, or a session bound to the browser a human
signed in with. For those the scraper starts an ordinary Chrome process (the
user's own binary, a dedicated persistent profile, a debug port) and Playwright
only attaches to it over CDP.

This module holds the parts of that which are the same for every institution:
finding the Chrome binary, launching it on a debug port, checking the port,
connecting, finding a tab, asking Chrome what it has open and tearing the
process tree down. A managed launch is also recorded in a pidfile inside its
profile, so a Chrome orphaned by a killed scraper service (its `finally` never
ran) is found and stopped by the next run instead of holding the port and the
profile lock forever (`record_managed_launch`, `reclaim_orphaned_chrome`).
Nothing here
knows an institution: ports, profile directories, URLs and env var names are
arguments. Its users are Mercado Pago (`backends/mercadopago_web.py`) and
Tarjeta Lider Bci (`backends/bci_lider_web.py`).

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
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union
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
# Orderly close through CDP `Browser.close`: the connect gets its own short
# timeout and the whole attempt (driver start, connect, send) a bound well
# above it, so an abandoned attempt cannot connect late and overlap the
# SIGTERM; then the browser gets this long to exit by itself.
_CDP_CLOSE_CONNECT_TIMEOUT_MS = 5_000
_CDP_CLOSE_ATTEMPT_S = 10
_CDP_CLOSE_EXIT_WAIT_S = 15
# The longest `terminate_chrome` can take (CDP close, then the POSIX signals).
TERMINATE_WORST_CASE_S = (
    _CDP_CLOSE_ATTEMPT_S + _CDP_CLOSE_EXIT_WAIT_S + _BROWSER_EXIT_WAIT_S + 2 * _GROUP_EXIT_WAIT_S
)
# How often an orphan's pid is polled while it is asked to exit.
_ORPHAN_POLL_S = 0.1
# The state reader may spawn `ps` (macOS), so it is consulted every Nth poll.
_STATUS_EVERY_N_POLLS = 5
# Inside the profile dir (gitignored with it), under a name Chrome never uses.
MANAGED_PIDFILE_NAME = "el-chanchito-managed-chrome.json"
_PS_TIMEOUT_S = 5
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


def terminate_chrome(proc: subprocess.Popen, cdp_url: Optional[str] = None) -> None:
    """Stop a Chrome started by `launch_chrome`, including its child processes.

    With `cdp_url` (its debug endpoint) it is first asked to quit through CDP
    `Browser.close`, the same orderly shutdown as closing its window, which
    writes the cookie store to disk; a SIGTERM was seen live to cut that
    write short. Then, if it is still running: SIGTERM to the browser process
    alone (or taskkill without /F), then forced. Never raises. A Chrome that
    has already exited is left alone, so calling this twice is safe: once the
    process is reaped its pid may belong to something else.
    """
    try:
        if proc.poll() is not None:
            return
    except Exception:
        logger.warning("Could not tell whether the launched Chrome is running", exc_info=True)
    if cdp_url is not None and close_via_cdp(proc, cdp_url):
        return
    if is_windows():
        _terminate_windows(proc)
    else:
        _terminate_posix(proc)


def _send_browser_close(
    cdp_url: str, connect_timeout_ms: int = _CDP_CLOSE_CONNECT_TIMEOUT_MS
) -> None:
    """Send CDP `Browser.close` to the Chrome at `cdp_url` (raises on failure).

    The connection drops as Chrome quits, so an error after the command was
    sent is expected and ignored; only failing to get that far raises.
    """
    from playwright.sync_api import sync_playwright  # lazy: keeps tests browser-free

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=connect_timeout_ms)
        session = browser.new_browser_cdp_session()
        try:
            session.send("Browser.close")
        except Exception as exc:
            logger.info("CDP Browser.close: connection ended (%s)", type(exc).__name__)


def close_via_cdp(
    proc: subprocess.Popen, cdp_url: str, exit_wait_s: float = _CDP_CLOSE_EXIT_WAIT_S
) -> bool:
    """Ask a Chrome we launched to quit through CDP; True once it has exited.

    The CDP call runs in a daemon thread joined for at most
    `_CDP_CLOSE_ATTEMPT_S`, so a frozen browser cannot hold the caller (a
    sync Playwright call has no timeout of its own), and so it never runs on
    a thread with an asyncio loop. Failures are logged by type only.
    """
    if has_exited(proc):
        return True
    failure: list[str] = []

    def send() -> None:
        try:
            _send_browser_close(cdp_url)
        except Exception as exc:
            failure.append(type(exc).__name__)

    worker = threading.Thread(target=send, name="chrome-cdp-close", daemon=True)
    worker.start()
    worker.join(_CDP_CLOSE_ATTEMPT_S)
    if worker.is_alive():
        logger.warning("CDP Browser.close did not return in time; falling back to signals")
        return has_exited(proc)
    if failure:
        logger.warning("CDP Browser.close failed (%s); falling back to signals", failure[0])
        return has_exited(proc)
    if _reaped(proc, exit_wait_s):
        return True
    logger.warning("Chrome did not exit after CDP Browser.close; falling back to signals")
    return False


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


# What a command-line reader returns: the argv list (Linux /proc), one
# space-joined string (`ps` on macOS), or None when it cannot be read.
Cmdline = Optional[Union[Sequence[str], str]]


def managed_pidfile(profile_dir: Path) -> Path:
    """Where the record of the Chrome launched on `profile_dir` lives."""
    return Path(profile_dir) / MANAGED_PIDFILE_NAME


def record_managed_launch(proc: subprocess.Popen, *, port: int, profile_dir: Path) -> None:
    """Write down the Chrome `launch_chrome` just started. Never raises.

    The record (pid, port, owner pid, resolved profile, launch time) is written
    atomically inside the profile, so a later run can tell whether a Chrome
    still holding the port is this one, orphaned.
    """
    try:
        pidfile = managed_pidfile(profile_dir)
        pidfile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {
            "pid": int(proc.pid),
            "port": int(port),
            "owner_pid": os.getpid(),
            "profile_dir": str(Path(profile_dir).resolve()),
            "launched_at": time.time(),
        }
        tmp = pidfile.with_name(f"{pidfile.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, pidfile)
    except Exception as exc:
        logger.warning(
            "Could not record the launched Chrome (%s); an orphan of this run "
            "would not be reclaimed",
            type(exc).__name__,
        )


def forget_managed_launch(profile_dir: Path, pid: int) -> None:
    """Drop the record of `pid`, only if the pidfile still names it. Never raises."""
    try:
        pidfile = managed_pidfile(profile_dir)
        record = _read_record(pidfile)
        if record is not None and record["pid"] == pid:
            pidfile.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not clear the launched Chrome's record (%s)", type(exc).__name__)


def _read_record(pidfile: Path) -> Optional[dict]:
    """The pidfile's record; None when missing. Raises ValueError when corrupt."""
    try:
        raw = pidfile.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError("not an object")
    pid, port, profile = record.get("pid"), record.get("port"), record.get("profile_dir")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        raise ValueError("bad pid")
    if not (isinstance(port, int) and not isinstance(port, bool)):
        raise ValueError("bad port")
    if not isinstance(profile, str):
        raise ValueError("bad profile")
    return record


def _remove(pidfile: Path) -> None:
    try:
        pidfile.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove the managed Chrome record (%s)", type(exc).__name__)


def read_proc_cmdline(pid: int, proc_root: Path = Path("/proc")) -> Cmdline:
    """Linux: the process's argv from /proc, NUL-separated; None if unreadable.

    Chrome rewrites its process title into one space-joined string, so a
    single field comes back as a `ps`-style line, matched like one.
    """
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    parts = [p.decode("utf-8", "surrogateescape") for p in raw.rstrip(b"\0").split(b"\0")]
    if not parts[0]:
        return None
    return parts if len(parts) > 1 else parts[0]


def _run_ps(pid: int, fields: str) -> Optional[str]:
    """One `ps` column set for `pid`, stripped; None if `ps` fails or knows no such pid."""
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", fields, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except Exception:
        return None
    line = result.stdout.strip() if result.returncode == 0 else ""
    return line or None


def read_ps_command(pid: int) -> Cmdline:
    """macOS: the process's full command line from `ps`; None if unreadable."""
    return _run_ps(pid, "command=")


@dataclass(frozen=True)
class ProcessStatus:
    """A process's parent pid and its state letters ("S", "R", "Z", ...)."""

    ppid: int
    state: str


def read_proc_status(pid: int, proc_root: Path = Path("/proc")) -> Optional[ProcessStatus]:
    """Linux: state and parent from /proc/<pid>/stat; None if unreadable."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        # The command name sits in parentheses and may hold spaces or ')'.
        fields = raw[raw.rindex(")") + 1 :].split()
        return ProcessStatus(ppid=int(fields[1]), state=fields[0])
    except (OSError, ValueError, IndexError):
        return None


def read_ps_status(pid: int) -> Optional[ProcessStatus]:
    """macOS: state and parent from `ps`; None if unreadable."""
    line = _run_ps(pid, "ppid=,stat=")
    try:
        ppid, state = line.split()[:2]
        return ProcessStatus(ppid=int(ppid), state=state)
    except (AttributeError, ValueError):
        return None


def _cmdline_reader(platform: str) -> Optional[Callable[[int], Cmdline]]:
    if platform.startswith("linux"):
        return read_proc_cmdline
    if platform == "darwin":
        return read_ps_command
    return None


def _status_reader(platform: str) -> Optional[Callable[[int], Optional[ProcessStatus]]]:
    if platform.startswith("linux"):
        return read_proc_status
    if platform == "darwin":
        return read_ps_status
    return None


def _has_whole_arg(cmdline: Union[Sequence[str], str], arg: str) -> bool:
    """Whether `arg` is one whole argument of `cmdline`, never a prefix of one.

    An argv list is compared element by element. A `ps` line has lost the
    argument boundaries, so `arg` must follow a space and be followed by the
    next option (" -") or the line's end; a path with a space in it still
    matches, and `--x=/a/b` does not match `--x=/a/bc` or `--x=/a/b c`.
    """
    if not isinstance(cmdline, str):
        return arg in cmdline
    needle = f" {arg}"
    start = cmdline.find(needle)
    while start != -1:
        end = start + len(needle)
        if end == len(cmdline) or cmdline.startswith(" -", end):
            return True
        start = cmdline.find(needle, start + 1)
    return False


StatusReader = Optional[Callable[[int], Optional[ProcessStatus]]]


def _pid_state(pid: int, read_status: StatusReader = None) -> str:
    """'gone', 'alive' (ours to signal) or 'foreign' (exists, not signallable).

    A zombie counts as gone: it has exited, only its parent has not reaped
    it yet (it is reaped here when that parent is us).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except OSError:
        return "foreign"
    if _reaped_here(pid):
        return "gone"
    status = read_status(pid) if read_status is not None else None
    if status is not None and status.state.startswith("Z"):
        return "gone"
    return "alive"


def _reaped_here(pid: int) -> bool:
    """Reap `pid` if it is our own exited child; True when it was."""
    no_hang = getattr(os, "WNOHANG", None)
    if no_hang is None:
        return False
    try:
        reaped, _ = os.waitpid(pid, no_hang)
    except (ChildProcessError, OSError):
        return False
    return reaped == pid


def _gone_within(pid: int, timeout_s: float, read_status: StatusReader = None) -> bool:
    """Poll `pid` for up to `timeout_s`; True once it is no longer ours."""
    deadline = time.monotonic() + timeout_s
    tick = 0
    while True:
        reader = read_status if tick % _STATUS_EVERY_N_POLLS == 0 else None
        tick += 1
        if _pid_state(pid, reader) != "alive":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_ORPHAN_POLL_S)


def _terminate_orphan(pid: int, read_status: StatusReader = None) -> bool:
    """`_terminate_posix` for a pid we cannot `wait()` on: browser, then group."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        logger.warning("Error signalling the orphaned Chrome", exc_info=True)
    if _gone_within(pid, _BROWSER_EXIT_WAIT_S, read_status):
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        except Exception:
            logger.warning("Error signalling the orphaned Chrome's process group", exc_info=True)
        if _gone_within(pid, _GROUP_EXIT_WAIT_S, read_status):
            return True
    logger.warning("The orphaned Chrome (pid %s) did not exit after SIGKILL", pid)
    return False


def _owner_still_holds(pid: int, record: dict, read_status: StatusReader) -> bool:
    """Whether the run that launched `pid` is alive and still its parent.

    Then it is not an orphan (a sign-in window still open, or our own drive
    still running) and must not be killed. A parent that cannot be read
    counts as still holding it. A record without an owner proves nothing.
    """
    owner = record.get("owner_pid")
    if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
        return False
    if owner != os.getpid() and _pid_state(owner) == "gone":
        return False
    status = read_status(pid) if read_status is not None else None
    return status is None or status.ppid == owner


def reclaim_orphaned_chrome(
    *,
    port: int,
    profile_dir: Path,
    platform: str = sys.platform,
    read_cmdline: Optional[Callable[[int], Cmdline]] = None,
    read_status: StatusReader = None,
) -> bool:
    """Stop a managed Chrome a killed run left behind on `profile_dir`.

    Reads the pidfile `record_managed_launch` wrote. A missing record does
    nothing; a corrupt one, or one whose process has ended (or is a zombie),
    is removed. A Chrome whose launching run is alive and still its parent
    is not an orphan: it is left running and its record is kept, and the
    caller's busy-port check reports it. Otherwise a live process is terminated only
    when it is verifiably that Chrome: the record names this profile, its
    command line carries both exact `--remote-debugging-port` (the recorded
    port, which may differ from `port` after a config change) and
    `--user-data-dir` arguments `launch_chrome` passes, and it still leads
    its own process group (pgid == pid, as its new session made it).
    Any of those checks failing kills nothing and drops the stale record. A
    command line that cannot be read (a slow or failing `ps`) kills nothing
    and keeps the record, so the next run checks again.

    Termination mirrors `terminate_chrome` and can take up to about 20 s.
    Returns True when an orphan was stopped. Skipped on Windows (the record
    stays). Never raises.
    """
    if platform.startswith(("win", "cygwin")):
        return False
    pidfile = managed_pidfile(profile_dir)
    if read_status is None:
        read_status = _status_reader(platform)
    try:
        try:
            record = _read_record(pidfile)
        except ValueError:
            logger.warning("The managed Chrome record is corrupt; removing it")
            _remove(pidfile)
            return False
        if record is None:
            return False
        pid = record["pid"]
        state = _pid_state(pid, read_status)
        if state == "gone":
            _remove(pidfile)
            return False
        if state == "alive" and _owner_still_holds(pid, record, read_status):
            logger.info("The managed Chrome (pid %s) still has its launching run; leaving it", pid)
            return False
        verdict = (
            _is_managed_chrome(
                pid, record, profile_dir=profile_dir, platform=platform, read_cmdline=read_cmdline
            )
            if state == "alive"
            else False
        )
        if verdict is None:
            logger.warning(
                "Could not read the command line of pid %s; keeping the managed Chrome record",
                pid,
            )
            return False
        if not verdict:
            logger.warning(
                "pid %s from the managed Chrome record is not that Chrome; "
                "leaving it alone and dropping the record",
                pid,
            )
            _remove(pidfile)
            return False
        logger.warning("Stopping an orphaned managed Chrome (pid %s)", pid)
        if not _terminate_orphan(pid, read_status):
            return False
        _remove(pidfile)
        logger.info(
            "Reclaimed an orphaned managed Chrome (pid %s, debug port %s)", pid, record["port"]
        )
        return True
    except Exception:
        logger.warning("Could not reclaim an orphaned Chrome", exc_info=True)
        return False


def _is_managed_chrome(
    pid: int,
    record: dict,
    *,
    profile_dir: Path,
    platform: str,
    read_cmdline: Optional[Callable[[int], Cmdline]],
) -> Optional[bool]:
    """Whether `pid` is the recorded Chrome; None when its command line cannot be read."""
    if record["profile_dir"] != str(Path(profile_dir).resolve()):
        return False
    reader = read_cmdline if read_cmdline is not None else _cmdline_reader(platform)
    cmdline = reader(pid) if reader is not None else None
    if cmdline is None:
        return None
    if not cmdline:
        return False
    expected = (
        f"--remote-debugging-port={record['port']}",
        f"--user-data-dir={profile_dir}",
    )
    if not all(_has_whole_arg(cmdline, arg) for arg in expected):
        return False
    try:
        return os.getpgid(pid) == pid
    except Exception:
        return False
