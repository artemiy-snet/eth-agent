"""Просування після кожної збірки сайту (викликає deploy/update.sh):

- IndexNow: нові або змінені адреси з sitemap.xml → api.indexnow.org (Bing, Yandex, Seznam та інші підхоплюють одразу).
  Ключ — INDEXNOW_KEY (32 hex), файл <ключ>.txt лежить у корені сайту (його пише build.py).
- Telegram-канал: нова стаття → пост із заголовком, анонсом і посиланням. Вмикається лише коли задано
  TELEGRAM_BOT_TOKEN і TELEGRAM_CHANNEL_ID (у /etc/criptobot/site.env); без них — нічого не робить.

Стан (що вже оголошено) — DATA_DIR/announced.json. Лише stdlib.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sitemap_urls(www: Path) -> dict[str, str]:
    try:
        x = (www / "sitemap.xml").read_text(encoding="utf-8")
    except OSError:
        return {}
    return dict(re.findall(r"<loc>([^<]+)</loc><lastmod>([^<]+)</lastmod>", x))


def indexnow(host: str, key: str, urls: list[str], opener=urllib.request.urlopen) -> int:
    if not urls:
        return 0
    body = json.dumps({"host": host, "key": key, "keyLocation": f"https://{host}/{key}.txt", "urlList": urls[:10000]})
    req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body.encode(), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with opener(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def telegram_post(token: str, channel: str, article: dict, base: str, opener=urllib.request.urlopen) -> bool:
    """Пост у канал: обкладинка + заголовок + анонс + посилання. Повертає True при успіху."""
    url = f"{base}/{article['slug']}.html"
    text = f"<b>{_h(article['title'])}</b>\n\n{_h(article.get('lead', ''))}\n\n<a href=\"{url}\">Читати в журналі →</a>"
    params = {"chat_id": channel, "photo": f"{base}/assets/media/{article.get('cover', 'robot')}.jpg",
              "caption": text[:1024], "parse_mode": "HTML"}
    data = urllib.parse.urlencode(params).encode()
    try:
        with opener(f"https://api.telegram.org/bot{token}/sendPhoto", data=data, timeout=20) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except (urllib.error.URLError, ValueError, TimeoutError):
        return False


def _h(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run(www: Path, data: Path, env: dict | None = None, opener=urllib.request.urlopen) -> dict:
    env = env if env is not None else dict(os.environ)
    sys.path.insert(0, str(HERE))
    import build
    host = build.CFG["domain"]
    base = f"https://{host}"
    state_p = data / "announced.json"
    try:
        st = json.loads(state_p.read_text())
    except (OSError, ValueError):
        st = {}
    first = not st
    urls = sitemap_urls(www)
    out = {"indexnow": 0, "telegram": 0}
    key = env.get("INDEXNOW_KEY", "")
    changed = [u for u, lm in urls.items() if st.get("urls", {}).get(u) != lm]
    if re.fullmatch(r"[0-9a-f]{32}", key) and changed and (www / f"{key}.txt").exists():
        code = indexnow(host, key, changed, opener)
        out["indexnow"] = len(changed) if code in (200, 202) else 0
        out["indexnow_status"] = code
        if code not in (200, 202):
            changed = []                              # не запам'ятовуємо — спробуємо наступного разу
    st.setdefault("urls", {}).update({u: urls[u] for u in changed})
    # Telegram: лише нові статті; при першому запуску старі просто запам'ятовуємо
    arts = build.articles()
    done = set(st.get("posted", []))
    tok, chan = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHANNEL_ID", "")
    if first or not (tok and chan):
        if first:
            done |= {a["slug"] for a in arts}
    else:
        for a in sorted(arts, key=lambda a: a["ts"]):
            if a["slug"] in done:
                continue
            if telegram_post(tok, chan, a, base, opener):
                done.add(a["slug"])
                out["telegram"] += 1
                time.sleep(1)
            if out["telegram"] >= 3:                  # не більше трьох постів за раз
                break
    st["posted"] = sorted(done)
    data.mkdir(parents=True, exist_ok=True)
    state_p.write_text(json.dumps(st, ensure_ascii=False))
    return out


if __name__ == "__main__":
    r = run(Path(os.environ.get("WWW_DIR", "/var/www/criptobot")), Path(os.environ.get("DATA_DIR", "/var/lib/criptobot")))
    print(f"просування: IndexNow {r['indexnow']} адрес" + (f" (код {r.get('indexnow_status')})" if "indexnow_status" in r else "")
          + f", Telegram {r['telegram']} постів")
