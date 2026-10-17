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
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db.connection import close_pool, database_url, is_transient_db_error
from db.writer import (
    UnresolvedRateScope,
    delete_superseded_transactions,
    finish_scraper_run,
    start_scraper_run,
    upsert_exchange_rate,
    upsert_product,
    write_transactions,
)
from scrapers.backends import banchile_web
from scrapers.backends.email import get_session as get_email_session
from scrapers.base import BaseScraper, ScrapedExchangeRate
from scrapers.institutions import (
    BanChileScraper,
    BciLiderScraper,
    BudaScraper,
    FintualScraper,
    MachScraper,
    MercadoPagoScraper,
    SantanderScraper,
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
logger = logging.getLogger("scraper-service")


def run_outcome(
    errors: list[str], warnings: list[str], landed: int
) -> tuple[str, str | None]:
    """The final (status, error_message) of a run.

    `landed` counts what the run did store: scraped transactions that were
    written (new or already stored) plus products written. A failure only makes
    the run `error` when nothing landed; a run that stored part of its data is
    `partial`, as is one whose scraper reported warnings.
    """
    message = "; ".join(errors + warnings) or None
    if errors and not landed:
        return "error", message
    if errors or warnings:
        return "partial", message
    return "success", None


async def run_scraper(scraper: BaseScraper) -> None:
    """Run a single scraper with logging and DB tracking.

    Transactions and products are scraped as independent legs: a failure in one
    must not stop the other. BanChile in particular reads both from one browser
    session (the transactions leg opens it and caches the products half), so a
    session that crashes must still leave the balances refreshable: its products
    leg falls back to a balance-only session of its own, which reuses the
    cached one while it's live (a second full login inside BanChile's login
    cooldown is refused).

    Writes are isolated per row too: a product or transaction that fails to
    write is logged and counted, and the rest are still written. The final
    status comes from `run_outcome`, and the products leg's non-fatal warnings
    (e.g. a BanChile surface that failed all its retries) join the message.
    The exchange rates it read are written after its products, each on its
    own: a rate that names an unknown product, or fails to write, is a
    warning.
    """
    run_id = start_scraper_run(scraper.method, scraper.institution)
    logger.info("Starting scraper: %s (run=%s)", scraper.name, run_id)

    errors: list[str] = []
    warnings: list[str] = []
    n_tx = 0
    inserted = 0
    n_prod = 0
    landed = 0

    try:
        transactions = await scraper.scrape_transactions()
        n_tx = len(transactions)
        written = write_transactions(transactions)
        inserted = written.inserted
        landed += n_tx - written.failed
        if written.failed:
            errors.append(f"transactions: {written.failed} of {n_tx} not written")
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

    rates: list[ScrapedExchangeRate] = []
    try:
        result = await scraper.scrape_products()
    except Exception as e:
        logger.exception("Scraper %s: products failed", scraper.name)
        errors.append(f"products: {e}")
    else:
        n_prod = len(result.products)
        warnings.extend(result.warnings)
        rates = list(result.exchange_rates)
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

    warnings.extend(_write_exchange_rates(scraper, rates))

    logger.info(
        "Scraper %s: %d transactions (%d new), %d products",
        scraper.name,
        n_tx,
        inserted,
        n_prod,
    )
    status, message = run_outcome(errors, warnings, landed)
    finish_scraper_run(
        run_id, status, transactions_imported=inserted, error_message=message
    )


def _write_exchange_rates(
    scraper: BaseScraper, rates: list[ScrapedExchangeRate]
) -> list[str]:
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
            logger.warning(
                "Scraper %s: exchange rate %s skipped: %s", scraper.name, label, e
            )
            warnings.append(f"exchange rate {label}: {e}")
        except Exception as e:
            logger.exception("Scraper %s: exchange rate %s failed", scraper.name, label)
            warnings.append(f"exchange rate {label}: {e}")
    return warnings


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

    if all(
        os.environ.get(k)
        for k in ("EMAIL_IMAP_HOST", "EMAIL_IMAP_USER", "EMAIL_IMAP_PASSWORD")
    ):
        scrapers["mach"] = MachScraper()
        scrapers["mercadopago"] = MercadoPagoScraper()
        scrapers["tenpo"] = TenpoScraper()
        logger.info("Email-based scrapers enabled: mach, mercadopago, tenpo")

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
    "fintual":      {"hours": 6,  "label": "Fintual API (every 6h)"},
    "buda":         {"hours": 1,  "label": "Buda API (every 1h)"},
    "banchile":     {"hours": 24, "label": "BanChile (daily)"},
    "bci_lider":    {"hours": 24, "label": "BCI Lider (daily)"},
    "santander":    {"hours": 24, "label": "Santander (daily)"},
    "mach":         {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "MACH email (every 30m)"},
    "mercadopago":  {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "MercadoPago email (every 30m)"},
    "tenpo":        {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "Tenpo email (every 30m)"},
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


def _make_control_handler(
    scheduler: AsyncIOScheduler,
    scraper_keys: set[str],
    heartbeat: Heartbeat,
    cooldowns: dict[str, Callable[[], float | None]] | None = None,
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
    job, so a trigger during it isn't absorbed (BanChile's login gate still
    stops a second login).

    GET /health reports the scheduler, not just this thread: it answers 503
    when `heartbeat` has gone stale, which is what the compose healthcheck
    reads.
    """
    cooldowns = cooldowns or {}

    def seconds_left(slug: str) -> int:
        retry_after = cooldowns.get(slug)
        wait = retry_after() if retry_after else None
        return math.ceil(wait) if wait and wait > 0 else 0

    def trigger(slug: str) -> bool:
        job = scheduler.get_job(slug)
        if job is None:
            return False
        job.modify(next_run_time=datetime.now(timezone.utc))
        logger.info("Manual trigger for %s", slug)
        return True

    class ControlHandler(BaseHTTPRequestHandler):
        def _send(
            self, code: int, payload: dict, headers: dict[str, str] | None = None
        ) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                age = round(heartbeat.age())
                if age < _HEARTBEAT_STALE_SECONDS:
                    self._send(200, {"status": "ok", "heartbeat_age_seconds": age})
                else:
                    self._send(503, {"status": "stale", "heartbeat_age_seconds": age})
            elif self.path == "/scrapers":
                # Which scrapers exist is decided at startup by env vars
                # (build_scrapers()), so expose the list for the dashboard
                # instead of letting it hardcode a stale copy. Advertise only
                # slugs with a scheduler job: POST /refresh/{slug} 404s on the
                # rest (no _SCHEDULES entry ⇒ never registered), and the two
                # endpoints must agree on the set.
                enabled = sorted(s for s in scraper_keys if scheduler.get_job(s))
                self._send(200, {"scrapers": enabled})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/refresh":
                triggered: list[str] = []
                skipped: list[dict] = []
                for slug in sorted(s for s in scraper_keys if scheduler.get_job(s)):
                    wait = seconds_left(slug)
                    if wait:
                        logger.info(
                            "Manual trigger for %s skipped: cooldown, %ds left", slug, wait
                        )
                        skipped.append({"slug": slug, "retry_after_seconds": wait})
                    elif trigger(slug):
                        triggered.append(slug)
                self._send(202, {"triggered": triggered, "skipped": skipped})
            elif self.path.startswith("/refresh/"):
                slug = self.path[len("/refresh/") :]
                wait = seconds_left(slug) if scheduler.get_job(slug) else 0
                if wait:
                    logger.info(
                        "Manual trigger for %s refused: cooldown, %ds left", slug, wait
                    )
                    self._send(
                        429,
                        {
                            "error": f"{slug} is cooling down; retry in {wait}s",
                            "skipped": [{"slug": slug, "retry_after_seconds": wait}],
                        },
                        {"Retry-After": str(wait)},
                    )
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
    scheduler: AsyncIOScheduler, scraper_keys: set[str], heartbeat: Heartbeat
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
    handler = _make_control_handler(
        scheduler, scraper_keys, heartbeat, _REFRESH_COOLDOWNS
    )
    server = ThreadingHTTPServer((host, int(port)), handler)
    thread = threading.Thread(
        target=server.serve_forever, name="scraper-control", daemon=True
    )
    thread.start()
    logger.info(
        "Control server listening on %s:%s "
        "(POST /refresh[/{slug}], GET /scrapers, GET /health)",
        host,
        port,
    )
    return server


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
    scrapers = build_scrapers()

    if not scrapers:
        logger.error("No scrapers configured. Exiting.")
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # AsyncIOScheduler.start() (apscheduler 3.11+) binds to the *running* loop
    # unless one is passed explicitly. We start it below before the loop is
    # running, so hand it our loop up front.
    scheduler = AsyncIOScheduler(event_loop=loop)

    for key, scraper in scrapers.items():
        cfg = _SCHEDULES.get(key)
        if not cfg:
            logger.warning("No schedule configured for %s; skipping", key)
            continue
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
    logger.info("Scheduler started. Running initial scrape...")

    control_server = _start_control_server(scheduler, set(scrapers), heartbeat)

    loop.run_until_complete(run_all_once(scrapers))

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
