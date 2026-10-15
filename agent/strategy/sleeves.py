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

# Макроподії (FOMC + CPI, agent/features/calendar.py): гіпотеза «не тримати ризик на подію».
EV = ["v3.filters.event_before_h=24", "v3.filters.event_after_h=2"]
EV_C = ["trend.bots=2", "trend.budget_usdt=250", "trend.event_bots=1", "trend.event_before_h=24", "trend.event_after_h=2"]
for _k in ("A", "B", "B1"):
    SLEEVES[f"{_k}_ев"] = {**SLEEVES[_k], "label": SLEEVES[_k]["label"] + " + без входів ±подія",
                           "set": SLEEVES[_k]["set"] + EV}
SLEEVES["B_ев48"] = {**SLEEVES["B"], "label": "Відскок розширений + без входів 48 год до події",
                     "set": SLEEVES["B"]["set"] + ["v3.filters.event_before_h=48", "v3.filters.event_after_h=2"]}
SLEEVES["C_ев"] = {**SLEEVES["C"], "label": "Режимне утримання, половина в USDT на подію",
                   "set": SLEEVES["C"]["set"] + EV_C}
SLEEVES["C_ев_fomc"] = {**SLEEVES["C"], "label": "Режимне утримання, половина в USDT лише на FOMC",
                        "set": SLEEVES["C"]["set"] + EV_C + ["features.event_kinds=fomc"]}
SLEEVES["C_ев_все"] = {**SLEEVES["C"], "label": "Режимне утримання, увесь рукав у USDT на подію",
                       "set": SLEEVES["C"]["set"] + ["trend.bots=1", "trend.event_bots=1",
                                                     "trend.event_before_h=24", "trend.event_after_h=2"]}

PRESETS: dict[str, dict[str, float]] = {
    "поточний": {"A": 1.0},
    "обережний": {"B": 1.0},
    "збалансований": {"B1": 0.7, "C": 0.3},
    "агресивний": {"B1": 0.5, "C": 0.5},
    "лише_режим": {"C": 1.0},
    # макроподії — для порівняння з базовими
    "поточний_ев": {"A_ев": 1.0},
    "обережний_ев": {"B_ев": 1.0},
    "обережний_ев48": {"B_ев48": 1.0},
    "збалансований_ев": {"B1_ев": 0.7, "C_ев": 0.3},
    "агресивний_ев": {"B1_ев": 0.5, "C_ев": 0.5},
    "лише_режим_ев": {"C_ев": 1.0},
    "лише_режим_ев_fomc": {"C_ев_fomc": 1.0},
    "лише_режим_ев_все": {"C_ев_все": 1.0},
}

BASE_PRESETS = ["поточний", "обережний", "збалансований", "агресивний", "лише_режим"]
GROUPS = {"all": list(PRESETS), "base": BASE_PRESETS, "macro": list(PRESETS)}


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
