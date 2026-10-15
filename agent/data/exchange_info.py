"""Фільтри символу з /api/v3/exchangeInfo: tickSize, stepSize, minQty, minNotional."""

from __future__ import annotations

import dataclasses

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from agent.money import ZERO, D, floor_to_step

# data-api.binance.vision — публічні ринкові дані Binance, доступні й там, де api.binance.com блокується
EXCHANGE_INFO_URL = "https://data-api.binance.vision/api/v3/exchangeInfo"


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def round_price(self, price: Decimal) -> Decimal:
        return floor_to_step(price, self.tick_size)

    def round_qty(self, qty: Decimal) -> Decimal:
        return floor_to_step(qty, self.step_size)

    def check(self, qty: Decimal, price: Decimal) -> str | None:
        """None, якщо ордер проходить фільтри; інакше причина відмови."""
        if qty < self.min_qty:
            return f"qty {qty} < minQty {self.min_qty}"
        if floor_to_step(qty, self.step_size) != qty:
            return f"qty {qty} не кратна stepSize {self.step_size}"
        if qty * price < self.min_notional:
            return f"notional {qty * price} < minNotional {self.min_notional}"
        return None

    @classmethod
    def from_exchange_info(cls, payload: dict, symbol: str) -> "SymbolFilters":
        sym = next(s for s in payload["symbols"] if s["symbol"] == symbol)
        f = {x["filterType"]: x for x in sym["filters"]}
        notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        return cls(
            symbol=symbol,
            tick_size=D(f["PRICE_FILTER"]["tickSize"]).normalize(),
            step_size=D(f["LOT_SIZE"]["stepSize"]).normalize(),
            min_qty=D(f["LOT_SIZE"]["minQty"]).normalize(),
            min_notional=D(notional.get("minNotional", "0")).normalize(),
        )


# Значення ETHUSDT на момент написання — лише для тестів і офлайн-бектесту.
# Перед live агент ЗАВЖДИ бере свіжі фільтри з біржі.
ETHUSDT_DEFAULT = SymbolFilters(
    symbol="ETHUSDT",
    tick_size=D("0.01"),
    step_size=D("0.0001"),
    min_qty=D("0.0001"),
    min_notional=D("5"),
)


def fetch_filters(symbol: str, cache_dir: Path | None = None) -> SymbolFilters:
    import httpx

    r = httpx.get(EXCHANGE_INFO_URL, params={"symbol": symbol}, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"exchangeInfo_{symbol}.json").write_text(json.dumps(payload))
    return SymbolFilters.from_exchange_info(payload, symbol)


def load_cached_filters(symbol: str, cache_dir: Path) -> SymbolFilters | None:
    p = cache_dir / f"exchangeInfo_{symbol}.json"
    if not p.exists():
        return None
    return SymbolFilters.from_exchange_info(json.loads(p.read_text()), symbol)


def default_filters(symbol: str) -> SymbolFilters:
    """Типові фільтри для бектесту без мережі (крок кількості відрізняється між парами)."""
    step = {"BTCUSDT": "0.00001", "ETHUSDT": "0.0001", "BNBUSDT": "0.001", "SOLUSDT": "0.001"}.get(symbol.upper(), "0.0001")
    return dataclasses.replace(ETHUSDT_DEFAULT, symbol=symbol.upper(), step_size=D(step))


__all__ = ["SymbolFilters", "ETHUSDT_DEFAULT", "default_filters", "fetch_filters", "load_cached_filters", "ZERO"]
