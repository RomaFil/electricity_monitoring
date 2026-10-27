#!/bin/bash
# When the charger is back (and at every start of power-guard): bring the NAS disk back.
# Counterpart of disk-detach.sh. Idempotent: every step is skipped if it is already done.
DEV=/dev/disk/by-id/ata-FUJITSU_MHZ2320BH_G2_K618T8A2P499
POOL=NAS
CT=100
MERGED=/mnt/pve/unified_pool
STATE=/var/lib/power-guard/disk-detached
LOG=/var/lib/power-guard/disk.log
log() { echo "$(date '+%F %T') attach: $*" >> "$LOG"; }

if [ ! -e "$DEV" ]; then
    log "start: disk is not there, rescanning"
    for h in /sys/class/scsi_host/host*; do echo "- - -" > "$h/scan"; done
    for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
    [ -e "$DEV" ] || { log "FAILED: the disk did not come back"; exit 1; }
fi
if ! zpool list -H "$POOL" > /dev/null 2>&1; then
    log "importing $POOL"
    timeout 120 zpool import "$POOL" >> "$LOG" 2>&1 || timeout 120 zpool import -d /dev/disk/by-id "$POOL" >> "$LOG" 2>&1 || {
        log "FAILED: zpool import"
        exit 1
    }
fi
zfs mount -a >> "$LOG" 2>&1
mountpoint -q "$MERGED" || mount "$MERGED" >> "$LOG" 2>&1 || log "mount $MERGED failed"
pct status "$CT" 2>/dev/null | grep -q running || timeout 90 pct start "$CT" >> "$LOG" 2>&1 || log "pct start failed"
if [ -e "$STATE" ]; then
    rm -f "$STATE"
    log "done: pool imported, container $CT started"
fi
