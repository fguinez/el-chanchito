"""Agnostic wrapper around the `fintself` library.

Provides a single helper that runs fintself's synchronous Playwright scraper
in a thread executor, so institution-specific consumers (e.g. BanChile) can
stay focused on mapping `MovementModel` instances to `ScrapedTransaction`s.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


def _force_new_headless() -> None:
    """Make Playwright launch Chromium via the "chromium" channel.

    Playwright's default headless uses the stripped-down headless shell,
    which Banco de Chile serves a degraded post-login page (no "Mis
    Productos" menu — see fintself#28). The "chromium" channel runs the
    full browser binary in new-headless mode, which behaves like a headed
    session. Falls back to the default launch on Playwright versions
    without channel support (<1.49).
    """
    from playwright.sync_api import BrowserType

    if getattr(BrowserType.launch, "_new_headless_patch", False):
        return

    original_launch = BrowserType.launch

    def launch(self, **kwargs):
        kwargs.setdefault("channel", "chromium")
        try:
            return original_launch(self, **kwargs)
        except Exception:
            kwargs.pop("channel", None)
            return original_launch(self, **kwargs)

    launch._new_headless_patch = True
    BrowserType.launch = launch


async def run_fintself_scraper(bank_key: str, user: str, password: str) -> list:
    """Run fintself for `bank_key` and return its list of MovementModel.

    Raises ImportError (to be handled by the caller) if `fintself` isn't
    installed. Callers are responsible for converting the returned movements
    into `ScrapedTransaction` shape.
    """
    from fintself import get_scraper  # imported here so tests don't require it

    _force_new_headless()
    scraper = get_scraper(bank_key, headless=True)

    logger.info("Starting fintself scrape: bank=%s", bank_key)

    loop = asyncio.get_event_loop()
    movements = await loop.run_in_executor(None, scraper.scrape, user, password)

    logger.info("fintself[%s] returned %d movements", bank_key, len(movements))
    return movements
