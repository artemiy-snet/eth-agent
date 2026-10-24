#!/usr/bin/env bash
# Одноразове налаштування сервера для criptobot.store (Ubuntu/Debian, від root).
#
#   curl -fsSL https://raw.githubusercontent.com/artemiy-snet/eth-agent/main/site/deploy/setup.sh -o setup.sh
#   sudo bash setup.sh criptobot.store
#
# Що робить: nginx + HTTPS (Let's Encrypt), сайт з GitHub (оновлюється сам кожні 5 хв), чат «Запитати ШІ».
# Нічого не видаляє; чужі сайти nginx для цього ж домену лише вимикає (посилання переносить у sites-disabled-criptobot/).
set -euo pipefail

DOMAIN="${1:-criptobot.store}"
REPO_URL="https://github.com/artemiy-snet/eth-agent.git"
BASE=/opt/criptobot
WWW=/var/www/criptobot
DATA=/var/lib/criptobot
ETC=/etc/criptobot

[ "$(id -u)" = 0 ] || { echo "Запусти через sudo: sudo bash setup.sh $DOMAIN"; exit 1; }
command -v apt-get >/dev/null || { echo "Потрібен Ubuntu або Debian (apt)."; exit 1; }

echo "== 1/7 Пакети"
export DEBIAN_FRONTEND=noninteractive
# «apt_pkg»-помилку хука command-not-found (коли python3 на сервері підмінено іншою версією) пропускаємо — списки пакетів
# при цьому оновлюються нормально
apt-get update -q -o APT::Update::Post-Invoke-Success::= || true
apt-get install -y -q nginx python3 python3-markdown git rsync certbot python3-certbot-nginx sudo

# Python, якому доступний пакет markdown (python3-markdown ставиться для системного python3.10 в Ubuntu 22.04)
PY=""
for c in /usr/bin/python3 /usr/bin/python3.10 /usr/bin/python3.12 /usr/bin/python3.11; do
  if [ -x "$c" ] && "$c" -c "import markdown" 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "!! Не знайшов python з пакетом markdown. Спробую pip для /usr/bin/python3."
  apt-get install -y -q python3-pip || true
  /usr/bin/python3 -m pip install -q markdown || /usr/bin/python3 -m pip install -q --break-system-packages markdown
  PY=/usr/bin/python3
fi
echo "   Python для збірки сайту: $PY ($("$PY" --version 2>&1))"

# порт 80 має бути вільний для nginx (Apache зупиняємо нижче; інше — показуємо й зупиняємось)
busy=$(ss -ltnpH '( sport = :80 )' 2>/dev/null | grep -v nginx || true)
if [ -n "$busy" ] && ! echo "$busy" | grep -q apache2; then
  echo "!! Порт 80 зайнятий не nginx:"; echo "$busy"
  echo "   Зупини цю програму (напр. docker-контейнер старого сайту) і запусти скрипт ще раз."
  exit 1
fi

if systemctl is-active --quiet apache2 2>/dev/null; then
  echo "!! На сервері працює Apache і займає порт 80. Зупиняю його (вимкнути назад: systemctl enable --now apache2)."
  systemctl disable --now apache2
fi

echo "== 2/7 Користувач і теки"
id criptobot >/dev/null 2>&1 || useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin criptobot
mkdir -p "$BASE" "$WWW" "$DATA/articles" "$ETC"
echo "$PY" > "$ETC/python"
chown -R criptobot:criptobot "$BASE" "$WWW" "$DATA"

echo "== 3/7 Код з GitHub"
if [ ! -d "$BASE/repo/.git" ]; then
  sudo -u criptobot git clone -q "$REPO_URL" "$BASE/repo"
fi

echo "== 4/7 Ключ для чату"
if [ ! -f "$ETC/chat.env" ]; then
  echo "Для чату потрібен ключ Anthropic (console.anthropic.com → API Keys)."
  echo "Порада: окремий ключ саме для сайту, з лімітом витрат у консолі. Не той, що в агента на телефоні."
  read -r -s -p "Встав ключ і натисни Enter (або просто Enter — додаси пізніше): " KEY; echo
  umask 077
  cat > "$ETC/chat.env" <<ENV
ANTHROPIC_API_KEY=${KEY}
# Скільки $ на добу може витратити чат на весь сайт (після — «ліміт вичерпано» до півночі UTC)
CHAT_DAILY_USD=1.0
CHAT_PER_IP_HOUR=10
CHAT_PER_IP_DAY=30
CHAT_MODEL=claude-haiku-5-5,claude-sonnet-5-5
CHAT_HOSTS=${DOMAIN},www.${DOMAIN}
# Токен для статей з Telegram (/post) і з пульта ${DOMAIN}/admin.html — тримай у секреті
POST_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
# Модель «редакції» (статті з оновлень і нотаток)
EDITOR_MODEL=claude-sonnet-5-5,claude-haiku-5-5
ENV
  chown root:criptobot "$ETC/chat.env"
  chmod 640 "$ETC/chat.env"
fi

echo "== 5/7 Сервіси"
install -m 644 "$BASE/repo/site/deploy/criptobot-chat.service" /etc/systemd/system/
install -m 644 "$BASE/repo/site/deploy/criptobot-update.service" /etc/systemd/system/
install -m 644 "$BASE/repo/site/deploy/criptobot-update.timer" /etc/systemd/system/
echo "criptobot ALL=(root) NOPASSWD: /bin/systemctl restart criptobot-chat" > /etc/sudoers.d/criptobot
chmod 440 /etc/sudoers.d/criptobot
visudo -cf /etc/sudoers.d/criptobot >/dev/null
systemctl daemon-reload
sudo -u criptobot env DATA_DIR="$DATA" ARTICLES_DIR="$DATA/articles" "$PY" "$BASE/repo/site/editor.py" commits
sudo -u criptobot bash "$BASE/repo/site/deploy/update.sh" --force
systemctl enable --now criptobot-update.timer
if grep -q '^ANTHROPIC_API_KEY=.\+' "$ETC/chat.env"; then
  systemctl enable --now criptobot-chat
else
  echo "!! Ключа немає — чат не запущено. Додай: sudo nano $ETC/chat.env, потім: sudo systemctl enable --now criptobot-chat"
fi

echo "== 6/7 nginx"
sed "s/DOMAIN/${DOMAIN}/g" "$BASE/repo/site/deploy/nginx.conf" > /etc/nginx/sites-available/criptobot
ln -sf /etc/nginx/sites-available/criptobot /etc/nginx/sites-enabled/criptobot
mkdir -p /etc/nginx/sites-disabled-criptobot
for f in /etc/nginx/sites-enabled/*; do
  [ "$(basename "$f")" = criptobot ] && continue
  if grep -qE "server_name[^;]*\b${DOMAIN//./\\.}\b" "$f" 2>/dev/null || [ "$(basename "$f")" = default ]; then
    echo "   вимикаю старий сайт nginx: $f (копія посилання в /etc/nginx/sites-disabled-criptobot/)"
    mv "$f" /etc/nginx/sites-disabled-criptobot/
  fi
done
nginx -t
systemctl reload nginx || systemctl restart nginx
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 'Nginx Full' >/dev/null
fi

echo "== 7/7 HTTPS"
if certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --non-interactive --agree-tos \
     --register-unsafely-without-email --redirect; then
  echo "HTTPS увімкнено."
else
  echo "!! Сертифікат не отримано. Перевір, що DNS-записи A для $DOMAIN і www.$DOMAIN вказують на IP цього сервера:"
  echo "   $(curl -fsS4 https://api4.ipify.org 2>/dev/null || echo '?')"
  echo "   Потім повтори: sudo certbot --nginx -d $DOMAIN -d www.$DOMAIN --redirect"
fi

echo
echo "Готово: https://$DOMAIN"
echo "Токен для статей (Telegram /post і пульт https://$DOMAIN/admin.html):"
echo "   $(grep '^POST_TOKEN=' "$ETC/chat.env" | cut -d= -f2)"
echo "Сайт оновлюється з GitHub сам кожні 2 хв. Перевірка чату: curl -s http://127.0.0.1:8790/api/health"
echo "Питання з чату (анонімно): sudo tail $DATA/questions.jsonl"
