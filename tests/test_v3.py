"""Переходи стану v3."""

from decimal import Decimal

from agent.money import D
from agent.strategy import v3
from agent.strategy.types import Fill, Side, Tick

ATR = D(10)


def P(**kw):
    base = dict(bots=1, trend_block_pct=None, atr_min_pct=None, atr_max_pct=None, risk_per_trade_pct=D(100))
    base.update(kw)
    return v3.V3Params(**base)


def F(anchor=1000, atr=ATR, **kw):
    return v3.Features(atr=atr, atr_pct=kw.get("atr_pct"), ema_slope_pct=kw.get("slope"), anchor_high=D(anchor))


def feed(state, prices, p, f=None, t0=0, fill=True):
    intents, blocked = [], []
    for i, px in enumerate(prices):
        tick = Tick(t0 + i * 60_000, D(px))
        state, out, bl = v3.step(state, tick, f or F(), p)
        blocked += bl
        for it in out:
            intents.append(it)
            if fill:
                v3.on_fill(state, Fill(tick.ts, it.bot_id, it.side, it.qty, it.price, it.qty * it.price * D("0.001")),
                           atr=ATR)
    return state, intents, blocked


def test_arm_after_drop_and_buy_on_bounce():
    p = P()
    s = v3.init_state(p)
    # рівень бота 0 = 1000, зведення при ≤ 1000 − 1×ATR = 990; мін 980 → база 990; вікно [991; 994]
    s, it, _ = feed(s, [995, 989, 980, 985, 992], p)
    assert [i.side for i in it] == [Side.BUY]
    assert it[0].price == D(992) and it[0].stop == D(992) - 3 * ATR
    b = s.bots[0]
    assert b.phase is v3.Phase.LONG and b.stop_px == D(962)


def test_bounce_too_high_disarms():
    p = P()
    s, it, _ = feed(v3.init_state(p), [989, 980, 1001], p)    # 1001 ≥ база 990 + 1 ATR
    assert it == [] and not s.bots[0].buy_armed


def test_pending_blocks_second_intent():
    p = P()
    s, it, _ = feed(v3.init_state(p), [989, 980, 992, 993, "992.5"], p, fill=False)
    assert len(it) == 1 and s.bots[0].phase is v3.Phase.PENDING_BUY
    v3.on_reject(s, 0)
    assert s.bots[0].phase is v3.Phase.FLAT


def test_bots_are_staggered_by_interval():
    p = P(bots=3, interval_atr=D(2))
    # рівні: 1000, 980, 960 → зведення ≤ 990, 970, 950
    s, it, _ = feed(v3.init_state(p), [989, 975, 987], p)   # мін 975 → вікно [986; 989]
    assert {i.bot_id for i in it} == {0}
    assert s.bots[1].buy_armed is False and s.bots[2].buy_armed is False


def test_stop_loss_exits():
    p = P()
    s, _, _ = feed(v3.init_state(p), [989, 980, 992], p)
    s, it, _ = feed(s, [975, 961], p, t0=10 * 60_000)
    assert it[-1].side is Side.SELL and it[-1].reason == "v3_stop"
    assert s.bots[0].phase is v3.Phase.FLAT


def test_time_stop():
    p = P(max_hold_h=1)
    s, _, _ = feed(v3.init_state(p), [989, 980, 992], p)
    s, it, _ = feed(s, [993, 994], p, t0=2 * 3_600_000)
    assert it and it[0].reason == "v3_time"


def test_take_profit_trailing_never_below_min_profit():
    p = P()
    s, _, _ = feed(v3.init_state(p), [989, 980, 992], p)
    floor = v3.min_profit_px(D(992), p)
    # зведення при ≥ вхід + 1 ATR = 1002; максимум 1030 → вихід при ≤ 1030 − 0,9 ATR = 1021
    s, it, _ = feed(s, [1003, 1030, 1025, 1021], p, t0=10 * 60_000)
    assert len(it) == 1 and it[0].reason == "v3_take" and it[0].price == D(1021) >= floor


def test_trailing_exit_floored_at_breakeven_plus_edge():
    p = P(min_edge_pct=D(1))
    s, _, _ = feed(v3.init_state(p), [989, 980, 992], p)
    floor = v3.min_profit_px(D(992), p)              # ≈ 1003,9
    s, it, _ = feed(s, [1004, 1006, 1003], p, t0=10 * 60_000)
    assert it and it[0].reason == "v3_take" and it[0].price < floor   # пробили «підлогу» — продаємо
    s2, _, _ = feed(v3.init_state(p), [989, 980, 992], p)
    s2, it2, _ = feed(s2, [1004, 1006, 1005], p, t0=10 * 60_000)
    assert it2 == []                                 # над «підлогою» і над трейлінгом — тримаємо


def test_risk_based_size():
    p = P(risk_per_trade_pct=D("0.5"), budget_usdt=D(100))
    # капітал 100, ризик 0,5 USDT, відстань до стопу 30 → 0,0166 ETH (а не 100/992 ≈ 0,1)
    s, it, _ = feed(v3.init_state(p), [989, 980, 992], p)
    assert it[0].qty == D("0.0166")      # 0,0166 × 992 ≈ 16,5 USDT ≥ minNotional


def test_entry_gates():
    p = P(trend_block_pct=D(-3), atr_min_pct=D("0.3"))
    s, it, bl = feed(v3.init_state(p), [989, 980, 992], p, f=F(slope=-5, atr_pct=1))
    assert it == [] and ("trend_down" in {r for _, r in bl})
    s, it, bl = feed(v3.init_state(p), [989, 980, 992], p, f=F(slope=0, atr_pct=Decimal("0.1")))
    assert it == [] and ("atr_low" in {r for _, r in bl})


def test_no_entries_without_features():
    p = P()
    s, it, _ = feed(v3.init_state(p), [989, 980, 992], p, f=v3.Features(atr=None))
    assert it == [] and not s.bots[0].buy_armed


def test_profits_are_reinvested_compounding():
    """Прибуток збільшує наступну позицію: розмір = ризик% від ПОТОЧНОГО капіталу, бюджет бота = його USDT з прибутком."""
    p = P(risk_per_trade_pct=D("0.5"))
    s = v3.init_state(p)
    s, it1, _ = feed(s, [989, 980, 992], p)
    q1 = it1[0].qty
    s, out, _ = feed(s, [1013, 1060, 1044], p, t0=10 * 60_000)        # прибутковий вихід
    assert out and out[-1].side is Side.SELL and s.bots[0].usdt > D(100)
    s, it2, _ = feed(s, [989, 980, 992], p, t0=100 * 60_000)           # той самий сетап знову
    assert it2[0].qty > q1
