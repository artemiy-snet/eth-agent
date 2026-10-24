"""Збирає сайт-журнал criptobot.store з репозиторію.

    python3 site/build.py [--out site/_out]

Головна — «фільм» зі сцен рекламного ролика (робот, ШІ-щит, червона кнопка, ніч, ранок), далі ранковий журнал:
статті (site/articles/*.md + згенеровані «редакцією» з ARTICLES_DIR), хроніка змін (з історії комітів), міфи
(розділи docs/IDEAS.md), документація, форум і чат. Залежність — пакет markdown (на сервері python3-markdown).
Сервер раз на кілька хвилин робить git pull і перезбирає сайт (site/deploy/update.sh).
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
CFG = json.loads((SITE / "config.json").read_text(encoding="utf-8"))
GH = f"https://github.com/{CFG['repo']}"
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня",
          "листопада", "грудня"]
MONTHS_NOM = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень",
              "жовтень", "листопад", "грудень"]
COVERS = ["robot", "brain", "button", "desk", "night", "morning", "finale"]

SKIP = re.compile(r"^(Merge|ci[:(]|test[:(]|chore|wip\b|[\w./-]+\.(ya?ml|sh|py|json|css|js):)", re.I)
TRAILER = re.compile(r"^(Co-Authored-By|Claude-Session|Signed-off-by):", re.I)


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def date_uk(ts: int) -> str:
    t = time.gmtime(ts)
    return f"{t.tm_mday} {MONTHS[t.tm_mon - 1]} {t.tm_year}"


# --- хроніка (коміти) ---------------------------------------------------------------------------
def news(limit: int | None = None) -> list[dict]:
    """Хроніка = коміти в main (заголовок + опис), від нових до старих."""
    try:
        raw = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H%x1f%ad%x1f%s%x1f%b%x1e", "--date=unix"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    out = []
    for rec in raw.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 4:
            continue
        sha, ts, subj, body = parts
        subj = subj.strip()
        if not subj or SKIP.match(subj):
            continue
        body = "\n".join(l for l in body.strip().splitlines() if not TRAILER.match(l.strip())).strip()
        m = re.match(r"^(v\d+\.\d+\.\d+(?:-\w+)?):\s*(.*)$", subj)
        kind = "release" if m else "ideas" if "IDEAS" in subj else \
            "backtest" if re.search(r"бектест|backtest", subj, re.I) else "update"
        title = m.group(2) if m else re.sub(r"^[\w./-]+:\s+", "", subj)     # «docs/IDEAS.md: …» → «…»
        title = title[:1].upper() + title[1:]
        out.append({"sha": sha, "ts": int(ts), "title": title, "version": m.group(1) if m else "",
                    "body": body, "kind": kind})
        if limit and len(out) >= limit:
            break
    return out


KIND = {"release": "Нова версія", "backtest": "Бектест", "ideas": "Розбір міфу", "update": "Зміни"}


def chronicle_item(n: dict, full: bool = True) -> str:
    ver = ' <span class="ver">%s</span>' % esc(n["version"]) if n["version"] else ""
    body = "<p>%s</p>" % esc(n["body"]).replace("\n", "<br>") if full and n["body"] else ""
    src = '<a class="src" href="%s/commit/%s" rel="noopener">Зміни в коді</a>' % (GH, n["sha"]) if full else ""
    return ('<li class="chron k-%s" id="n-%s"><time>%s</time><div><span class="kind">%s</span>%s<h3>%s</h3>%s%s</div></li>'
            % (n["kind"], n["sha"][:7], date_uk(n["ts"]), KIND[n["kind"]], ver, esc(n["title"]), body, src))


def rss(items: list[dict], arts: list[dict]) -> str:
    base = f"https://{CFG['domain']}"
    rows = [(a["ts"], a["title"], f"{base}/{a['slug']}.html", a["lead"], a["slug"]) for a in arts] + \
           [(n["ts"], (n["version"] + ": " if n["version"] else "") + n["title"], f"{base}/news.html#n-{n['sha'][:7]}",
             n["body"][:1500], n["sha"]) for n in items[:30]]
    rows.sort(key=lambda r: -r[0])
    it = "".join(f"<item><title>{esc(t)}</title><link>{l}</link><guid>{esc(g)}</guid>"
                 f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(ts))}</pubDate>"
                 f"<description>{esc(d)}</description></item>" for ts, t, l, d, g in rows[:40])
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{esc(CFG["title"])}</title>'
            f"<link>{base}</link><description>{esc(CFG['tagline'])}</description>{it}</channel></rss>")


# --- markdown -----------------------------------------------------------------------------------
def md_to_html(text: str) -> str:
    import markdown
    doc_map = {f: slug for slug, f, _ in CFG["docs"]}

    def link(m):
        label, href = m.group(1), m.group(2)
        name = href.split("#")[0].split("/")[-1]
        anchor = "#" + href.split("#", 1)[1] if "#" in href else ""
        if name in doc_map:
            return f"[{label}]({doc_map[name]}.html{anchor})"
        if not re.match(r"^(https?:|mailto:|#)", href):
            path = href.lstrip("./")
            path = path if path.startswith(("agent/", "scripts/", "tests/", ".github/", "config", "site/")) else "docs/" + path
            return f"[{label}]({GH}/blob/main/{path})"
        return m.group(0)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "toc"])


# --- статті -------------------------------------------------------------------------------------
def parse_article(path: Path) -> dict | None:
    t = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", t, re.S)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip():
            meta[k.strip()] = v.strip()
    try:
        ts = calendar.timegm(time.strptime(meta.get("date", ""), "%Y-%m-%d")) + 12 * 3600   # полудень UTC — без зсуву дати
    except ValueError:
        ts = int(path.stat().st_mtime)
    cover = meta.get("cover", "robot")
    return {"slug": "a-" + re.sub(r"[^\w-]", "", path.stem), "title": meta.get("title", path.stem),
            "rubric": meta.get("rubric", "Статті"), "lead": meta.get("lead", ""), "ts": ts,
            "cover": cover if cover in COVERS else "robot", "body": m.group(2).strip(), "author": meta.get("author", ""),
            "pin": meta.get("pin", "").lower() in ("true", "так", "1")}


def articles() -> list[dict]:
    dirs = [SITE / "articles"] + ([Path(os.environ["ARTICLES_DIR"])] if os.environ.get("ARTICLES_DIR") else [])
    out = [a for d in dirs if d.exists() for p in sorted(d.glob("*.md")) if (a := parse_article(p))]
    return sorted(out, key=lambda a: (not a["pin"], -a["ts"]))         # закріплена — головна історія випуску


def myths() -> list[dict]:
    """Розділи «## N. …» з docs/IDEAS.md — окремі короткі статті рубрики «Розвінчуємо міф»."""
    p = ROOT / "docs" / "IDEAS.md"
    if not p.exists():
        return []
    out = []
    for m in re.finditer(r"^## (\d+)\. (.+?)\n(.*?)(?=^## |\n---\n\n\*Бета|\Z)", p.read_text(encoding="utf-8"), re.S | re.M):
        body = m.group(3).strip()
        verdict = re.search(r"\*\*(?:Чому ні|Бектест[^*]*|Але питання слушне[^*]*)\.?\*\*", body)
        lead = re.sub(r"\*\*|\[|\]\([^)]*\)", "", body.split("\n\n")[0])[:220]
        out.append({"n": int(m.group(1)), "slug": f"myth-{m.group(1)}", "title": m.group(2).strip(), "body": body,
                    "lead": lead, "rejected": verdict is not None or "відхилено" in body.lower()})
    return out


# --- щоденник бота ------------------------------------------------------------------------------
def load_diary() -> tuple[dict | None, dict]:
    d = Path(os.environ.get("DATA_DIR", "/var/lib/criptobot"))
    try:
        snap = json.loads((d / "diary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snap = None
    try:
        days = json.loads((d / "diary_days.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        days = {}
    return snap, days


def kyiv(ts_ms: float, fmt: str = "%d.%m.%Y %H:%M") -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts_ms / 1000, ZoneInfo("Europe/Kyiv")).strftime(fmt)
    except Exception:  # noqa: BLE001 — без бази часових поясів
        return time.strftime(fmt, time.gmtime(ts_ms / 1000)) + " UTC"


def pct(x, d: int = 1) -> str:
    if x is None:
        return "—"
    return ("+" if x > 0 else "−" if x < 0 else "") + f"{abs(x):.{d}f}".replace(".", ",") + " %"


def svg_chart(eq: list, h: int = 300, small: bool = False) -> str:
    """Лінії «портфель» і «просто тримати ETH» (індекс, старт = 100) — inline SVG, адаптивний."""
    if len(eq) < 2:
        return '<p class="muted">Графік з\'явиться, щойно назбирається кілька точок.</p>'
    w, pad = 900, (8 if small else 36)
    xs = [p[0] for p in eq]
    ys = [v for p in eq for v in (p[1], p[2])] + [100]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    m = (y1 - y0) * 0.12 or 1
    y0, y1 = y0 - m, y1 + m
    X = lambda x: pad + (x - x0) / ((x1 - x0) or 1) * (w - 2 * pad)
    Y = lambda y: h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad)
    line = lambda i: " ".join(f"{X(p[0]):.1f},{Y(p[i]):.1f}" for p in eq)
    base = f'<line x1="{pad}" x2="{w - pad}" y1="{Y(100):.1f}" y2="{Y(100):.1f}" class="base"/>'
    labels = "" if small else (
        f'<text x="{pad}" y="{h - 8}" class="lbl">{date_uk(x0 // 1000)}</text>'
        f'<text x="{w - pad}" y="{h - 8}" class="lbl" text-anchor="end">{date_uk(x1 // 1000)}</text>'
        f'<text x="{w - pad + 2}" y="{Y(eq[-1][1]) + 4:.1f}" class="lbl pf" text-anchor="end" dy="-10">{pct(eq[-1][1] - 100)}</text>'
        f'<text x="{w - pad + 2}" y="{Y(eq[-1][2]) + 4:.1f}" class="lbl eth" text-anchor="end" dy="16">ETH {pct(eq[-1][2] - 100)}</text>')
    return (f'<svg class="chart{" small" if small else ""}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="Капітал бота проти ETH: {pct(eq[-1][1] - 100)} і {pct(eq[-1][2] - 100)} від старту">'
            f'{base}<polyline class="eth" points="{line(2)}"/><polyline class="pf" points="{line(1)}"/>'
            f'<circle class="pf" cx="{X(eq[-1][0]):.1f}" cy="{Y(eq[-1][1]):.1f}" r="5"/>{labels}</svg>')


REGIME = {"NORMAL": "звичайний режим", "CAUTION": "обережно — покупки вдвічі менші", "RISK_OFF": "без нових покупок"}


def diary_stats(snap: dict) -> dict:
    eq = snap.get("equity") or []
    tr = [t for t in snap.get("trades") or [] if t.get("pnl_pct") is not None]
    wins = sum(1 for t in tr if t["pnl_pct"] > 0)
    since = snap.get("since") or (eq[0][0] if eq else snap.get("ts"))
    return {"pf": eq[-1][1] - 100 if eq else None, "eth": eq[-1][2] - 100 if eq else None,
            "days": max(1, int((snap["ts"] - since) / 86_400_000) + 1), "since": since, "n": len(tr),
            "win": (wins / len(tr) * 100) if tr else None,
            "regime": (snap.get("ai") or {}).get("regime")}


def promo_block() -> str:
    """Рекламний/партнерський блок (site/config.json → promo) і кнопка «Дякую» (donate_url). Порожньо — нічого не показуємо."""
    out = ""
    p = CFG.get("promo") or {}
    if p.get("text") and p.get("url"):
        out += (f'<aside class="promo"><span class="tag">{esc(p.get("label") or "Реклама")}</span><p>{esc(p["text"])}</p>'
                f'<a class="btn" href="{esc(p["url"])}" rel="sponsored noopener" target="_blank">{esc(p.get("button") or "Детальніше")}</a></aside>')
    if CFG.get("donate_url"):
        out += (f'<aside class="promo thanks"><p>Журнал і бот безкоштовні та з відкритим кодом. Якщо проєкт тобі корисний — '
                f'можна подякувати.</p><a class="btn" href="{esc(CFG["donate_url"])}" rel="noopener" target="_blank">Подякувати</a></aside>')
    return out


def diary_block(snap: dict | None) -> str:
    """Блок на головній: міні-графік і три числа."""
    if not snap:
        return ""
    st = diary_stats(snap)
    return f"""<a class="diary-card" href="diary.html"><div><span class="rubric">Щоденник бота</span>
<h3>День {st['days']} на справжніх грошах</h3>
<p class="nums"><b class="pf">{pct(st['pf'])}</b> бот <b class="eth">{pct(st['eth'])}</b> ETH <b>{st['n']}</b> угод</p>
<p class="muted small">Дані надходять від бота автоматично. Оновлено {kyiv(snap['ts'], '%d.%m %H:%M')} (Київ)</p></div>
{svg_chart(snap.get('equity') or [], 160, small=True)}</a>"""


def diary_page(snap: dict | None, days: dict) -> str:
    if not snap:
        return inner("Щоденник бота", '<div class="prose"><p>Щоденник з\'явиться, щойно бот надішле перші дані. '
                     'Він оновлюється сам кожні 30 хвилин.</p></div>', cover="night")
    st = diary_stats(snap)
    pos = "".join(f"<tr><td>{esc(str(p.get('sleeve')))}</td><td>{esc(str(p.get('coin')))}</td>"
                  f"<td>{date_uk(int(p['since']) // 1000) if p.get('since') else '—'}</td>"
                  f"<td class=\"{'pos' if (p.get('pnl_pct') or 0) >= 0 else 'neg'}\">{pct(p.get('pnl_pct'), 2)}</td></tr>"
                  for p in snap.get("positions") or [])
    pos = (f'<table><tr><th>Рукав</th><th>Монета</th><th>Відкрито</th><th>Зараз</th></tr>{pos}</table>' if pos
           else '<p class="muted">Відкритих позицій немає — бот у доларах і чекає свого сигналу.</p>')
    trs = "".join(f"<tr><td>{date_uk(int(t['exit_ts']) // 1000)}</td><td>{esc(str(t.get('sleeve') or '—'))}</td>"
                  f"<td class=\"{'pos' if (t.get('pnl_pct') or 0) >= 0 else 'neg'}\">{pct(t.get('pnl_pct'), 2)}</td>"
                  f"<td>{esc(str(t.get('reason') or ''))}</td></tr>" for t in snap.get("trades") or [])
    trs = (f'<table><tr><th>Закрито</th><th>Рукав</th><th>Результат</th><th>Причина</th></tr>{trs}</table>' if trs
           else '<p class="muted">Закритих угод ще немає. Бот торгує рідко — одна-дві угоди на місяць на рукав.</p>')
    log = []
    since_day = time.strftime("%Y-%m-%d", time.gmtime(st["since"] / 1000))
    for day, d in sorted(days.items(), reverse=True)[:60]:
        n = (calendar.timegm(time.strptime(day, "%Y-%m-%d")) - calendar.timegm(time.strptime(since_day, "%Y-%m-%d"))) // 86400 + 1
        if n < 1:
            continue
        tr = d.get("trades") or []
        tr_s = ("Закрито угод: " + ", ".join(f"{esc(str(t.get('sleeve') or ''))} {pct(t.get('pnl_pct'), 2)}" for t in tr) + ". ") if tr else ""
        reg = REGIME.get(d.get("regime") or "", "")
        log.append(f'<li><time>День {n} · {date_uk(calendar.timegm(time.strptime(day, "%Y-%m-%d")))}</time>'
                   f'<p>Бот {pct((d.get("idx") or 100) - 100)} від старту, ETH {pct((d.get("eth") or 100) - 100)}. {tr_s}'
                   f'Відкритих позицій: {d.get("positions", 0)}.{" ШІ: " + reg + "." if reg else ""}'
                   f'{" ⛔ Спрацював запобіжник." if d.get("killed") else ""}</p></li>')
    body = f"""<div class="stats"><div><b class="pf">{pct(st['pf'])}</b><span>бот від старту</span></div>
<div><b class="eth">{pct(st['eth'])}</b><span>просто тримати ETH</span></div>
<div><b>{st['days']}</b><span>днів на справжніх грошах</span></div>
<div><b>{st['n']}</b><span>закритих угод{f", виграшних {st['win']:.0f} %" if st['win'] is not None else ""}</span></div></div>
{svg_chart(snap.get('equity') or [], 420)}
<p class="legend"><span class="pf">Бот</span> <span class="eth">Просто тримати ETH</span> Обидві лінії — індекс, старт = 100.
Зміна суми, з якою працює бот, не рахується прибутком чи збитком.</p>
<h2 class="section">Відкриті позиції</h2>{pos}
<h2 class="section">Закриті угоди</h2>{trs}
<h2 class="section">День за днем</h2><ol class="daylog">{''.join(log) or '<li><p class="muted">Записи з\'являться з першого повного дня.</p></li>'}</ol>
<p class="muted small">Публікуються лише відсотки: суми в доларах, кількість монет, ключі й адреси не передаються.
ШІ-режим: {esc(REGIME.get(st['regime'] or '', st['regime'] or '—'))}. Версія бота {esc(str(snap.get('version', '')))}.
Оновлено {kyiv(snap['ts'])} (Київ).</p>"""
    return inner("Щоденник бота", body, "Наживо", "night",
                 "Справжні гроші, справжні угоди. Бот сам надсилає сюди свій стан кожні 30 хвилин.")


# --- каркас сторінки ----------------------------------------------------------------------------
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;600;800&family=Golos+Text:wght@400;500;600&display=swap" rel="stylesheet">')
THEME_JS = ("<script>try{var t=localStorage.getItem('cb_theme');if(t)document.documentElement.dataset.theme=t}catch(e){}</script>")


def nav() -> str:
    items = [("index", "Журнал"), ("diary", "Щоденник бота"), ("articles", "Статті"), ("news", "Хроніка"), ("myths", "Міфи"),
             ("install", "Встановлення"), ("forum", "Форум"), ("chat", "Запитати ШІ")]
    return "".join(f'<a href="{k}.html">{t}</a>' for k, t in items)


def issue_line() -> str:
    t = time.gmtime()
    n = (t.tm_year - 2026) * 12 + t.tm_mon - 10 + 1          # випуск 1 — жовтень 2026, далі щомісяця
    return f"Випуск {n}, {MONTHS_NOM[t.tm_mon - 1]} {t.tm_year}"


def page(slug: str, title: str, body: str, desc: str = "", home: bool = False, og_img: str = "robot") -> str:
    d = esc(desc or CFG["tagline"])
    return f"""<!doctype html><html lang="uk"{' class="home"' if home else ''}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(title)}{'' if home else ' — ' + esc(CFG['title'])}</title><meta name="description" content="{d}">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{d}">
<meta property="og:image" content="https://{CFG['domain']}/assets/media/{og_img}.jpg"><meta name="theme-color" content="#07121A">
{THEME_JS}{FONTS}<link rel="stylesheet" href="assets/style.css"><link rel="alternate" type="application/rss+xml" href="rss.xml" title="{esc(CFG['title'])}">
<link rel="icon" href="assets/icon.svg" type="image/svg+xml">
</head><body>
<header class="bar"><a class="mark" href="index.html"><span class="eye"></span>ETH-Агент</a>
<nav id="nav">{nav()}</nav>
<button class="theme" type="button" aria-label="Змінити тему" title="Ніч / ранок"></button>
<button class="burger" type="button" aria-label="Меню" aria-controls="nav" aria-expanded="false"><i></i><i></i></button></header>
{body}
<footer class="foot"><div class="col"><p class="warn"><b>Бета-експеримент. Не фінансова порада.</b> Торгівля криптовалютою може
призвести до втрати всіх вкладених коштів. Результати бектестів — минуле, яке не гарантує майбутнього.</p>
<p><a href="{GH}">Код на GitHub</a>, ліцензія MIT. <a href="rss.xml">RSS</a>.</p></div></footer>
<a class="fab" href="chat.html" aria-label="Запитати ШІ про бота"><span class="eye"></span></a>
<script src="assets/site.js" defer></script>
</body></html>"""


def media(name: str, cls: str, lazy: bool = True) -> str:
    return (f'<video class="{cls}" muted loop playsinline preload="none" poster="assets/media/{name}.jpg" '
            f'data-src="assets/media/{name}.mp4" aria-hidden="true"></video>')


def scene(sid: str, clip: str, cap: str, sub: str, story: str, tone: str = "") -> str:
    return f"""<section class="scene {tone}" id="{sid}">
<div class="stage">{media(clip, 'bg')}<div class="veil"></div>
<h2 class="cap">{cap}</h2><figure class="frame">{media(clip, 'fg')}</figure><p class="sub">{sub}</p></div>
<article class="story">{story}</article></section>"""


# --- головна: фільм + ранковий журнал --------------------------------------------------------------
def index_page(items: list[dict], arts: list[dict], ms: list[dict], snap: dict | None = None) -> str:
    cover = f"""<section class="scene cover on" id="top">
<div class="stage">{media('robot', 'bg')}<div class="veil"></div>
<div class="masthead"><p class="issue">{issue_line()}</p><h1 class="title">ETH-Агент</h1>
<p class="motto">журнал про бота, що торгує, поки ти спиш</p></div>
<figure class="frame">{media('robot', 'fg')}</figure>
<p class="question">Чи може звичайний телефон торгувати криптовалютою сам?</p>
<a class="down" href="#ai" aria-label="Гортати далі"><span></span></a></div></section>"""

    s_ai = scene("ai", "brain", "ШІ стежить за ринком", "і може лише зменшити ризик", """
<h3>Аналітик із гальмом замість газу</h3>
<p>Торгує не ШІ, а код за перевіреними правилами. Claude раз на 12 годин читає новини, стан ринку й позиції — і позачергово,
якщо ETH за чотири години рухнув на 5 % чи спрацював запобіжник.</p>
<p>У нього три відповіді: <b>NORMAL</b> — працюємо як завжди, <b>CAUTION</b> — нові покупки вдвічі менші,
<b>RISK_OFF</b> — нових покупок немає. Збільшити ризик він не може за жодних умов.</p>""")

    s_btn = scene("stop", "button", "Червона кнопка", "зупинка одним дотиком", """
<h3>Запобіжник, що не питає дозволу</h3>
<p>Kill switch зупиняє нові входи миттєво: кнопкою в панелі, командою <code>/stop</code> у Telegram або сам — коли
просадка перейшла межу, гроші на біржі не зійшлися з обліком бота більш ніж на 0,5 % чи ціна летить понад 3 % за хвилину.</p>
<p>Стоп-ордери лишаються на самій біржі й спрацюють, навіть якщо телефон вимкнеться. Зняти зупинку може лише людина.</p>""", "red")

    s_desk = scene("rules", "desk", "Тільки спот", "без плеча й позик", """
<h3>Правила, перевірені на семи роках</h3>
<p>Жодна ідея не потрапляє в бота без бектесту на історії ETH з 2019 року, з комісіями й прослизанням. Ось що показала
перевірка — разом із просадками, про які зазвичай мовчать:</p>
<table><tr><th>Склад</th><th>На рік</th><th>Найгірше падіння</th></tr>
<tr><td>обережний</td><td>≈ 2,5 %</td><td>≈ 8 %</td></tr><tr><td>збалансований</td><td>≈ 22 %</td><td>≈ 50 %</td></tr>
<tr><td>агресивний</td><td>≈ 29 %</td><td>≈ 56 %</td></tr><tr><td>просто тримати ETH</td><td>≈ 46 %</td><td>≈ 79 %</td></tr></table>
<p class="note">Бот не обганяє «купив і тримай» на бичачому ринку. Його задача — падати менше.</p>""", "green")

    s_night = scene("night", "night", "Ти спиш — він працює", "Android, Termux, 24/7", """
<h3>Старий телефон на тумбочці</h3>
<p>Агент живе в Termux на звичайному Android. Сам запускається після перезавантаження, сам перезапускається після збою,
а зовнішній сторож напише в Telegram, якщо телефон замовк.</p>
<p>Поки бот чекає нагоди, його вільні долари лежать у гнучкому депозиті Binance і приносять відсотки, а комісія
сплачується в BNB — на чверть дешевше.</p>""", "warm")

    s_morning = scene("morning", "morning", "Вранці — звіт у Telegram", "/status, /chart, /equity", """
<h3>Кишеньковий пульт</h3>
<p>Бот пише про кожну покупку й продаж, а на запит — показує стан, графік ціни з точками входу і криву капіталу проти
«просто тримати ETH». Повна панель відкривається в браузері телефона.</p>""", "warm")

    feat, rest = (arts[0], arts[1:4]) if arts else (None, [])
    feature = f"""<a class="feature" href="{feat['slug']}.html"><img src="assets/media/{feat['cover']}.jpg" alt="" loading="lazy">
<div><span class="rubric">{esc(feat['rubric'])}</span><h3>{esc(feat['title'])}</h3><p>{esc(feat['lead'])}</p></div></a>""" if feat else ""
    cards = "".join(f"""<a class="acard" href="{a['slug']}.html"><img src="assets/media/{a['cover']}.jpg" alt="" loading="lazy">
<span class="rubric">{esc(a['rubric'])}</span><h4>{esc(a['title'])}</h4><time>{date_uk(a['ts'])}</time></a>""" for a in rest)
    chron = "".join(chronicle_item(n, full=False) for n in items[:5])
    mys = "".join(f'<li><a href="{m["slug"]}.html"><span class="num">{m["n"]}</span>{esc(m["title"])}</a></li>' for m in ms[:6])

    paper = f"""<div class="dawn" aria-hidden="true"></div>
<section class="paper" id="journal"><div class="col wide">
{diary_block(snap)}<h2 class="section">У цьому випуску</h2>{feature}<div class="cards">{cards}</div>
{promo_block()}<div class="split"><div><h2 class="section">Хроніка</h2><ol class="chronicle short">{chron}</ol>
<a class="more" href="news.html">Уся хроніка</a></div>
<div><h2 class="section">Розвінчуємо міфи</h2><p class="muted">Стратегії з TikTok, які нам надсилають, — і що показала перевірка.</p>
<ol class="myths">{mys}</ol><a class="more" href="myths.html">Усі розбори</a></div></div>
<div class="ask"><img src="assets/media/finale.jpg" alt="" loading="lazy"><div><h2>Маєш питання про бота?</h2>
<p>ШІ відповість за документацією проєкту: як встановити, налаштувати й що означає кожен параметр.</p>
<a class="btn" href="chat.html">Запитати ШІ</a> <a class="btn ghost" href="forum.html">На форум</a></div></div>
</div></section>"""

    finale = f"""<section class="scene finale" id="start">
<div class="stage">{media('finale', 'bg')}<div class="veil"></div>
<h2 class="cap">Почнемо?</h2><figure class="frame">{media('finale', 'fg')}</figure>
<p class="sub cta"><a class="btn" href="install.html">Встановити на телефон</a><a class="btn ghost" href="{GH}">Код на GitHub</a></p></div></section>"""
    return cover + s_ai + s_btn + s_desk + s_night + s_morning + paper + finale


# --- внутрішні сторінки -------------------------------------------------------------------------
def inner(title: str, body: str, kicker: str = "", cover: str | None = None, lead: str = "") -> str:
    hero = (f'<div class="hero-img"><img src="assets/media/{cover}.jpg" alt=""></div>' if cover else "")
    k = f'<span class="rubric">{esc(kicker)}</span>' if kicker else ""
    ld = f'<p class="lead">{esc(lead)}</p>' if lead else ""
    return f'<main class="paper inner">{hero}<div class="col">{k}<h1>{esc(title)}</h1>{ld}{body}</div></main>'


def article_page(a: dict) -> str:
    by = f'<p class="by">{esc(a["author"])}</p>' if a.get("author") else ""
    return inner(a["title"], f'<p class="dateline">{date_uk(a["ts"])}</p>{by}<div class="prose drop">{md_to_html(a["body"])}</div>'
                             f'{promo_block()}',
                 a["rubric"], a["cover"], a["lead"])


def articles_page(arts: list[dict]) -> str:
    cards = "".join(f"""<a class="acard" href="{a['slug']}.html"><img src="assets/media/{a['cover']}.jpg" alt="" loading="lazy">
<span class="rubric">{esc(a['rubric'])}</span><h4>{esc(a['title'])}</h4><p>{esc(a['lead'])}</p><time>{date_uk(a['ts'])}</time></a>""" for a in arts)
    return inner("Статті", f'<div class="cards">{cards}</div>', cover=None,
                 lead="Історії про те, як працює бот, що зламалося і чого ми навчилися.")


def myths_page(ms: list[dict]) -> str:
    rows = "".join(f'<li><a href="{m["slug"]}.html"><span class="num">{m["n"]}</span><b>{esc(m["title"])}</b>'
                   f'<span class="muted">{esc(m["lead"])}</span></a></li>' for m in ms)
    return inner("Розвінчуємо міфи", f'<ol class="myths big">{rows}</ol>', "Рубрика", "desk",
                 "Кожна «секретна стратегія» з коротких відео проходить через бектест на семи роках історії. Ось вердикти.")


def chat_page() -> str:
    return inner("Запитати ШІ", """<div class="chat"><div id="log" class="log" aria-live="polite"></div>
<form id="ask"><textarea id="q" maxlength="800" rows="2" placeholder="Напр.: як встановити на Android? Що таке kill switch?" required></textarea>
<button class="btn" id="send">Надіслати</button></form><p id="note" class="muted small"></p>
<p class="muted small">Відповідає Claude за документацією проєкту. Питання не по темі й фінансові поради — ні.
Питання зберігаються анонімно, без IP, щоб покращувати документацію.</p></div><script src="assets/chat.js" defer></script>""",
                 cover="robot", lead="Як встановити, налаштувати, що означає параметр — питай.")


def forum_page() -> str:
    g = CFG.get("giscus") or {}
    embed = (f'<div class="giscus"></div><script src="https://giscus.app/client.js" data-repo="{esc(CFG["repo"])}" '
             f'data-repo-id="{esc(g["repo_id"])}" data-category="{esc(g["category"])}" data-category-id="{esc(g["category_id"])}" '
             f'data-mapping="specific" data-term="Форум ETH-Агента" data-reactions-enabled="1" data-input-position="top" '
             f'data-theme="preferred_color_scheme" data-lang="uk" crossorigin="anonymous" async></script>'
             if g.get("repo_id") and g.get("category_id") else "")
    return inner("Форум", f"""<div class="prose"><p>Обговорення живуть у GitHub Discussions: питання, ідеї, звіти про роботу бота у
вас. Щоб писати, потрібен безкоштовний акаунт GitHub.</p></div>
<p class="cta-row"><a class="btn" href="{GH}/discussions/new/choose">Створити тему</a><a class="btn ghost" href="{GH}/discussions">Усі теми</a>
<a class="btn ghost" href="{GH}/issues/new">Повідомити про помилку</a></p>{embed}""", cover="desk",
                 lead="Діліться досвідом, ставте питання, пропонуйте ідеї.")


def admin_page() -> str:
    return inner("Пульт редакції", """<div class="chat"><p class="muted">Напиши нотатку — що сталося з ботом, що помітив, що змінив.
Редакція (Claude) зробить з неї статтю, і вона з'явиться в журналі за 2–3 хвилини. Те саме — командою <code>/post текст</code>
у Telegram-боті.</p>
<form id="post"><input id="tok" type="password" placeholder="Токен редакції (з налаштування сервера)" autocomplete="off"
style="width:100%;margin:0 0 10px;padding:12px 14px;border-radius:14px;border:1px solid var(--line);background:var(--bg-2);color:var(--ink);font:inherit">
<textarea id="txt" rows="9" placeholder="Напр.: Сьогодні бот уперше продав ETH з прибутком…" required
style="width:100%;padding:13px 15px;border-radius:16px;border:1px solid var(--line);background:var(--bg-2);color:var(--ink);font:inherit"></textarea>
<button class="btn" id="go">Надіслати в редакцію</button></form><p id="res" class="muted"></p></div>
<script>(function(){var t=document.getElementById("tok");try{t.value=localStorage.getItem("cb_tok")||""}catch(e){}
document.getElementById("post").addEventListener("submit",async function(e){e.preventDefault();var r=document.getElementById("res"),b=document.getElementById("go");
try{localStorage.setItem("cb_tok",t.value)}catch(e){}b.disabled=true;r.textContent="Редакція пише статтю, це до хвилини…";
try{var x=await fetch("/api/post",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+t.value},
body:JSON.stringify({text:document.getElementById("txt").value})});var j=await x.json();
if(j.ok){r.innerHTML='Готово. Стаття з’явиться тут за 2–3 хв: <a href="'+j.url+'">'+j.url+'</a>';document.getElementById("txt").value=""}
else r.textContent=j.error||("Помилка "+x.status)}catch(err){r.textContent="Немає зв’язку з сервером."}b.disabled=false})})();</script>""",
                 lead="Нотатки автора стають статтями журналу.")


def news_page(items: list[dict]) -> str:
    return inner("Хроніка", f'<ol class="chronicle">{"".join(chronicle_item(n) for n in items)}</ol>',
                 lead="Кожна зміна в коді й документації — у день, коли вона сталася.")


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "kb").mkdir(parents=True)
    shutil.copytree(SITE / "assets", out / "assets")
    items, arts, ms = news(), articles(), myths()
    snap, days = load_diary()
    w = lambda name, html_: (out / name).write_text(html_, encoding="utf-8")
    w("index.html", page("index", f"{CFG['title']} — журнал", index_page(items, arts, ms, snap), home=True))
    w("diary.html", page("diary", "Щоденник бота", diary_page(snap, days), og_img="night"))
    w("news.html", page("news", "Хроніка", news_page(items)))
    w("articles.html", page("articles", "Статті", articles_page(arts)))
    w("myths.html", page("myths", "Розвінчуємо міфи", myths_page(ms), og_img="desk"))
    for a in arts:
        w(f"{a['slug']}.html", page(a["slug"], a["title"], article_page(a), a["lead"], og_img=a["cover"]))
    for m in ms:
        w(f"{m['slug']}.html", page(m["slug"], m["title"], inner(
            m["title"], f'<div class="prose">{md_to_html(m["body"])}</div><p class="more"><a href="myths.html">Усі розбори</a></p>',
            "Розвінчуємо міф", "desk"), m["lead"], og_img="desk"))
    for slug, fname, title in CFG["docs"]:
        src = ROOT / "docs" / fname
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        body_html = md_to_html(re.sub(r"^# .*\n", "", text, count=1))
        w(f"{slug}.html", page(slug, title, inner(title, f'<div class="prose doc">{body_html}</div>'
                                                         f'<p class="src"><a href="{GH}/blob/main/docs/{fname}">Джерело на GitHub</a></p>')))
        (out / "kb" / fname).write_text(text, encoding="utf-8")       # база знань для чату
    (out / "kb" / "README.md").write_text((ROOT / "README.md").read_text(encoding="utf-8"), encoding="utf-8")
    for a in arts:                                                    # статті журналу теж знає чат
        (out / "kb" / f"article-{a['slug']}.md").write_text(f"# {a['title']}\n\n{a['lead']}\n\n{a['body']}", encoding="utf-8")
    w("forum.html", page("forum", "Форум", forum_page(), og_img="desk"))
    w("chat.html", page("chat", "Запитати ШІ", chat_page()))
    w("admin.html", page("admin", "Пульт редакції", admin_page()).replace("<head>", '<head><meta name="robots" content="noindex">', 1))
    w("robots.txt", "User-agent: *\nDisallow: /kb/\nDisallow: /api/\nDisallow: /admin.html\n")
    w("rss.xml", rss(items, arts))
    print(f"сайт зібрано: {out} · статей {len(arts)} · міфів {len(ms)} · хроніка {len(items)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SITE / "_out"))
    build(Path(ap.parse_args().out))
