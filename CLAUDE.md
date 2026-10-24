# ETH-Агент — карта проєкту для Claude

Автономний бот для **спотової** торгівлі ETHUSDT на Binance. Живий агент працює на Android-телефоні в Termux
(панель `127.0.0.1:8765`, Telegram-бот). Сайт-журнал — `criptobot.store` (VPS, Ubuntu 22.04). Власника звати Командир:
спілкування українською, коротко й практично, команди — по одній у блоках коду.

Повні вимоги — `docs/BIBLE.md`. Журнал бектестів — `docs/backtest_log.md`. Стратегії — `docs/strategies.md`.
Телефон і щоденна робота — `docs/PHONE.md`. Розбори «секретних стратегій» з відео — `docs/IDEAS.md`.

## Незмінні правила

- Лише спот: без плеча, ф'ючерсів і позик. ШІ (Claude) лише **зменшує** ризик (CAUTION → ½, RISK_OFF → 0),
  ордерів не створює (виняток — рукав D з перевірками).
- Будь-що, що збільшує ризик, капітал чи додає стратегію або фільтр, — лише після бектесту (7 років, комісії),
  walk-forward і «так» Командира.
- Ключі й токени — ніколи в коді, git, логах чи на сайті. На телефоні: `~/.eth-agent/secrets.env`; на сервері:
  `/etc/criptobot/*.env`.
- Ніколи не давати `rm -rf` (16.10 злиплі в Termux команди стерли домашню теку). Команди — по одній.
- Гроші — `Decimal` (`agent/money.py`). Живий агент — лише stdlib + pyyaml (Termux). Сайт — stdlib + markdown,
  **Python 3.10** на сервері (жодних бекслешів у виразах f-рядків; CI-задача `site-py310` це перевіряє).

## Карта коду

| Шлях | Що там |
|---|---|
| `agent/strategy/` | Чисті функції `step(state, tick, features, params) → (state, intents, blocked)`. `v3.py` — відскок (рукави A/B/B1/Bm; `stop_mode` atr/sweep), `trend.py` — режимне утримання за EMA200 днів (C/Cm/Cs), `ai_trader.py` — рукав D, `legacy_v2.py` — старий CryptoBot v2.02 (базова лінія), `sleeves.py` — рукави, пресети, групи бектестів, `types.py` — Tick/Intent/Fill |
| `agent/risk/guard.py` | Право вето: kill switch, денний ліміт, просадка, ордери/год, рух > 3 %/хв, спред, розбіжність балансу > 0,5 % |
| `agent/features/` | Ознаки з закритих свічок (ATR, EMA, якір), календар FOMC/CPI |
| `agent/live/runner.py` | Цикл агента, `Agent` (один рукав, своя пара) і `Portfolio` (звірка по монетах `demand/shortage`), `build()` (брокери на кожну пару, Earn, BNB, preflight), `site_feed`, `heartbeat_ping`, `outage_note`, `auth_check` |
| `agent/live/broker.py` | Paper / testnet / live: MARKET на суму, стоп STOP_LOSS_LIMIT на біржі, UNKNOWN → запит статусу, комісія в BNB, `base_asset()` |
| `agent/live/binance.py` | REST-клієнт (IPv4, захист від бану), `fill_from_response(o, base)` |
| `agent/live/settings.py` | Конфіг без pydantic: рукави з пресету, `sol_auto` (Cs від 100 $), межа капіталу live, `overrides.yaml` з панелі |
| `agent/live/earn.py` | Simple Earn: вільні USDT у депозит, повернення перед купівлею |
| `agent/live/tg_bot.py` | Telegram: /status /chart /equity /ai /trades /stop /flatten /resume /post |
| `agent/live/panel_api.py`, `status_server.py`, `web/index.html` | Панель (vanilla JS, SVG, UA/EN) і її API |
| `agent/live/site_feed.py` | Знімок для щоденника на сайті (лише відсотки) → POST `/api/diary` |
| `agent/analyst/shadow.py` | ШІ-аналітик (блок «що є нормою» проти хибних тривог — правити тут) |
| `agent/backtest/` | `v3_engine.py` (той самий step на 1m-історії), `portfolio.py` (пресети, `run_all`), `grid.py` (сітка), `event_study.py` |
| `agent/learning/walkforward.py` | Сітки гіпотез і walk-forward |
| `agent/cli.py` | `eth-agent fetch / backtest / walkforward / portfolio / grid / live / ai-review` |
| `site/build.py` | Сайт-журнал: сцени ролика, статті (`site/articles/*.md` + `ARTICLES_DIR`), хроніка з комітів, міфи з IDEAS.md, щоденник бота, sitemap/feed, OG/schema, трекер Umami |
| `site/chat/server.py`, `stats.py` | Сервер на 127.0.0.1:8790: `/api/ask` (чат, потрібен ключ ШІ), `/api/post`, `/api/diary`, `/api/stats` (для Пульта) |
| `site/editor.py`, `promote.py` | «Редакція» (статті з комітів, нотаток, щотижневий щоденник) і просування (IndexNow, Telegram-канал) |
| `site/deploy/` | `setup.sh`, `stats_setup.sh`, `update.sh` (кожні 2 хв: git pull → збірка), systemd, nginx, Umami, GoAccess |
| `scripts/` | Телефон: `run.sh` (перезапуск після падіння), `upd.sh` (оновлення з GitHub), `autostart.sh` (Termux:Boot) |
| `.github/workflows/` | `ci` (pytest + збірка сайту на 3.10 + gitleaks), `portfolio`/`grid`/`backtest`/`walkforward` (бектести в Actions), `release` |

## Як працювати

- Тести: `python -m pytest -q` — усі зелені перед комітом.
- Реліз агента: підняти версію в `agent/__init__.py` і `pyproject.toml` → коміт → push у `main` →
  `gh api -X POST repos/artemiy-snet/eth-agent/actions/workflows/release.yml/dispatches -f ref=main`.
- Бектести — лише в GitHub Actions (data.binance.vision з робочого середовища недоступний):
  `.../workflows/portfolio.yml/dispatches -f ref=main -f "inputs[preset]=base" -f "inputs[pair]=ETHUSDT"`;
  результат — в анотаціях запуску (`gh api repos/.../check-runs/<job>/annotations`). Новий результат — у `docs/backtest_log.md`.
- Нова ідея з відео → розбір у `docs/IDEAS.md` (сайт сам зробить з нього сторінку міфу).
- Стаття в журнал → `site/articles/YYYY-MM-DD-slug.md` (frontmatter: title, rubric, cover, date, lead, author, pin).
  Обкладинки: robot, brain, button, desk, night, morning, finale. Сервер підхопить за 2 хв.
- Телефон: `upd` (оновлення) і `cd ~/eth-agent-main && bash scripts/run.sh`. Сервер сайту оновлюється сам.

## Поточний стан (оновлювати при змінах)

- Live з 15.10.2026, межа капіталу 17 $, склад live_міні: Bm (відскок) + Cm (режимне утримання). SOL (Cs) додасться
  автоматично від 100 $. На рахунку «сирітський» ETH ~0,0041 — до кінця експерименту не чіпати.
- Відхилено бектестами: фільтр FOMC/CPI, LIMIT_MAKER, BTC і BNB як друга пара, сітковий бот, стоп під прострілом.
- Сервер сайту без ключа ШІ (рішення Командира 24.10): чат, `/post` і автостатті вимкнені; статті пишемо в репозиторії.
