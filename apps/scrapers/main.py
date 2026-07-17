"""Scraper service entry point with APScheduler."""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db.connection import close_pool
from db.writer import (
    finish_scraper_run,
    start_scraper_run,
    upsert_product,
    upsert_transactions,
)
from scrapers.backends.email import get_session as get_email_session
from scrapers.base import BaseScraper
from scrapers.institutions import (
    BanChileScraper,
    BciLiderScraper,
    BudaScraper,
    FintualScraper,
    MachScraper,
    MercadoPagoScraper,
    TenpoScraper,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("scraper-service")


async def run_scraper(scraper: BaseScraper) -> None:
    """Run a single scraper with logging and DB tracking.

    Transactions and products are scraped as independent legs: a failure in one
    must not stop the other. BanChile in particular scrapes transactions via the
    (flaky) fintself browser login and products via its own login, so a fintself
    timeout must still leave the balances refreshable — and vice versa.

    The products leg can also report non-fatal warnings (e.g. a BanChile
    surface that failed all its retries): a run with warnings but no errors is
    recorded as `partial`, with the warnings as its message.
    """
    run_id = start_scraper_run(scraper.method, scraper.institution)
    logger.info("Starting scraper: %s (run=%s)", scraper.name, run_id)

    errors: list[str] = []
    warnings: list[str] = []
    n_tx = 0
    inserted = 0
    n_prod = 0

    try:
        transactions = await scraper.scrape_transactions()
        n_tx = len(transactions)
        inserted = upsert_transactions(transactions)
    except Exception as e:
        logger.exception("Scraper %s: transactions failed", scraper.name)
        errors.append(f"transactions: {e}")

    try:
        result = await scraper.scrape_products()
        n_prod = len(result.products)
        warnings.extend(result.warnings)
        for sp in result.products:
            upsert_product(sp)
    except Exception as e:
        logger.exception("Scraper %s: products failed", scraper.name)
        errors.append(f"products: {e}")

    logger.info(
        "Scraper %s: %d transactions (%d new), %d products",
        scraper.name,
        n_tx,
        inserted,
        n_prod,
    )
    if errors:
        finish_scraper_run(
            run_id, "error", transactions_imported=inserted,
            error_message="; ".join(errors + warnings),
        )
    elif warnings:
        finish_scraper_run(
            run_id, "partial", transactions_imported=inserted,
            error_message="; ".join(warnings),
        )
    else:
        finish_scraper_run(run_id, "success", transactions_imported=inserted)


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
    "mach":         {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "MACH email (every 30m)"},
    "mercadopago":  {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "MercadoPago email (every 30m)"},
    "tenpo":        {"minutes": _EMAIL_INTERVAL_MINUTES, "label": "Tenpo email (every 30m)"},
}


def _make_control_handler(scheduler: AsyncIOScheduler, scraper_keys: set[str]):
    """Build the HTTP handler for the internal scraper control server.

    Triggering a scrape means moving a scheduled job's next run time to now:
    APScheduler then runs it on the scheduler's own event loop, reusing the
    job's `coalesce` / `max_instances=1` guards so a manual trigger can't
    overlap a scheduled or in-flight run. `job.modify()` is thread-safe, so
    it's fine to call from this handler's thread.
    """

    def trigger(slug: str) -> bool:
        job = scheduler.get_job(slug)
        if job is None:
            return False
        job.modify(next_run_time=datetime.now(timezone.utc))
        logger.info("Manual trigger for %s", slug)
        return True

    class ControlHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/refresh":
                triggered = sorted(s for s in scraper_keys if trigger(s))
                self._send(202, {"triggered": triggered})
            elif self.path.startswith("/refresh/"):
                slug = self.path[len("/refresh/") :]
                if trigger(slug):
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
    scheduler: AsyncIOScheduler, scraper_keys: set[str]
) -> ThreadingHTTPServer | None:
    """Start the internal HTTP control server when SCRAPER_CONTROL_PORT is set.

    This is an unauthenticated trigger meant to be reachable only from the
    dashboard over the private container network (or localhost in host-dev).
    Never publish the port publicly — see issue #23.
    """
    port = os.environ.get("SCRAPER_CONTROL_PORT")
    if not port:
        return None

    handler = _make_control_handler(scheduler, scraper_keys)
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), handler)
    thread = threading.Thread(
        target=server.serve_forever, name="scraper-control", daemon=True
    )
    thread.start()
    logger.info("Control server listening on :%s (POST /refresh[/{slug}])", port)
    return server


def main_scheduled() -> None:
    """Run scrapers on a schedule using APScheduler."""
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

    control_server = _start_control_server(scheduler, set(scrapers))

    loop.run_until_complete(run_all_once(scrapers))

    logger.info("Initial scrape complete. Scheduler running...")
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
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
