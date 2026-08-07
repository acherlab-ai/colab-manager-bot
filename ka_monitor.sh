#!/bin/bash
# Monitor a bot-created lab: VM alive? keep-alive daemon? proxy in daemon env?
# Writes one line per check. Run detached WITHOUT proxy env.
ACC_HOME=/root/colab-bot/data/8901416214/acc1
CFG=$ACC_HOME/.config/colab-cli/sessions.json
COLAB=/tmp/opencode/colab-venv/bin/colab
OUT=/root/colab-bot/ka_monitor.out
echo "monitor start $(date '+%F %T %z')" > "$OUT"
for i in $(seq 1 25); do
  T=$(date '+%H:%M:%S')
  SESS=$($COLAB --config "$CFG" sessions 2>&1)
  LAB=$(echo "$SESS" | grep -o 'lab-[a-f0-9]\{4\}' | head -1)
  KP=$(ps aux | grep 'keep-alive' | grep -v grep | awk '{print $2}' | head -1)
  PX="none"
  if [ -n "$KP" ]; then
    PX=$(tr '\0' '\n' < /proc/$KP/environ 2>/dev/null | grep -i proxy | tr '\n' ' ')
    [ -z "$PX" ] && PX="none"
  fi
  echo "$T | lab=$LAB | daemon_pid=${KP:-none} | daemon_proxy=[$PX] | $SESS" >> "$OUT"
  sleep 55
done
echo "monitor end $(date '+%F %T %z')" >> "$OUT"
