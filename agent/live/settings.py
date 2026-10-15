"""Конфіг для живого агента без pydantic. Ті самі секції й типові значення, що й agent/config.py
(тест test_live_settings_match_research_config стежить, щоб вони не розійшлись)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path

import yaml

from agent.features.params import FeatureParams
from agent.money import D
from agent.risk.guard import RiskParams
from agent.strategy.trend import TrendParams
from agent.strategy.v3 import AtrPoint, V3Params

V3_DEFAULTS = {
    "bots": 5, "interval_atr": "1.0", "budget_usdt_per_bot": "100",
    "buy": {"limit_up": "1.0", "limit_bot": "-1.0", "action_up": "0.4", "action_bot": "0.1"},
    "sell": {"limit_up": "2.0", "limit_bot": "-1.0", "action_up": "0.5", "action_bot": "-0.4"},
    "min_edge_pct": "0.15", "fee_pct": "0.1", "k_stop_atr": "2", "max_hold_h": 72,
    "risk_per_trade_pct": "0.5",
    "filters": {"trend_block_pct": "-3", "atr_min_pct": "0.3", "atr_max_pct": "4.0", "regime_min_pct": None,
                "event_before_h": None, "event_after_h": None},
}
FEATURE_DEFAULTS = {"atr_tf": "1d", "atr_len": 14, "ema_len": 200, "ema_slope_h": 24, "anchor_h": 24,
                    "regime_ema_days": 200, "event_kinds": "fomc,cpi"}


TREND_DEFAULTS = {"bots": 1, "budget_usdt": "500", "breakout_days": 20, "k_stop_atr": "3", "k_trail_atr": "3",
                  "risk_per_trade_pct": "0.5", "regime_min_pct": "0", "regime_exit": True, "alloc_pct": None,
                  "breakout": True, "fee_pct": "0.1",
                  "event_bots": 0, "event_before_h": None, "event_after_h": None}


AI_DEFAULTS = {"risk_per_trade_pct": "3", "min_stop_pct": "1", "max_stop_pct": "12", "max_drift_pct": "1",
               "max_hold_h": 168, "fee_pct": "0.1"}


def ai_params(raw: dict | None):
    from agent.strategy.ai_trader import AiParams
    c = _merge(AI_DEFAULTS, raw)
    return AiParams(risk_per_trade_pct=D(c["risk_per_trade_pct"]), min_stop_pct=D(c["min_stop_pct"]),
                    max_stop_pct=D(c["max_stop_pct"]), max_drift_pct=D(c["max_drift_pct"]),
                    max_hold_h=int(c["max_hold_h"]), fee_pct=D(c["fee_pct"]))


@dataclass(frozen=True)
class Sleeve:
    """Рукав портфеля в живому агенті."""

    name: str
    strategy: str          # v3 | trend
    params: object         # V3Params | TrendParams
    risk: RiskParams
    features: FeatureParams


@dataclass(frozen=True)
class LiveSettings:
    pair: str
    mode: str
    v3: V3Params
    risk: RiskParams
    features: FeatureParams
    poll_s: int = 10
    features_every_s: int = 60
    reconcile_every_s: int = 300
    stop_limit_buffer_pct: Decimal = D("1.0")   # ліміт стоп-ордера нижче стоп-ціни на 1 %
    status_port: int = 8765
    sleeves: tuple = ()          # порожньо — одна стратегія v3 з секції v3
    capital_usdt: Decimal = D("500")
    live_confirmed: bool = False                # live.confirmed — підтвердження Командира з панелі
    live_max_capital: Decimal = D("20")         # жорстка межа капіталу агента в live
    analyst: dict = field(default_factory=dict)   # секція analyst (ШІ-аналітик новин, тіньовий режим)


def _merge(base: dict, over: dict | None) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _dec_or_none(x):
    return None if x is None else D(x)


def _int_or_none(x):
    return None if x is None else int(x)


def v3_params(raw: dict, qty_step: Decimal = D("0.0001"), min_notional: Decimal = D("5")) -> V3Params:
    c = _merge(V3_DEFAULTS, raw)
    pt = lambda d: AtrPoint(D(d["limit_up"]), D(d["limit_bot"]), D(d["action_up"]), D(d["action_bot"]))
    f = c["filters"]
    return V3Params(
        bots=int(c["bots"]), interval_atr=D(c["interval_atr"]), budget_usdt=D(c["budget_usdt_per_bot"]),
        buy=pt(c["buy"]), sell=pt(c["sell"]), min_edge_pct=D(c["min_edge_pct"]), fee_pct=D(c["fee_pct"]),
        k_stop_atr=D(c["k_stop_atr"]), max_hold_h=int(c["max_hold_h"]),
        risk_per_trade_pct=D(c["risk_per_trade_pct"]), qty_step=qty_step, min_notional=min_notional,
        trend_block_pct=_dec_or_none(f.get("trend_block_pct")), atr_min_pct=_dec_or_none(f.get("atr_min_pct")),
        atr_max_pct=_dec_or_none(f.get("atr_max_pct")), regime_min_pct=_dec_or_none(f.get("regime_min_pct")),
        event_before_h=_int_or_none(f.get("event_before_h")), event_after_h=_int_or_none(f.get("event_after_h")),
    )


def trend_params(raw: dict | None) -> TrendParams:
    c = _merge(TREND_DEFAULTS, raw)
    return TrendParams(bots=int(c["bots"]), budget_usdt=D(c["budget_usdt"]), k_stop_atr=D(c["k_stop_atr"]),
                       k_trail_atr=D(c["k_trail_atr"]), risk_per_trade_pct=D(c["risk_per_trade_pct"]),
                       regime_min_pct=_dec_or_none(c["regime_min_pct"]), regime_exit=bool(c["regime_exit"]),
                       alloc_pct=_dec_or_none(c["alloc_pct"]), breakout=bool(c["breakout"]), fee_pct=D(c["fee_pct"]),
                       event_bots=int(c["event_bots"]), event_before_h=_int_or_none(c["event_before_h"]),
                       event_after_h=_int_or_none(c["event_after_h"]))


def build_sleeves(raw: dict) -> tuple[tuple, Decimal]:
    """Секція portfolio: {capital_usdt: 500, preset: збалансований} або {sleeves: {B1: 0.7, C: 0.3}}."""
    import copy
    import dataclasses

    from agent.strategy.sleeves import PRESETS, SLEEVES, apply_overrides

    pf = raw.get("portfolio") or {}
    if not pf:
        return (), D(500)
    capital = D(pf.get("capital_usdt", 500))
    weights = pf.get("sleeves") or PRESETS[pf.get("preset", "обережний")]
    out = []
    for name, w in weights.items():
        sl = SLEEVES[name]
        r = apply_overrides(copy.deepcopy(raw), list(sl["set"]))
        share = capital * D(w)
        fp = feature_params(r.get("features"))
        if sl["strategy"] == "ai":
            ap = dataclasses.replace(ai_params(r.get("ai_trader")), budget_usdt=share)
            out.append(Sleeve(name, "ai", ap, risk_params(r.get("risk")), fp))
        elif sl["strategy"] == "trend":
            tp = trend_params(r.get("trend"))
            tp = dataclasses.replace(tp, budget_usdt=share / tp.bots)
            # якір (максимум за N днів) потрібен лише для пробою; без пробою — 24 год (менше запитів 1m-свічок)
            fp = dataclasses.replace(fp, atr_tf="1d",
                                     anchor_h=int(_merge(TREND_DEFAULTS, r.get("trend"))["breakout_days"]) * 24 if tp.breakout else 24)
            out.append(Sleeve(name, "trend", tp, risk_params(r.get("risk")), fp))
        else:
            vp = v3_params(r.get("v3"))
            vp = dataclasses.replace(vp, budget_usdt=share / vp.bots)
            out.append(Sleeve(name, "v3", vp, risk_params(r.get("risk")), fp))
    return tuple(out), capital


def risk_params(raw: dict | None) -> RiskParams:
    kw = {}
    for fld in fields(RiskParams):
        if raw and fld.name in raw:
            v = raw[fld.name]
            kw[fld.name] = int(v) if fld.type in ("int", int) else D(v)
    return RiskParams(**kw)


def feature_params(raw: dict | None) -> FeatureParams:
    c = _merge(FEATURE_DEFAULTS, raw)
    return FeatureParams(atr_tf=str(c["atr_tf"]), atr_len=int(c["atr_len"]), ema_len=int(c["ema_len"]),
                         ema_slope_h=int(c["ema_slope_h"]), anchor_h=int(c["anchor_h"]),
                         regime_ema_days=int(c["regime_ema_days"]), event_kinds=str(c["event_kinds"] or ""))


def load_raw(path: Path, home: Path | None = None) -> dict:
    """Конфіг + overrides.yaml з робочої теки (зміни з панелі; не губляться при оновленні коду)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    ov = (home / "overrides.yaml") if home else None
    if ov and ov.exists():
        raw = _merge(raw, yaml.safe_load(ov.read_text(encoding="utf-8")) or {})
    return raw


def live_mode(cli: str | None, cfg: str | None) -> str:
    """Режим живого агента: аргумент запуску → налаштування (панель/конфіг) → paper.
    agent.mode у конфігу бектесту = backtest — для живого агента це paper."""
    if cli:
        return cli
    return cfg if cfg in ("paper", "testnet", "live") else "paper"


def apply_live_cap(raw: dict, mode: str) -> dict:
    """У live агент бачить лише live.max_capital_usdt (за замовчуванням 20 $). Склад — live.preset, якщо задано,
    інакше той самий портфель, що в paper/testnet (повна версія)."""
    if mode != "live":
        return raw
    import copy
    raw = copy.deepcopy(raw)
    live = raw.get("live") or {}
    cap = D(str(live.get("max_capital_usdt", 20)))
    pf = raw.setdefault("portfolio", {})
    pf["capital_usdt"] = str(min(D(str(pf.get("capital_usdt", cap))), cap))
    if live.get("preset") and live["preset"] != "як_у_портфелі":
        pf.pop("sleeves", None)
        pf["preset"] = live["preset"]
    return raw


def load(path: Path, mode: str | None = None, home: Path | None = None) -> LiveSettings:
    raw = load_raw(path, home)
    agent = raw.get("agent", {})
    live = raw.get("live", {})
    raw = apply_live_cap(raw, live_mode(mode, agent.get("mode")))
    return LiveSettings(
        pair=agent.get("pair", "ETHUSDT"),
        mode=live_mode(mode, agent.get("mode")),
        v3=v3_params(raw.get("v3")),
        risk=risk_params(raw.get("risk")),
        features=feature_params(raw.get("features")),
        poll_s=int(live.get("poll_s", 10)),
        features_every_s=int(live.get("features_every_s", 60)),
        reconcile_every_s=int(live.get("reconcile_every_s", 300)),
        stop_limit_buffer_pct=D(live.get("stop_limit_buffer_pct", "1.0")),
        status_port=int(live.get("status_port", 8765)),
        sleeves=build_sleeves(raw)[0],
        capital_usdt=build_sleeves(raw)[1],
        analyst=dict(raw.get("analyst") or {}),
        live_confirmed=bool(live.get("confirmed", False)),
        live_max_capital=D(str(live.get("max_capital_usdt", 20))),
    )
