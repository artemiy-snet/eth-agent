"""Цикл живого агента: ринок → ознаки → стратегія v3 → вето ризику → виконання → журнал.

Режими: paper (живі ціни, віртуальні угоди) і testnet (справжні ордери на тестових грошах).
live (реальні гроші) заблоковано до проходження етапів 1–2 Біблії.

Запуск:  eth-agent live --mode paper   |   eth-agent live --mode testnet
Зупинка: файл STOP у робочій теці (або кнопка на сторінці статусу) → kill switch;
         файл STOP_FLATTEN → kill switch + продаж усіх позицій.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from agent.data.exchange_info import ETHUSDT_DEFAULT, SymbolFilters
from agent.live import features_live
from agent.live.binance import BASES, BinanceError, Client, NetworkError
from agent.live.broker import ExchangeBroker, ExecResult, PaperBroker
from agent.live.journal import Journal, load_state, save_state
from agent.live.notify import Notifier
from agent.live.settings import LiveSettings
from agent.money import HUNDRED, ZERO, D
from agent.risk.guard import RiskContext, RiskGuard
from agent.strategy import v3
from agent.strategy.types import Fill, Intent, Side, Tick

STALE_FEATURES_S = 600


class LiveDisabled(Exception):
    pass


@dataclass
class Market:
    bid: Decimal = ZERO
    ask: Decimal = ZERO
    price_ts: int = 0
    connected: bool = False
    feat: v3.Features = field(default_factory=lambda: v3.Features(atr=None))
    feat_ts: int = 0
    last_minute: int = 0


class Agent:
    """Один рукав: одна стратегія на своїй частці капіталу."""

    def __init__(self, s: LiveSettings, client: Client, broker, home: Path, journal: Journal,
                 notifier: Notifier, filters: SymbolFilters, strategy=v3, params=None, risk=None,
                 features=None, name: str = ""):
        if s.mode not in ("paper", "testnet"):
            raise LiveDisabled("Режим live вимкнено: спершу testnet і paper (Біблія, розділ 9).")
        self.s, self.c, self.broker, self.home, self.j, self.n, self.f = s, client, broker, home, journal, notifier, filters
        self.strat, self.name = strategy, name
        self.fp = features or s.features
        self.p = dataclasses.replace(params or s.v3, qty_step=filters.step_size, min_notional=filters.min_notional)
        self.risk = RiskGuard(risk or s.risk)
        self.m = Market()
        self.state_path = home / f"state_{s.mode}{'_' + name if name else ''}.json"
        loaded = load_state(self.state_path)
        if loaded:
            self.state, self.extra = loaded
            self.j.event("INFO", "start", "стан відновлено з файла")
        else:
            self.state, self.extra = self.strat.init_state(self.p), {}
        self.extra.setdefault("stops", {})        # bot_id → coid стоп-ордера на біржі
        self.extra.setdefault("entries", {})      # bot_id → [ts, ціна, вартість]
        self.extra.setdefault("pending", {})      # bot_id → намір у стані UNKNOWN
        if self.extra.get("killed"):
            self.risk.kill(0, self.extra.get("kill_reason", "відновлено зі стану"))
        self.risk.peak_equity = D(self.extra.get("peak_equity", "0"))
        self.next_features = 0
        self.next_reconcile = 0
        self.next_stop_poll = 0
        self.balance_diff = ZERO
        self.last_status: dict = {}
        self.analyst = None          # ШІ-аналітик, див. build()
        self.ai_mult = D(1)          # множник розміру нових входів від ШІ (≤ 1; лише зменшує ризик)

    # --- дані ----------------------------------------------------------------------------
    def now(self) -> int:
        return self.c.server_now_ms()

    def refresh_market(self, now: int) -> None:
        try:
            self.m.bid, self.m.ask = self.c.book_ticker(self.s.pair)
            self.m.price_ts, self.m.connected = now, True
        except (NetworkError, BinanceError) as e:
            if self.m.connected:
                self.j.event("WARN", "market", f"немає ціни: {e}")
            self.m.connected = False

    def refresh_features(self, now: int) -> None:
        fp = self.fp
        try:
            k_atr = self.c.klines(self.s.pair, fp.atr_tf, limit=200)
            k_1h = self.c.klines(self.s.pair, "1h", limit=1000)
            k_1d = self.c.klines(self.s.pair, "1d", limit=1000)
            need = fp.anchor_h * 60 + 1
            k_1m = self.c.klines(self.s.pair, "1m", limit=min(need, 1000))
            while len(k_1m) < need:
                older = self.c.klines(self.s.pair, "1m", limit=min(need - len(k_1m), 1000), end_time=k_1m[0][0] - 1)
                if not older:
                    break
                k_1m = older + k_1m
        except (NetworkError, BinanceError) as e:
            self.j.event("WARN", "features", f"ознаки не оновлено: {e}")
            return
        feat, ret = features_live.compute(k_atr, k_1h, k_1m, now, fp, k_1d=k_1d)
        self.m.feat, self.m.feat_ts = feat, now
        minute = now // 60_000
        if minute != self.m.last_minute and ret == ret:          # ret == ret → не NaN
            self.m.last_minute = minute
            self.risk.on_price_move(now, D(f"{ret:.6f}"))

    # --- облік ---------------------------------------------------------------------------
    def equity(self, px: Decimal) -> Decimal:
        return self.state.equity(px)

    def open_risk(self, px: Decimal) -> Decimal:
        return sum(((px - b.stop_px) * b.eth for b in self.state.bots
                    if b.phase is v3.Phase.LONG and px > b.stop_px), ZERO)

    def context(self, now: int, px: Decimal) -> RiskContext:
        spread = (self.m.ask - self.m.bid) / px * HUNDRED if px > 0 else ZERO
        age = (now - self.m.price_ts) // 1000 if self.m.price_ts else 10**6
        f_age = (now - self.m.feat_ts) // 1000 if self.m.feat_ts else 10**6
        if f_age > STALE_FEATURES_S:
            age = max(age, f_age)
        return RiskContext(ts=now, equity=self.equity(px), eth_value=sum((b.eth * px for b in self.state.bots), ZERO),
                           open_risk=self.open_risk(px), spread_pct=spread, data_age_s=int(age),
                           exchange_connected=self.m.connected, balance_diff_pct=self.balance_diff)

    # --- застосування виконань ------------------------------------------------------------------
    def apply_fill(self, fill: Fill, reason: str = "") -> None:
        b = self.state.bots[fill.bot_id]
        key = str(fill.bot_id)
        if fill.side is Side.BUY:
            v3.on_fill(self.state, fill, atr=self.m.feat.atr)
            self.extra["entries"][key] = [fill.ts, fill.price, b.entry_cost]
            msg = f"{self.tag()}КУПІВЛЯ бот {fill.bot_id}: {fill.qty} ETH по {fill.price:.2f}, стоп {b.stop_px:.2f}"
            self.j.event("INFO", "fill", msg)
            self.n.send(f"🟢 [{self.s.mode}] {msg}")
            coid = self.broker.place_stop(fill.bot_id, b.eth, b.stop_px, fill.ts) if b.stop_px > 0 else None
            if coid:
                self.extra["stops"][key] = coid
            elif self.broker.name != "paper" and b.stop_px > 0:
                self.j.event("WARN", "stop", f"стоп на біржі не поставлено для бота {fill.bot_id}; стоп веде агент")
        else:
            reason = reason or b.pending_reason or "?"
            v3.on_fill(self.state, fill)
            ets, epx, cost = self.extra["entries"].pop(key, [fill.ts, fill.price, ZERO])
            pnl = fill.qty * fill.price - fill.fee_quote - D(cost)
            self.j.trade(fill.bot_id, int(ets), fill.ts, epx, fill.price, fill.qty, pnl, f"{self.tag()}{reason}".strip())
            self.risk.on_trade_closed(fill.ts, pnl)
            self.extra["stops"].pop(key, None)
            msg = f"{self.tag()}ПРОДАЖ бот {fill.bot_id} ({reason}): {fill.qty} ETH по {fill.price:.2f}, PnL {pnl:+.2f} USDT"
            self.j.event("INFO", "fill", msg)
            self.n.send(f"{'✅' if pnl >= 0 else '🔻'} [{self.s.mode}] {msg}")

    def tag(self) -> str:
        return f"[{self.name}] " if self.name else ""

    def handle_result(self, it: Intent, r: ExecResult) -> None:
        self.j.order(it.ts, r.coid, it.bot_id, it.side.value, r.status,
                     r.fill.qty if r.fill else it.qty, r.fill.price if r.fill else it.price,
                     r.fill.fee_quote if r.fill else None, r.detail)
        key = str(it.bot_id)
        if r.status == "FILLED" and r.fill:
            self.extra["pending"].pop(key, None)
            self.apply_fill(r.fill)
        elif r.status == "UNKNOWN":
            self.extra["pending"][key] = {"coid": r.coid, "side": it.side.value, "qty": it.qty, "price": it.price,
                                          "ts": it.ts, "stop": it.stop}
            self.j.event("WARN", "order", f"ордер {r.coid} у стані UNKNOWN — з'ясовую статус")
        else:
            self.extra["pending"].pop(key, None)
            v3.on_reject(self.state, it.bot_id)
            self.j.event("WARN", "order", f"ордер відхилено: {r.detail}")

    def resolve_pending(self, now: int) -> None:
        for key, p in list(self.extra["pending"].items()):
            it = Intent(int(p["ts"]), int(key), Side(p["side"]), D(p["qty"]), D(p["price"]), "resolve",
                        stop=D(p["stop"]) if p.get("stop") is not None else None)
            r = self.broker.resolve(p["coid"], it, now)
            if r.status != "UNKNOWN":
                self.handle_result(it, r)

    def poll_stops(self, now: int) -> None:
        for key, coid in list(self.extra["stops"].items()):
            fill = self.broker.poll_stop(coid, int(key), now)
            if fill:
                b = self.state.bots[int(key)]
                b.phase = v3.Phase.PENDING_SELL
                self.apply_fill(fill, reason="stop_exchange")

    def execute(self, it: Intent, now: int) -> None:
        key = str(it.bot_id)
        if it.side is Side.SELL and key in self.extra["stops"]:
            st, fill = self.broker.cancel_stop(self.extra["stops"][key], it.bot_id, now)
            if st == "FILLED" and fill:
                self.apply_fill(fill, reason="stop_exchange")
                return
            if st == "ERROR":
                v3.on_reject(self.state, it.bot_id)
                self.j.event("WARN", "stop", f"не вдалось зняти стоп бота {it.bot_id}; повтор на наступному тіку")
                return
            self.extra["stops"].pop(key, None)
        self.handle_result(it, self.broker.execute(it, self.m.bid, self.m.ask, now))

    # --- звірка й запобіжники ------------------------------------------------------------------
    def reconcile(self) -> None:
        bal = self.broker.balances()
        if bal is None:
            return
        need_eth = sum((b.eth for b in self.state.bots), ZERO)
        need_usdt = sum((b.usdt for b in self.state.bots), ZERO)
        short = []
        if need_eth > 0 and bal.get("ETH", ZERO) < need_eth * D("0.995"):
            short.append((need_eth - bal.get("ETH", ZERO)) / need_eth * HUNDRED)
        if need_usdt > 0 and bal.get("USDT", ZERO) < need_usdt * D("0.995"):
            short.append((need_usdt - bal.get("USDT", ZERO)) / need_usdt * HUNDRED)
        self.balance_diff = max(short) if short else ZERO
        if short:
            self.j.event("ERROR", "reconcile", f"на біржі менше, ніж думає агент: {self.balance_diff:.2f}%",
                         exchange=bal, agent_eth=need_eth, agent_usdt=need_usdt)

    def check_stop_files(self, now: int) -> list[Intent]:
        out = []
        if (self.home / "STOP_FLATTEN").exists() and not self.extra.get("flattened"):
            self.kill(now, "STOP_FLATTEN")
            self.extra["flattened"] = True
            for b in self.state.bots:
                if b.phase is v3.Phase.LONG and b.eth > 0:
                    out.append(Intent(now, b.bot_id, Side.SELL, b.eth, self.m.bid, "v3_flatten"))
                    b.phase, b.pending_reason = v3.Phase.PENDING_SELL, "flatten"
        elif (self.home / "STOP").exists() and not self.risk.killed:
            self.kill(now, "файл STOP")
        return out

    def clear_kill(self) -> None:
        """Ручний рестарт після kill switch (Біблія: тільки ручна команда)."""
        for name in ("STOP", "STOP_FLATTEN"):
            (self.home / name).unlink(missing_ok=True)
        self.risk.manual_restart(self.now())
        self.extra.update(killed=False, kill_reason="", flattened=False)
        save_state(self.state_path, self.state, self.extra)
        self.j.event("INFO", "restart", "kill switch знято вручну")

    def kill(self, now: int, reason: str) -> None:
        if not self.risk.killed:
            self.risk.kill(now, reason)
            self.j.event("ERROR", "kill", f"KILL SWITCH: {reason}")
            self.n.send(f"⛔ [{self.s.mode}] KILL SWITCH: {reason}. Нові входи заблоковано, стопи лишаються.")

    # --- один крок -----------------------------------------------------------------------
    def tick(self) -> None:
        now = self.now()
        self.refresh_market(now)
        if now >= self.next_features:
            self.refresh_features(now)
            self.next_features = now + self.s.features_every_s * 1000
        if self.extra["pending"]:
            self.resolve_pending(now)
        if now >= self.next_stop_poll and self.extra["stops"]:
            self.poll_stops(now)
            self.next_stop_poll = now + 30_000
        if now >= self.next_reconcile and self.broker.name != "paper":
            self.reconcile()
            self.next_reconcile = now + self.s.reconcile_every_s * 1000
        intents = self.check_stop_files(now)

        if self.m.connected and self.m.bid > 0:
            px = (self.m.bid + self.m.ask) / 2
            was_killed = self.risk.killed
            self.risk.on_equity(now, self.equity(px))
            if self.risk.killed and not was_killed:
                self.kill(now, self.risk.kill_reason)
            self.state, new, blocked = self.strat.step(self.state, Tick(now, px), self.m.feat, self.p)
            intents += new
            for it in intents:
                if it.side is Side.BUY and self.ai_mult < 1:
                    it = self.apply_ai(it, px)
                    if it is None:
                        continue
                veto = self.risk.check(it, self.context(now, px))
                if veto:
                    v3.on_reject(self.state, it.bot_id)
                    self.j.event("INFO", "veto", f"вето входу бота {it.bot_id}: {veto}")
                    continue
                self.risk.on_order(now)
                self.execute(it, now)
            st = self.status(now, px)
            (self.home / f"status_{self.s.mode}{'_' + self.name if self.name else ''}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))
        self.extra["killed"] = self.risk.killed
        self.extra["kill_reason"] = self.risk.kill_reason
        self.extra["peak_equity"] = self.risk.peak_equity
        save_state(self.state_path, self.state, self.extra)

    def apply_ai(self, it: Intent, px: Decimal) -> Intent | None:
        """Правило асиметрії: ШІ може лише зменшити або заборонити НОВИЙ вхід."""
        from agent.money import floor_to_step
        qty = floor_to_step(it.qty * self.ai_mult, self.f.step_size)
        if qty <= 0 or qty * px < self.f.min_notional:
            v3.on_reject(self.state, it.bot_id)
            self.j.event("INFO", "veto", f"вхід бота {it.bot_id}{' (' + self.name + ')' if self.name else ''} "
                                         f"заблоковано ШІ (множник {self.ai_mult})")
            return None
        self.j.event("INFO", "ai", f"вхід бота {it.bot_id} зменшено ШІ: {it.qty} → {qty} ETH")
        return dataclasses.replace(it, qty=qty, quote_qty=None if it.quote_qty is None else it.quote_qty * self.ai_mult)

    def status(self, now: int, px: Decimal) -> dict:
        f = self.m.feat
        self.last_status = {
            "mode": self.s.mode, "pair": self.s.pair, "sleeve": self.name or "v3", "ts": now, "price": f"{px:.2f}",
            "bid": str(self.m.bid), "ask": str(self.m.ask), "connected": self.m.connected,
            "equity": f"{self.equity(px):.2f}",
            "start_equity": f"{self.p.budget_usdt * self.p.bots:.2f}", "ema_dist_pct": str(f.ema_dist_pct),
            "killed": self.risk.killed, "kill_reason": self.risk.kill_reason,
            "pause_until": self.risk.pause_until, "pause_reason": self.risk.pause_reason,
            "atr": str(f.atr), "atr_pct": str(f.atr_pct), "ema_slope_pct": str(f.ema_slope_pct),
            "anchor_high": str(f.anchor_high), "entry_gate": self.strat.entry_gate(f, self.p) or "відкрито",
            "bots": [{"id": b.bot_id, "phase": b.phase.value, "usdt": f"{b.usdt:.2f}", "eth": f"{b.eth:.5f}",
                      "entry": f"{b.entry_px:.2f}" if b.entry_px else "", "stop": f"{b.stop_px:.2f}" if b.stop_px else "",
                      "armed": b.buy_armed or b.sell_armed,
                      "level": f"{(f.anchor_high - D(b.bot_id) * self.p.interval_atr * f.atr + self.p.buy.limit_bot * f.atr):.2f}"
                      if f.atr and f.anchor_high and hasattr(self.p, "interval_atr") else ""}
                     for b in self.state.bots],
        }
        return self.last_status

    def run(self, stop_after_s: int | None = None) -> None:
        t_end = time.time() + stop_after_s if stop_after_s else None
        self.j.event("INFO", "start", f"агент запущено, режим {self.s.mode}")
        self.n.send(f"▶️ ETH-Агент запущено, режим {self.s.mode}")
        start_notes(self.j, self.analyst)
        while t_end is None or time.time() < t_end:
            t0 = time.time()
            try:
                ai_step(self, [self], self.now())
                self.tick()
            except Exception as e:                       # noqa: BLE001 — нічого не ковтаємо мовчки
                self.j.event("ERROR", "crash", f"{type(e).__name__}: {e}")
                self.n.send(f"⚠️ [{self.s.mode}] помилка циклу: {type(e).__name__}: {e}")
            time.sleep(max(1.0, self.s.poll_s - (time.time() - t0)))


def market_for_analyst(statuses: list[dict]) -> dict:
    """Стислий ринковий пакет для ШІ-аналітика зі статусів рукавів (без ключів і балансів біржі)."""
    s0 = next((x for x in statuses if x), {})
    if not s0:
        return {}
    return {"price": s0.get("price"), "ema_dist_pct_vs_ema200d": s0.get("ema_dist_pct"),
            "atr_1d_pct": s0.get("atr_pct"), "ema200_1h_slope_24h_pct": s0.get("ema_slope_pct"),
            "high_24h": s0.get("anchor_high"),
            "sleeves": [{"name": x.get("sleeve"), "entry_gate": x.get("entry_gate"),
                         "in_eth": any(b.get("phase") != "FLAT" for b in x.get("bots", []))} for x in statuses if x]}


def book_for_analyst(agents: list, journal) -> dict:
    """Позиції, стопи, результат і стан ризику — для «контролю ставок»."""
    sleeves = []
    for a in agents:
        st = a.last_status or {}
        sleeves.append({
            "name": a.name or "v3", "equity": st.get("equity"), "start_equity": st.get("start_equity"),
            "killed": st.get("killed"), "kill_reason": st.get("kill_reason"), "pause_reason": st.get("pause_reason"),
            "entry_gate": st.get("entry_gate"),
            "positions": [{"bot": b["id"], "phase": b["phase"], "eth": b["eth"], "usdt": b["usdt"], "entry": b["entry"],
                           "stop": b["stop"]} for b in st.get("bots", []) if b.get("phase") != "FLAT"],
            "flat_bots": sum(1 for b in st.get("bots", []) if b.get("phase") == "FLAT"),
            "pending_orders": len(a.extra.get("pending", {}) or {}), "exchange_stops": len(a.extra.get("stops", {}) or {}),
            "balance_diff_pct": str(getattr(a, "balance_diff", "0")),
        })
    trades = [{"bot": t[0], "entry_px": t[3], "exit_px": t[4], "pnl": t[6], "reason": t[7]}
              for t in journal.recent_trades(10)]
    warn = [m for _, lvl, _, m in journal.recent_events(40) if lvl in ("WARN", "ERROR")][:8]
    return {"mode": agents[0].s.mode, "sleeves": sleeves, "recent_trades": trades, "recent_warnings": warn}


def ai_step(owner, agents: list, now: int) -> None:
    """Раз на цикл: передати множник ШІ рукавам і, за потреби, запустити оцінку."""
    an = owner.analyst
    if an is None:
        return
    mult = an.size_mult(now)
    for a in agents:
        a.ai_mult = mult
    killed = any(a.risk.killed for a in agents)
    force = "kill switch" if killed and not getattr(owner, "_ai_saw_kill", False) else ""
    owner._ai_saw_kill = killed
    an.maybe_run(now, market_for_analyst([a.last_status for a in agents]), book_for_analyst(agents, owner.j), force)


def start_notes(j, analyst) -> None:
    from agent.features import calendar
    left = calendar.days_left(int(time.time() * 1000))
    if left < 45:
        j.event("WARN", "calendar", f"календар макроподій закінчується через {left:.0f} дн — онови agent/features/calendar.py")
    if analyst is None:
        j.event("INFO", "ai_news", "ШІ-аналітик вимкнено (немає ANTHROPIC_API_KEY у secrets.env або analyst.enabled: false)")
    else:
        how = ("автоматично гальмує нові входи (CAUTION ½, RISK_OFF 0), ордерів не створює" if analyst.mode == "auto"
               else "тіньовий режим, лише журнал")
        j.event("INFO", "ai_news", f"ШІ-аналітик: {how}; модель {analyst.model}, раз на {analyst.every_h:g} год "
                                   f"+ позачергово при русі ≥ {analyst.trigger_move_pct:g} % за 4 год або kill switch")


class Portfolio:
    """Кілька рукавів в одному процесі: спільні STOP-файли, Telegram і звірка балансів на біржі."""

    def __init__(self, agents: list[Agent]):
        self.agents = agents
        a0 = agents[0]
        self.s, self.home, self.j, self.n, self.broker = a0.s, a0.home, a0.j, a0.n, a0.broker
        self.next_reconcile = 0
        self.analyst = None
        for a in agents:
            a.next_reconcile = 10**18      # звірка спільна — див. reconcile()

    @property
    def last_status(self) -> dict:
        return {"portfolio": [a.last_status for a in self.agents]}

    def reconcile(self) -> None:
        bal = self.broker.balances()
        if bal is None:
            return
        need_eth = sum((b.eth for a in self.agents for b in a.state.bots), ZERO)
        need_usdt = sum((b.usdt for a in self.agents for b in a.state.bots), ZERO)
        short = []
        if need_eth > 0 and bal.get("ETH", ZERO) < need_eth * D("0.995"):
            short.append((need_eth - bal.get("ETH", ZERO)) / need_eth * HUNDRED)
        if need_usdt > 0 and bal.get("USDT", ZERO) < need_usdt * D("0.995"):
            short.append((need_usdt - bal.get("USDT", ZERO)) / need_usdt * HUNDRED)
        diff = max(short) if short else ZERO
        for a in self.agents:
            a.balance_diff = diff
        if short:
            self.j.event("ERROR", "reconcile", f"на біржі менше, ніж думає портфель: {diff:.2f}%")

    def tick(self) -> None:
        now = self.agents[0].now()
        if now >= self.next_reconcile and self.broker.name != "paper":
            self.reconcile()
            self.next_reconcile = now + self.s.reconcile_every_s * 1000
        for a in self.agents:
            a.tick()

    def clear_kill(self) -> None:
        for a in self.agents:
            a.clear_kill()

    def run(self, stop_after_s: int | None = None) -> None:
        t_end = time.time() + stop_after_s if stop_after_s else None
        names = ", ".join(a.name for a in self.agents)
        self.j.event("INFO", "start", f"портфель запущено ({names}), режим {self.s.mode}")
        self.n.send(f"▶️ ETH-Агент: портфель {names}, режим {self.s.mode}")
        start_notes(self.j, self.analyst)
        while t_end is None or time.time() < t_end:
            t0 = time.time()
            try:
                ai_step(self, self.agents, self.agents[0].now())
                self.tick()
            except Exception as e:                       # noqa: BLE001
                self.j.event("ERROR", "crash", f"{type(e).__name__}: {e}")
                self.n.send(f"⚠️ [{self.s.mode}] помилка циклу: {type(e).__name__}: {e}")
            time.sleep(max(1.0, self.s.poll_s - (time.time() - t0)))


def read_secrets(home: Path) -> dict[str, str]:
    p = home / "secrets.env"
    out = {}
    if p.exists():
        if os.name == "posix" and p.stat().st_mode & 0o077:
            os.chmod(p, 0o600)
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def build(settings: LiveSettings, home: Path) -> "Agent | Portfolio":
    home.mkdir(parents=True, exist_ok=True)
    sec = read_secrets(home)
    if settings.mode not in ("paper", "testnet"):
        raise LiveDisabled("Режим live вимкнено: спершу testnet і paper (Біблія, розділ 9).")
    if settings.mode == "testnet" and not (sec.get("BINANCE_API_KEY") and sec.get("BINANCE_API_SECRET")):
        raise SystemExit(f"Для testnet потрібні BINANCE_API_KEY і BINANCE_API_SECRET у {home / 'secrets.env'}")
    client = Client(BASES[settings.mode], sec.get("BINANCE_API_KEY", ""), sec.get("BINANCE_API_SECRET", ""))
    client.sync_time()
    try:
        filters = SymbolFilters.from_exchange_info(client.exchange_info(settings.pair), settings.pair)
    except (NetworkError, BinanceError):
        filters = ETHUSDT_DEFAULT
    journal = Journal(home / f"journal_{settings.mode}.sqlite")
    notifier = Notifier(sec.get("TELEGRAM_TOKEN"), sec.get("TELEGRAM_CHAT_ID"))
    if settings.mode == "paper":
        broker = PaperBroker(settings.v3.fee_pct, filters)
    else:
        broker = ExchangeBroker(client, settings.pair, filters, settings.mode, settings.stop_limit_buffer_pct)
    from agent.analyst import shadow
    if not settings.sleeves:
        obj = Agent(settings, client, broker, home, journal, notifier, filters)
    else:
        from agent.strategy import trend
        agents = [Agent(settings, client, broker, home, journal, notifier, filters,
                        strategy=trend if sl.strategy == "trend" else v3, params=sl.params, risk=sl.risk,
                        features=sl.features, name=sl.name) for sl in settings.sleeves]
        obj = Portfolio(agents)
    obj.analyst = shadow.from_settings(settings.analyst, sec, journal, home, notifier)
    return obj
