#!/usr/bin/env bash
# Статистика й просування criptobot.store + адреса для «Пульта». Запуск від root, повторний запуск безпечний.
#
#   curl -fsSL https://raw.githubusercontent.com/artemiy-snet/eth-agent/main/site/deploy/stats_setup.sh -o stats_setup.sh
#   bash stats_setup.sh criptobot.store
#
# Робить: резервну копію → Umami (Docker, 127.0.0.1) на stats.<домен> → трекер у всіх сторінках → GoAccess щогодини →
# sitemap/feed/IndexNow/Open Graph (у збірці сайту) → GET /api/stats з окремим токеном. Нічого не видаляє.
set -euo pipefail

DOMAIN="${1:-criptobot.store}"
STATS="stats.${DOMAIN}"
REPO=/opt/criptobot/repo
ETC=/etc/criptobot
DATA=/var/lib/criptobot
UM=/opt/criptobot/umami
PY=$(cat "$ETC/python" 2>/dev/null || echo /usr/bin/python3)
step() { echo; echo "== $*"; }
gen() { head -c 64 /dev/urandom | base64 | tr -d '/+=\n' | head -c "$1"; }
getv() { grep -m1 "^$1=" "$2" 2>/dev/null | cut -d= -f2- || true; }

[ "$(id -u)" = 0 ] || { echo "Запусти від root"; exit 1; }
[ -d "$REPO/.git" ] || { echo "Спершу встанови сайт: bash setup.sh $DOMAIN"; exit 1; }

step "1/9 Резервна копія конфігів і сайту"
BK="/root/backups/criptobot-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"; chmod 700 /root/backups
tar czf "$BK/nginx.tgz" -C / etc/nginx
tar czf "$BK/www.tgz" -C / var/www/criptobot
tar czf "$BK/etc-criptobot.tgz" -C / etc/criptobot
sudo -u criptobot git -C "$REPO" rev-parse HEAD > "$BK/repo_head.txt"
chmod 600 "$BK"/*
echo "   $BK"

step "2/9 Свіжий код сайту"
sudo -u criptobot git -C "$REPO" fetch -q origin main
sudo -u criptobot git -C "$REPO" reset -q --hard origin/main
echo "   $(sudo -u criptobot git -C "$REPO" log -1 --format='%h %s' | cut -c1-90)"

step "3/9 Пакети (GoAccess, Docker Compose)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q -o APT::Update::Post-Invoke-Success::= >/dev/null || true
apt-get install -y -q goaccess >/dev/null
if ! command -v docker >/dev/null; then apt-get install -y -q docker.io >/dev/null; fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -q docker-compose-plugin >/dev/null 2>&1 || apt-get install -y -q docker-compose-v2 >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
echo "   $(goaccess --version | head -1); $(docker compose version | head -1)"

step "4/9 Секрети (лише у файлах з доступом root/сервісу, поза git)"
mkdir -p "$UM" "$DATA/goaccess"
if [ ! -f "$UM/.env" ]; then
  umask 077
  printf 'UMAMI_DB_PASS=%s\nUMAMI_APP_SECRET=%s\n' "$(gen 32)" "$(gen 48)" > "$UM/.env"
fi
chmod 600 "$UM/.env"
NEW_ADMIN=0; NEW_TOKEN=0
ADMIN_PASS=$(getv UMAMI_PASS "$ETC/stats.env")
STATS_TOKEN=$(getv STATS_TOKEN "$ETC/stats.env")
[ -n "$ADMIN_PASS" ] || { ADMIN_PASS=$(gen 32); NEW_ADMIN=1; }
[ -n "$STATS_TOKEN" ] || { STATS_TOKEN=$(gen 40); NEW_TOKEN=1; }
INDEXNOW_KEY=$(getv INDEXNOW_KEY "$ETC/site.env")
[ -n "$INDEXNOW_KEY" ] || INDEXNOW_KEY=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
TG_TOKEN=$(getv TELEGRAM_BOT_TOKEN "$ETC/site.env")
TG_CHAN=$(getv TELEGRAM_CHANNEL_ID "$ETC/site.env")
echo "   готово"

step "5/9 Umami у Docker (лише 127.0.0.1:3010)"
install -m 644 "$REPO/site/deploy/umami/docker-compose.yml" "$UM/docker-compose.yml"
docker compose -f "$UM/docker-compose.yml" --env-file "$UM/.env" -p criptobot-umami up -d --quiet-pull
WID=$(UMAMI_NEW_PASS="$ADMIN_PASS" SITE_DOMAIN="$DOMAIN" "$PY" "$REPO/site/deploy/umami_init.py")
echo "   сайт в Umami: $WID"
umask 027
cat > "$ETC/stats.env" <<ENV
UMAMI_URL=http://127.0.0.1:3010
UMAMI_USER=admin
UMAMI_PASS=${ADMIN_PASS}
UMAMI_WEBSITE_ID=${WID}
STATS_TOKEN=${STATS_TOKEN}
ENV
cat > "$ETC/site.env" <<ENV
SITE_DOMAIN=${DOMAIN}
UMAMI_SRC=https://${STATS}/script.js
UMAMI_WEBSITE_ID=${WID}
INDEXNOW_KEY=${INDEXNOW_KEY}
# Telegram-автопостинг нових статей: впиши обидва значення й нічого більше не треба (наступна стаття піде в канал)
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHANNEL_ID=${TG_CHAN}
ENV
chown root:criptobot "$ETC/stats.env" "$ETC/site.env"
chmod 640 "$ETC/stats.env" "$ETC/site.env"
umask 022

step "6/9 nginx: $STATS, ліміт для /api/stats, розмір запитів"
cat > /etc/nginx/conf.d/criptobot-stats.conf <<'NG'
limit_req_zone $binary_remote_addr zone=criptostats:10m rate=30r/m;
NG
if [ ! -f /etc/nginx/sites-available/criptobot-stats ]; then
  cat > /etc/nginx/sites-available/criptobot-stats <<NG
server {
    listen 80;
    listen [::]:80;
    server_name ${STATS};
    location / {
        proxy_pass http://127.0.0.1:3010;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NG
fi
ln -sf /etc/nginx/sites-available/criptobot-stats /etc/nginx/sites-enabled/criptobot-stats
"$PY" - /etc/nginx/sites-available/criptobot <<'PYX'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = s.replace("client_max_body_size 20k;", "client_max_body_size 400k;")
block = """    location = /api/stats {
        limit_req zone=criptostats burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Host $host;
    }

"""
if "location = /api/stats" not in s:
    s = s.replace("    location /api/ {", block + "    location /api/ {")
open(p, "w").write(s)
PYX
if ! nginx -t 2>/dev/null; then
  echo "!! nginx -t не пройшов — повертаю конфіги з резервної копії"
  tar xzf "$BK/nginx.tgz" -C /
  nginx -t && systemctl reload nginx
  exit 1
fi
systemctl reload nginx
if certbot --nginx -d "$STATS" --non-interactive --agree-tos --register-unsafely-without-email --redirect >/dev/null 2>&1; then
  echo "   HTTPS для $STATS — є"
  STATS_OK=1
else
  STATS_OK=0
  echo "!! Сертифікат для $STATS не отримано: додай у DNS A-запис  stats → $(curl -fsS4 https://api4.ipify.org 2>/dev/null || echo 'IP сервера')"
  echo "   і повтори: certbot --nginx -d $STATS --redirect   (до того трекер не рахуватиме відвідування)"
fi

step "7/9 Сервіси й перезбірка сайту (трекер, sitemap, feed, IndexNow)"
install -m 644 "$REPO/site/deploy/criptobot-chat.service" /etc/systemd/system/
install -m 644 "$REPO/site/deploy/criptobot-update.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart criptobot-chat
set -a; . "$ETC/site.env"; set +a
sudo -u criptobot env DATA_DIR="$DATA" ARTICLES_DIR="$DATA/articles" UMAMI_SRC="$UMAMI_SRC" \
  UMAMI_WEBSITE_ID="$UMAMI_WEBSITE_ID" INDEXNOW_KEY="$INDEXNOW_KEY" TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  TELEGRAM_CHANNEL_ID="$TELEGRAM_CHANNEL_ID" bash "$REPO/site/deploy/update.sh" --force | sed 's/^/   /'

step "8/9 GoAccess щогодини (звіт лише на сервері: $DATA/goaccess/)"
cat > /etc/cron.d/criptobot-goaccess <<CR
# Розбір журналів nginx: боти й сканери → $DATA/goaccess (для /api/stats), без публічного доступу
7 * * * * root $PY $REPO/site/deploy/goaccess_daily.py >/dev/null 2>&1
CR
chmod 644 /etc/cron.d/criptobot-goaccess
"$PY" "$REPO/site/deploy/goaccess_daily.py" && echo "   перший звіт готовий: $(ls "$DATA/goaccess" | wc -l) файлів"

step "9/9 Перевірка"
sleep 2
c401=$(curl -s -o /dev/null -w '%{http_code}' "https://${DOMAIN}/api/stats?days=7" || true)
c200=$(curl -s -o /tmp/criptostats.json -w '%{http_code}' -H "Authorization: Bearer ${STATS_TOKEN}" "https://${DOMAIN}/api/stats?days=7" || true)
echo "   без токена: ${c401} (очікується 401) · з токеном: ${c200} (очікується 200)"
[ "$c200" = 200 ] && "$PY" -c "import json;d=json.load(open('/tmp/criptostats.json'));print('   сьогодні відвідувачів:',d['today']['visitors'],'· статей:',d['articles']['total'],'· ботів за 7 днів:',d['bots']['requests'])" || true
rm -f /tmp/criptostats.json

echo
echo "================ ЗВІТ (покажи це лише собі) ================"
echo "Резервна копія:      $BK"
echo "Дашборд Umami:       https://${STATS}/   (логін: admin)"
if [ "$NEW_ADMIN" = 1 ]; then echo "Пароль Umami:        ${ADMIN_PASS}"; else echo "Пароль Umami:        не змінювався"; fi
echo "API для Пульта:      GET https://${DOMAIN}/api/stats?days=7"
if [ "$NEW_TOKEN" = 1 ]; then echo "STATS_TOKEN:         ${STATS_TOKEN}"; else echo "STATS_TOKEN:         не змінювався"; fi
echo "IndexNow:            https://${DOMAIN}/${INDEXNOW_KEY}.txt"
echo "Telegram-автопостинг: вимкнено — впиши TELEGRAM_BOT_TOKEN і TELEGRAM_CHANNEL_ID у $ETC/site.env"
[ "$STATS_OK" = 1 ] || echo "УВАГА: додай DNS-запис stats → IP сервера і виконай: certbot --nginx -d $STATS --redirect"
echo "Пароль і токен збережені лише в $ETC/stats.env (root і сервіс). Очисти екран: clear"
