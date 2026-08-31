"""Micros conversion. A bug here is a 1,000,000x bug."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gads_write.safety.units import (
    MAX_REASONABLE_UNITS,
    MoneyError,
    format_units,
    from_micros,
    percent_change,
    to_micros,
)


@pytest.mark.parametrize(
    ("units", "expected_micros"),
    [
        (0, 0),
        (1, 1_000_000),
        (500, 500_000_000),
        ("12.34", 12_340_000),
        (Decimal("0.01"), 10_000),
        (2500.50, 2_500_500_000),
    ],
)
def test_to_micros(units: object, expected_micros: int) -> None:
    assert to_micros(units) == expected_micros


def test_float_does_not_pick_up_binary_noise() -> None:
    # Decimal(0.1) would be 0.1000000000000000055511151231257827.
    # Going through str() keeps it exact.
    assert to_micros(0.1) == 100_000
    assert to_micros(0.7) == 700_000


def test_round_trip() -> None:
    assert from_micros(to_micros("1234.56")) == Decimal("1234.56")


def test_micros_must_be_int_not_float() -> None:
    # A float in micros means someone divided somewhere they should not have.
    with pytest.raises(MoneyError, match="integer"):
        from_micros(500_000_000.0)


def test_bool_is_rejected() -> None:
    # bool subclasses int in Python; True must not become 1 rupee.
    with pytest.raises(MoneyError, match="boolean"):
        to_micros(True)


def test_negative_rejected() -> None:
    with pytest.raises(MoneyError, match="negative"):
        to_micros(-1)


def test_double_conversion_is_caught_by_the_sanity_ceiling() -> None:
    # The classic disaster: a value already in micros gets converted again.
    already_micros = 500_000_000
    with pytest.raises(MoneyError, match="sanity ceiling"):
        to_micros(already_micros)


def test_sanity_ceiling_boundary() -> None:
    assert to_micros(MAX_REASONABLE_UNITS) > 0
    with pytest.raises(MoneyError):
        to_micros(MAX_REASONABLE_UNITS + 1)


def test_percent_change() -> None:
    assert percent_change(100, 125) == Decimal("25.0000")
    assert percent_change(1000, 900) == Decimal("-10.0000")


def test_percent_change_from_zero_raises_rather_than_guessing() -> None:
    # Treating this as 0% would let a zero budget be raised to anything.
    with pytest.raises(MoneyError, match="undefined"):
        percent_change(0, 5000)


def test_format_units_is_unambiguous() -> None:
    assert format_units("1234.5", "INR") == "INR 1,234.50"
