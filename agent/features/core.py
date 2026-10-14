"""Розрахунок ознак з 1m-свічок.

Вихід: DataFrame з колонкою minute (open_time хвилини, мс) і ознаками, які були ВІДОМІ
на початок цієї хвилини:
  atr          — ATR(n) на таймфреймі atr_tf, у ціні
  atr_pct      — ATR / close, %
  ema_slope_pct— зміна EMA(ema_len) на 1h за ema_slope_h годин, %
  anchor_high  — максимум ціни за anchor_h годин (за закритими 1m-свічками)
  ret_1m_pct   — зміна ціни за останню закриту хвилину, % (для запобіжника «рух > 3 %»)

Ознаки — float: це не гроші, а вимірювання. Гроші лишаються Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


@dataclass(frozen=True)
class FeatureParams:
    atr_tf: str = "1h"
    atr_len: int = 14
    ema_len: int = 200
    ema_slope_h: int = 24
    anchor_h: int = 24


def _ohlc(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({"open_time": df["open_time"].astype("int64")})
    for c in ("open", "high", "low", "close"):
        out[c] = df[c].astype(float)
    return out


def _resample(m1: pd.DataFrame, tf: str) -> pd.DataFrame:
    step = TF_MS[tf]
    g = m1.assign(bucket=m1["open_time"] // step * step).groupby("bucket", sort=True)
    r = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
              n=("open", "size")).reset_index().rename(columns={"bucket": "open_time"})
    r["available"] = r["open_time"] + step          # свічка закрита й відома з цього моменту
    return r


def _atr(c: pd.DataFrame, n: int) -> pd.Series:
    prev = c["close"].shift(1)
    tr = np.maximum(c["high"] - c["low"], np.maximum((c["high"] - prev).abs(), (c["low"] - prev).abs()))
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()       # Wilder


def _asof(minutes: pd.Series, src: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    left = pd.DataFrame({"minute": minutes.astype("int64")})
    right = src[["available", *cols]].rename(columns={"available": "minute"}).sort_values("minute")
    right["minute"] = right["minute"].astype("int64")
    return pd.merge_asof(left, right, on="minute", direction="backward")


def compute_features(df: pd.DataFrame, p: FeatureParams = FeatureParams()) -> pd.DataFrame:
    m1 = _ohlc(df)
    minutes = m1["open_time"]

    a = _resample(m1, p.atr_tf)
    a["atr"] = _atr(a, p.atr_len)
    a["atr_pct"] = a["atr"] / a["close"] * 100

    h = _resample(m1, "1h")
    ema = h["close"].ewm(span=p.ema_len, adjust=False, min_periods=p.ema_len).mean()
    h["ema_slope_pct"] = (ema / ema.shift(p.ema_slope_h) - 1) * 100

    m1c = m1.assign(available=m1["open_time"] + 60_000)
    m1c["anchor_high"] = m1c["high"].rolling(p.anchor_h * 60, min_periods=1).max()
    m1c["ret_1m_pct"] = (m1c["close"] / m1c["open"] - 1) * 100

    out = _asof(minutes, a, ["atr", "atr_pct"])
    out = out.merge(_asof(minutes, h, ["ema_slope_pct"]), on="minute")
    out = out.merge(_asof(minutes, m1c, ["anchor_high", "ret_1m_pct"]), on="minute")
    return out
