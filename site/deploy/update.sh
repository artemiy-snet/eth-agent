#!/usr/bin/env bash
# Раз на 5 хв (systemd-таймер criptobot-update): підтягнути main з GitHub і, якщо є зміни, перезібрати сайт.
set -euo pipefail
REPO=/opt/criptobot/repo
WWW=/var/www/criptobot
cd "$REPO"
old=$(git rev-parse HEAD)
git fetch -q origin main
git reset -q --hard origin/main
new=$(git rev-parse HEAD)
if [ "$old" != "$new" ] || [ ! -f "$WWW/index.html" ] || [ "${1:-}" = "--force" ]; then
  tmp=$(mktemp -d /opt/criptobot/build.XXXXXX)
  python3 site/build.py --out "$tmp/out"
  rsync -a --delete "$tmp/out/" "$WWW/"
  rm -r -- "$tmp"
  # змінився код чату — перезапустити сервіс
  if [ "$old" != "$new" ] && git diff --name-only "$old" "$new" | grep -q '^site/chat/'; then
    sudo -n /bin/systemctl restart criptobot-chat || true
  fi
  echo "сайт оновлено: $new"
fi
