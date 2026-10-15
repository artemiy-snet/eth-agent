import numpy as np

from agent.backtest import portfolio as pf


def test_combine_weights_and_stats():
    days = np.arange(4)
    a = pf.Curve(days, np.array([100, 110, 121, 133.1]))
    b = pf.Curve(days, np.array([100, 100, 100, 100.0]))
    c = pf.combine({"a": a, "b": b}, {"a": 0.5, "b": 0.5})
    assert np.allclose(c.equity, [1.0, 1.05, 1.105, 1.1655])
    s = pf.stats(c.days, c.equity)
    assert s["max_dd_pct"] == 0 and s["pnl_pct"] > 16


def test_drawdown_of_combined_curve_is_real_not_sum():
    days = np.arange(3)
    up = pf.Curve(days, np.array([100, 50, 100.0]))      # −50 % посередині
    down = pf.Curve(days, np.array([100, 150, 100.0]))   # +50 % посередині
    c = pf.combine({"u": up, "d": down}, {"u": 0.5, "d": 0.5})
    assert pf.stats(c.days, c.equity)["max_dd_pct"] == 0   # протилежні рухи гасять одне одного


def test_presets_reference_existing_sleeves():
    for name, w in pf.PRESETS.items():
        assert abs(sum(w.values()) - 1) < 1e-9 and set(w) <= set(pf.SLEEVES)


def test_portfolio_runs_on_synthetic():
    from tests.test_data_and_backtest import synthetic_klines
    df = synthetic_klines(n=3 * 1440, vol=0.002)
    raw = {"features": {"ema_len": 20, "regime_ema_days": 1}}
    r = pf.run_all(df, raw, ["збалансований"], log=lambda *_: None)
    assert "збалансований" in r["portfolios"] and set(r["sleeves"]) == {"B1", "C"}
    assert "Портфелі" in pf.report_md(r, "a", "b")
