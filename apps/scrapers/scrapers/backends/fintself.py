"""Agnostic wrapper around the `fintself` library.

Provides a single helper that runs fintself's synchronous Playwright scraper
in a thread executor, so institution-specific consumers (e.g. BanChile) can
stay focused on mapping `MovementModel` instances to `ScrapedTransaction`s.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_fintself_scraper(bank_key: str, user: str, password: str) -> list:
    """Run fintself for `bank_key` and return its list of MovementModel.

    Raises ImportError (to be handled by the caller) if `fintself` isn't
    installed. Callers are responsible for converting the returned movements
    into `ScrapedTransaction` shape.
    """
    from fintself import get_scraper  # imported here so tests don't require it

    scraper = get_scraper(bank_key)
    scraper.user = user
    scraper.password = password
    scraper.headless = True

    logger.info("Starting fintself scrape: bank=%s", bank_key)

    loop = asyncio.get_event_loop()
    movements = await loop.run_in_executor(None, scraper.scrape)

    logger.info("fintself[%s] returned %d movements", bank_key, len(movements))
    return movements
