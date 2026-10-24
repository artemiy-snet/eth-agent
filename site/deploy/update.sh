#!/usr/bin/env bash
# Кожні 2 хв (systemd-таймер criptobot-update): підтягнути main з GitHub; нові релізи/бектести → статті «редакції»;
# перезібрати сайт, якщо змінився код або з'явилась нова стаття (прапорець rebuild ставить /api/post і редакція).
set -euo pipefail
REPO=/opt/criptobot/repo
WWW=/var/www/criptobot
DATA=/var/lib/criptobot
export DATA_DIR="$DATA" ARTICLES_DIR="$DATA/articles"
cd "$REPO"
old=$(git rev-parse HEAD)
git fetch -q origin main
git reset -q --hard origin/main
new=$(git rev-parse HEAD)
if [ "$old" != "$new" ]; then
  python3 site/editor.py commits || echo "редакція: помилка, спробую наступного разу"
fi
if [ "$old" != "$new" ] || [ -f "$DATA/rebuild" ] || [ ! -f "$WWW/index.html" ] || [ "${1:-}" = "--force" ]; then
  rm -f "$DATA/rebuild"
  tmp=$(mktemp -d /opt/criptobot/build.XXXXXX)
  python3 site/build.py --out "$tmp/out"
  rsync -a --delete "$tmp/out/" "$WWW/"
  rm -r -- "$tmp"
  if [ "$old" != "$new" ] && git diff --name-only "$old" "$new" | grep -qE '^site/(chat/|editor\.py)'; then
    sudo -n /bin/systemctl restart criptobot-chat || true
  fi
  echo "сайт оновлено: $new"
fi
