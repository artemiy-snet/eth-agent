"""ai — рукав «ШІ-трейдер»: виконує ПЕРЕВІРЕНІ торгові ідеї ШІ-аналітика (Біблія, розділ 7.6).

ШІ не торгує «з голови»: купівля дозволена лише тоді, коли в ринку ЗАРАЗ виконується умова однієї з
перевірених бектестом стратегій (SETUPS), а стоп рахується в ATR, як у них. ШІ лише обирає, чи варто
скористатися цією умовою з огляду на новини, позиції й контекст, і розмір у межах лімітів.

Ідея ШІ (BUY / SELL) доходить сюди лише після трьох перевірок в agent/analyst: схема і жорсткі правила
коду, незалежний ШІ-критик, і далі — ризик-модуль (вето), як для будь-якого наміру. Тут — останній рубіж:
  * сигнал одноразовий і має термін дії; ціна не відійшла від ціни рішення більше max_drift_pct;
  * BUY лише з FLAT, обов'язковий стоп у [min_stop_pct; max_stop_pct], розмір ≤ size_pct бюджету рукава
    і ≤ ризику risk_per_trade_pct капіталу рукава до стопу;
  * у позиції: стоп (і на біржі), необов'язковий тейк, часовий вихід max_hold_h, SELL від ШІ.

Використовує стан і виконання v3 (BotV3, on_fill, on_reject). Чиста функція, без вводу/виводу.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from agent.money import HUNDRED, D, floor_to_step
from agent.strategy import v3
from agent.strategy.types import Intent, Side, Tick
from agent.strategy.v3 import Features, Phase, V3State

on_reject = v3.on_reject


def on_fill(state: V3State, fill, atr=None) -> None:
    """Як v3, але зберігає ціль тейку (sell_max), яку v3.on_fill обнуляє при купівлі."""
    b = state.bots[fill.bot_id]
    take = b.sell_max
    v3.on_fill(state, fill, atr=atr)
    if fill.side is Side.BUY:
        b.sell_max = take


# Умови входу перевірених стратегій (docs/strategies.md) — та сама семантика, що в v3/trend.
SETUPS = {
    "dip_bounce": "відскок (A/B): ціна ≥ 1 ATR(1d) нижче максимуму 24 год, ATR(1d) у межах, без обвалу EMA200(1h)",
    "regime_trend": "режим (C): ціна вище 200-денної EMA",
}
SELL_REASONS = {
    "regime_break": "режим зламався: ціна нижче 200-денної EMA",
    "news_risk": "конкретний негативний факт у новинах/стані (продаж лише зменшує ризик)",
    "target": "ціль угоди досягнута за тезою входу",
}


def setup_holds(setup: str, px: Decimal, f: Features, p: "AiParams") -> str | None:
    """None — умова стратегії виконується зараз; інакше причина відмови."""
    if setup == "dip_bounce":
        if f.atr is None or f.anchor_high is None:
            return "немає ознак ATR/максимуму"
        if px > f.anchor_high - p.dip_atr * f.atr:
            return f"немає падіння ≥ {p.dip_atr} ATR від максимуму 24 год"
        if f.atr_pct is not None and f.atr_pct > p.dip_atr_max_pct:
            return f"ATR {f.atr_pct} % > {p.dip_atr_max_pct} % (як у рукаві B)"
        if f.ema_slope_pct is not None and f.ema_slope_pct < p.trend_block_pct:
            return "обвал EMA200(1h) — гейт trend_down"
        return None
    if setup == "regime_trend":
        if f.ema_dist_pct is None or f.ema_dist_pct < 0:
            return "ціна нижче 200-денної EMA — режим не висхідний"
        return None
    return f"невідома умова {setup!r}; дозволено {sorted(SETUPS)}"


def stop_bounds(f: Features, p: "AiParams") -> tuple[Decimal, Decimal]:
    """Стоп у % від ціни: [k_min; k_max] × ATR(1d), але в абсолютних межах min/max_stop_pct."""
    lo, hi = p.min_stop_pct, p.max_stop_pct
    if f.atr_pct:
        lo, hi = max(lo, p.k_stop_min * f.atr_pct), min(hi, p.k_stop_max * f.atr_pct)
    return lo, hi


@dataclass(frozen=True)
class AiParams:
    bots: int = 1
    budget_usdt: Decimal = D("50")
    risk_per_trade_pct: Decimal = D("3")     # % капіталу РУКАВА до стопу (рукав малий — 10 % портфеля)
    min_stop_pct: Decimal = D("1")
    max_stop_pct: Decimal = D("12")
    max_drift_pct: Decimal = D("1")
    max_hold_h: int = 168                    # як у рукаві B
    dip_atr: Decimal = D("1")
    dip_atr_max_pct: Decimal = D("15")
    trend_block_pct: Decimal = D("-3")
    k_stop_min: Decimal = D("1.5")           # стоп 1,5…3 ATR (v3: 2 ATR, trend: 3 ATR)
    k_stop_max: Decimal = D("3")
    fee_pct: Decimal = D("0.1")
    qty_step: Decimal = D("0.0001")
    min_notional: Decimal = D("5")
    min_edge_pct: Decimal = D("0")           # сумісність з v3.position_qty


def init_state(p: AiParams) -> V3State:
    return V3State(bots=[v3.BotV3(bot_id=0, usdt=p.budget_usdt)])


def entry_gate(f: Features, p: AiParams) -> str | None:
    return None if f.ai_signal and f.ai_signal.get("action") == "BUY" else "чекає ідею ШІ"


def check_signal(sig: dict, px: Decimal, f: Features, p: AiParams, now: int) -> str | None:
    """Жорсткі правила коду. Викликається і перед критиком (analyst), і в момент виконання (тут)."""
    if now > int(sig.get("expires", 0)):
        return "сигнал прострочено"
    ref = D(str(sig["price"]))
    if abs(px / ref - 1) * HUNDRED > p.max_drift_pct:
        return f"ціна відійшла від ціни рішення {ref} більше {p.max_drift_pct} %"
    if sig["action"] == "BUY":
        why = setup_holds(str(sig.get("setup")), px, f, p)
        if why:
            return f"умова стратегії не виконується: {why}"
        sp = D(str(sig["stop_pct"]))
        lo, hi = stop_bounds(f, p)
        if not lo <= sp <= hi:
            return f"стоп {sp} % поза [{lo:.2f}; {hi:.2f}] % (1,5–3 ATR)"
        if not 0 < D(str(sig["size_pct"])) <= 100:
            return "розмір поза (0; 100] %"
    elif sig["action"] == "SELL":
        if sig.get("reason") not in SELL_REASONS:
            return f"причина продажу поза {sorted(SELL_REASONS)}"
    else:
        return "дія лише BUY або SELL"
    return None


def step(state: V3State, tick: Tick, f: Features, p: AiParams) -> tuple[V3State, list[Intent], list]:
    out: list[Intent] = []
    blocked: list[tuple[int, str]] = []
    px = tick.price
    b = state.bots[0]
    sig = f.ai_signal
    if b.phase is Phase.FLAT:
        if sig and sig.get("action") == "BUY":
            why = check_signal(sig, px, f, p, tick.ts)
            if why:
                blocked.append((0, why))
                return state, out, blocked
            stop = px * (1 - D(str(sig["stop_pct"])) / HUNDRED)
            budget = b.usdt * D(str(sig["size_pct"])) / HUNDRED
            qty = v3.position_qty(px, stop, budget, state.equity(px), p)   # type: ignore[arg-type]
            if qty <= 0:
                blocked.append((0, "size_zero"))
                return state, out, blocked
            out.append(Intent(tick.ts, 0, Side.BUY, qty, px, f"ai_buy:{sig.get('id', '')}", stop=stop))
            b.phase, b.pending_stop = Phase.PENDING_BUY, stop
            take = sig.get("take_pct")
            b.sell_max = px * (1 + D(str(take)) / HUNDRED) if take else D(0)   # ціль тейку (0 — немає)
    elif b.phase is Phase.LONG:
        reason = None
        if b.stop_px > 0 and px <= b.stop_px:
            reason = "stop"
        elif b.sell_max > 0 and px >= b.sell_max:
            reason = "take"
        elif tick.ts - b.entry_ts >= p.max_hold_h * 3_600_000:
            reason = "time"
        elif sig and sig.get("action") == "SELL" and check_signal(sig, px, f, p, tick.ts) is None:
            reason = f"ai_sell_{sig['reason']}"
        if reason:
            qty = floor_to_step(b.eth, p.qty_step)
            if qty > 0:
                out.append(Intent(tick.ts, 0, Side.SELL, qty, px, f"ai_{reason}"))
                b.phase, b.pending_reason = Phase.PENDING_SELL, reason
    return state, out, blocked
