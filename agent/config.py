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


class Config(BaseModel):
    agent: AgentSection = AgentSection()
    legacy: LegacyCfg | None = None


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
