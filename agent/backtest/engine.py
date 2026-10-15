"""Бектест-двигун (етап 5, перша версія — для legacy_v2).

Шлях ціни всередині 1m-свічки: O → (L → H, якщо свічка зростаюча, інакше H → L) → C.
Це наближення: v2.02 бачив ціну раз на секунду. Для точнішої моделі пізніше — 1s-свічки.

Звіт: PnL, кількість угод, win rate, profit factor, max DD, час у позиції, «застряглі» позиції.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Iterator

import pandas as pd

from agent.money import ZERO, D
from agent.strategy import legacy_v2 as lv2
from agent.strategy.types import Intent, Side, Tick


TICK_SECONDS = 15  # 4 точки на 1m-свічку


def ticks_from_klines(df: pd.DataFrame) -> Iterator[Tick]:
    for t, o, h, l, c in zip(df["open_time"], df["open"], df["high"], df["low"], df["close"]):
        o, h, l, c = D(o), D(h), D(l), D(c)
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        t = int(t)
        for k, px in enumerate(path):
            yield Tick(t + k * TICK_SECONDS * 1000, px)


@dataclass
class Trade:
    bot_id: int
    entry_ts: int
    exit_ts: int
    entry_px: Decimal
    exit_px: Decimal
    qty: Decimal
    pnl_usdt: Decimal


@dataclass
class Report:
    start_ts: int
    end_ts: int
    start_equity: Decimal
    end_equity: Decimal
    trades: list[Trade] = field(default_factory=list)
    max_dd_pct: Decimal = ZERO
    open_positions: int = 0
    buy_and_hold_pct: Decimal = ZERO
    extra: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)     # [(UTC-день, капітал)] — для портфельного аналізу

    @property
    def pnl_usdt(self) -> Decimal:
        return self.end_equity - self.start_equity

    @property
    def pnl_pct(self) -> Decimal:
        return self.pnl_usdt / self.start_equity * 100 if self.start_equity else ZERO

    @property
    def realized(self) -> Decimal:
        return sum((t.pnl_usdt for t in self.trades), ZERO)

    @property
    def profit_factor(self) -> Decimal | None:
        gains = sum((t.pnl_usdt for t in self.trades if t.pnl_usdt > 0), ZERO)
        losses = -sum((t.pnl_usdt for t in self.trades if t.pnl_usdt < 0), ZERO)
        if losses == 0:
            return None if gains == 0 else D("Infinity")
        return gains / losses

    @property
    def win_rate(self) -> Decimal:
        if not self.trades:
            return ZERO
        return D(sum(1 for t in self.trades if t.pnl_usdt > 0)) / D(len(self.trades)) * 100

    def summary(self) -> dict:
        pf = self.profit_factor
        hold_h = [(t.exit_ts - t.entry_ts) / 3.6e6 for t in self.trades]
        return {
            "period": f"{pd.to_datetime(self.start_ts, unit='ms', utc=True):%Y-%m-%d} … "
                      f"{pd.to_datetime(self.end_ts, unit='ms', utc=True):%Y-%m-%d}",
            "start_equity": f"{self.start_equity:.2f}",
            "end_equity": f"{self.end_equity:.2f}",
            "pnl_pct": f"{self.pnl_pct:.2f}",
            "realized_pnl_usdt": f"{self.realized:.2f}",
            "unrealized_pnl_usdt": f"{self.pnl_usdt - self.realized:.2f}",
            "buy_and_hold_pct": f"{self.buy_and_hold_pct:.2f}",
            "trades": len(self.trades),
            "win_rate_pct": f"{self.win_rate:.1f}",
            "profit_factor_closed": "n/a" if pf is None else f"{pf:.2f}",  # лише закриті угоди!
            "max_dd_pct": f"{self.max_dd_pct:.2f}",
            "avg_hold_h": f"{(sum(hold_h) / len(hold_h)) if hold_h else 0:.1f}",
            "max_hold_h": f"{max(hold_h) if hold_h else 0:.1f}",
            "open_positions_at_end": self.open_positions,
            **self.extra,
        }


def _equity(state: lv2.LegacyState, price: Decimal) -> Decimal:
    return sum((b.usdt + b.eth * price for b in state.bots), ZERO)


def run_legacy(df: pd.DataFrame, params: lv2.LegacyParams,
               gates: Iterable[tuple[int, bool, bool]] | None = None) -> tuple[Report, list[Intent]]:
    """gates: необов'язкові події (ts, can_buy, can_sell) — так фільтри v2.02 вмикали/вимикали масив."""
    state = lv2.init_state(params)
    gate_iter = iter(sorted(gates or []))
    next_gate = next(gate_iter, None)
    open_pos: dict[int, Intent] = {}
    trades: list[Trade] = []
    intents_all: list[Intent] = []

    first_px = D(df["open"].iloc[0])
    start_eq = _equity(state, first_px)
    peak, max_dd = start_eq, ZERO
    last_px = first_px
    last_minute = None

    for tick in ticks_from_klines(df):
        while next_gate is not None and next_gate[0] <= tick.ts:
            lv2.set_gates(state, params, next_gate[1], next_gate[2], tick.price)
            next_gate = next(gate_iter, None)
        state, intents = lv2.step(state, tick, params, seconds=TICK_SECONDS)
        for it in intents:
            intents_all.append(it)
            if it.side is Side.BUY:
                open_pos[it.bot_id] = it
            else:
                ent = open_pos.pop(it.bot_id, None)
                if ent is not None:
                    fee = params.fee_pct / 100
                    cost = ent.qty * ent.price
                    proceeds = it.qty * it.price * (1 - fee)
                    # ETH після комісії купівлі = qty×(1−fee); продано it.qty (пил лишається)
                    pnl = proceeds - cost * (it.qty / (ent.qty * (1 - fee)))
                    trades.append(Trade(it.bot_id, ent.ts, it.ts, ent.price, it.price, it.qty, pnl))
        last_px = tick.price
        minute = tick.ts // 60_000
        if minute != last_minute:   # equity і просадка — раз на хвилину
            last_minute = minute
            eq = _equity(state, last_px)
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100)

    end_eq = _equity(state, last_px)
    rep = Report(
        start_ts=int(df["open_time"].iloc[0]),
        end_ts=int(df["open_time"].iloc[-1]),
        start_equity=start_eq,
        end_equity=end_eq,
        trades=trades,
        max_dd_pct=max_dd,
        open_positions=sum(1 for b in state.bots if b.long),
        buy_and_hold_pct=(last_px / first_px - 1) * 100,
    )
    return rep, intents_all
