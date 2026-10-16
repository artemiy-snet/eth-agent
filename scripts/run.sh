#!/data/data/com.termux/files/usr/bin/bash
# Запуск агента:   bash scripts/run.sh                 — режим і склад з панелі (за замовчуванням paper, портфель)
#                  bash scripts/run.sh paper            — примусово paper
#                  bash scripts/run.sh testnet config.portfolio.yaml
# Панель: http://127.0.0.1:8765 у браузері телефона. Кнопка «Перезапустити» в панелі застосовує налаштування.
MODE="${1:-}"
CONFIG="${2:-config.portfolio.yaml}"
cd "$(dirname "$0")/.."
command -v termux-wake-lock >/dev/null && termux-wake-lock   # не давати Android присипляти процес
ARGS=()
[ -n "$MODE" ] && [ "$MODE" != "auto" ] && ARGS=(--mode "$MODE")
while true; do
  python -m agent.cli live "${ARGS[@]}" --config "$CONFIG"
  code=$?
  case "$code" in
    0|2|4|130) exit "$code" ;;           # штатна зупинка / live не підтверджено / порт зайнятий / CTRL+C
    3) echo "Перезапуск з новими налаштуваннями…"; sleep 2 ;;
    *) echo "Агент упав (код $code) — автоперезапуск через 15 с. Зупинити: CTRL+C"; sleep 15 ;;
  esac
  ARGS=()                                # після перезапуску режим береться з налаштувань панелі
done
