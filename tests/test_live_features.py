"""Ознаки живого агента збігаються з бектестовими (інакше бектест перевіряв би іншу стратегію)."""

import math

from agent.config import Config
from agent.features import FeatureParams, compute_features
from agent.features.params import TF_MS
from agent.live import features_live as fl
from agent.live import settings as ls
from tests.test_data_and_backtest import synthetic_klines


def resample(df, tf):
    step = TF_MS[tf]
    out = {}
    for t, o, h, l, c in zip(df.open_time, df.open, df.high, df.low, df.close):
        b = int(t) // step * step
        o, h, l, c = float(o), float(h), float(l), float(c)
        if b not in out:
            out[b] = [b, o, h, l, c]
        else:
            r = out[b]
            r[2], r[3], r[4] = max(r[2], h), min(r[3], l), c
    return [tuple(v) for v in out.values()]


def test_live_features_match_backtest_features():
    fp = FeatureParams(atr_tf="15m", atr_len=5, ema_len=10, ema_slope_h=2, anchor_h=2)
    df = synthetic_klines(n=3000)
    ref = compute_features(df, fp)
    k_atr, k_1h, k_1m = resample(df, "15m"), resample(df, "1h"), resample(df, "1m")
    for i in (1500, 2222, 2999):
        now = int(ref.minute.iloc[i])
        f, ret = fl.compute(k_atr, k_1h, k_1m, now, fp)
        r = ref.iloc[i]
        assert math.isclose(float(f.atr), r.atr, rel_tol=1e-6)
        assert math.isclose(float(f.atr_pct), r.atr_pct, rel_tol=1e-5)
        assert math.isclose(float(f.ema_slope_pct), r.ema_slope_pct, rel_tol=1e-4, abs_tol=1e-6)
        assert math.isclose(float(f.anchor_high), r.anchor_high, rel_tol=1e-9)
        assert math.isclose(ret, r.ret_1m_pct, rel_tol=1e-9, abs_tol=1e-12)


def test_live_ignores_unclosed_candle():
    df = synthetic_klines(n=200)
    k = resample(df, "1m")
    now = int(df.open_time.iloc[150]) + 30_000           # посеред хвилини 150
    f, _ = fl.compute(k, k, k, now, FeatureParams(atr_tf="1m", atr_len=3, ema_len=3, ema_slope_h=1, anchor_h=1))
    assert float(f.anchor_high) == max(float(x) for x in df.high.iloc[150 - 60:150])


def test_live_settings_match_research_config():
    import yaml

    raw = yaml.safe_load(open("config.example.yaml", encoding="utf-8"))
    cfg = Config.model_validate(raw)
    assert ls.v3_params(raw.get("v3")) == cfg.v3.to_params()
    assert ls.feature_params(raw.get("features")) == FeatureParams(**cfg.features.model_dump())
    from agent.risk import RiskParams
    assert ls.risk_params(raw.get("risk")) == RiskParams(**cfg.risk.model_dump())
    # і типові значення без конфігу
    assert ls.v3_params({}) == Config().v3.to_params()
