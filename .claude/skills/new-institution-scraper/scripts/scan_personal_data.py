#!/usr/bin/env python3
"""Scan added lines for personal or financial data before they are committed.

Implements the repo's pre-commit rule: grep the diff for CLP-formatted
amounts, account numbers, RUTs, card numbers and the like, and confirm every
hit is synthetic. Values from the canonical synthetic set pass silently;
anything else is printed for review.

Usage (from anywhere inside the repo):
    scan_personal_data.py                      # staged changes (default)
    scan_personal_data.py --range main...HEAD  # everything this branch adds
    scan_personal_data.py --log main..HEAD     # this branch's commit messages
    scan_personal_data.py --paths FILE...      # whole files (fixtures, a PR body)

Hits are tiered: canonical synthetic values pass silently; round or
boundary-test figures and institution e-mail addresses print as `note`
(likely synthetic, still glance at them); everything else prints as
`REVIEW` and fails the scan.

Exit status: 0 when nothing needs review, 1 when a REVIEW hit must be checked,
2 on git errors. Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

# 1235 is the canonical USD 1.234,56 rounded to whole units.
CANONICAL_CLP = {"2500000", "1000000", "999999", "1235"}
CANONICAL_USD = {"1.234,56"}
CANONICAL_LAST4 = {"1234", "0000"}
SYNTHETIC_DOMAINS = ("example.com", "example.org", "example.net", "anthropic.com")
PERSONAL_DOMAINS = (
    "gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "uc.cl", "uchile.cl",
)
SYNTHETIC_SECRET_PREFIXES = (
    "synthetic", "test", "not-a-secret", "fake", "dummy", "example", "changeme",
    "${", "<", "$(", "{", "http", "#", "/", ".",
)
# Keys that hold counts, not money, even when they say "total" or "limit".
NON_MONEY_KEY = re.compile(r"(?i)page|count|size|per_?|items|rows|offset|retries|timeout")


OK, NOTE, REVIEW = "ok", "note", "review"


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _obviously_fake(digits: str) -> bool:
    """Repdigits (1111111) or ascending/descending runs (1234567, 7654321)."""
    if not digits or len(set(digits)) == 1:
        return True
    return digits in "1234567890" or digits in "9876543210"


def _roundish(digits: str) -> bool:
    """Round or boundary-test figures (1800000, 2500, 1099999, 250001): likely, not surely, fake."""
    return (
        re.fullmatch(r"[1-9]\d{0,2}0{2,}", digits) is not None
        or re.fullmatch(r"[1-9]\d?(?:0{3,}|9{3,})\d{0,2}", digits) is not None
    )


def _figure(digits: str, canonical: set[str]) -> str:
    if digits in canonical or _obviously_fake(digits):
        return OK
    return NOTE if _roundish(digits) else REVIEW


def _clp(match: str) -> str:
    return _figure(_digits(match), CANONICAL_CLP)


def _usd(match: str) -> str:
    number = re.sub(r"^(?:USD|US\$)\s?-?", "", match)
    if number in CANONICAL_USD:
        return OK
    return _figure(_digits(number), set())


def _account(match: str) -> str:
    return OK if re.fullmatch(r"00-000-00000-0\d?", match) else REVIEW


def _rut(match: str) -> str:
    return OK if _obviously_fake(_digits(match)[:-1]) else REVIEW


def _card(match: str) -> str:
    digits = _digits(match)
    return OK if _obviously_fake(digits) or digits.count("0") >= 10 else REVIEW


def _keyed_account(match: str) -> str:
    """An account-like number assigned to an account-ish key."""
    key, _, number = re.split(r"([:=])", match, maxsplit=1)
    number = number.strip(" '\"")
    if re.search(r"(?i)date|fecha|time|_at$", key) or re.match(r"\d{4}-\d{2}-\d{2}", number):
        return OK
    if number.startswith("00-000-00000-0") or _obviously_fake(_digits(number)):
        return OK
    return REVIEW


def _token(match: str) -> str:
    """Bearer tokens, JWTs and cookie values pass only when visibly synthetic or templated."""
    lowered = match.lower()
    return OK if "{" in match or "synthetic" in lowered or "test" in lowered else REVIEW


def _last4(match: str) -> str:
    last4 = _digits(match)[-4:]
    return OK if last4 in CANONICAL_LAST4 or _obviously_fake(last4) else REVIEW


def _email(match: str) -> str:
    domain = match.rsplit("@", 1)[-1].lower()
    if domain.endswith(SYNTHETIC_DOMAINS) or domain.endswith("users.noreply.github.com"):
        return OK
    return REVIEW if domain in PERSONAL_DOMAINS else NOTE


def _secret(match: str) -> str:
    """Synthetic values pass; unquoted values pass only when they read as code.

    `self.password`, `os.environ[...]` and `FINTUAL_PASSWORD` (an env name) are
    code. A quoted value is a literal, so only the synthetic prefixes save it:
    an all-letters quoted password is still flagged.
    """
    raw = re.split(r"[:=]\s*", match, maxsplit=1)[-1]
    quoted = raw[:1] in ("'", '"', "`")
    value = raw.strip("'\"` ").rstrip(".,;:")
    if value.lower().startswith(SYNTHETIC_SECRET_PREFIXES):
        return OK
    if not quoted and (
        "(" in value
        or "[" in value
        or re.fullmatch(r"[a-z_$][A-Za-z0-9_$]*", value)
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", value)
        or re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[\w$]+)+", value)
    ):
        return OK
    return REVIEW


def _keyed_amount(match: str) -> str:
    """A bare number assigned to a money-like key: `"balance": <digits>`."""
    key, _, number = re.split(r"([:=])", match, maxsplit=1)
    if NON_MONEY_KEY.search(key):
        return OK
    number = re.sub(r"^\s*(?:Decimal|int|float)?\(?['\"]?", "", number)
    number = number.rstrip("'\" )")
    split = re.fullmatch(r"-?(\d+)[.,](\d+)", number)
    if split and (len(split.group(1)) > 3 or len(split.group(2)) != 3):
        number = split.group(1)
    digits = _digits(number)
    if len(digits) < 4:
        return OK
    return _figure(digits, CANONICAL_CLP)


def _phone(match: str) -> str:
    return OK if _obviously_fake(_digits(match)[-8:]) else REVIEW


@dataclass(frozen=True)
class Rule:
    category: str
    pattern: re.Pattern[str]
    classify: Callable[[str], str]


# Specific patterns first: a span an earlier rule matched is not re-reported.
RULES = [
    Rule("account-number", re.compile(r"\b\d{2}-\d{3}-\d{5}-\d{1,2}\b"), _account),
    Rule("rut", re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b"), _rut),
    Rule("card-number", re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b|\b\d{16}\b"), _card),
    Rule(
        "card-last4",
        re.compile(
            r"(?:\*{2,}[\s-]?|[Xx•]{4}[\s-]?|termina(?:da)?\s+en\s+|ending\s+in\s+|last4\W{1,4})\d{4}\b"
        ),
        _last4,
    ),
    Rule("phone", re.compile(r"\+?56\s?9\s?\d{4}\s?\d{4}\b"), _phone),
    Rule(
        "account-number",
        re.compile(
            r"(?i)\b\w*(?:cuenta|account|acct|numero|nro)\w*['\"]?\s*[:=]\s*['\"]?\d[\d-]{7,}"
        ),
        _keyed_account,
    ),
    Rule(
        "token",
        re.compile(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
            r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
            r"|\bCookie['\"]?\s*:\s*['\"]?[^'\"\s;=]+=[^'\"\s;]{12,}"
        ),
        _token,
    ),
    Rule(
        "secret",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_]*(?:password|passwd|secret|token|api[_-]?key)"
            r"[A-Za-z0-9_]*['\"]?\s*[:=]\s*['\"]?[^'\"\s,;)}\]]{8,}"
        ),
        _secret,
    ),
    Rule("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), _email),
    Rule("usd-amount", re.compile(r"\b(?:USD|US\$)\s?-?\d{1,3}(?:\.\d{3})*,\d{2}\b"), _usd),
    Rule("clp-amount", re.compile(r"\$\s?-?\d{1,3}(?:\.\d{3})+(?:,\d+)?"), _clp),
    Rule("clp-amount", re.compile(r"\$\s?-?\d{4,}\b"), _clp),
    Rule("clp-amount", re.compile(r"\bCLP\s?-?\d{1,3}(?:[.,]\d{3})+\b"), _clp),
    Rule("clp-amount", re.compile(r"\bCLP\s?-?\d{4,}\b"), _clp),
    Rule(
        "keyed-amount",
        re.compile(
            r"(?i)\b\w*(?:amount|balance|saldo|monto|cupo|disponible|limit|total|nav|deposited"
            r"|profit|owed|available|principal)\w*['\"]?\s*[:=]\s*"
            r"(?:Decimal|int|float)?\(?['\"]?-?\d[\d._,]*\d"
        ),
        _keyed_amount,
    ),
    Rule("dotted-figure", re.compile(r"(?<![\d.$])\d{1,3}(?:\.\d{3}){2,}(?![\d.])"), _clp),
]


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    category: str
    text: str
    blocking: bool


def _scan_line(path: str, line_no: int, line: str) -> Iterator[Hit]:
    """Hits on one line; a span already reported by an earlier rule is not repeated."""
    seen: list[tuple[int, int]] = []
    for rule in RULES:
        for match in rule.pattern.finditer(line):
            start, end = match.span()
            if any(start < e and s < end for s, e in seen):
                continue
            seen.append((start, end))
            verdict = rule.classify(match.group(0))
            if verdict != OK:
                yield Hit(path, line_no, rule.category, match.group(0), verdict == REVIEW)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def _diff_added_lines(diff_args: list[str]) -> Iterator[tuple[str, int, str]]:
    """(path, new line number, text) for every added line of a git diff.

    `+++ ` names the file only in a file header (between `diff --git` and the
    first hunk), so an added line whose content starts with `++` is still scanned.
    """
    path = ""
    line_no = 0
    in_header = False
    for raw in _git("diff", "--no-color", "-U0", *diff_args).splitlines():
        if raw.startswith("diff --git "):
            in_header = True
        elif in_header:
            if raw.startswith("+++ "):
                path = (raw[6:] if raw.startswith("+++ b/") else raw[4:]).rstrip("\t")
            elif raw.startswith("@@"):
                in_header = False
        if raw.startswith("@@"):
            found = re.search(r"\+(\d+)", raw)
            line_no = int(found.group(1)) if found else 0
        elif not in_header and raw.startswith("+"):
            yield path, line_no, raw[1:]
            line_no += 1


def _commit_message_lines(rev_range: str) -> Iterator[tuple[str, int, str]]:
    """(commit label, line number, text) for every line of each commit message."""
    log = _git("log", "--format=%x1e%h%n%B", rev_range)
    for entry in log.split("\x1e"):
        if not entry.strip():
            continue
        sha, _, body = entry.partition("\n")
        for number, line in enumerate(body.splitlines(), start=1):
            yield f"commit {sha}", number, line


def _file_lines(paths: Iterable[str]) -> Iterator[tuple[str, int, str]]:
    for name in paths:
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except (IsADirectoryError, FileNotFoundError) as e:
            print(f"skip {name}: {e.__class__.__name__}", file=sys.stderr)
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            yield name, number, line


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--staged", action="store_true", help="scan the staged diff (default)")
    source.add_argument("--range", help="scan the added lines of a git revision range")
    source.add_argument("--log", help="scan the commit messages of a git revision range")
    source.add_argument("--paths", nargs="+", help="scan whole files")
    args = parser.parse_args()

    if args.paths:
        lines = _file_lines(args.paths)
        scope = f"{len(args.paths)} file(s)"
    elif args.range:
        lines = _diff_added_lines([args.range])
        scope = f"range {args.range}"
    elif args.log:
        lines = _commit_message_lines(args.log)
        scope = f"commit messages {args.log}"
    else:
        lines = _diff_added_lines(["--cached"])
        scope = "staged changes"

    try:
        hits = [hit for path, number, line in lines for hit in _scan_line(path, number, line)]
    except subprocess.CalledProcessError as e:
        print(f"git failed: {e.stderr.strip()}", file=sys.stderr)
        return 2

    blocking = [h for h in hits if h.blocking]
    for hit in hits:
        marker = "REVIEW" if hit.blocking else "note"
        print(f"{hit.path}:{hit.line}: [{marker} {hit.category}] {hit.text}")
    if blocking:
        print(
            f"\n{len(blocking)} hit(s) in {scope} are not from the synthetic set. "
            "Replace each real value with a canonical synthetic one, or confirm it is synthetic."
        )
        return 1
    print(f"No personal-data patterns outside the synthetic set in {scope}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
