"""Стоп під мінімумом прострілу (stop_mode=sweep) — гіпотеза для бектесту."""

import dataclasses

from agent.money import D
from agent.strategy import v3
from agent.strategy.sleeves import GROUPS, PRESETS, SLEEVES
from agent.strategy.types import Tick


def _run(mode, buf="0.25"):
    p = dataclasses.replace(v3.V3Params(bots=1, budget_usdt=D(1000), risk_per_trade_pct=D(100)),
                            stop_mode=mode, sweep_buffer_atr=D(buf))
    st = v3.init_state(p)
    f = v3.Features(atr=D(100), atr_pct=D(2), ema_slope_pct=D(0), anchor_high=D(3000))
    out = []
    for i, px in enumerate([2950, 2890, 2850, 2800, 2860, 2920]):   # падіння до 2800, відскок
        _, intents, _ = v3.step(st, Tick(ts=i * 60_000, price=D(px)), f, p)
        out += intents
    return out


def test_atr_stop_unchanged():
    i = _run("atr")[0]
    assert i.stop == D(2920) - 3 * D(100)


def test_sweep_stop_below_swept_low():
    i = _run("sweep")[0]
    assert i.stop == D(2800) - D("0.25") * 100        # мінімум прострілу − 0,25 ATR


def test_sweep_size_atr_keeps_atr_size():
    a, s = _run("atr")[0], _run("sweep_size_atr")[0]
    assert s.qty == a.qty and s.stop == D(2775)


def test_presets_registered():
    assert "збалансований_зн25" in GROUPS["sweep"] and "B1_зн0" in SLEEVES
    assert all(not k.endswith(("зн0", "зн25", "зн50", "зн25фікс")) for k in GROUPS["all"])
    assert PRESETS["live_міні_зн25"] == {"Bm_зн25": 0.5, "Cm": 0.5}
