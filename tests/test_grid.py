import numpy as np
import pandas as pd

from agent.backtest.grid import GridParams, run_grid


def _df(prices):
    p = np.array(prices, float)
    days = np.arange(len(p)) * 86_400_000          # одна «хвилина» = день, щоб крива мала точки
    return pd.DataFrame({"open_time": days, "open": p, "high": p, "low": p, "close": p})


def test_grid_profits_on_oscillation_and_counts_fees():
    prices = [100, 99, 98, 99, 100, 99, 98, 99, 100] * 5
    r = run_grid(_df(prices), GridParams(step_pct=1.0, levels=5, fee_pct=0.1, slip_pct=0))
    assert r["trades"] > 10 and r["end_equity"] > 500


def test_no_same_bar_round_trip():
    # одна свічка з великим діапазоном не може і купити, і продати той самий лот
    df = pd.DataFrame({"open_time": [0, 86_400_000], "open": [100, 97], "high": [103, 97],
                       "low": [97, 97], "close": [100, 97]})
    r = run_grid(df, GridParams(step_pct=1.0, levels=5, fee_pct=0, slip_pct=0))
    assert r["trades"] <= 3                        # лише купівлі в першій свічці
