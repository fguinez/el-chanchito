"""Agnostic IMAP email-parsing backend.

Provides a process-wide `ImapSession` with NOOP keepalive and automatic
reconnection so multiple per-institution scrapers can share a single live
login, plus a `fetch_transactions_for_pattern` helper that institution
scrapers call with their own `EmailPattern`.
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
from dataclasses import dataclass
from datetime import date, timedelta
from email.header import decode_header
from typing import Optional

from scrapers.base import ScrapedTransaction

logger = logging.getLogger(__name__)


@dataclass
class EmailPattern:
    """Configuration for parsing a specific institution's emails."""

    institution: str
    product_kind: str
    sender_contains: list[str]        # match any of these in From header
    subject_contains: list[str]       # match any of these in Subject (optional filter)
    amount_patterns: list[str]        # regex patterns to extract amount from body
    merchant_patterns: list[str]      # regex patterns to extract merchant name
    is_expense: bool = True           # True if amount should be negative


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


def _strip_html(html_text: str) -> str:
    """Convert an HTML body to plain-ish text.

    Drops <style>/<script> blocks first: their contents survive naive tag
    stripping and pollute merchant/amount regexes with CSS and URLs.
    """
    text = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", html_text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


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


def _parse_amount(text: str, patterns: list[str]) -> Optional[int]:
    """Try to extract an amount in CLP from text using patterns.

    Handles Chilean number formats:
      - "45.000"    -> 45000 (dots as thousands separator)
      - "1.234.567" -> 1234567
      - "1.234,56"  -> 1235 (comma as decimal, rounded)
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw = match.group(1).strip()
            if "," in raw:
                raw = raw.replace(".", "").replace(",", ".")
                try:
                    return int(round(float(raw)))
                except ValueError:
                    continue
            else:
                amount_str = raw.replace(".", "")
                try:
                    return int(amount_str)
                except ValueError:
                    continue
    return None


def _parse_merchant(text: str, patterns: list[str]) -> str:
    """Try to extract a merchant name from text."""
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:100]
    return "Desconocido"


def _match_pattern(from_addr: str, subject: str, pattern: EmailPattern) -> bool:
    """Return True if the sender + subject match the given pattern."""
    from_lower = from_addr.lower()
    subject_lower = subject.lower()

    if not any(s in from_lower for s in pattern.sender_contains):
        return False

    if pattern.subject_contains:
        if not any(s in subject_lower for s in pattern.subject_contains):
            return False

    return True


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
            if not self._is_alive():
                self._reconnect()
            assert self._mail is not None
            return self._mail
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()

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
        mail = imaplib.IMAP4_SSL(self._host)
        mail.login(self._user, self._password)
        mail.select("INBOX")
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


async def fetch_transactions_for_pattern(
    pattern: EmailPattern,
    lookback_days: int = 7,
) -> list[ScrapedTransaction]:
    """Search the inbox for emails matching `pattern` and return transactions."""
    transactions: list[ScrapedTransaction] = []

    async with get_session() as mail:
        since_date = date.today() - timedelta(days=lookback_days)
        since_str = since_date.strftime("%d-%b-%Y")

        for sender_keyword in pattern.sender_contains:
            search_criteria = f'(SINCE {since_str} FROM "{sender_keyword}")'

            status, message_ids = mail.search(None, search_criteria)
            if status != "OK" or not message_ids[0]:
                continue

            ids = message_ids[0].split()
            logger.info(
                "[%s] Found %d emails from '%s'",
                pattern.institution,
                len(ids),
                sender_keyword,
            )

            for msg_id in ids[-50:]:  # Process at most 50 per sender
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                # Fetch responses may interleave bare flag lines (bytes) with
                # the (envelope, payload) tuple; indexing [0][1] blindly can
                # hit an int inside a bytes object.
                raw_email = next(
                    (
                        part[1]
                        for part in msg_data
                        if isinstance(part, tuple)
                        and len(part) > 1
                        and isinstance(part[1], (bytes, bytearray))
                    ),
                    None,
                )
                if raw_email is None:
                    continue
                msg = email.message_from_bytes(raw_email)

                from_addr = _decode_header_value(msg.get("From", ""))
                subject = _decode_header_value(msg.get("Subject", ""))
                msg_date_str = msg.get("Date", "")

                if not _match_pattern(from_addr, subject, pattern):
                    continue

                body = _get_body(msg)
                if not body:
                    continue

                amount = _parse_amount(body, pattern.amount_patterns)
                if amount is None or amount == 0:
                    continue

                if pattern.is_expense:
                    amount = -abs(amount)

                merchant = _parse_merchant(body, pattern.merchant_patterns)
                description = f"{pattern.institution.upper()} - {merchant}"

                tx_date = date.today()
                try:
                    parsed = email.utils.parsedate_to_datetime(msg_date_str)
                    tx_date = parsed.date()
                except Exception:
                    pass

                # hashlib, not hash(): Python's hash() is salted per process,
                # which would mint a new external_id (and a duplicate row)
                # for the same email on every run.
                message_id = msg.get("Message-ID", "")
                digest = hashlib.sha1(message_id.encode()).hexdigest()[:8]
                external_id = f"email_{pattern.institution}_{digest}"

                transactions.append(
                    ScrapedTransaction(
                        institution=pattern.institution,
                        product_kind=pattern.product_kind,
                        description=description,
                        amount=amount,
                        transaction_date=tx_date,
                        external_id=external_id,
                        scheduled_month=date(tx_date.year, tx_date.month, 1),
                    )
                )

    logger.info(
        "[%s] Email backend: %d transactions parsed",
        pattern.institution,
        len(transactions),
    )
    return transactions


async def fetch_latest_code(
    sender_contains: list[str],
    subject_contains: list[str],
    code_pattern: str = r"\b(\d{6})\b",
    lookback_days: int = 2,
) -> Optional[str]:
    """Return the first code matching `code_pattern` from the newest matching email.

    Searches the inbox for emails whose From matches `sender_contains` and
    Subject matches `subject_contains`, scans them newest-first, and returns
    the first regex capture (e.g. a 2FA code) found in the body, or None.
    """
    since_date = date.today() - timedelta(days=lookback_days)
    since_str = since_date.strftime("%d-%b-%Y")

    async with get_session() as mail:
        pattern = EmailPattern(
            institution="2fa",
            product_kind="code",
            sender_contains=sender_contains,
            subject_contains=subject_contains,
            amount_patterns=[],
            merchant_patterns=[],
        )
        for sender_keyword in sender_contains:
            search_criteria = f'(SINCE {since_str} FROM "{sender_keyword}")'

            status, message_ids = mail.search(None, search_criteria)
            if status != "OK" or not message_ids[0]:
                continue

            ids = message_ids[0].split()
            for msg_id in reversed(ids[-20:]):
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = next(
                    (
                        part[1]
                        for part in msg_data
                        if isinstance(part, tuple)
                        and len(part) > 1
                        and isinstance(part[1], (bytes, bytearray))
                    ),
                    None,
                )
                if raw_email is None:
                    continue
                msg = email.message_from_bytes(raw_email)

                from_addr = _decode_header_value(msg.get("From", ""))
                subject = _decode_header_value(msg.get("Subject", ""))
                if not _match_pattern(from_addr, subject, pattern):
                    continue

                body = _get_body(msg)
                if not body:
                    continue
                match = re.search(code_pattern, body)
                if match:
                    logger.info(
                        "Email backend: code found in email from '%s'", from_addr
                    )
                    return match.group(1)

    return None
