"""Tests for the email parser scraper."""

import pytest
from scrapers.email_parser import (
    _parse_amount,
    _parse_merchant,
    _match_pattern,
    _decode_header_value,
    PATTERNS,
)


class TestParseAmount:
    """Test Chilean CLP amount parsing."""

    MP = PATTERNS[0].amount_patterns  # MercadoPago patterns
    MACH = PATTERNS[1].amount_patterns

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
    MP = PATTERNS[0].merchant_patterns

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
    def test_mercadopago_sender(self):
        p = _match_pattern("noreply@mercadopago.cl", "Tu pago fue exitoso", PATTERNS)
        assert p is not None
        assert p.institution == "mercadopago"

    def test_mercadolibre_sender(self):
        p = _match_pattern("info@mercadolibre.cl", "Compra realizada", PATTERNS)
        assert p is not None
        assert p.institution == "mercadopago"

    def test_mach_sender(self):
        p = _match_pattern("notificaciones@somosmach.com", "Compra aprobada", PATTERNS)
        assert p is not None
        assert p.institution == "mach"

    def test_tenpo_sender(self):
        p = _match_pattern("info@tenpo.cl", "Transaccion exitosa", PATTERNS)
        assert p is not None
        assert p.institution == "tenpo"

    def test_unknown_sender(self):
        p = _match_pattern("noreply@other.com", "Hello", PATTERNS)
        assert p is None

    def test_matching_sender_wrong_subject(self):
        """MercadoPago sender but truly irrelevant subject should not match."""
        p = _match_pattern("noreply@mercadopago.cl", "Actualiza tu perfil", PATTERNS)
        assert p is None

    def test_subject_substring_match(self):
        """'MercadoPago' contains 'pago' so it matches the subject filter."""
        p = _match_pattern("noreply@mercadopago.cl", "Bienvenido a MercadoPago", PATTERNS)
        assert p is not None

    def test_case_insensitive_sender(self):
        p = _match_pattern("NoReply@MercadoPago.CL", "Tu pago ok", PATTERNS)
        assert p is not None
        assert p.institution == "mercadopago"

    def test_case_insensitive_subject(self):
        p = _match_pattern("noreply@mercadopago.cl", "PAGO EXITOSO", PATTERNS)
        assert p is not None


class TestDecodeHeader:
    def test_plain_ascii(self):
        assert _decode_header_value("Hello World") == "Hello World"

    def test_utf8_encoded(self):
        # RFC 2047 encoded header
        result = _decode_header_value("=?utf-8?q?Pago_exitoso?=")
        assert "Pago exitoso" in result

    def test_empty(self):
        assert _decode_header_value("") == ""
