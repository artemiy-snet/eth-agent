"""Трендова стратегія: пробій, трейлінг-стоп, режим."""

from agent.money import D
from agent.strategy import trend, v3
from agent.strategy.types import Fill, Side, Tick

ATR = D(10)


def F(anchor=1000, dist=5):
    return v3.Features(atr=ATR, anchor_high=D(anchor), ema_dist_pct=None if dist is None else D(dist))


def feed(s, prices, p, f=None, t0=0):
    out = []
    for i, px in enumerate(prices):
        t = Tick(t0 + i * 60_000, D(px))
        s, it, _ = trend.step(s, t, f or F(), p)
        for x in it:
            out.append(x)
            trend.on_fill(s, Fill(t.ts, x.bot_id, x.side, x.qty, x.price, D(0)), atr=ATR)
    return s, out


P = trend.TrendParams(risk_per_trade_pct=D(100))


def test_breakout_buys_with_atr_stop():
    s, out = feed(trend.init_state(P), [990, 1001], P)
    assert [x.side for x in out] == [Side.BUY] and out[0].stop == D(1001) - 3 * ATR


def test_no_entry_in_down_regime():
    s, out = feed(trend.init_state(P), [1001, 1005], P, f=F(dist=-2))
    assert out == []


def test_trailing_stop_only_rises_and_exits():
    s, _ = feed(trend.init_state(P), [1001], P)
    s, _ = feed(s, [1050, 1100], P)
    assert s.bots[0].stop_px == D(1100) - 3 * ATR
    s, _ = feed(s, [1080], P)
    assert s.bots[0].stop_px == D(1070)                  # не опустився
    s, out = feed(s, [1069], P)
    assert out[-1].reason == "trend_trail" and s.bots[0].phase is v3.Phase.FLAT


def test_regime_break_exits():
    s, _ = feed(trend.init_state(P), [1001], P)
    s, out = feed(s, [1010], P, f=F(dist=-1))
    assert out and out[-1].reason == "trend_regime"
