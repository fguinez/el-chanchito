"""Parse a percent figure as scraped pages print it ("1,23 %", "4.50")."""

import re
from decimal import Decimal
from typing import Any

_MINUS_SIGNS = "-" + chr(0x2212)
_PERCENT_RE = re.compile(r"^\s*([" + re.escape(_MINUS_SIGNS) + r"]?)(\d+(?:[.,]\d+)?)\s*%?\s*$")


def parse_percent(raw: Any, *, signed: bool = False) -> Decimal | None:
    """A lone percent figure with either decimal separator as a Decimal, or None.

    The "%" is optional. A leading minus (ASCII or U+2212) is read only when
    `signed`; otherwise a signed figure is None.
    """
    if not isinstance(raw, str):
        return None
    match = _PERCENT_RE.match(raw)
    if match is None or (match.group(1) and not signed):
        return None
    value = Decimal(match.group(2).replace(",", "."))
    return -value if match.group(1) else value
