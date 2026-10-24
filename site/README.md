# Журнал criptobot.store

Сайт-журнал у стилі рекламного ролика: головна — «фільм» зі сцен (робот, ШІ-щит, червона кнопка, ніч, ранок),
далі ранковий журнал — статті, хроніка змін, розвінчані міфи, форум і чат «Запитати ШІ».

- `build.py` — збирає сайт (`python3 site/build.py --out site/_out`), потрібен пакет `markdown`.
- `articles/*.md` — статті редакції (frontmatter: title, rubric, cover, date, lead, pin).
- `editor.py` — «редакція» (Claude): нові релізи/бектести/розбори з git і нотатки автора → статті.
- `chat/server.py` — чат по документації (ліміти на IP і денний бюджет) + `/api/post` для нотаток автора.
- `assets/media/` — сцени ролика (зациклені mp4 + постери).
- `deploy/` — налаштування сервера, nginx, systemd, оновлення кожні 2 хв.

Нотатка → стаття: у Telegram-боті `/post текст` (токен редакції — у панелі агента, поле «Журнал criptobot.store»)
або з комп'ютера на `https://criptobot.store/admin.html`.

Встановлення на сервері (Ubuntu/Debian):

```
curl -fsSL https://raw.githubusercontent.com/artemiy-snet/eth-agent/main/site/deploy/setup.sh -o setup.sh
sudo bash setup.sh criptobot.store
```

## Статистика й просування (deploy/stats_setup.sh)

```
curl -fsSL https://raw.githubusercontent.com/artemiy-snet/eth-agent/main/site/deploy/stats_setup.sh -o stats_setup.sh
bash stats_setup.sh criptobot.store
```

- Umami (Docker, 127.0.0.1:3010) на `stats.criptobot.store` — потрібен DNS A-запис `stats`. Трекер у всіх сторінках.
- GoAccess щогодини (cron) → `/var/lib/criptobot/goaccess/` (боти й сканери по днях), без публічного доступу.
- `sitemap.xml`, `robots.txt`, `/feed.xml`, Open Graph, Twitter Card, schema.org Article — у збірці; IndexNow-пінг
  і Telegram-автопостинг (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID` у `/etc/criptobot/site.env`) — `promote.py`.
- `GET /api/stats?days=1..90` з `Authorization: Bearer <STATS_TOKEN>` — для Пульта (site/chat/stats.py).
