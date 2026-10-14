# ETH-Агент

Спотовий агент ETHUSDT (Binance) на логіці «точок дії» CryptoBot v2.02.
Джерело вимог — «ЕТН-Агент — Промт-Біблія» (проєкт Claude «Кріптобот»).
Розбір оригіналу v2.02 — [docs/v202_analysis.md](docs/v202_analysis.md).

> Жоден бектест не гарантує прибутку. Live — лише після етапів 0–2 з розділу 9 Біблії.

## Стан

| Етап | Що | Статус |
|---|---|---|
| 1 | Завантажувач 1m-свічок (data.binance.vision + REST) у Parquet, фільтри exchangeInfo | ✅ код і тести |
| 2 | `strategy/legacy_v2` — модель v2.02 з тестами на кожен перехід | ✅ |
| 2 | `strategy/v3` | ⏳ |
| 3–4 | features/filters, risk | ⏳ |
| 5 | Бектест: legacy_v2, звіт PnL / PF / DD / застряглі позиції | ✅ перша версія; walk-forward ⏳ |
| 6–10 | execution, journal, analyst, learning, cli+Telegram, панель | ⏳ |

Паритет з v2.02 (етап 0) неможливий: реальні налаштування й журнали v2.02 не збереглися.
`legacy_v2` відтворює логіку з декомпільованого коду і слугує базовою лінією для v3.

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
