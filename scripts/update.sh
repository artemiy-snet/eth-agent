#!/data/data/com.termux/files/usr/bin/bash
# Оновлення ETH-Агента одним рядком:   bash ~/eth-agent-main/scripts/update.sh
# Бере НАЙНОВІШИЙ eth-agent*.zip із «Завантажень» (байдуже, «(1)» чи ні), перевіряє, що це справжній архів
# з агентом, ставить замість старої теки і показує версію. Ключі, журнал і налаштування (~/.eth-agent) не чіпає.
set -e
DL="$HOME/storage/downloads"
ZIP=$(ls -t "$DL"/eth-agent*.zip 2>/dev/null | head -1 || true)
if [ -z "$ZIP" ]; then
  echo "У «Завантаженнях» немає eth-agent*.zip. Скачай архів (з чату або GitHub Releases) і повтори."
  exit 1
fi
echo "Архів: $ZIP ($(du -k "$ZIP" | cut -f1) КБ)"
if ! unzip -tq "$ZIP" >/dev/null 2>&1; then
  echo "Це не архів (найчастіше браузер зберіг сторінку входу GitHub). Видали його і скачай знову:"
  echo "  rm \"$ZIP\""
  exit 1
fi
TMP="$HOME/.eth-agent-update"
rm -rf "$TMP" && mkdir -p "$TMP"
unzip -q "$ZIP" -d "$TMP"
SRC=$(find "$TMP" -maxdepth 2 -name pyproject.toml -printf '%h\n' | head -1)
if [ -z "$SRC" ] || [ ! -f "$SRC/agent/__init__.py" ]; then
  echo "В архіві немає агента — скачай правильний eth-agent-beta.zip"; exit 1
fi
NEW=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/agent/__init__.py")
OLD=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HOME/eth-agent-main/agent/__init__.py" 2>/dev/null || true)
pkill -f "agent[.]cli live" 2>/dev/null && sleep 2 || true
rm -rf "$HOME/eth-agent-main.old"
[ -d "$HOME/eth-agent-main" ] && mv "$HOME/eth-agent-main" "$HOME/eth-agent-main.old"
mv "$SRC" "$HOME/eth-agent-main"
rm -rf "$TMP" "$HOME/eth-agent-main.old"
echo "Оновлено: ${OLD:-—} → ${NEW:-?}"
echo "Запуск:   cd ~/eth-agent-main && bash scripts/run.sh"
