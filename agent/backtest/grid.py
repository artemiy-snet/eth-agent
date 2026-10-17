"""Бектест сіткового бота (spot grid) на 1m-свічках — для порівняння з нашими стратегіями.

Модель (консервативна):
- рівні L_k = P0·(1+s)^k; сітка нижче опорної ціни P0 на n рівнів; лот = капітал/n USDT (переобчислюється
  при кожному переякорюванні — прибуток реінвестується);
- ціна опустилась до рівня → купівля лоту за ціною рівня; лот, куплений на L_k, продається на L_{k+1};
- у межах однієї свічки спершу продажі лотів, куплених РАНІШЕ, потім купівлі (без «покупки й продажу в одній
  хвилині» — це завищило б результат);
- комісія fee_pct з кожної угоди; прослизання slip_pct проти нас;
- ціна вийшла вгору при порожній сітці → переякорювання на поточну ціну (сітка «йде» за ринком);
- варіанти: stop — ціна впала нижче дна сітки на крок → продати все й переякоритись;
  regime — торгувати лише коли денна ціна вище EMA200 днів (закриті дні), інакше все в USDT.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd

from agent.backtest.portfolio import buy_and_hold, stats


@dataclass(frozen=True)
class GridParams:
    step_pct: float = 1.0
    levels: int = 10
    fee_pct: float = 0.1
    slip_pct: float = 0.02
    mode: str = "plain"          # plain | stop | regime
    capital: float = 500.0


def _regime_on(df: pd.DataFrame, ema_days: int = 200) -> np.ndarray:
    """Для кожної хвилини: чи була ціна закриття ПОПЕРЕДНЬОГО дня вище EMA200 днів (без зазирання вперед)."""
    day = (df["open_time"].to_numpy() // 86_400_000)
    closes = pd.Series(df["close"].to_numpy(float)).groupby(day).last()
    ema = closes.ewm(span=ema_days, adjust=False).mean()
    on = (closes > ema).shift(1).fillna(False)
    on.iloc[:ema_days] = False                         # поки EMA не «прогрілась»
    return on.reindex(day).to_numpy(bool)


def run_grid(df: pd.DataFrame, p: GridParams) -> dict:
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    t = df["open_time"].to_numpy()
    day = t // 86_400_000
    reg = _regime_on(df) if p.mode == "regime" else None
    g = 1 + p.step_pct / 100
    fee, slip = p.fee_pct / 100, p.slip_pct / 100

    cash = p.capital
    lots: deque = deque()          # ETH кожного лота; [0] — куплений на найнижчому рівні (k_ref)
    p0, k = o[0], 0                 # опорна ціна і поточний рівень
    lot_usdt = cash / p.levels
    trades = 0
    active = reg is None
    eq_days, eq_vals = [], []
    cur_day = day[0]

    def level(j):
        return p0 * g ** j

    def sell_all(px):
        nonlocal cash, trades
        while lots:
            q = lots.popleft()
            cash += q * px * (1 - slip) * (1 - fee)
            trades += 1

    for i in range(len(c)):
        if day[i] != cur_day:
            eq_days.append(cur_day)
            eq_vals.append(cash + sum(lots) * c[i - 1])
            cur_day = day[i]
        if reg is not None:
            if reg[i] and not active:                  # режим увімкнувся — нова сітка від поточної ціни
                active, p0, k = True, o[i], 0
                lot_usdt = cash / p.levels
            elif not reg[i] and active:                # режим вимкнувся — все в USDT
                sell_all(o[i])
                active = False
            if not active:
                continue
        # 1) продажі лотів, куплених раніше
        while lots and h[i] >= level(k + 1):
            q = lots.popleft()
            cash += q * level(k + 1) * (1 - slip) * (1 - fee)
            k += 1
            trades += 1
        # 2) порожня сітка й ціна вище опори — переякорювання вгору
        if not lots and c[i] > level(1):
            p0, k = c[i], 0
            lot_usdt = cash / p.levels
            continue
        # 3) купівлі на рівнях нижче
        while k > -p.levels and lo[i] <= level(k - 1) and cash >= lot_usdt * 0.999:
            px = level(k - 1) * (1 + slip)
            spend = min(lot_usdt, cash)
            lots.appendleft(spend * (1 - fee) / px)
            cash -= spend
            k -= 1
            trades += 1
        # 4) стоп нижче дна сітки
        if p.mode == "stop" and lots and c[i] < level(-p.levels - 1):
            sell_all(c[i])
            p0, k = c[i], 0
            lot_usdt = cash / p.levels
    eq_days.append(cur_day)
    eq_vals.append(cash + sum(lots) * c[-1])
    eq = np.array(eq_vals)
    st = stats(np.array(eq_days), eq)
    st.update(trades=trades, trades_per_day=trades / max(len(eq), 1), end_equity=float(eq[-1]))
    return st


VARIANTS = [(s, n, m) for m in ("plain", "stop", "regime") for s in (0.5, 1.0, 2.0, 3.0) for n in (10, 20)]


def run_all(df: pd.DataFrame, fee_pct: float = 0.1, log=print) -> dict:
    out = {}
    for s, n, m in VARIANTS:
        r = run_grid(df, GridParams(step_pct=s, levels=n, mode=m, fee_pct=fee_pct))
        out[f"{m} · крок {s:g} % · {n} рівнів"] = r
        log(f"{m} s={s} n={n}: CAGR {r['cagr_pct']:.1f} % DD {r['max_dd_pct']:.1f} % угод/день {r['trades_per_day']:.1f}")
    bh = buy_and_hold(df)
    out["просто тримати ETH"] = stats(bh.days, bh.equity) | {"trades": 1, "trades_per_day": 0.0}
    return out


def report_md(r: dict, start: str, end: str, fee_pct: float) -> str:
    f = lambda x: f"{x:.1f}"
    L = [f"## Сітковий бот · {start} … {end}", "",
         f"Капітал 500 USDT, реінвестування, комісія {fee_pct:g} % за угоду, прослизання 0,02 %.", "",
         "plain — сітка йде за ціною вгору; stop — продаж усього нижче дна сітки; regime — лише коли ETH вище EMA200 днів.", "",
         "| Варіант | Річна, % | Макс. просадка, % | Sharpe | Calmar | Угод | Угод/день |", "|---|---|---|---|---|---|---|"]
    for k, v in r.items():
        L.append(f"| {k} | {f(v['cagr_pct'])} | {f(v['max_dd_pct'])} | {v['sharpe']:.2f} | {v['calmar']:.2f} | "
                 f"{v['trades']} | {v['trades_per_day']:.1f} |")
    L += ["", "Для порівняння (той самий період, docs/backtest_log.md): збалансований 21,8 % / DD 50 %, "
              "лише_режим 39,3 % / DD 65,9 %, обережний 2,5 % / DD 7,7 %.", ""]
    return "\n".join(L)
