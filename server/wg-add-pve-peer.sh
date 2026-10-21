#!/bin/bash
# Додає домашній pve як WireGuard-пір на VPS. Запуск: sudo ./wg-add-pve-peer.sh <публічний ключ pve>
# Пір отримує одну адресу 10.0.0.X/32. Нічого назовні не відкривається:
# порт WireGuard на VPS уже працює для інших пірів.
set -euo pipefail
PUB="$1"
CONF=/etc/wireguard/wg0.conf
OUT=/home/admin1/svitlo-local/wg-pve-peer.txt

if wg show wg0 peers | grep -qxF "$PUB"; then
    IP=$(wg show wg0 allowed-ips | awk -v k="$PUB" '$1==k {print $2}' | cut -d/ -f1)
    echo "пір уже є: $IP"
else
    used=$( (wg show wg0 allowed-ips | awk '{for(i=2;i<=NF;i++) print $i}'; echo 10.0.0.1/32) | grep -o '^10\.0\.0\.[0-9]*' | cut -d. -f4 | sort -n)
    for n in $(seq 2 254); do
        echo "$used" | grep -qx "$n" || { IP=10.0.0.$n; break; }
    done
    cp -a "$CONF" "$CONF.bak-$(date +%Y%m%d-%H%M%S)"
    printf '\n# pve (домашній Proxmox, Svitlo-Local power-guard), додано %s\n[Peer]\nPublicKey = %s\nAllowedIPs = %s/32\n' \
        "$(date +%F)" "$PUB" "$IP" >> "$CONF"
    wg set wg0 peer "$PUB" allowed-ips "$IP/32"
    echo "пір додано: $IP"
fi

{
    echo "PEER_IP=$IP"
    echo "SERVER_PUB=$(wg show wg0 public-key)"
    echo "SERVER_PORT=$(wg show wg0 listen-port)"
} > "$OUT"
chown admin1:admin1 "$OUT"
cat "$OUT"
