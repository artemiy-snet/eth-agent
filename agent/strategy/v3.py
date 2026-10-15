"""v3 — точки дії з покращеннями таблиці 3.4 Біблії і висновками бектесту legacy_v2.

Що змінено відносно v2.02 і навіщо:
  * Пороги в одиницях ATR (ознака з закритих свічок), а не у фіксованих %.
  * Рухома сітка: рівень бота i = anchor_high − i × interval_atr × ATR, де anchor_high — максимум
    за anchor_h годин. У v2.02 сітка стояла на місці і за рік «відірвалась» від ринку.
  * Входи рознесені: бот i зводиться лише після падіння на (|limit_bot| + i × interval_atr) ATR
    від максимуму. У v2.02 всі боти купували на одному відскоку.
  * Трейлінг за екстремумом (мін/макс з моменту зведення) — не залежить від частоти тіків.
  * Розмір позиції від ризику: qty = min(бюджет/ціна, капітал × ризик% / (вхід − стоп)).
  * Явний стоп (вхід − k_stop × ATR) і часовий стоп (max_hold_h).
  * Вихід у плюс лише вище вхід × (1 + 2×комісія + min_edge); після зведення — трейлінг,
    який не опускається нижче цього рівня.
  * Стан PENDING між наміром і виконанням: другий сигнал не створить другий ордер.

Чиста функція: step(state, tick, feat, params) -> (state, intents); виконання приходить через
on_fill / on_reject. Жодного вводу/виводу.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from agent.features.calendar import in_window
from agent.money import HUNDRED, ZERO, D, floor_to_step
from agent.strategy.types import Fill, Intent, Side, Tick


class Phase(str, Enum):
    FLAT = "FLAT"
    PENDING_BUY = "PENDING_BUY"
    LONG = "LONG"
    PENDING_SELL = "PENDING_SELL"


@dataclass(frozen=True)
class AtrPoint:
    """Рівні точки дії в одиницях ATR відносно бази B."""

    limit_up: Decimal
    limit_bot: Decimal
    action_up: Decimal
    action_bot: Decimal


@dataclass(frozen=True)
class V3Params:
    bots: int = 5
    interval_atr: Decimal = D("1.0")
    budget_usdt: Decimal = D("100")
    buy: AtrPoint = AtrPoint(D("1.0"), D("-1.0"), D("0.4"), D("0.1"))
    sell: AtrPoint = AtrPoint(D("1.0"), D("-1.0"), D("0.1"), D("-0.4"))
    min_edge_pct: Decimal = D("0.15")
    fee_pct: Decimal = D("0.1")
    k_stop_atr: Decimal = D("3")
    max_hold_h: int = 72
    risk_per_trade_pct: Decimal = D("0.5")
    qty_step: Decimal = D("0.0001")
    min_notional: Decimal = D("5")
    # гейти входу (розділ 4)
    trend_block_pct: Decimal | None = D("-3")   # заборона входів, коли EMA200(1h) за 24 год впала більше
    atr_min_pct: Decimal | None = D("0.3")
    atr_max_pct: Decimal | None = D("4.0")
    regime_min_pct: Decimal | None = None       # входи лише коли ціна ≥ EMA200d × (1 + x/100); None — вимкнено
    event_before_h: int | None = None           # не входити за N год до FOMC/CPI; None — вимкнено
    event_after_h: int | None = None            # і N год після


@dataclass(frozen=True)
class Features:
    """Ознаки, відомі на момент тіку (з закритих свічок)."""

    atr: Decimal | None
    atr_pct: Decimal | None = None
    ema_slope_pct: Decimal | None = None
    anchor_high: Decimal | None = None
    ema_dist_pct: Decimal | None = None      # режим: ціна відносно 200-денної EMA, %
    event_in_h: float | None = None          # годин до наступної макроподії (календар, agent/features/calendar.py)
    event_ago_h: float | None = None         # годин після останньої


@dataclass
class BotV3:
    bot_id: int
    usdt: Decimal
    eth: Decimal = ZERO
    phase: Phase = Phase.FLAT
    buy_armed: bool = False
    buy_min: Decimal = ZERO
    sell_armed: bool = False
    sell_max: Decimal = ZERO
    entry_px: Decimal = ZERO
    entry_ts: int = 0
    entry_cost: Decimal = ZERO        # USDT, включно з комісією
    stop_px: Decimal = ZERO
    atr_entry: Decimal = ZERO
    pending_stop: Decimal = ZERO
    pending_reason: str = ""


@dataclass
class V3State:
    bots: list[BotV3] = field(default_factory=list)

    def clone(self) -> "V3State":
        return copy.deepcopy(self)

    def equity(self, price: Decimal) -> Decimal:
        return sum((b.usdt + b.eth * price for b in self.bots), ZERO)


def init_state(p: V3Params) -> V3State:
    return V3State(bots=[BotV3(bot_id=i, usdt=p.budget_usdt) for i in range(p.bots)])


def entry_gate(f: Features, p: V3Params) -> str | None:
    """None — входи дозволено; інакше причина (йде в журнал як veto_reason)."""
    if f.atr is None or f.atr <= 0 or f.anchor_high is None:
        return "no_features"
    if p.trend_block_pct is not None and f.ema_slope_pct is not None and f.ema_slope_pct < p.trend_block_pct:
        return "trend_down"
    if p.atr_min_pct is not None and f.atr_pct is not None and f.atr_pct < p.atr_min_pct:
        return "atr_low"
    if p.atr_max_pct is not None and f.atr_pct is not None and f.atr_pct > p.atr_max_pct:
        return "atr_high"
    if p.regime_min_pct is not None and (f.ema_dist_pct is None or f.ema_dist_pct < p.regime_min_pct):
        return "regime_down"
    if in_window(f.event_in_h, f.event_ago_h, p.event_before_h, p.event_after_h):
        return "macro_event"
    return None


def position_qty(price: Decimal, stop: Decimal, budget: Decimal, capital: Decimal, p: V3Params) -> Decimal:
    if price <= stop:
        return ZERO
    by_budget = budget / (price * (1 + p.fee_pct / HUNDRED))
    by_risk = capital * p.risk_per_trade_pct / HUNDRED / (price - stop)
    qty = floor_to_step(min(by_budget, by_risk), p.qty_step)
    return qty if qty * price >= p.min_notional else ZERO


def min_profit_px(entry: Decimal, p: V3Params) -> Decimal:
    return entry * (1 + (2 * p.fee_pct + p.min_edge_pct) / HUNDRED)


def _flat(b: BotV3, tick: Tick, f: Features, p: V3Params, capital: Decimal, out: list[Intent],
          blocked: list[tuple[int, str]]) -> None:
    if f.atr is None or f.anchor_high is None:
        return
    px, atr = tick.price, f.atr
    level = f.anchor_high - D(b.bot_id) * p.interval_atr * atr
    if not b.buy_armed:
        if px <= level + p.buy.limit_bot * atr:
            b.buy_armed, b.buy_min = True, px
        return
    b.buy_min = min(b.buy_min, px)
    base = b.buy_min - p.buy.limit_bot * atr                 # limitBot «прилипає» до мінімуму
    if px >= base + p.buy.limit_up * atr:                    # відскок без нас
        b.buy_armed = False
        return
    if base + p.buy.action_bot * atr <= px <= base + p.buy.action_up * atr:
        why = entry_gate(f, p)
        if why:
            blocked.append((b.bot_id, why))
            return
        stop = px - p.k_stop_atr * atr
        qty = position_qty(px, stop, b.usdt, capital, p)
        if qty <= 0:
            blocked.append((b.bot_id, "size_zero"))
            return
        out.append(Intent(tick.ts, b.bot_id, Side.BUY, qty, px, "v3_bounce", stop=stop))
        b.phase, b.pending_stop, b.buy_armed = Phase.PENDING_BUY, stop, False


def _long(b: BotV3, tick: Tick, f: Features, p: V3Params, out: list[Intent]) -> None:
    px = tick.price
    atr = f.atr if f.atr else b.atr_entry
    reason = None
    if px <= b.stop_px:
        reason = "stop"
    elif tick.ts - b.entry_ts >= p.max_hold_h * 3_600_000:
        reason = "time"
    else:
        floor = min_profit_px(b.entry_px, p)
        if not b.sell_armed:
            if px >= max(b.entry_px + p.sell.limit_up * atr, floor):
                b.sell_armed, b.sell_max = True, px
        else:
            b.sell_max = max(b.sell_max, px)
            base = b.sell_max - p.sell.limit_up * atr       # limitUp «прилипає» до максимуму
            exit_px = max(base + p.sell.action_up * atr, floor)
            if px <= exit_px:
                reason = "take"
    if reason:
        qty = floor_to_step(b.eth, p.qty_step)
        if qty > 0:
            out.append(Intent(tick.ts, b.bot_id, Side.SELL, qty, px, f"v3_{reason}"))
            b.phase, b.pending_reason = Phase.PENDING_SELL, reason


def step(state: V3State, tick: Tick, feat: Features, p: V3Params) -> tuple[V3State, list[Intent], list]:
    """Повертає (стан, наміри, заблоковані_входи[(bot_id, причина)])."""
    out: list[Intent] = []
    blocked: list[tuple[int, str]] = []
    capital = state.equity(tick.price)
    for b in state.bots:
        if b.phase is Phase.FLAT:
            _flat(b, tick, feat, p, capital, out, blocked)
        elif b.phase is Phase.LONG:
            _long(b, tick, feat, p, out)
        # PENDING_*: чекаємо on_fill / on_reject — повторного наміру не буде
    return state, out, blocked


def on_fill(state: V3State, fill: Fill, atr: Decimal | None = None) -> None:
    b = state.bots[fill.bot_id]
    if fill.side is Side.BUY:
        cost = fill.qty * fill.price + fill.fee_quote
        b.usdt -= cost
        b.eth += fill.qty
        b.phase = Phase.LONG
        b.entry_px, b.entry_ts, b.entry_cost = fill.price, fill.ts, cost
        b.stop_px = b.pending_stop
        b.atr_entry = atr or ZERO
        b.sell_armed, b.sell_max = False, ZERO
    else:
        b.usdt += fill.qty * fill.price - fill.fee_quote
        b.eth -= fill.qty
        b.phase = Phase.FLAT if b.eth * fill.price < D(1) else Phase.LONG
        if b.phase is Phase.FLAT:
            b.buy_armed = False


def on_reject(state: V3State, bot_id: int) -> None:
    """Ордер відхилено біржею або вето ризику: повертаємо бота в попередню фазу."""
    b = state.bots[bot_id]
    if b.phase is Phase.PENDING_BUY:
        b.phase = Phase.FLAT
    elif b.phase is Phase.PENDING_SELL:
        b.phase = Phase.LONG
