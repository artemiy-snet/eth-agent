"""Розрахунок ознак з 1m-свічок.

Вихід: DataFrame з колонкою minute (open_time хвилини, мс) і ознаками, які були ВІДОМІ
на початок цієї хвилини:
  atr          — ATR(n) на таймфреймі atr_tf, у ціні
  atr_pct      — ATR / close, %
  ema_slope_pct— зміна EMA(ema_len) на 1h за ema_slope_h годин, %
  anchor_high  — максимум ціни за anchor_h годин (за закритими 1m-свічками)
  ret_1m_pct   — зміна ціни за останню закриту хвилину, % (для запобіжника «рух > 3 %»)
  ema_dist_pct — останнє денне закриття відносно EMA(regime_ema_days) денних закриттів, %
  event_in_h   — годин до наступної запланованої макроподії (FOMC/CPI), event_ago_h — після останньої

Ознаки — float: це не гроші, а вимірювання. Гроші лишаються Decimal.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from agent.features.params import TF_MS, FeatureParams  # noqa: F401


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

    d = _resample(m1, "1d")
    ema_d = d["close"].ewm(span=p.regime_ema_days, adjust=False, min_periods=p.regime_ema_days).mean()
    d["ema_dist_pct"] = (d["close"] / ema_d - 1) * 100

    m1c = m1.assign(available=m1["open_time"] + 60_000)
    m1c["anchor_high"] = m1c["high"].rolling(p.anchor_h * 60, min_periods=1).max()
    m1c["ret_1m_pct"] = (m1c["close"] / m1c["open"] - 1) * 100

    out = _asof(minutes, a, ["atr", "atr_pct"])
    out = out.merge(_asof(minutes, h, ["ema_slope_pct"]), on="minute")
    out = out.merge(_asof(minutes, d, ["ema_dist_pct"]), on="minute")
    out = out.merge(_asof(minutes, m1c, ["anchor_high", "ret_1m_pct"]), on="minute")
    out["event_in_h"], out["event_ago_h"] = event_columns(out["minute"].to_numpy(), p.event_kinds)
    return out


def event_columns(minute_ms: np.ndarray, kinds: str) -> tuple[np.ndarray, np.ndarray]:
    """Векторно, та сама семантика, що calendar.proximity (календар відомий заздалегідь — це не look-ahead)."""
    from agent.features.calendar import event_times

    ts = np.array(event_times(kinds), dtype="int64")
    m = minute_ms.astype("int64")
    if not len(ts):
        nan = np.full(len(m), np.nan)
        return nan, nan.copy()
    i = np.searchsorted(ts, m, side="left")
    nxt = np.where(i < len(ts), (ts[np.minimum(i, len(ts) - 1)] - m) / 3_600_000, np.nan)
    prev = np.where(i > 0, (m - ts[np.maximum(i - 1, 0)]) / 3_600_000, np.nan)
    return nxt, prev
