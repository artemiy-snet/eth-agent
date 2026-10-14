# ETH-Агент

Спотовий агент ETHUSDT (Binance) на логіці «точок дії» CryptoBot v2.02.
Джерело вимог — [Біблія, редакція 2](docs/BIBLE.md). Журнал прогонів — [docs/backtest_log.md](docs/backtest_log.md).
Розбір оригіналу v2.02 — [docs/v202_analysis.md](docs/v202_analysis.md).

> Жоден бектест не гарантує прибутку. Live — лише після етапів 0–2 з розділу 9 Біблії.

## Стан

| Етап | Що | Статус |
|---|---|---|
| 1 | Завантажувач 1m-свічок (data.binance.vision + REST) у Parquet, фільтри exchangeInfo | ✅ код і тести |
| 2 | `strategy/legacy_v2` — модель v2.02 з тестами на кожен перехід | ✅ |
| 2 | `strategy/v3`: пороги в ATR, рухома сітка, стоп, часовий стоп, розмір від ризику, PENDING | ✅ |
| 3 | features: ATR, нахил EMA200, якір — лише закриті свічки, тест look-ahead | ✅ |
| 4 | risk: усі запобіжники розділу 5 + ліміт сукупного ризику, тест на кожен | ✅ |
| 5 | Бектест legacy_v2 і v3 (комісія, прослизання, вето ризику, CAGR, Sharpe), walk-forward; запуск з телефону | ✅ |
| 6 | Живий агент: paper і testnet, ордери з clientOrderId, стоп на біржі, звірка, STOP, Telegram, сторінка статусу; працює на телефоні — [docs/PHONE.md](docs/PHONE.md) | ✅ |
| 7–10 | аналітик Claude, конвеєр навчання, команди Telegram, Docker-панель | ⏳ |

Основний профіль — **денний** (ATR 1d): на 3 роках PF ≈ 2, але лише ≈ 52 угоди. Етап 1 (≥ 100 угод) ще не пройдено.
Паритет з v2.02 неможливий: налаштувань і журналів v2.02 не збереглося; `legacy_v2` — базова лінія.

## Бектест з телефону

`github.com/artemiy-snet/eth-agent/actions` → **backtest** або **walkforward** → **Run workflow**.
Результат — таблицею на сторінці запуску (Summary).

## Швидкий старт (на машині з доступом до Binance)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
cp config.example.yaml config.yaml
eth-agent fetch    --start 2025-10 --end 2026-09   # ~12 місяців 1m, кеш у ./data
eth-agent gaps     --start 2025-10 --end 2026-09
eth-agent backtest --start 2025-10 --end 2026-09 --out reports/legacy_v2.json
```

## Структура

```
agent/
  data/        завантажувач свічок, фільтри символу
  strategy/    legacy_v2 (модель v2.02), далі v3; чисті функції step(state, tick, params)
  backtest/    двигун і звіт
  features/ risk/ execution/ journal/ analyst/ learning/   — наступні етапи
docs/          аналіз v2.02
```

## Правила репозиторію

- Жодних ключів у коді чи конфігу в git: `config.yaml`, `/data`, `.env` — у `.gitignore`; CI запускає gitleaks.
- Гроші — тільки `Decimal`.
- Стратегія й ризик — без вводу/виводу; один код для backtest / paper / shadow / live.
