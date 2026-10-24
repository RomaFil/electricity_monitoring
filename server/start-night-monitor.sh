#!/bin/bash
# Starts night-monitor.py detached from the SSH session; the log is ~/svitlo-local/night-monitor.log
cd "$(dirname "$0")"
echo "---- detailed log from $(date "+%d.%m %H:%M")" >> night-monitor.log
setsid nohup python3 night-monitor.py "${1:-10}" >> night-monitor.log 2>&1 < /dev/null &
