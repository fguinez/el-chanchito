"""Institution-unique product slugs.

Pure string helpers, no DB access. The web resolver implements the same
canonical spec in TypeScript (apps/web/src/lib/db/slug.ts) so both product
creation paths mint identical slugs; keep the two in sync.
"""

import re
import unicodedata


def slugify(name: str, kind: str) -> str:
    """URL-safe slug for a product name.

    NFKD-normalize and drop non-ASCII (folding accents, e.g. "crédito" ->
    "credito"), lowercase, then collapse every run outside [a-z0-9] into a
    single hyphen. A name with nothing to keep (e.g. symbols only) falls back
    to the kind with underscores hyphenated, which is never empty.
    """
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or kind.replace("_", "-")


def unique_slug(base: str, taken: set[str]) -> str:
    """`base` if free, else the first free `base-n` for n = 2, 3, 4, ..."""
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"
