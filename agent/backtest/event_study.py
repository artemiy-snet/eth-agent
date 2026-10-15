"""Подієве дослідження: чи ETH поводиться інакше навколо FOMC / CPI, ніж у звичайний час.

Для кожної події: рух ціни за вікно [подія − before_h; подія + after_h] і найбільше падіння від
початку вікна всередині нього. Те саме для всіх інших вікон такої ж довжини (контроль). Якщо навколо
подій падіння не глибші й волатильність не вища — фільтр календаря нічого не дасть, крім втрати угод.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from agent.features.calendar import events


def _windows(close: pd.Series, starts: np.ndarray, length_min: int) -> tuple[np.ndarray, np.ndarray]:
    """close — індекс у хвилинах (мс). Повертає (рух %, макс. падіння %) для кожного старту."""
    idx = close.index.to_numpy()
    px = close.to_numpy()
    rets, dds = [], []
    for s in starts:
        i0 = np.searchsorted(idx, s)
        i1 = np.searchsorted(idx, s + length_min * 60_000)
        if i0 >= len(px) or i1 - i0 < length_min * 0.9:
            continue
        seg = px[i0:i1]
        rets.append((seg[-1] / seg[0] - 1) * 100)
        dds.append((seg.min() / seg[0] - 1) * 100)
    return np.array(rets), np.array(dds)


def study(df: pd.DataFrame, before_h: int = 24, after_h: int = 24) -> dict:
    close = pd.Series(df["close"].astype(float).to_numpy(), index=df["open_time"].astype("int64").to_numpy())
    t0, t1 = int(close.index[0]), int(close.index[-1])
    length = (before_h + after_h) * 60
    out = {}
    all_ev = [t for t, _ in events("fomc,cpi")]
    for kind in ("fomc", "cpi", "fomc,cpi"):
        ev = np.array([t for t, _ in events(kind) if t0 + before_h * 3_600_000 <= t <= t1 - after_h * 3_600_000])
        r, d = _windows(close, ev - before_h * 3_600_000, length)
        out[kind] = r, d
    # контроль: старти щодоби о тій самій годині, далеко (> 72 год) від будь-якої події
    starts = np.arange(t0, t1 - length * 60_000, 86_400_000)
    ae = np.array(all_ev)
    far = np.array([np.abs(ae - (s + before_h * 3_600_000)).min() > 72 * 3_600_000 for s in starts])
    out["контроль"] = _windows(close, starts[far], length)
    return {k: _stats(r, d) for k, (r, d) in out.items()}


def _stats(r: np.ndarray, d: np.ndarray) -> dict:
    if not len(r):
        return {"n": 0}
    return {"n": int(len(r)), "mean_ret": float(r.mean()), "abs_ret": float(np.abs(r).mean()),
            "std": float(r.std()), "mean_dd": float(d.mean()), "p10_dd": float(np.percentile(d, 10)),
            "worst_dd": float(d.min()), "share_dd_gt5": float((d < -5).mean() * 100)}


def report_md(s: dict, before_h: int, after_h: int) -> str:
    f = lambda x: f"{x:.2f}"
    names = {"fomc": "FOMC", "cpi": "CPI", "fomc,cpi": "FOMC + CPI", "контроль": "звичайні дні (контроль)"}
    L = [f"### Подієве дослідження: вікно −{before_h} год … +{after_h} год", "",
         "| Вікна | n | Сер. рух, % | Сер. модуль руху, % | Std, % | Сер. падіння, % | 10-й перц. падіння, % | Найгірше, % | Падіння > 5 %, частка |",
         "|---|---|---|---|---|---|---|---|---|"]
    for k, v in s.items():
        if not v.get("n"):
            continue
        L.append(f"| {names[k]} | {v['n']} | {f(v['mean_ret'])} | {f(v['abs_ret'])} | {f(v['std'])} | "
                 f"{f(v['mean_dd'])} | {f(v['p10_dd'])} | {f(v['worst_dd'])} | {f(v['share_dd_gt5'])} % |")
    L += ["", "Падіння — від ціни на початку вікна до мінімуму всередині нього.", ""]
    return "\n".join(L)
