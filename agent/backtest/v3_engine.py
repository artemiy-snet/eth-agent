"""Бектест v3: стратегія → ризик (вето) → модель виконання (комісія + прослизання) → on_fill.

Той самий ланцюжок буде в paper/live, лише модель виконання заміниться на execution (етап 6).
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import numpy as np
import pandas as pd

from agent.backtest.engine import TICK_SECONDS, Report, Trade, ticks_from_klines
from agent.features import FeatureParams, compute_features
from agent.money import HUNDRED, ZERO, D
from agent.risk import RiskContext, RiskGuard, RiskParams
from agent.strategy import v3
from agent.strategy.types import Fill, Side


def _dec(x: float) -> Decimal | None:
    return None if x is None or np.isnan(x) else D(f"{x:.6f}")


def run_v3(df: pd.DataFrame, p: v3.V3Params, rp: RiskParams = RiskParams(),
           fp: FeatureParams = FeatureParams(), slippage_pct: Decimal = D("0.05"),
           feats: pd.DataFrame | None = None, strategy=v3) -> Report:
    """feats — передраховані ознаки (з прогрітою історією до початку df), інакше рахуються з df.
    strategy — модуль стратегії з init_state/step/on_fill/on_reject (v3 або trend)."""
    if feats is None:
        feats = compute_features(df, fp)
    fmap = {int(m): i for i, m in enumerate(feats["minute"].to_numpy())}
    cols = {c: feats[c].to_numpy() for c in ("atr", "atr_pct", "ema_slope_pct", "anchor_high", "ret_1m_pct", "ema_dist_pct")}

    state = strategy.init_state(p)
    risk = RiskGuard(rp)
    trades: list[Trade] = []
    entries: dict[int, tuple[int, Decimal, Decimal]] = {}
    reasons: Counter = Counter()
    vetoes: Counter = Counter()
    gate_blocks: Counter = Counter()

    first_px = D(df["open"].iloc[0])
    start_eq = state.equity(first_px)
    peak, max_dd = start_eq, ZERO
    last_px, cur_min, f = first_px, None, v3.Features(atr=None)
    slip = slippage_pct / HUNDRED
    fee = p.fee_pct / HUNDRED
    daily_eq: dict[int, float] = {}
    daily_px: dict[int, float] = {}
    minutes_total = minutes_in_market = 0
    bh_peak, bh_dd = float(first_px), 0.0

    for tick in ticks_from_klines(df):
        minute = tick.ts // 60_000 * 60_000
        if minute != cur_min:
            cur_min = minute
            i = fmap.get(minute)
            if i is not None:
                f = v3.Features(atr=_dec(cols["atr"][i]), atr_pct=_dec(cols["atr_pct"][i]),
                                ema_slope_pct=_dec(cols["ema_slope_pct"][i]),
                                anchor_high=_dec(cols["anchor_high"][i]),
                                ema_dist_pct=_dec(cols["ema_dist_pct"][i]))
                mv = _dec(cols["ret_1m_pct"][i])
                if mv is not None:
                    risk.on_price_move(minute, mv)
            eq = state.equity(last_px)
            daily_eq[minute // 86_400_000] = float(eq)
            daily_px[minute // 86_400_000] = float(last_px)
            minutes_total += 1
            minutes_in_market += any(b.phase is not v3.Phase.FLAT for b in state.bots)
            lp = float(last_px)
            bh_peak = max(bh_peak, lp)
            bh_dd = max(bh_dd, (bh_peak - lp) / bh_peak * 100)
            risk.on_equity(minute, eq)
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * HUNDRED)

        state, intents, blocked = strategy.step(state, tick, f, p)
        for _, why in blocked:
            gate_blocks[why] += 1
        for it in intents:
            eth_value = sum((b.eth * tick.price for b in state.bots), ZERO)
            open_risk = sum(((tick.price - b.stop_px) * b.eth for b in state.bots
                             if b.phase is v3.Phase.LONG and tick.price > b.stop_px), ZERO)
            ctx = RiskContext(ts=tick.ts, equity=state.equity(tick.price), eth_value=eth_value,
                              open_risk=open_risk)
            veto = risk.check(it, ctx)
            if veto:
                vetoes[veto.split(":")[0]] += 1
                strategy.on_reject(state, it.bot_id)
                continue
            px = it.price * (1 + slip) if it.side is Side.BUY else it.price * (1 - slip)
            fill = Fill(tick.ts, it.bot_id, it.side, it.qty, px, it.qty * px * fee)
            risk.on_order(tick.ts)
            b = state.bots[it.bot_id]
            if it.side is Side.BUY:
                strategy.on_fill(state, fill, atr=f.atr)
                entries[it.bot_id] = (tick.ts, px, b.entry_cost)
            else:
                reason = b.pending_reason
                strategy.on_fill(state, fill)
                ets, epx, cost = entries.pop(it.bot_id, (tick.ts, px, ZERO))
                pnl = fill.qty * px - fill.fee_quote - cost
                trades.append(Trade(it.bot_id, ets, tick.ts, epx, px, fill.qty, pnl))
                reasons[reason] += 1
                risk.on_trade_closed(tick.ts, pnl)
        last_px = tick.price

    end_eq = state.equity(last_px)
    eqs = np.array(list(daily_eq.values()))
    rets = np.diff(eqs) / eqs[:-1] if len(eqs) > 2 else np.array([])
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if len(rets) > 1 and rets.std() > 0 else 0.0
    pxs = np.array(list(daily_px.values()))
    bret = np.diff(pxs) / pxs[:-1] if len(pxs) > 2 else np.array([])
    bh_sharpe = float(bret.mean() / bret.std() * np.sqrt(365)) if len(bret) > 1 and bret.std() > 0 else 0.0
    years = max(len(eqs) / 365, 1e-9)
    cagr = (float(end_eq) / float(start_eq)) ** (1 / years) - 1 if start_eq > 0 and end_eq > 0 else -1.0
    return Report(
        start_ts=int(df["open_time"].iloc[0]), end_ts=int(df["open_time"].iloc[-1]),
        start_equity=start_eq, end_equity=end_eq, trades=trades, max_dd_pct=max_dd,
        open_positions=sum(1 for b in state.bots if b.phase is not v3.Phase.FLAT),
        buy_and_hold_pct=(last_px / first_px - 1) * 100,
        extra={
            "cagr_pct": f"{cagr * 100:.2f}",
            "sharpe": f"{sharpe:.2f}",
            "exposure_pct": f"{100 * minutes_in_market / max(minutes_total, 1):.1f}",
            "buy_and_hold_max_dd_pct": f"{bh_dd:.2f}",
            "buy_and_hold_sharpe": f"{bh_sharpe:.2f}",
            "exits": dict(reasons),
            "risk_vetoes": dict(vetoes),
            "gate_blocks": dict(gate_blocks),
            "kill_switch": risk.kill_reason or "—",
            "risk_events": len(risk.events),
        },
    )
