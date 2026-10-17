"""Racional scraper: portfolio values and contributions over plain HTTP.

Racional (racional.cl, an investment app) has no API for individuals, Chile's
open finance system is not in force yet, and `fintself` doesn't cover it. Its
web app (app.racional.cl) is a single-page app over Firebase: Firebase Auth
for the session and Firestore for the data. Both have documented REST APIs,
so the scraper reproduces the web app's own sign-in and reads the same
Firestore documents with httpx; no browser is involved:

    POST {functions}/authMfaStart            -> {email, deviceId, method: "email"}
                                                answers {showMfaModal, canSkipMfa};
                                                when shown, a 6-digit code is e-mailed
    POST {functions}/authMfaVerify           -> {email, code, deviceId, skipMfa: false}
    POST identitytoolkit accounts:signInWithPassword -> idToken + refreshToken
    POST securetoken /v1/token (refresh_token grant)  -> a fresh idToken per run
    POST firestore documents:runQuery        -> portfolios where userId == uid

The refresh token is cached (`.racional_session.json`, 0600, gitignored), so a
scheduled run never signs in: it mints an ID token and runs one query. A full
sign-in happens only through `make racional-login` (prompts for the e-mailed
code) or, when RACIONAL_EMAIL is the IMAP mailbox the service already reads,
automatically with the code read from that mailbox. A failed automatic sign-in
turns automatic sign-ins off until `make racional-login` succeeds, so a wrong
password can't be retried every cycle into a lockout.

The Firebase web API key is public (the web app serves it to every visitor).
It isn't committed here: it is read from the app's public bundle at sign-in
and cached with the session, or taken from RACIONAL_FIREBASE_API_KEY. It
travels in the `X-Goog-Api-Key` header rather than the `?key=` query string
the Firebase SDK uses, so it stays out of URLs (httpx logs each one at INFO).

Accounts with Racional's "seguridad de dispositivo" (a `deviceSecurityEnabled`
claim on the ID token) are sent to Racional's new app by the web app, which
signs them out; the scraper respects that and raises
`RacionalDeviceSecurityError` instead of reading around it. The new app
(beta.racional.cl) reads its data through per-build server functions with
opaque ids, which is no contract to build on.

`scrape_products()` emits one `investment` product per portfolio, keyed by its
Firestore document id (`external_ref`): CLP for Racional Portafolios, USD for
the Racional Stocks portfolio (`isDrivewealth`). `nav` is the portfolio value,
`deposited` the contributions (`accumulatedInputAmount`, else value minus
all-time profit), plus profit and the daily, 30-day and year-to-date returns.
`scrape_transactions()` returns nothing: movements belong to one portfolio
each, and the writer files transactions only on a singleton product, so they
need the ingest contract to carry a product reference first.

Built from the public web bundle only; nothing here was verified against a
signed-in account yet. To verify live: the e-mail OTP sender and subject, that
`accumulatedInputAmount` sits on portfolio documents (not only on the user's),
that the Stocks value includes or excludes the uninvested USD wallet, and that
Firestore accepts REST queries without an App Check token.
"""

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import httpx

from product_model import InvestmentMetrics

from scrapers.backends.email import fetch_latest_code
from scrapers.base import (
    BaseScraper,
    ProductScrapeResult,
    ScrapedProduct,
    ScrapedTransaction,
)

logger = logging.getLogger(__name__)

WEB_ORIGIN = "https://app.racional.cl"
LOGIN_PAGE_URL = f"{WEB_ORIGIN}/login"
FIREBASE_PROJECT = "racional-prod"
FUNCTIONS_BASE = f"https://us-central1-{FIREBASE_PROJECT}.cloudfunctions.net"
MFA_START_URL = f"{FUNCTIONS_BASE}/authMfaStart"
MFA_VERIFY_URL = f"{FUNCTIONS_BASE}/authMfaVerify"
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
RUN_QUERY_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
    "/databases/(default)/documents:runQuery"
)

# A desktop Chrome UA plus the web app's Origin/Referer: what its own calls carry.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Google APIs take an API key in this header as well as in `?key=`. Not yet
# verified live against identitytoolkit/securetoken; if Google ignores it,
# the fallback is the `key` query param (and the key then shows in httpx logs).
API_KEY_HEADER = "X-Goog-Api-Key"

PRODUCT_KIND = "investment"
# Racional has few portfolios per user; hitting this cap means something changed.
MAX_PORTFOLIOS = 100

# The web app asks for an e-mailed code; the e-mail's sender and subject are
# not verified live, so both filters are deliberately loose.
OTP_SENDERS = ["racional"]
OTP_SUBJECTS = ["codigo", "verifica"]
OTP_POLLS = 15
OTP_POLL_SECONDS = 4.0

# Firebase answers these when a refresh token can no longer mint ID tokens.
_DEAD_SESSION_ERRORS = frozenset(
    {"TOKEN_EXPIRED", "INVALID_REFRESH_TOKEN", "USER_DISABLED", "USER_NOT_FOUND"}
)
_BAD_CREDENTIALS_ERRORS = frozenset(
    {"INVALID_LOGIN_CREDENTIALS", "INVALID_PASSWORD", "EMAIL_NOT_FOUND", "INVALID_EMAIL"}
)

# apps/scrapers/, resolved from this file so cwd doesn't matter.
_APP_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_SESSION_FILE = _APP_DIR / ".racional_session.json"

_CENT = Decimal("0.01")
_MAIN_BUNDLE = re.compile(r'src="(main-es2015\.[0-9a-f]+\.js)"')
# Anchored on the project's authDomain so another Firebase config can't match.
_API_KEY = re.compile(
    r'apiKey:"([^"]+)",authDomain:"' + re.escape(FIREBASE_PROJECT) + r'\.firebaseapp\.com"'
)


class RacionalSessionError(RuntimeError):
    """No usable session and no unattended way to get one; the message names the fix."""


class RacionalLoginError(RacionalSessionError):
    """Racional or Firebase refused a sign-in step; never retried automatically."""


class RacionalDeviceSecurityError(RacionalSessionError):
    """The account has device security on, which only Racional's new app supports."""


# -- pure parsers ---------------------------------------------------------------


def _decode_value(value: Any) -> Any:
    """One Firestore REST typed value as plain Python.

    Firestore wraps every value in a one-key object naming its type;
    `integerValue` travels as a string. Unknown wrappers decode to None.
    """
    if not isinstance(value, dict) or len(value) != 1:
        return None
    (tag, raw), = value.items()
    if tag == "nullValue":
        return None
    if tag == "booleanValue":
        return bool(raw)
    if tag == "integerValue":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if tag == "doubleValue":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if tag in ("stringValue", "timestampValue", "referenceValue", "bytesValue"):
        return raw if isinstance(raw, str) else None
    if tag == "mapValue":
        return _decode_fields((raw or {}).get("fields") or {})
    if tag == "arrayValue":
        return [_decode_value(v) for v in (raw or {}).get("values") or []]
    return None


def _decode_fields(fields: dict) -> dict:
    """A Firestore document's `fields` object as a plain dict."""
    return {key: _decode_value(value) for key, value in fields.items()}


def _documents(payload: Any) -> list[tuple[str, dict]]:
    """(document id, decoded fields) for each document in a runQuery answer.

    The answer is a JSON array; entries without a `document` (a bare
    `readTime`, sent when nothing matched) are not results.
    """
    if not isinstance(payload, list):
        raise RuntimeError("Racional sent an unexpected payload reading portfolios")
    documents = []
    for entry in payload:
        document = entry.get("document") if isinstance(entry, dict) else None
        if not isinstance(document, dict):
            continue
        name = document.get("name")
        if not isinstance(name, str) or "/" not in name:
            continue
        documents.append((name.rsplit("/", 1)[1], _decode_fields(document.get("fields") or {})))
    return documents


def _decimal(raw: Any) -> Decimal | None:
    """A finite JSON number as an exact Decimal, else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _round(value: Decimal, currency: str) -> float:
    """Whole CLP or USD cents, half up (never Python's banker's `round`)."""
    if currency == "CLP":
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _percent(raw: Any) -> float | None:
    """A Racional return (a fraction: 0.042) as a percent number (4.2)."""
    value = _decimal(raw)
    if value is None:
        return None
    return float((value * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _parse_portfolio(doc_id: str, fields: dict) -> tuple[ScrapedProduct | None, str | None]:
    """One portfolio document as an investment product, else (None, skip reason).

    A skip reason of None means "nothing to report" (a Stocks portfolio that
    never held anything); a string is unexpected and becomes a run warning.
    Archived portfolios are still emitted: one emptied and archived between
    two runs must drop to zero rather than keep its last value.
    """
    evolution = fields.get("lastHistoricEvolution")
    is_stocks = fields.get("isDrivewealth") is True
    if is_stocks:
        currency = "USD"
        figures = evolution.get("portfolioEvolutionUSD") if isinstance(evolution, dict) else None
        if not isinstance(figures, dict):
            # The web app shows a Stocks portfolio without USD figures as empty;
            # one whose CLP value says otherwise is worth a warning.
            clp_total = _decimal(evolution.get("totalAmount")) if isinstance(evolution, dict) else None
            return None, ("without USD figures" if clp_total else None)
    else:
        currency = "CLP"
        figures = evolution
        if not isinstance(figures, dict):
            return None, "without figures"

    total = _decimal(figures.get("totalAmount"))
    if total is None:
        return None, "without a readable value"
    profit = _decimal(figures.get("allTimeProfitAmount"))
    deposited = _decimal(figures.get("accumulatedInputAmount"))
    if deposited is None and profit is not None:
        # Racional's all-time profit is value minus contributions.
        deposited = total - profit

    name = fields.get("portfolioName")
    if not isinstance(name, str) or not name.strip():
        name = "Racional Stocks" if is_stocks else "Portafolio Racional"

    return (
        ScrapedProduct(
            institution="racional",
            kind=PRODUCT_KIND,
            currency=currency,
            external_ref=doc_id,
            name=name.strip(),
            metrics=InvestmentMetrics(
                nav=_round(total, currency),
                deposited=None if deposited is None else _round(deposited, currency),
                profit=None if profit is None else _round(profit, currency),
                var_daily_pct=_percent(figures.get("dailyProfitPercentage")),
                # Racional's "month" is "Retorno últimos 30 días" in the web app.
                var_30d_pct=_percent(figures.get("monthProfitPercentage")),
                var_ytd_pct=_percent(figures.get("ytdProfitPercentage")),
            ),
        ),
        None,
    )


def _token_claims(id_token: str) -> dict:
    """The claims of a Firebase ID token, unverified (Firebase issued it to us)."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _find_main_bundle(html: str) -> str | None:
    """The file name of the web app's main ES2015 bundle in its index page."""
    match = _MAIN_BUNDLE.search(html)
    return match.group(1) if match else None


def _find_firebase_api_key(js: str) -> str | None:
    """The public Firebase web API key in the web app's bundle."""
    match = _API_KEY.search(js)
    return match.group(1) if match else None


def _firebase_error(resp: httpx.Response) -> str:
    """Firebase's error code ("INVALID_REFRESH_TOKEN", ...) without any detail."""
    try:
        message = resp.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        return ""
    # Codes may carry a free-text suffix ("TOO_MANY_ATTEMPTS_TRY_LATER : ...").
    return str(message).split(":", 1)[0].strip().upper()


def _json_dict(resp: httpx.Response) -> dict:
    """A response's JSON object, or {} when the body is not one."""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_api_key_error(resp: httpx.Response) -> bool:
    """Google's answer to an invalid or restricted API key (400 or 403)."""
    return resp.status_code in (400, 403) and "API KEY" in _firebase_error(resp)


def _failure_reason(error: Exception) -> str:
    """What to remember about a failed automatic sign-in: our own message, or the type.

    Our exception messages are written to carry no secrets; anything else
    (an httpx error names the URL, say) is reduced to its type.
    """
    if isinstance(error, RuntimeError):
        return str(error)
    return f"an unexpected {type(error).__name__}."


# -- scraper --------------------------------------------------------------------


class RacionalScraper(BaseScraper):
    method = "http_api"
    institution = "racional"

    def __init__(self) -> None:
        self.email = os.environ["RACIONAL_EMAIL"].strip()
        self.password = os.environ.get("RACIONAL_PASSWORD", "")
        self.session_file = Path(
            os.environ.get("RACIONAL_SESSION_FILE") or str(_DEFAULT_SESSION_FILE)
        )
        self.api_key_override = os.environ.get("RACIONAL_FIREBASE_API_KEY") or None

    # -- transport ----------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """The one place the HTTP client is built, so tests can swap its transport."""
        return httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Origin": WEB_ORIGIN,
                "Referer": f"{WEB_ORIGIN}/",
            },
        )

    # -- session persistence ------------------------------------------------

    def _load_session(self) -> dict:
        """The cached session; a file that exists but can't be read counts as blocked.

        Reading a damaged file as "no session" would forget a block and the
        device id, and the next run would sign in by itself.
        """
        if not self.session_file.exists():
            return {}
        try:
            data = json.loads(self.session_file.read_text())
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
        logger.warning("Racional: the session file couldn't be read; automatic sign-in stays off")
        return {"auto_login_blocked": "the session file couldn't be read."}

    def _save_session(self, session: dict) -> None:
        """Replace the cache atomically with an owner-only file: it holds a refresh token."""
        tmp = self.session_file.with_name(self.session_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)  # a leftover temp file keeps its old mode
            json.dump(session, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.session_file)

    def _block_auto_login(self, reason: str, *, drop_session: bool = False) -> None:
        """Turn automatic sign-ins off, keeping the device id (and the session unless told)."""
        session = self._load_session()
        if drop_session:
            session.pop("refresh_token", None)
        session["auto_login_blocked"] = reason
        self._save_session(session)

    @staticmethod
    def _blocked_message(reason: Any) -> str:
        if not isinstance(reason, str):
            reason = "an earlier failure."
        return (
            "Racional's automatic sign-in is off until `make racional-login` succeeds. "
            f"Last failure: {reason}"
        )

    # -- sign-in --------------------------------------------------------------

    async def _api_key(self, client: httpx.AsyncClient, session: dict) -> str:
        """The override, the cached key, or the key read from the public bundle."""
        if self.api_key_override:
            return self.api_key_override
        if session.get("api_key"):
            return session["api_key"]
        return await self._discover_api_key(client)

    async def _discover_api_key(self, client: httpx.AsyncClient) -> str:
        page = await client.get(LOGIN_PAGE_URL, headers={"Accept": "text/html"})
        bundle = _find_main_bundle(page.text) if page.is_success else None
        if bundle:
            js = await client.get(f"{WEB_ORIGIN}/{bundle}")
            key = _find_firebase_api_key(js.text) if js.is_success else None
            if key:
                return key
        raise RacionalSessionError(
            "Couldn't read Racional's public Firebase key from its web app; "
            "set RACIONAL_FIREBASE_API_KEY (see USAGE.md)."
        )

    async def _sign_in_api_key(self, client: httpx.AsyncClient, session: dict) -> str:
        """A key read fresh for a sign-in (cheap next to a sign-in), else the cached one.

        A stale cached key would otherwise fail the sign-in after the e-mailed
        code was already spent.
        """
        if self.api_key_override:
            return self.api_key_override
        try:
            return await self._discover_api_key(client)
        except RacionalSessionError:
            if session.get("api_key"):
                return session["api_key"]
            raise

    async def login(self, code_provider: Callable[[], str | Awaitable[str]]) -> tuple[str, str]:
        """Sign in the way the web app does, cache the session, return (ID token, uid).

        `code_provider` is called only when Racional asks for the e-mailed
        code; it may be sync (a prompt) or async (the IMAP reader). Every
        refusal raises `RacionalLoginError` and nothing is retried. The device
        id is saved before Racional first sees it, so a failed sign-in doesn't
        turn the next one into a new device.
        """
        if not self.password:
            raise RacionalLoginError(
                "RACIONAL_PASSWORD is not set; run `make secret-set KEY=RACIONAL_PASSWORD`."
            )
        previous = self._load_session()
        device_id = previous.get("device_id") or str(uuid.uuid4())
        if previous.get("device_id") != device_id:
            previous["device_id"] = device_id
            self._save_session(previous)
        async with self._client() as client:
            api_key = await self._sign_in_api_key(client, previous)

            start = await client.post(
                MFA_START_URL,
                json={"email": self.email, "deviceId": device_id, "method": "email"},
            )
            show_code = True
            if start.is_success:
                show_code = _json_dict(start).get("showMfaModal") is not False
            else:
                # The web app falls back to asking for the code; so do we.
                logger.warning("Racional: authMfaStart answered %s", start.status_code)

            if show_code:
                result = code_provider()
                if inspect.isawaitable(result):
                    result = await result
                verify = await client.post(
                    MFA_VERIFY_URL,
                    json={
                        "email": self.email,
                        "code": str(result).strip(),
                        "deviceId": device_id,
                        "skipMfa": False,
                    },
                )
                if not verify.is_success or _json_dict(verify).get("success") is not True:
                    raise RacionalLoginError(
                        "Racional rejected the verification code. Run `make racional-login` "
                        "and type the newest code it e-mails you."
                    )

            resp = await client.post(
                SIGN_IN_URL,
                headers={API_KEY_HEADER: api_key},
                json={"email": self.email, "password": self.password, "returnSecureToken": True},
            )
        if _is_api_key_error(resp):
            raise RacionalSessionError(
                "Google rejected Racional's Firebase key; set RACIONAL_FIREBASE_API_KEY "
                "(see USAGE.md)."
            )
        if not resp.is_success:
            raise RacionalLoginError(self._sign_in_refusal(_firebase_error(resp), resp.status_code))
        data = _json_dict(resp)
        if data.get("mfaPendingCredential"):
            raise RacionalLoginError(
                "This Racional account signs in with an SMS second factor, which "
                "El Chanchito doesn't support yet."
            )
        id_token, refresh_token, uid = data.get("idToken"), data.get("refreshToken"), data.get("localId")
        if not (id_token and refresh_token and uid):
            raise RacionalLoginError("Racional's sign-in answer had no session in it.")
        self._guard_device_security(id_token)
        self._save_session(
            {"refresh_token": refresh_token, "uid": uid, "device_id": device_id, "api_key": api_key}
        )
        logger.info("Racional: signed in; session cached at %s", self.session_file)
        return id_token, uid

    @staticmethod
    def _sign_in_refusal(code: str, status: int) -> str:
        if code in _BAD_CREDENTIALS_ERRORS:
            return (
                "Racional rejected the e-mail or password. Check RACIONAL_EMAIL and run "
                "`make secret-set KEY=RACIONAL_PASSWORD`; don't retry until they're fixed."
            )
        if code.startswith("TOO_MANY_ATTEMPTS"):
            return "Racional is rate-limiting sign-ins; wait before running `make racional-login`."
        if "RECAPTCHA" in code:
            return (
                "Racional's sign-in now demands a reCAPTCHA check, which El Chanchito "
                "doesn't solve; the HTTP sign-in no longer works."
            )
        return f"Racional's sign-in failed ({status} {code or 'no error code'})."

    def _guard_device_security(self, id_token: str) -> None:
        """Stop for good on an account with device security; fail closed on an unreadable token.

        The session is dropped and automatic sign-in turned off, so the
        scraper doesn't keep minting tokens for an account the web app signs out.
        """
        claims = _token_claims(id_token)
        if not claims:
            raise RuntimeError("Racional's ID token couldn't be read")
        if claims.get("deviceSecurityEnabled") is True:
            error = RacionalDeviceSecurityError(
                "This Racional account has device security on: Racional serves it only "
                "through its new app, and El Chanchito doesn't sign in around it."
            )
            self._block_auto_login(str(error), drop_session=True)
            raise error

    # -- unattended session ---------------------------------------------------

    @property
    def _can_auto_login(self) -> bool:
        """True when the code Racional e-mails lands in the service's IMAP mailbox."""
        imap_user = os.environ.get("EMAIL_IMAP_USER", "").strip().lower()
        return bool(
            self.password
            and os.environ.get("EMAIL_IMAP_HOST")
            and os.environ.get("EMAIL_IMAP_PASSWORD")
            and imap_user
            and imap_user == self.email.lower()
        )

    async def _imap_code_provider(self) -> Callable[[], Awaitable[str]]:
        """A provider that waits for a code newer than the one in the inbox now.

        The newest matching e-mail may hold the code of an earlier sign-in, so
        it is noted before the code is requested and skipped while polling.
        """
        stale = await fetch_latest_code(sender_contains=OTP_SENDERS, subject_contains=OTP_SUBJECTS)

        async def provider() -> str:
            for _ in range(OTP_POLLS):
                code = await fetch_latest_code(
                    sender_contains=OTP_SENDERS, subject_contains=OTP_SUBJECTS
                )
                if code and code != stale:
                    logger.info("Racional: read the verification code from e-mail")
                    return code
                await asyncio.sleep(OTP_POLL_SECONDS)
            raise RacionalLoginError("Racional's verification e-mail didn't arrive in the IMAP inbox.")

        return provider

    async def _auto_login(self) -> tuple[str, str]:
        """One unattended sign-in; any failure but a dropped connection turns them off.

        A refused step, a code e-mail that never arrives, device security or a
        Racional-side error would all repeat on the next cycle, and each
        attempt is a real sign-in plus an e-mail, so none is tried again
        automatically. The reason is kept for the next run's error message.
        """
        logger.info("Racional: no usable session; signing in with the e-mailed code")
        try:
            return await self.login(await self._imap_code_provider())
        except httpx.TransportError:
            raise
        except Exception as e:
            self._block_auto_login(_failure_reason(e))
            raise

    async def _refresh(self, client: httpx.AsyncClient, session: dict) -> tuple[str | None, str]:
        """(ID token, "") or (None, Firebase's error code) for the cached refresh token.

        A rejected API key (Racional rotated it) is re-read from the public
        bundle once, unless it was set by hand.
        """
        resp = await self._post_refresh(client, session, await self._api_key(client, session))
        if _is_api_key_error(resp) and not self.api_key_override:
            logger.info("Racional: the cached Firebase key was rejected; reading it again")
            session["api_key"] = await self._discover_api_key(client)
            self._save_session(session)
            resp = await self._post_refresh(client, session, session["api_key"])
        if resp.is_success:
            data = _json_dict(resp)
            if data.get("refresh_token") and data["refresh_token"] != session["refresh_token"]:
                session["refresh_token"] = data["refresh_token"]
                self._save_session(session)
            if data.get("id_token"):
                return data["id_token"], ""
            raise RuntimeError("Racional's token answer had no ID token in it")
        code = _firebase_error(resp)
        if code in _DEAD_SESSION_ERRORS:
            return None, code
        raise RuntimeError(f"Racional answered {resp.status_code} refreshing the session")

    @staticmethod
    async def _post_refresh(client: httpx.AsyncClient, session: dict, api_key: str) -> httpx.Response:
        return await client.post(
            REFRESH_URL,
            headers={API_KEY_HEADER: api_key},
            data={"grant_type": "refresh_token", "refresh_token": session["refresh_token"]},
        )

    async def _session_token(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """(ID token, uid): the cached session, else at most one unattended sign-in."""
        session = self._load_session()
        expired = False
        if session.get("refresh_token") and session.get("uid"):
            id_token, error = await self._refresh(client, session)
            if id_token:
                self._guard_device_security(id_token)
                return id_token, session["uid"]
            logger.info("Racional: the cached session is no longer valid (%s)", error)
            expired = True
        if session.get("auto_login_blocked"):
            raise RacionalSessionError(self._blocked_message(session["auto_login_blocked"]))
        if not self._can_auto_login:
            raise RacionalSessionError(
                "Racional session expired. Run `make racional-login` to sign in again."
                if expired
                else "No cached Racional session. Run `make racional-login`."
            )
        return await self._auto_login()

    # -- data -----------------------------------------------------------------

    async def _portfolio_documents(
        self, client: httpx.AsyncClient, id_token: str, uid: str
    ) -> list[tuple[str, dict]]:
        """The user's portfolio documents: the query the web app itself runs."""
        resp = await client.post(
            RUN_QUERY_URL,
            headers={"Authorization": f"Bearer {id_token}"},
            json={
                "structuredQuery": {
                    "from": [{"collectionId": "portfolios"}],
                    "where": {
                        "fieldFilter": {
                            "field": {"fieldPath": "userId"},
                            "op": "EQUAL",
                            "value": {"stringValue": uid},
                        }
                    },
                    "limit": MAX_PORTFOLIOS,
                }
            },
        )
        if resp.status_code == 401:
            raise RacionalSessionError(
                "Racional's database refused the session. Run `make racional-login`."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "Racional's database denied the portfolios query (403); its access rules "
                "may have changed (for example, to require App Check)."
            )
        if resp.is_error:
            raise RuntimeError(f"Racional answered {resp.status_code} reading portfolios")
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError("Racional sent an unexpected payload reading portfolios") from None
        return _documents(payload)

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        # Movements belong to one portfolio each; see the module docstring.
        return []

    async def scrape_products(self) -> ProductScrapeResult:
        async with self._client() as client:
            id_token, uid = await self._session_token(client)
            documents = await self._portfolio_documents(client, id_token, uid)

        products: list[ScrapedProduct] = []
        skipped: dict[str, int] = {}
        for doc_id, fields in documents:
            product, reason = _parse_portfolio(doc_id, fields)
            if product is not None:
                products.append(product)
            elif reason:
                skipped[reason] = skipped.get(reason, 0) + 1

        warnings = [
            f"Racional: skipped {n} portfolio(s) {reason}" for reason, n in sorted(skipped.items())
        ]
        if len(documents) >= MAX_PORTFOLIOS:
            warnings.append(f"Racional: read only the first {MAX_PORTFOLIOS} portfolios")
        for warning in warnings:
            logger.warning(warning)
        logger.info("Racional: %d portfolio(s)", len(products))
        return ProductScrapeResult(products, warnings)


def _login_cli() -> None:
    """Manual sign-in: `python -m scrapers.institutions.racional` (or `make racional-login`)."""
    from dotenv import load_dotenv

    load_dotenv()  # walks up to the repo-root .env for RACIONAL_EMAIL
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    scraper = RacionalScraper()

    def prompt_code() -> str:
        return input("Enter the 6-digit code Racional e-mailed you: ")

    try:
        asyncio.run(scraper.login(prompt_code))
    except RuntimeError as e:
        raise SystemExit(f"Racional sign-in failed: {e}") from None
    print(f"\n✅ Signed in. Session cached at {scraper.session_file}")


if __name__ == "__main__":
    _login_cli()
