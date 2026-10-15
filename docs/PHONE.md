# ETH-Агент на телефоні (Android, Termux)

Режими: **paper** — справжні ціни, віртуальні угоди; **testnet** — справжні ордери на тестових грошах Binance.
Режим live (реальні гроші) заблоковано до проходження етапів 1–2 Біблії.

Обмеження телефона: коли телефон без інтернету чи вимкнений, агент стоїть. У testnet на біржі
лишається стоп-ордер, тож позиція захищена й без агента.

## 1. Termux

1. Встанови **Termux з F-Droid** (f-droid.org → «Termux»). Версія з Google Play застаріла.
2. Налаштування Android → Програми → Termux → Батарея → **Без обмежень**.
3. Відкрий Termux і дай доступ до файлів:
   ```
   termux-setup-storage
   ```
   (підтвердь дозвіл у вікні Android)

## 2. Код агента

1. У браузері телефона: `github.com/artemiy-snet/eth-agent` → зелена кнопка **Code** → **Download ZIP**.
   Файл `eth-agent-main.zip` з'явиться в «Завантаженнях».
2. У Termux:
   ```
   pkg install -y unzip
   cp ~/storage/downloads/eth-agent-main.zip ~ && cd ~ && unzip -o eth-agent-main.zip
   cd ~/eth-agent-main
   bash scripts/termux-install.sh
   ```

Оновлення коду: знову Download ZIP і ті самі команди `cp … unzip -o`. Стан і журнал лежать окремо в `~/.eth-agent` і не губляться.

## 3. Перший запуск — paper

```
cd ~/eth-agent-main
bash scripts/run.sh paper
```

Сторінка статусу: відкрий у браузері телефона **http://127.0.0.1:8765** (Termux має працювати).
На сторінці: ціна, капітал агента, боти, рівні зведення, події, угоди, кнопки **STOP** і **STOP FLATTEN**.

Зупинити агента: у Termux `Ctrl+C` (на клавіатурі Termux — кнопка CTRL, потім C).

## 3а. Портфель (кілька стратегій одночасно)

```
cd ~/eth-agent-main
bash scripts/run.sh paper config.portfolio.yaml
```

Склад задається в кінці `config.portfolio.yaml` (`preset: збалансований` = 70 % «відскок розширений» + 30 % «режимне утримання»).
На сторінці статусу — загальний капітал, режим ринку і окремий блок на кожну стратегію.

## 4. Testnet (тестові гроші, справжні ордери)

1. Відкрий **testnet.binance.vision** → **Log In with GitHub** → **Generate HMAC_SHA256 Key**.
   Скопіюй API Key і Secret Key (Secret показується один раз). На тестовому рахунку вже є тестові USDT і ETH.
2. У Termux:
   ```
   nano ~/.eth-agent/secrets.env
   ```
   Встав ключі після `BINANCE_API_KEY=` і `BINANCE_API_SECRET=`, збережи: `Ctrl+O`, `Enter`, `Ctrl+X`.
3. Запуск:
   ```
   cd ~/eth-agent-main
   bash scripts/run.sh testnet
   ```

У цей файл — **лише ключі testnet**. Реальний ключ Binance сюди не вставляти.

## 5. Telegram (необов'язково)

1. У Telegram напиши **@BotFather** → `/newbot` → придумай ім'я → отримаєш токен.
2. Додай токен у `~/.eth-agent/secrets.env` після `TELEGRAM_TOKEN=`.
3. Напиши своєму новому боту будь-що (наприклад `/start`).
4. У Termux:
   ```
   cd ~/eth-agent-main && python -m agent.cli live --telegram-setup
   ```
   Скопіюй показаний номер у `TELEGRAM_CHAT_ID=`.

Сповіщення приходять про запуск, купівлю, продаж, kill switch і помилки.

## 5а. ШІ-аналітик (необов'язково)

1. Отримай ключ на **console.anthropic.com** → API Keys → Create Key. Вартість ≈ $1–2 на місяць (2–4 запити на добу).
   Там же в Billing → Limits можна поставити місячний ліміт, наприклад $5.
2. Додай у `~/.eth-agent/secrets.env` рядок `ANTHROPIC_API_KEY=…` (через `nano`).
3. Перезапусти агента. На сторінці статусу — блок «Події та новини»: найближча макроподія, оцінка ШІ
   (NORMAL / CAUTION / RISK_OFF), розбір позицій і помічені аномалії. Зміни режиму й аномалії приходять у Telegram.
4. Режим `auto` (у конфігу): ШІ сам, без погодження, може лише пригальмувати нові входи (CAUTION — ½ розміру,
   RISK_OFF — без входів, до 24 год). Ордерів не створює, не продає, стопи не чіпає.
   Лише спостерігати: `mode: shadow` у секції `analyst` конфігу.
5. Через 1–3 місяці: `python -m agent.cli ai-review` покаже, чи гальмування справді збігалося з падіннями.

## 6. Аварійні дії

| Що | Як |
|---|---|
| Заборонити нові входи | кнопка **STOP** на сторінці статусу (або `touch ~/.eth-agent/STOP`) |
| Продати все й зупинитись | кнопка **STOP FLATTEN** |
| Зняти STOP і продовжити | `python -m agent.cli live --clear-kill`, потім знову `bash scripts/run.sh …` |
| Почати paper/testnet з нуля | видалити `~/.eth-agent/state_paper.json` або `state_testnet.json` |

## Що очікувати

Денний профіль торгує рідко: ≈ 1–2 угоди на місяць на весь масив. Перші дні — перевірка техніки:
агент бачить ціну, рахує рівні, сторінка оновлюється, testnet-ордери проходять і стоп з'являється на біржі
(видно в розділі Open Orders на testnet.binance.vision).
