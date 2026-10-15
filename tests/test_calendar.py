"""Календар макроподій і гейти за ним."""

from dataclasses import replace
from datetime import date, datetime, timezone

import numpy as np

from agent.features import calendar as cal
from agent.money import D
from agent.strategy import trend, v3
from agent.strategy.types import Fill, Tick


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_dst_offsets():
    assert cal.ny_offset_h(date(2024, 1, 31)) == -5
    assert cal.ny_offset_h(date(2024, 3, 10)) == -4        # 2-га неділя березня
    assert cal.ny_offset_h(date(2024, 3, 9)) == -5
    assert cal.ny_offset_h(date(2024, 11, 3)) == -5        # 1-ша неділя листопада
    assert cal.ny_offset_h(date(2024, 11, 2)) == -4


def test_event_times_in_utc():
    ev = dict((k, t) for t, k in cal.events() if t == ms("2024-01-31T19:00:00"))
    assert ev == {"fomc": ms("2024-01-31T19:00:00")}          # 14:00 EST
    assert ms("2024-06-12T12:30:00") in cal.event_times("cpi")  # 08:30 EDT
    assert ms("2024-06-12T18:00:00") in cal.event_times("fomc")


def test_no_unscheduled_emergency_meetings():
    days = {datetime.fromtimestamp(t / 1000, timezone.utc).date() for t in cal.event_times("fomc")}
    assert date(2020, 3, 3) not in days and date(2020, 3, 15) not in days


def test_calendar_sorted_and_counts():
    ts = cal.event_times()
    assert ts == sorted(ts)
    assert len(cal.event_times("fomc")) == 9 * 8


def test_proximity_and_window():
    e = ms("2024-01-31T19:00:00")
    nxt, prev = cal.proximity(e - 10 * 3_600_000, "fomc")
    assert nxt == 10 and prev > 24
    assert cal.in_window(*cal.proximity(e - 10 * 3_600_000, "fomc"), 24, 2)
    assert cal.in_window(*cal.proximity(e + 3_600_000, "fomc"), 24, 2)
    assert not cal.in_window(*cal.proximity(e + 3 * 3_600_000, "fomc"), 24, 2)
    assert not cal.in_window(*cal.proximity(e - 30 * 3_600_000, "fomc"), 24, 2)
    assert not cal.in_window(10.0, 1.0, None, None)


def test_vectorized_matches_scalar():
    from agent.features.core import event_columns

    rng = np.random.default_rng(1)
    m = np.sort(rng.integers(ms("2018-06-01T00:00:00"), ms("2028-06-01T00:00:00"), 500)) // 60_000 * 60_000
    nxt, prev = event_columns(m, "fomc,cpi")
    for i, t in enumerate(m):
        a, b = cal.proximity(int(t))
        assert (a is None and np.isnan(nxt[i])) or abs(a - nxt[i]) < 1e-9
        assert (b is None and np.isnan(prev[i])) or abs(b - prev[i]) < 1e-9


def test_v3_gate_macro_event():
    p = replace(v3.V3Params(), event_before_h=24, event_after_h=2)
    base = dict(atr=D(10), atr_pct=D(1), anchor_high=D(1000))
    assert v3.entry_gate(v3.Features(**base, event_in_h=5.0, event_ago_h=100.0), p) == "macro_event"
    assert v3.entry_gate(v3.Features(**base, event_in_h=100.0, event_ago_h=1.0), p) == "macro_event"
    assert v3.entry_gate(v3.Features(**base, event_in_h=30.0, event_ago_h=5.0), p) is None
    assert v3.entry_gate(v3.Features(**base, event_in_h=5.0), v3.V3Params()) is None   # вимкнено за замовчуванням


def test_trend_event_bot_exits_and_reenters():
    p = trend.TrendParams(bots=2, budget_usdt=D(250), alloc_pct=D(100), breakout=False, k_stop_atr=D(0),
                          k_trail_atr=D(0), event_bots=1, event_before_h=24, event_after_h=2)
    calm = v3.Features(atr=D(10), ema_dist_pct=D(5), event_in_h=100.0, event_ago_h=100.0)
    near = replace(calm, event_in_h=10.0)
    s = trend.init_state(p)

    def go(s, f, t):
        s, it, blocked = trend.step(s, Tick(t, D(1000)), f, p)
        for x in it:
            trend.on_fill(s, Fill(t, x.bot_id, x.side, x.qty, x.price, D(0)))
        return s, it, blocked

    s, it, _ = go(s, calm, 0)
    assert sorted(x.bot_id for x in it) == [0, 1]
    s, it, _ = go(s, near, 60_000)
    assert [(x.bot_id, x.side.value, x.reason) for x in it] == [(1, "SELL", "trend_event")]
    s, it, blocked = go(s, near, 120_000)
    assert it == [] and blocked == [(1, "macro_event")]
    s, it, _ = go(s, calm, 180_000)
    assert [x.bot_id for x in it] == [1]
