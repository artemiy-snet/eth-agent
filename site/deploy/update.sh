#!/usr/bin/env bash
# Кожні 2 хв (systemd-таймер criptobot-update): підтягнути main з GitHub; нові релізи/бектести → статті «редакції»;
# перезібрати сайт, якщо зібрана версія відстає від коду або з'явилась нова стаття (прапорець rebuild).
# Усе тіло — у функції: bash спершу читає її повністю, тож git reset, що змінює цей файл, не ламає поточний запуск.
main() {
  set -euo pipefail
  local REPO=/opt/criptobot/repo WWW=/var/www/criptobot DATA=/var/lib/criptobot
  export DATA_DIR="$DATA" ARTICLES_DIR="$DATA/articles"
  local PY
  PY=$(cat /etc/criptobot/python 2>/dev/null || echo /usr/bin/python3)
  cd "$REPO"
  git fetch -q origin main
  git reset -q --hard origin/main
  local new built
  new=$(git rev-parse HEAD)
  built=$(cat "$DATA/built_sha" 2>/dev/null || echo none)
  if [ "$built" != "$new" ]; then
    "$PY" site/editor.py commits || echo "редакція: помилка, спробую наступного разу"
  fi
  if [ -f "$DATA/diary_days.json" ]; then
    "$PY" site/editor.py weekly || echo "редакція: тижневий щоденник — помилка"
  fi
  if [ "$built" != "$new" ] || [ -f "$DATA/rebuild" ] || [ ! -f "$WWW/index.html" ] || [ "${1:-}" = "--force" ]; then
    rm -f "$DATA/rebuild"
    local tmp
    tmp=$(mktemp -d /opt/criptobot/build.XXXXXX)
    "$PY" site/build.py --out "$tmp/out"
    rsync -a --delete "$tmp/out/" "$WWW/"
    rm -r -- "$tmp"
    "$PY" site/promote.py || echo "просування: помилка, спробую наступного разу"
    if [ "$built" != "$new" ] && { [ "$built" = none ] || git diff --name-only "$built" "$new" 2>/dev/null | grep -qE '^site/(chat/|editor\.py)'; }; then
      sudo -n /bin/systemctl restart criptobot-chat || true
    fi
    echo "$new" > "$DATA/built_sha"
    echo "сайт оновлено: $new"
  fi
}
main "$@"
exit
