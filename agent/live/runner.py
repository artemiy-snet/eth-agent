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
from agent.live.broker import ExchangeBroker, ExecResult, PaperBroker, base_asset
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
    candles: dict = field(default_factory=dict)     # tf → [(t, o, h, l, c)] для графіків панелі


class Agent:
    """Один рукав: одна стратегія на своїй частці капіталу."""

    def __init__(self, s: LiveSettings, client: Client, broker, home: Path, journal: Journal,
                 notifier: Notifier, filters: SymbolFilters, strategy=v3, params=None, risk=None,
                 features=None, name: str = "", pair: str = ""):
        if s.mode not in ("paper", "testnet") and not (s.mode == "live" and s.live_confirmed):
            raise LiveDisabled("Режим live не підтверджено: панель → Налаштування → Режим live + підтвердження.")
        self.s, self.c, self.broker, self.home, self.j, self.n, self.f = s, client, broker, home, journal, notifier, filters
        self.strat, self.name = strategy, name
        self.pair = pair or s.pair
        self.base = base_asset(self.pair)
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
        self.rebase_budget()
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
        self.ai_signal = None        # перевірена ідея ШІ для рукава ai — діє один крок
        self.order_backoff_until = 0  # пауза після відмови біржі
        self.extra.setdefault("ai_pnls", [])

    def rebase_budget(self) -> None:
        """Змінили бюджет у панелі (межа капіталу, склад) — боти без позиції переходять на новий бюджет.
        Інакше збережений стан тримає стару суму, і звірка з біржею бачить «нестачу» → kill switch."""
        new = D(self.p.budget_usdt)
        legacy = "budget" not in self.extra               # стан зі старих версій — бюджет не збережено
        old = D(self.extra.get("budget", "0"))
        self.extra["budget"] = str(new)
        if not legacy and old == new:
            return
        moved = 0
        for b in self.state.bots:
            if b.phase is v3.Phase.FLAT and b.eth * (b.entry_px or D(1)) < D(1) and b.usdt != new:
                old = b.usdt if legacy else old
                b.usdt = new
                moved += 1
        if moved:
            self.extra["peak_equity"] = "0"                 # нова база — просадка рахується від неї
            why = str(self.extra.get("kill_reason", ""))
            if self.extra.get("killed") and ("balance_diff" in why or "біржі" in why):
                self.extra.update(killed=False, kill_reason="")   # причина kill — стара сума; звірка перевірить знову
            self.j.event("INFO", "start", f"{self.tag()}бюджет змінено: {old} → {new} USDT на бота ({moved} бот.)")

    # --- дані ----------------------------------------------------------------------------
    def now(self) -> int:
        return self.c.server_now_ms()

    def refresh_market(self, now: int) -> None:
        try:
            self.m.bid, self.m.ask = self.c.book_ticker(self.pair)
            self.m.price_ts, self.m.connected = now, True
        except (NetworkError, BinanceError) as e:
            if self.m.connected:
                self.j.event("WARN", "market", f"немає ціни: {e}")
            self.m.connected = False

    def refresh_features(self, now: int) -> None:
        fp = self.fp
        try:
            k_atr = self.c.klines(self.pair, fp.atr_tf, limit=200)
            k_1h = self.c.klines(self.pair, "1h", limit=1000)
            k_1d = self.c.klines(self.pair, "1d", limit=1000)
            need = fp.anchor_h * 60 + 1
            k_1m = self.c.klines(self.pair, "1m", limit=min(need, 1000))
            while len(k_1m) < need:
                older = self.c.klines(self.pair, "1m", limit=min(need - len(k_1m), 1000), end_time=k_1m[0][0] - 1)
                if not older:
                    break
                k_1m = older + k_1m
        except (NetworkError, BinanceError) as e:
            self.j.event("WARN", "features", f"ознаки не оновлено: {e}")
            return
        self.m.candles = {"1m": k_1m[-1440:], "1h": k_1h[-1000:], "1d": k_1d[-1000:]}
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
            self.strat.on_fill(self.state, fill, atr=self.m.feat.atr)
            self.extra["entries"][key] = [fill.ts, fill.price, b.entry_cost]
            msg = f"{self.tag()}КУПІВЛЯ бот {fill.bot_id}: {fill.qty} {self.base} по {fill.price:.2f}, стоп {b.stop_px:.2f}"
            self.j.event("INFO", "fill", msg)
            self.n.send(f"🟢 [{self.s.mode}] {msg}")
            coid = self.broker.place_stop(fill.bot_id, b.eth, b.stop_px, fill.ts) if b.stop_px > 0 else None
            if coid:
                self.extra["stops"][key] = coid
            elif self.broker.name != "paper" and b.stop_px > 0:
                self.j.event("WARN", "stop", f"стоп на біржі не поставлено для бота {fill.bot_id}; стоп веде агент")
        else:
            reason = reason or b.pending_reason or "?"
            held = b.eth
            self.strat.on_fill(self.state, fill)
            ets, epx, cost = self.extra["entries"].pop(key, [fill.ts, fill.price, ZERO])
            # Продати можна лише кратне кроку 0,0001 ETH: залишок («пил») лишається на рахунку і в капіталі бота,
            # тому в PnL угоди йде лише частка вартості входу, що відповідає проданому.
            frac = min(D(1), fill.qty / held) if held > 0 else D(1)
            pnl = fill.qty * fill.price - fill.fee_quote - D(cost) * frac
            self.j.trade(fill.bot_id, int(ets), fill.ts, epx, fill.price, fill.qty, pnl, f"{self.tag()}{reason}".strip())
            self.risk.on_trade_closed(fill.ts, pnl)
            if getattr(self.strat, "__name__", "").endswith("ai_trader"):
                self.extra["ai_pnls"].append(str(pnl))
            self.extra["stops"].pop(key, None)
            msg = f"{self.tag()}ПРОДАЖ бот {fill.bot_id} ({reason}): {fill.qty} {self.base} по {fill.price:.2f}, PnL {pnl:+.2f} USDT"
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
            # Відмова біржі (ключ/права/IP, баланс, фільтри) не зникне за 10 с — пауза нових ордерів,
            # одне сповіщення замість спаму. Стопи на біржі працюють і без агента.
            first = self.order_backoff_until <= it.ts
            self.order_backoff_until = it.ts + 5 * 60_000
            if first:
                detail = str(r.detail)
                if "-2015" in detail:                    # змінилась IP — окреме сповіщення з адресою (auth_check)
                    auth_check(self.broker, self.j, self.n)
                    detail = "Binance не прийняв ключ (-2015) — див. повідомлення про IP"
                self.j.event("WARN", "order", f"{self.tag()}ордер відхилено: {detail} · наступна спроба через 5 хв")
                self.n.send(f"⚠️ [{self.s.mode}] {self.tag()}ордер відхилено: {detail[:400]}")

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
        need = demand([self])
        self.balance_diff = shortage(need, bal)
        if self.balance_diff > 0:
            self.j.event("ERROR", "reconcile", f"на біржі менше, ніж думає агент: {self.balance_diff:.2f}%",
                         exchange=bal, agent=need)

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
            earn_step(self.broker, [self], now)
            auth_check(self.broker, self.j, self.n)
        intents = self.check_stop_files(now)

        if self.m.connected and self.m.bid > 0:
            px = (self.m.bid + self.m.ask) / 2
            was_killed = self.risk.killed
            self.risk.on_equity(now, self.equity(px))
            if self.risk.killed and not was_killed:
                self.kill(now, self.risk.kill_reason)
            feat = self.m.feat
            if self.ai_signal is not None:
                feat = dataclasses.replace(feat, ai_signal=self.ai_signal)
                self.ai_signal = None
            self.state, new, blocked = self.strat.step(self.state, Tick(now, px), feat, self.p)
            if feat.ai_signal is not None:
                why = blocked[0][1] if blocked else ("виконується" if new else "без дії")
                self.j.event("INFO", "ai_trade", f"{self.tag()}сигнал {feat.ai_signal.get('id')}: {why}")
            intents += new
            if now < self.order_backoff_until:
                for it in new:                       # нові входи/виходи стратегії — після паузи
                    v3.on_reject(self.state, it.bot_id)
                intents = [it for it in intents if it not in new]
            for it in intents:
                if it.side is Side.BUY and self.ai_mult < 1:
                    it = self.apply_ai(it, px)
                    if it is None:
                        continue
                veto = self.risk.check(it, self.context(now, px))
                if veto:
                    v3.on_reject(self.state, it.bot_id)
                    self.j.event("INFO", "veto", f"{self.tag()}вето входу бота {it.bot_id}: {veto}")
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
        self.j.event("INFO", "ai", f"вхід бота {it.bot_id} зменшено ШІ: {it.qty} → {qty} {self.base}")
        return dataclasses.replace(it, qty=qty, quote_qty=None if it.quote_qty is None else it.quote_qty * self.ai_mult)

    def status(self, now: int, px: Decimal) -> dict:
        f = self.m.feat
        self.last_status = {
            "mode": self.s.mode, "pair": self.pair, "base": self.base, "sleeve": self.name or "v3", "ts": now, "price": f"{px:.2f}",
            "strategy": getattr(self.strat, "__name__", "v3").rsplit(".", 1)[-1], "ai_mult": str(self.ai_mult),
            "bid": str(self.m.bid), "ask": str(self.m.ask), "connected": self.m.connected,
            "equity": f"{self.equity(px):.2f}",
            "start_equity": f"{self.p.budget_usdt * self.p.bots:.2f}", "ema_dist_pct": str(f.ema_dist_pct),
            "killed": self.risk.killed, "kill_reason": self.risk.kill_reason,
            "pause_until": self.risk.pause_until, "pause_reason": self.risk.pause_reason,
            "atr": str(f.atr), "atr_pct": str(f.atr_pct), "ema_slope_pct": str(f.ema_slope_pct),
            "anchor_high": str(f.anchor_high), "entry_gate": self.strat.entry_gate(f, self.p) or "відкрито",
            "bots": [{"id": b.bot_id, "phase": b.phase.value, "usdt": f"{b.usdt:.2f}", "eth": f"{b.eth:.5f}",
                      "entry": f"{b.entry_px:.2f}" if b.entry_px else "", "stop": f"{b.stop_px:.2f}" if b.stop_px else "",
                      "entry_ts": b.entry_ts if b.phase is not v3.Phase.FLAT else 0,
                      "take": f"{b.sell_max:.2f}" if b.sell_max and getattr(self.strat, "__name__", "").endswith("ai_trader") else "",
                      "armed": b.buy_armed or b.sell_armed,
                      "level": f"{(f.anchor_high - D(b.bot_id) * self.p.interval_atr * f.atr + self.p.buy.limit_bot * f.atr):.2f}"
                      if f.atr and f.anchor_high and hasattr(self.p, "interval_atr") else ""}
                     for b in self.state.bots],
        }
        return self.last_status

    def run(self, stop_after_s: int | None = None) -> str | None:
        t_end = time.time() + stop_after_s if stop_after_s else None
        self.j.event("INFO", "start", f"агент запущено, режим {self.s.mode}")
        self.n.send(f"▶️ ETH-Агент запущено, режим {self.s.mode}")
        start_notes(self.j, self.analyst)
        outage_note(self)
        while t_end is None or time.time() < t_end:
            t0 = time.time()
            try:
                ai_step(self, [self], self.now())
                self.tick()
                record_equity(self, [self], self.now())
                heartbeat_ping(self, self.now())
                if (self.home / "CLEAR_KILL").exists():
                    (self.home / "CLEAR_KILL").unlink()
                    self.clear_kill()
                    self.j.event("INFO", "kill", "kill switch знято з панелі (ручна команда Командира)")
                if restart_requested(self.home):
                    self.j.event("INFO", "start", "перезапуск з панелі")
                    return "restart"
            except Exception as e:                       # noqa: BLE001 — нічого не ковтаємо мовчки
                self.j.event("ERROR", "crash", f"{type(e).__name__}: {e}")
                self.n.send(f"⚠️ [{self.s.mode}] помилка циклу: {type(e).__name__}: {e}")
            time.sleep(max(1.0, self.s.poll_s - (time.time() - t0)))


def demand(agents: list) -> dict:
    """Скільки кожної монети має бути на біржі за обліком агента: {"USDT": …, "ETH": …, "SOL": …}."""
    out = {"USDT": ZERO}
    for a in agents:
        base = getattr(a, "base", "ETH")
        for b in a.state.bots:
            out["USDT"] += b.usdt
            out[base] = out.get(base, ZERO) + b.eth
    return out


def shortage(need: dict, bal: dict) -> Decimal:
    """Найбільша нестача у % серед монет (0 — усе зійшлось, допуск 0,5 %)."""
    short = [(v - bal.get(k, ZERO)) / v * HUNDRED for k, v in need.items()
             if v > 0 and bal.get(k, ZERO) < v * D("0.995")]
    return max(short) if short else ZERO


def market_for_analyst(statuses: list[dict]) -> dict:
    """Стислий ринковий пакет для ШІ-аналітика зі статусів рукавів (без ключів і балансів біржі)."""
    s0 = next((x for x in statuses if x), {})
    if not s0:
        return {}
    return {"price": s0.get("price"), "ema_dist_pct_vs_ema200d": s0.get("ema_dist_pct"),
            "atr_1d_pct": s0.get("atr_pct"), "ema200_1h_slope_24h_pct": s0.get("ema_slope_pct"),
            "high_24h": s0.get("anchor_high"),
            "sleeves": [{"name": x.get("sleeve"), "pair": x.get("pair"), "entry_gate": x.get("entry_gate"),
                         "in_coin": any(b.get("phase") != "FLAT" for b in x.get("bots", [])),
                         **({"price": x.get("price"), "ema_dist_pct_vs_ema200d": x.get("ema_dist_pct")}
                            if x.get("pair") != s0.get("pair") else {})} for x in statuses if x]}


def book_for_analyst(agents: list, journal) -> dict:
    """Позиції, стопи, результат і стан ризику — для «контролю ставок»."""
    sleeves = []
    for a in agents:
        st = a.last_status or {}
        sleeves.append({
            "name": a.name or "v3", "pair": getattr(a, "pair", "ETHUSDT"), "equity": st.get("equity"), "start_equity": st.get("start_equity"),
            "killed": st.get("killed"), "kill_reason": st.get("kill_reason"), "pause_reason": st.get("pause_reason"),
            "entry_gate": st.get("entry_gate"),
            "positions": [{"bot": b["id"], "phase": b["phase"], "qty": b["eth"], "asset": st.get("base", "ETH"), "usdt": b["usdt"], "entry": b["entry"],
                           "stop": b["stop"]} for b in st.get("bots", []) if b.get("phase") != "FLAT"],
            "flat_bots": sum(1 for b in st.get("bots", []) if b.get("phase") == "FLAT"),
            "pending_orders": len(a.extra.get("pending", {}) or {}), "exchange_stops": len(a.extra.get("stops", {}) or {}),
            "balance_diff_pct": str(getattr(a, "balance_diff", "0")),
        })
    trades = [{"bot": t[0], "entry_px": t[3], "exit_px": t[4], "pnl": t[6], "reason": t[7]}
              for t in journal.recent_trades(10)]
    out = {"mode": agents[0].s.mode, "sleeves": sleeves, "recent_trades": trades,
           "recent_trades_note": "лише закриті угоди; відкриті позиції — у sleeves[].positions"}
    em = getattr(agents[0].broker, "earn", None)
    if em is not None and em.parked > 0:
        out["earn"] = {"usdt_in_flexible_deposit": f"{em.parked:.2f}",
                       "note": "USDT агента в гнучкому депозиті Binance; перед кожною купівлею повертаються на спот автоматично"}
    out.update(fresh_warnings(journal.recent_events(300), int(time.time() * 1000)))
    ai = ai_agent(agents)
    if ai is not None and ai.m.bid > 0:
        from agent.strategy import ai_trader as at
        px = (ai.m.bid + ai.m.ask) / 2
        f, b = ai.m.feat, ai.state.bots[0]
        lo, hi = at.stop_bounds(f, ai.p)
        pnls = [D(x) for x in ai.extra.get("ai_pnls", [])]
        out["ai_sleeve"] = {
            "phase": b.phase.value, "equity": f"{ai.equity(px):.2f}", "budget": f"{ai.p.budget_usdt:.2f}",
            "entry": f"{b.entry_px:.2f}" if b.entry_px else None, "stop": f"{b.stop_px:.2f}" if b.stop_px else None,
            "setups_now": [k for k in at.SETUPS if at.setup_holds(k, px, f, ai.p) is None],
            "setups_explained": at.SETUPS, "sell_reasons": at.SELL_REASONS,
            "stop_range_pct": [f"{lo:.2f}", f"{hi:.2f}"], "closed_trades": len(pnls),
            "pnl_total": f"{sum(pnls, ZERO):.2f}"}
    return out


def fresh_warnings(events: list, now_ms: int, window_h: float = 6) -> dict:
    """Лише актуальні попередження: за останні window_h год; помилки ордерів, після яких уже був
    успішний fill, вважаються розв'язаними і ШІ не показуються (інакше він бачить старі -2015 як поточні)."""
    last_fill = max((ts for ts, _, kind, _ in events if kind == "fill"), default=0)
    warn = []
    for ts, lvl, kind, msg in events:
        if lvl not in ("WARN", "ERROR") or now_ms - ts > window_h * 3_600_000:
            continue
        if kind in ("order", "stop") and ts < last_fill:
            continue
        warn.append({"min_ago": int((now_ms - ts) / 60_000), "kind": kind, "msg": msg[:200]})
    return {"recent_warnings": warn[:8],
            "last_fill_min_ago": int((now_ms - last_fill) / 60_000) if last_fill else None}


def ai_agent(agents: list):
    return next((a for a in agents if getattr(a.strat, "__name__", "").endswith("ai_trader")), None)


def ai_gate(an, ai, j, n) -> None:
    """Автовимкнення ШІ-трейдера: після ≥ 10 угод profit factor < 1 → лише аналітика, сповіщення."""
    pnls = [D(x) for x in ai.extra.get("ai_pnls", [])]
    if an.trading_disabled or len(pnls) < 10:
        return
    win = sum((x for x in pnls if x > 0), ZERO)
    loss = -sum((x for x in pnls if x < 0), ZERO)
    if loss > 0 and win / loss < 1:
        an.trading_disabled = f"PF {win / loss:.2f} < 1 після {len(pnls)} угод"
        ai.extra["ai_disabled"] = an.trading_disabled
        j.event("WARN", "ai_trade", f"ШІ-трейдер вимкнено: {an.trading_disabled}. Відкриті позиції ведуть стоп і час.")
        n.send(f"🧠⛔ ШІ-трейдер вимкнено: {an.trading_disabled}. Увімкнути знову — лише рішенням Командира.")


def ai_step(owner, agents: list, now: int) -> None:
    """Раз на цикл: передати множник ШІ рукавам і, за потреби, запустити оцінку."""
    an = owner.analyst
    if an is None:
        return
    mult = an.size_mult(now)
    for a in agents:
        a.ai_mult = mult
    ai = ai_agent(agents)
    if ai is not None:
        if ai.extra.get("ai_disabled") and not an.trading_disabled:
            an.trading_disabled = ai.extra["ai_disabled"]
        ai_gate(an, ai, owner.j, owner.n)
        if an.precheck is None:
            from agent.strategy import ai_trader as at

            def precheck(sig, _a=ai):
                if _a.m.bid <= 0:
                    return "немає ціни"
                b = _a.state.bots[0]
                if sig["action"] == "BUY" and b.phase is not v3.Phase.FLAT:
                    return "рукав уже в позиції"
                if sig["action"] == "SELL" and b.phase is not v3.Phase.LONG:
                    return "рукав без позиції"
                if _a.risk.killed:
                    return "kill switch рукава"
                return at.check_signal(sig, (_a.m.bid + _a.m.ask) / 2, _a.m.feat, _a.p, _a.now())
            an.precheck = precheck
        if an.signal is not None:
            ai.ai_signal = an.pop_signal(now)
    killed = any(a.risk.killed for a in agents)
    force = "kill switch" if killed and not getattr(owner, "_ai_saw_kill", False) else ""
    owner._ai_saw_kill = killed
    an.maybe_run(now, market_for_analyst([a.last_status for a in agents]), book_for_analyst(agents, owner.j), force)


def record_equity(owner, agents: list, now: int) -> None:
    """Раз на 5 хв: загальний капітал і ціна — для графіка капіталу на панелі."""
    if now < getattr(owner, "_next_eq", 0):
        return
    sts = [a.last_status for a in agents if a.last_status]
    if not sts:
        return
    owner._next_eq = now + 300_000
    total = sum(float(x.get("equity") or 0) for x in sts)
    start = sum(float(x.get("start_equity") or 0) for x in sts)     # бюджет: зміна межі капіталу ≠ збиток
    path = owner.home / f"equity_{owner.s.mode}.csv"
    if not getattr(owner, "_eq_checked", False):        # старий формат без бюджету — у архів, графік з нуля
        owner._eq_checked = True
        try:
            first = path.read_text().splitlines()[0]
            if first.count(",") < 3:
                path.replace(path.with_name(f"equity_{owner.s.mode}_old.csv"))
        except (OSError, IndexError):
            pass
    with path.open("a") as fh:
        fh.write(f"{now},{total:.2f},{sts[0].get('price')},{start:.2f}\n")


def earn_step(broker, agents: list, now: int) -> None:
    """Відсотки на вільні USDT: перекласти надлишок у гнучкий депозит (або повернути, якщо вимкнено)."""
    em = getattr(broker, "earn", None)
    if em is None:
        return
    em.sweep(sum((b.usdt for a in agents for b in a.state.bots), ZERO), now)


def auth_check(broker, j, n) -> None:
    """Binance не приймає ключ → один раз дізнатись поточну IP і повідомити (панель блимає червоним)."""
    ae = getattr(broker, "auth_error", None)
    if not ae or ae.get("ip"):
        return
    ae["ip"] = public_ip() or "?"
    msg = (f"Binance не приймає ключ (-2015): найімовірніше змінилась IP-адреса інтернету. Зараз: {ae['ip']}. "
           f"Додай її в ключ: Binance → Керування API → Редагувати → Обмеження IP. Торгівля стоїть, доки ключ не прийнято.")
    j.event("ERROR", "key", msg)
    n.send("⛔ " + msg)


def public_ip() -> str:
    """Поточна зовнішня IPv4-адреса (для підказки, яку IP додати в ключ Binance)."""
    import urllib.request
    for url in ("https://api4.ipify.org", "https://ipv4.icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip.count(".") == 3:
                    return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


def outage_note(owner, now_ms: int | None = None) -> None:
    """Після старту: якщо агент не працював > 15 хв (сів телефон, перезавантаження) — сказати, скільки."""
    now_ms = now_ms or int(time.time() * 1000)
    try:
        lines = (owner.home / f"equity_{owner.s.mode}.csv").read_text().strip().splitlines()
        last = int(lines[-1].split(",")[0])
    except (OSError, ValueError, IndexError):
        return
    gap_min = (now_ms - last) // 60_000
    if gap_min < 15:
        return
    since = time.strftime("%d.%m %H:%M", time.localtime(last / 1000))
    msg = (f"Агент не працював {gap_min // 60} год {gap_min % 60} хв (з {since}). Стан відновлено, "
           f"позиції звіряються з біржею. Щоб не повторилось: телефон на зарядці, автозапуск — scripts/autostart.sh")
    owner.j.event("WARN", "start", msg)
    owner.n.send("⚠️ " + msg)


def heartbeat_ping(owner, now: int) -> None:
    """Раз на 5 хв — пінг на HEALTHCHECK_URL (напр. healthchecks.io): якщо пінги зникнуть, сервіс сам напише
    в Telegram/на пошту, навіть коли телефон вимкнений. У фоні — торговий цикл не чекає."""
    url = getattr(owner, "healthcheck_url", "")
    if not url or now < getattr(owner, "_next_hc", 0):
        return
    owner._next_hc = now + 300_000

    def ping():
        import urllib.request
        try:
            urllib.request.urlopen(url, timeout=10).read()
        except Exception:  # noqa: BLE001
            pass
    import threading
    threading.Thread(target=ping, daemon=True).start()


def restart_requested(home: Path) -> bool:
    f = home / "RESTART"
    if f.exists():
        f.unlink()
        return True
    return False


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
        need = demand(self.agents)
        diff = shortage(need, bal)
        for a in self.agents:
            a.balance_diff = diff
        if diff > 0:
            agent_s = ", ".join(f"{k} {v:.5f}" if k != "USDT" else f"USDT {v:.2f}" for k, v in need.items())
            ex_s = ", ".join(f"{k} {bal.get(k, ZERO):.5f}" if k != "USDT" else f"USDT {bal.get(k, ZERO):.2f}" for k in need)
            self.j.event("ERROR", "reconcile",
                         f"на біржі менше, ніж думає портфель: {diff:.1f} % (агент: {agent_s}; біржа: {ex_s}). "
                         f"Поповни USDT або зменш «Live: межа капіталу»")

    def tick(self) -> None:
        now = self.agents[0].now()
        if now >= self.next_reconcile and self.broker.name != "paper":
            self.reconcile()
            every = 60 if getattr(self.broker, "auth_error", None) else self.s.reconcile_every_s
            self.next_reconcile = now + every * 1000
        earn_step(self.broker, self.agents, now)
        for a in self.agents:
            a.tick()
        for b in getattr(self, "brokers", [self.broker]):
            if b is not self.broker and getattr(b, "auth_error", None) and not getattr(self.broker, "auth_error", None):
                self.broker.auth_error = b.auth_error          # помилка ключа на іншій парі — та сама тривога
                b.auth_error = None
        auth_check(self.broker, self.j, self.n)

    def clear_kill(self) -> None:
        for a in self.agents:
            a.clear_kill()

    def run(self, stop_after_s: int | None = None) -> str | None:
        t_end = time.time() + stop_after_s if stop_after_s else None
        names = ", ".join(a.name for a in self.agents)
        self.j.event("INFO", "start", f"портфель запущено ({names}), режим {self.s.mode}")
        self.n.send(f"▶️ ETH-Агент: портфель {names}, режим {self.s.mode}")
        start_notes(self.j, self.analyst)
        outage_note(self)
        while t_end is None or time.time() < t_end:
            t0 = time.time()
            try:
                ai_step(self, self.agents, self.agents[0].now())
                self.tick()
                record_equity(self, self.agents, self.agents[0].now())
                heartbeat_ping(self, self.agents[0].now())
                if (self.home / "CLEAR_KILL").exists():
                    (self.home / "CLEAR_KILL").unlink()
                    self.clear_kill()
                    self.j.event("INFO", "kill", "kill switch знято з панелі (ручна команда Командира)")
                if restart_requested(self.home):
                    self.j.event("INFO", "start", "перезапуск з панелі")
                    return "restart"
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
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
              "ANTHROPIC_WORKSPACE_ID", "HEALTHCHECK_URL"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def build(settings: LiveSettings, home: Path) -> "Agent | Portfolio":
    home.mkdir(parents=True, exist_ok=True)
    sec = read_secrets(home)
    live = settings.mode == "live"
    if live and not settings.live_confirmed:
        raise LiveDisabled("Режим live не підтверджено: панель → Налаштування → Режим live + підтвердження.")
    if settings.mode not in ("paper", "testnet", "live"):
        raise LiveDisabled(f"Невідомий режим {settings.mode}")
    kk, ks = ("BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET") if live else ("BINANCE_API_KEY", "BINANCE_API_SECRET")
    if settings.mode != "paper" and not (sec.get(kk) and sec.get(ks)):
        raise SystemExit(f"Для {settings.mode} потрібні {kk} і {ks} (панель → Налаштування → ключі)")
    if live:
        total = sum((sl.params.budget_usdt * sl.params.bots for sl in settings.sleeves), ZERO)
        if not settings.sleeves or total > settings.live_max_capital + D("0.01"):
            raise LiveDisabled(f"Live: бюджет рукавів {total} перевищує межу {settings.live_max_capital} $")
    client = Client(BASES[settings.mode], sec.get(kk, ""), sec.get(ks, ""))
    client.sync_time()
    filters = pair_filters(client, settings.pair)
    journal = Journal(home / f"journal_{settings.mode}.sqlite")
    notifier = Notifier(sec.get("TELEGRAM_TOKEN"), sec.get("TELEGRAM_CHAT_ID"))
    if settings.mode == "paper":
        broker = PaperBroker(settings.v3.fee_pct, filters)
    else:
        broker = ExchangeBroker(client, settings.pair, filters, settings.mode, settings.stop_limit_buffer_pct)
    from agent.analyst import shadow
    from agent.strategy import ai_trader
    if not settings.sleeves:
        obj = Agent(settings, client, broker, home, journal, notifier, filters)
    else:
        from agent.strategy import trend
        brokers, fl = {settings.pair: broker}, {settings.pair: filters}
        for sl in settings.sleeves:                     # інші пари (напр. SOL) — свої фільтри й брокер, той самий рахунок
            p = sl.pair or settings.pair
            if p not in brokers:
                fl[p] = pair_filters(client, p)
                brokers[p] = PaperBroker(settings.v3.fee_pct, fl[p]) if settings.mode == "paper" else \
                    ExchangeBroker(client, p, fl[p], settings.mode, settings.stop_limit_buffer_pct)
        agents = [Agent(settings, client, brokers[sl.pair or settings.pair], home, journal, notifier,
                        fl[sl.pair or settings.pair],
                        strategy={"trend": trend, "ai": ai_trader}.get(sl.strategy, v3), params=sl.params, risk=sl.risk,
                        features=sl.features, name=sl.name, pair=sl.pair) for sl in settings.sleeves]
        obj = Portfolio(agents)
        obj.brokers = list(brokers.values())
        extra = sorted({a.pair for a in agents} - {settings.pair})
        if extra:
            journal.event("INFO", "start", f"додаткові пари: {', '.join(extra)} "
                                           f"({', '.join(a.name for a in agents if a.pair != settings.pair)})")
    obj.analyst = shadow.from_settings(settings.analyst, sec, journal, home, notifier)
    obj.bnb_fees = None
    obj.healthcheck_url = sec.get("HEALTHCHECK_URL", "") if sec.get("HEALTHCHECK_URL", "").startswith("https://") else ""
    if live:
        live_preflight(client, settings, journal, notifier)
        obj.bnb_fees = bnb_check(client, journal, notifier)
        from agent.live.earn import EarnManager
        ec = settings.earn or {}
        em = EarnManager(client, home, journal, notifier, enabled=bool(ec.get("enabled", False)),
                         reserve_pct=D(str(ec.get("reserve_pct", 20))))
        if em.enabled or em.parked > 0:
            for b in getattr(obj, "brokers", [broker]):     # перед купівлею будь-якої пари USDT повертаються з депозиту
                b.earn = em
    return obj


def pair_filters(client, pair: str) -> SymbolFilters:
    from agent.data.exchange_info import default_filters
    try:
        return SymbolFilters.from_exchange_info(client.exchange_info(pair), pair)
    except (NetworkError, BinanceError):
        return ETHUSDT_DEFAULT if pair == "ETHUSDT" else default_filters(pair)


def bnb_check(client, journal, notifier) -> bool | None:
    """Чи платиться комісія в BNB (−25 %) і чи є BNB на рахунку. Лише повідомляє — налаштування не змінює."""
    try:
        on = bool(client.bnb_burn().get("spotBNBBurn"))
        acc = client.account()
    except (BinanceError, NetworkError):
        return None
    bnb = next((D(b["free"]) for b in acc.get("balances", []) if b["asset"] == "BNB"), ZERO)
    if not on:
        msg = ("Комісія НЕ в BNB. Щоб платити на 25 % менше: Binance → Профіль → Налаштування → "
               "«Оплачувати комісію в BNB» і купи BNB на 3–5 $.")
    elif bnb <= D("0.002"):
        msg = "Оплата комісії в BNB увімкнена, але BNB майже немає — купи BNB на 3–5 $, інакше знижки не буде."
    else:
        journal.event("INFO", "live", f"Комісія в BNB: увімкнено, BNB {bnb} — знижка 25 %")
        return True
    journal.event("WARN", "live", msg)
    notifier.send("🪙 " + msg)
    return on


def live_preflight(client, settings, journal, notifier) -> None:
    """Перед live: права ключа і баланс. Право виведення — попередження (рішення Командира), не блок."""
    acc = client.account()
    if not acc.get("canTrade"):
        raise LiveDisabled("Live: ключ без права спотової торгівлі")
    usdt = next((D(b["free"]) for b in acc.get("balances", []) if b["asset"] == "USDT"), ZERO)
    total = sum((sl.params.budget_usdt * sl.params.bots for sl in settings.sleeves), ZERO)
    msg = (f"LIVE: межа капіталу агента {total:.2f} $ (з {settings.live_max_capital} $), на рахунку вільних USDT {usdt:.2f}. "
           f"Агент торгує лише своїм бюджетом, решту рахунку не чіпає.")
    journal.event("WARN", "live", msg)
    notifier.send("🔴 " + msg)
    if acc.get("canWithdraw"):
        w = "LIVE: ключ має право ВИВЕДЕННЯ коштів. Агент його не використовує; захист — IP-обмеження ключа."
        journal.event("WARN", "live", w)
        notifier.send("⚠️ " + w)
    if usdt < total:
        journal.event("WARN", "live", f"LIVE: вільних USDT ({usdt:.2f}) менше за бюджет ({total:.2f}) — частина входів не пройде")
    for line in sleeve_feasibility(settings):
        journal.event("WARN", "live", line)
        notifier.send("⚠️ " + line)


def sleeve_feasibility(settings) -> list[str]:
    """Чи зможе рукав відкрити хоч одну угоду при мінімальному ордері біржі (5 $) і своєму розмірі за ризиком."""
    out = []
    mn = D("5.5")      # 5 $ minNotional + запас на комісію й округлення
    for sl in settings.sleeves:
        p = sl.params
        per_bot = p.budget_usdt
        if per_bot < mn:
            out.append(f"Рукав {sl.name}: {per_bot:.2f} $ на бота < 5 $ (мінімальний ордер Binance) — угод не буде")
            continue
        if sl.strategy == "trend" and getattr(p, "alloc_pct", None) is not None:
            continue
        stop_pct = D("7") if sl.strategy != "ai" else D("5")       # типовий стоп 2 ATR(1d) / ШІ
        by_risk = per_bot * p.bots * p.risk_per_trade_pct / stop_pct
        if by_risk < mn:
            need = mn * stop_pct / p.risk_per_trade_pct
            out.append(f"Рукав {sl.name}: розмір за ризиком {p.risk_per_trade_pct} % ≈ {by_risk:.2f} $ < 5 $ — угод не буде "
                       f"(потрібно ≈ {need:.0f} $ у рукаві)")
    return out
