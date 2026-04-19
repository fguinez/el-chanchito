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
from db.writer import start_scraper_run, finish_scraper_run, upsert_transactions, upsert_balance
from scrapers.base import BaseScraper
from scrapers.fintual import FintualScraper
from scrapers.buda import BudaScraper
from scrapers.banchile import BanChileScraper
from scrapers.email_parser import EmailParserScraper
from scrapers.bci_lider import BciLiderScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("scraper-service")


async def run_scraper(scraper: BaseScraper) -> None:
    """Run a single scraper with logging and DB tracking."""
    run_id = start_scraper_run(scraper.name)
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

    if os.environ.get("FINTUAL_EMAIL") and os.environ.get("FINTUAL_TOKEN"):
        scrapers["fintual"] = FintualScraper()
        logger.info("Fintual scraper enabled")

    if os.environ.get("BUDA_API_KEY") and os.environ.get("BUDA_API_SECRET"):
        scrapers["buda"] = BudaScraper()
        logger.info("Buda scraper enabled")

    if os.environ.get("BANCHILE_RUT") and os.environ.get("BANCHILE_PASSWORD"):
        scrapers["banchile"] = BanChileScraper()
        logger.info("BanChile scraper enabled")

    if os.environ.get("EMAIL_IMAP_HOST") and os.environ.get("EMAIL_IMAP_USER") and os.environ.get("EMAIL_IMAP_PASSWORD"):
        scrapers["email"] = EmailParserScraper()
        logger.info("Email parser enabled")

    return scrapers


async def run_all_once(scrapers: dict[str, BaseScraper]) -> None:
    """Run all configured scrapers once."""
    if not scrapers:
        logger.warning("No scrapers configured. Set env vars to enable them.")
        return

    logger.info("Running %d scrapers", len(scrapers))
    for scraper in scrapers.values():
        await run_scraper(scraper)


def main_scheduled() -> None:
    """Run scrapers on a schedule using APScheduler."""
    scrapers = build_scrapers()

    if not scrapers:
        logger.error("No scrapers configured. Exiting.")
        sys.exit(1)

    scheduler = AsyncIOScheduler()

    # Schedule each scraper at its own interval
    if "fintual" in scrapers:
        scheduler.add_job(
            run_scraper,
            IntervalTrigger(hours=6),
            args=[scrapers["fintual"]],
            id="fintual",
            name="Fintual API (every 6h)",
        )

    if "buda" in scrapers:
        scheduler.add_job(
            run_scraper,
            IntervalTrigger(hours=1),
            args=[scrapers["buda"]],
            id="buda",
            name="Buda API (every 1h)",
        )

    if "banchile" in scrapers:
        scheduler.add_job(
            run_scraper,
            IntervalTrigger(hours=24),
            args=[scrapers["banchile"]],
            id="banchile",
            name="BanChile (daily)",
        )

    if "email" in scrapers:
        scheduler.add_job(
            run_scraper,
            IntervalTrigger(minutes=30),
            args=[scrapers["email"]],
            id="email",
            name="Email parser (every 30m)",
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down (signal %s)...", signum)
        scheduler.shutdown(wait=False)
        close_pool()
        loop.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    scheduler.start()
    logger.info("Scheduler started. Running initial scrape...")

    # Run all scrapers immediately on startup
    loop.run_until_complete(run_all_once(scrapers))

    logger.info("Initial scrape complete. Scheduler running...")
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
        close_pool()
        logger.info("Scraper service stopped.")


def main_once() -> None:
    """Run all scrapers once and exit (for cron-based scheduling)."""
    scrapers = build_scrapers()
    asyncio.run(run_all_once(scrapers))
    close_pool()


if __name__ == "__main__":
    mode = os.environ.get("SCRAPER_MODE", "scheduled")
    if mode == "once":
        main_once()
    else:
        main_scheduled()
