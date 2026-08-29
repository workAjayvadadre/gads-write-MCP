"""Money. The only place in this codebase that converts to or from micros.

Google Ads expresses currency in *micros* - millionths of the unit.
500.00 rupees is 500_000_000 micros. A missing conversion is a 1,000,000x
error that type-checks perfectly, which is why every conversion funnels
through here and every variable carries its unit in its name.

Naming rule enforced by review: `*_micros` or `*_units`. Never a bare
`budget`, `amount`, or `bid`.

Python notes for a TypeScript reader:
  - `Decimal` is exact decimal arithmetic, unlike float. We use it for money
    everywhere; JS has no built-in equivalent, which is why that ecosystem
    reaches for decimal.js.
  - Passing a float into Decimal captures binary rounding noise, so we
    convert via `str()` first. See `coerce_units`.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MICROS_PER_UNIT = 1_000_000

# An absurdity backstop, NOT a policy limit. Policy limits live in
# config/policy.yaml and are enforced in safety/policy.py. This exists only
# to turn a catastrophic typo (a budget pasted with extra zeros, or a value
# that was already in micros being converted a second time) into a loud
# error rather than a very large number.
MAX_REASONABLE_UNITS = Decimal("100000000")  # 100 million currency units

_CENTS = Decimal("0.01")


class MoneyError(ValueError):
    """Raised when a monetary value cannot be trusted."""


def coerce_units(amount: object, *, field: str = "amount") -> Decimal:
    """Turn caller input into an exact Decimal, or raise."""
    if isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, bool):
        # bool is a subclass of int in Python. Silently treating True as
        # 1 rupee would be absurd, so reject it explicitly.
        raise MoneyError(f"{field}: expected a number, got a boolean")
    elif isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, float):
        # str() first: Decimal(0.1) is 0.1000000000000000055511151231257827,
        # Decimal("0.1") is exactly 0.1.
        value = Decimal(str(amount))
    elif isinstance(amount, str):
        try:
            value = Decimal(amount.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"{field}: {amount!r} is not a number") from exc
    else:
        raise MoneyError(f"{field}: expected a number, got {type(amount).__name__}")

    if not value.is_finite():
        raise MoneyError(f"{field}: {amount!r} is not a finite number")
    return value


def to_micros(amount: object, *, field: str = "amount") -> int:
    """Convert whole currency units to integer micros.

    >>> to_micros(500)
    500000000
    >>> to_micros("12.34")
    12340000
    """
    value = coerce_units(amount, field=field)

    if value < 0:
        raise MoneyError(f"{field}: must not be negative, got {value}")
    if value > MAX_REASONABLE_UNITS:
        raise MoneyError(
            f"{field}: {value} exceeds the sanity ceiling of {MAX_REASONABLE_UNITS}. "
            "This usually means a value already in micros was converted a second "
            "time, or a typo added zeros."
        )

    micros = (value * MICROS_PER_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(micros)


def from_micros(micros: object, *, field: str = "amount_micros") -> Decimal:
    """Convert integer micros back to currency units, exact to 2 decimals."""
    if isinstance(micros, bool) or not isinstance(micros, int):
        raise MoneyError(
            f"{field}: micros must be an integer, got {type(micros).__name__}. "
            "A float here means a conversion happened somewhere it should not have."
        )
    if micros < 0:
        raise MoneyError(f"{field}: must not be negative, got {micros}")
    return (Decimal(micros) / MICROS_PER_UNIT).quantize(_CENTS, rounding=ROUND_HALF_UP)


def format_units(amount: object, currency_code: str, *, field: str = "amount") -> str:
    """Render a currency-unit amount for a human-readable preview.

    Previews are what someone reads before approving a spend change, so this
    is deliberately unambiguous: currency code, thousands separators, always
    two decimals.
    """
    value = coerce_units(amount, field=field).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return f"{currency_code} {value:,.2f}"


def format_micros(micros: int, currency_code: str) -> str:
    """Render micros for a human-readable preview."""
    return format_units(from_micros(micros), currency_code)


def percent_change(from_units: object, to_units: object) -> Decimal:
    """Percentage change between two currency-unit amounts.

    Raises if `from_units` is zero: the percentage increase from zero is
    undefined, and silently treating it as 0% or as infinity would let a
    zero-budget campaign be raised to anything in a single step. Callers
    must handle that case explicitly.
    """
    start = coerce_units(from_units, field="from_units")
    end = coerce_units(to_units, field="to_units")
    if start == 0:
        raise MoneyError(
            "percent change from zero is undefined; the caller must handle a "
            "zero starting amount explicitly rather than treating it as 0%"
        )
    return ((end - start) / start * 100).quantize(Decimal("0.0001"))
