"""Журнал (SQLite) і збереження стану між перезапусками. Лише стандартна бібліотека."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from decimal import Decimal
from enum import Enum
from pathlib import Path

from agent.strategy import v3

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (ts INTEGER, level TEXT, kind TEXT, msg TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS orders (ts INTEGER, coid TEXT, bot INTEGER, side TEXT, status TEXT, qty TEXT,
                                   price TEXT, fee TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS trades (bot INTEGER, entry_ts INTEGER, exit_ts INTEGER, entry_px TEXT, exit_px TEXT,
                                   qty TEXT, pnl TEXT, reason TEXT);
"""


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # сторінка статусу читає журнал з іншого потоку — один з'єднання під замком
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)

    def event(self, level: str, kind: str, msg: str, **data) -> None:
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                            (int(time.time() * 1000), level, kind, msg, json.dumps(data, default=str, ensure_ascii=False)))
            self.db.commit()

    def order(self, ts: int, coid: str, bot: int, side: str, status: str, qty=None, price=None, fee=None,
              detail: str = "") -> None:
        with self.lock:
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                            (ts, coid, bot, side, status, str(qty or ""), str(price or ""), str(fee or ""), detail))
            self.db.commit()

    def trade(self, bot: int, entry_ts: int, exit_ts: int, entry_px, exit_px, qty, pnl, reason: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
                            (bot, entry_ts, exit_ts, str(entry_px), str(exit_px), str(qty), str(pnl), reason))
            self.db.commit()

    def recent_events(self, n: int = 20) -> list[tuple]:
        with self.lock:
            return self.db.execute("SELECT ts, level, kind, msg FROM events ORDER BY rowid DESC LIMIT ?",
                                   (n,)).fetchall()

    def recent_trades(self, n: int = 20) -> list[tuple]:
        with self.lock:
            return self.db.execute("SELECT * FROM trades ORDER BY rowid DESC LIMIT ?", (n,)).fetchall()


# --- стан ----------------------------------------------------------------------------------

def _enc(x):
    if isinstance(x, Decimal):
        return {"__d": str(x)}
    if isinstance(x, Enum):
        return x.value
    if dataclasses.is_dataclass(x):
        return {k: _enc(v) for k, v in dataclasses.asdict(x).items()}
    if isinstance(x, dict):
        return {str(k): _enc(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_enc(v) for v in x]
    return x


def _dec(x):
    if isinstance(x, dict):
        if set(x) == {"__d"}:
            return Decimal(x["__d"])
        return {k: _dec(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_dec(v) for v in x]
    return x


def save_state(path: Path, state: v3.V3State, extra: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"bots": _enc(state.bots), "extra": _enc(extra)}, ensure_ascii=False, indent=1))
    tmp.replace(path)                       # атомарно: не лишає напівзаписаного файла


def load_state(path: Path) -> tuple[v3.V3State, dict] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    bots = []
    for b in _dec(raw["bots"]):
        b["phase"] = v3.Phase(b["phase"])
        bots.append(v3.BotV3(**b))
    return v3.V3State(bots=bots), _dec(raw.get("extra", {}))
