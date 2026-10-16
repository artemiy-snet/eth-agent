#!/data/data/com.termux/files/usr/bin/bash
# Автозапуск агента після перезавантаження/вмикання телефона (потрібен застосунок Termux:Boot з F-Droid,
# відкрити його один раз). Встановлення:  bash ~/eth-agent-main/scripts/autostart.sh
# Вимкнути автозапуск:  rm ~/.termux/boot/eth-agent.sh
set -e
mkdir -p ~/.termux/boot ~/.eth-agent
cat > ~/.termux/boot/eth-agent.sh <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
sleep 30                                   # дати телефону підключитися до Wi-Fi
cd ~/eth-agent-main && nohup bash scripts/run.sh >> ~/.eth-agent/run.log 2>&1 &
BOOT
chmod +x ~/.termux/boot/eth-agent.sh
echo "Готово. Після вмикання телефона агент стартує сам (через ~30 с)."
echo "Перевір, що встановлено Termux:Boot з F-Droid і що його відкривали хоча б раз."
echo "Журнал запуску: tail -f ~/.eth-agent/run.log"
