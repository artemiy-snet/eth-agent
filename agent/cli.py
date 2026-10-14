"""Командний рядок.

  eth-agent fetch    --start 2025-10 --end 2026-09        # історія 1m у data/klines
  eth-agent gaps     --start 2025-10 --end 2026-09        # перевірка пропусків
  eth-agent backtest --config config.yaml --start 2025-10 --end 2026-09 [--strategy legacy_v2]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.money import D


def _cmd_fetch(a) -> int:
    from agent.data import exchange_info, klines

    klines.download(a.symbol, a.start, a.end, Path(a.data))
    f = exchange_info.fetch_filters(a.symbol, cache_dir=Path(a.data))
    print(f"exchangeInfo {a.symbol}: tick={f.tick_size} step={f.step_size} minNotional={f.min_notional}")
    return 0


def _cmd_gaps(a) -> int:
    from agent.data import klines

    df = klines.load(a.symbol, a.start, a.end, Path(a.data))
    g = klines.gaps(df)
    print(f"свічок: {len(df)}, пропусків: {len(g)}")
    if len(g):
        print(g.to_string(index=False))
    return 0


def _cmd_backtest(a) -> int:
    from agent.backtest.engine import run_legacy
    from agent.config import load_config
    from agent.data import exchange_info, klines

    cfg = load_config(Path(a.config))
    data = Path(a.data)
    df = klines.load(cfg.agent.pair, a.start, a.end, data)
    filters = exchange_info.load_cached_filters(cfg.agent.pair, data) or exchange_info.ETHUSDT_DEFAULT
    if a.strategy != "legacy_v2" or cfg.legacy is None:
        print("Поки реалізовано лише --strategy legacy_v2 (потрібна секція legacy у конфігу)")
        return 2
    params = cfg.legacy.to_params(first_price=D(df["open"].iloc[0]), qty_step=filters.step_size)
    rep, _ = run_legacy(df, params)
    out = {"strategy": "legacy_v2", "grid_base": str(params.base_price), **rep.summary()}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eth-agent")
    p.add_argument("--data", default="data", help="тека даних (поза git)")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("fetch", "gaps"):
        s = sub.add_parser(name)
        s.add_argument("--symbol", default="ETHUSDT")
        s.add_argument("--start", required=True, help="YYYY-MM")
        s.add_argument("--end", required=True, help="YYYY-MM")

    b = sub.add_parser("backtest")
    b.add_argument("--config", default="config.yaml")
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    b.add_argument("--strategy", default="legacy_v2", choices=["legacy_v2", "v3"])
    b.add_argument("--out", help="зберегти звіт JSON")

    a = p.parse_args(argv)
    return {"fetch": _cmd_fetch, "gaps": _cmd_gaps, "backtest": _cmd_backtest}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
