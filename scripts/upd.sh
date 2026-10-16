#!/data/data/com.termux/files/usr/bin/bash
# Оновлення одним рухом із GitHub (після одноразового налаштування git + gh, див. docs/PHONE.md):
#   upd
# Зупиняє агента, підтягує свіжий код з main, показує версію і запускає агента знову.
# Ключі, журнал, стан і налаштування лежать у ~/.eth-agent — оновлення їх не чіпає.
cd ~/eth-agent-main || { echo "Немає ~/eth-agent-main"; exit 1; }
pkill -f "agent[.]cli live" 2>/dev/null; sleep 1
OLD=$(python -c "import agent; print(agent.__version__)" 2>/dev/null)
git fetch -q origin main && git reset -q --hard origin/main || { echo "Не вдалося завантажити з GitHub — перевір інтернет або gh auth status"; exit 1; }
NEW=$(python -c "import agent; print(agent.__version__)")
echo "Оновлено: ${OLD:-?} → $NEW"
exec bash scripts/run.sh
