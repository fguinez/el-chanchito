"""Tests for the agnostic email backend + per-institution pattern matching.

Never opens a real IMAP connection: whole emails come from the synthetic
corpus in `tests/fixtures/emails/` (every sender address is an institution's
notification domain; every recipient, counterparty, merchant, amount and
Message-ID is fabricated, see its README), and the IMAP round trips run
against an in-memory `FakeMail`.
"""

import asyncio
import dataclasses
import email
import email.message
import hashlib
import logging
import os
import subprocess
import sys
import threading
from datetime import UTC, date, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from scrapers.backends import email as email_mod
from scrapers.backends.email import (
    ImapSession,
    _decode_header_value,
    _domain_matches,
    _match_pattern,
    _parse_amount,
    _parse_merchant,
    _quote_mailbox,
    _sender_domain,
    _strip_html,
    _to_clp,
    anchored,
    fetch_latest_code,
    fetch_transactions_for_pattern,
    parse_transaction,
)
from scrapers.institutions.mach import PATTERN as MACH_PATTERN
from scrapers.institutions.mercadopago import PATTERN as MP_PATTERN
from scrapers.institutions.tenpo import PATTERN as TENPO_PATTERN

APP_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def load(name: str) -> email.message.Message:
    return email.message_from_bytes((FIXTURES / name).read_bytes())


def build(from_addr: str, subject: str, body: str, **headers: str) -> email.message.Message:
    """A synthetic email, round-tripped through bytes like a fetched one."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = headers.get("date", "Sun, 20 Sep 2026 12:00:00 -0300")
    msg["Message-ID"] = headers.get("message_id", "<synthetic-inline@example.com>")
    msg.set_content(body)
    return email.message_from_bytes(msg.as_bytes())


class TestToClp:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("999.999", 999_999),
            ("2.500.000", 2_500_000),
            ("1000000", 1_000_000),
            ("1.234,56", 1_235),
            ("1.234,5", 1_235),
            ("999.999.", 999_999),
            ("abc", None),
        ],
    )
    def test_chilean_formats(self, raw, expected):
        """Dots group thousands, a comma marks decimals that round half up."""
        assert _to_clp(raw) == expected


class TestParseAmount:
    def test_anchor_beats_an_earlier_promo_figure(self):
        """A banner figure before the anchored one is ignored."""
        text = "Gana hasta $ 1.000.000 invitando amigos.\nPagaste $ 999.999 a COMERCIO"

        assert _parse_amount(text, [anchored("pagaste")]) == (999_999, False)

    def test_no_anchor_no_amount(self):
        """A figure no anchor claims is not a transaction."""
        text = "Obten hasta $ 1.000.000 en descuentos."

        assert _parse_amount(text, MP_PATTERN.amount_rules) is None

    def test_earliest_anchor_wins_across_rules(self):
        """A refund that restates the purchase is still a refund of its own figure."""
        text = "Te devolvimos $ 999.999 de tu compra de $ 1.000.000."

        assert _parse_amount(text, MP_PATTERN.amount_rules) == (999_999, True)

    def test_label_and_value_in_separate_lines(self):
        """HTML tables put the label and the figure in different cells."""
        assert _parse_amount("Monto\n$ 999.999", TENPO_PATTERN.amount_rules) == (999_999, False)

    def test_anchor_too_far_from_the_figure(self):
        """The anchor only claims a figure within 60 characters."""
        text = "Pagaste " + "x" * 61 + " $ 999.999"

        assert _parse_amount(text, [anchored("pagaste")]) is None

    @pytest.mark.parametrize(
        ("text", "pattern", "expected"),
        [
            (
                "Tu compra fue aprobada.\n\nInvita a un amigo y gana $ 1.000.000\n\nMonto: $ 999.999",
                MACH_PATTERN,
                (999_999, False),
            ),
            (
                "¡Ganaste cashback en tu compra! Realizaste una compra por $ 999.999 en COMERCIO",
                TENPO_PATTERN,
                (999_999, False),
            ),
            ("Compraste en COMERCIO SINTETICO S.A. por $ 999.999.", MACH_PATTERN, (999_999, False)),
        ],
        ids=["promo-after-purchase", "cashback-headline", "dotted-merchant"],
    )
    def test_anchor_stops_at_a_sentence_end(self, text, pattern, expected):
        """An anchor never claims a figure from the next sentence."""
        assert _parse_amount(text, pattern.amount_rules) == expected

    def test_foreign_currency_is_not_clp(self):
        assert _parse_amount("Total US$ 1.234,56", MACH_PATTERN.amount_rules) is None


class TestParseMerchant:
    def test_bare_preposition_does_not_anchor(self):
        """MercadoPago's merchant needs the verb, not any "a" in the body."""
        text = "Hola a todos. Pagaste $ 999.999 a COMERCIO SINTETICO. Gracias"

        assert _parse_merchant(text, MP_PATTERN.merchant_patterns) == "COMERCIO SINTETICO"

    def test_merchant_without_amount(self):
        assert (
            _parse_merchant("Pagaste en COMERCIO SINTETICO.\nOtro texto", MP_PATTERN.merchant_patterns)
            == "COMERCIO SINTETICO"
        )

    def test_runaway_capture_is_rejected(self):
        """A capture longer than any merchant name is not one."""
        assert _parse_merchant("comercio: " + "A" * 200 + ".", MP_PATTERN.merchant_patterns) is None

    def test_no_match(self):
        assert _parse_merchant("Notificacion de seguridad", MP_PATTERN.merchant_patterns) is None


class TestStripHtml:
    def test_entities_decoded_and_cells_split(self):
        """&#36;/&nbsp; decode before any regex runs, and cells don't run together."""
        html_body = "<table><tr><td>Monto</td><td>&#36;&nbsp;999.999</td></tr></table>"

        assert _strip_html(html_body) == "Monto\n$ 999.999"

    def test_style_and_script_dropped(self):
        html_body = "<style>.a{color:red}</style><script>var x=1;</script><p>Hola</p>"

        assert _strip_html(html_body) == "Hola"


class TestDomainMatches:
    @pytest.mark.parametrize(
        ("domain", "wanted", "expected"),
        [
            ("somosmach.com", "somosmach", True),
            ("mail.somosmach.com", "somosmach", True),
            ("notsomosmach.com", "somosmach", False),
            ("bci.cl", "somosmach", False),
            ("mercadopago.cl", "mercadopago.cl", True),
            ("mercadopago.com", "mercadopago.cl", False),
            ("", "tenpo", False),
        ],
    )
    def test_whole_labels_only(self, domain, wanted, expected):
        assert _domain_matches(domain, wanted) is expected


class TestSenderDomain:
    @pytest.mark.parametrize(
        ("from_header", "expected"),
        [
            ("MACH <notificaciones@somosmach.com>", "somosmach.com"),
            ("Tenpo, Notificaciones  <notificaciones@TENPO.cl>", "tenpo.cl"),
            ("info@mercadopago.cl", "mercadopago.cl"),
            ("Sin direccion", ""),
        ],
    )
    def test_domain_of_the_address(self, from_header, expected):
        """A decoded display name with a comma still yields the address."""
        assert _sender_domain(from_header) == expected


class TestMatchPattern:
    """Each institution's PATTERN should match its own senders, not others."""

    @pytest.mark.parametrize(
        ("from_addr", "subject", "pattern", "expected"),
        [
            ("noreply@mercadopago.cl", "Tu pago fue exitoso", MP_PATTERN, True),
            ("info@mercadolibre.cl", "Compra realizada", MP_PATTERN, True),
            ("notificaciones@somosmach.com", "Compra aprobada", MACH_PATTERN, True),
            ("info@tenpo.cl", "Transacción exitosa", TENPO_PATTERN, True),
            ("NoReply@MercadoPago.CL", "PAGO EXITOSO", MP_PATTERN, True),
            ("noreply@mercadopago.cl", "Bienvenido a MercadoPago", MP_PATTERN, True),
            ("noreply@mercadopago.cl", "Actualiza tu perfil", MP_PATTERN, False),
            ("Banco Bci <notificaciones@bci.cl>", "Compra aprobada", MACH_PATTERN, False),
            ("MACH <alertas@bci.cl>", "Compra aprobada", MACH_PATTERN, False),
            ("machuca@example.com", "Compra aprobada", MACH_PATTERN, False),
            ("noreply@other.com", "Compra aprobada", MP_PATTERN, False),
            ("noreply@other.com", "Compra aprobada", TENPO_PATTERN, False),
        ],
        ids=lambda v: v.institution if hasattr(v, "institution") else None,
    )
    def test_sender_and_subject(self, from_addr, subject, pattern, expected):
        """Senders match on their domain only; subjects ignore case and accents."""
        assert _match_pattern(from_addr, subject, pattern) is expected


class TestParseTransaction:
    """Table-driven over the fixture corpus: (amount, description, date)."""

    @pytest.mark.parametrize(
        ("fixture", "pattern", "amount", "merchant", "tx_date"),
        [
            ("mach_purchase.eml", MACH_PATTERN, -999_999, "COMERCIO SINTETICO", date(2026, 9, 20)),
            ("mach_transfer_in.eml", MACH_PATTERN, 1_000_000, "Persona Sintetica", date(2026, 9, 20)),
            ("mach_transfer_out.eml", MACH_PATTERN, -2_500_000, "Persona Sintetica", date(2026, 9, 19)),
            ("mp_payment.eml", MP_PATTERN, -999_999, "COMERCIO SINTETICO", date(2026, 9, 20)),
            ("mp_money_received.eml", MP_PATTERN, 1_000_000, "Persona Sintetica", date(2026, 9, 20)),
            ("mp_refund.eml", MP_PATTERN, 999_999, "COMERCIO SINTETICO", date(2026, 9, 20)),
            ("tenpo_purchase.eml", TENPO_PATTERN, -999_999, "COMERCIO SINTETICO", date(2026, 9, 20)),
            ("tenpo_refund.eml", TENPO_PATTERN, 999_999, "COMERCIO SINTETICO", date(2026, 9, 20)),
            ("tenpo_transfer_in.eml", TENPO_PATTERN, 1_000_000, "Persona Sintetica", date(2026, 9, 20)),
            ("tenpo_cashback.eml", TENPO_PATTERN, 999_999, "Recibiste cashback", date(2026, 9, 20)),
            ("tenpo_cashback_purchase.eml", TENPO_PATTERN, -999_999, "COMERCIO SINTETICO", date(2026, 9, 20)),
        ],
        ids=lambda v: v if isinstance(v, str) and v.endswith(".eml") else None,
    )
    def test_fixture(self, fixture, pattern, amount, merchant, tx_date):
        """Income and refunds come out positive, purchases and transfers out negative."""
        tx = parse_transaction(load(fixture), pattern)

        assert (tx.amount, tx.description, tx.transaction_date) == (
            amount,
            f"{pattern.institution.upper()} - {merchant}",
            tx_date,
        )

    @pytest.mark.parametrize(
        ("fixture", "pattern"),
        [
            ("bci_bank_purchase.eml", MACH_PATTERN),
            ("mp_promo.eml", MP_PATTERN),
            ("tenpo_zero_amount.eml", TENPO_PATTERN),
        ],
    )
    def test_not_a_transaction(self, fixture, pattern):
        """A Banco Bci email, a promo, and a zero amount yield nothing."""
        assert parse_transaction(load(fixture), pattern) is None

    def test_zero_amount_is_logged(self, caplog):
        """A zero amount is skipped with a debug line naming the subject."""
        caplog.set_level(logging.DEBUG, logger=email_mod.__name__)

        parse_transaction(load("tenpo_zero_amount.eml"), TENPO_PATTERN)

        assert "Zero amount in 'Compra aprobada'" in caplog.text

    def test_unanchored_figure_is_logged(self, caplog):
        """An email with a figure no rule anchors is skipped, visibly."""
        caplog.set_level(logging.INFO, logger=email_mod.__name__)
        msg = build("MACH <notificaciones@somosmach.com>", "Compra aprobada", "Se hizo un cargo de $ 999.999")

        tx = parse_transaction(msg, MACH_PATTERN)

        assert tx is None
        assert "no amount rule anchors" in caplog.text

    def test_subject_decides_a_context_free_figure(self):
        """A bare "Monto:" is income when the subject says money came in."""
        msg = build("MACH <notificaciones@somosmach.com>", "Recibiste una transferencia", "Monto: $ 1.000.000")

        assert parse_transaction(msg, MACH_PATTERN).amount == 1_000_000

    def test_merchant_falls_back_to_the_subject(self):
        """Without a recognisable merchant, the subject beats "Desconocido"."""
        msg = build("Tenpo <notificaciones@tenpo.cl>", "Compra aprobada", "Monto: $ 999.999")

        assert parse_transaction(msg, TENPO_PATTERN).description == "TENPO - Compra aprobada"


class TestTransactionDate:
    @pytest.mark.parametrize(
        "header",
        [b"not a date", b"Sun, 20 Sep 99999999999 12:00:00 -0300"],
        ids=["garbage", "overflow"],
    )
    def test_bad_date_header_uses_internaldate(self, header):
        """The IMAP arrival time dates the email, in Chile's calendar."""
        raw = (FIXTURES / "mach_bad_date.eml").read_bytes().replace(b"not a date", header)
        arrival = datetime(2026, 9, 19, 2, 30, tzinfo=UTC)

        tx = parse_transaction(email.message_from_bytes(raw), MACH_PATTERN, arrival)

        assert tx.transaction_date == date(2026, 9, 18)

    def test_eight_bit_date_header(self):
        """A raw 8-bit byte in the header (a Header object) doesn't crash."""
        raw = (FIXTURES / "mach_bad_date.eml").read_bytes().replace(
            b"not a date", b"Sun, 20 Sep 2026 12:00:00 -0300 (Hora de Chile \xe9)"
        )

        tx = parse_transaction(email.message_from_bytes(raw), MACH_PATTERN)

        assert tx.transaction_date == date(2026, 9, 20)

    def test_today_fallback_warns(self, monkeypatch, caplog):
        """With neither date available the email is dated today, loudly."""
        monkeypatch.setattr(email_mod, "_today", lambda: date(2026, 9, 1))

        tx = parse_transaction(load("mach_bad_date.eml"), MACH_PATTERN)

        assert tx.transaction_date == date(2026, 9, 1)
        assert "dating it today" in caplog.text


class TestExternalId:
    def test_message_id_key_is_unchanged(self):
        """Rows stored before this change keep deduplicating."""
        digest = hashlib.sha1(b"<synthetic-mach-purchase@example.com>").hexdigest()[:8]

        tx = parse_transaction(load("mach_purchase.eml"), MACH_PATTERN)

        assert tx.external_id == f"email_mach_{digest}"

    def test_missing_message_id_keys_on_content(self):
        """Emails without a Message-ID no longer all share the digest of ""."""
        first = load("tenpo_no_message_id.eml")
        second = load("tenpo_no_message_id.eml")
        second.replace_header("Date", "Sun, 20 Sep 2026 12:31:00 -0300")
        empty = f"email_tenpo_{hashlib.sha1(b'').hexdigest()[:8]}"

        keys = {
            parse_transaction(first, TENPO_PATTERN).external_id,
            parse_transaction(second, TENPO_PATTERN).external_id,
        }

        assert len(keys) == 2 and empty not in keys

    @pytest.mark.parametrize("seed", ["1", "2"])
    def test_same_key_in_another_process(self, seed):
        """hash() is salted per process; the keys must not be (issue #2)."""
        script = (
            "import email, pathlib\n"
            "from scrapers.backends.email import parse_transaction\n"
            "from scrapers.institutions.mach import PATTERN as MACH\n"
            "from scrapers.institutions.tenpo import PATTERN as TENPO\n"
            f"root = pathlib.Path({str(FIXTURES)!r})\n"
            "for name, pattern in (('mach_purchase.eml', MACH), ('tenpo_no_message_id.eml', TENPO)):\n"
            "    msg = email.message_from_bytes((root / name).read_bytes())\n"
            "    print(parse_transaction(msg, pattern).external_id)\n"
        )
        expected = [
            parse_transaction(load("mach_purchase.eml"), MACH_PATTERN).external_id,
            parse_transaction(load("tenpo_no_message_id.eml"), TENPO_PATTERN).external_id,
        ]

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=APP_DIR,
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.split() == expected


class FakeMail:
    """In-memory stand-in for imaplib.IMAP4_SSL over fixture emails.

    Message numbers follow list order (oldest first); SEARCH FROM is a
    case-insensitive substring match on the From header, like IMAP's.
    """

    def __init__(self, names: list[str], internal_date: str = "20-Sep-2026 12:00:00 -0300"):
        self.messages = {n: (FIXTURES / name).read_bytes() for n, name in enumerate(names, 1)}
        self.internal_date = internal_date
        self.selected: list[tuple[str, bool]] = []
        self.fetched: list[str] = []
        self.threads: set[threading.Thread] = set()

    def noop(self):
        return "OK", [b""]

    def select(self, mailbox, readonly=False):
        self.selected.append((mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, criteria):
        self.threads.add(threading.current_thread())
        sender = criteria.split('FROM "', 1)[1].split('"', 1)[0].lower()
        hits = [
            str(n)
            for n, raw in self.messages.items()
            if sender in email.message_from_bytes(raw)["From"].lower()
        ]
        return "OK", [" ".join(hits).encode()]

    def fetch(self, msg_id, parts):
        self.fetched.append(str(msg_id))
        raw = self.messages[int(msg_id)]
        envelope = f'{msg_id} (INTERNALDATE "{self.internal_date}" BODY[] {{{len(raw)}}}'
        return "OK", [(envelope.encode(), raw), b")"]


@pytest.fixture
def imap(monkeypatch):
    """Install a FakeMail as the shared session's live connection."""
    monkeypatch.delenv("EMAIL_MAX_MESSAGES", raising=False)
    monkeypatch.delenv("EMAIL_IMAP_MAILBOX", raising=False)

    def install(names: list[str], **kwargs) -> FakeMail:
        mail = FakeMail(names, **kwargs)
        session = ImapSession("imap.example.com", "usuario@example.com", "not-a-secret")
        session._mail = mail
        monkeypatch.setattr(email_mod, "_SESSION", session)
        return mail

    return install


def run(pattern, lookback_days=7):
    return asyncio.run(fetch_transactions_for_pattern(pattern, lookback_days))


class TestFetchTransactions:
    def test_each_email_fetched_once(self, imap):
        """Two sender searches hitting the same email fetch it once; Bci is ignored."""
        mail = imap(["mach_purchase.eml", "bci_bank_purchase.eml", "mach_transfer_in.eml"])

        txs = run(MACH_PATTERN)

        assert sorted(mail.fetched) == ["1", "3"]
        assert [tx.amount for tx in txs] == [-999_999, 1_000_000]

    def test_cap_keeps_the_newest_and_warns(self, imap, monkeypatch, caplog):
        """A truncated window says so instead of silently dropping mail."""
        monkeypatch.setenv("EMAIL_MAX_MESSAGES", "1")
        mail = imap(["mach_purchase.eml", "mach_transfer_in.eml"])

        run(MACH_PATTERN)

        assert mail.fetched == ["2"]
        assert "EMAIL_MAX_MESSAGES" in caplog.text

    @pytest.mark.parametrize("value", ["0", "-5", "many"])
    def test_invalid_cap_fails_the_run(self, imap, monkeypatch, value):
        monkeypatch.setenv("EMAIL_MAX_MESSAGES", value)
        imap(["mach_purchase.eml"])

        with pytest.raises(ValueError):
            run(MACH_PATTERN)

    @pytest.mark.parametrize(
        ("env", "override", "expected"),
        [
            (None, None, '"INBOX"'),
            ("[Gmail]/All Mail", None, '"[Gmail]/All Mail"'),
            ("[Gmail]/All Mail", "Finanzas", '"Finanzas"'),
        ],
    )
    def test_mailbox_is_configurable_and_read_only(self, imap, monkeypatch, env, override, expected):
        """EMAIL_IMAP_MAILBOX, else INBOX; a pattern's own mailbox wins."""
        if env:
            monkeypatch.setenv("EMAIL_IMAP_MAILBOX", env)
        mail = imap(["mach_purchase.eml"])

        run(dataclasses.replace(MACH_PATTERN, mailbox=override))

        assert mail.selected == [(expected, True)]

    def test_internaldate_comes_from_the_fetch(self, imap):
        mail = imap(["mach_bad_date.eml"], internal_date="19-Sep-2026 02:30:00 +0000")

        txs = run(MACH_PATTERN)

        assert mail.fetched == ["1"]
        assert txs[0].transaction_date == date(2026, 9, 18)

    def test_one_broken_email_does_not_sink_the_run(self, imap, monkeypatch, caplog):
        """A parser crash on one email is logged; the others still land."""
        real_parse = email_mod.parse_transaction

        def flaky(msg, pattern, internal_date=None):
            if "transfer-in" in msg["Message-ID"]:
                raise RuntimeError("synthetic parser failure")
            return real_parse(msg, pattern, internal_date)

        monkeypatch.setattr(email_mod, "parse_transaction", flaky)
        imap(["mach_purchase.eml", "mach_transfer_in.eml"])

        txs = run(MACH_PATTERN)

        assert [tx.amount for tx in txs] == [-999_999]
        assert "Could not parse email 2" in caplog.text

    def test_imap_runs_off_the_event_loop(self, imap):
        """Blocking imaplib calls run in a worker thread, not on the loop."""
        mail = imap(["mach_purchase.eml"])

        run(MACH_PATTERN)

        assert mail.threads and threading.main_thread() not in mail.threads


class TestFetchLatestCode:
    def test_reads_the_code_from_the_inbox(self, imap, monkeypatch):
        """2FA codes are read from INBOX whatever EMAIL_IMAP_MAILBOX says."""
        mail = imap(["mach_purchase.eml", "fintual_code.eml"])
        monkeypatch.setenv("EMAIL_IMAP_MAILBOX", "[Gmail]/All Mail")

        code = asyncio.run(
            fetch_latest_code(sender_contains=["hola@fintual.com"], subject_contains=["código para entrar"])
        )

        assert code == "000000"
        assert mail.selected == [('"INBOX"', True)]


class TestQuoteMailbox:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("INBOX", '"INBOX"'),
            ("[Gmail]/All Mail", '"[Gmail]/All Mail"'),
            ('"Already quoted"', '"Already quoted"'),
            ('Say "hi"', '"Say \\"hi\\""'),
        ],
    )
    def test_quoting(self, name, expected):
        assert _quote_mailbox(name) == expected


class TestDecodeHeader:
    def test_plain_ascii(self):
        assert _decode_header_value("Hello World") == "Hello World"

    def test_utf8_encoded(self):
        result = _decode_header_value("=?utf-8?q?Pago_exitoso?=")
        assert "Pago exitoso" in result

    def test_empty(self):
        assert _decode_header_value("") == ""
