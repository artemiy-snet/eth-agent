"""trend — слідування за трендом на денному таймфреймі (кандидат для збільшення прибутку).

Навіщо: точки дії (v3) купують відскоки й ≈ 90 % часу стоять у USDT, тому пропускають великі
тренди (утримання ETH за 2023-10…2026-09 дало +60 %). Трендова стратегія працює саме тоді.

Правила:
  * Вхід: ціна пробиває максимум за N днів (anchor_high при anchor_h = N×24) і ринок у висхідному
    режимі (ціна вище EMA200 денних закриттів на ≥ regime_min_pct).
  * Розмір: ризик% від поточного капіталу / (вхід − стоп), не більше бюджету — прибуток реінвестується.
  * Стоп: вхід − k_stop × ATR(1d); далі трейлінг «люстра»: максимум з моменту входу − k_trail × ATR.
    Стоп лише піднімається.
  * Вихід також, якщо режим зламався (ціна нижче EMA200d) і regime_exit = True.
  * Макроподії: останні event_bots ботів «чутливі» — за event_before_h год до FOMC/CPI вони продають
    і не входять до event_after_h год після події. Так частка рукава (напр. половина при bots=2)
    стоїть у USDT на час події, решта тримає позицію.

Використовує той самий стан і виконання, що й v3 (BotV3, on_fill, on_reject) — бектест, paper
і ризик-модуль працюють без змін.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from agent.features.calendar import in_window
from agent.money import D, floor_to_step
from agent.strategy import v3
from agent.strategy.types import Intent, Side, Tick
from agent.strategy.v3 import Features, Phase, V3State

on_fill = v3.on_fill
on_reject = v3.on_reject


@dataclass(frozen=True)
class TrendParams:
    bots: int = 1
    budget_usdt: Decimal = D("500")
    k_stop_atr: Decimal = D("3")
    k_trail_atr: Decimal = D("3")
    risk_per_trade_pct: Decimal = D("0.5")
    regime_min_pct: Decimal | None = D("0")
    regime_exit: bool = True
    fee_pct: Decimal = D("0.1")
    qty_step: Decimal = D("0.0001")
    min_notional: Decimal = D("5")
    alloc_pct: Decimal | None = None      # фіксована частка капіталу замість ризик-розміру (режимне утримання)
    breakout: bool = True                 # False — вхід лише за режимом, без пробою максимуму
    event_bots: int = 0                   # скільки останніх ботів виходять у USDT перед макроподією
    event_before_h: int | None = None
    event_after_h: int | None = None
    # сумісність з position_qty з v3
    min_edge_pct: Decimal = D("0")


def init_state(p: TrendParams) -> V3State:
    return V3State(bots=[v3.BotV3(bot_id=i, usdt=p.budget_usdt) for i in range(p.bots)])


def entry_gate(f: Features, p: TrendParams) -> str | None:
    if f.atr is None or f.atr <= 0 or (p.breakout and f.anchor_high is None):
        return "no_features"
    if p.regime_min_pct is not None and (f.ema_dist_pct is None or f.ema_dist_pct < p.regime_min_pct):
        return "regime_down"
    return None


def step(state: V3State, tick: Tick, f: Features, p: TrendParams) -> tuple[V3State, list[Intent], list]:
    out: list[Intent] = []
    blocked: list[tuple[int, str]] = []
    px = tick.price
    capital = state.equity(px)
    event_now = p.event_bots > 0 and in_window(f.event_in_h, f.event_ago_h, p.event_before_h, p.event_after_h)
    for b in state.bots:
        sensitive = event_now and b.bot_id >= p.bots - p.event_bots
        if b.phase is Phase.FLAT:
            if p.breakout and (f.anchor_high is None or px <= f.anchor_high):
                continue
            why = "macro_event" if sensitive else entry_gate(f, p)
            if why:
                blocked.append((b.bot_id, why))
                continue
            stop = px - p.k_stop_atr * f.atr if p.k_stop_atr > 0 else D(0)   # 0 — без ATR-стопу (вихід лише за режимом)
            if p.alloc_pct is not None:
                qty = floor_to_step(min(capital * p.alloc_pct / 100, b.usdt) / (px * (1 + p.fee_pct / 100)),
                                    p.qty_step)
                qty = qty if qty * px >= p.min_notional else D(0)
            else:
                qty = v3.position_qty(px, stop, b.usdt, capital, p)   # type: ignore[arg-type]
            if qty <= 0:
                blocked.append((b.bot_id, "size_zero"))
                continue
            out.append(Intent(tick.ts, b.bot_id, Side.BUY, qty, px, "trend_breakout", stop=stop))
            b.phase, b.pending_stop = Phase.PENDING_BUY, stop
        elif b.phase is Phase.LONG:
            b.sell_max = max(b.sell_max, px)
            atr = f.atr or b.atr_entry
            if atr and p.k_trail_atr > 0:
                b.stop_px = max(b.stop_px, b.sell_max - p.k_trail_atr * atr)
            reason = None
            if b.stop_px > 0 and px <= b.stop_px:
                reason = "trail"
            elif p.regime_exit and f.ema_dist_pct is not None and f.ema_dist_pct < 0:
                reason = "regime"
            elif sensitive:
                reason = "event"
            if reason:
                qty = floor_to_step(b.eth, p.qty_step)
                if qty > 0:
                    out.append(Intent(tick.ts, b.bot_id, Side.SELL, qty, px, f"trend_{reason}"))
                    b.phase, b.pending_reason = Phase.PENDING_SELL, reason
    return state, out, blocked
