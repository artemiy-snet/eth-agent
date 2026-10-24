"""Збирає статичний сайт criptobot.store з репозиторію: головна, новини (з історії комітів), документи, форум, чат.

    python3 site/build.py [--out site/_out]

Залежності: python3 + markdown (на сервері — пакет python3-markdown). Сайт оновлюється сам: сервер раз на 5 хв робить
git pull і перезбирає (site/deploy/update.sh), тому все, що потрапляє в main, за кілька хвилин з'являється як новина.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
CFG = json.loads((SITE / "config.json").read_text(encoding="utf-8"))
GH = f"https://github.com/{CFG['repo']}"

# коміти, що не є новинами (службові)
SKIP = re.compile(r"^(Merge|ci[:(]|test[:(]|chore|wip\b|[\w./-]+\.ya?ml:)", re.I)
TRAILER = re.compile(r"^(Co-Authored-By|Claude-Session|Signed-off-by):", re.I)


def esc(s: str) -> str:
    return html.escape(s, quote=True)


# --- новини -------------------------------------------------------------------------------------
def news(limit: int | None = None) -> list[dict]:
    """Новини = коміти в main (заголовок + опис), від нових до старих."""
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


KIND = {"release": ("Оновлення", "rel"), "backtest": ("Бектест", "bt"), "ideas": ("Розбір ідеї", "idea"),
        "update": ("Зміни", "upd")}


def fmt_date(ts: int) -> str:
    return time.strftime("%d.%m.%Y", time.gmtime(ts))


def news_item(n: dict, full: bool = True) -> str:
    label, cls = KIND[n["kind"]]
    ver = f'<span class="ver">{esc(n["version"])}</span>' if n["version"] else ""
    body = f'<p>{esc(n["body"]).replace(chr(10), "<br>")}</p>' if full and n["body"] else ""
    return (f'<article class="news" id="n-{n["sha"][:7]}"><div class="meta"><span class="tag {cls}">{label}</span>{ver}'
            f'<time>{fmt_date(n["ts"])}</time></div><h3>{esc(n["title"])}</h3>{body}'
            f'<a class="src" href="{GH}/commit/{n["sha"]}" rel="noopener">зміни в коді →</a></article>')


def rss(items: list[dict]) -> str:
    base = f"https://{CFG['domain']}"
    it = "".join(
        f"<item><title>{esc((n['version'] + ': ' if n['version'] else '') + n['title'])}</title>"
        f"<link>{base}/news.html#n-{n['sha'][:7]}</link><guid>{n['sha']}</guid>"
        f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(n['ts']))}</pubDate>"
        f"<description>{esc(n['body'][:1500])}</description></item>" for n in items[:30])
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{esc(CFG["title"])} — новини</title>'
            f"<link>{base}</link><description>{esc(CFG['tagline'])}</description>{it}</channel></rss>")


# --- документи ----------------------------------------------------------------------------------
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
            path = path if path.startswith(("agent/", "scripts/", "tests/", ".github/", "config")) else "docs/" + path
            return f"[{label}]({GH}/blob/main/{path})"
        return m.group(0)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "toc"])


# --- сторінки -----------------------------------------------------------------------------------
def nav(active: str) -> str:
    items = [("index", "Головна"), ("news", "Новини")] + [(s, t) for s, _, t in CFG["docs"][:4]] + \
        [("forum", "Форум"), ("chat", "Запитати ШІ")]
    return "".join(f'<a href="{"index" if k == "index" else k}.html" class="{"on" if k == active else ""}">{t}</a>'
                   for k, t in items)


def page(slug: str, title: str, body: str, desc: str = "") -> str:
    return f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · {esc(CFG['title'])}</title><meta name="description" content="{esc(desc or CFG['tagline'])}">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc or CFG['tagline'])}">
<link rel="stylesheet" href="assets/style.css"><link rel="alternate" type="application/rss+xml" href="rss.xml" title="Новини">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><path d='M16 2 6 16l10 6 10-6z' fill='%237b8cff'/><path d='m6 18 10 12 10-12-10 6z' fill='%23b6bfff'/></svg>">
</head><body>
<header class="top"><div class="wrap"><a class="logo" href="index.html"><span class="gem"></span>{esc(CFG['title'])}</a>
<button class="burger" aria-label="меню" onclick="document.body.classList.toggle('nav-open')">☰</button>
<nav>{nav(slug)}</nav></div></header>
<main class="wrap">{body}</main>
<footer><div class="wrap"><p><b>Бета-експеримент. Не фінансова порада.</b> Торгівля криптовалютою може призвести до втрати
всіх вкладених коштів. Результати бектестів — минуле, яке не гарантує майбутнього.</p>
<p><a href="{GH}">GitHub</a> · ліцензія MIT · <a href="rss.xml">RSS новин</a></p></div></footer>
<a class="fab" href="chat.html" title="Запитати ШІ про бота">💬</a>
</body></html>"""


def index_page(items: list[dict]) -> str:
    latest = "".join(news_item(n, full=False) for n in items[:4])
    donate = (f'<section class="card center"><h2>Підтримати проєкт</h2><p>Проєкт безкоштовний і відкритий. '
              f'Якщо він тобі корисний — можна подякувати.</p><a class="btn" href="{esc(CFG["donate_url"])}" '
              f'rel="noopener">☕ Дякую</a></section>') if CFG.get("donate_url") else ""
    return f"""
<section class="hero"><div class="badges"><span>бета</span><span>відкритий код · MIT</span><span>лише спот, без плеча</span></div>
<h1>Бот, що торгує ETH<br>зі звичайного телефона</h1>
<p class="lead">{esc(CFG['tagline'])}. Детермінований код торгує за перевіреними правилами, ШІ-аналітик (Claude) стежить
за новинами й може лише <b>зменшити</b> ризик. Керування — з панелі в браузері та з Telegram.</p>
<div class="cta"><a class="btn" href="install.html">Встановити</a><a class="btn ghost" href="{GH}">GitHub</a>
<a class="btn ghost" href="chat.html">Запитати ШІ</a></div></section>

<section class="grid3">
<div class="card"><h3>📱 Працює на Android</h3><p>Termux, без сервера й без комп'ютера. Автозапуск, перезапуск після збою,
сторож, що напише, якщо телефон вимкнувся.</p></div>
<div class="card"><h3>🛡 Ризик під контролем</h3><p>Лише спот. Жорстка межа капіталу, kill switch, денний ліміт, звірка з біржею,
стоп-ордери на самій біржі.</p></div>
<div class="card"><h3>🧠 ШІ як запобіжник</h3><p>Claude читає новини раз на 12 год і може пригальмувати входи
(CAUTION — половина, RISK_OFF — нуль). Збільшити ризик він не може.</p></div>
<div class="card"><h3>💬 Telegram</h3><p>/status, /chart, /equity, /stop — графіки й керування з месенджера.</p></div>
<div class="card"><h3>💰 Менше витрат</h3><p>Комісія в BNB (−25 %), відсотки на вільні USDT через Binance Earn.</p></div>
<div class="card"><h3>🔬 Чесні перевірки</h3><p>Кожна ідея — через бектест на 7 роках з комісіями.
<a href="ideas.html">Що ми перевірили з «секретних стратегій»</a>.</p></div>
</section>

<section class="card"><h2>Чесні очікування</h2>
<p>Бектест ETHUSDT 2019–2026, комісія 0,1 %, прослизання 0,05 %, прибуток реінвестується.</p>
<table><tr><th>Портфель</th><th>Річна дохідність</th><th>Макс. просадка</th></tr>
<tr><td>обережний</td><td>≈ 2,5 %</td><td>≈ 8 %</td></tr>
<tr><td>збалансований</td><td>≈ 22 %</td><td>≈ 50 %</td></tr>
<tr><td>агресивний</td><td>≈ 29 %</td><td>≈ 56 %</td></tr>
<tr><td>просто тримати ETH</td><td>≈ 46 %</td><td>≈ 79 %</td></tr></table>
<p class="muted">Бот не обганяє «просто тримати» в бичачий ринок — його задача менші просадки. Живі результати на реальних
грошах з'являться тут, коли назбирається достатньо угод.</p></section>

<section><div class="row"><h2>Останні новини</h2><a href="news.html">усі →</a></div>{latest}</section>
{donate}"""


def chat_page() -> str:
    return """<section class="chat card"><h1>Запитати ШІ про ETH-Агента</h1>
<p class="muted">Відповідає Claude на основі документації проєкту: встановлення, налаштування, стратегії, бектести.
Питання не по темі й фінансові поради — ні. Питання зберігаються анонімно (без IP), щоб покращувати документацію.</p>
<div id="log" class="log" aria-live="polite"></div>
<form id="ask"><textarea id="q" maxlength="800" rows="2" placeholder="Напр.: як встановити на Android? що таке kill switch?" required></textarea>
<button class="btn" id="send">Надіслати</button></form><p id="note" class="muted small"></p></section>
<script src="assets/chat.js"></script>"""


def forum_page() -> str:
    g = CFG.get("giscus") or {}
    if g.get("repo_id") and g.get("category_id"):
        embed = (f'<script src="https://giscus.app/client.js" data-repo="{esc(CFG["repo"])}" data-repo-id="{esc(g["repo_id"])}" '
                 f'data-category="{esc(g["category"])}" data-category-id="{esc(g["category_id"])}" data-mapping="specific" '
                 f'data-term="Форум ETH-Агента" data-strict="0" data-reactions-enabled="1" data-emit-metadata="0" '
                 f'data-input-position="top" data-theme="dark" data-lang="uk" crossorigin="anonymous" async></script>')
    else:
        embed = ""
    return f"""<section class="card"><h1>Форум</h1>
<p>Обговорення живуть у <a href="{GH}/discussions">GitHub Discussions</a> — питання, ідеї, звіти про роботу бота.
Щоб писати, потрібен безкоштовний акаунт GitHub.</p>
<div class="cta"><a class="btn" href="{GH}/discussions/new/choose">Нова тема</a><a class="btn ghost" href="{GH}/discussions">Усі теми</a>
<a class="btn ghost" href="{GH}/issues/new">Повідомити про помилку</a></div>{embed}</section>"""


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "kb").mkdir(parents=True)
    shutil.copytree(SITE / "assets", out / "assets")
    items = news()
    (out / "index.html").write_text(page("index", CFG["title"], index_page(items)), encoding="utf-8")
    body = '<h1>Новини проєкту</h1><p class="muted">Кожна зміна в коді й документації — автоматично. ' \
           '<a href="rss.xml">RSS</a></p>' + "".join(news_item(n) for n in items)
    (out / "news.html").write_text(page("news", "Новини", body), encoding="utf-8")
    (out / "rss.xml").write_text(rss(items), encoding="utf-8")
    for slug, fname, title in CFG["docs"]:
        src = ROOT / "docs" / fname
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        (out / f"{slug}.html").write_text(
            page(slug, title, f'<article class="doc card">{md_to_html(text)}'
                              f'<p class="src"><a href="{GH}/blob/main/docs/{fname}">джерело на GitHub</a></p></article>'),
            encoding="utf-8")
        (out / "kb" / fname).write_text(text, encoding="utf-8")       # база знань для чату
    (out / "kb" / "README.md").write_text((ROOT / "README.md").read_text(encoding="utf-8"), encoding="utf-8")
    (out / "forum.html").write_text(page("forum", "Форум", forum_page()), encoding="utf-8")
    (out / "chat.html").write_text(page("chat", "Запитати ШІ", chat_page()), encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /kb/\nDisallow: /api/\n", encoding="utf-8")
    print(f"сайт зібрано: {out} · новин {len(items)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SITE / "_out"))
    build(Path(ap.parse_args().out))
