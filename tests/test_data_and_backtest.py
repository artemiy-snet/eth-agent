import io
import zipfile
from decimal import Decimal

import numpy as np
import pandas as pd

from agent.backtest.engine import run_legacy, ticks_from_klines
from agent.data import klines
from agent.data.exchange_info import ETHUSDT_DEFAULT, SymbolFilters
from agent.money import D
from agent.strategy import legacy_v2 as lv2


def synthetic_klines(n=2000, start=3000.0, seed=1, vol=0.0015) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(0, vol, n)))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, vol, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, vol, n))
    t0 = 1_760_000_000_000 // 60_000 * 60_000
    f = lambda a: [f"{x:.2f}" for x in a]
    return pd.DataFrame({
        "open_time": t0 + np.arange(n) * 60_000, "open": f(open_), "high": f(high),
        "low": f(low), "close": f(close),
    })


def test_filters_rounding_and_check():
    f = ETHUSDT_DEFAULT
    assert f.round_qty(D("0.123456")) == D("0.1234")
    assert f.round_price(D("3456.789")) == D("3456.78")
    assert f.check(D("0.001"), D(3000)) is not None          # 3 USDT < minNotional 5
    assert f.check(D("0.01"), D(3000)) is None


def test_filters_from_exchange_info_payload():
    payload = {"symbols": [{"symbol": "ETHUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00010000", "minQty": "0.00010000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ]}]}
    f = SymbolFilters.from_exchange_info(payload, "ETHUSDT")
    assert f.step_size == D("0.0001") and f.min_notional == D(5)


def test_vision_zip_microseconds_normalized():
    row = "1735689600000000,3300.1,3310,3290,3305.5,12.3,1735689659999999,40000,100,6,20000,0\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ETHUSDT-1m-2025-01.csv", row)
    df = klines.parse_vision_zip(buf.getvalue())
    assert int(df.open_time[0]) == 1735689600000
    assert df.close[0] == "3305.5"          # рядок, без втрат точності


def test_month_range_and_gaps():
    assert klines.month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    df = pd.DataFrame({"open_time": [0, 60_000, 240_000]})
    g = klines.gaps(df)
    assert len(g) == 1 and int(g.missing_candles[0]) == 2


def test_tick_path_order():
    df = pd.DataFrame({"open_time": [0], "open": ["10"], "high": ["12"], "low": ["9"], "close": ["11"]})
    assert [t.price for t in ticks_from_klines(df)] == [D(10), D(9), D(12), D(11)]


def test_backtest_runs_and_accounts_consistently():
    df = synthetic_klines()
    pt = lv2.PointParams(D("0.3"), D("-0.3"), D("0.1"), D("-0.1"))
    p = lv2.LegacyParams(base_price=D(2950), bots=5, interval_pct=D(1), budget_usdt=D(100),
                         step_sell_pct=D("0.3"), buy=pt, sell=pt)
    rep, intents = run_legacy(df, p)
    assert rep.start_equity == D(500)
    buys = sum(1 for i in intents if i.side.value == "BUY")
    sells = len(intents) - buys
    assert sells == len(rep.trades) and buys - sells == rep.open_positions
    s = rep.summary()
    assert s["trades"] == len(rep.trades)


def test_config_overrides(tmp_path):
    from agent.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text(open("config.example.yaml", encoding="utf-8").read(), encoding="utf-8")
    cfg = load_config(p, ["legacy.bots=7", "legacy.buy.limit_bot=-0.5", "legacy.sell_on_any_level=true"])
    assert cfg.legacy.bots == 7 and cfg.legacy.buy.limit_bot == Decimal("-0.5") and cfg.legacy.sell_on_any_level
