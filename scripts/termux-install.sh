#!/data/data/com.termux/files/usr/bin/bash
# Встановлення ETH-Агента в Termux (Android). Запуск з теки проєкту: bash scripts/termux-install.sh
# pip у Termux НЕ оновлюємо (це ламає пакет python); після оновлення Python — завжди "python -m pip".
set -e
pkg update -y
pkg upgrade -y
pkg install -y python
python -m pip install pyyaml
mkdir -p ~/.eth-agent
if [ ! -f ~/.eth-agent/secrets.env ]; then
  cat > ~/.eth-agent/secrets.env <<'EOF'
# Ключі ТІЛЬКИ від testnet.binance.vision (тестові гроші). Цей файл не потрапляє в git.
BINANCE_API_KEY=
BINANCE_API_SECRET=
# Необов'язково: сповіщення в Telegram
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
EOF
  chmod 600 ~/.eth-agent/secrets.env
fi
python -m agent.cli live --help >/dev/null && echo "Готово. Запуск: bash scripts/run.sh paper"
