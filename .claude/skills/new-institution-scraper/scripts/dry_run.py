#!/usr/bin/env python3
"""Run one scraper's legs live without touching the database; print shapes only.

The output is counts, kinds, id prefixes and field presence, never amounts,
descriptions or ids, so it is safe to paste into a PR. It also checks the
invariants the writer relies on (unique ids, transactions landing on a
singleton product or on a product the run emits under the ref they name, no
duplicate product identities).

Run it with the user present (it logs in to the real institution) from the
repo root, with secrets loaded:

    . ./scripts/load-secrets.sh && .venv/bin/python \\
        .claude/skills/new-institution-scraper/scripts/dry_run.py <slug>

Options:
    --legs products               run only one leg (default: both, in run_scraper's order)
    --dump local/<slug>-1.json    also write the full envelopes (REAL values) under local/
    --compare local/<slug>-1.json report how many ids and product identities this run
                                  shares with an earlier dump (counts only): run twice,
                                  with separate logins, to prove the ids are stable

Exit status: 0 when every leg ran and every check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
APP_DIR = REPO_ROOT / "apps" / "scrapers"
LOCAL_DIR = REPO_ROOT / "local"
sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

import scrapers.institutions as institutions  # noqa: E402
from scrapers.base import BaseScraper, ScrapedProduct, ScrapedTransaction  # noqa: E402

# An institution tag plus at most one lowercase qualifier: `buda_`, `bch_op_`,
# `email_mach_`, `global66_`. Longer prefixes would start eating the id itself.
_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9]*_(?:[a-z]+_)?)")


def _scraper_class(slug: str) -> type[BaseScraper]:
    """The exported scraper whose `institution` (or class name) matches `slug`."""
    for name in institutions.__all__:
        cls = getattr(institutions, name)
        if getattr(cls, "institution", None) == slug or name.lower() == slug.lower():
            return cls
    known = sorted(getattr(institutions, n).institution for n in institutions.__all__)
    raise SystemExit(f"unknown scraper {slug!r}; exported: {', '.join(known)}")


def _fields(model: Any) -> tuple[list[str], list[str]]:
    """(set, null) field names of a pydantic payload, discriminator excluded."""
    if model is None:
        return [], []
    data = model.model_dump()
    data.pop("kind", None)
    return (
        sorted(k for k, v in data.items() if v is not None),
        sorted(k for k, v in data.items() if v is None),
    )


def _summarize_transactions(txns: list[ScrapedTransaction]) -> None:
    ids = [t.external_id for t in txns]
    print(f"transactions: {len(txns)} (distinct external_id: {len(set(ids))})")
    if not txns:
        return
    by_target = Counter(
        f"{t.product_kind}/{t.currency}" + (" ref=<set>" if t.product_external_ref else "")
        for t in txns
    )
    prefixes = Counter((_PREFIX.match(i) or [None, "<no prefix>"])[1] for i in ids)
    signs = Counter("out" if t.amount < 0 else "in" if t.amount > 0 else "zero" for t in txns)
    dates = sorted({t.transaction_date for t in txns})
    print(f"  by product: {dict(by_target)}")
    print(f"  id prefixes: {dict(prefixes)}")
    print(f"  signs: {dict(signs)}")
    print(f"  dates: {len(dates)} distinct, span {(dates[-1] - dates[0]).days} days")
    print(
        "  accounting_date set: %d, scheduled_month set: %d, category_hint set: %d, "
        "internal_transfer: %d, empty descriptions: %d"
        % (
            sum(t.accounting_date is not None for t in txns),
            sum(t.scheduled_month is not None for t in txns),
            sum(t.category_hint is not None for t in txns),
            sum(t.internal_transfer for t in txns),
            sum(not t.description.strip() for t in txns),
        )
    )


def _summarize_products(products: list[ScrapedProduct], warnings: list[str]) -> None:
    print(f"products: {len(products)} (warnings: {len(warnings)})")
    for p in products:
        metrics_set, metrics_null = _fields(p.metrics)
        attrs_set, attrs_null = _fields(p.attributes)
        ref = "None" if p.external_ref is None else "<set>"
        print(
            f"  {p.kind}/{p.currency} ref={ref} name={'<set>' if p.name else 'None'} "
            f"metrics={type(p.metrics).__name__ if p.metrics else None} "
            f"set={metrics_set} null={metrics_null} attrs set={attrs_set} null={attrs_null}"
        )
    for w in warnings:
        print(f"  warning: {w}")


def _checks(
    institution: str,
    txns: list[ScrapedTransaction] | None,
    products: list[ScrapedProduct] | None,
) -> tuple[list[str], list[str]]:
    """Writer invariants this run breaks, as human-readable failures, plus
    notes on rows the writer may not land (not failures)."""
    failures: list[str] = []
    notes: list[str] = []
    envelopes: list[Any] = [*(txns or []), *(products or [])]
    strays = {e.institution for e in envelopes} - {institution}
    if strays:
        failures.append(f"envelopes for another institution: {sorted(strays)}")
    if txns:
        ids = [t.external_id for t in txns]
        dupes = len(ids) - len(set(ids))
        if dupes:
            failures.append(f"{dupes} duplicate external_id(s): the writer keeps only the first")
        if any(not _PREFIX.match(i) for i in ids):
            failures.append("external_id without an institution prefix")
    if products:
        identities = Counter((p.kind, p.currency, p.external_ref) for p in products)
        dupes = sum(n - 1 for n in identities.values() if n > 1)
        if dupes:
            failures.append(f"{dupes} duplicate product identity(ies) (kind, currency, external_ref)")
        singletons = {(p.kind, p.currency) for p in products if p.external_ref is None}
        multi = {(p.kind, p.currency) for p in products if p.external_ref is not None}
        mixed = singletons & multi
        if mixed:
            failures.append(f"singleton and per-ref products share (kind, currency): {sorted(mixed)}")
        if txns:
            targets = {(t.product_kind, t.currency) for t in txns if t.product_external_ref is None}
            orphan = targets - singletons
            if orphan:
                failures.append(
                    "transactions target (kind, currency) with no singleton product in this "
                    f"run: {sorted(orphan)}; the writer files them on a product with no "
                    "external_ref, creating one if needed"
                )
            # A ref is looked up, never created: a row naming a product this run
            # doesn't emit lands only if an earlier run already stored it.
            emitted = set(identities)
            waiting = sum(
                (t.product_kind, t.currency, t.product_external_ref) not in emitted
                for t in txns
                if t.product_external_ref is not None
            )
            if waiting:
                notes.append(
                    f"{waiting} transaction(s) name a product (kind, currency, external_ref) "
                    "this run does not emit: written only if it is already stored"
                )
    return failures, notes


def _local_path(path_arg: str, flag: str) -> Path:
    path = (REPO_ROOT / path_arg).resolve()
    if LOCAL_DIR.resolve() not in path.parents:
        raise SystemExit(f"{flag} must point inside {LOCAL_DIR} (gitignored); got {path}")
    return path


def _compare(path: Path, txns: list | None, products: list | None) -> None:
    """Overlap with an earlier dump, as counts: stable ids overlap heavily."""
    previous = json.loads(path.read_text())
    if txns is not None:
        before = {t["external_id"] for t in previous.get("transactions", [])}
        now = {t.external_id for t in txns}
        print(
            f"compare: external_id shared {len(before & now)}, only now {len(now - before)}, "
            f"only before {len(before - now)}"
        )
    if products is not None:
        def ident(kind: str, currency: str, ref: str | None) -> tuple:
            return (kind, currency, ref)

        before_p = {
            ident(p["kind"], p["currency"], p.get("external_ref"))
            for p in previous.get("products", [])
        }
        now_p = {ident(p.kind, p.currency, p.external_ref) for p in products}
        print(
            f"compare: product identities shared {len(before_p & now_p)}, "
            f"only now {len(now_p - before_p)}, only before {len(before_p - now_p)}"
        )


def _dump(path: Path, txns: list | None, products: list | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "transactions": [t.model_dump(mode="json") for t in txns or []],
        "products": [p.model_dump(mode="json") for p in products or []],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    path.chmod(0o600)
    print(f"full envelopes (real values) written to {path.relative_to(REPO_ROOT)}")


async def _run(scraper: BaseScraper, legs: set[str]) -> tuple[Any, Any, list[str], bool]:
    txns = products = None
    warnings: list[str] = []
    ok = True
    if "transactions" in legs:
        try:
            txns = await scraper.scrape_transactions()
        except Exception as e:  # noqa: BLE001 - report and keep going, like run_scraper
            ok = False
            print(f"transactions leg FAILED: {type(e).__name__}: {e}")
    if "products" in legs:
        try:
            result = await scraper.scrape_products()
            products, warnings = result.products, result.warnings
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"products leg FAILED: {type(e).__name__}: {e}")
    return txns, products, warnings, ok


def _close_email_session() -> None:
    try:
        from scrapers.backends.email import get_session

        get_session().close()
    except Exception:  # noqa: BLE001 - never initialised, or already closed
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slug", help="institution slug (e.g. buda) or class name")
    parser.add_argument("--legs", default="transactions,products")
    parser.add_argument("--dump", help="write full envelopes to this path under local/")
    parser.add_argument("--compare", help="an earlier --dump to compare ids against (counts only)")
    args = parser.parse_args()

    legs = {leg.strip() for leg in args.legs.split(",") if leg.strip()}
    if not legs <= {"transactions", "products"}:
        raise SystemExit("--legs takes transactions and/or products")

    # Validate output paths before logging in: a typo must not cost a login.
    dump_path = _local_path(args.dump, "--dump") if args.dump else None
    compare_path = _local_path(args.compare, "--compare") if args.compare else None
    if compare_path is not None and not compare_path.is_file():
        raise SystemExit(f"--compare file not found: {compare_path}")

    cls = _scraper_class(args.slug)
    try:
        scraper = cls()
    except KeyError as e:
        raise SystemExit(f"{cls.__name__} needs env var {e}; load .env and the Keychain secrets")
    print(f"dry run: {scraper.name} (legs: {', '.join(sorted(legs))}); nothing is written to the DB")

    try:
        txns, products, warnings, ok = asyncio.run(_run(scraper, legs))
    finally:
        _close_email_session()

    if txns is not None:
        _summarize_transactions(txns)
    if products is not None:
        _summarize_products(products, warnings)
    superseded = scraper.superseded_transactions() if txns is not None else []
    if superseded:
        print(f"superseded rows the runner would delete: {len(superseded)}")

    failures, notes = _checks(scraper.institution, txns, products)
    for note in notes:
        print(f"note: {note}")
    for failure in failures:
        print(f"CHECK FAILED: {failure}")
    if not failures and ok:
        print("all checks passed")
    if compare_path is not None:
        _compare(compare_path, txns, products)
    if dump_path is not None:
        _dump(dump_path, txns, products)
    return 0 if ok and not failures else 1


if __name__ == "__main__":
    sys.exit(main())
