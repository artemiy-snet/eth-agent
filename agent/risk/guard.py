"""Запобіжники розділу 5 + ліміт сукупного відкритого ризику (висновок бектесту legacy_v2).

Принципи:
  * Вето діє лише на ВХОДИ. Виходи (стоп, тейк, часовий стоп) не блокуються ніколи —
    інакше запобіжник заважав би зменшувати ризик.
  * Kill switch не продає панічно: блокує нові входи назавжди до ручного рестарту,
    відкриті позиції лишаються під своїми стопами (STOP FLATTEN — окрема команда, етап 9).
  * Усе детерміновано: стан змінюється лише через on_* методи, перевірка — через check().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from agent.money import HUNDRED, ZERO, D
from agent.strategy.types import Intent, Side

HOUR = 3_600_000
DAY = 24 * HOUR


@dataclass(frozen=True)
class RiskParams:
    max_eth_share_pct: Decimal = D("60")
    daily_loss_pct: Decimal = D("2")
    max_dd_pct: Decimal = D("10")
    max_consecutive_losses: int = 5
    losses_pause_h: int = 4
    max_orders_per_hour: int = 20
    max_move_1m_pct: Decimal = D("3")
    move_pause_min: int = 15
    max_spread_pct: Decimal = D("0.1")
    max_open_risk_pct: Decimal = D("2")          # сума (вхід − стоп) × qty по всіх позиціях
    stale_after_s: int = 60
    max_balance_diff_pct: Decimal = D("0.5")


@dataclass(frozen=True)
class RiskContext:
    """Що ризик знає про світ на момент перевірки."""

    ts: int
    equity: Decimal
    eth_value: Decimal                 # вартість ETH у портфелі агента
    open_risk: Decimal                 # Σ (ціна − стоп) × qty відкритих позицій, USDT
    spread_pct: Decimal = ZERO
    data_age_s: int = 0
    exchange_connected: bool = True
    balance_diff_pct: Decimal = ZERO   # розбіжність агент ↔ біржа (reconcile)


@dataclass
class RiskGuard:
    p: RiskParams = field(default_factory=RiskParams)
    peak_equity: Decimal = ZERO
    day: int = -1
    day_start_equity: Decimal = ZERO
    consecutive_losses: int = 0
    order_ts: list[int] = field(default_factory=list)
    pause_until: int = 0
    pause_reason: str = ""
    killed: bool = False
    kill_reason: str = ""
    events: list[tuple[int, str]] = field(default_factory=list)

    # --- події ---------------------------------------------------------------------------
    def on_equity(self, ts: int, equity: Decimal) -> None:
        d = ts // DAY
        if d != self.day:
            self.day, self.day_start_equity = d, equity
        self.peak_equity = max(self.peak_equity, equity)
        if not self.killed and self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity * HUNDRED
            if dd >= self.p.max_dd_pct:
                self.kill(ts, f"max_dd {dd:.2f}%")

    def on_price_move(self, ts: int, move_1m_pct: Decimal) -> None:
        if abs(move_1m_pct) > self.p.max_move_1m_pct:
            self._pause(ts, self.p.move_pause_min * 60_000, f"move_1m {move_1m_pct:.2f}%")

    def on_order(self, ts: int) -> None:
        self.order_ts = [t for t in self.order_ts if t > ts - HOUR] + [ts]
        if len(self.order_ts) > self.p.max_orders_per_hour:
            self._pause(ts, HOUR, "orders_per_hour")

    def on_trade_closed(self, ts: int, pnl: Decimal) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if self.consecutive_losses >= self.p.max_consecutive_losses:
            self._pause(ts, self.p.losses_pause_h * HOUR, f"losses_in_row {self.consecutive_losses}")
            self.consecutive_losses = 0

    def kill(self, ts: int, reason: str) -> None:
        """Kill switch: автоматично, файлом STOP або командою Telegram (етап 9)."""
        self.killed, self.kill_reason = True, reason
        self.events.append((ts, f"KILL {reason}"))

    def manual_restart(self, ts: int) -> None:
        self.killed, self.kill_reason, self.pause_until = False, "", 0
        self.events.append((ts, "RESTART manual"))

    def _pause(self, ts: int, dur: int, reason: str) -> None:
        if ts + dur > self.pause_until:
            self.pause_until, self.pause_reason = ts + dur, reason
            self.events.append((ts, f"PAUSE {reason}"))

    # --- перевірка ------------------------------------------------------------------------
    def check(self, it: Intent, ctx: RiskContext) -> str | None:
        """None — дозволено; інакше причина вето."""
        if it.side is Side.SELL:
            return None
        if self.killed:
            return f"kill_switch: {self.kill_reason}"
        if ctx.balance_diff_pct > self.p.max_balance_diff_pct:
            self.kill(ctx.ts, f"balance_diff {ctx.balance_diff_pct}%")
            return "balance_diff"
        if ctx.ts < self.pause_until:
            return f"pause: {self.pause_reason}"
        if not ctx.exchange_connected:
            return "exchange_disconnected"
        if ctx.data_age_s > self.p.stale_after_s:
            return "stale_data"
        if ctx.spread_pct > self.p.max_spread_pct:
            return "spread"
        if self.day_start_equity > 0:
            day_pnl = (ctx.equity - self.day_start_equity) / self.day_start_equity * HUNDRED
            if day_pnl <= -self.p.daily_loss_pct:
                self._pause(ctx.ts, (ctx.ts // DAY + 1) * DAY - ctx.ts, f"daily_loss {day_pnl:.2f}%")
                return "daily_loss"
        notional = it.qty * it.price
        if ctx.equity > 0 and (ctx.eth_value + notional) / ctx.equity * HUNDRED > self.p.max_eth_share_pct:
            return "max_eth_share"
        if it.stop is not None and ctx.equity > 0:
            new_risk = (it.price - it.stop) * it.qty
            if (ctx.open_risk + new_risk) / ctx.equity * HUNDRED > self.p.max_open_risk_pct:
                return "max_open_risk"
        return None
