"""Agnostic IMAP email-parsing backend.

Provides a process-wide `ImapSession` with NOOP keepalive and automatic
reconnection so multiple per-institution scrapers can share a single live
login, plus a `fetch_transactions_for_pattern` helper that institution
scrapers call with their own `EmailPattern`.

imaplib is synchronous, so every IMAP round trip runs in a worker thread
(`asyncio.to_thread`) while the session lock is held: the email scrapers
still take turns on the one connection, but the event loop stays free for
the other scrapers.
"""

import asyncio
import email
import email.message
import email.utils
import hashlib
import html
import imaplib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from email.header import decode_header
from typing import Optional
from zoneinfo import ZoneInfo

from scrapers.base import ScrapedTransaction

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Santiago")
DEFAULT_MAILBOX = "INBOX"
DEFAULT_MAX_MESSAGES = 100
IMAP_TIMEOUT_SECONDS = 60
_MAX_MERCHANT_LEN = 80

# A CLP figure such as "$ 2.500.000", "$999.999" or "$ 1.234,56"; group 1 is
# the number. `\s` also covers the non-breaking space HTML emails use, and
# the lookbehind leaves foreign figures such as "US$ 1.234,56" alone.
CLP_AMOUNT = r"(?<![A-Za-z])\$\s*(\d[\d.]*(?:,\d+)?)"

# The gap between an anchor and its figure: any characters but "$" or a
# sentence end (".", "!" or "?" before a capitalised word), so "Tu compra fue
# aprobada. Gana $ N" never pairs the purchase with the promo, while
# "COMERCIO S.A. por $ N" still does.
_ANCHOR_GAP = r"(?:(?![.!?]\s+(?-i:[A-ZÁÉÍÓÚÑ¡¿]))[^$]){0,60}?"

# Lookahead that ends a merchant capture: " por", a sentence-ending period
# (not the dots inside "S.A."), a line break, or the end of the text.
MERCHANT_END = r"(?=\s+por\b|\.(?:\s|$)|\n|$)"


@dataclass(frozen=True)
class AmountRule:
    """An amount regex anchored to its context; group 1 is the figure.

    `income` marks anchors that only ever describe money coming in
    ("recibiste", "te devolvimos"); every other rule reads as an expense
    unless the subject says otherwise (see `EmailPattern.income_keywords`).
    """

    pattern: str
    income: bool = False


def anchored(phrases: str, income: bool = False) -> AmountRule:
    """Rule for the first CLP figure within 60 characters after `phrases`.

    `phrases` is a regex alternation of whole words ("pagaste|compraste").
    The gap may cross a line break (HTML tables put a label and its value in
    separate cells) but never another `$` or the end of a sentence, so the
    anchor claims the figure that follows it, not a promo banner further on.
    """
    return AmountRule(rf"\b(?:{phrases})\b{_ANCHOR_GAP}{CLP_AMOUNT}", income)


@dataclass
class EmailPattern:
    """Configuration for parsing a specific institution's emails."""

    institution: str
    product_kind: str
    sender_domains: list[str]         # From domain must contain one of these as whole labels
    subject_contains: list[str]       # match any of these in Subject (optional filter)
    amount_rules: list[AmountRule]    # the rule anchored earliest in the body wins
    merchant_patterns: list[str]      # regex patterns to extract merchant name
    income_keywords: list[str] = field(default_factory=list)  # in Subject: money came in
    mailbox: Optional[str] = None     # None: EMAIL_IMAP_MAILBOX, else INBOX
    max_messages: Optional[int] = None  # None: EMAIL_MAX_MESSAGES, else 100


def _decode_header_value(raw: str) -> str:
    """Decode an email header that may be encoded."""
    parts = decode_header(raw)
    decoded = []
    for content, charset in parts:
        if isinstance(content, bytes):
            decoded.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(content)
    return " ".join(decoded)


def _fold(text: str) -> str:
    """Lowercase and drop accents, so "Transacción" matches "transaccion"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _contains_any(text: str, keywords: list[str]) -> bool:
    """True when any keyword appears in `text`, ignoring case and accents."""
    folded = _fold(text)
    return any(_fold(k) in folded for k in keywords)


def _strip_html(html_text: str) -> str:
    """Convert an HTML body to plain-ish text.

    Drops <style>/<script> blocks first: their contents survive naive tag
    stripping and pollute merchant/amount regexes with CSS and URLs. Block
    boundaries become line breaks and entities are decoded, so "&#36;&nbsp;"
    reads as "$ " and a table cell never runs into the next one.
    """
    text = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", html_text)
    text = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|td|th|li|h[1-6]|table)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r" ?\n\s*", "\n", text).strip()


def _get_body(msg: email.message.Message) -> str:
    """Extract the plain text body from an email message.

    Prefers a text/plain part regardless of MIME ordering; falls back to
    stripped text/html.
    """
    plain_body: str | None = None
    html_body: str | None = None

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace")
        if ctype == "text/plain" and plain_body is None:
            plain_body = decoded
        elif ctype == "text/html" and html_body is None:
            html_body = decoded

    if plain_body:
        return plain_body
    if html_body:
        return _strip_html(html_body)
    return ""


def _to_clp(raw: str) -> Optional[int]:
    """Parse a Chilean-formatted figure into whole CLP.

    Dots group thousands and a comma marks decimals, which round half up:
    "45.000" -> 45000, "1.234.567" -> 1234567, "1.234,56" -> 1235.
    """
    integer, _, decimals = raw.strip().rstrip(".,").partition(",")
    digits = integer.replace(".", "")
    if not digits.isdigit() or (decimals and not decimals.isdigit()):
        return None
    value = Decimal(f"{digits}.{decimals}" if decimals else digits)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _parse_amount(
    text: str, rules: list[AmountRule]
) -> Optional[tuple[int, bool]]:
    """Return (amount, the rule's `income`) for the earliest anchor in `text`.

    An email opens with what happened and only then restates figures, so the
    anchor that appears first wins, whichever rule it belongs to: "Te
    devolvimos $ N de tu compra por $ M" is a refund of N. Ties go to the
    earlier rule. None when no anchored figure is present.
    """
    best: Optional[tuple[int, int, int, bool]] = None
    for index, rule in enumerate(rules):
        match = re.search(rule.pattern, text, re.IGNORECASE)
        if not match:
            continue
        amount = _to_clp(match.group(1))
        if amount is None:
            continue
        candidate = (match.start(), index, amount, rule.income)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return None if best is None else (best[2], best[3])


def _parse_merchant(text: str, patterns: list[str]) -> Optional[str]:
    """Return the first plausible merchant name captured by `patterns`.

    A capture longer than a merchant name can be is a runaway match (a whole
    paragraph), so it is rejected and the next pattern gets a turn.
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        merchant = " ".join(match.group(1).split()).strip(" .,;:-")
        if merchant and len(merchant) <= _MAX_MERCHANT_LEN:
            return merchant
    return None


_ANGLE_ADDRESS_RE = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")


def _sender_domain(from_header: str) -> str:
    """Return the lowercased domain of the From address ('' when absent).

    The trailing <address> is read first: a decoded display name with a comma
    ("Tenpo, Notificaciones <...>") makes strict parseaddr give up.
    """
    angle = _ANGLE_ADDRESS_RE.search(from_header)
    address = angle.group(1) if angle else email.utils.parseaddr(from_header)[1]
    local, at, domain = address.rpartition("@")
    return domain.lower() if at and local else ""


def _domain_matches(domain: str, wanted: str) -> bool:
    """True when `wanted`'s labels appear, whole and in order, in `domain`.

    "somosmach" matches "somosmach.com" and "mail.somosmach.com", but not
    "bci.cl" or "notsomosmach.com": a display name or a mailbox local part
    that happens to contain the word never counts.
    """
    labels = domain.split(".")
    want = wanted.lower().split(".")
    return any(
        labels[i : i + len(want)] == want
        for i in range(len(labels) - len(want) + 1)
    )


def _match_pattern(from_addr: str, subject: str, pattern: EmailPattern) -> bool:
    """Return True if the sender + subject match the given pattern."""
    domain = _sender_domain(from_addr)
    if not any(_domain_matches(domain, d) for d in pattern.sender_domains):
        return False

    if pattern.subject_contains:
        if not _contains_any(subject, pattern.subject_contains):
            return False

    return True


def _today() -> date:
    return datetime.now(LOCAL_TZ).date()


def _local_date(moment: datetime) -> date:
    """Calendar date of `moment` in Chile; a naive time is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date()


def _transaction_date(
    msg: email.message.Message,
    institution: str,
    internal_date: Optional[datetime],
) -> date:
    """Date the email was sent: its Date header, else the IMAP INTERNALDATE.

    Only when both are missing does it fall back to today, and it says so:
    a silently mis-dated transaction skews the daily planning math.
    """
    header = str(msg.get("Date", ""))
    try:
        return _local_date(email.utils.parsedate_to_datetime(header))
    except Exception:  # malformed headers raise ValueError, OverflowError, ...
        pass
    if internal_date is not None:
        logger.info(
            "[%s] Unparseable Date header %r; using the IMAP INTERNALDATE",
            institution,
            header,
        )
        return _local_date(internal_date)
    logger.warning(
        "[%s] Email without a usable Date header or INTERNALDATE; dating it today",
        institution,
    )
    return _today()


def _external_id(
    institution: str, msg: email.message.Message, fallback_parts: list[str]
) -> str:
    """Deterministic dedup key for one email.

    Keyed on the Message-ID, so the same email found again (a later run, or
    another mailbox) maps to the same row. hashlib, not hash(): Python's
    hash() is salted per process and would mint a new key on every restart.
    An email without a Message-ID is keyed on its content instead, rather
    than every such email sharing the digest of "".
    """
    message_id = str(msg.get("Message-ID", ""))
    seed = message_id if message_id.strip() else "|".join(fallback_parts)
    digest = hashlib.sha1(seed.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"email_{institution}_{digest}"


def parse_transaction(
    msg: email.message.Message,
    pattern: EmailPattern,
    internal_date: Optional[datetime] = None,
) -> Optional[ScrapedTransaction]:
    """Turn one email into a transaction, or None when it isn't one.

    The sign follows the money: an email is income when its subject carries
    one of the pattern's `income_keywords` or its amount was anchored by an
    income rule; anything else is an expense.
    """
    from_addr = _decode_header_value(msg.get("From", ""))
    subject = _decode_header_value(msg.get("Subject", ""))
    if not _match_pattern(from_addr, subject, pattern):
        return None

    body = _get_body(msg)
    if not body:
        logger.debug("[%s] No text body in %r", pattern.institution, subject)
        return None

    found = _parse_amount(body, pattern.amount_rules)
    if found is None:
        if re.search(CLP_AMOUNT, body):
            # Likely a notification whose wording no rule knows yet: say so,
            # so a missed transaction doesn't go unnoticed.
            logger.info(
                "[%s] %r has a $ figure no amount rule anchors; skipped",
                pattern.institution,
                subject,
            )
        else:
            logger.debug("[%s] No amount in %r", pattern.institution, subject)
        return None
    amount, anchored_income = found
    if amount == 0:
        logger.debug("[%s] Zero amount in %r, skipping", pattern.institution, subject)
        return None

    income = anchored_income or _contains_any(subject, pattern.income_keywords)
    amount = abs(amount) if income else -abs(amount)

    merchant = _parse_merchant(body, pattern.merchant_patterns)
    if merchant is None:
        merchant = " ".join(subject.split())[:_MAX_MERCHANT_LEN] or "Desconocido"

    tx_date = _transaction_date(msg, pattern.institution, internal_date)
    external_id = _external_id(
        pattern.institution, msg, [str(msg.get("Date", "")), from_addr, subject, body]
    )

    return ScrapedTransaction(
        institution=pattern.institution,
        product_kind=pattern.product_kind,
        description=f"{pattern.institution.upper()} - {merchant}",
        amount=amount,
        transaction_date=tx_date,
        external_id=external_id,
        scheduled_month=date(tx_date.year, tx_date.month, 1),
    )


def _resolve_mailbox(pattern: EmailPattern) -> str:
    return pattern.mailbox or os.environ.get("EMAIL_IMAP_MAILBOX") or DEFAULT_MAILBOX


def _resolve_max_messages(pattern: EmailPattern) -> int:
    """Per-run cap on fetched emails: the pattern's, else EMAIL_MAX_MESSAGES."""
    if pattern.max_messages is not None:
        cap = pattern.max_messages
    else:
        raw = os.environ.get("EMAIL_MAX_MESSAGES") or str(DEFAULT_MAX_MESSAGES)
        try:
            cap = int(raw)
        except ValueError:
            raise ValueError(
                f"EMAIL_MAX_MESSAGES must be an integer, got {raw!r}"
            ) from None
    if cap < 1:
        raise ValueError(f"The email cap must be at least 1, got {cap}")
    return cap


def _quote_mailbox(name: str) -> str:
    """Quote a mailbox name for imaplib, which sends arguments verbatim.

    Gmail folders such as "[Gmail]/All Mail" carry spaces and brackets.
    """
    if len(name) > 1 and name.startswith('"') and name.endswith('"'):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _select(mail: imaplib.IMAP4_SSL, mailbox: str) -> None:
    """Open `mailbox` read-only, so fetching never marks mail as read."""
    status, data = mail.select(_quote_mailbox(mailbox), readonly=True)
    if status != "OK":
        raise RuntimeError(f"IMAP mailbox {mailbox!r} could not be opened: {data!r}")


def _search_ids(mail: imaplib.IMAP4_SSL, since: str, senders: list[str]) -> list[str]:
    """Message numbers from any of `senders` since `since`, oldest first.

    One search per sender, merged, so an email two senders' searches both
    hit is fetched once.
    """
    found: set[int] = set()
    for sender in senders:
        status, data = mail.search(None, f'(SINCE {since} FROM "{sender}")')
        if status != "OK" or not data or not data[0]:
            continue
        found.update(int(n) for n in data[0].split())
    return [str(n) for n in sorted(found)]


_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


def _fetch_message(
    mail: imaplib.IMAP4_SSL, msg_id: str
) -> tuple[Optional[bytes], Optional[datetime]]:
    """Fetch one message without flagging it as seen.

    Returns (raw RFC 822 bytes, INTERNALDATE). Fetch responses may interleave
    bare bytes lines with the (envelope, payload) tuple, and the server may
    put INTERNALDATE on either side of the payload, so every part is scanned.
    """
    status, msg_data = mail.fetch(msg_id, "(INTERNALDATE BODY.PEEK[])")
    if status != "OK":
        return None, None

    raw_email: Optional[bytes] = None
    internal_date: Optional[datetime] = None
    for part in msg_data:
        envelope = part[0] if isinstance(part, tuple) else part
        if (
            raw_email is None
            and isinstance(part, tuple)
            and len(part) > 1
            and isinstance(part[1], (bytes, bytearray))
        ):
            raw_email = bytes(part[1])
        if internal_date is None and isinstance(envelope, (bytes, bytearray)):
            match = _INTERNALDATE_RE.search(envelope)
            if match:
                try:
                    internal_date = datetime.strptime(
                        match.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z"
                    )
                except ValueError:
                    pass
    return raw_email, internal_date


class ImapSession:
    """Process-wide IMAP connection with NOOP keepalive + auto-reconnect.

    Serializes access via an asyncio.Lock because imaplib is synchronous and
    multiple per-institution scrapers can fire concurrently under APScheduler.
    """

    def __init__(self, host: str, user: str, password: str) -> None:
        self._host = host
        self._user = user
        self._password = password
        self._mail: Optional[imaplib.IMAP4_SSL] = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> imaplib.IMAP4_SSL:
        await self._lock.acquire()
        try:
            return await asyncio.to_thread(self._connection)
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()

    def _connection(self) -> imaplib.IMAP4_SSL:
        if not self._is_alive():
            self._reconnect()
        assert self._mail is not None
        return self._mail

    def _is_alive(self) -> bool:
        if self._mail is None:
            return False
        try:
            status, _ = self._mail.noop()
            if status == "OK":
                logger.debug("IMAP session reused (NOOP OK)")
                return True
            return False
        except Exception:
            return False

    def _reconnect(self) -> None:
        if self._mail is not None:
            try:
                self._mail.logout()
            except Exception:
                pass
            self._mail = None

        logger.info("IMAP login to %s as %s", self._host, self._user)
        mail = imaplib.IMAP4_SSL(self._host, timeout=IMAP_TIMEOUT_SECONDS)
        mail.login(self._user, self._password)
        self._mail = mail

    def close(self) -> None:
        """Best-effort logout. Safe to call even if nothing was opened."""
        if self._mail is None:
            return
        try:
            self._mail.logout()
        except Exception:
            pass
        self._mail = None


_SESSION: Optional[ImapSession] = None


def get_session() -> ImapSession:
    """Return the lazily-initialised singleton ImapSession.

    Reads EMAIL_IMAP_HOST / EMAIL_IMAP_USER / EMAIL_IMAP_PASSWORD on first use.
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = ImapSession(
            host=os.environ["EMAIL_IMAP_HOST"],
            user=os.environ["EMAIL_IMAP_USER"],
            password=os.environ["EMAIL_IMAP_PASSWORD"],
        )
    return _SESSION


def _collect_transactions(
    mail: imaplib.IMAP4_SSL, pattern: EmailPattern, lookback_days: int
) -> list[ScrapedTransaction]:
    """Blocking half of `fetch_transactions_for_pattern`; runs in a thread."""
    mailbox = _resolve_mailbox(pattern)
    cap = _resolve_max_messages(pattern)
    since = (_today() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")

    _select(mail, mailbox)
    ids = _search_ids(mail, since, pattern.sender_domains)
    logger.info(
        "[%s] Found %d emails in %s since %s", pattern.institution, len(ids), mailbox, since
    )
    if len(ids) > cap:
        logger.warning(
            "[%s] %d emails match but only the newest %d are parsed; "
            "raise EMAIL_MAX_MESSAGES to cover the whole %d-day window",
            pattern.institution,
            len(ids),
            cap,
            lookback_days,
        )
        ids = ids[-cap:]

    transactions: list[ScrapedTransaction] = []
    for msg_id in ids:
        raw_email, internal_date = _fetch_message(mail, msg_id)
        if raw_email is None:
            continue
        msg = email.message_from_bytes(raw_email)
        try:
            tx = parse_transaction(msg, pattern, internal_date)
        except Exception:
            # One malformed email must not cost the rest of the run.
            logger.warning(
                "[%s] Could not parse email %s (%r); skipped",
                pattern.institution,
                msg_id,
                str(msg.get("Subject", "")),
                exc_info=True,
            )
            continue
        if tx is not None:
            transactions.append(tx)

    logger.info(
        "[%s] Email backend: %d transactions parsed from %d emails",
        pattern.institution,
        len(transactions),
        len(ids),
    )
    return transactions


async def fetch_transactions_for_pattern(
    pattern: EmailPattern,
    lookback_days: int = 7,
) -> list[ScrapedTransaction]:
    """Search the mailbox for emails matching `pattern` and return transactions."""
    async with get_session() as mail:
        return await asyncio.to_thread(
            _collect_transactions, mail, pattern, lookback_days
        )


def _find_code(
    mail: imaplib.IMAP4_SSL,
    sender_contains: list[str],
    subject_contains: list[str],
    code_pattern: str,
    lookback_days: int,
) -> Optional[str]:
    """Blocking half of `fetch_latest_code`; runs in a thread."""
    since = (_today() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    _select(mail, DEFAULT_MAILBOX)
    for sender_keyword in sender_contains:
        status, message_ids = mail.search(None, f'(SINCE {since} FROM "{sender_keyword}")')
        if status != "OK" or not message_ids or not message_ids[0]:
            continue

        ids = message_ids[0].split()
        for msg_id in reversed(ids[-20:]):
            raw_email, _ = _fetch_message(mail, msg_id.decode())
            if raw_email is None:
                continue
            msg = email.message_from_bytes(raw_email)

            from_addr = _decode_header_value(msg.get("From", ""))
            subject = _decode_header_value(msg.get("Subject", ""))
            if not any(s.lower() in from_addr.lower() for s in sender_contains):
                continue
            if subject_contains and not _contains_any(subject, subject_contains):
                continue

            body = _get_body(msg)
            if not body:
                continue
            match = re.search(code_pattern, body)
            if match:
                logger.info("Email backend: code found in email from '%s'", from_addr)
                return match.group(1)
    return None


async def fetch_latest_code(
    sender_contains: list[str],
    subject_contains: list[str],
    code_pattern: str = r"\b(\d{6})\b",
    lookback_days: int = 2,
) -> Optional[str]:
    """Return the first code matching `code_pattern` from the newest matching email.

    Searches the INBOX (2FA codes are never auto-archived, whatever
    EMAIL_IMAP_MAILBOX says) for emails whose From contains one of
    `sender_contains` and whose Subject contains one of `subject_contains`,
    scans them newest-first, and returns the first regex capture (e.g. a 2FA
    code) found in the body, or None.
    """
    async with get_session() as mail:
        return await asyncio.to_thread(
            _find_code, mail, sender_contains, subject_contains, code_pattern, lookback_days
        )
