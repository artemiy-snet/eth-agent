"""Walk-forward: вибір лише за навчальними місяцями, оцінка лише на перевірочних."""

from agent.learning.walkforward import MonthResult, Variant, walk_forward
from agent.money import D


def mr(v, m, pnl, n=20):
    return MonthResult(v, m, D(pnl), [D(pnl) / n] * n)


def test_choice_uses_only_train_months_and_scores_test():
    months = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    res = []
    # A: чудовий на навчанні, провальний на перевірці (перепідгонка); B — навпаки; base — нуль
    for m in months:
        res.append(mr("A", m, 10 if m <= "2026-03" else -10))
        res.append(mr("B", m, 1))
        res.append(mr("base", m, 0.5))
    r = walk_forward(res, train=3, test=1, baseline="base")
    w0 = r["windows"][0]
    assert w0.test == "2026-04" and w0.chosen == "A" and w0.test_pnl == D(-10)
    assert r["variants_tested"] == 3
    assert not r["passed"]


def test_min_train_trades_respected():
    months = ["2026-01", "2026-02", "2026-03", "2026-04"]
    res = []
    for m in months:
        res.append(mr("few", m, 100, n=1))
        res.append(mr("base", m, 1))
    r = walk_forward(res, train=3, baseline="base", min_train_trades=10)
    assert r["windows"][0].chosen == "base"


def test_grid_runs_end_to_end_on_synthetic():
    import numpy as np
    import pandas as pd

    from agent.learning import walkforward as wf
    from tests.test_data_and_backtest import synthetic_klines

    df = synthetic_klines(n=4 * 30 * 1440 // 8, vol=0.001)       # ~15 днів, переносимо на 4 місяці
    df["open_time"] = np.int64(1767225600000) + np.arange(len(df)) * 480_000   # 8 хв крок → 4 міс
    raw = {"agent": {"pair": "ETHUSDT"}, "features": {"ema_len": 20}}
    grid = [Variant("base", ["features.atr_tf=1h"]), Variant("k2", ["features.atr_tf=1h", "v3.k_stop_atr=2"])]
    res = wf.run_grid(df, raw, grid, workers=1, log=lambda *_: None)
    assert len(res) == 2 * 4
    r = wf.walk_forward(res, train=3, baseline="base", min_train_trades=0)
    assert len(r["windows"]) == 1
    assert "Walk-forward" in wf.report_md(r, "a", "b")


def test_multi_month_test_window():
    months = [f"2025-{m:02d}" for m in range(1, 9)]
    res = []
    for m in months:
        res.append(mr("A", m, 2))
        res.append(mr("base", m, 1))
    r = walk_forward(res, train=4, test=2, baseline="base")
    assert [w.test for w in r["windows"]] == ["2025-05…2025-06", "2025-07…2025-08"]
    assert r["windows"][0].test_pnl == D(4) and r["oos"]["pnl"] == D(8)


def test_daily_grid_has_baseline_and_no_risk_param():
    from agent.learning.walkforward import DAILY_BASELINE, grid_daily

    g = grid_daily()
    assert DAILY_BASELINE in {v.name for v in g}
    assert not any("risk_per_trade" in o for v in g for o in v.overrides)
