"""Тест на кожен запобіжник розділу 5 (Біблія, розділ 11, етап 4)."""

from agent.money import D
from agent.risk import RiskContext, RiskGuard, RiskParams
from agent.strategy.types import Intent, Side

H = 3_600_000


def buy(ts=0, qty="0.01", px=1000, stop=970):
    return Intent(ts, 0, Side.BUY, D(qty), D(px), "t", stop=D(stop))


def sell(ts=0):
    return Intent(ts, 0, Side.SELL, D("1"), D(1000), "stop")


def ctx(ts=0, equity=1000, eth=0, open_risk=0, **kw):
    return RiskContext(ts=ts, equity=D(equity), eth_value=D(eth), open_risk=D(open_risk), **kw)


def guard(**kw):
    g = RiskGuard(RiskParams(**kw))
    g.on_equity(0, D(1000))
    return g


def test_ok_by_default():
    assert guard().check(buy(), ctx()) is None


def test_exits_are_never_vetoed():
    g = guard()
    g.kill(0, "test")
    assert g.check(sell(), ctx(exchange_connected=False, spread_pct=D(5))) is None


def test_max_eth_share():
    assert guard().check(buy(qty="0.2"), ctx(eth=500)) == "max_eth_share"     # 500 + 200 > 60 %


def test_max_open_risk_aggregate():
    # нова позиція ризикує 0,3 USDT; вже відкрито 19,8 → 20,1 > 2 % від 1000
    assert guard().check(buy(), ctx(open_risk="19.8")) == "max_open_risk"
    assert guard().check(buy(), ctx(open_risk="19.6")) is None


def test_daily_loss_pauses_until_next_utc_day():
    g = guard()
    assert g.check(buy(ts=H), ctx(ts=H, equity=979)) == "daily_loss"
    assert g.check(buy(ts=2 * H), ctx(ts=2 * H, equity=1000)).startswith("pause")
    g.on_equity(24 * H, D(979))                                # нова доба — нова база
    assert g.check(buy(ts=24 * H), ctx(ts=24 * H, equity=979)) is None


def test_max_drawdown_kill_switch_needs_manual_restart():
    g = guard()
    g.on_equity(H, D(1100))
    g.on_equity(2 * H, D(989))                                 # −10,09 % від піку
    assert g.killed
    assert g.check(buy(ts=100 * H), ctx(ts=100 * H, equity=1200)).startswith("kill_switch")
    g.manual_restart(100 * H)
    assert g.check(buy(ts=100 * H), ctx(ts=100 * H, equity=1200)) is None


def test_consecutive_losses_pause_4h():
    g = guard()
    for i in range(5):
        g.on_trade_closed(i, D(-1))
    assert g.check(buy(ts=10), ctx(ts=10)).startswith("pause: losses_in_row")
    assert g.check(buy(ts=4 * H + 10), ctx(ts=4 * H + 10)) is None


def test_win_resets_loss_streak():
    g = guard()
    for pnl in (-1, -1, -1, -1, 1, -1):
        g.on_trade_closed(0, D(pnl))
    assert g.check(buy(), ctx()) is None


def test_orders_per_hour():
    g = guard()
    for i in range(21):
        g.on_order(i * 1000)
    assert g.check(buy(ts=30_000), ctx(ts=30_000)).startswith("pause: orders_per_hour")


def test_price_move_1m_pause_15min():
    g = guard()
    g.on_price_move(0, D("-3.5"))
    assert g.check(buy(ts=60_000), ctx(ts=60_000)).startswith("pause: move_1m")
    assert g.check(buy(ts=16 * 60_000), ctx(ts=16 * 60_000)) is None


def test_spread():
    assert guard().check(buy(), ctx(spread_pct=D("0.15"))) == "spread"


def test_stale_data_blocks_entries():
    assert guard().check(buy(), ctx(data_age_s=61)) == "stale_data"


def test_exchange_disconnected():
    assert guard().check(buy(), ctx(exchange_connected=False)) == "exchange_disconnected"


def test_balance_mismatch_kills():
    g = guard()
    assert g.check(buy(), ctx(balance_diff_pct=D("0.6"))) == "balance_diff"
    assert g.killed
