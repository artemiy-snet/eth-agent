"""Керування агентом з Telegram: /status, /trades, /stop, /flatten, /resume, /help.

Слухає лише свій TELEGRAM_CHAT_ID (повідомлення з інших чатів ігноруються). Команди діють через ті самі
файли, що й кнопки панелі (STOP, STOP_FLATTEN, CLEAR_KILL), тому торговий цикл не змінюється.
Старі повідомлення, надіслані до запуску агента, не виконуються.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COMMANDS = [
    ("status", "стан: капітал, позиції, ШІ"),
    ("chart", "графік ETH: /chart, /chart 7d, /chart 30d"),
    ("equity", "графік капіталу проти ETH"),
    ("ai", "остання оцінка ШІ-аналітика"),
    ("trades", "останні угоди"),
    ("stop", "заборонити нові покупки"),
    ("flatten", "продати все і зупинитись"),
    ("resume", "дозволити торгівлю знову"),
    ("post", "нотатка в журнал criptobot.store: /post текст"),
    ("club", "клуб ботів: /club нік — приєднатися, /club вийти"),
    ("help", "список команд"),
]
HELP = "Команди:\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS)
CONFIRM_S = 60


def _f(x, d=2) -> str:
    try:
        return f"{float(x):,.{d}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def status_text(ov: dict) -> str:
    """Коротке зведення з даних вкладки «Огляд» панелі."""
    eq, st = float(ov.get("equity") or 0), float(ov.get("start_equity") or 0)
    pnl = eq - st
    px = float(ov.get("price") or 0)
    lines = [f"{'🔴 LIVE' if ov.get('mode') == 'live' else '🧪 ' + str(ov.get('mode'))} · ETH {_f(px)} $",
             f"Капітал: {_f(eq)} $ ({'+' if pnl >= 0 else ''}{_f(pnl)} $ від старту {_f(st)} $)"]
    if ov.get("killed"):
        lines.append("⛔ KILL SWITCH — нові входи заблоковано (/resume — зняти)")
    elif ov.get("stop_file"):
        lines.append("■ STOP — нові входи заборонено (/resume — зняти)")
    pos = []
    others = {}
    for s in ov.get("sleeves") or []:
        spx = float(s.get("price") or 0) or px              # ціна пари рукава (ETH або SOL…)
        coin = s.get("base") or "ETH"
        if s.get("pair") and ov.get("pair") and s["pair"] != ov["pair"] and spx:
            others[coin] = spx
        for b in s.get("bots") or []:
            if b.get("phase") == "FLAT" or not float(b.get("eth") or 0):
                continue
            e, q = float(b.get("entry") or 0), float(b.get("eth") or 0)
            u = (spx - e) * q if e and spx else 0
            pc = (spx / e - 1) * 100 if e and spx else 0
            stop = f", стоп {_f(b['stop'])}" if b.get("stop") else ""
            pos.append(f"• {s.get('sleeve')} #{b.get('id')}: {q:.4f} {coin} по {_f(e)} → "
                       f"{'+' if u >= 0 else ''}{_f(u)} $ ({'+' if pc >= 0 else ''}{pc:.1f} %){stop}")
    if others:
        lines[0] += "".join(f" · {k} {_f(v)} $" for k, v in others.items())
    lines.append("Позиції:\n" + "\n".join(pos) if pos else "Позицій немає — агент у USDT, чекає входу")
    an = ov.get("analyst") or {}
    if an:
        reg = an.get("active_regime") or "NORMAL"
        lines.append(f"🧠 ШІ: {reg}" + ("" if reg == "NORMAL" else f" (входи ×{an.get('size_mult')})"))
    earn = ov.get("earn") or {}
    if earn.get("parked"):
        lines.append(f"💰 Earn: {_f(earn['parked'])} USDT під {_f(earn.get('apr_pct'))} % річних")
    if ov.get("bnb_fees") is not None:
        lines.append("🪙 Комісія в BNB: " + ("так (−25 %)" if ov["bnb_fees"] else "ні — увімкни в Binance"))
    return "\n".join(lines)


def trades_text(rows: list) -> str:
    if not rows:
        return "Закритих угод ще немає."
    out = ["Останні угоди:"]
    for t in rows[:5]:
        pnl = float(t.get("pnl") or 0)
        when = time.strftime("%d.%m %H:%M", time.localtime(int(t.get("exit_ts") or 0) / 1000))
        out.append(f"{'✅' if pnl >= 0 else '🔻'} {when} {t.get('sleeve') or ''} #{t.get('bot')}: "
                   f"{_f(t.get('entry_px'))} → {_f(t.get('exit_px'))}, {'+' if pnl >= 0 else ''}{_f(pnl)} $")
    return "\n".join(out)


def chart_photo(arg: str, candles, ov: dict, trades: list):
    """(png, підпис) графіка ціни ETH з входами/стопами відкритих позицій і угодами за період."""
    from agent.live import chart_png as cp
    arg = (arg or "24h").lower()
    tf, n, label = ("1h", 168, "7 днів") if arg.startswith("7") else \
                   ("1h", 720, "30 днів") if arg.startswith("30") else ("1m", 1440, "24 год")
    rows = (candles(tf) or [])[-n:]
    if len(rows) < 2:
        return None, "Даних для графіка ще немає — агент щойно запустився."
    pts = cp.downsample([(r[0], r[4]) for r in rows])
    t0 = rows[0][0]
    hl, mk, notes = [], [], []
    other = {s.get("sleeve") for s in ov.get("sleeves") or [] if s.get("pair") and ov.get("pair") and s["pair"] != ov["pair"]}
    trades = [t for t in trades if t.get("sleeve") not in other]       # графік ETH — без угод інших пар
    for s in ov.get("sleeves") or []:
        if s.get("sleeve") in other:
            continue
        for b in s.get("bots") or []:
            if b.get("phase") == "FLAT" or not float(b.get("eth") or 0):
                continue
            if b.get("entry"):
                hl.append((float(b["entry"]), cp.COLORS["entry"], True))
                notes.append(f"вхід {s.get('sleeve')} {_f(b['entry'])}")
            if b.get("stop"):
                hl.append((float(b["stop"]), cp.COLORS["stop"], True))
                notes.append(f"стоп {_f(b['stop'])}")
            if b.get("entry_ts") and int(b["entry_ts"]) >= t0:
                mk.append((int(b["entry_ts"]), float(b["entry"]), cp.COLORS["buy"], True))
    for t in trades:
        if int(t.get("entry_ts") or 0) >= t0:
            mk.append((int(t["entry_ts"]), float(t["entry_px"]), cp.COLORS["buy"], True))
        if int(t.get("exit_ts") or 0) >= t0:
            mk.append((int(t["exit_ts"]), float(t["exit_px"]), cp.COLORS["sell"], False))
    png = cp.chart([(pts, cp.COLORS["eth"])], hl, mk)
    first, last = rows[0][4], rows[-1][4]
    lo, hi = min(r[3] for r in rows), max(r[2] for r in rows)
    cap = (f"ETH за {label}: {_f(first)} → {_f(last)} ({(last / first - 1) * 100:+.1f} %)\n"
           f"мін {_f(lo)} · макс {_f(hi)}" + ("\n" + " · ".join(notes) if notes else "") +
           "\n🟢 пунктир — вхід, 🔴 — стоп; ▲ купівля, ▼ продаж")
    return png, cap


def equity_photo(rows: list):
    """(png, підпис) капіталу портфеля проти ETH (індекс, старт = 100; зміна бюджету не рахується збитком)."""
    from agent.live import chart_png as cp
    if len(rows) < 2:
        return None, "Історії капіталу ще мало — точки пишуться раз на 5 хв."
    pf, v = [], 100.0
    for i, r in enumerate(rows):
        if i:
            a = rows[i - 1]
            if len(a) > 3 and len(r) > 3 and a[3] and r[3]:
                k = (r[1] / r[3]) / (a[1] / a[3])
            elif len(a) <= 3 and len(r) <= 3:
                k = r[1] / a[1] if a[1] else 1
            else:
                k = 1
            v *= k if k > 0 else 1
        pf.append((r[0], v))
    eth = [(r[0], r[2] / rows[0][2] * 100) for r in rows]
    png = cp.chart([(cp.downsample(eth), cp.COLORS["eth"]), (cp.downsample(pf), cp.COLORS["pf"])],
                   [(100.0, cp.COLORS["grey"], True)])
    return png, (f"🔵 Портфель {pf[-1][1] - 100:+.1f} %  ·  🟠 Тримати ETH {eth[-1][1] - 100:+.1f} %\n"
                 f"(індекс від старту = 100; сіра лінія — старт)")


def ai_text(home: Path) -> str:
    try:
        r = json.loads((home / "ai_news.json").read_text())
    except (OSError, ValueError):
        return "ШІ-аналітик ще не робив оцінок."
    a = r.get("assessment") or r
    facts = "\n".join("• " + str(x) for x in (a.get("facts") or [])[:3])
    when = time.strftime("%d.%m %H:%M", time.localtime(int(r.get("ts") or 0) / 1000)) if r.get("ts") else ""
    return (f"🧠 {a.get('news_regime', '?')} (впевненість {a.get('confidence', '?')}) {when}\n{facts}\n"
            f"Вплив: {str(a.get('impact_on_strategy', ''))[:400]}")


class TelegramControl:
    def __init__(self, token: str, chat_id: str, home: Path, overview, trades, journal=None,
                 api: str = "https://api.telegram.org", candles=None, equity=None, site: str = "", site_token: str = "", club=None):
        self.token, self.chat, self.home = token.strip(), str(chat_id).strip(), home
        self.overview, self.trades, self.j, self.api = overview, trades, journal, api
        self.candles, self.equity = candles or (lambda tf: []), equity or (lambda: [])
        self.site, self.site_token = site.rstrip("/"), site_token.strip()
        self.club = club
        self.offset = 0
        self.flatten_asked = 0.0
        self.stop_flag = threading.Event()

    # --- транспорт ------------------------------------------------------------------------
    def _call(self, method: str, params: dict | None = None, timeout: float = 35):
        data = urllib.parse.urlencode(params or {}).encode()
        with urllib.request.urlopen(f"{self.api}/bot{self.token}/{method}", data=data, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("result")

    def send(self, text: str) -> None:
        try:
            self._call("sendMessage", {"chat_id": self.chat, "text": text[:4000]}, timeout=15)
        except Exception:  # noqa: BLE001
            pass

    def send_photo(self, png: bytes, caption: str = "") -> None:
        b = "----ethagent" + str(int(time.time() * 1000))
        parts = []
        for k, v in (("chat_id", self.chat), ("caption", caption[:1000])):
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
                     f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
        body = b"".join(parts) + f"--{b}--\r\n".encode()
        req = urllib.request.Request(f"{self.api}/bot{self.token}/sendPhoto", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:  # noqa: BLE001
            self.send(caption or "Не вдалося надіслати графік.")

    def skip_backlog(self) -> None:
        """Повідомлення, надіслані поки агент не працював, не виконуються."""
        res = self._call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15) or []
        if res:
            self.offset = res[-1]["update_id"] + 1

    def post(self, note: str) -> str:
        """Нотатка → редакція журналу criptobot.store (/api/post): Claude зробить статтю, сайт оновиться за 2–3 хв."""
        if not self.site_token:
            return "Журнал не підключено: додай «токен редакції» в панелі → Налаштування → ключі."
        if len(note) < 20:
            return "Напиши нотатку після команди, напр.: /post Сьогодні бот уперше продав ETH з прибутком…"
        req = urllib.request.Request(f"{self.site}/api/post", data=json.dumps({"text": note}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.site_token}"})
        try:
            with urllib.request.urlopen(req, timeout=150) as r:
                j = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read().decode())
            except ValueError:
                j = {"error": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            return f"Сайт недоступний: {e}"
        if j.get("ok"):
            return f"📰 Редакція написала статтю. З'явиться за 2–3 хв: {self.site}{j.get('url', '')}"
        return "Не вийшло: " + str(j.get("error", "невідома помилка"))

    # --- команди --------------------------------------------------------------------------
    def handle(self, text: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        cmd = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
        cmd = cmd.split("@")[0].lstrip("/")
        if cmd in ("status", "s", "статус"):
            return status_text(self.overview())
        if cmd in ("trades", "угоди"):
            return trades_text(self.trades())
        if cmd in ("chart", "графік"):
            arg = (text or "").split()[1] if len((text or "").split()) > 1 else ""
            png, cap = chart_photo(arg, self.candles, self.overview(), self.trades())
            return (png, cap) if png else cap
        if cmd in ("equity", "капітал"):
            png, cap = equity_photo(self.equity())
            return (png, cap) if png else cap
        if cmd == "ai":
            return ai_text(self.home)
        if cmd in ("post", "стаття"):
            return self.post((text or "").strip().split(None, 1)[1] if len((text or "").split()) > 1 else "")
        if cmd == "stop":
            (self.home / "STOP").touch()
            self._log("STOP з Telegram")
            return "■ STOP: нові покупки заборонено. Відкриті позиції і стопи працюють далі. /resume — зняти."
        if cmd == "flatten":
            self.flatten_asked = now
            return (f"Продати ВСЕ за ринком і зупинитись? Щоб підтвердити, надішли /flatten_yes протягом {CONFIRM_S} с.")
        if cmd == "flatten_yes":
            if now - self.flatten_asked > CONFIRM_S:
                return "Немає активного запиту. Спершу /flatten."
            self.flatten_asked = 0
            (self.home / "STOP_FLATTEN").touch()
            self._log("STOP FLATTEN з Telegram")
            return "⛔ STOP FLATTEN: агент продає позиції і блокує нові входи. /resume — дозволити торгівлю знову."
        if cmd == "resume":
            (self.home / "CLEAR_KILL").touch()
            self._log("зняття kill switch / STOP з Telegram")
            return "▶️ Торгівлю дозволено (STOP і kill switch знято протягом ~10 с)."
        if cmd in ("club", "клуб") and self.club is not None:
            return self.club((text or "").split()[1] if len((text or "").split()) > 1 else "")
        if cmd in ("help", "start", "допомога"):
            return HELP
        return "Не знаю такої команди.\n" + HELP

    def _log(self, msg: str) -> None:
        if self.j is not None:
            self.j.event("INFO", "telegram", msg)

    # --- цикл -----------------------------------------------------------------------------
    def poll_once(self, timeout: int = 25) -> None:
        res = self._call("getUpdates", {"offset": self.offset, "timeout": timeout,
                                        "allowed_updates": '["message"]'}, timeout=timeout + 10) or []
        for u in res:
            self.offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != self.chat:
                continue                                   # чужий чат — ігнор
            out = self.handle(m.get("text", ""))
            if isinstance(out, tuple):
                self.send_photo(*out)
            else:
                self.send(out)

    def run(self) -> None:
        try:
            self._call("setMyCommands", {"commands": json.dumps(
                [{"command": c, "description": d} for c, d in COMMANDS if c != "help"], ensure_ascii=False)},
                timeout=15)
            self.skip_backlog()
        except Exception:  # noqa: BLE001
            pass
        while not self.stop_flag.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — мережа; Telegram не має зупиняти торгівлю
                self.stop_flag.wait(15)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="telegram", daemon=True)
        t.start()
        return t


def club_text(agent, arg: str) -> str:
    """/club — стан, /club нік — приєднатися, /club вийти — вийти (журнал лишається, але прихований)."""
    from agent.live import club
    try:
        if arg.lower() in ("вийти", "leave"):
            club.leave(agent)
            return "Бот вийшов з клубу. Відправку зупинено."
        if arg:
            s = club.join(agent, arg)
            return f"✅ Бот у клубі як {s['nick']}. Раз на добу він надсилатиме лише відсотки.\nТвій журнал: {s['page']}"
    except ValueError as e:
        return f"Не вийшло: {e}"
    s = club.status(agent)
    if s["joined"]:
        return f"Бот у клубі як {s['nick']}: {s['page']}\nВийти: /club вийти"
    return ("Клуб ботів — журнали учасників поруч, лише відсотки (без ключів і сум).\n"
            f"Приєднатися: /club твій_нік (латиницею)\nПодивитися: {s['club']}")


def start(agent, sec: dict, home: Path):
    """Запустити керування з Telegram, якщо задано токен і chat id."""
    if not (sec.get("TELEGRAM_TOKEN") and sec.get("TELEGRAM_CHAT_ID")):
        return None
    from agent.live import panel_api as api
    from agent.live.status_server import overview
    mode = getattr(agent, "agents", [agent])[0].s.mode
    tc = TelegramControl(sec["TELEGRAM_TOKEN"], sec["TELEGRAM_CHAT_ID"], home,
                         overview=lambda: overview(agent), trades=lambda: api.trades(agent.j, 50), journal=agent.j,
                         candles=lambda tf: api.candles(agent, tf), equity=lambda: api.equity_history(home, mode),
                         site=sec.get("SITE_URL", "https://criptobot.store"), site_token=sec.get("SITE_POST_TOKEN", ""), club=lambda arg: club_text(agent, arg))
    tc.start()
    return tc
