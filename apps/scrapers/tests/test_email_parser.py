"""Tests for the agnostic email backend + per-institution pattern matching."""

import pytest

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
