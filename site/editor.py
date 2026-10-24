"""«Редакція» журналу: Claude перетворює зміни в репозиторії та нотатки автора на статті людською мовою.

    python3 site/editor.py commits      # нові релізи / бектести / розбори з git → статті (викликає update.sh)
    python3 site/editor.py post < text  # нотатка автора → стаття (те саме робить /api/post з Telegram і пульта)

Статті пишуться в ARTICLES_DIR (на сервері /var/lib/criptobot/articles) у форматі site/articles/*.md, сайт підхоплює
їх при наступній збірці. Лише stdlib. Ключ — ANTHROPIC_API_KEY з /etc/criptobot/chat.env.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://api.anthropic.com/v1/messages"
RUBRICS = ["Нова версія", "Як це працює", "Чесно про ризик", "Розвінчуємо міф", "Щоденник експерименту", "Головна історія"]
COVERS = {"robot": "робот біля телефона з графіком (загальне, нова версія)",
          "brain": "мозок ШІ за щитом над графіком (ШІ-аналітик, ризик)",
          "button": "палець тисне червону кнопку (зупинка, захист, kill switch)",
          "desk": "людина за моніторами, поруч робот (стратегії, бектести, міфи)",
          "night": "робот на тумбочці вночі, людина спить (надійність, 24/7, щоденник)",
          "morning": "ранок, робот показує телефон з графіком (результати, Telegram, звіти)",
          "finale": "телефон і робот, що махає (підсумки, анонси)"}

STYLE = f"""Ти — редакція журналу про відкритий проєкт «ETH-Агент» (бот для спотової торгівлі ETH на Binance з Android-телефона).
Пишеш як науково-популярний журнал для широкого читача: тепло, ясно, з цікавим заголовком-питанням або образом,
короткі абзаци, без жаргону (а якщо термін потрібен — поясни його в дужках), без реклами й гучних обіцянок.

Жорсткі правила:
- Лише факти з джерела нижче (і з короткої довідки про проєкт). Нічого не вигадуй: ні цифр, ні дат, ні подій.
- Жодних фінансових порад і прогнозів ціни. Якщо мова про дохідність — нагадай, що це бектест/експеримент, не обіцянка.
- Не публікуй ключі, паролі, IP-адреси, номери рахунків, імена й особисті дані людей (крім підпису «автор проєкту»).
- Обсяг тексту — 200–450 слів. Markdown: абзаци, за потреби 1–3 підзаголовки «## …», один список, одна цитата «> …».
- Мова — українська.

Поверни ЛИШЕ JSON без пояснень:
{{"title": "...", "lead": "1–2 речення-анонс", "rubric": одне з {json.dumps(RUBRICS, ensure_ascii=False)},
 "cover": одне з {json.dumps(list(COVERS))}, "body": "текст статті в markdown без заголовка"}}
Обкладинки: {json.dumps(COVERS, ensure_ascii=False)}"""

ABOUT = """Довідка: бот торгує лише ETHUSDT на споті (без плеча), двома перевіреними стратегіями — «відскок» після різкого
падіння і «режимне утримання», коли ціна вище своєї 200-денної середньої (з v0.11 — також SOL від 100 $ капіталу).
ШІ-аналітик Claude може лише гальмувати нові входи. Kill switch, стопи на біржі, звірка балансу. Live-експеримент
з 15.10.2026 на 17–20 $. Відкритий код, MIT, бета, не фінансова порада."""


def call(key: str, model: str, prompt: str, opener=urllib.request.urlopen) -> tuple[dict, dict]:
    body = {"model": model, "max_tokens": 2000, "system": STYLE, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with opener(req, timeout=120) as r:
        resp = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("редакція не повернула JSON")
    return json.loads(m.group(0)), resp.get("usage", {})


def slugify(s: str) -> str:
    tr = dict(zip("абвгґдеєжзиіїйклмнопрстуфхцчшщьюяыэё",
                  ["a", "b", "v", "h", "g", "d", "e", "ie", "zh", "z", "y", "i", "i", "i", "k", "l", "m", "n", "o", "p", "r",
                   "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "iu", "ia", "y", "e", "e"]))
    s = "".join(tr.get(c, c) for c in s.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:50] or "stattia"


def save(art: dict, out_dir: Path, now: float, author: str = "") -> Path:
    rubric = art.get("rubric") if art.get("rubric") in RUBRICS else "Нова версія"
    cover = art.get("cover") if art.get("cover") in COVERS else "robot"
    clean = lambda s: re.sub(r"[\r\n]+", " ", str(s or "")).strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    path = out_dir / f"{day}-{slugify(clean(art.get('title')))}.md"
    n = 2
    while path.exists():
        path = out_dir / f"{day}-{slugify(clean(art.get('title')))}-{n}.md"
        n += 1
    meta = [f"title: {clean(art.get('title'))}", f"rubric: {rubric}", f"cover: {cover}", f"date: {day}",
            f"lead: {clean(art.get('lead'))}"] + ([f"author: {clean(author)}"] if author else [])
    path.write_text("---\n" + "\n".join(meta) + "\n---\n\n" + str(art.get("body", "")).strip() + "\n", encoding="utf-8")
    return path


def models() -> list[str]:
    return [m.strip() for m in os.environ.get("EDITOR_MODEL", "claude-sonnet-5-5,claude-haiku-5-5").split(",") if m.strip()]


def write(prompt: str, out_dir: Path, author: str = "", opener=urllib.request.urlopen, charge=None) -> Path:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("немає ANTHROPIC_API_KEY")
    last = None
    for m in models():
        try:
            art, usage = call(key, m, prompt, opener)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                continue
            raise
        if charge:
            charge(usage)
        return save(art, out_dir, time.time(), author)
    raise RuntimeError(f"жодна модель недоступна: {last}")


def from_post(text: str, out_dir: Path, **kw) -> Path:
    prompt = (f"{ABOUT}\n\nНотатка автора проєкту, яку треба перетворити на статтю (зберігай його факти й інтонацію, "
              f"можна впорядкувати й пояснити терміни):\n<note>\n{text.strip()[:6000]}\n</note>")
    return write(prompt, out_dir, author="автор проєкту", **kw)


def from_commit(n: dict, out_dir: Path, **kw) -> Path:
    head = (n["version"] + ": " if n["version"] else "") + n["title"]
    prompt = (f"{ABOUT}\n\nЗміна в репозиторії (заголовок і опис коміту), про яку треба написати коротку статтю для читачів:\n"
              f"<commit>\n{head}\n\n{n['body'][:4000]}\n</commit>")
    return write(prompt, out_dir, **kw)


def run_commits(out_dir: Path, state: Path, **kw) -> int:
    """Нові релізи, бектести й розбори з git → статті. Перший запуск лише запам'ятовує поточний стан (без статей)."""
    sys.path.insert(0, str(HERE))
    import build
    items = [n for n in build.news(200) if n["kind"] in ("release", "backtest", "ideas")]
    try:
        done = set(json.loads(state.read_text()))
    except (OSError, ValueError):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps([n["sha"] for n in items]))
        print("редакція: перший запуск — старі зміни пропущено")
        return 0
    made = 0
    for n in reversed(items):                       # від старих до нових
        if n["sha"] in done:
            continue
        try:
            p = from_commit(n, out_dir, **kw)
            print(f"редакція: {p.name}")
            made += 1
        except Exception as e:                      # noqa: BLE001 — одна невдала стаття не зупиняє решту
            print(f"редакція: пропуск {n['sha'][:7]}: {e}")
            continue
        done.add(n["sha"])
        state.write_text(json.dumps(sorted(done)))
        if made >= 3:                               # не більше трьох статей за раз (захист від лавини)
            break
    return made


def main() -> None:
    data = Path(os.environ.get("DATA_DIR", "/var/lib/criptobot"))
    out = Path(os.environ.get("ARTICLES_DIR", str(data / "articles")))
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "commits":
        state = data / "editor_done.json"
        if state.exists() and not os.environ.get("ANTHROPIC_API_KEY"):
            return
        if run_commits(out, state):
            (data / "rebuild").touch()
    elif cmd == "post":
        print(from_post(sys.stdin.read(), out))
        (data / "rebuild").touch()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
