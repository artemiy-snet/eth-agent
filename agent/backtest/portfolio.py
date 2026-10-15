"""Портфельний бектест: кілька стратегій («рукавів») на частках одного капіталу.

Кожен рукав проганяється один раз на всьому капіталі; портфель = зважена сума нормованих денних
кривих капіталу. Рукави незалежні (окремі бюджети, окремий ризик-модуль), тому сума точна з точністю
до округлень кількості — просадка і Sharpe портфеля рахуються з реальної спільної кривої.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Рукави — фіксовані гіпотези з docs/strategies.md (не підбираються під результат)
SLEEVES: dict[str, dict] = {
    "A": {"strategy": "v3", "label": "Денний відскок (поточний)", "set": []},
    "B": {"strategy": "v3", "label": "Відскок розширений",
          "set": ["v3.filters.atr_max_pct=15", "v3.max_hold_h=168"]},
    "B1": {"strategy": "v3", "label": "Відскок розширений, ризик 1 %",
           "set": ["v3.filters.atr_max_pct=15", "v3.max_hold_h=168", "v3.risk_per_trade_pct=1",
                   "risk.max_open_risk_pct=5"]},
    "C": {"strategy": "trend", "label": "Режимне утримання (100 % рукава, EMA200d)",
          "set": ["trend.alloc_pct=100", "trend.breakout=false", "trend.k_stop_atr=0", "trend.k_trail_atr=0",
                  "risk.max_dd_pct=100", "risk.daily_loss_pct=100", "risk.max_open_risk_pct=100",
                  "risk.max_eth_share_pct=100"]},
}

PRESETS: dict[str, dict[str, float]] = {
    "поточний": {"A": 1.0},
    "обережний": {"B": 1.0},
    "збалансований": {"B1": 0.7, "C": 0.3},
    "агресивний": {"B1": 0.5, "C": 0.5},
    "лише_режим": {"C": 1.0},
}


@dataclass
class Curve:
    days: np.ndarray      # UTC-дні
    equity: np.ndarray    # капітал


def run_sleeve(df: pd.DataFrame, raw_cfg: dict, name: str):
    from agent.backtest.v3_engine import run_v3
    from agent.config import Config, apply_overrides
    from agent.data.exchange_info import ETHUSDT_DEFAULT
    from agent.features import FeatureParams
    from agent.risk import RiskParams
    from agent.strategy import trend

    import copy
    sl = SLEEVES[name]
    cfg = Config.model_validate(apply_overrides(copy.deepcopy(raw_cfg), ["agent.mode=backtest", *sl["set"]]))
    f = ETHUSDT_DEFAULT
    fp = FeatureParams(**cfg.features.model_dump())
    rp = RiskParams(**cfg.risk.model_dump())
    if sl["strategy"] == "trend":
        fp = dataclasses.replace(fp, atr_tf="1d", anchor_h=cfg.trend.breakout_days * 24)
        return run_v3(df, cfg.trend.to_params(f.step_size, f.min_notional), rp, fp, cfg.backtest.slippage_pct,
                      strategy=trend)
    return run_v3(df, cfg.v3.to_params(f.step_size, f.min_notional), rp, fp, cfg.backtest.slippage_pct)


def stats(days: np.ndarray, eq: np.ndarray) -> dict:
    rets = np.diff(eq) / eq[:-1]
    peak = np.maximum.accumulate(eq)
    dd = float(((peak - eq) / peak).max() * 100)
    years = max(len(eq) / 365, 1e-9)
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1
    sharpe = float(rets.mean() / rets.std() * math.sqrt(365)) if rets.std() > 0 else 0.0
    return {"pnl_pct": (eq[-1] / eq[0] - 1) * 100, "cagr_pct": cagr * 100, "max_dd_pct": dd, "sharpe": sharpe,
            "calmar": (cagr * 100 / dd) if dd > 0 else 0.0}


def combine(curves: dict[str, Curve], weights: dict[str, float]) -> Curve:
    days = sorted(set().union(*[set(curves[k].days.tolist()) for k in weights]))
    total = np.zeros(len(days))
    for k, w in weights.items():
        s = pd.Series(curves[k].equity, index=curves[k].days).reindex(days).ffill().bfill().to_numpy()
        total += w * s / s[0]
    return Curve(np.array(days), total)


def buy_and_hold(df: pd.DataFrame) -> Curve:
    d = pd.DataFrame({"day": df["open_time"].astype("int64") // 86_400_000, "c": df["close"].astype(float)})
    g = d.groupby("day")["c"].last()
    return Curve(g.index.to_numpy(), g.to_numpy())


def run_all(df: pd.DataFrame, raw_cfg: dict, presets: list[str] | None = None, log=print,
            workers: int | None = None) -> dict:
    presets = presets or list(PRESETS)
    need = sorted({k for p in presets for k in PRESETS[p]})
    curves, sleeve_rows = {}, {}
    log(f"рукави {need} …")
    if workers and workers > 1 and len(need) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(min(workers, len(need))) as ex:
            reps = dict(zip(need, ex.map(run_sleeve, [df] * len(need), [raw_cfg] * len(need), need)))
    else:
        reps = {k: run_sleeve(df, raw_cfg, k) for k in need}
    for k in need:
        rep = reps[k]
        c = np.array(rep.equity_curve, dtype=float)
        curves[k] = Curve(c[:, 0], c[:, 1])
        s = rep.summary()
        sleeve_rows[k] = {"label": SLEEVES[k]["label"], "trades": s["trades"], "pf": s["profit_factor_closed"],
                          **stats(curves[k].days, curves[k].equity)}
    out = {"sleeves": sleeve_rows, "portfolios": {}}
    for p in presets:
        c = combine(curves, PRESETS[p])
        out["portfolios"][p] = {"weights": PRESETS[p], **stats(c.days, c.equity)}
    bh = buy_and_hold(df)
    out["buy_and_hold"] = stats(bh.days, bh.equity)
    return out


def report_md(r: dict, start: str, end: str) -> str:
    f = lambda x: f"{x:.2f}"
    L = [f"## Портфелі · {start} … {end}", "",
         "Капітал 500 USDT, прибуток реінвестується, комісія 0,1 %, прослизання 0,05 %.", "",
         "### Портфелі", "", "| Портфель | Склад | Результат, % | Річна, % | Макс. просадка, % | Sharpe | Calmar |",
         "|---|---|---|---|---|---|---|"]
    for name, p in r["portfolios"].items():
        comp = " + ".join(f"{int(w * 100)} % {k}" for k, w in p["weights"].items())
        L.append(f"| {name} | {comp} | {f(p['pnl_pct'])} | {f(p['cagr_pct'])} | {f(p['max_dd_pct'])} | "
                 f"{f(p['sharpe'])} | {f(p['calmar'])} |")
    b = r["buy_and_hold"]
    L.append(f"| просто тримати ETH | 100 % ETH | {f(b['pnl_pct'])} | {f(b['cagr_pct'])} | {f(b['max_dd_pct'])} | "
             f"{f(b['sharpe'])} | {f(b['calmar'])} |")
    L += ["", "### Рукави", "", "| Рукав | Опис | Результат, % | Річна, % | Макс. просадка, % | Sharpe | Угод | PF |",
          "|---|---|---|---|---|---|---|---|"]
    for k, s in r["sleeves"].items():
        L.append(f"| {k} | {s['label']} | {f(s['pnl_pct'])} | {f(s['cagr_pct'])} | {f(s['max_dd_pct'])} | "
                 f"{f(s['sharpe'])} | {s['trades']} | {s['pf']} |")
    L += ["", "Calmar = річна дохідність / макс. просадка (більше — краще співвідношення дохід/ризик).", ""]
    return "\n".join(L)
