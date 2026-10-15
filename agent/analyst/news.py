"""Заголовки новин з RSS (лише стандартна бібліотека — працює в Termux).

Новини — це дані для аналітика, а не інструкції: текст заголовків передається в Claude як
цитований матеріал, і схема відповіді не дозволяє нічого, крім оцінки режиму.
"""

from __future__ import annotations

import email.utils
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]

_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Headline:
    ts: int            # мс UTC; 0 — невідомо
    source: str
    title: str
    summary: str


def _text(el, *names) -> str:
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or "").strip():
            return html.unescape(_TAG.sub(" ", x.text)).strip()
    return ""


def _ts(s: str) -> int:
    if not s:
        return 0
    try:
        return int(email.utils.parsedate_to_datetime(s).timestamp() * 1000)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def parse(xml_bytes: bytes, source: str) -> list[Headline]:
    root = ET.fromstring(xml_bytes)
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in items:
        title = _text(it, "title", "{http://www.w3.org/2005/Atom}title")
        if not title:
            continue
        summary = _text(it, "description", "{http://www.w3.org/2005/Atom}summary")[:300]
        ts = _ts(_text(it, "pubDate", "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published"))
        out.append(Headline(ts, source, title[:200], summary))
    return out


def fetch(feeds: list[str], since_ms: int, limit: int = 40, timeout: float = 15) -> tuple[list[Headline], list[str]]:
    """Повертає (заголовки новіші за since_ms, найсвіжіші першими; помилки джерел)."""
    heads, errors = [], []
    for url in feeds:
        src = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "eth-agent/1.0 (+rss)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                heads += parse(r.read(), src)
        except Exception as e:  # noqa: BLE001 — недоступне джерело не зупиняє аналітика
            errors.append(f"{src}: {type(e).__name__}")
    seen, uniq = set(), []
    for h in sorted(heads, key=lambda h: -h.ts):
        key = h.title.lower()
        if key in seen or (h.ts and h.ts < since_ms):
            continue
        seen.add(key)
        uniq.append(h)
    return uniq[:limit], errors
