"""Гроші тільки в Decimal (Біблія, розділ 6: float для грошей заборонено)."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, getcontext

getcontext().prec = 28

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
THOUSAND = Decimal(1000)


def D(x: object) -> Decimal:
    """Безпечне перетворення в Decimal (float іде через str, щоб не тягнути двійковий шум)."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(str(x))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Округлення вниз до кроку (tickSize / stepSize)."""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def pct(base: Decimal, p: Decimal) -> Decimal:
    """base × (1 + p/100)."""
    return base + base * p / HUNDRED
