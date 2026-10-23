"""Виконання (Біблія, розділ 6): paper-біржа і біржа (testnet/live) з одним інтерфейсом.

Стан наміру: NEW → SENT → FILLED | REJECTED | UNKNOWN. UNKNOWN (немає відповіді) завжди
з'ясовується запитом статусу за clientOrderId, а не повторною відправкою.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from agent.data.exchange_info import SymbolFilters
from agent.live.binance import BinanceError, Client, NetworkError, fee_in, fill_from_response, fmt
from agent.money import HUNDRED, ZERO, D
from agent.strategy.types import Fill, Intent, Side

_seq = itertools.count(1)


ASSETS = ("ETH", "SOL", "USDT", "BNB")      # монети, які бачить звірка (бази пар агента + USDT + BNB для комісії)


def base_asset(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def client_order_id(prefix: str, it: Intent) -> str:
    # ≤ 36 символів, лише [A-Za-z0-9_-]
    return f"{prefix}{it.bot_id}{it.side.value[0]}{it.ts // 1000}{next(_seq) % 1000:03d}"[:36]


@dataclass
class ExecResult:
    status: str                    # FILLED | REJECTED | UNKNOWN
    coid: str
    fill: Fill | None = None
    detail: str = ""
    raw: dict = field(default_factory=dict)


class PaperBroker:
    """Віртуальне виконання: купівля за ask, продаж за bid, комісія в USDT. Стопи веде стратегія."""

    name = "paper"

    def __init__(self, fee_pct: Decimal, filters: SymbolFilters):
        self.fee = fee_pct / HUNDRED
        self.f = filters
        self.base = base_asset(filters.symbol)

    def execute(self, it: Intent, bid: Decimal, ask: Decimal, ts: int) -> ExecResult:
        coid = client_order_id("eap", it)
        qty = self.f.round_qty(it.qty)
        px = ask if it.side is Side.BUY else bid
        why = self.f.check(qty, px)
        if why:
            return ExecResult("REJECTED", coid, detail=why)
        return ExecResult("FILLED", coid, Fill(ts, it.bot_id, it.side, qty, px, qty * px * self.fee))

    def resolve(self, coid: str, it: Intent, ts: int) -> ExecResult:
        return ExecResult("REJECTED", coid, detail="paper: невідомий ордер")

    def place_stop(self, bot_id: int, qty: Decimal, stop: Decimal, ts: int) -> str | None:
        return None

    def cancel_stop(self, coid: str, bot_id: int, ts: int) -> tuple[str, Fill | None]:
        return "CANCELED", None

    def poll_stop(self, coid: str, bot_id: int, ts: int) -> Fill | None:
        return None

    def balances(self) -> dict[str, Decimal] | None:
        return None


class ExchangeBroker:
    """Testnet або live. MARKET-вхід на суму (quoteOrderQty) — бюджет бота не перевищується;
    одразу після входу на біржі ставиться STOP_LOSS_LIMIT, що спрацює навіть якщо агент впав."""

    def __init__(self, client: Client, symbol: str, filters: SymbolFilters, mode: str,
                 stop_buffer_pct: Decimal = D("1.0"), resolve_attempts: int = 3, sleep=time.sleep):
        self.c, self.symbol, self.f, self.name = client, symbol, filters, mode
        self.base = base_asset(symbol)
        self.buffer = stop_buffer_pct / HUNDRED
        self.attempts, self.sleep = resolve_attempts, sleep
        # префікс clientOrderId: для ETH — як раніше (eal/eat), для інших пар — з літерою пари (eals…)
        self.prefix = ("eat" if mode == "testnet" else "eal") + ("" if self.base == "ETH" else self.base[0].lower())
        self.auth_error = None           # {"since", "ip"} коли Binance не приймає ключ (-2015)
        self.earn = None                 # EarnManager (гнучкий депозит для вільних USDT), див. runner.build

    def bnb_price(self) -> Decimal:
        try:
            bid, ask = self.c.book_ticker("BNBUSDT")
            return (bid + ask) / 2
        except (BinanceError, NetworkError):
            return ZERO

    # --- ордери ---------------------------------------------------------------------------
    def _to_fill(self, o: dict, it_bot: int, side: Side, ts: int) -> Fill | None:
        qty, avg, fee_usdt, fee_eth = fill_from_response(o, self.base)
        if qty <= 0:
            return None
        bnb = fee_in(o, "BNB")
        if bnb > 0:                     # комісія в BNB: ETH приходить повністю, вартість BNB — у витрати угоди
            fee_usdt += bnb * self.bnb_price()
        if side is Side.BUY:
            # комісія з ETH: бот отримав менше ETH; у USDT-еквіваленті — щоб баланс USDT зійшовся
            return Fill(ts, it_bot, side, qty - fee_eth, avg, fee_usdt + fee_eth * avg)
        return Fill(ts, it_bot, side, qty, avg, fee_usdt + fee_eth * avg)

    def execute(self, it: Intent, bid: Decimal, ask: Decimal, ts: int) -> ExecResult:
        coid = client_order_id(self.prefix, it)
        params = dict(symbol=self.symbol, side=it.side.value, type="MARKET", newClientOrderId=coid)
        if it.side is Side.BUY:
            quote = (self.f.round_qty(it.qty) * ask).quantize(D("0.01"), rounding=ROUND_DOWN)
            if quote < self.f.min_notional:
                return ExecResult("REJECTED", coid, detail=f"сума {quote} < minNotional")
            params["quoteOrderQty"] = fmt(quote)
            if self.earn is not None:
                self.earn.ensure_spot(quote * D("1.002"))
        else:
            qty = self.f.round_qty(it.qty)
            why = self.f.check(qty, bid)
            if why:
                return ExecResult("REJECTED", coid, detail=why)
            params["quantity"] = fmt(qty)
        try:
            o = self.c.new_order(**params)
        except BinanceError as e:
            if e.code == -2015:
                self.auth_error = self.auth_error or {"since": int(time.time() * 1000), "ip": ""}
            return ExecResult("REJECTED", coid, detail=str(e))
        except NetworkError as e:
            return self.resolve(coid, it, ts, first_error=str(e))
        return self._result(o, coid, it, ts)

    def _result(self, o: dict, coid: str, it: Intent, ts: int) -> ExecResult:
        st = o.get("status", "")
        fill = self._to_fill(o, it.bot_id, it.side, ts)
        if st in ("FILLED", "PARTIALLY_FILLED") or (st == "EXPIRED" and fill):
            return ExecResult("FILLED", coid, fill, detail=st, raw=o)
        if st in ("NEW",):
            return ExecResult("UNKNOWN", coid, detail="ордер ще не виконаний", raw=o)
        return ExecResult("REJECTED", coid, detail=st or "невідомий статус", raw=o)

    def resolve(self, coid: str, it: Intent, ts: int, first_error: str = "") -> ExecResult:
        """UNKNOWN → запит статусу. Ніколи не відправляє ордер повторно."""
        last = first_error
        for i in range(self.attempts):
            if i:
                self.sleep(2 * i)
            try:
                return self._result(self.c.get_order(self.symbol, coid), coid, it, ts)
            except BinanceError as e:
                if e.code == -2013:          # ордер не існує — запит до біржі не дійшов
                    return ExecResult("REJECTED", coid, detail="ордер не створено (-2013)")
                last = str(e)
            except NetworkError as e:
                last = str(e)
        return ExecResult("UNKNOWN", coid, detail=last)

    # --- стопи на біржі -------------------------------------------------------------------
    def place_stop(self, bot_id: int, qty: Decimal, stop: Decimal, ts: int) -> str | None:
        coid = f"{self.prefix}{bot_id}S{ts // 1000}{next(_seq) % 1000:03d}"[:36]
        qty = self.f.round_qty(qty)
        stop_px = self.f.round_price(stop)
        limit_px = self.f.round_price(stop * (1 - self.buffer))
        if self.f.check(qty, limit_px):
            return None
        try:
            self.c.new_order(symbol=self.symbol, side="SELL", type="STOP_LOSS_LIMIT", timeInForce="GTC",
                             quantity=fmt(qty), price=fmt(limit_px), stopPrice=fmt(stop_px),
                             newClientOrderId=coid, newOrderRespType="ACK")
            return coid
        except (BinanceError, NetworkError):
            return None

    def cancel_stop(self, coid: str, bot_id: int, ts: int) -> tuple[str, Fill | None]:
        """Повертає ('CANCELED'|'FILLED'|'GONE'|'ERROR', fill якщо стоп уже спрацював)."""
        try:
            self.c.cancel_order(self.symbol, coid)
            return "CANCELED", None
        except BinanceError as e:
            if e.code == -2011:      # уже не активний — можливо, виконався
                try:
                    o = self.c.get_order(self.symbol, coid)
                    if o.get("status") == "FILLED":
                        return "FILLED", self._to_fill(o, bot_id, Side.SELL, ts)
                    return "GONE", None
                except (BinanceError, NetworkError):
                    return "ERROR", None
            return "ERROR", None
        except NetworkError:
            return "ERROR", None

    def poll_stop(self, coid: str, bot_id: int, ts: int) -> Fill | None:
        try:
            o = self.c.get_order(self.symbol, coid)
        except (BinanceError, NetworkError):
            return None
        if o.get("status") == "FILLED":
            return self._to_fill(o, bot_id, Side.SELL, ts)
        return None

    def balances(self) -> dict[str, Decimal] | None:
        try:
            acc = self.c.account()
        except BinanceError as e:
            if e.code == -2015:                     # ключ не прийнято (найчастіше — змінилась IP)
                self.auth_error = self.auth_error or {"since": int(time.time() * 1000), "ip": ""}
            return None
        except NetworkError:
            return None
        self.auth_error = None
        out = {b["asset"]: Decimal(b["free"]) + Decimal(b["locked"]) for b in acc.get("balances", [])
               if b["asset"] in ASSETS}
        if self.earn is not None:        # USDT агента, що лежать у гнучкому депозиті, теж його
            out["USDT"] = out.get("USDT", ZERO) + self.earn.parked
        return out


__all__ = ["PaperBroker", "ExchangeBroker", "ExecResult", "ZERO"]
