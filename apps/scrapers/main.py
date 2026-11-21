"""Scraper service entry point with APScheduler."""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from credential_manager import CredentialManager

from db.connection import close_pool, database_url, is_transient_db_error
from db.writer import (
    UnresolvedPriceScope,
    UnresolvedRateScope,
    delete_superseded_transactions,
    finish_scraper_run,
    mark_billed_statements,
    retire_vanished_products,
    soft_delete_vanished_transactions,
    start_scraper_run,
    upsert_exchange_rate,
    upsert_price_history,
    upsert_product,
    write_transactions,
)
from interactive_login import InteractiveLogins, RunGuard, build_providers
from product_model import CREDENTIAL_SPECS
from scrapers.backends import banchile_web
from scrapers.backends.email import get_session as get_email_session
from scrapers.backends.email import reset_session as reset_email_session
from scrapers.base import (
    BaseScraper,
    RunSkipped,
    ScrapedExchangeRate,
    ScrapedPriceHistory,
    SignInRequired,
)
from scrapers.credentials import env_fingerprint
from scrapers.institutions import (
    BanChileScraper,
    BancoFalabellaScraper,
    BciLiderScraper,
    BudaScraper,
    FintualScraper,
    MercadoPagoScraper,
    SantanderScraper,
    SkipoScraper,
    TenpoScraper,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# The executor logs two INFO lines per job run: run_scraper already logs each
# scrape's start and end, and the heartbeat would add a pair every 30s.
# Missed runs and job exceptions are WARNING/ERROR and still show.
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
# httpx logs every request URL at INFO, and some carry account data (the
# Fintual pricing request lists the held tickers).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("scraper-service")


def run_outcome(
    errors: list[str],
    warnings: list[str],
    landed: int,
    notes: list[str] | None = None,
    sign_in: list[str] | None = None,
    skipped: list[str] | None = None,
) -> tuple[str, str | None]:
    """The final (status, error_message) of a run.

    `landed` counts what the run did store: scraped transactions that were
    written (new or already stored) plus products written. A failure only makes
    the run `error` when nothing landed; a run that stored part of its data is
    `partial`, as is one whose scraper reported warnings. `notes` (e.g.
    products retired as closed) only join the message: they never change the
    status, so a clean run that retired a product stays `success`.

    `sign_in` holds the messages of legs that raised `SignInRequired`. When
    nothing landed and nothing else failed, the run is `needs_sign_in`: a
    human must act, and nothing is broken. Otherwise they count as errors.
    Both legs usually raise the same one, so each message is kept once, and
    first: the dashboard shows only a message's first 120 characters.

    `skipped` holds the messages of legs that raised `RunSkipped` (the scraper
    chose not to read, e.g. BanChile's login cooldown). When nothing landed
    and nothing else failed or needs a sign-in, the run is `skipped`: nothing
    is broken and the last run's data stands. Otherwise they count as errors.
    They are deduplicated like `sign_in`, and follow it.
    """
    sign_in = list(dict.fromkeys(sign_in or []))
    skipped = list(dict.fromkeys(skipped or []))
    message = "; ".join(sign_in + skipped + errors + warnings + (notes or [])) or None
    if not errors and not landed:
        if sign_in:
            return "needs_sign_in", message
        if skipped:
            return "skipped", message
    if (errors or sign_in or skipped) and not landed:
        return "error", message
    if errors or sign_in or skipped or warnings:
        return "partial", message
    return "success", message


# Institutions a run or a dashboard-started sign-in holds right now: the two
# never overlap (a sign-in rewrites the session a run reads). It also keeps a
# manual trigger from overlapping the startup run, which is no scheduler job, so
# `max_instances=1` can't stop it.
_RUN_GUARD = RunGuard()


async def run_scraper(scraper: BaseScraper) -> None:
    """Run a single scraper with logging and DB tracking, unless a sign-in
    to its institution is in progress (that sign-in refreshes it when done).

    A call while the same scraper's run is still in flight records nothing and
    returns: that run is already fetching what the call asked for, and two
    sessions at once would fight over one login (BanChile's second run would
    lose its restored session and hit the login cooldown).

    Transactions and products are scraped as independent legs: a failure in one
    must not stop the other. BanChile in particular reads both from one browser
    session (the transactions leg opens it and caches the products half), so a
    session that crashes must still leave the balances refreshable: its products
    leg falls back to a balance-only session of its own, which reuses the
    cached one while it's live (a second full login inside BanChile's login
    cooldown is refused).

    Writes are isolated per row too: a product or transaction that fails to
    write is logged and counted, and the rest are still written. Transactions
    that name a product not stored yet are a warning and land on a later run.
    A leg that raises `SignInRequired` or `RunSkipped` logs one line, no
    traceback: neither is a bug.
    The final status comes from `run_outcome`, and the products leg's
    non-fatal warnings (e.g. a BanChile surface that failed all its retries)
    join the message.
    Once every product of the leg is written, the products its scraper listed
    in full but no longer carries are flagged, and retired on the second
    complete listing in a row that lacks them (`retire_vanished_products`);
    the counts are `retired:` / `pending:` notes, which keep the status, and a
    failure there is a `cleanup:` warning.
    The exchange rates and price histories it read are written after its
    products, each on its own: one that names an unknown product, or fails to
    write, is a warning. Neither counts as landed.
    """
    if not _RUN_GUARD.claim(scraper.institution, "run"):
        logger.info(
            "Scraper %s skipped: %s is busy (%s)",
            scraper.name,
            scraper.institution,
            _RUN_GUARD.holder(scraper.institution),
        )
        return
    try:
        await _run_scraper(scraper)
    finally:
        _RUN_GUARD.release(scraper.institution)


async def _run_scraper(scraper: BaseScraper) -> None:
    """`run_scraper`'s body, once it holds the scraper's institution."""
    run_id = start_scraper_run(scraper.method, scraper.institution)
    logger.info("Starting scraper: %s (run=%s)", scraper.name, run_id)

    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    sign_in: list[str] = []
    skipped: list[str] = []
    n_tx = 0
    inserted = 0
    n_prod = 0
    landed = 0

    try:
        transactions = await scraper.scrape_transactions()
        n_tx = len(transactions)
        written = write_transactions(transactions)
        inserted = written.inserted
        landed += n_tx - written.failed - written.unresolved
        if written.failed:
            errors.append(f"transactions: {written.failed} of {n_tx} not written")
        if written.unresolved:
            # Rows naming a product the products leg (which runs next) has
            # not stored yet: the next run lands them.
            warnings.append(
                f"transactions: {written.unresolved} of {n_tx} not written: "
                "their product does not exist yet"
            )
    except SignInRequired as e:
        logger.warning("Scraper %s: transactions need sign-in: %s", scraper.name, e)
        sign_in.append(str(e))
    except RunSkipped as e:
        logger.info("Scraper %s: transactions skipped: %s", scraper.name, e)
        skipped.append(str(e))
    except Exception as e:
        logger.exception("Scraper %s: transactions failed", scraper.name)
        errors.append(f"transactions: {e}")
    else:
        # Only rows whose replacement this write stored; a failure here is a
        # warning, since the scrape itself succeeded.
        try:
            superseded = scraper.superseded_transactions()
            if superseded:
                delete_superseded_transactions(
                    superseded,
                    written.persisted_external_ids,
                    {t.external_id for t in transactions},
                )
        except Exception as e:
            logger.exception("Scraper %s: superseded-row cleanup failed", scraper.name)
            warnings.append(f"cleanup: {e}")
        # Pending rows the source stopped listing. Only after a write where
        # every transaction landed: a movement that failed to write would leave
        # its stored row looking vanished.
        try:
            vanished = scraper.vanished_windows()
            if vanished and not written.failed and not written.unresolved:
                soft_delete_vanished_transactions(vanished, {t.external_id for t in transactions})
        except Exception as e:
            logger.exception("Scraper %s: vanished-row cleanup failed", scraper.name)
            warnings.append(f"cleanup: {e}")
        # Statements the scrape read mark the stored rows they billed, the
        # ones this write stored included; a failure is a warning.
        try:
            statements = scraper.billed_statements()
            if statements:
                mark_billed_statements(statements)
        except Exception as e:
            logger.exception("Scraper %s: statement billing failed", scraper.name)
            warnings.append(f"statements: {e}")

    rates: list[ScrapedExchangeRate] = []
    histories: list[ScrapedPriceHistory] = []
    try:
        result = await scraper.scrape_products()
    except SignInRequired as e:
        logger.warning("Scraper %s: products need sign-in: %s", scraper.name, e)
        sign_in.append(str(e))
    except RunSkipped as e:
        logger.info("Scraper %s: products skipped: %s", scraper.name, e)
        skipped.append(str(e))
    except Exception as e:
        logger.exception("Scraper %s: products failed", scraper.name)
        errors.append(f"products: {e}")
    else:
        n_prod = len(result.products)
        warnings.extend(result.warnings)
        rates = list(result.exchange_rates)
        histories = list(result.price_histories)
        products_written = 0
        failed: list[str] = []
        for sp in result.products:
            try:
                upsert_product(sp)
                products_written += 1
            except Exception as e:
                logger.exception(
                    "Scraper %s: writing %s/%s %s failed",
                    scraper.name,
                    sp.institution,
                    sp.kind,
                    sp.currency,
                )
                failed.append(f"{sp.institution}/{sp.kind} {sp.currency}: {e}")
                if is_transient_db_error(e):
                    # upsert_product already retried, so the database is down:
                    # each remaining write would block the loop through its own
                    # retries. They count as not written.
                    break
        landed += products_written
        if products_written < n_prod:
            errors.append(
                f"products: {n_prod - products_written} of {n_prod} not written "
                f"({'; '.join(failed)})"
            )
        else:
            # Products the source stopped listing. Only after a write where
            # every product landed: one that failed to write must not look
            # closed.
            for scope in result.vanished_product_scopes:
                try:
                    swept = retire_vanished_products(scope)
                    label = f"{scope.institution} {scope.product_kind} {scope.currency}"
                    if swept.retired:
                        notes.append(
                            f"retired: {swept.retired} {label} product(s) no longer listed"
                        )
                    if swept.pending:
                        notes.append(
                            f"pending: {swept.pending} {label} product(s) "
                            "missing from this listing; retired if the next complete "
                            "one lacks them too"
                        )
                except Exception as e:
                    logger.exception("Scraper %s: vanished-product cleanup failed", scraper.name)
                    warnings.append(f"cleanup: {e}")

    warnings.extend(_write_exchange_rates(scraper, rates))
    warnings.extend(_write_price_histories(scraper, histories))

    logger.info(
        "Scraper %s: %d transactions (%d new), %d products, %d price points read",
        scraper.name,
        n_tx,
        inserted,
        n_prod,
        sum(len(h.points) for h in histories),
    )
    status, message = run_outcome(errors, warnings, landed, notes, sign_in, skipped)
    finish_scraper_run(run_id, status, transactions_imported=inserted, error_message=message)


def _write_exchange_rates(scraper: BaseScraper, rates: list[ScrapedExchangeRate]) -> list[str]:
    """Write each rate on its own; return a warning per rate that wasn't written.

    A rate is auxiliary to the products it prices, so neither an unresolvable
    scope nor a write error may cost the run its other results.
    """
    warnings: list[str] = []
    for rate in rates:
        label = f"{rate.base_currency}/{rate.quote_currency}"
        try:
            upsert_exchange_rate(rate)
        except UnresolvedRateScope as e:
            logger.warning("Scraper %s: exchange rate %s skipped: %s", scraper.name, label, e)
            warnings.append(f"exchange rate {label}: {e}")
        except Exception as e:
            logger.exception("Scraper %s: exchange rate %s failed", scraper.name, label)
            warnings.append(f"exchange rate {label}: {e}")
    return warnings


def _write_price_histories(scraper: BaseScraper, histories: list[ScrapedPriceHistory]) -> list[str]:
    """Write each price history on its own; return one warning counting the
    series that weren't written, by exception type.

    Like a rate, a series is auxiliary to the product it prices, so neither an
    unresolvable product nor a write error may cost the run its other
    results. An error `is_transient_db_error` accepts (any psycopg
    OperationalError: a lost connection, a pool timeout, a deadlock) has
    already outlived `upsert_price_history`'s retries, so it stops the rest,
    which count as not written: each would sit through its own retries.
    """
    failures: Counter[str] = Counter()
    for i, history in enumerate(histories):
        label = f"{history.product.kind} {history.product.currency}"
        try:
            upsert_price_history(history)
        except UnresolvedPriceScope as e:
            logger.warning("Scraper %s: price history %s skipped: %s", scraper.name, label, e)
            failures[type(e).__name__] += 1
        except Exception as e:
            logger.exception("Scraper %s: price history %s failed", scraper.name, label)
            if is_transient_db_error(e):
                # This series and every one not tried yet.
                failures[type(e).__name__] += len(histories) - i
                break
            failures[type(e).__name__] += 1
    if not failures:
        return []
    return [
        f"price histories: {sum(failures.values())} of {len(histories)} not "
        f"written ({', '.join(sorted(failures))})"
    ]


def _env_flag(name: str) -> bool:
    """Whether the opt-in flag `name` is on: set, and not 0/false/no/off."""
    value = os.environ.get(name, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def build_scrapers() -> dict[str, BaseScraper]:
    """Instantiate only scrapers whose credentials are configured."""
    scrapers: dict[str, BaseScraper] = {}

    if os.environ.get("FINTUAL_EMAIL") and (
        os.environ.get("FINTUAL_PASSWORD") or os.environ.get("FINTUAL_TOKEN")
    ):
        scrapers["fintual"] = FintualScraper()
        logger.info("Fintual scraper enabled")

    if os.environ.get("BUDA_API_KEY") and os.environ.get("BUDA_API_SECRET"):
        scrapers["buda"] = BudaScraper()
        logger.info("Buda scraper enabled")

    if os.environ.get("SKIPO_API_KEY"):
        scrapers["skipo"] = SkipoScraper()
        logger.info("Skipo scraper enabled")

    if os.environ.get("BANCHILE_RUT") and os.environ.get("BANCHILE_PASSWORD"):
        scrapers["banchile"] = BanChileScraper()
        logger.info("BanChile scraper enabled")

    # Auth is a cached web session (make bci-lider-login), so only the RUT is
    # required to enable it; a missing/expired session surfaces as a run error.
    if os.environ.get("LIDER_BCI_RUT"):
        scrapers["bci_lider"] = BciLiderScraper()
        logger.info("BCI Lider scraper enabled")

    if os.environ.get("SANTANDER_RUT") and os.environ.get("SANTANDER_PASSWORD"):
        scrapers["santander"] = SantanderScraper()
        logger.info("Santander scraper enabled")

    if os.environ.get("BANCO_FALABELLA_RUT") and os.environ.get("BANCO_FALABELLA_PASSWORD"):
        scrapers["banco_falabella"] = BancoFalabellaScraper()
        logger.info("Banco Falabella scraper enabled")

    # Mercado Pago has no credential to gate on: auth is the Chrome profile a
    # human signed in to (make mercadopago-login), so it is opt-in by flag; a
    # missing or expired session surfaces as a run error.
    if _env_flag("MERCADOPAGO_ENABLED"):
        scrapers["mercadopago"] = MercadoPagoScraper()
        logger.info("MercadoPago scraper enabled")

    if all(
        os.environ.get(k) for k in ("EMAIL_IMAP_HOST", "EMAIL_IMAP_USER", "EMAIL_IMAP_PASSWORD")
    ):
        # MACH is disabled: its email parser produced transactions that never happened.
        scrapers["tenpo"] = TenpoScraper()
        logger.info("Email-based scrapers enabled: tenpo")

    return scrapers


async def run_all_once(scrapers: dict[str, BaseScraper]) -> None:
    """Run all configured scrapers once."""
    if not scrapers:
        logger.warning("No scrapers configured. Set env vars to enable them.")
        return

    logger.info("Running %d scrapers", len(scrapers))
    for scraper in scrapers.values():
        await run_scraper(scraper)


# APScheduler intervals per institution (hours unless noted)
_EMAIL_INTERVAL_MINUTES = 30
_SCHEDULES: dict[str, dict] = {
    "fintual": {"hours": 6, "label": "Fintual API (every 6h)"},
    "buda": {"hours": 1, "label": "Buda API (every 1h)"},
    "skipo": {"hours": 1, "label": "Skipo API (every 1h)"},
    "banchile": {"hours": 24, "label": "BanChile (daily)"},
    "bci_lider": {"hours": 24, "label": "BCI Lider (daily)"},
    "santander": {"hours": 24, "label": "Santander (daily)"},
    "banco_falabella": {"hours": 24, "label": "Banco Falabella (daily)"},
    # No login per run (the Chrome profile holds the session), so it can run
    # more often than the daily browser logins.
    "mercadopago": {"hours": 6, "label": "MercadoPago (every 6h)"},
    "mach": {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "MACH email (every 30m)"},
    "tenpo": {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "Tenpo email (every 30m)"},
}

# Manual-refresh cooldowns: slug -> the seconds until a manual trigger may run,
# or None when it may run now. Kept out of _SCHEDULES, whose entries become
# IntervalTrigger kwargs. The bank throttles full logins (issue #28), so
# BanChile refuses only a run that would certainly need one inside its login
# cooldown; anything its cached session can serve goes through.
_REFRESH_COOLDOWNS: dict[str, Callable[[], float | None]] = {
    "banchile": banchile_web.login_cooldown_retry_after,
}


# The scheduler runs a heartbeat job this often, and GET /health answers 503
# once it has gone this long without one. The threshold is generous because
# sync work (IMAP scans, DB writes) still runs on the event loop and delays the
# beat while it does; only a loop stuck far longer than that reads as wedged.
_HEARTBEAT_INTERVAL_SECONDS = 30
_HEARTBEAT_STALE_SECONDS = 10 * 60


class Heartbeat:
    """When the scheduler last ran its heartbeat job.

    Written on the event loop and read from the control server's thread; a
    single float assignment needs no lock.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last = clock()

    def beat(self) -> None:
        self._last = self._clock()

    def age(self) -> float:
        return self._clock() - self._last


# The largest request body the control server reads (a sign-in code).
_MAX_CONTROL_BODY = 1024


def _trigger_job(scheduler: AsyncIOScheduler, slug: str) -> bool:
    """Run `slug`'s scheduled job now; False when it has none."""
    job = scheduler.get_job(slug)
    if job is None:
        return False
    job.modify(next_run_time=datetime.now(UTC))
    logger.info("Manual trigger for %s", slug)
    return True


def _make_control_handler(
    scheduler: AsyncIOScheduler,
    scraper_keys: "set[str] | Callable[[], frozenset[str]]",
    heartbeat: Heartbeat,
    cooldowns: dict[str, Callable[[], float | None]] | None = None,
    logins: InteractiveLogins | None = None,
    credentials: "CredentialControl | None" = None,
):
    """Build the HTTP handler for the internal scraper control server.

    Triggering a scrape means moving a scheduled job's next run time to now:
    APScheduler then runs it on the scheduler's own event loop, reusing the
    job's `coalesce` / `max_instances=1` guards so a manual trigger can't
    overlap a scheduled or in-flight run. `job.modify()` is thread-safe, so
    it's fine to call from this handler's thread.

    Those guards stop overlap, not frequency, so `cooldowns` maps a slug to
    the seconds until it may be triggered again (None: now). Until then
    `POST /refresh/{slug}` answers 429 with `Retry-After`, and `POST /refresh`
    triggers the rest and lists it under `skipped`. The startup run isn't a
    job, so a trigger during it isn't absorbed by `max_instances`;
    `run_scraper`'s in-flight guard absorbs it instead.

    GET /health reports the scheduler, not just this thread: it answers 503
    when `heartbeat` has gone stale, which is what the compose healthcheck
    reads.

    `/login` routes start the interactive sign-ins of `logins`
    (`interactive_login`): `GET /login` lists them, `POST /login/{slug}`
    starts one, `GET /login/{slug}` reports it, and `POST /login/{slug}/code`
    hands a `code` sign-in its one-time code. A sign-in opens windows on this
    machine and mails codes, so these routes answer only the dashboard's
    server: a request carrying a browser's `Origin` or `Sec-Fetch-Site` is
    refused (a page reaching this port directly, DNS rebinding included), and
    a POST must be labelled JSON, so a cross-origin one always needs a
    preflight this server never grants.

    With `credentials` (refused to browsers like the /login routes),
    GET /credentials/status reports per env var whether a
    fallback value exists and whether a stored one was opened or needs
    re-entry (booleans only, never a value), and POST /credentials/reload
    re-reads the stored credentials and adds, drops or rebuilds scrapers to
    match, without touching a run in flight.
    """
    cooldowns = cooldowns or {}
    # A callable returns an immutable snapshot (ScraperSet.snapshot); a plain
    # set is copied, so no request iterates a set another thread edits.
    current_keys = scraper_keys if callable(scraper_keys) else lambda: frozenset(scraper_keys)

    def seconds_left(slug: str) -> int:
        retry_after = cooldowns.get(slug)
        wait = retry_after() if retry_after else None
        return math.ceil(wait) if wait and wait > 0 else 0

    def trigger(slug: str) -> bool:
        return _trigger_job(scheduler, slug)

    class ControlHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _browser_refused(self) -> bool:
            """Send the refusal for a /login or /credentials request a browser
            made, or a POST not labelled JSON; True when one was sent."""
            if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"):
                self._send(403, {"error": "browser requests are not accepted"})
                return True
            if self.command == "POST":
                content_type = (self.headers.get("Content-Type") or "").split(";")[0]
                if content_type.strip().lower() != "application/json":
                    self._send(415, {"error": "expected application/json"})
                    return True
            return False

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if not 0 <= length <= _MAX_CONTROL_BODY:
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            return body if isinstance(body, dict) else None

        def _handle_login(self) -> None:
            if logins is None:
                self._send(404, {"error": "not found"})
                return
            if self._browser_refused():
                return
            parts = self.path.strip("/").split("/")
            if self.command == "GET" and parts == ["login"]:
                self._send(200, {"logins": logins.supported()})
            elif self.command == "GET" and len(parts) == 2:
                self._send(*logins.status(parts[1]))
            elif self.command == "POST" and len(parts) == 2:
                self._send(*logins.start(parts[1]))
            elif self.command == "POST" and len(parts) == 3 and parts[2] == "code":
                body = self._read_json()
                if body is None:
                    self._send(400, {"error": "invalid JSON body"})
                else:
                    self._send(*logins.submit_code(parts[1], body.get("code")))
            else:
                self._send(404, {"error": "not found"})

        def do_GET(self) -> None:
            if self.path == "/login" or self.path.startswith("/login/"):
                self._handle_login()
            elif self.path == "/health":
                age = round(heartbeat.age())
                if age < _HEARTBEAT_STALE_SECONDS:
                    self._send(200, {"status": "ok", "heartbeat_age_seconds": age})
                else:
                    self._send(503, {"status": "stale", "heartbeat_age_seconds": age})
            elif self.path == "/credentials/status":
                if credentials is None:
                    self._send(404, {"error": "not found"})
                elif not self._browser_refused():
                    self._send(200, credentials.status())
            elif self.path == "/scrapers":
                # Which scrapers exist is decided by build_scrapers(), at
                # startup and on each credentials reload, so expose the list for the dashboard
                # instead of letting it hardcode a stale copy. Advertise only
                # slugs with a scheduler job: POST /refresh/{slug} 404s on the
                # rest (no _SCHEDULES entry ⇒ never registered), and the two
                # endpoints must agree on the set.
                enabled = sorted(s for s in current_keys() if scheduler.get_job(s))
                self._send(200, {"scrapers": enabled})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.startswith("/login/"):
                self._handle_login()
            elif self.path == "/credentials/reload":
                if credentials is None:
                    self._send(404, {"error": "not found"})
                    return
                if self._browser_refused():
                    return
                try:
                    self._send(200, credentials.reload())
                except Exception:
                    logger.exception("Credentials reload failed")
                    self._send(500, {"error": "credentials reload failed"})
            elif self.path == "/refresh":
                triggered: list[str] = []
                skipped: list[dict] = []
                for slug in sorted(s for s in current_keys() if scheduler.get_job(s)):
                    wait = seconds_left(slug)
                    if wait:
                        logger.info("Manual trigger for %s skipped: cooldown, %ds left", slug, wait)
                        skipped.append({"slug": slug, "retry_after_seconds": wait})
                    elif trigger(slug):
                        triggered.append(slug)
                self._send(202, {"triggered": triggered, "skipped": skipped})
            elif self.path.startswith("/refresh/"):
                slug = self.path[len("/refresh/") :]
                if slug not in current_keys():
                    # Only scrapers: never the heartbeat or credentials jobs.
                    self._send(404, {"error": f"unknown scraper: {slug}"})
                    return
                wait = seconds_left(slug) if scheduler.get_job(slug) else 0
                if wait:
                    logger.info("Manual trigger for %s refused: cooldown, %ds left", slug, wait)
                    self._send(
                        429,
                        {
                            "error": f"{slug} is cooling down; retry in {wait}s",
                            "skipped": [{"slug": slug, "retry_after_seconds": wait}],
                        },
                        {"Retry-After": str(wait)},
                    )
                elif _RUN_GUARD.holder(slug) == "login":
                    # The run would be dropped; the sign-in refreshes it when it succeeds.
                    self._send(409, {"error": f"a sign-in to {slug} is in progress"})
                elif trigger(slug):
                    self._send(202, {"triggered": [slug]})
                else:
                    self._send(404, {"error": f"unknown scraper: {slug}"})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, fmt: str, *args) -> None:
            # Route the default stderr access log through our logger (debug).
            logger.debug("control: " + fmt, *args)

    return ControlHandler


def _start_control_server(
    scheduler: AsyncIOScheduler,
    scraper_keys: "set[str] | Callable[[], frozenset[str]]",
    heartbeat: Heartbeat,
    credentials: "CredentialControl | None" = None,
) -> ThreadingHTTPServer | None:
    """Start the internal HTTP control server when SCRAPER_CONTROL_PORT is set.

    This is an unauthenticated trigger meant to be reachable only from the
    dashboard, so it binds SCRAPER_CONTROL_HOST, loopback by default (host-dev).
    Docker Compose sets 0.0.0.0 so the web container can reach it over the
    private compose network, which never publishes the port (see issue #23).
    """
    port = os.environ.get("SCRAPER_CONTROL_PORT")
    if not port:
        return None

    host = os.environ.get("SCRAPER_CONTROL_HOST") or "127.0.0.1"
    current_keys = scraper_keys if callable(scraper_keys) else lambda: frozenset(scraper_keys)
    logins = InteractiveLogins(
        lambda: build_providers(current_keys()),
        lambda slug: _trigger_job(scheduler, slug),
        _RUN_GUARD,
    )
    handler = _make_control_handler(
        scheduler, scraper_keys, heartbeat, _REFRESH_COOLDOWNS, logins, credentials
    )
    server = ThreadingHTTPServer((host, int(port)), handler)
    thread = threading.Thread(target=server.serve_forever, name="scraper-control", daemon=True)
    thread.start()
    logger.info(
        "Control server listening on %s:%s (POST /refresh[/{slug}], /login[/{slug}], "
        "GET /scrapers, GET /health, GET /credentials/status, POST /credentials/reload)",
        host,
        port,
    )
    return server


# Fintual's refresh token dies after several idle hours, well before its
# cookie's Max-Age, so a job between the 6h runs keeps it alive (no sign-in).
FINTUAL_KEEPALIVE_JOB = "fintual_keepalive"
_FINTUAL_KEEPALIVE_DEFAULT_HOURS = 2.0


def fintual_keepalive_hours() -> float:
    """FINTUAL_KEEPALIVE_HOURS (default 2); 0 or less disables the job."""
    raw = os.environ.get("FINTUAL_KEEPALIVE_HOURS", "").strip()
    if not raw:
        return _FINTUAL_KEEPALIVE_DEFAULT_HOURS
    try:
        hours = float(raw)
    except ValueError:
        logger.warning(
            "FINTUAL_KEEPALIVE_HOURS is not a number; using %s",
            _FINTUAL_KEEPALIVE_DEFAULT_HOURS,
        )
        return _FINTUAL_KEEPALIVE_DEFAULT_HOURS
    return hours if math.isfinite(hours) else _FINTUAL_KEEPALIVE_DEFAULT_HOURS


async def _fintual_keepalive(scheduler: AsyncIOScheduler) -> None:
    """Keep the scheduled Fintual instance's session alive. Records no run:
    it reads nothing, so the dashboard has nothing to show for it."""
    job = scheduler.get_job("fintual")
    if job is None:
        return
    # A run or a dashboard sign-in in flight drives the session itself.
    if _RUN_GUARD.holder("fintual") is not None:
        logger.info("Fintual keepalive skipped: %s in progress", _RUN_GUARD.holder("fintual"))
        return
    await job.args[0].keepalive()


def _schedule_fintual_keepalive(scheduler: AsyncIOScheduler) -> bool:
    """Add the Fintual keepalive job; False when FINTUAL_KEEPALIVE_HOURS disables it.

    It resolves the scraper when it runs, so a rebuilt instance (new
    credentials) is the one kept alive, under the same session lock as its runs.
    """
    hours = fintual_keepalive_hours()
    if hours <= 0:
        logger.info("Fintual keepalive disabled (FINTUAL_KEEPALIVE_HOURS=%s)", hours)
        return False
    scheduler.add_job(
        _fintual_keepalive,
        IntervalTrigger(hours=hours),
        args=[scheduler],
        id=FINTUAL_KEEPALIVE_JOB,
        name=f"Fintual session keepalive (every {hours:g}h)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )
    return True


def _schedule_scraper(scheduler: AsyncIOScheduler, key: str, scraper: BaseScraper) -> bool:
    """Add `scraper`'s interval job; False when `key` has no schedule."""
    cfg = _SCHEDULES.get(key)
    if not cfg:
        logger.warning("No schedule configured for %s; skipping", key)
        return False
    trigger_kwargs = {k: v for k, v in cfg.items() if k != "label"}
    scheduler.add_job(
        run_scraper,
        IntervalTrigger(**trigger_kwargs),
        args=[scraper],
        id=key,
        name=cfg["label"],
        # A manual trigger reuses these guards so it can never overlap a
        # scheduled or in-flight run of the same institution.
        max_instances=1,
        coalesce=True,
        # APScheduler's default grace is 1s: a run (or a manual trigger)
        # that comes due while sync work holds the event loop would be
        # dropped as missed. Run it late instead; `coalesce` keeps it to one.
        misfire_grace_time=None,
    )
    return True


def _credential_env_vars(slug: str) -> tuple[str, ...]:
    """The env vars (legacy names included) whose credentials feed `slug`."""
    return tuple(
        name
        for spec in CREDENTIAL_SPECS
        if slug in spec.scraper_slugs
        for f in spec.fields
        for name in (f.env_var, *f.fallback_env_vars)
    )


def _reset_scraper_state(slug: str, scraper: BaseScraper) -> None:
    """Forget what a scraper kept from its previous credentials. A cached
    session of another account is discarded where it is loaded (see
    scrapers/session_account.py), so a session written back by a run still
    on the old credentials is caught too."""
    try:
        if slug == "fintual":
            scraper._login_failed_file.unlink(missing_ok=True)
        elif slug == "tenpo":
            reset_email_session()
    except OSError as exc:
        logger.warning("Could not reset %s's cached state: %s", slug, type(exc).__name__)


class ScraperSet:
    """The scheduled scrapers, kept in step with the credentials.

    `snapshot()` is what the control server reads: an immutable set, swapped
    whole on each change, so other threads never iterate one being edited.
    `rebuild()` runs `build_scrapers()` again: a scraper that lost its
    credentials loses its job, a newly configured one gets one, and one whose
    credentials changed gets a fresh instance for its next run, with its
    sign-in marker cleared (a cached session of another account is discarded
    where it is loaded). A run in flight keeps the instance it started with, and
    `max_instances=1` still stops the next run from overlapping it. A scraper
    whose credentials did not change keeps its instance and its session.
    """

    def __init__(
        self,
        scheduler: AsyncIOScheduler,
        scrapers: dict[str, BaseScraper] | None,
        build: Callable[[], dict[str, BaseScraper]] = lambda: build_scrapers(),
    ) -> None:
        self.scheduler = scheduler
        self._keys: frozenset[str] = frozenset()
        self._build = build
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.Lock()
        # None: built by the first rebuild(), once the stored credentials
        # are applied, as at a start (no marker or session is reset).
        self._initialized = scrapers is not None
        for slug, scraper in (scrapers or {}).items():
            self._add(slug, scraper)

    def snapshot(self) -> frozenset[str]:
        return self._keys

    @property
    def keys(self) -> frozenset[str]:
        return self._keys

    @staticmethod
    def _fingerprint(slug: str) -> str:
        return env_fingerprint(_credential_env_vars(slug))

    def _add(self, slug: str, scraper: BaseScraper, fingerprint: str | None = None) -> bool:
        if not _schedule_scraper(self.scheduler, slug, scraper):
            return False
        if slug == "fintual":
            _schedule_fintual_keepalive(self.scheduler)
        self._keys = self._keys | {slug}
        self._fingerprints[slug] = fingerprint or self._fingerprint(slug)
        return True

    def current(self) -> dict[str, BaseScraper]:
        """The scheduled instance of each scraper."""
        jobs = {slug: self.scheduler.get_job(slug) for slug in sorted(self._keys)}
        return {slug: job.args[0] for slug, job in jobs.items() if job is not None}

    def rebuild(self) -> dict[str, list[str]]:
        with self._lock:
            # Fingerprint before building: if a concurrent apply changes the
            # env mid-build, the stored fingerprint is the older one, so the
            # next rebuild sees the change instead of skipping it.
            slugs = self._keys | {s for spec in CREDENTIAL_SPECS for s in spec.scraper_slugs}
            fingerprints = {slug: self._fingerprint(slug) for slug in slugs}
            wanted = self._build()
            if not self._initialized:
                self._initialized = True
                added = [
                    slug
                    for slug, scraper in wanted.items()
                    if self._add(slug, scraper, fingerprints.get(slug))
                ]
                return {"added": sorted(added), "removed": [], "updated": []}
            added: list[str] = []
            removed: list[str] = []
            updated: list[str] = []
            for slug in sorted(self._keys - wanted.keys()):
                if self.scheduler.get_job(slug):
                    self.scheduler.remove_job(slug)
                if slug == "fintual" and self.scheduler.get_job(FINTUAL_KEEPALIVE_JOB):
                    self.scheduler.remove_job(FINTUAL_KEEPALIVE_JOB)
                self._keys = self._keys - {slug}
                self._fingerprints.pop(slug, None)
                removed.append(slug)
                if slug == "tenpo":
                    reset_email_session()
                logger.info("Scraper %s disabled: its credentials are gone", slug)
            for slug in sorted(wanted):
                fingerprint = fingerprints.get(slug) or self._fingerprint(slug)
                if slug not in self._keys:
                    if self._add(slug, wanted[slug], fingerprint):
                        _reset_scraper_state(slug, wanted[slug])
                        added.append(slug)
                elif fingerprint != self._fingerprints.get(slug):
                    _reset_scraper_state(slug, wanted[slug])
                    job = self.scheduler.get_job(slug)
                    if job is not None:
                        job.modify(args=[wanted[slug]])
                    self._fingerprints[slug] = fingerprint
                    updated.append(slug)
                    logger.info("Scraper %s picks up its new credentials on its next run", slug)
            return {"added": added, "removed": removed, "updated": updated}


class CredentialControl:
    """What the control server's /credentials endpoints and the periodic
    credentials job call."""

    def __init__(self, manager: CredentialManager, scrapers: ScraperSet) -> None:
        self.manager = manager
        self.scrapers = scrapers

    def rebuild(self) -> dict[str, list[str]]:
        """Rebuild the scrapers, but only once the stored credentials are
        applied or known unusable: before that the env may still hold
        credentials the dashboard replaced, and a scraper built on them
        could log in with them (POST /refresh)."""
        if not self.manager.settled.is_set():
            return {"added": [], "removed": [], "updated": []}
        return self.scrapers.rebuild()

    def status(self) -> dict:
        # Not published or not loaded yet (database down, V048 missing): try
        # again now, off this request's thread.
        self.manager.retry_in_background(on_loaded=self.rebuild)
        return self.manager.status()

    def reload(self) -> dict:
        applied = self.manager.reload()
        changes = self.rebuild()
        enabled = sorted(s for s in self.scrapers.snapshot() if self.scrapers.scheduler.get_job(s))
        return {"applied": applied, **changes, "scrapers": enabled}

    def tick(self) -> None:
        if self.manager.tick():
            self.rebuild()


# How often the service retries publishing its key and reading the stored
# credentials until both work, and checks nobody replaced its key.
_CREDENTIALS_TICK_SECONDS = 60


def _start_credentials() -> CredentialManager:
    """Load (or create) the key file. The database work runs in the
    background, so the service comes up and reports status without it."""
    manager = CredentialManager()
    manager.start()
    return manager


# How often the first scrape checks whether the stored credentials are applied.
_SETTLE_POLL_SECONDS = 1.0


async def _first_scrape(
    manager: CredentialManager, scraper_set: "ScraperSet", control_port: str | None
) -> None:
    """Wait until the stored credentials are applied (or known to be
    unusable), then build the scrapers and run each once. A scraper must
    never log in with credentials the dashboard replaced, so with the
    database down this waits; the control server answers meanwhile."""
    waited = False
    while not manager.settled.is_set():
        if not waited:
            logger.info("Waiting for the database before the first scrape...")
            waited = True
        await asyncio.sleep(_SETTLE_POLL_SECONDS)
    scraper_set.rebuild()
    scrapers = scraper_set.current()
    if not scrapers:
        if not control_port:
            logger.error("No scrapers configured. Exiting.")
            sys.exit(1)
        # Credentials entered in the dashboard reach this service through
        # the control server, so it stays up waiting for them.
        logger.warning("No scrapers configured yet; add credentials in the dashboard")
        return
    await run_all_once(scrapers)


def _require_database_url() -> None:
    """Exit before scheduling or scraping anything when DATABASE_URL is unset."""
    try:
        database_url()
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)


def main_scheduled() -> None:
    """Run scrapers on a schedule using APScheduler."""
    _require_database_url()
    credential_manager = _start_credentials()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # AsyncIOScheduler.start() (apscheduler 3.11+) binds to the *running* loop
    # unless one is passed explicitly. We start it below before the loop is
    # running, so hand it our loop up front.
    scheduler = AsyncIOScheduler(event_loop=loop)

    scraper_set = ScraperSet(scheduler, None)

    heartbeat = Heartbeat()

    async def beat() -> None:
        heartbeat.beat()

    # A coroutine, so it runs on the event loop itself: the beat goes stale
    # exactly when the loop, and every scraper job with it, is stuck.
    scheduler.add_job(
        beat,
        IntervalTrigger(seconds=_HEARTBEAT_INTERVAL_SECONDS),
        id="heartbeat",
        name="Heartbeat",
        max_instances=1,
        coalesce=True,
        # A late beat is still true (it only runs once the loop is free), and
        # skipping it would log a missed-run warning after every busy second.
        misfire_grace_time=None,
    )

    def shutdown(signum, frame):
        logger.info("Shutting down (signal %s)...", signum)
        scheduler.shutdown(wait=False)
        try:
            get_email_session().close()
        except KeyError:
            # Email session never initialised (no EMAIL_IMAP_* creds).
            pass
        except Exception:
            logger.exception("Error closing IMAP session")
        close_pool()
        loop.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    scheduler.start()
    logger.info("Scheduler started")

    credential_control = CredentialControl(credential_manager, scraper_set)
    # Sync, so the executor runs it on a worker thread: its DB retries never
    # hold the event loop.
    scheduler.add_job(
        credential_control.tick,
        IntervalTrigger(seconds=_CREDENTIALS_TICK_SECONDS),
        id="credentials",
        name="Credentials key and rows",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )
    control_server = _start_control_server(
        scheduler, scraper_set.snapshot, heartbeat, credential_control
    )
    credential_manager.retry_in_background(on_loaded=credential_control.rebuild)

    loop.run_until_complete(
        _first_scrape(credential_manager, scraper_set, os.environ.get("SCRAPER_CONTROL_PORT"))
    )

    logger.info("Initial scrape complete. Scheduler running...")
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # The signal handler has usually shut it down already, and a second
        # shutdown() raises, which would skip the cleanup below.
        if scheduler.running:
            scheduler.shutdown(wait=False)
        if control_server is not None:
            control_server.shutdown()
        try:
            get_email_session().close()
        except KeyError:
            pass
        except Exception:
            logger.exception("Error closing IMAP session")
        close_pool()
        logger.info("Scraper service stopped.")


def main_once() -> None:
    """Run all scrapers once and exit (for cron-based scheduling)."""
    _require_database_url()
    # A one-off run never creates or publishes a key: it would replace the
    # long-running service's, whose stored credentials would then go unread.
    CredentialManager().start_readonly()
    scrapers = build_scrapers()
    try:
        asyncio.run(run_all_once(scrapers))
    finally:
        try:
            get_email_session().close()
        except KeyError:
            pass
        except Exception:
            logger.exception("Error closing IMAP session")
        close_pool()


if __name__ == "__main__":
    mode = os.environ.get("SCRAPER_MODE", "scheduled")
    if mode == "once":
        main_once()
    else:
        main_scheduled()
