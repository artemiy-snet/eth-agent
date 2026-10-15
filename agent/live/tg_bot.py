"""Керування агентом з Telegram: /status, /trades, /stop, /flatten, /resume, /help.

Слухає лише свій TELEGRAM_CHAT_ID (повідомлення з інших чатів ігноруються). Команди діють через ті самі
файли, що й кнопки панелі (STOP, STOP_FLATTEN, CLEAR_KILL), тому торговий цикл не змінюється.
Старі повідомлення, надіслані до запуску агента, не виконуються.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

COMMANDS = [
    ("status", "стан: капітал, позиції, ШІ"),
    ("trades", "останні угоди"),
    ("stop", "заборонити нові покупки"),
    ("flatten", "продати все і зупинитись"),
    ("resume", "дозволити торгівлю знову"),
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
    for s in ov.get("sleeves") or []:
        for b in s.get("bots") or []:
            if b.get("phase") == "FLAT" or not float(b.get("eth") or 0):
                continue
            e, q = float(b.get("entry") or 0), float(b.get("eth") or 0)
            u = (px - e) * q if e and px else 0
            pc = (px / e - 1) * 100 if e and px else 0
            stop = f", стоп {_f(b['stop'])}" if b.get("stop") else ""
            pos.append(f"• {s.get('sleeve')} #{b.get('id')}: {q:.4f} ETH по {_f(e)} → "
                       f"{'+' if u >= 0 else ''}{_f(u)} $ ({'+' if pc >= 0 else ''}{pc:.1f} %){stop}")
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


class TelegramControl:
    def __init__(self, token: str, chat_id: str, home: Path, overview, trades, journal=None,
                 api: str = "https://api.telegram.org"):
        self.token, self.chat, self.home = token.strip(), str(chat_id).strip(), home
        self.overview, self.trades, self.j, self.api = overview, trades, journal, api
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

    def skip_backlog(self) -> None:
        """Повідомлення, надіслані поки агент не працював, не виконуються."""
        res = self._call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15) or []
        if res:
            self.offset = res[-1]["update_id"] + 1

    # --- команди --------------------------------------------------------------------------
    def handle(self, text: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        cmd = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
        cmd = cmd.split("@")[0].lstrip("/")
        if cmd in ("status", "s", "статус"):
            return status_text(self.overview())
        if cmd in ("trades", "угоди"):
            return trades_text(self.trades())
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
            self.send(self.handle(m.get("text", "")))

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


def start(agent, sec: dict, home: Path):
    """Запустити керування з Telegram, якщо задано токен і chat id."""
    if not (sec.get("TELEGRAM_TOKEN") and sec.get("TELEGRAM_CHAT_ID")):
        return None
    from agent.live import panel_api as api
    from agent.live.status_server import overview
    tc = TelegramControl(sec["TELEGRAM_TOKEN"], sec["TELEGRAM_CHAT_ID"], home,
                         overview=lambda: overview(agent), trades=lambda: api.trades(agent.j, 5), journal=agent.j)
    tc.start()
    return tc
