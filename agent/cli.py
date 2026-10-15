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

    paths = klines.download(a.symbol, a.start, a.end, Path(a.data))
    try:
        f = exchange_info.fetch_filters(a.symbol, cache_dir=Path(a.data))
        print(f"exchangeInfo {a.symbol}: tick={f.tick_size} step={f.step_size} minNotional={f.min_notional}")
    except Exception as exc:  # noqa: BLE001
        print(f"exchangeInfo недоступний ({exc}); бектест візьме вбудовані фільтри ETHUSDT")
    return 0 if paths else 1


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

    cfg = load_config(Path(a.config), a.set)
    data = Path(a.data)
    df = klines.load(cfg.agent.pair, a.start, a.end, data)
    filters = exchange_info.load_cached_filters(cfg.agent.pair, data) or exchange_info.ETHUSDT_DEFAULT
    if a.strategy == "v3":
        from agent.backtest.v3_engine import run_v3
        from agent.features import FeatureParams
        from agent.risk import RiskParams

        rep = run_v3(df, cfg.v3.to_params(filters.step_size, filters.min_notional),
                     RiskParams(**cfg.risk.model_dump()), FeatureParams(**cfg.features.model_dump()),
                     slippage_pct=cfg.backtest.slippage_pct)
        out = {"strategy": "v3", **rep.summary()}
    elif a.strategy == "trend":
        import dataclasses

        from agent.backtest.v3_engine import run_v3
        from agent.features import FeatureParams
        from agent.risk import RiskParams
        from agent.strategy import trend

        fp = dataclasses.replace(FeatureParams(**cfg.features.model_dump()), atr_tf="1d",
                                 anchor_h=cfg.trend.breakout_days * 24)
        rep = run_v3(df, cfg.trend.to_params(filters.step_size, filters.min_notional),
                     RiskParams(**cfg.risk.model_dump()), fp, slippage_pct=cfg.backtest.slippage_pct,
                     strategy=trend)
        out = {"strategy": "trend", **rep.summary()}
    else:
        if cfg.legacy is None:
            print("Немає секції legacy у конфігу")
            return 2
        params = cfg.legacy.to_params(first_price=D(df["open"].iloc[0]), qty_step=filters.step_size)
        rep, _ = run_legacy(df, params)
        out = {"strategy": "legacy_v2", "grid_base": str(params.base_price), **rep.summary()}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.summary_md:
        Path(a.summary_md).write_text(_report_md(out, a), encoding="utf-8")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _cmd_walkforward(a) -> int:
    import yaml

    from agent.data import klines
    from agent.learning import walkforward as wf

    raw = yaml.safe_load(Path(a.config).read_text(encoding="utf-8")) or {}
    from agent.config import apply_overrides
    raw = apply_overrides(raw, a.set)
    df = klines.load(raw.get("agent", {}).get("pair", "ETHUSDT"), a.start, a.end, Path(a.data))
    res = wf.run_grid(df, raw, wf.GRIDS[a.grid](), workers=a.workers)
    r = wf.walk_forward(res, train=a.train, test=a.test, baseline=wf.BASELINES[a.grid],
                        min_train_trades=a.min_train_trades)
    md = wf.report_md(r, a.start, a.end)
    print(md)
    if a.summary_md:
        Path(a.summary_md).write_text(md, encoding="utf-8")
    return 0


def _cmd_portfolio(a) -> int:
    import os

    import yaml

    from agent.backtest import portfolio as pf
    from agent.data import klines

    raw = yaml.safe_load(Path(a.config).read_text(encoding="utf-8")) or {}
    df = klines.load(raw.get("agent", {}).get("pair", "ETHUSDT"), a.start, a.end, Path(a.data))
    from agent.strategy.sleeves import GROUPS
    presets = GROUPS.get(a.preset) or [a.preset]
    r = pf.run_all(df, raw, presets, workers=os.cpu_count())
    md = pf.report_md(r, a.start, a.end)
    if a.preset == "macro":
        from agent.backtest import event_study as es
        md += "\n" + es.report_md(es.study(df, 24, 24), 24, 24) + "\n" + es.report_md(es.study(df, 24, 2), 24, 2)
    print(md)
    if a.summary_md:
        Path(a.summary_md).write_text(md, encoding="utf-8")
    return 0


def _cmd_live(a) -> int:
    from agent.live import runner, settings
    from agent.live.status_server import serve

    cfg = Path(a.config)
    if not cfg.exists():
        cfg = Path(__file__).resolve().parent.parent / "config.example.yaml"
    s = settings.load(cfg, mode=a.mode)
    home = Path(a.home).expanduser()
    if a.telegram_setup:
        from agent.live.notify import chat_ids
        sec = runner.read_secrets(home)
        for cid, name in chat_ids(sec.get("TELEGRAM_TOKEN", "")):
            print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
        return 0
    try:
        agent = runner.build(s, home)
    except runner.LiveDisabled as e:
        print(e)
        return 2
    if a.clear_kill:
        agent.clear_kill()
        print("Kill switch знято. Запусти агента знову.")
        return 0
    serve(agent, s.status_port)
    print(f"Сторінка статусу: http://127.0.0.1:{s.status_port}   ·   журнал і стан: {home}")
    agent.run(stop_after_s=a.minutes * 60 if a.minutes else None)
    return 0


LABELS = {
    "period": "Період", "grid_base": "База сітки", "start_equity": "Капітал на старті, USDT",
    "end_equity": "Капітал в кінці, USDT", "pnl_pct": "Результат, %", "buy_and_hold_pct": "Просто тримати ETH, %",
    "realized_pnl_usdt": "Реалізований PnL, USDT", "unrealized_pnl_usdt": "Нереалізований PnL, USDT",
    "trades": "Закритих угод", "win_rate_pct": "Виграшних, %", "profit_factor_closed": "Profit factor (закриті)",
    "max_dd_pct": "Макс. просадка, %", "avg_hold_h": "Сер. час у позиції, год", "max_hold_h": "Макс. час у позиції, год",
    "open_positions_at_end": "Відкритих позицій у кінці",
    "exits": "Виходи (причина: к-сть)", "risk_vetoes": "Вето ризику", "gate_blocks": "Блоки фільтрів входу",
    "kill_switch": "Kill switch", "risk_events": "Подій ризику",
    "cagr_pct": "Річна дохідність (CAGR), %", "sharpe": "Sharpe (річний)", "exposure_pct": "Час у ринку, %",
    "buy_and_hold_max_dd_pct": "Просадка «просто тримати», %", "buy_and_hold_sharpe": "Sharpe «просто тримати»",
}


def _report_md(out: dict, a) -> str:
    fmt = lambda v: ", ".join(f"{k}: {n}" for k, n in v.items()) or "—" if isinstance(v, dict) else v
    rows = "\n".join(f"| {LABELS.get(k, k)} | {fmt(v)} |" for k, v in out.items() if k != "strategy")
    verdict = []
    try:
        if float(out["pnl_pct"]) < float(out["buy_and_hold_pct"]):
            verdict.append("Стратегія гірша за просте утримання ETH.")
        if int(out["open_positions_at_end"]) > 0:
            verdict.append(f"У кінці {out['open_positions_at_end']} позицій «застрягли» — дивись нереалізований PnL.")
    except (KeyError, ValueError):
        pass
    return (f"## Бектест `{out['strategy']}` · {a.start} … {a.end}\n\n| Показник | Значення |\n|---|---|\n"
            f"{rows}\n\n" + ("\n".join(f"- {v}" for v in verdict) + "\n" if verdict else ""))


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
    b.add_argument("--strategy", default="legacy_v2", choices=["legacy_v2", "v3", "trend"])
    b.add_argument("--out", help="зберегти звіт JSON")
    b.add_argument("--set", action="append", default=[], metavar="КЛЮЧ=ЗНАЧЕННЯ",
                   help="перевизначити параметр конфігу, напр. legacy.bots=7 (можна кілька разів)")
    b.add_argument("--summary-md", help="зберегти звіт Markdown (для сторінки GitHub Actions)")

    w = sub.add_parser("walkforward")
    w.add_argument("--config", default="config.yaml")
    w.add_argument("--start", required=True)
    w.add_argument("--end", required=True)
    w.add_argument("--train", type=int, default=3)
    w.add_argument("--test", type=int, default=1)
    w.add_argument("--workers", type=int, default=None)
    w.add_argument("--grid", default="daily", choices=["daily", "v2", "v1"])
    w.add_argument("--min-train-trades", type=int, default=5)
    w.add_argument("--set", action="append", default=[], metavar="КЛЮЧ=ЗНАЧЕННЯ")
    w.add_argument("--summary-md")

    pp = sub.add_parser("portfolio", help="портфельний бектест (суміш стратегій)")
    pp.add_argument("--config", default="config.yaml")
    pp.add_argument("--start", required=True)
    pp.add_argument("--end", required=True)
    pp.add_argument("--preset", default="all")
    pp.add_argument("--summary-md")

    lv = sub.add_parser("live", help="живий агент: paper або testnet")
    lv.add_argument("--mode", choices=["paper", "testnet", "live"], default="paper")
    lv.add_argument("--config", default="config.yaml")
    lv.add_argument("--home", default="~/.eth-agent", help="журнал, стан, secrets.env (поза git)")
    lv.add_argument("--minutes", type=int, default=0, help="зупинитись через N хвилин (для тесту)")
    lv.add_argument("--clear-kill", action="store_true", help="зняти kill switch вручну")
    lv.add_argument("--telegram-setup", action="store_true", help="показати chat id для secrets.env")

    a = p.parse_args(argv)
    return {"fetch": _cmd_fetch, "gaps": _cmd_gaps, "backtest": _cmd_backtest,
            "walkforward": _cmd_walkforward, "live": _cmd_live,
            "portfolio": _cmd_portfolio}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
