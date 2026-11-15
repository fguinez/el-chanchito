from decimal import Decimal

import pytest

from scrapers.backends.percent import parse_percent


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("1.23 %", Decimal("1.23"), id="dot"),
        pytest.param("1,23 %", Decimal("1.23"), id="comma"),
        pytest.param("1.23", Decimal("1.23"), id="bare"),
        pytest.param(" 4,50 ", Decimal("4.50"), id="padded"),
    ],
)
def test_parse_percent(raw, expected):
    """A lone percent figure parses with either decimal separator."""
    assert parse_percent(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, 1.0, "", "abc %", "1,23 % diario", "1.0.0", "1.234,5", "-0,5"]
)
def test_parse_percent_rejects_junk(raw):
    """Anything but a lone unsigned percent figure is None by default."""
    assert parse_percent(raw) is None


@pytest.mark.parametrize("raw", ["-0,5", "−0.5", "-0.5 %"])
def test_signed_reads_either_minus(raw):
    """With `signed`, an ASCII or U+2212 minus makes the figure negative."""
    assert parse_percent(raw, signed=True) == Decimal("-0.5")
