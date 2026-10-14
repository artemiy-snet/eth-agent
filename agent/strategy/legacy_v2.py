"""legacy_v2 — точна модель логіки CryptoBot v2.02 (BotArrayController02 / BotController02 / BotActionPoint02).

Навіщо: еталон, з яким порівнюється v3. Вади v2.02 тут відтворені СВІДОМО (позначені «v2.02:»),
виправлення живуть лише у v3. Опис поведінки — docs/v202_analysis.md.

Модель часу. У v2.02 ціна приходила раз на ~1 с, а FixedUpdate крутився 50 разів/с,
тож кожна ціна оброблялась до 50 «кадрів». Тут те саме: `frames_per_tick` ітерацій на ціну
з раннім виходом, щойно стан перестав змінюватись (результат ідентичний, швидше).

Модель виконання. v2.02 у віртуальному режимі виконував угоду миттєво за поточною ціною
в тому ж кадрі. legacy_v2 робить так само: застосовує «оптимістичне» виконання до власного
стану і повертає Intent. Реальне виконання й звірку робить execution (етап 6).

Чистота. Жодного вводу/виводу. Стан змінюється на місці заради швидкості бектесту
(мільйони тіків); для незалежної копії є LegacyState.clone().
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal

from agent.money import HUNDRED, THOUSAND, ZERO, D, floor_to_step, pct
from agent.strategy.types import Intent, Side, Tick


@dataclass(frozen=True)
class PointParams:
    """Пороги точки дії у % від бази (поля inputFieldPoint* панелі v2.02)."""

    limit_up: Decimal
    limit_bot: Decimal
    action_up: Decimal
    action_bot: Decimal


@dataclass(frozen=True)
class LegacyParams:
    base_price: Decimal                 # arrayBasePrice
    bots: int                           # arrayBotCount
    interval_pct: Decimal               # arrayBotIntervalP
    budget_usdt: Decimal                # botStartUSDT
    step_sell_pct: Decimal              # stepOfSellPoints (зсув точки продажу від рівня бота)
    buy: PointParams
    sell: PointParams
    greedy_buy: int = 0                 # greedyBuyValue (дотиків до купівлі)
    greedy_sell: int = 0                # greedySellValue
    sell_on_any_level: bool = False     # isSellOnAnyLevel
    fee_pct: Decimal = D("0.1")         # marketFee
    qty_step: Decimal = D("0.0001")     # LOT_SIZE stepSize (v2.02: TruncateFloat за точністю)
    frames_per_tick: int = 50           # FixedUpdate 50 Гц / ціна 1 Гц
    gap_cancel_abs: Decimal = D("500")  # v2.02: захардкожене limitBot − 500 (залишок від BTC)
    start_eth: Decimal = ZERO           # botStartBTC

    def bot_level(self, i: int) -> Decimal:
        # CreateBotArray: arrayBasePrice + i × arrayBasePrice × arrayBotIntervalP / 100
        return self.base_price + D(i) * self.base_price * self.interval_pct / HUNDRED


@dataclass
class PointState:
    offset_pct: Decimal      # basePointPriceOffsetP
    main: Decimal            # basePointMainPrice
    start: Decimal           # basePointMainPriceStart
    armed: bool = False      # pointReadyToAction
    was: bool = False        # pointBuyWas / pointSellWas
    offset_on: bool = True   # v2.02: UpdatePointStatus2AfterSell рахує базу БЕЗ зсуву

    def base(self) -> Decimal:
        return pct(self.main, self.offset_pct) if self.offset_on else self.main

    def levels(self, p: PointParams) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        b = self.base()
        return pct(b, p.limit_up), pct(b, p.limit_bot), pct(b, p.action_up), pct(b, p.action_bot)

    def reset(self) -> None:
        # ResetBuyWasPoint / ResetSellWasPoint
        self.main = self.start
        self.armed = False
        self.was = False
        self.offset_on = True


@dataclass
class BotState:
    bot_id: int
    level: Decimal
    usdt: Decimal
    eth: Decimal
    buy_pt: PointState
    sell_pt: PointState
    long: bool = False            # buyWas
    last_buy_price: Decimal = ZERO
    touches_buy: int = 0
    touches_sell: int = 0
    deals: int = 0

    def snapshot(self) -> tuple:
        return (
            self.usdt, self.eth, self.long, self.touches_buy, self.touches_sell,
            self.buy_pt.main, self.buy_pt.armed, self.buy_pt.was,
            self.sell_pt.main, self.sell_pt.armed, self.sell_pt.was, self.sell_pt.offset_on,
        )


@dataclass
class LegacyState:
    bots: list[BotState] = field(default_factory=list)
    can_buy: bool = True     # isArrayCanBuy (керують фільтри / тогл панелі)
    can_sell: bool = True    # isArrayCanSell

    def clone(self) -> "LegacyState":
        return copy.deepcopy(self)


def init_state(params: LegacyParams) -> LegacyState:
    """CreateBotArray + CreateActionPointsArray (префаб: 1 точка купівлі, 1 точка продажу на бота)."""
    bots = []
    for i in range(params.bots):
        lvl = params.bot_level(i)
        bots.append(BotState(
            bot_id=i,
            level=lvl,
            usdt=params.budget_usdt,
            eth=params.start_eth,
            buy_pt=PointState(offset_pct=ZERO, main=lvl, start=lvl),
            sell_pt=PointState(offset_pct=params.step_sell_pct, main=lvl, start=lvl),
            touches_buy=params.greedy_buy,
            touches_sell=params.greedy_sell,
        ))
    return LegacyState(bots=bots)


def reset_array(state: LegacyState, params: LegacyParams, price: Decimal) -> None:
    """v2.02: RestartBotArray() — викликається при КОЖНОМУ перемиканні тоглу (зокрема фільтрами).

    Баланси й лічильники дотиків живуть у файлах і переживають рестарт; зведення точок
    і трейлінг-бази — ні. buyWas відновлюється через BuyActionLoad: eth × ціна > usdt.
    """
    for b in state.bots:
        b.buy_pt.reset()
        b.sell_pt.reset()
        b.long = b.eth * price > b.usdt
        if b.long:
            # BuyActionLoad точки купівлі: база = ціна з файлу стану
            b.buy_pt.main = b.last_buy_price
            b.buy_pt.was = True


def set_gates(state: LegacyState, params: LegacyParams, can_buy: bool, can_sell: bool,
              price: Decimal) -> None:
    """Зміна дозволів так, як це робили фільтри v2.02: кожна зміна = RestartBotArray."""
    changed = (can_buy != state.can_buy) or (can_sell != state.can_sell)
    state.can_buy, state.can_sell = can_buy, can_sell
    if changed:
        reset_array(state, params, price)


# --- дії ---------------------------------------------------------------------------------

def _buy(b: BotState, params: LegacyParams, price: Decimal, ts: int, out: list[Intent]) -> None:
    """BuyAction2ActionPoint → BuyAction2BotController (віртуальне виконання v2.02)."""
    if b.buy_pt.was or b.long or b.usdt <= 0:
        return
    qty = floor_to_step(b.usdt / price, params.qty_step)   # v2.02: кількість, а не quoteOrderQty
    if qty <= 0:
        return
    out.append(Intent(ts, b.bot_id, Side.BUY, qty, price, "legacy_buy_window"))
    b.eth += qty - qty * params.fee_pct / HUNDRED          # v2.02: комісія завжди з ETH
    b.usdt -= qty * price
    b.touches_sell = max(params.greedy_sell, 0)            # ResetTouchSellCount
    b.long = True
    b.last_buy_price = price                               # SaveBotActionsFile: ціна ринку
    b.sell_pt.reset()                                      # ResetSellPoint
    b.buy_pt.was = True
    b.buy_pt.main = price                                  # basePointMainPrice = ціна угоди


def _sell(b: BotState, params: LegacyParams, price: Decimal, ts: int, out: list[Intent]) -> None:
    """SellAction2ActionPoint → SellAction2BotController."""
    if b.sell_pt.was or not b.long or b.eth <= 0:
        return
    qty = floor_to_step(b.eth, params.qty_step)
    if qty <= 0:
        return
    out.append(Intent(ts, b.bot_id, Side.SELL, qty, price, "legacy_sell_window"))
    b.buy_pt.reset()                                       # ResetBuyPoint (до await у v2.02)
    gross = qty * price
    b.usdt += gross - gross * params.fee_pct / HUNDRED
    b.eth -= qty                                           # v2.02: пил (dust) лишається у бота
    b.deals += 1
    b.touches_buy = max(params.greedy_buy, 0)              # ResetTouchBuyCount
    b.long = False
    b.sell_pt.was = True
    b.sell_pt.main = price
    b.sell_pt.offset_on = False                            # UpdatePointStatus2AfterSell


def _frame_buy(b: BotState, params: LegacyParams, price: Decimal, ts: int, out: list[Intent],
               can_buy: bool) -> None:
    """BotActionPoint02.LimitsBuyAction — дослівний порядок перевірок."""
    pt, p = b.buy_pt, params.buy
    up, bot, a_up, a_bot = pt.levels(p)
    if price >= up and pt.armed and not b.long:
        pt.armed = False
    if a_bot <= price <= a_up and pt.armed and not b.long and not pt.was:
        if b.touches_buy <= 0:
            if can_buy:                                     # isCanBuy перевіряється всередині дії
                _buy(b, params, price, ts, out)
        else:
            b.touches_buy = max(b.touches_buy - 1, 0)       # AddTouchBuyCount(-1)
            pt.armed = False
    if price <= bot and not b.long:
        pt.armed = True
        if price != 0:
            pt.main += pt.main * p.limit_bot / THOUSAND     # isMoving


def _frame_sell(b: BotState, params: LegacyParams, price: Decimal, ts: int, out: list[Intent],
                can_sell: bool) -> None:
    """BotActionPoint02.LimitsSellAction — дослівний порядок перевірок, рівні перераховуються після кожного зсуву."""
    pt, p = b.sell_pt, params.sell
    up, bot, a_up, a_bot = pt.levels(p)
    if price >= up and b.long:
        pt.armed = True
        if price != 0:
            if params.sell_on_any_level and pt.main < b.last_buy_price:
                pt.main = b.last_buy_price
            pt.main += pt.main * p.limit_up / THOUSAND
            up, bot, a_up, a_bot = pt.levels(p)
    if params.sell_on_any_level and price <= bot and b.last_buy_price <= pt.main and b.long:
        pt.armed = True
        if price != 0:
            pt.main += pt.main * p.limit_bot / THOUSAND
            up, bot, a_up, a_bot = pt.levels(p)
    if (a_bot <= price <= a_up and pt.armed and not pt.was and b.long and b.eth > 0):
        if b.touches_sell <= 0:
            if can_sell:
                _sell(b, params, price, ts, out)
        else:
            b.touches_sell = max(b.touches_sell - 1, 0)
            pt.armed = False
    if price <= bot and pt.armed and b.long and price >= bot - params.gap_cancel_abs:
        pt.armed = False


def step(state: LegacyState, tick: Tick, params: LegacyParams,
         seconds: int = 1) -> tuple[LegacyState, list[Intent]]:
    """Один тік ціни = до frames_per_tick × seconds кадрів FixedUpdate з тією ж ціною.

    seconds — скільки секунд реального часу представляє тік (v2.02 опитував ціну раз на 1 с).
    Від цього залежить, наскільки далеко встигає сповзти трейлінг-база (≤ 50 кроків/с).
    """
    intents: list[Intent] = []
    frames = params.frames_per_tick * max(seconds, 1)
    price = tick.price
    if price <= 0:
        return state, intents
    for b in state.bots:
        for _ in range(frames):
            before = b.snapshot()
            if not b.long:
                _frame_buy(b, params, price, tick.ts, intents, state.can_buy)
            else:
                _frame_sell(b, params, price, tick.ts, intents, state.can_sell)
            if b.snapshot() == before:
                break
    return state, intents
