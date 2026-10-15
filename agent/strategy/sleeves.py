"""Рукави (стратегії з параметрами) і пресети портфелів — спільні для бектесту й живого агента.
Без залежностей, щоб працювало в Termux. Фіксовані гіпотези з docs/strategies.md."""

SLEEVES: dict[str, dict] = {
    "A": {"strategy": "v3", "label": "Денний відскок (поточний)", "set": []},
    "B": {"strategy": "v3", "label": "Відскок розширений",
          "set": ["v3.filters.atr_max_pct=15", "v3.max_hold_h=168"]},
    "B1": {"strategy": "v3", "label": "Відскок розширений, ризик 1 %",
           "set": ["v3.filters.atr_max_pct=15", "v3.max_hold_h=168", "v3.risk_per_trade_pct=1",
                   "risk.max_open_risk_pct=5"]},
    "C": {"strategy": "trend", "label": "Режимне утримання (100 % рукава, EMA200d)",
          "set": ["trend.alloc_pct=100", "trend.breakout=false", "trend.k_stop_atr=0", "trend.k_trail_atr=0",
                  "risk.max_dd_pct=100", "risk.daily_loss_pct=100", "risk.max_open_risk_pct=100",
                  "risk.max_eth_share_pct=100"]},
}

PRESETS: dict[str, dict[str, float]] = {
    "поточний": {"A": 1.0},
    "обережний": {"B": 1.0},
    "збалансований": {"B1": 0.7, "C": 0.3},
    "агресивний": {"B1": 0.5, "C": 0.5},
    "лише_режим": {"C": 1.0},
}


def apply_overrides(raw: dict, overrides: list[str]) -> dict:
    """['v3.max_hold_h=168', ...] → змінює вкладені ключі (значення парсяться як YAML)."""
    import yaml

    for item in overrides:
        key, sep, value = item.partition("=")
        if not key or not sep:
            raise ValueError(f"очікую ключ=значення, отримав {item!r}")
        node = raw
        *path, last = key.strip().split(".")
        for k in path:
            node = node.setdefault(k, {})
        node[last] = yaml.safe_load(value)
    return raw
