"""Параметри ознак (без залежностей — використовуються і в живому агенті)."""

from __future__ import annotations

from dataclasses import dataclass

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "4h": 14_400_000, "1d": 86_400_000}


@dataclass(frozen=True)
class FeatureParams:
    atr_tf: str = "1d"
    atr_len: int = 14
    ema_len: int = 200
    ema_slope_h: int = 24
    anchor_h: int = 24
    regime_ema_days: int = 200      # режим: ціна відносно EMA денних закриттів
    event_kinds: str = "fomc,cpi"   # макроподії для гейту (agent/features/calendar.py)
