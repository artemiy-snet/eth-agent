"""Спільні типи стратегії."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Tick:
    ts: int          # UTC, мс
    price: Decimal


@dataclass(frozen=True)
class Intent:
    """Намір стратегії. Ордери створює тільки модуль execution (Біблія, розділ 6)."""

    ts: int
    bot_id: int
    side: Side
    qty: Decimal
    price: Decimal          # ціна, за якою стратегія прийняла рішення
    reason: str
    quote_qty: Decimal | None = None   # для купівлі «на суму» (quoteOrderQty)
