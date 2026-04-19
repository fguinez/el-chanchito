"""Email notification parser for MercadoPago, MACH, and Tenpo.

Connects via IMAP, searches for transaction notification emails from known
senders, parses amounts/merchants, and returns ScrapedTransactions.

Sender patterns and body regexes are configured per institution and should
be tuned to match the actual email formats received.
"""

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

from scrapers.base import BaseScraper, ScrapedBalance, ScrapedTransaction

logger = logging.getLogger(__name__)


@dataclass
class EmailPattern:
    """Configuration for parsing a specific institution's emails."""
    institution: str
    account_type: str
    sender_contains: list[str]        # match any of these in From header
    subject_contains: list[str]       # match any of these in Subject (optional filter)
    amount_patterns: list[str]        # regex patterns to extract amount from body
    merchant_patterns: list[str]      # regex patterns to extract merchant name
    is_expense: bool = True           # True if amount should be negative


# Known email patterns for Chilean fintechs
PATTERNS: list[EmailPattern] = [
    EmailPattern(
        institution="mercadopago",
        account_type="prepaid",
        sender_contains=["mercadopago", "mercadolibre"],
        subject_contains=["pago", "compra", "transferencia", "pagaste"],
        amount_patterns=[
            r"(?:pagaste|pago de|monto:?)\s*\$?\s*([\d.,]+)",
            r"\$\s*([\d.,]+)",
        ],
        merchant_patterns=[
            r"\b(?:en|a)\s+(.+?)(?:\.|$|\n)",
            r"comercio:?\s*(.+?)(?:\.|$|\n)",
        ],
    ),
    EmailPattern(
        institution="mach",
        account_type="prepaid",
        sender_contains=["mach", "somosmach", "bci"],
        subject_contains=["compra", "pago", "transaccion", "transferencia"],
        amount_patterns=[
            r"\$\s*([\d.,]+)",
            r"(?:monto|valor):?\s*\$?\s*([\d.,]+)",
        ],
        merchant_patterns=[
            r"(?:en|comercio)\s+(.+?)(?:\s+por|\.|$|\n)",
        ],
    ),
    EmailPattern(
        institution="tenpo",
        account_type="prepaid",
        sender_contains=["tenpo"],
        subject_contains=["compra", "pago", "transaccion"],
        amount_patterns=[
            r"\$\s*([\d.,]+)",
            r"(?:monto|valor):?\s*\$?\s*([\d.,]+)",
        ],
        merchant_patterns=[
            r"(?:en|comercio)\s+(.+?)(?:\s+por|\.|$|\n)",
        ],
    ),
]


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
                    # Strip HTML tags for basic text extraction
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
            # If there's a comma, treat it as decimal separator (Chilean format)
            if "," in raw:
                raw = raw.replace(".", "").replace(",", ".")
                try:
                    return int(round(float(raw)))
                except ValueError:
                    continue
            else:
                # No comma — dots are thousands separators
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


def _match_pattern(from_addr: str, subject: str, patterns: list[EmailPattern]) -> Optional[EmailPattern]:
    """Find the matching email pattern for a given sender and subject."""
    from_lower = from_addr.lower()
    subject_lower = subject.lower()

    for pattern in patterns:
        sender_match = any(s in from_lower for s in pattern.sender_contains)
        if not sender_match:
            continue

        # If subject filters exist, at least one must match
        if pattern.subject_contains:
            subject_match = any(s in subject_lower for s in pattern.subject_contains)
            if not subject_match:
                continue

        return pattern

    return None


class EmailParserScraper(BaseScraper):
    @property
    def name(self) -> str:
        return "email_parser"

    def __init__(self) -> None:
        self.imap_host = os.environ["EMAIL_IMAP_HOST"]
        self.imap_user = os.environ["EMAIL_IMAP_USER"]
        self.imap_password = os.environ["EMAIL_IMAP_PASSWORD"]
        # How many days back to search
        self.lookback_days = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))

    async def scrape_transactions(self) -> list[ScrapedTransaction]:
        """Fetch and parse transaction emails via IMAP."""
        transactions: list[ScrapedTransaction] = []
        mail = None

        try:
            logger.info("Connecting to IMAP %s as %s", self.imap_host, self.imap_user)
            mail = imaplib.IMAP4_SSL(self.imap_host)
            mail.login(self.imap_user, self.imap_password)
            mail.select("INBOX")

            # Search for recent emails (correct cross-month lookback)
            since_date = date.today() - timedelta(days=self.lookback_days)
            since_str = since_date.strftime("%d-%b-%Y")

            # Build OR search for all known senders
            all_senders = set()
            for p in PATTERNS:
                all_senders.update(p.sender_contains)

            for sender_keyword in all_senders:
                search_criteria = f'(SINCE {since_str} FROM "{sender_keyword}")'

                status, message_ids = mail.search(None, search_criteria)
                if status != "OK" or not message_ids[0]:
                    continue

                ids = message_ids[0].split()
                logger.info(
                    "Found %d emails from '%s'", len(ids), sender_keyword
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

                    pattern = _match_pattern(from_addr, subject, PATTERNS)
                    if not pattern:
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

                    # Parse date from email headers
                    tx_date = date.today()
                    try:
                        parsed = email.utils.parsedate_to_datetime(msg_date_str)
                        tx_date = parsed.date()
                    except Exception:
                        pass

                    # Dedup key from message-id header
                    message_id = msg.get("Message-ID", "")
                    external_id = f"email_{pattern.institution}_{hash(message_id) & 0xFFFFFFFF:08x}"

                    transactions.append(
                        ScrapedTransaction(
                            account_institution=pattern.institution,
                            account_type=pattern.account_type,
                            description=description,
                            amount=amount,
                            transaction_date=tx_date,
                            external_id=external_id,
                            scheduled_month=date(tx_date.year, tx_date.month, 1),
                        )
                    )

            logger.info("Email parser: %d transactions parsed", len(transactions))

        except imaplib.IMAP4.error as e:
            logger.error("IMAP error: %s", e)
            raise
        except Exception:
            logger.exception("Email parser failed")
            raise
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

        return transactions

    async def scrape_balances(self) -> list[ScrapedBalance]:
        return []
