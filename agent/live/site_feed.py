"""Щоденник бота на сайті criptobot.store: раз на 30 хв агент надсилає знімок свого стану (POST /api/diary).

Публікуються лише ВІДСОТКИ (індекс капіталу проти ETH, % угод і позицій), а не суми в доларах, кількості монет,
ключі чи IP — щоденник публічний. Потрібні SITE_POST_TOKEN (і за бажання SITE_URL) у secrets.env / полях панелі.
Відправка у фоні — торговий цикл не чекає мережі.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

EVERY_MS = 30 * 60_000


def equity_index(rows: list) -> list[list]:
    """[[ts, портфель, ETH]] — обидва індекси, старт = 100. Зміна бюджету (межі капіталу) не рахується прибутком/збитком."""
    out, idx, prev = [], 100.0, None
    p0 = None
    for r in rows:
        ts, total, price = int(r[0]), float(r[1]), float(r[2])
        start = float(r[3]) if len(r) > 3 else None
        if total <= 0 or price <= 0:
            continue
        if prev is not None:
            pt, pstart = prev
            if start is not None and pstart is not None and abs(start - pstart) > 1e-9:
                total_adj = total - (start - pstart)          # поповнення/зменшення бюджету — не результат
                idx *= total_adj / pt if pt > 0 else 1
            else:
                idx *= total / pt
        p0 = p0 or price
        out.append([ts, round(idx, 3), round(price / p0 * 100, 3)])
        prev = (total, start)
    return out


def downsample(pts: list, n: int = 400) -> list:
    if len(pts) <= n:
        return pts
    step = len(pts) / n
    return [pts[int(i * step)] for i in range(n)] + [pts[-1]]


def snapshot(home: Path, mode: str, agents: list, journal, analyst=None, now_ms: int | None = None) -> dict:
    from agent import __version__
    from agent.live.panel_api import equity_history, trades
    now_ms = now_ms or int(time.time() * 1000)
    eq = equity_index(equity_history(home, mode, limit=20000))
    since = eq[0][0] if eq else now_ms
    pos = []
    for a in agents:
        st = a.last_status or {}
        px = float(st.get("price") or 0)
        for b in st.get("bots", []):
            if b.get("phase") == "FLAT" or not float(b.get("eth") or 0) or not b.get("entry"):
                continue
            e = float(b["entry"])
            pos.append({"sleeve": st.get("sleeve"), "coin": st.get("base", "ETH"), "since": int(b.get("entry_ts") or 0),
                        "pnl_pct": round((px / e - 1) * 100, 2) if e and px else None})
    closed = []
    for t in trades(journal, 100):
        if t["exit_ts"] < since:                     # до старту щоденника (старий стан, помилкові записи пилу)
            continue
        base = t["entry_px"] * t["qty"]
        closed.append({"sleeve": t["sleeve"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
                       "pnl_pct": round(t["pnl"] / base * 100, 2) if base > 0 else None, "reason": t["reason"][:40]})
    ai = None
    if analyst is not None:
        try:
            ai = {"regime": analyst.active_regime(now_ms), "mode": analyst.mode}
        except Exception:  # noqa: BLE001
            ai = None
    s0 = next((a.last_status for a in agents if a.last_status), {}) or {}
    return {"v": 1, "ts": now_ms, "mode": mode, "version": __version__, "since": since,
            "price": s0.get("price"), "pair": s0.get("pair", "ETHUSDT"),
            "equity": downsample(eq), "positions": pos, "trades": closed, "ai": ai,
            "killed": any((a.last_status or {}).get("killed") for a in agents)}


def site_feed(owner, agents: list, now: int) -> None:
    """Раз на 30 хв — знімок на сайт (якщо задано SITE_POST_TOKEN). Помилки мережі не заважають торгівлі."""
    token = getattr(owner, "site_token", "")
    if not token or now < getattr(owner, "_next_site", 0):
        return
    owner._next_site = now + EVERY_MS
    try:
        snap = snapshot(owner.home, owner.s.mode, agents, owner.j, getattr(owner, "analyst", None), now)
    except Exception as e:  # noqa: BLE001
        owner.j.event("WARN", "site", f"щоденник для сайту не зібрано: {e}")
        return
    url = getattr(owner, "site_url", "https://criptobot.store").rstrip("/") + "/api/diary"

    def send():
        req = urllib.request.Request(url, data=json.dumps(snap).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception:  # noqa: BLE001 — сайт недоступний: наступна спроба через 30 хв
            pass
    threading.Thread(target=send, daemon=True).start()
