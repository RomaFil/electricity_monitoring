#!/bin/bash
# Before the laptop sleeps on battery: stop the NAS and take its failing disk away from the system.
# Why: since 26.10.2026 the old HDD (sdb, ZFS pool NAS) gives read errors, and the laptop died while
# waking it from sleep (journal ends at rtcwake, the RTC alarm then starts it cold 5 min later).
# The data is copied to the VPS. Idempotent: does nothing if the disk is already gone.
# Called by power-guard (DISK_DETACH_CMD). The counterpart is disk-attach.sh.
DEV=/dev/disk/by-id/ata-FUJITSU_MHZ2320BH_G2_K618T8A2P499
POOL=NAS
CT=100
MERGED=/mnt/pve/unified_pool
STATE=/var/lib/power-guard/disk-detached
LOG=/var/lib/power-guard/disk.log
log() { echo "$(date '+%F %T') detach: $*" >> "$LOG"; }

[ -e "$DEV" ] || exit 0   # already detached (or the disk is dead)
disk=$(basename "$(readlink -f "$DEV")")
host=$(readlink -f "/sys/block/$disk/device" | grep -o 'host[0-9]*' | head -1)
log "start: disk=$disk $host"

if pct status "$CT" 2>/dev/null | grep -q running; then
    timeout 90 pct shutdown "$CT" --forceStop 1 --timeout 40 >> "$LOG" 2>&1 || log "pct shutdown failed"
fi
mountpoint -q "$MERGED" && { umount "$MERGED" >> "$LOG" 2>&1 || umount -l "$MERGED" >> "$LOG" 2>&1; }
if zpool list -H "$POOL" > /dev/null 2>&1; then
    timeout 90 zpool export "$POOL" >> "$LOG" 2>&1 || timeout 90 zpool export -f "$POOL" >> "$LOG" 2>&1 || {
        log "FAILED: zpool export; the disk stays"
        exit 1
    }
fi
sync
echo "$host" > "$STATE"
echo 1 > "/sys/block/$disk/device/delete"   # the kernel parks the disk and forgets it
sleep 1
log "done: pool exported, $disk removed"
