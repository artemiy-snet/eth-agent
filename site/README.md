# Сайт criptobot.store

Статичний сайт + чат «Запитати ШІ». Новини — автоматично з історії комітів у `main`.

- `build.py` — збирає сайт (`python3 site/build.py --out site/_out`), потрібен пакет `markdown`.
- `chat/server.py` — чат (лише stdlib): Claude відповідає по документації, ліміти на IP і денний бюджет у $.
- `deploy/setup.sh` — одноразове налаштування сервера (Ubuntu/Debian): nginx, HTTPS, оновлення кожні 5 хв, чат.
- `config.json` — назва, домен, форум (giscus), посилання «Дякую».

Встановлення на сервері:

```
curl -fsSL https://raw.githubusercontent.com/artemiy-snet/eth-agent/main/site/deploy/setup.sh -o setup.sh
sudo bash setup.sh criptobot.store
```
