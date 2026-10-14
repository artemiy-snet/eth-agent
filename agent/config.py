"""Конфіг (pydantic). Структура — Біблія, розділ 10.1; секція legacy — параметри v2.02."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from agent.strategy.legacy_v2 import LegacyParams, PointParams


class AgentSection(BaseModel):
    pair: str = "ETHUSDT"
    mode: Literal["backtest", "paper", "shadow", "live"] = "backtest"
    params_version: str = "v1"
    data_dir: Path = Path("data")


class PointCfg(BaseModel):
    limit_up: Decimal
    limit_bot: Decimal
    action_up: Decimal
    action_bot: Decimal


class LegacyCfg(BaseModel):
    """Поля відповідають панелі v2.02 (у дужках — назва поля в C#)."""

    base_price: Decimal | None = None            # arrayBasePrice; None = відцентрувати сітку на першій ціні
    bots: int = Field(ge=1, le=50)               # arrayBotCount
    interval_pct: Decimal                        # arrayBotIntervalP
    budget_usdt_per_bot: Decimal                 # botStartUSDT
    step_sell_pct: Decimal                       # stepOfSellPoints
    buy: PointCfg
    sell: PointCfg
    greedy_buy: int = 0
    greedy_sell: int = 0
    sell_on_any_level: bool = False
    fee_pct: Decimal = Decimal("0.1")

    def resolve_base(self, first_price: Decimal) -> Decimal:
        if self.base_price is not None:
            return self.base_price
        # нижній бот на (bots−1)/2 інтервалів нижче ціни — сітка симетрична навколо старту
        half = Decimal(self.bots - 1) / 2 * self.interval_pct
        return (first_price / (1 + half / 100)).quantize(Decimal("0.01"))

    def to_params(self, first_price: Decimal, qty_step: Decimal = Decimal("0.0001")) -> LegacyParams:
        return LegacyParams(
            base_price=self.resolve_base(first_price), bots=self.bots, interval_pct=self.interval_pct,
            budget_usdt=self.budget_usdt_per_bot, step_sell_pct=self.step_sell_pct,
            buy=PointParams(**self.buy.model_dump()), sell=PointParams(**self.sell.model_dump()),
            greedy_buy=self.greedy_buy, greedy_sell=self.greedy_sell,
            sell_on_any_level=self.sell_on_any_level, fee_pct=self.fee_pct, qty_step=qty_step,
        )


class V3FiltersCfg(BaseModel):
    trend_block_pct: Decimal | None = Decimal("-3")
    atr_min_pct: Decimal | None = Decimal("0.3")
    atr_max_pct: Decimal | None = Decimal("4.0")


class V3Cfg(BaseModel):
    bots: int = Field(5, ge=1, le=50)
    interval_atr: Decimal = Decimal("1.0")
    budget_usdt_per_bot: Decimal = Decimal("100")
    buy: PointCfg = PointCfg(limit_up=Decimal("1.0"), limit_bot=Decimal("-1.0"),
                             action_up=Decimal("0.4"), action_bot=Decimal("0.1"))
    sell: PointCfg = PointCfg(limit_up=Decimal("1.0"), limit_bot=Decimal("-1.0"),
                              action_up=Decimal("0.1"), action_bot=Decimal("-0.4"))
    min_edge_pct: Decimal = Decimal("0.15")
    fee_pct: Decimal = Decimal("0.1")
    k_stop_atr: Decimal = Decimal("3")
    max_hold_h: int = 72
    risk_per_trade_pct: Decimal = Decimal("0.5")
    filters: V3FiltersCfg = V3FiltersCfg()

    def to_params(self, qty_step: Decimal = Decimal("0.0001"), min_notional: Decimal = Decimal("5")):
        from agent.strategy.v3 import AtrPoint, V3Params

        return V3Params(
            bots=self.bots, interval_atr=self.interval_atr, budget_usdt=self.budget_usdt_per_bot,
            buy=AtrPoint(**self.buy.model_dump()), sell=AtrPoint(**self.sell.model_dump()),
            min_edge_pct=self.min_edge_pct, fee_pct=self.fee_pct, k_stop_atr=self.k_stop_atr,
            max_hold_h=self.max_hold_h, risk_per_trade_pct=self.risk_per_trade_pct,
            qty_step=qty_step, min_notional=min_notional, **self.filters.model_dump(),
        )


class FeaturesCfg(BaseModel):
    atr_tf: Literal["5m", "15m", "30m", "1h", "4h"] = "1h"
    atr_len: int = 14
    ema_len: int = 200
    ema_slope_h: int = 24
    anchor_h: int = 24


class RiskCfg(BaseModel):
    max_eth_share_pct: Decimal = Decimal("60")
    daily_loss_pct: Decimal = Decimal("2")
    max_dd_pct: Decimal = Decimal("10")
    max_consecutive_losses: int = 5
    losses_pause_h: int = 4
    max_orders_per_hour: int = 20
    max_move_1m_pct: Decimal = Decimal("3")
    move_pause_min: int = 15
    max_spread_pct: Decimal = Decimal("0.1")
    max_open_risk_pct: Decimal = Decimal("2")


class BacktestCfg(BaseModel):
    slippage_pct: Decimal = Decimal("0.05")


class Config(BaseModel):
    agent: AgentSection = AgentSection()
    legacy: LegacyCfg | None = None
    v3: V3Cfg = V3Cfg()
    features: FeaturesCfg = FeaturesCfg()
    risk: RiskCfg = RiskCfg()
    backtest: BacktestCfg = BacktestCfg()


def apply_overrides(raw: dict, overrides: list[str]) -> dict:
    """['legacy.bots=7', 'legacy.buy.limit_bot=-0.5'] → змінює вкладені ключі (значення парсяться як YAML)."""
    for item in overrides:
        key, _, value = item.partition("=")
        if not key or not _:
            raise ValueError(f"очікую ключ=значення, отримав {item!r}")
        node = raw
        *path, last = key.strip().split(".")
        for k in path:
            node = node.setdefault(k, {})
        node[last] = yaml.safe_load(value)
    return raw


def load_config(path: Path, overrides: list[str] | None = None) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(apply_overrides(raw, overrides or []))
