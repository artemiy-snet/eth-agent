"""Walk-forward (Біблія, розділ 8, кроки 4–5).

Метод:
  1. Кожен варіант параметрів проганяється окремо по кожному календарному місяцю
     (місяць стартує «з нуля»: 5 ботів × бюджет, ознаки прогріті на попередній історії).
  2. Вікно k: навчання = місяці [k, k+train), перевірка = місяць k+train.
     На навчанні обирається варіант з найкращим PnL (за умови ≥ min_train_trades угод).
     На перевірці береться результат САМЕ цього варіанта — так, як було б у реальності.
  3. Порівняння з базовим варіантом (поточні параметри) на тих самих місяцях перевірки.

Критерії (розділ 8 і етап 1 розділу 9) рахуються лише на out-of-sample місяцях.
Звіт завжди показує, скільки варіантів перевірено (захист від самообману).

Спрощення, яке треба пам'ятати: позиції не переходять через межу місяця, а kill switch
діє в межах місяця. Для відбору параметрів це прийнятно; підсумковий кандидат потім
проганяється суцільним бектестом.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from agent.money import ZERO, D

_DF: pd.DataFrame | None = None
_FEATS: dict[str, pd.DataFrame] = {}


@dataclass
class MonthResult:
    variant: str
    month: str
    pnl: Decimal
    trades: list[Decimal] = field(default_factory=list)   # PnL кожної закритої угоди
    max_dd: Decimal = ZERO
    killed: bool = False


@dataclass
class Variant:
    name: str
    overrides: list[str]          # формат --set: "v3.k_stop_atr=2"


def grid_v2() -> list[Variant]:
    """Сітка №2 (після walk-forward №1): лише повільні ATR, ширші виходи, суворіший фільтр тренду.
    1h відкинуто: на році 2025-10…2026-09 усі 1h-варіанти були гірші за 4h."""
    exits = {
        "E2": ["v3.sell.limit_up=2.0", "v3.sell.action_up=0.5"],
        "E3": ["v3.sell.limit_up=3.0", "v3.sell.action_up=1.0"],
        "E4": ["v3.sell.limit_up=4.0", "v3.sell.action_up=1.5"],   # зведення +4 ATR, відкат 2,5
    }
    out = []
    for tf in ("4h", "1d"):
        for k in ("2", "3", "4"):
            for en, ex in exits.items():
                for tb in ("-3", "-1"):
                    out.append(Variant(f"{tf}·stop{k}·{en}·tr{tb}",
                                       [f"features.atr_tf={tf}", f"v3.k_stop_atr={k}", *ex,
                                        f"v3.filters.trend_block_pct={tb}"]))
    out.append(Variant(BASELINE, ["features.atr_tf=1h", "v3.k_stop_atr=3", "v3.sell.limit_up=1.0",
                                  "v3.sell.action_up=0.1"]))
    return out


def grid_daily() -> list[Variant]:
    """Сітка №3: лише денний ATR (висновок walk-forward №2). Розмір ризику НЕ підбирається —
    це рішення Командира (Біблія, розділ 5)."""
    exits = {
        "E2": ["v3.sell.limit_up=2.0", "v3.sell.action_up=0.5"],
        "E3": ["v3.sell.limit_up=3.0", "v3.sell.action_up=1.0"],
    }
    out = []
    for k in ("2", "3"):
        for en, ex in exits.items():
            for hold in ("72", "168"):
                for anchor in ("24", "72"):
                    for amax in ("4", "15"):
                        out.append(Variant(f"1d·stop{k}·{en}·hold{hold}·anc{anchor}·amax{amax}",
                                           ["features.atr_tf=1d", f"v3.k_stop_atr={k}", *ex,
                                            f"v3.max_hold_h={hold}", f"features.anchor_h={anchor}",
                                            f"v3.filters.atr_max_pct={amax}"]))
    return out


DAILY_BASELINE = "1d·stop2·E2·hold72·anc24·amax4"
GRIDS = {"v1": None, "v2": grid_v2, "daily": grid_daily}
BASELINES = {"v1": "1h·stop3·E1", "v2": "1h·stop3·E1", "daily": DAILY_BASELINE}


def default_grid() -> list[Variant]:
    """Невелика явна сітка гіпотез (див. журнал бектестів). Кожен варіант — гіпотеза, а не перебір усього."""
    exits = {
        "E1": ["v3.sell.limit_up=1.0", "v3.sell.action_up=0.1"],   # Біблія 10.1: зведення +1 ATR, відкат 0,9
        "E2": ["v3.sell.limit_up=2.0", "v3.sell.action_up=0.5"],   # зведення +2 ATR, відкат 1,5
        "E3": ["v3.sell.limit_up=3.0", "v3.sell.action_up=1.0"],   # зведення +3 ATR, відкат 2
    }
    out = []
    for tf in ("1h", "4h"):
        for k in ("1.5", "2", "3"):
            for en, ex in exits.items():
                out.append(Variant(f"{tf}·stop{k}·{en}", [f"features.atr_tf={tf}", f"v3.k_stop_atr={k}", *ex]))
    return out


BASELINE = "1h·stop3·E1"
GRIDS["v1"] = default_grid


def _init(df: pd.DataFrame, feats: dict[str, pd.DataFrame]) -> None:
    global _DF, _FEATS
    _DF, _FEATS = df, feats


def _run_month(args) -> MonthResult:
    from agent.backtest.v3_engine import run_v3
    from agent.config import Config, apply_overrides
    from agent.features import FeatureParams
    from agent.risk import RiskParams

    raw, variant, month = args
    cfg = Config.model_validate(apply_overrides(_deep(raw), ["agent.mode=backtest", *variant.overrides]))
    months = _DF["month"]
    mask = (months == month).to_numpy()
    df_m = _DF.loc[mask].reset_index(drop=True)
    f_m = _FEATS[_fkey(cfg)].loc[mask].reset_index(drop=True)
    rep = run_v3(df_m, cfg.v3.to_params(), RiskParams(**cfg.risk.model_dump()),
                 FeatureParams(**cfg.features.model_dump()), cfg.backtest.slippage_pct, feats=f_m)
    return MonthResult(variant.name, month, rep.pnl_usdt, [t.pnl_usdt for t in rep.trades], rep.max_dd_pct,
                       rep.extra.get("kill_switch", "—") != "—")


def _fkey(cfg) -> str:
    """Ключ кешу ознак — УСІ параметри ознак (раніше лише atr_tf: варіанти anchor_h ділили одні ознаки)."""
    import json
    return json.dumps(cfg.features.model_dump(), sort_keys=True)


def _deep(x):
    import copy
    return copy.deepcopy(x)


def run_grid(df: pd.DataFrame, raw_cfg: dict, variants: list[Variant], workers: int | None = None,
             log=print) -> list[MonthResult]:
    from agent.config import Config, apply_overrides
    from agent.features import FeatureParams, compute_features

    df = df.copy()
    df["month"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.strftime("%Y-%m")
    feats: dict[str, pd.DataFrame] = {}
    for v in variants:
        cfg = Config.model_validate(apply_overrides(_deep(raw_cfg), v.overrides))
        tf = _fkey(cfg)
        if tf not in feats:
            log(f"ознаки atr_tf={tf} …")
            feats[tf] = compute_features(df, FeatureParams(**cfg.features.model_dump()))
    months = sorted(df["month"].unique())
    jobs = [(raw_cfg, v, m) for v in variants for m in months]
    log(f"{len(variants)} варіантів × {len(months)} міс = {len(jobs)} прогонів")
    workers = workers or os.cpu_count() or 1
    if workers == 1:
        _init(df, feats)
        return [_run_month(j) for j in jobs]
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(df, feats)) as ex:
        return list(ex.map(_run_month, jobs, chunksize=2))


# --- аналіз ------------------------------------------------------------------------------

def _pf(trades: list[Decimal]) -> Decimal | None:
    g = sum((t for t in trades if t > 0), ZERO)
    l = -sum((t for t in trades if t < 0), ZERO)
    return None if l == 0 else g / l


@dataclass
class WindowResult:
    train: list[str]
    test: str
    chosen: str
    train_pnl: Decimal
    test_pnl: Decimal
    base_test_pnl: Decimal


def walk_forward(results: list[MonthResult], train: int = 3, test: int = 1,
                 baseline: str = BASELINE, min_train_trades: int = 10) -> dict:
    by = {(r.variant, r.month): r for r in results}
    months = sorted({r.month for r in results})
    variants = sorted({r.variant for r in results})
    windows: list[WindowResult] = []
    oos_trades: list[Decimal] = []
    base_trades: list[Decimal] = []
    oos_dd, base_dd = ZERO, ZERO
    for k in range(0, len(months) - train - test + 1, test):
        tr, te = months[k:k + train], months[k + train:k + train + test]
        best, best_pnl = None, None
        for v in variants:
            rs = [by[(v, m)] for m in tr]
            n = sum(len(r.trades) for r in rs)
            pnl = sum((r.pnl for r in rs), ZERO)
            if n >= min_train_trades and (best_pnl is None or pnl > best_pnl):
                best, best_pnl = v, pnl
        best = best or baseline
        ch = [by[(best, m)] for m in te]
        bs = [by[(baseline, m)] for m in te]
        windows.append(WindowResult(tr, te[0] if len(te) == 1 else f"{te[0]}…{te[-1]}", best, best_pnl or ZERO,
                                    sum((r.pnl for r in ch), ZERO), sum((r.pnl for r in bs), ZERO)))
        for r in ch:
            oos_trades += r.trades
            oos_dd = max(oos_dd, r.max_dd)
        for r in bs:
            base_trades += r.trades
            base_dd = max(base_dd, r.max_dd)

    oos_pnl = sum((w.test_pnl for w in windows), ZERO)
    base_pnl = sum((w.base_test_pnl for w in windows), ZERO)
    better = sum(1 for w in windows if w.test_pnl > w.base_test_pnl)
    pf, base_pf = _pf(oos_trades), _pf(base_trades)
    n = len(windows)
    checks = {
        "pf_not_worse": pf is not None and (base_pf is None or pf >= base_pf),
        "dd_not_worse_10pct": oos_dd <= base_dd * D("1.1") if base_dd > 0 else True,
        "better_in_2_of_3_windows": n > 0 and better * 3 >= n * 2,
        "trades_ge_30": len(oos_trades) >= 30,
    }
    stage1 = {
        "pf_gt_1_2": pf is not None and pf > D("1.2"),
        "dd_lt_15": oos_dd < 15,
        "trades_ge_100": len(oos_trades) >= 100,
    }
    full_year = []
    for v in variants:
        rs = [by[(v, m)] for m in months]
        tr_all = [t for r in rs for t in r.trades]
        full_year.append({"variant": v, "pnl": sum((r.pnl for r in rs), ZERO), "trades": len(tr_all),
                          "pf": _pf(tr_all), "kills": sum(r.killed for r in rs)})
    full_year.sort(key=lambda x: x["pnl"], reverse=True)
    return {
        "variants_tested": len(variants),
        "months": months,
        "windows": windows,
        "oos": {"pnl": oos_pnl, "trades": len(oos_trades), "pf": pf, "max_month_dd": oos_dd},
        "baseline": {"name": baseline, "pnl": base_pnl, "trades": len(base_trades), "pf": base_pf,
                     "max_month_dd": base_dd},
        "windows_better": f"{better}/{n}",
        "checks_vs_baseline": checks,
        "stage1_criteria": stage1,
        "passed": all(checks.values()) and all(stage1.values()),
        "in_sample_table": full_year,
    }


def report_md(r: dict, start: str, end: str) -> str:
    f = lambda x, n=2: "—" if x is None else f"{x:.{n}f}"
    ok = lambda b: "✅" if b else "❌"
    L = [f"## Walk-forward v3 · {start} … {end}", "",
         f"Перевірено варіантів: **{r['variants_tested']}**. Вікон: {len(r['windows'])}. Базовий: `{r['baseline']['name']}`.",
         f"Вердикт: **{'ПРОЙДЕНО' if r['passed'] else 'НЕ ПРОЙДЕНО'}**", "",
         "### Out-of-sample (лише місяці перевірки)", "",
         "| | Обраний на навчанні | Базовий (Біблія) |", "|---|---|---|",
         f"| PnL, USDT (на 500) | {f(r['oos']['pnl'])} | {f(r['baseline']['pnl'])} |",
         f"| Угод | {r['oos']['trades']} | {r['baseline']['trades']} |",
         f"| Profit factor | {f(r['oos']['pf'])} | {f(r['baseline']['pf'])} |",
         f"| Макс. просадка за місяць, % | {f(r['oos']['max_month_dd'])} | {f(r['baseline']['max_month_dd'])} |",
         f"| Вікон, де краще за базовий | {r['windows_better']} | |", "",
         "### Критерії", "",
         *[f"- {ok(v)} {k}" for k, v in {**r["checks_vs_baseline"], **r["stage1_criteria"]}.items()], "",
         "### Вікна", "", "| Перевірка | Обрано | PnL навч. | PnL перев. | Базовий перев. |", "|---|---|---|---|---|",
         *[f"| {w.test} | {w.chosen} | {f(w.train_pnl)} | {f(w.test_pnl)} | {f(w.base_test_pnl)} |" for w in r["windows"]],
         "", "### Усі варіанти за весь період (IN-SAMPLE — не для вибору!)", "",
         "| Варіант | PnL | Угод | PF | Місяців з kill |", "|---|---|---|---|---|",
         *[f"| {x['variant']} | {f(x['pnl'])} | {x['trades']} | {f(x['pf'])} | {x['kills']} |"
           for x in r["in_sample_table"]], ""]
    return "\n".join(L)


def report_md_header(r: dict) -> str:
    return f"Базовий варіант: `{r['baseline']['name']}`"
