"""Ознаки для живого агента без pandas — та сама семантика, що agent/features/core.py:
лише закриті свічки, ATR Вайлдера, EMA зі span, якір = максимум закритих 1m-свічок.

Свічка — кортеж (open_time_ms, open, high, low, close) у float.
"""

from __future__ import annotations

import math

from agent.features.params import TF_MS, FeatureParams
from agent.money import D
from agent.strategy.v3 import Features

Candle = tuple[int, float, float, float, float]


def closed(candles: list[Candle], tf_ms: int, now_ms: int) -> list[Candle]:
    return [c for c in candles if c[0] + tf_ms <= now_ms]


def atr_wilder(candles: list[Candle], n: int) -> float:
    """Як pandas: TR[0] = NaN, далі ewm(alpha=1/n, adjust=False, min_periods=n)."""
    val, count, prev_close = None, 0, None
    for _, _, h, l, c in candles:
        if prev_close is not None:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            val = tr if val is None else val + (tr - val) / n
            count += 1
        prev_close = c
    return val if count >= n else math.nan


def ema_series(xs: list[float], span: int) -> list[float]:
    a = 2 / (span + 1)
    out, v = [], None
    for i, x in enumerate(xs):
        v = x if v is None else v + a * (x - v)
        out.append(v if i + 1 >= span else math.nan)
    return out


def compute(k_atr: list[Candle], k_1h: list[Candle], k_1m: list[Candle], now_ms: int,
            p: FeatureParams) -> tuple[Features, float]:
    """Повертає (ознаки для стратегії, рух останньої закритої хвилини у %)."""
    tf = TF_MS[p.atr_tf]
    ca = closed(k_atr, tf, now_ms)
    atr = atr_wilder(ca, p.atr_len) if ca else math.nan
    atr_pct = atr / ca[-1][4] * 100 if ca and not math.isnan(atr) else math.nan

    ch = closed(k_1h, TF_MS["1h"], now_ms)
    ema = ema_series([c[4] for c in ch], p.ema_len)
    slope = math.nan
    if len(ema) > p.ema_slope_h and not math.isnan(ema[-1]) and not math.isnan(ema[-1 - p.ema_slope_h]):
        slope = (ema[-1] / ema[-1 - p.ema_slope_h] - 1) * 100

    cm = closed(k_1m, TF_MS["1m"], now_ms)
    window = cm[-p.anchor_h * 60:]
    anchor = max(c[2] for c in window) if window else math.nan
    ret_1m = (cm[-1][4] / cm[-1][1] - 1) * 100 if cm else math.nan

    dec = lambda x: None if x is None or math.isnan(x) else D(f"{x:.6f}")
    return Features(atr=dec(atr), atr_pct=dec(atr_pct), ema_slope_pct=dec(slope), anchor_high=dec(anchor)), ret_1m
