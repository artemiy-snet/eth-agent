# ETH-Агент · v0.9.1-beta

Спотовий торговий агент ETHUSDT (Binance) для телефона (Android/Termux) з веб-панеллю.
Джерело вимог — [Біблія](docs/BIBLE.md). Встановлення й щоденна робота — [docs/PHONE.md](docs/PHONE.md).

> Жоден бектест не гарантує прибутку. Live — рішення Командира, з жорсткою межею капіталу.

## Що вміє бета

| Частина | Стан |
|---|---|
| Стратегії: відскок v3 (A/B/B1), режимне утримання C, ШІ-трейдер D (лише за умовами A/B/C, з перевіркою) | ✅ |
| Портфелі з реінвестуванням прибутку; пресети обережний … лише_режим, «+ші», live_міні | ✅ |
| Ризик-модуль: kill switch, денний ліміт, сукупний ризик, рух > 3 %/хв, спред, звірка балансу | ✅ |
| Виконання: paper / testnet / live, clientOrderId, стоп на біржі, UNKNOWN → запит статусу, захист від бану API | ✅ |
| ШІ-аналітик (Claude): новини + макрокалендар + контроль позицій, режим auto (лише зменшує ризик) | ✅ |
| Панель: огляд з пульсом, графіки з зонами й мітками, прогноз і новини, угоди, налаштування; UA/EN | ✅ |
| Live: підтвердження LIVE, окремі ключі, межа капіталу (за замовч. 20 $), IPv4 для IP-обмеження ключа | ✅ |
| Бектест і walk-forward 7 років (GitHub Actions) | ✅ |

## Встановлення (телефон)

Releases → **eth-agent-beta.zip** (або Code → Download ZIP), далі — [docs/PHONE.md](docs/PHONE.md):

```
cd ~ && cp ~/storage/downloads/eth-agent-beta.zip ~ && unzip -o eth-agent-beta.zip && cd ~/eth-agent-main
bash scripts/termux-install.sh
bash scripts/run.sh
```
Панель: http://127.0.0.1:8765

## Розробка

```bash
pip install -e ".[research,dev]"
pytest -q                     # тести не входять у бета-збірку, але обов'язкові перед кожним випуском
eth-agent portfolio --config config.example.yaml --start 2019-01 --end 2026-09 --preset base
```

Структура: `agent/{strategy, features, risk, live, analyst, backtest, learning, data}`, панель — `agent/live/web/index.html`.

## Правила

- Ключі — лише в `~/.eth-agent/secrets.env` (поза git); CI запускає gitleaks.
- Гроші — тільки `Decimal`. Стратегія й ризик — чисті функції, один код для backtest / paper / testnet / live.
- ШІ поза рукавом D лише зменшує ризик; ордери створює детермінований код.
