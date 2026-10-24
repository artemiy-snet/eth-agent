#!/usr/bin/env python3
"""Щогодини (cron, від root): розбір журналів nginx через GoAccess у JSON, без публічного доступу.

- /var/lib/criptobot/goaccess/report.json — повний звіт GoAccess за останні 14 днів журналів (для перегляду на сервері);
- /var/lib/criptobot/goaccess/bots_YYYY-MM-DD.json — по днях: скільки запитів від ботів/сканерів і топ назв
  (їх читає GET /api/stats). Перезаписуються сьогоднішній і вчорашній дні, старші за 90 днів видаляються.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOGS = Path("/var/log/nginx")
OUT = Path("/var/lib/criptobot/goaccess")
DATE = re.compile(r"\[(\d{2})/(\w{3})/(\d{4}):")
MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
SCANNER = re.compile(r"(zgrab|masscan|nmap|nikto|sqlmap|python-requests|go-http-client|curl/|wget/|libwww|scrapy|"
                     r"httpclient|okhttp|aiohttp|censys|shodan|expanse|nuclei|wpscan)", re.I)


def read_lines() -> list[str]:
    files = sorted(LOGS.glob("access.log*"), key=lambda p: p.stat().st_mtime)[-15:]
    out = []
    for f in files:
        opener = gzip.open if f.suffix == ".gz" else open
        try:
            with opener(f, "rt", encoding="utf-8", errors="replace") as fh:
                out.extend(fh.readlines())
        except OSError:
            continue
    return out


def goaccess_json(lines: list[str]) -> dict:
    if not lines or not shutil.which("goaccess"):
        return {}
    p = subprocess.run(["goaccess", "-", "--log-format=COMBINED", "-o", "json", "--no-progress", "--ignore-panel=OS"],
                       input="".join(lines), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {}


def crawlers(report: dict) -> dict[str, int]:
    """Назви ботів із панелі browsers → категорія Crawlers у звіті GoAccess."""
    out: dict[str, int] = {}
    for cat in (report.get("browsers") or {}).get("data", []):
        if str(cat.get("data", "")).lower().startswith("crawler"):
            for it in cat.get("items", []):
                out[str(it.get("data"))] = int((it.get("hits") or {}).get("count", 0))
    return out


def day_stats(lines: list[str]) -> dict:
    rep = goaccess_json(lines)
    top = crawlers(rep)
    scanners = defaultdict(int)                    # сканери, яких GoAccess не відносить до Crawlers
    for ln in lines:
        m = SCANNER.search(ln.rsplit('"', 2)[-2] if ln.count('"') >= 2 else "")
        if m:
            scanners["scanner:" + m.group(1).lower().rstrip("/")] += 1
    for k, v in scanners.items():
        top[k] = top.get(k, 0) + v
    return {"requests": sum(top.values()), "top": dict(sorted(top.items(), key=lambda x: -x[1])[:20])}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lines = read_lines()
    by_day = defaultdict(list)
    for ln in lines:
        m = DATE.search(ln)
        if m:
            by_day[f"{m.group(3)}-{MON.get(m.group(2), 1):02d}-{m.group(1)}"].append(ln)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    for day, ls in sorted(by_day.items()):
        f = OUT / f"bots_{day}.json"
        if day in (today, yday) or not f.exists():
            f.write_text(json.dumps(day_stats(ls), ensure_ascii=False))
    rep = goaccess_json(lines)
    if rep:
        (OUT / "report.json").write_text(json.dumps(rep, ensure_ascii=False))
    cutoff = time.time() - 90 * 86400
    for f in OUT.glob("bots_*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
    for f in OUT.iterdir():
        f.chmod(0o640)
    shutil.chown(OUT, "root", "criptobot")
    for f in OUT.iterdir():
        shutil.chown(f, "root", "criptobot")


if __name__ == "__main__":
    main()
