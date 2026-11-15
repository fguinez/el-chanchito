"""Interactive sign-ins started from the dashboard (the `make *-login` flows).

Lets the control server start the human step a `needs_sign_in` run waits
for, in the form the institution needs:

  * `code`: the service signs in with the configured credentials and waits for
    the one-time code the institution e-mailed, which the dashboard posts back
    (Fintual).
  * `browser`: a visible Chrome window opens on the machine running the
    service and the human signs in there; nothing is typed for them
    (Mercado Pago).
  * `refresh`: the institution's normal run already opens a visible Chrome
    when its sign-in needs a human (Tarjeta Lider Bci in managed mode), so the
    sign-in is that run, started now while someone is at the computer.

`code` and `browser` sign-ins run on a thread of their own, never on the
scheduler's event loop, and claim the institution in `RunGuard` first, so a
sign-in and a scraper run of the same institution never overlap. A successful
one asks for a refresh of that institution, which is what clears the
dashboard's notice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger("scraper-service.login")

CODE_PATTERN = re.compile(r"^\d{6}$")
# How long a `code` sign-in waits for the code before giving up.
CODE_TIMEOUT_SECONDS = 10 * 60


class RunGuard:
    """Which institutions are busy: a scraper run or a sign-in holds each.

    Claimed from the event loop (runs) and from sign-in threads, so it locks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: dict[str, str] = {}

    def claim(self, slug: str, holder: str) -> bool:
        """Claim `slug` for `holder` ("run" or "login"); False if already held."""
        with self._lock:
            if slug in self._busy:
                return False
            self._busy[slug] = holder
            return True

    def release(self, slug: str) -> None:
        with self._lock:
            self._busy.pop(slug, None)

    def holder(self, slug: str) -> str | None:
        with self._lock:
            return self._busy.get(slug)


class LoginError(RuntimeError):
    """A sign-in that did not complete; its message is safe to show."""


@dataclass
class LoginProvider:
    """How one institution signs in.

    `run` does the sign-in and returns once the session is saved, raising on
    failure. A `code` provider gets a callable that blocks until the dashboard
    posts the code (or raises `LoginError` on timeout); the others get None.
    A `refresh` provider has no `run`: its sign-in is the scraper run itself.
    """

    method: str
    run: Callable[[Callable[[], str] | None], None] | None = None


@dataclass
class LoginSession:
    slug: str
    method: str
    state: str = "running"  # running | awaiting_code | succeeded | failed
    message: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    _code: str | None = None
    _code_ready: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {
            "institution": self.slug,
            "method": self.method,
            "state": self.state,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class InteractiveLogins:
    """Starts sign-ins and tracks the latest one per institution.

    `trigger` asks the scheduler to run an institution now (False when it has
    no job). It starts `refresh` sign-ins and follows every successful one.
    """

    def __init__(
        self,
        providers: dict[str, LoginProvider],
        trigger: Callable[[str], bool],
        guard: RunGuard,
        code_timeout: float = CODE_TIMEOUT_SECONDS,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._providers = providers
        self._trigger = trigger
        self._guard = guard
        self._code_timeout = code_timeout
        self._spawn = spawn or _spawn_daemon
        self._lock = threading.Lock()
        self._sessions: dict[str, LoginSession] = {}

    def supported(self) -> list[dict]:
        return [{"institution": s, "method": p.method} for s, p in sorted(self._providers.items())]

    def status(self, slug: str) -> tuple[int, dict]:
        if slug not in self._providers:
            return 404, {"error": f"no interactive sign-in for {slug}"}
        with self._lock:
            session = self._sessions.get(slug)
        if session is None:
            return 200, {
                "institution": slug,
                "method": self._providers[slug].method,
                "state": "idle",
            }
        return 200, session.public()

    def start(self, slug: str) -> tuple[int, dict]:
        provider = self._providers.get(slug)
        if provider is None:
            return 404, {"error": f"no interactive sign-in for {slug}"}
        if provider.run is None:
            if self._guard.holder(slug):
                return 409, {"error": f"{slug} is already running"}
            if not self._trigger(slug):
                return 404, {"error": f"unknown scraper: {slug}"}
            session = LoginSession(slug, provider.method, state="succeeded")
            session.message = "refresh started"
            session.finished_at = session.started_at
            with self._lock:
                self._sessions[slug] = session
            return 202, session.public()

        with self._lock:
            current = self._sessions.get(slug)
            if current is not None and current.state in ("running", "awaiting_code"):
                return 409, {"error": f"a sign-in to {slug} is already in progress"}
            if not self._guard.claim(slug, "login"):
                return 409, {"error": f"{slug} is running; try again when it finishes"}
            session = LoginSession(slug, provider.method)
            self._sessions[slug] = session
        logger.info("Interactive sign-in started for %s (%s)", slug, provider.method)
        self._spawn(lambda: self._run(session, provider))
        return 202, session.public()

    def submit_code(self, slug: str, code: object) -> tuple[int, dict]:
        if slug not in self._providers:
            return 404, {"error": f"no interactive sign-in for {slug}"}
        if not isinstance(code, str) or not CODE_PATTERN.match(code.strip()):
            return 400, {"error": "code must be 6 digits", "field": "code"}
        with self._lock:
            session = self._sessions.get(slug)
            if session is None or session.state != "awaiting_code":
                return 409, {"error": f"{slug} is not waiting for a code"}
            session._code = code.strip()
            session.state = "running"
            session._code_ready.set()
        return 202, session.public()

    def _await_code(self, session: LoginSession) -> str:
        with self._lock:
            session.state = "awaiting_code"
        if not session._code_ready.wait(self._code_timeout):
            raise LoginError("No code arrived in time; start the sign-in again.")
        assert session._code is not None
        return session._code

    def _run(self, session: LoginSession, provider: LoginProvider) -> None:
        assert provider.run is not None
        await_code = (lambda: self._await_code(session)) if provider.method == "code" else None
        try:
            provider.run(await_code)
        except Exception as exc:
            # Only our own messages are safe to show: library errors can carry
            # URLs or account details.
            message = str(exc) if isinstance(exc, LoginError) else _safe_message(exc)
            logger.warning("Interactive sign-in for %s failed: %s", session.slug, message)
            self._finish(session, "failed", message)
            return
        finally:
            self._guard.release(session.slug)
        self._finish(session, "succeeded", None)
        logger.info("Interactive sign-in for %s succeeded; refreshing it", session.slug)
        self._trigger(session.slug)

    def _finish(self, session: LoginSession, state: str, message: str | None) -> None:
        with self._lock:
            session.state = state
            session.message = message
            session.finished_at = datetime.now(UTC).isoformat()


def _safe_message(exc: Exception) -> str:
    """A failure message for the dashboard: the scrapers' own sign-in errors
    are written to be shown (they are what a needs_sign_in run displays);
    anything else is reduced to its type."""
    from scrapers.base import SignInRequired

    if isinstance(exc, SignInRequired):
        return str(exc)
    return f"Sign-in failed ({type(exc).__name__})."


def _spawn_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="interactive-login", daemon=True).start()


def _fintual_provider() -> LoginProvider:
    def run(await_code: Callable[[], str] | None) -> None:
        from scrapers.institutions.fintual import FintualScraper

        assert await_code is not None
        # A fresh instance: the scheduled one's state belongs to its runs.
        # `login` clears the `.login-failed` marker once the session is saved.
        asyncio.run(FintualScraper().login(await_code))

    return LoginProvider("code", run)


def _mercadopago_provider() -> LoginProvider:
    def run(_: Callable[[], str] | None) -> None:
        from scrapers.backends.mercadopago_web import save_login_session

        # Opens a visible Chrome on the dedicated profile and waits (bounded)
        # for the human; clears the sign-in marker on success.
        save_login_session()

    return LoginProvider("browser", run)


def build_providers(scraper_keys: set[str]) -> dict[str, LoginProvider]:
    """The sign-ins the dashboard can start, among the enabled scrapers.

    Fintual needs its password configured (a token-only setup has no sign-in
    to run). Lider only in managed mode: with `LIDER_BCI_CDP_URL` set, runs
    drive a Chrome someone else started, so a run cannot open a sign-in
    window.
    """
    providers: dict[str, LoginProvider] = {}
    if "fintual" in scraper_keys and os.environ.get("FINTUAL_PASSWORD"):
        providers["fintual"] = _fintual_provider()
    if "mercadopago" in scraper_keys:
        providers["mercadopago"] = _mercadopago_provider()
    if "bci_lider" in scraper_keys and not os.environ.get("LIDER_BCI_CDP_URL"):
        providers["bci_lider"] = LoginProvider("refresh")
    return providers
