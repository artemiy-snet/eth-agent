"""Дані й налаштування для панелі (agent/live/web/index.html). Лише стандартна бібліотека + pyyaml.

Безпека: сервер слухає тільки 127.0.0.1; кожен POST вимагає заголовок X-ETH-Agent і JSON
(сторонній сайт у браузері телефона не може надіслати такий запит без CORS, а CORS сервер не дає);
Host має бути localhost/127.0.0.1 (захист від DNS rebinding). Ключі ніколи не віддаються назад —
лише останні 4 символи. Режим live з панелі увімкнути неможливо.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import yaml

from agent.features import calendar
from agent.strategy.sleeves import BASE_PRESETS, PRESETS, SLEEVES

SECRET_KEYS = ["BINANCE_API_KEY", "BINANCE_API_SECRET", "ANTHROPIC_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
LIVE_PRESETS = BASE_PRESETS + [k for k in PRESETS if "+ші" in k]

# Очікування за бектестом 7 років (docs/strategies.md) — для підказок у виборі пресета.
EXPECT = {"поточний": (1.1, 2.1), "обережний": (2.5, 7.7), "збалансований": (21.8, 50.0), "агресивний": (28.6, 56.2),
          "лише_режим": (39.3, 65.9)}


def _f(key, typ, sec, lo=None, hi=None, opts=None, step=None, uk="", en="", help_uk="", help_en="", warn_above=None):
    return {"key": key, "type": typ, "section": sec, "min": lo, "max": hi, "options": opts, "step": step,
            "label": {"uk": uk, "en": en}, "help": {"uk": help_uk, "en": help_en}, "warn_above": warn_above}


SCHEMA = [
    _f("agent.mode", "select", "general", opts=["paper", "testnet"], uk="Режим", en="Mode",
       help_uk="paper — справжні ціни, віртуальні угоди; testnet — справжні ордери на тестових грошах. Live з панелі вимкнено.",
       help_en="paper — real prices, virtual trades; testnet — real orders with test money. Live is disabled here."),
    _f("portfolio.capital_usdt", "number", "portfolio", 10, 1_000_000, step=1, uk="Капітал, USDT", en="Capital, USDT"),
    _f("portfolio.preset", "select", "portfolio", opts=LIVE_PRESETS, uk="Портфель", en="Portfolio",
       help_uk="Склад стратегій. Більший дохід = більша просадка (див. підказку).",
       help_en="Strategy mix. Higher return = deeper drawdown (see hint)."),
    _f("analyst.enabled", "bool", "ai", uk="ШІ-аналітик увімкнено", en="AI analyst enabled"),
    _f("analyst.mode", "select", "ai", opts=["auto", "shadow"], uk="Вплив ШІ", en="AI influence",
       help_uk="auto — сам гальмує нові входи (лише зменшує ризик); shadow — лише записує.",
       help_en="auto — brakes new entries by itself (risk-down only); shadow — log only."),
    _f("analyst.model", "select", "ai", opts=["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
       uk="Модель", en="Model", help_uk="Sonnet ≈ $2–3/міс, Haiku ≈ $0,2/міс, Opus ≈ $5/міс",
       help_en="Sonnet ≈ $2–3/mo, Haiku ≈ $0.2/mo, Opus ≈ $5/mo"),
    _f("analyst.every_h", "number", "ai", 2, 48, step=1, uk="Оцінка раз на, год", en="Assess every, h"),
    _f("analyst.trigger_move_pct", "number", "ai", 2, 20, step=0.5, uk="Позачергово при русі ≥, % за 4 год",
       en="Extra run on move ≥, % in 4 h"),
    _f("analyst.min_gap_h", "number", "ai", 1, 24, step=1, uk="Не частіше ніж раз на, год", en="Not more often than, h"),
    _f("analyst.max_action_h", "number", "ai", 1, 48, step=1, uk="Гальмування діє до, год", en="Brake lasts up to, h"),
    _f("analyst.trade", "bool", "ai_trader", uk="ШІ-трейдер пропонує угоди (рукав D)", en="AI trader proposes trades (sleeve D)"),
    _f("analyst.min_confidence", "number", "ai_trader", 0.5, 0.95, step=0.05, uk="Мін. впевненість ШІ",
       en="Min AI confidence"),
    _f("ai_trader.risk_per_trade_pct", "number", "ai_trader", 0.5, 5, step=0.5, uk="Ризик до стопу, % рукава D",
       en="Risk to stop, % of sleeve D", warn_above=3),
    _f("ai_trader.max_stop_pct", "number", "ai_trader", 3, 15, step=0.5, uk="Макс. стоп, %", en="Max stop, %"),
    _f("ai_trader.max_hold_h", "number", "ai_trader", 24, 336, step=12, uk="Макс. утримання, год", en="Max hold, h"),
    _f("v3.risk_per_trade_pct", "number", "strategy", 0.1, 2, step=0.05, uk="Ризик на угоду (A/B), %",
       en="Risk per trade (A/B), %", warn_above=0.5,
       help_uk="Біблія: > 0,5 % — рішення Командира. Рукав B1 має власні 1 %.",
       help_en="Bible: > 0.5 % is the Commander's decision. Sleeve B1 uses its own 1 %."),
    _f("v3.k_stop_atr", "number", "strategy", 1, 4, step=0.5, uk="Стоп, × ATR(1d)", en="Stop, × ATR(1d)"),
    _f("v3.max_hold_h", "number", "strategy", 24, 336, step=12, uk="Макс. утримання (A), год", en="Max hold (A), h"),
    _f("v3.filters.atr_max_pct", "number", "strategy", 2, 20, step=0.5, uk="Фільтр ATR ≤, % (A)",
       en="ATR filter ≤, % (A)"),
    _f("risk.max_dd_pct", "number", "risk", 5, 50, step=1, uk="Kill switch: просадка, %", en="Kill switch: drawdown, %",
       warn_above=20),
    _f("risk.daily_loss_pct", "number", "risk", 1, 20, step=0.5, uk="Денний ліміт збитку, %", en="Daily loss limit, %"),
    _f("risk.max_open_risk_pct", "number", "risk", 1, 100, step=1, uk="Сумарний ризик до стопів, %",
       en="Total open risk, %"),
    _f("risk.max_eth_share_pct", "number", "risk", 10, 100, step=5, uk="Макс. частка в ETH (A/B), %",
       en="Max ETH share (A/B), %"),
]
SCHEMA_BY_KEY = {f["key"]: f for f in SCHEMA}


def _get(raw: dict, key: str):
    node = raw
    for k in key.split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _set(raw: dict, key: str, value) -> None:
    *path, last = key.split(".")
    node = raw
    for k in path:
        node = node.setdefault(k, {})
    node[last] = value


def mask(v: str) -> str:
    return ("…" + v[-4:]) if v else ""


def read_secrets_file(home: Path) -> dict[str, str]:
    out = {}
    p = home / "secrets.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_secrets(home: Path, updates: dict[str, str]) -> None:
    p = home / "secrets.env"
    lines = p.read_text().splitlines() if p.exists() else ["# Ключі ТІЛЬКИ testnet / API. Файл не потрапляє в git."]
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in updates and not line.strip().startswith("#"):
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def settings_view(config_path: Path, home: Path) -> dict:
    from agent.live.settings import load_raw
    raw = load_raw(config_path, home)
    sec = read_secrets_file(home)
    presets = {k: {"weights": PRESETS[k], "labels": {s: SLEEVES[s]["label"] for s in PRESETS[k]},
                   "expect": EXPECT.get(k)} for k in LIVE_PRESETS}
    return {"schema": SCHEMA, "values": {f["key"]: _get(raw, f["key"]) for f in SCHEMA},
            "secrets": {k: mask(sec.get(k, "")) for k in SECRET_KEYS}, "presets": presets,
            "config_path": str(config_path), "overrides": str(home / "overrides.yaml")}


def validate_values(values: dict) -> tuple[dict, list[str]]:
    clean, errors = {}, []
    for key, v in (values or {}).items():
        f = SCHEMA_BY_KEY.get(key)
        if f is None:
            errors.append(f"{key}: невідоме поле")
            continue
        if f["type"] == "bool":
            if not isinstance(v, bool):
                errors.append(f"{key}: очікую так/ні")
                continue
        elif f["type"] == "select":
            if v not in f["options"]:
                errors.append(f"{key}: дозволено {f['options']}")
                continue
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                errors.append(f"{key}: не число")
                continue
            if not f["min"] <= v <= f["max"]:
                errors.append(f"{key}: поза межами [{f['min']}; {f['max']}]")
                continue
            if v == int(v) and (f["step"] or 1) >= 1:
                v = int(v)
        clean[key] = v
    return clean, errors


def save_settings(home: Path, body: dict) -> dict:
    clean, errors = validate_values(body.get("values") or {})
    secrets = {k: str(v).strip() for k, v in (body.get("secrets") or {}).items() if k in SECRET_KEYS and str(v).strip()}
    bad = [k for k, v in secrets.items() if any(c in v for c in "\n\r= ")]
    errors += [f"{k}: недопустимі символи" for k in bad]
    if clean.get("agent.mode") == "testnet":
        sec = read_secrets_file(home) | secrets
        if not (sec.get("BINANCE_API_KEY") and sec.get("BINANCE_API_SECRET")):
            errors.append("testnet: спершу додай BINANCE_API_KEY і BINANCE_API_SECRET (testnet.binance.vision)")
    if errors:
        return {"ok": False, "errors": errors}
    ov_path = home / "overrides.yaml"
    ov = (yaml.safe_load(ov_path.read_text(encoding="utf-8")) or {}) if ov_path.exists() else {}
    for k, v in clean.items():
        _set(ov, k, v)
    tmp = ov_path.with_suffix(".tmp")
    tmp.write_text("# Зміни з панелі. Діють поверх конфігу після перезапуску.\n"
                   + yaml.safe_dump(ov, allow_unicode=True, sort_keys=True), encoding="utf-8")
    tmp.replace(ov_path)
    if secrets:
        write_secrets(home, secrets)
    return {"ok": True, "saved": sorted(clean), "secrets_saved": sorted(secrets), "restart_required": True}


# --- перевірки ключів ------------------------------------------------------------------------
def check(kind: str, home: Path, mode: str, given: dict | None = None) -> dict:
    """Перевіряє ключі з поля (ще не збережені) або, якщо поле порожнє, — збережені."""
    sec = read_secrets_file(home)
    for k, v in (given or {}).items():
        if k in SECRET_KEYS and str(v).strip():
            sec[k] = str(v).strip()
    try:
        if kind == "binance":
            from agent.live.binance import BASES, Client
            if not (sec.get("BINANCE_API_KEY") and sec.get("BINANCE_API_SECRET")):
                return {"ok": False, "msg": "ключі не задано"}
            c = Client(BASES["testnet"], sec["BINANCE_API_KEY"], sec["BINANCE_API_SECRET"])
            c.sync_time()
            acc = c.account()
            bal = {b["asset"]: b["free"] for b in acc.get("balances", []) if b["asset"] in ("USDT", "ETH")}
            warn = acc.get("canWithdraw")
            return {"ok": True, "msg": f"testnet: USDT {bal.get('USDT', '0')}, ETH {bal.get('ETH', '0')}, "
                                       f"торгівля {'так' if acc.get('canTrade') else 'ні'}"
                                       + (" · ⚠ ключ має право виведення!" if warn else "")}
        if kind == "anthropic":
            import urllib.request
            key = sec.get("ANTHROPIC_API_KEY", "")
            if not key:
                return {"ok": False, "msg": "ключ не задано"}
            req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1", headers={
                "x-api-key": key, "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            return {"ok": True, "msg": "ключ дійсний"}
        if kind == "telegram":
            if not (sec.get("TELEGRAM_TOKEN") and sec.get("TELEGRAM_CHAT_ID")):
                return {"ok": False, "msg": "токен або chat id не задано"}
            import urllib.parse
            import urllib.request
            data = urllib.parse.urlencode({"chat_id": sec["TELEGRAM_CHAT_ID"], "text": "✅ ETH-Агент: тест сповіщень"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{sec['TELEGRAM_TOKEN']}/sendMessage", data=data,
                                   timeout=15).read()
            return {"ok": True, "msg": "тестове повідомлення надіслано"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {str(e)[:160]}"}
    return {"ok": False, "msg": "невідома перевірка"}


# --- дані для графіків -------------------------------------------------------------------------
def candles(agent, tf: str) -> list:
    a0 = getattr(agent, "agents", [agent])[0]
    return a0.m.candles.get(tf, []) if tf in ("1m", "1h", "1d") else []


def equity_history(home: Path, mode: str, limit: int = 5000) -> list:
    p = home / f"equity_{mode}.csv"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines()[-limit:]:
        try:
            t, e, px = line.split(",")
            rows.append([int(t), float(e), float(px)])
        except ValueError:
            continue
    return rows


def trades(journal, n: int = 300) -> list:
    out = []
    for t in journal.recent_trades(n):
        reason = t[7] or ""
        sleeve = reason[1:reason.index("]")] if reason.startswith("[") and "]" in reason else ""
        out.append({"bot": t[0], "entry_ts": t[1], "exit_ts": t[2], "entry_px": float(t[3]), "exit_px": float(t[4]),
                    "qty": float(t[5]), "pnl": float(t[6]), "reason": reason.split("] ", 1)[-1], "sleeve": sleeve})
    return out


def events(journal, n: int = 150) -> list:
    return [{"ts": ts, "level": lvl, "kind": k, "msg": m} for ts, lvl, k, m in journal.recent_events(n)]


def ai_history(journal, n: int = 80) -> list:
    with journal.lock:
        rows = journal.db.execute("SELECT ts, data FROM events WHERE kind='ai_news' AND level='INFO' "
                                  "ORDER BY rowid DESC LIMIT ?", (n,)).fetchall()
    out = []
    for ts, d in rows:
        try:
            x = json.loads(d or "{}")
        except ValueError:
            continue
        if x.get("news_regime"):
            out.append({k: x.get(k) for k in ("ts", "news_regime", "confidence", "price", "facts", "trade", "mode",
                                               "trigger", "applied_until")})
    return out


def upcoming(now_ms: int, days: int = 30) -> list:
    return [{"ts": t, "kind": k} for t, k in calendar.events() if now_ms - 2 * 86_400_000 <= t <= now_ms + days * 86_400_000]


class NewsCache:
    def __init__(self, ttl_s: int = 900):
        self.ttl, self.at, self.items, self.errors, self.lock = ttl_s, 0.0, [], [], threading.Lock()
        self.busy = False

    def get(self, feeds: list[str] | None = None) -> dict:
        if time.time() - self.at > self.ttl and not self.busy:
            self.busy = True
            threading.Thread(target=self._refresh, args=(feeds,), daemon=True).start()
        with self.lock:
            return {"items": self.items, "errors": self.errors, "at": int(self.at * 1000)}

    def _refresh(self, feeds) -> None:
        from agent.analyst import news
        try:
            items, errors = news.fetch(feeds or news.DEFAULT_FEEDS, int((time.time() - 48 * 3600) * 1000), limit=40)
            with self.lock:
                self.items = [{"ts": h.ts, "source": h.source, "title": h.title, "summary": h.summary} for h in items]
                self.errors, self.at = errors, time.time()
        finally:
            self.busy = False
