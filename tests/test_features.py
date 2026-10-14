"""Ознаки: правильність і відсутність look-ahead (Біблія, розділ 11, етап 3)."""

import numpy as np

from agent.features import FeatureParams, compute_features
from tests.test_data_and_backtest import synthetic_klines

FP = FeatureParams(atr_tf="15m", atr_len=5, ema_len=10, ema_slope_h=2, anchor_h=2)


def test_no_lookahead_future_change_does_not_affect_past():
    df = synthetic_klines(n=3000)
    base = compute_features(df, FP)
    cut = 2000
    df2 = df.copy()
    # сильно змінюємо майбутнє (після хвилини cut)
    for c in ("open", "high", "low", "close"):
        df2.loc[cut:, c] = [f"{float(x) * 1.5:.2f}" for x in df2.loc[cut:, c]]
    f2 = compute_features(df2, FP)
    cols = ["atr", "atr_pct", "ema_slope_pct", "anchor_high", "ret_1m_pct"]
    # хвилина cut ще не бачить свічку cut (вона не закрита) — ознаки до неї включно однакові
    np.testing.assert_allclose(base.loc[:cut, cols].fillna(-1), f2.loc[:cut, cols].fillna(-1))
    assert not np.allclose(base.loc[cut + 1:, "anchor_high"], f2.loc[cut + 1:, "anchor_high"])


def test_feature_available_only_after_close():
    df = synthetic_klines(n=600)
    f = compute_features(df, FP)
    # ATR(5) з'являється рівно на межі закриття 15m-свічки і не раніше ніж через 5 свічок
    first = f["atr"].first_valid_index()
    m = int(f["minute"].iloc[first])
    assert m % 900_000 == 0
    assert m - int(f["minute"].iloc[0]) >= 4 * 900_000
    # ret_1m першої хвилини невідомий (свічка ще йде)
    assert np.isnan(f["ret_1m_pct"].iloc[0])
    o, c = float(df["open"].iloc[0]), float(df["close"].iloc[0])
    assert abs(f["ret_1m_pct"].iloc[1] - (c / o - 1) * 100) < 1e-9


def test_anchor_high_is_rolling_max_of_closed():
    df = synthetic_klines(n=400)
    f = compute_features(df, FP)
    highs = df["high"].astype(float)
    assert abs(f["anchor_high"].iloc[300] - highs.iloc[300 - 120:300].max()) < 1e-9
