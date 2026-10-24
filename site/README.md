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
