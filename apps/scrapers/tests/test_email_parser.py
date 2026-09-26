"""Tests for the agnostic email backend + per-institution pattern matching."""

import asyncio
import re
from email import message_from_bytes
from email.message import EmailMessage

import pytest

from scrapers.backends import email as email_backend
from scrapers.backends.email import (
    _decode_header_value,
    _match_pattern,
    _parse_amount,
    _parse_merchant,
)
from scrapers.institutions.mach import PATTERN as MACH_PATTERN
from scrapers.institutions.mercadopago import PATTERN as MP_PATTERN
from scrapers.institutions.tenpo import PATTERN as TENPO_PATTERN


class TestParseAmount:
    """Test Chilean CLP amount parsing."""

    MP = MP_PATTERN.amount_patterns
    MACH = MACH_PATTERN.amount_patterns

    def test_basic_clp(self):
        assert _parse_amount("Pagaste $45.000", self.MP) == 45_000

    def test_millions(self):
        assert _parse_amount("$1.234.567", self.MP) == 1_234_567

    def test_with_label(self):
        assert _parse_amount("Monto: $120.500", self.MP) == 120_500

    def test_no_dots(self):
        assert _parse_amount("$8500", self.MACH) == 8_500

    def test_with_decimal_comma(self):
        """Chilean format: dots for thousands, comma for decimals."""
        assert _parse_amount("$1.234,56", self.MP) == 1_235  # rounded

    def test_pago_de_pattern(self):
        assert _parse_amount("pago de $32.000 en Lider", self.MP) == 32_000

    def test_no_match(self):
        assert _parse_amount("No hay monto aqui", self.MP) is None

    def test_zero_amount(self):
        assert _parse_amount("$0", self.MP) == 0

    def test_large_amount(self):
        assert _parse_amount("$12.345.678", self.MP) == 12_345_678


class TestParseMerchant:
    MP = MP_PATTERN.merchant_patterns

    def test_basic_merchant(self):
        result = _parse_merchant("Pagaste en Supermercado Lider. Gracias", self.MP)
        assert result == "Supermercado Lider"

    def test_merchant_with_newline(self):
        result = _parse_merchant("Pagaste en UBER\nOtro texto", self.MP)
        assert result == "UBER"

    def test_comercio_pattern(self):
        result = _parse_merchant("comercio: Restaurant Los Andes.", self.MP)
        assert result == "Restaurant Los Andes"

    def test_no_match(self):
        result = _parse_merchant("Notificacion de seguridad", self.MP)
        assert result == "Desconocido"

    def test_long_merchant_truncated(self):
        long_name = "A" * 200
        result = _parse_merchant(f"en {long_name}.", self.MP)
        assert len(result) <= 100


class TestMatchPattern:
    """Each institution's PATTERN should match its own senders, not others."""

    def test_mercadopago_sender(self):
        assert _match_pattern(
            "noreply@mercadopago.cl", "Tu pago fue exitoso", MP_PATTERN
        )

    def test_mercadolibre_sender(self):
        assert _match_pattern(
            "info@mercadolibre.cl", "Compra realizada", MP_PATTERN
        )

    def test_mach_sender(self):
        assert _match_pattern(
            "notificaciones@somosmach.com", "Compra aprobada", MACH_PATTERN
        )

    def test_tenpo_sender(self):
        assert _match_pattern(
            "info@tenpo.cl", "Transaccion exitosa", TENPO_PATTERN
        )

    @pytest.mark.parametrize(
        "sender",
        [
            "Bci <notificaciones@bci.cl>",
            "contacto@bci.cl",
            "Banco Bci <alertas@mail.bci.cl>",
        ],
    )
    def test_banco_bci_sender_is_not_mach(self, sender):
        """A Banco BCI mail with a subject MACH accepts still isn't MACH's."""
        assert not _match_pattern(
            sender, "Comprobante de transferencia", MACH_PATTERN
        )

    def test_unknown_sender_rejects(self):
        assert not _match_pattern("noreply@other.com", "Hello", MP_PATTERN)
        assert not _match_pattern("noreply@other.com", "Hello", MACH_PATTERN)
        assert not _match_pattern("noreply@other.com", "Hello", TENPO_PATTERN)

    def test_matching_sender_wrong_subject(self):
        """MercadoPago sender but irrelevant subject should not match."""
        assert not _match_pattern(
            "noreply@mercadopago.cl", "Actualiza tu perfil", MP_PATTERN
        )

    def test_subject_substring_match(self):
        """'MercadoPago' contains 'pago' so it matches the subject filter."""
        assert _match_pattern(
            "noreply@mercadopago.cl", "Bienvenido a MercadoPago", MP_PATTERN
        )

    def test_case_insensitive_sender(self):
        assert _match_pattern(
            "NoReply@MercadoPago.CL", "Tu pago ok", MP_PATTERN
        )

    def test_case_insensitive_subject(self):
        assert _match_pattern(
            "noreply@mercadopago.cl", "PAGO EXITOSO", MP_PATTERN
        )


class TestDecodeHeader:
    def test_plain_ascii(self):
        assert _decode_header_value("Hello World") == "Hello World"

    def test_utf8_encoded(self):
        result = _decode_header_value("=?utf-8?q?Pago_exitoso?=")
        assert "Pago exitoso" in result

    def test_empty(self):
        assert _decode_header_value("") == ""


def _synthetic_mail(sender: str, subject: str, body: str, message_id: str) -> bytes:
    """A fabricated notification email: every figure and id in it is synthetic."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Mon, 07 Sep 2026 12:00:00 -0300"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


class _FakeMailbox:
    """In-memory IMAP double: `FROM` search is a case-insensitive substring
    match on the From header, as RFC 3501 specifies."""

    def __init__(self, messages: list[bytes]):
        self._messages = messages

    def search(self, charset, criteria):
        keyword = re.search(r'FROM "([^"]+)"', criteria).group(1).lower()
        ids = [
            str(i + 1).encode()
            for i, raw in enumerate(self._messages)
            if keyword in message_from_bytes(raw).get("From", "").lower()
        ]
        return "OK", [b" ".join(ids)]

    def fetch(self, msg_id, parts):
        raw = self._messages[int(msg_id) - 1]
        return "OK", [(msg_id + b" (RFC822 {%d}" % len(raw), raw), b")"]


class _FakeSession:
    def __init__(self, mailbox: _FakeMailbox):
        self._mailbox = mailbox

    async def __aenter__(self):
        return self._mailbox

    async def __aexit__(self, *exc):
        return None


class TestFetchTransactionsForPattern:
    def test_banco_bci_mail_is_not_imported_as_mach(self, monkeypatch):
        """Only the MACH email lands; the Banco BCI one beside it is left alone."""
        mailbox = _FakeMailbox([
            _synthetic_mail(
                "MACH <notificaciones@somosmach.com>",
                "Compra aprobada",
                "Compraste en Comercio de Prueba por $ 999.999.",
                "<synthetic-mach@example.test>",
            ),
            _synthetic_mail(
                "Bci <notificaciones@bci.cl>",
                "Comprobante de transferencia",
                "Transferiste $ 1.000.000 a Cuenta de Prueba.",
                "<synthetic-bci@example.test>",
            ),
        ])
        monkeypatch.setattr(
            email_backend, "get_session", lambda: _FakeSession(mailbox)
        )

        transactions = asyncio.run(
            email_backend.fetch_transactions_for_pattern(MACH_PATTERN)
        )

        assert {t.amount for t in transactions} == {-999_999}
