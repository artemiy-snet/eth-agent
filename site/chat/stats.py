"""GET /api/stats?days=N для «Пульта»: відвідувачі з Umami (без cookie) + боти з GoAccess.

Лише читання. Токен STATS_TOKEN — окремий від токена публікацій. Umami питаємо через його API на 127.0.0.1
(UMAMI_URL, UMAMI_USER, UMAMI_PASS, UMAMI_WEBSITE_ID у /etc/criptobot/stats.env). Відповідь кешується на 60 с.
Сумісно з Umami v2 (значення {"value":…}) і новішими (прості числа, поле path замість url).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

TZ = "Europe/Kyiv"


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ)
    except Exception:  # noqa: BLE001
        return None


def num(x) -> float:
    if isinstance(x, dict):
        x = x.get("value", 0)
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


class Umami:
    def __init__(self, url: str, user: str, password: str, website: str, opener=urllib.request.urlopen):
        self.url, self.user, self.password, self.website = url.rstrip("/"), user, password, website
        self.opener, self.token, self.token_at = opener, "", 0.0

    def _req(self, method: str, path: str, body: dict | None = None, auth: bool = True):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=h)
        with self.opener(req, timeout=15) as r:
            return json.loads(r.read().decode() or "null")

    def login(self) -> None:
        if self.token and time.time() - self.token_at < 6 * 3600:
            return
        r = self._req("POST", "/api/auth/login", {"username": self.user, "password": self.password}, auth=False)
        self.token, self.token_at = r["token"], time.time()

    def get(self, path: str, **q):
        self.login()
        qs = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
        try:
            return self._req("GET", f"/api/websites/{self.website}{path}" + (f"?{qs}" if qs else ""))
        except urllib.error.HTTPError as e:
            if e.code == 401:                          # токен прострочено — один повтор
                self.token = ""
                self.login()
                return self._req("GET", f"/api/websites/{self.website}{path}" + (f"?{qs}" if qs else ""))
            raise

    def metrics(self, kind: str, start: int, end: int, limit: int = 10) -> list:
        kinds = {"url": ("url", "path"), "referrer": ("referrer",), "country": ("country",), "device": ("device",)}[kind]
        for k in kinds:
            try:
                rows = self.get("/metrics", startAt=start, endAt=end, type=k, limit=limit)
            except urllib.error.HTTPError:
                continue
            if isinstance(rows, list):
                return rows[:limit]
        return []


def bots(goaccess_dir: Path, days: int, today: str) -> dict:
    """Сума добових файлів bots_YYYY-MM-DD.json за останні N днів (їх щогодини пише deploy/goaccess_daily.py)."""
    total, top = 0, defaultdict(int)
    d0 = datetime.strptime(today, "%Y-%m-%d")
    for i in range(days):
        day = (d0 - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            b = json.loads((goaccess_dir / f"bots_{day}.json").read_text())
        except (OSError, ValueError):
            continue
        total += int(b.get("requests", 0))
        for name, n in (b.get("top") or {}).items():
            top[name] += int(n)
    return {"requests": total, "top": [{"name": k, "requests": v} for k, v in sorted(top.items(), key=lambda x: -x[1])[:5]]}


def page_title(www: Path, path: str, cache: dict) -> str:
    if path in cache:
        return cache[path]
    name = path.split("?")[0].strip("/") or "index.html"
    t = ""
    if re.fullmatch(r"[\w.-]+\.html", name):
        try:
            m = re.search(r"<title>(.*?)</title>", (www / name).read_text(encoding="utf-8")[:4000], re.S)
            t = re.sub(r"\s+—\s+ETH-Агент$", "", m.group(1)).strip() if m else ""
        except OSError:
            t = ""
    import html as _h
    cache[path] = _h.unescape(t)
    return cache[path]


def collect(um: Umami, days: int, www: Path, goaccess_dir: Path, articles_fn, now: float | None = None) -> dict:
    tz = _tz()
    now_dt = datetime.fromtimestamp(now or time.time(), tz) if tz else datetime.utcfromtimestamp(now or time.time())
    end = int(now_dt.timestamp() * 1000)
    midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    start = int((midnight - timedelta(days=days - 1)).timestamp() * 1000)
    st = um.get("/stats", startAt=start, endAt=end) or {}
    td = um.get("/stats", startAt=int(midnight.timestamp() * 1000), endAt=end) or {}
    visits = num(st.get("visits"))
    act = um.get("/active") or {}
    live = num(act.get("visitors", act.get("x", 0))) if isinstance(act, dict) else (num(act[0].get("x")) if act else 0)
    series = um.get("/pageviews", startAt=start, endAt=end, unit="day", timezone=TZ) or {}
    pv = {str(r.get("x"))[:10]: int(num(r.get("y"))) for r in series.get("pageviews", [])}
    ss = {str(r.get("x"))[:10]: int(num(r.get("y"))) for r in series.get("sessions", [])}
    daily = []
    for i in range(days):
        day = (midnight - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        daily.append({"date": day, "visitors": ss.get(day, 0), "pageviews": pv.get(day, 0)})
    titles: dict = {}
    pages = [{"path": r.get("x") or "/", "title": page_title(www, r.get("x") or "/", titles), "views": int(num(r.get("y")))}
             for r in um.metrics("url", start, end)]
    countries = [{"code": (r.get("x") or "??").upper(), "visitors": int(num(r.get("y")))} for r in um.metrics("country", start, end)]
    refs = [{"source": r.get("x") or "(прямі)", "visitors": int(num(r.get("y")))} for r in um.metrics("referrer", start, end)]
    devs = [{"type": r.get("x") or "unknown", "visitors": int(num(r.get("y")))} for r in um.metrics("device", start, end)]
    arts = articles_fn() if articles_fn else []
    return {
        "ok": True, "generated": now_dt.isoformat(timespec="seconds"), "days": days, "live": int(live),
        "today": {"visitors": int(num(td.get("visitors"))), "pageviews": int(num(td.get("pageviews")))},
        "totals": {"visitors": int(num(st.get("visitors"))), "visits": int(visits), "pageviews": int(num(st.get("pageviews"))),
                   "bounce_rate": round(num(st.get("bounces")) / visits, 3) if visits else 0.0,
                   "avg_time_s": int(num(st.get("totaltime")) / visits) if visits else 0},
        "daily": daily, "pages": pages, "countries": countries, "referrers": refs, "devices": devs,
        "bots": bots(goaccess_dir, days, now_dt.strftime("%Y-%m-%d")),
        "articles": {"total": len(arts), "last": [{"title": a["title"], "url": f"/{a['slug']}.html",
                                                    "date": time.strftime("%Y-%m-%d", time.gmtime(a["ts"]))} for a in arts[:5]]},
        "search": None,
    }


class StatsAPI:
    """Авторизація, ліміт 30 запитів/хв на IP і кеш 60 с на кожне значення days."""

    def __init__(self, env: dict | None = None, opener=urllib.request.urlopen, articles_fn=None, club_fn=None):
        e = env if env is not None else dict(os.environ)
        self.token = e.get("STATS_TOKEN", "")
        self.um = Umami(e.get("UMAMI_URL", "http://127.0.0.1:3010"), e.get("UMAMI_USER", "admin"),
                        e.get("UMAMI_PASS", ""), e.get("UMAMI_WEBSITE_ID", ""), opener)
        self.www = Path(e.get("WWW_DIR", "/var/www/criptobot"))
        self.ga = Path(e.get("GOACCESS_DIR", "/var/lib/criptobot/goaccess"))
        self.articles_fn = articles_fn
        self.club_fn = club_fn
        self.hits: dict[str, deque] = defaultdict(deque)
        self.cache: dict[int, tuple[float, dict]] = {}
        self.lock = threading.Lock()

    def handle(self, auth: str, query: str, ip: str, now: float | None = None) -> tuple[int, dict]:
        now = time.time() if now is None else now
        if not self.token or not hmac.compare_digest(auth or "", f"Bearer {self.token}"):
            return 401, {"error": "Невірний токен"}
        with self.lock:
            q = self.hits[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= 30:
                return 429, {"error": "Забагато запитів — не більше 30 за хвилину"}
            q.append(now)
        try:
            days = int((urllib.parse.parse_qs(query).get("days") or ["7"])[0])
        except ValueError:
            return 400, {"error": "days — ціле число від 1 до 90"}
        if not 1 <= days <= 90:
            return 400, {"error": "days — ціле число від 1 до 90"}
        hit = self.cache.get(days)
        if hit and now - hit[0] < 60:
            return 200, hit[1]
        try:
            data = collect(self.um, days, self.www, self.ga, self.articles_fn, now)
        except Exception as e:  # noqa: BLE001
            return 502, {"error": f"Статистика тимчасово недоступна: {type(e).__name__}"}
        if self.club_fn is not None:
            try:
                data["club"] = self.club_fn(now)
            except Exception:  # noqa: BLE001 — клуб не має ламати статистику сайту
                data["club"] = None
        self.cache[days] = (now, data)
        return 200, data
