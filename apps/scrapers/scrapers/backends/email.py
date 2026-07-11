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


def _get_body(msg: email.message.Message) -> str:
    """Extract the plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
            elif ctype == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
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

                raw_email = msg_data[0][1]
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

                message_id = msg.get("Message-ID", "")
                external_id = (
                    f"email_{pattern.institution}_{hash(message_id) & 0xFFFFFFFF:08x}"
                )

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
