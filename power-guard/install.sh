#!/bin/bash
# Встановлення power-guard на Proxmox-хост. Запуск від root з теки power-guard/.
# Повторний запуск безпечний: конфіг /etc/power-guard.conf не перезаписується.
set -euo pipefail
cd "$(dirname "$0")"

install -d -m 755 /usr/local/lib/power-guard
install -m 755 power_guard.py /usr/local/lib/power-guard/power_guard.py
install -m 755 disk-detach.sh disk-attach.sh /usr/local/lib/power-guard/
install -d -m 700 /var/lib/power-guard
if [ ! -f /etc/power-guard.conf ]; then
    install -m 600 power-guard.conf.example /etc/power-guard.conf
    echo "Створено /etc/power-guard.conf — впиши TOKEN"
fi
install -m 644 power-guard.service /etc/systemd/system/power-guard.service
# Кришка: якщо в logind.conf уже стоїть HandleLidSwitch=ignore, нічого не чіпаємо.
if grep -qE '^\s*HandleLidSwitch\s*=\s*ignore' /etc/systemd/logind.conf; then
    echo "Кришка вже налаштована в /etc/systemd/logind.conf"
else
    install -d -m 755 /etc/systemd/logind.conf.d
    install -m 644 logind-lid.conf /etc/systemd/logind.conf.d/svitlo-lid.conf
    echo "Кришку налаштовано; діє після перезавантаження або: systemctl restart systemd-logind"
fi

systemctl daemon-reload
systemctl enable --now power-guard.service
systemctl restart power-guard.service
echo "Готово. Журнал: journalctl -u power-guard -f"
