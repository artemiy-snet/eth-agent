"""Клуб ETH-Агента (за бажанням): раз на добу бот надсилає на criptobot.store метрики вчорашнього дня — POST /api/club/metrics.

Лише відсотки й лічильники: індекс капіталу (старт = 100), зміна за день, просадка, кількість угод, версія і хеш
налаштувань без бюджету. Жодних сум у $, кількостей монет, ключів чи ордерів. Потрібен CLUB_TOKEN у secrets.env —
його дає `eth-agent club-register --nick <нік>`. Відправка у фоні; невдача — повтор через 30 хв.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from agent.live.site_feed import equity_index

CHECK_MS = 30 * 60_000
WARNING = ("Перед входом у клуб:\n"
           "• журнал публічний — нік і відсотки бачать усі на criptobot.store;\n"
           "• надсилається лише: зміна капіталу у %, просадка, кількість угод, версія бота; ключі, суми в $ і ордери — ніколи;\n"
           "• перші дані з'являться завтра — бот шле підсумок раз на добу;\n"
           "• рейтинг — від 14 днів; це порівняння, а не інвестиційна порада;\n"
           "• вийти можна будь-коли, журнал тоді сховається.")


def day_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def settings_hash(sleeves) -> str:
    """8 hex: склад рукавів і їх параметри без бюджету — однакові налаштування в різних учасників дають однаковий хеш."""
    parts = []
    for s in sleeves or ():
        p = dataclasses.asdict(s.params) if dataclasses.is_dataclass(s.params) else {}
        p = {k: str(v) for k, v in p.items() if "budget" not in k and "capital" not in k}
        parts.append([s.name, s.kind, s.pair, sorted(p.items())])
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:8]


def day_metrics(rows: list, closed: list, day: str) -> dict | None:
    """Метрики за UTC-добу day з рядків equity_<mode>.csv і закритих угод. None — даних за день немає."""
    eq = equity_index(rows)                                  # [[ts, індекс бота, індекс монети]]
    before = [p for p in eq if day_of(p[0]) < day]
    pts = [p for p in eq if day_of(p[0]) == day]
    if not pts:
        return None
    base = before[-1] if before else pts[0]
    dd = 0.0
    run = max([p[1] for p in before] or [base[1]])
    for p in pts:
        run = max(run, p[1])
        dd = min(dd, (p[1] / run - 1) * 100)
    done = [t for t in closed if day_of(t["exit_ts"]) == day]
    return {"date": day, "equity_change_pct": round((pts[-1][1] / base[1] - 1) * 100, 3),
            "equity_index": round(pts[-1][1], 3), "hold_index": round(pts[-1][2], 3),
            "trades": len(done), "win_rate": round(sum(t["pnl"] > 0 for t in done) / len(done), 3) if done else None,
            "max_drawdown_pct": round(dd, 3)}


def club_feed(owner, agents: list, now: int) -> None:
    """Раз на 30 хв перевіряє: чи надіслано вчорашній день. Помилки мережі торгівлі не заважають."""
    token = getattr(owner, "club_token", "")
    if not token or now < getattr(owner, "_next_club", 0):
        return
    owner._next_club = now + CHECK_MS
    day = day_of(now - 86_400_000)
    mark = owner.home / "club_sent.txt"
    if mark.exists() and mark.read_text().strip() >= day:
        return
    try:
        from agent import __version__
        from agent.live.panel_api import equity_history, trades
        m = day_metrics(equity_history(owner.home, owner.s.mode, limit=20000), trades(owner.j, 300), day)
        if m is None:
            return
        s0 = next((a.last_status for a in agents if a.last_status), {}) or {}
        m.update(strategy_version=str(__version__)[:16], settings_hash=settings_hash(getattr(owner.s, "sleeves", ())),
                 pair=s0.get("pair", "ETHUSDT"), mode=owner.s.mode)
    except Exception as e:  # noqa: BLE001
        owner.j.event("WARN", "club", f"метрики клубу не зібрано: {e}")
        return
    url = getattr(owner, "club_url", "https://criptobot.store").rstrip("/") + "/api/club/metrics"

    def send():
        req = urllib.request.Request(url, data=json.dumps(m).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            mark.write_text(day)
        except Exception:  # noqa: BLE001 — сайт недоступний: повтор через 30 хв
            pass
    threading.Thread(target=send, daemon=True).start()


def register(home: Path, nick: str, url: str = "https://criptobot.store", invite: str = "", opener=urllib.request.urlopen) -> str:
    """Реєструє бота в клубі й дописує CLUB_TOKEN у secrets.env (токен не друкується). Повертає адресу сторінки."""
    body = {"nick": nick} | ({"invite": invite} if invite else {})
    req = urllib.request.Request(url.rstrip("/") + "/api/club/register", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with opener(req, timeout=20) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", f"HTTP {e.code}")
        except ValueError:
            msg = f"HTTP {e.code}"
        raise ValueError(msg) from None
    except OSError as e:
        raise ValueError(f"сайт недоступний: {e}") from None
    _write_secrets(home, {"CLUB_TOKEN": res["token"], "CLUB_NICK": nick})
    return url.rstrip("/") + res["page"]


def _write_secrets(home: Path, kv: dict) -> None:
    """Замінює/видаляє (значення None) рядки KEY= у secrets.env, решту не чіпає."""
    home.mkdir(parents=True, exist_ok=True)
    p = home / "secrets.env"
    old = p.read_text() if p.exists() else ""
    lines = [ln for ln in old.splitlines() if ln.split("=", 1)[0].strip() not in kv]
    tmp = p.with_suffix(".tmp")                              # атомарно: обрив не зітре ключі Binance
    tmp.write_text("\n".join(lines + [f"{k}={v}" for k, v in kv.items() if v is not None]) + "\n")
    tmp.chmod(0o600)
    tmp.replace(p)


def status(owner) -> dict:
    nick = getattr(owner, "club_nick", "")
    url = getattr(owner, "club_url", "https://criptobot.store").rstrip("/")
    return {"joined": bool(getattr(owner, "club_token", "")), "nick": nick, "page": f"{url}/u-{nick}.html" if nick else "",
            "club": url + "/club.html"}


def join(owner, nick: str) -> dict:
    """Кнопка в панелі / команда /club: реєстрація і старт відправки без перезапуску агента."""
    nick = (nick or "").strip().lower()
    page = register(owner.home, nick, getattr(owner, "club_url", "https://criptobot.store"))
    from agent.live.runner import read_secrets
    owner.club_token, owner.club_nick = read_secrets(owner.home).get("CLUB_TOKEN", ""), nick
    owner._next_club = 0
    owner.j.event("INFO", "club", f"бот приєднався до клубу як {nick}")
    return status(owner) | {"page": page}


def leave(owner, delete_data: bool = False, opener=urllib.request.urlopen) -> dict:
    """Вийти з клубу: токен відкликається на сервері й видаляється з secrets.env."""
    token = getattr(owner, "club_token", "")
    if token:
        req = urllib.request.Request(getattr(owner, "club_url", "https://criptobot.store").rstrip("/") + "/api/club/leave",
                                     data=json.dumps({"delete_data": bool(delete_data)}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            opener(req, timeout=20).read()
        except OSError as e:
            raise ValueError(f"сайт недоступний: {e}") from None
    _write_secrets(owner.home, {"CLUB_TOKEN": None, "CLUB_NICK": None})
    owner.club_token, owner.club_nick = "", ""
    owner.j.event("INFO", "club", "бот вийшов з клубу")
    return status(owner)

