#!/data/data/com.termux/files/usr/bin/bash
# Запуск агента: bash scripts/run.sh paper   або   bash scripts/run.sh testnet
# Сторінка статусу: http://127.0.0.1:8765 у браузері телефона.
MODE="${1:-paper}"
cd "$(dirname "$0")/.."
command -v termux-wake-lock >/dev/null && termux-wake-lock   # не давати Android присипляти процес
exec python -m agent.cli live --mode "$MODE"
