"""Сповіщення в Telegram (необов'язково). Без токена — лише вивід у консоль."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request


class Notifier:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token, self.chat = token, chat_id

    def send(self, text: str) -> None:
        print(time.strftime("%H:%M:%S"), text, flush=True)
        if not (self.token and self.chat):
            return
        data = urllib.parse.urlencode({"chat_id": self.chat, "text": text}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage", data=data, timeout=10).read()
        except Exception:  # noqa: BLE001 — сповіщення не має зупиняти торгівлю
            pass


def chat_ids(token: str) -> list[tuple[str, str]]:
    """Допомога при налаштуванні: chat id з останніх повідомлень боту."""
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10) as r:
        upd = json.loads(r.read().decode()).get("result", [])
    out = []
    for u in upd:
        ch = (u.get("message") or {}).get("chat") or {}
        if ch:
            out.append((str(ch.get("id")), ch.get("first_name") or ch.get("title") or ""))
    return out
