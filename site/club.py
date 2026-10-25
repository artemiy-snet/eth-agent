"""Клуб ETH-Агента: учасники за бажанням надсилають щоденні метрики своїх ботів, сайт показує журнали й рейтинг.

Лише стандартна бібліотека, Python 3.10. Дані — JSON-файли в DATA_DIR/club/ (bots.json + m/<нік>.json).
Публічно лише відсотки й лічильники: ключі, баланси й суми в $ клуб не приймає взагалі — запит із будь-яким полем
поза схемою відхиляється. Токени зберігаються лише як sha256.

Змінні середовища: DATA_DIR, CLUB_OPEN (0 — реєстрацію закрито), CLUB_INVITE (якщо задано — потрібен для реєстрації).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import statistics
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

NICK = re.compile(r"[a-z0-9_-]{3,24}")
RESERVED = {"admin", "api", "club", "root", "www", "test", "eth-agent", "criptobot", "redakcia", "editor"}
MIN_DAYS = 14                       # менше днів даних — учасник не в рейтингу
MIN_GROUP = 5                       # версія/налаштування в аналітиці — лише від 5 ботів

# схема метрик: поле → перевірка; інших полів не буває
REQUIRED = {
    "date": lambda v: isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is not None,
    "equity_change_pct": lambda v: _num(v) and abs(v) <= 50,
    "equity_index": lambda v: _num(v) and 1 <= v <= 100_000,
    "trades": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000,
    "win_rate": lambda v: v is None or (_num(v) and 0 <= v <= 1),
    "max_drawdown_pct": lambda v: _num(v) and -100 <= v <= 0,
    "strategy_version": lambda v: isinstance(v, str) and re.fullmatch(r"[\w.+-]{1,16}", v) is not None,
    "settings_hash": lambda v: isinstance(v, str) and re.fullmatch(r"[0-9a-f]{8}", v) is not None,
    "pair": lambda v: isinstance(v, str) and re.fullmatch(r"[A-Z0-9]{5,12}", v) is not None,
    "mode": lambda v: v in ("live", "paper", "testnet"),
}
OPTIONAL = {
    "exposure_pct": lambda v: _num(v) and 0 <= v <= 100,
    "hold_index": lambda v: _num(v) and 1 <= v <= 100_000,
}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate(m) -> str | None:
    """Причина відмови або None."""
    if not isinstance(m, dict):
        return "Метрики мають бути об'єктом."
    extra = set(m) - set(REQUIRED) - set(OPTIONAL)
    if extra:
        return "Зайві поля (клуб приймає лише відсотки й лічильники): " + ", ".join(sorted(extra))[:200]
    for k, ok in REQUIRED.items():
        if k not in m or not ok(m[k]):
            return f"Некоректне або відсутнє поле: {k}"
    for k, ok in OPTIONAL.items():
        if k in m and not ok(m[k]):
            return f"Некоректне поле: {k}"
    try:
        d = date.fromisoformat(m["date"])
    except ValueError:
        return "Некоректна дата."
    today = datetime.now(timezone.utc).date()
    if d > today + timedelta(days=1) or d < today - timedelta(days=400):
        return "Дата поза допустимим діапазоном."
    return None


class Club:
    def __init__(self, data_dir: Path, env: dict | None = None):
        e = env if env is not None else dict(os.environ)
        self.dir = data_dir / "club"
        self.data_root = data_dir
        self.open = e.get("CLUB_OPEN", "1") != "0"
        self.invite = e.get("CLUB_INVITE", "")
        self.lock = threading.Lock()
        self.reg_hits: dict[str, deque] = defaultdict(deque)

    # --- зберігання ---------------------------------------------------------------------------------
    def bots(self) -> dict:
        try:
            return json.loads((self.dir / "bots.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_bots(self, b: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "bots.json.tmp"
        tmp.write_text(json.dumps(b, ensure_ascii=False))
        tmp.replace(self.dir / "bots.json")

    def metrics(self, nick: str) -> dict:
        try:
            return json.loads((self.dir / "m" / f"{nick}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_metrics(self, nick: str, m: dict) -> None:
        (self.dir / "m").mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "m" / f"{nick}.json.tmp"
        tmp.write_text(json.dumps(dict(sorted(m.items())[-800:]), ensure_ascii=False))
        tmp.replace(self.dir / "m" / f"{nick}.json")

    def _mark_rebuild(self, now: float) -> None:
        mark = self.dir / "built"
        if not mark.exists() or now - mark.stat().st_mtime > 25 * 60:
            self.dir.mkdir(parents=True, exist_ok=True)
            mark.touch()
            (self.data_root / "rebuild").touch()

    # --- API ------------------------------------------------------------------------------------------
    def auth(self, header: str) -> str | None:
        tok = header[7:].strip() if (header or "").startswith("Bearer ") else ""
        if not tok:
            return None
        h = _hash(tok)
        for nick, b in self.bots().items():
            if hmac.compare_digest(b.get("hash", ""), h) and not b.get("blocked"):
                return nick
        return None

    def register(self, data, ip: str, now: float | None = None) -> tuple[int, dict]:
        now = time.time() if now is None else now
        if not self.open:
            return 403, {"error": "Реєстрацію в клубі тимчасово закрито."}
        if not isinstance(data, dict):
            return 400, {"error": "Некоректний запит."}
        if self.invite and not hmac.compare_digest(str(data.get("invite") or ""), self.invite):
            return 403, {"error": "Потрібне запрошення."}
        nick = str(data.get("nick") or "").strip().lower()
        if not NICK.fullmatch(nick) or nick in RESERVED:
            return 400, {"error": "Нікнейм: 3–24 символи, латиниця, цифри, _ або -."}
        with self.lock:
            q = self.reg_hits[ip]
            while q and now - q[0] > 86_400:
                q.popleft()
            if len(q) >= 5:
                return 429, {"error": "Забагато реєстрацій з цієї адреси — спробуй завтра."}
            b = self.bots()
            if nick in b:
                return 409, {"error": "Такий нікнейм уже зайнятий."}
            token = "club_" + secrets.token_urlsafe(24)
            b[nick] = {"id": secrets.token_hex(6), "hash": _hash(token), "created": int(now), "hidden": False, "blocked": False}
            self._save_bots(b)
            q.append(now)
        return 200, {"ok": True, "bot_id": b[nick]["id"], "token": token, "page": f"/u-{nick}.html"}

    def submit(self, auth: str, data, now: float | None = None) -> tuple[int, dict]:
        now = time.time() if now is None else now
        nick = self.auth(auth)
        if not nick:
            return 401, {"error": "Невірний токен"}
        why = validate(data)
        if why:
            return 400, {"error": why}
        with self.lock:
            m = self.metrics(nick)
            m[data["date"]] = {k: data[k] for k in list(REQUIRED) + [k for k in OPTIONAL if k in data] if k != "date"}
            self._save_metrics(nick, m)
            b = self.bots()
            if nick in b:
                b[nick]["last"] = int(now)
                self._save_bots(b)
        self._mark_rebuild(now)
        return 200, {"ok": True}

    def leave(self, auth: str, data, now: float | None = None) -> tuple[int, dict]:
        now = time.time() if now is None else now
        nick = self.auth(auth)
        if not nick:
            return 401, {"error": "Невірний токен"}
        with self.lock:
            b = self.bots()
            if (data or {}).get("delete_data") is True:
                b.pop(nick, None)
                p = self.dir / "m" / f"{nick}.json"
                if p.exists():
                    p.unlink()
            else:
                b[nick]["hash"] = ""                       # токен відкликано, журнал лишається
                b[nick]["hidden"] = True
            self._save_bots(b)
        self._mark_rebuild(now)
        return 200, {"ok": True}

    # --- аналітика ---------------------------------------------------------------------------------
    def members(self, today: date | None = None) -> list[dict]:
        """Видимі учасники з підрахованими показниками за 7/30/90 днів."""
        today = today or datetime.now(timezone.utc).date()
        out = []
        for nick, b in sorted(self.bots().items()):
            if b.get("hidden") or b.get("blocked"):
                continue
            m = self.metrics(nick)
            days = sorted(m)
            row = {"nick": nick, "created": b.get("created", 0), "days": len(days), "last": days[-1] if days else None,
                   "mode": m[days[-1]]["mode"] if days else None,
                   "version": m[days[-1]]["strategy_version"] if days else None,
                   "settings": m[days[-1]]["settings_hash"] if days else None,
                   "pair": m[days[-1]]["pair"] if days else None}
            for n in (7, 30, 90):
                row[f"ret_{n}"], row[f"dd_{n}"] = window(m, today, n)
            row["trades_30"] = sum(m[d]["trades"] for d in days if d >= (today - timedelta(days=30)).isoformat())
            r, dd = row["ret_30"], row["dd_30"]
            row["stability"] = round(r / abs(dd), 2) if r is not None and dd not in (None, 0) else None
            out.append(row)
        return out

    def summary(self, now: float | None = None) -> dict:
        """Поле club у GET /api/stats для Пульта."""
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, timezone.utc).date()
        ms = self.members(today)
        week = (today - timedelta(days=7)).isoformat()
        ranked = [r for r in ms if r["days"] >= MIN_DAYS and r["ret_30"] is not None]
        top = sorted(ranked, key=lambda r: -(r["stability"] if r["stability"] is not None else r["ret_30"]))[:5]
        return {"bots": len(ms), "active_7d": sum(1 for r in ms if r["last"] and r["last"] >= week),
                "new_7d": sum(1 for r in ms if r["created"] >= now - 7 * 86_400),
                "top": [{"nick": r["nick"], "return_30d_pct": r["ret_30"], "max_dd_pct": r["dd_30"], "mode": r["mode"]} for r in top],
                "versions": versions(ranked)}


def window(m: dict, today: date, n: int) -> tuple[float | None, float | None]:
    """Дохідність і найгірша просадка за останні n днів з equity_index (лише відсотки)."""
    start = (today - timedelta(days=n)).isoformat()
    pts = [(d, m[d]["equity_index"]) for d in sorted(m) if d >= start]
    if len(pts) < 2:
        return None, None
    before = [m[d]["equity_index"] for d in sorted(m) if d < start]
    base = before[-1] if before else pts[0][1]
    ret = round((pts[-1][1] / base - 1) * 100, 2)
    peak, dd = base, 0.0
    for _, v in pts:
        peak = max(peak, v)
        dd = min(dd, (v / peak - 1) * 100)
    return ret, round(dd, 2)


def versions(ranked: list[dict]) -> list[dict]:
    g: dict[str, list] = defaultdict(list)
    for r in ranked:
        g[r["version"] or "?"].append(r)
    out = []
    for v, rs in sorted(g.items()):
        big = len(rs) >= MIN_GROUP
        out.append({"version": v, "bots": len(rs),
                    "median_return_30d_pct": round(statistics.median(r["ret_30"] for r in rs), 2) if big else None,
                    "median_dd_30d_pct": round(statistics.median(r["dd_30"] for r in rs), 2) if big else None})
    return out
