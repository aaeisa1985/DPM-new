"""Exact decimal money/quantity handling (D9).

Every monetary or quantity column is TEXT in SQLite through ``Money`` and is
quantised to 8 decimal places. API responses format numbers according to the
instrument type so clients always receive strings.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

E8 = Decimal("0.00000001")
E4 = Decimal("0.0001")
THREE_DECIMAL_CURRENCIES = {"KWD", "BHD", "OMR"}


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def q8(value: Any) -> Decimal:
    return D(value).quantize(E8, rounding=ROUND_HALF_UP)


def q4(value: Any) -> Decimal:
    return D(value).quantize(E4, rounding=ROUND_HALF_UP)


def q_ccy(value: Any, currency: str = "USD") -> Decimal:
    step = Decimal("0.001") if currency.upper() in THREE_DECIMAL_CURRENCIES else Decimal("0.01")
    return D(value).quantize(step, rounding=ROUND_HALF_UP)


def s_qty(value: Any) -> str:
    """Quantities: up to 8 dp, trailing zeros removed (e.g. ``12``)."""
    text = format(q8(value), "f").rstrip("0").rstrip(".")
    return text or "0"


def s_price(value: Any) -> str:
    """Prices: 4 dp, trailing zeros removed."""
    text = format(q4(value), "f").rstrip("0").rstrip(".")
    return text or "0"


def s_cash(value: Any, currency: str = "USD") -> str:
    """Cash: 2 dp (3 for KWD/BHD/OMR)."""
    return format(q_ccy(value, currency), "f")


def floor_shares(value: Decimal) -> Decimal:
    return D(value).quantize(Decimal("1"), rounding=ROUND_DOWN)


class Money(TypeDecorator):
    """Store decimals as TEXT to avoid binary floating point drift."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return format(q8(value), "f")

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return D(value)
