"""A file that says "a human must sign in again" and stops runs until they do.

The marker holds the ISO UTC time it was written. Every call is best-effort:
a filesystem error is logged, never raised, so the marker can never be the
reason a run fails.
"""

import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def mark(path: Path) -> None:
    """Write the marker with the current UTC time, private (0600) and whole."""
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    except OSError:
        logger.warning("Could not write the sign-in marker %s", path)
        return
    try:
        with os.fdopen(fd, "w") as f:
            f.write(datetime.now(UTC).isoformat())
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        logger.warning("Could not write the sign-in marker %s", path)


def marked_since(path: Path) -> str | None:
    """When the marker was written, or None when there is none.

    An unreadable or empty marker still counts as marked, with an empty
    timestamp: the safe side is to keep skipping until a sign-in clears it.
    """
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Could not read the sign-in marker %s", path)
        return ""


def clear(path: Path) -> None:
    """Remove the marker, if any."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove the sign-in marker %s", path)
