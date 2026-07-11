"""Scraper service entry point with APScheduler."""

import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db.connection import close_pool
from db.writer import (
    finish_scraper_run,
    start_scraper_run,
    upsert_balance,
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
    """Run a single scraper with logging and DB tracking."""
    run_id = start_scraper_run(scraper.method, scraper.institution)
    logger.info("Starting scraper: %s (run=%s)", scraper.name, run_id)

    try:
        transactions = await scraper.scrape_transactions()
        inserted = upsert_transactions(transactions)

        balances = await scraper.scrape_balances()
        for balance in balances:
            upsert_balance(balance)

        logger.info(
            "Scraper %s: %d transactions (%d new), %d balances",
            scraper.name,
            len(transactions),
            inserted,
            len(balances),
        )
        finish_scraper_run(run_id, "success", transactions_imported=inserted)

    except Exception as e:
        logger.exception("Scraper %s failed", scraper.name)
        finish_scraper_run(run_id, "error", error_message=str(e))


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


def main_scheduled() -> None:
    """Run scrapers on a schedule using APScheduler."""
    scrapers = build_scrapers()

    if not scrapers:
        logger.error("No scrapers configured. Exiting.")
        sys.exit(1)

    scheduler = AsyncIOScheduler()

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
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

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

    loop.run_until_complete(run_all_once(scrapers))

    logger.info("Initial scrape complete. Scheduler running...")
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
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
