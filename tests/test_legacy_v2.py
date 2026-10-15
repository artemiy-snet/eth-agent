"""Юніт-тести переходів стану legacy_v2 (Біблія, розділ 11, етап 2)."""



from agent.money import D
from agent.strategy import legacy_v2 as lv2
from agent.strategy.types import Side, Tick

PT = lv2.PointParams(limit_up=D("0.3"), limit_bot=D("-0.3"), action_up=D("0.1"), action_bot=D("-0.1"))


def params(**kw) -> lv2.LegacyParams:
    base = dict(base_price=D(1000), bots=1, interval_pct=D(1), budget_usdt=D(100),
                step_sell_pct=D("0.3"), buy=PT, sell=PT)
    base.update(kw)
    return lv2.LegacyParams(**base)


def run(prices, p=None, state=None):
    p = p or params()
    s = state or lv2.init_state(p)
    out = []
    for i, px in enumerate(prices):
        s, it = lv2.step(s, Tick(i * 1000, D(px)), p)
        out += it
    return s, out


def test_grid_levels():
    p = params(bots=3, interval_pct=D("1.5"))
    s = lv2.init_state(p)
    assert [b.level for b in s.bots] == [D(1000), D("1015.0"), D("1030.0")]


def test_no_trade_without_arming():
    # ціна в вікні дії, але точка не зведена (ціна не опускалась нижче limitBot)
    s, out = run([1000, "1000.5", 1000])
    assert out == [] and not s.bots[0].buy_pt.armed


def test_arming_and_trailing_sticks_to_minimum():
    s, _ = run([1000, 990])
    pt = s.bots[0].buy_pt
    assert pt.armed
    _, bot, _, _ = pt.levels(PT)
    # limitBot «прилипає» до мінімуму: трохи нижче ціни, не далі ніж на один крок 0,03 %
    assert bot <= D(990) < bot * (1 + D("0.0003")) + D("0.01")


def test_buy_on_bounce_into_window():
    s, out = run([1000, 990, "992.5"])
    assert len(out) == 1 and out[0].side is Side.BUY
    b = s.bots[0]
    assert b.long and b.usdt < D(1) and b.eth > 0
    # комісія 0,1 % знята з ETH
    assert b.eth == out[0].qty * (1 - D("0.001"))


def test_bounce_too_high_disarms():
    # відскок одразу вище limitUp — точка розряджається, купівлі немає
    s, out = run([1000, 990, 1000])
    assert out == [] and not s.bots[0].buy_pt.armed


def test_greedy_touch_counter():
    p = params(greedy_buy=1)
    s, out = run([1000, 990, "992.5"], p)
    assert out == [] and s.bots[0].touches_buy == 0 and not s.bots[0].buy_pt.armed
    s, out = run([985, "987.5"], p, s)
    assert len(out) == 1 and out[0].side is Side.BUY


def test_sell_is_anchored_to_grid_not_entry():
    s, out = run([1000, 990, "992.5"])
    assert s.bots[0].long
    # ріст до 998 (вище входу, але нижче рівня сітки) не зводить продаж
    s, out2 = run([998, 996], state=s)
    assert out2 == [] and not s.bots[0].sell_pt.armed
    # зведення лише вище рівень×(1+0,3%)×(1+0,3%) ≈ 1006,0
    s, out3 = run([1010, 1008], state=s)
    assert len(out3) == 1 and out3[0].side is Side.SELL
    b = s.bots[0]
    assert not b.long and b.usdt > D(100) and b.deals == 1


def test_never_sells_below_level_without_sell_on_any_level():
    s, _ = run([1000, 990, "992.5"])
    s, out = run([950, 900, 880, 890, 960, 970], state=s)
    assert out == [] and s.bots[0].long


def test_gap_below_window_cancels_sell_arming():
    s, _ = run([1000, 990, "992.5"])
    s, _ = run([1010], state=s)
    assert s.bots[0].sell_pt.armed
    s, out = run([1000], state=s)   # проскочили вікно вниз за один тік
    assert out == [] and not s.bots[0].sell_pt.armed


def test_sell_on_any_level_is_not_a_stop_loss():
    p = params(sell_on_any_level=True)
    s, _ = run([1000, 990, "992.5"], p)
    s, out = run([970, 940, 900], p, s)
    assert out == [] and s.bots[0].long        # падіння не закриває позицію
    # база продажу сповзла вниз до ціни входу (не нижче)
    assert s.bots[0].sell_pt.main < D(1000)
    assert s.bots[0].sell_pt.main >= s.bots[0].last_buy_price * (1 + D("-0.0003"))


def test_can_buy_gate_blocks_but_keeps_arming():
    p = params()
    s = lv2.init_state(p)
    s.can_buy = False
    s, out = run([1000, 990, "992.5"], p, s)
    assert out == [] and s.bots[0].buy_pt.armed


def test_filter_flip_resets_array_like_v202():
    p = params()
    s, _ = run([1000, 990], p)
    assert s.bots[0].buy_pt.armed
    lv2.set_gates(s, p, can_buy=True, can_sell=False, price=D(990))
    b = s.bots[0]
    assert not b.buy_pt.armed and b.buy_pt.main == D(1000)   # зведення і трейлінг втрачено


def test_reset_restores_long_from_balances():
    p = params()
    s, _ = run([1000, 990, "992.5"], p)
    lv2.reset_array(s, p, D(992))
    assert s.bots[0].long and s.bots[0].buy_pt.was


def test_all_bots_above_price_buy_the_same_bounce():
    """v2.02: точка купівлі кожного бота трейлить до мінімуму, тож усі боти вище ціни
    купують на ОДНОМУ відскоку. Сітка розносить лише рівні продажу, не входи."""
    p = params(bots=3, interval_pct=D(5), frames_per_tick=1000)  # рівні 1000, 1050, 1100
    s, out = run([1000, 990, "992.5"], p)
    assert [b.long for b in s.bots] == [True, True, True]
    assert len(out) == 3 and len({i.bot_id for i in out}) == 3
    assert len({i.price for i in out}) == 1
