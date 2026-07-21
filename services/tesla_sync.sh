#!/bin/bash
# ===========================================================
# tesla_sync.sh — 视频同步入口脚本
# 由 NM dispatcher / systemd timer / Web 手动触发
# ===========================================================

set -e

SYNC_SERVICE="/opt/radxa_data/teslausb/sync_service.py"
LOG_TAG="tesla_sync"

log() {
    echo "[$(date '+%H:%M:%S')] $*" | systemd-cat -t "$LOG_TAG" 2>/dev/null
    echo "[$(date '+%H:%M:%S')] $*"
}

# ── 1. 确保 rsync 可用 ──
if ! command -v rsync &>/dev/null; then
    log "ERROR: rsync not installed"
    exit 1
fi

# ── 2. 加载配置检查是否启用 ──
CONFIG_FILE="/opt/radxa_data/teslausb/config/sync.json"
if [ -f "$CONFIG_FILE" ]; then
    ENABLED=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('enabled', False))" 2>/dev/null || echo "False")
    if [ "$ENABLED" != "True" ]; then
        log "Sync not enabled, exiting"
        exit 0
    fi
else
    log "Config not found, exiting"
    exit 0
fi

# ── 3. WiFi SSID 检查 ──
HOME_SSID=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('home_ssid', ''))" 2>/dev/null || echo "")
CURRENT_SSID=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | grep '^yes:' | cut -d: -f2-)

if [ -n "$HOME_SSID" ] && [ "$CURRENT_SSID" != "$HOME_SSID" ]; then
    log "SSID mismatch: current='$CURRENT_SSID' != home='$HOME_SSID', skipping"
    exit 0
fi

# ── 4. Present Mode 检查 ──
if [ -f "/tmp/teslausb_mode" ]; then
    MODE=$(cat /tmp/teslausb_mode)
    if [ "$MODE" = "present" ]; then
        log "Present Mode active, skipping sync"
        exit 0
    fi
fi

# ── 5. 执行同步 ──
log "Starting sync (SSID=$CURRENT_SSID)..."
cd /opt/radxa_data/teslausb

python3 -c "
import json, sys
sys.path.insert(0, '.')
from sync_service import run_sync
result = run_sync()
print(json.dumps(result, ensure_ascii=False))
" 2>&1 | while IFS= read -r line; do
    log "$line"
done

EXIT_CODE=${PIPESTATUS[0]}
if [ $EXIT_CODE -eq 0 ]; then
    log "Sync completed successfully"
else
    log "Sync failed with exit code $EXIT_CODE"
fi

exit $EXIT_CODE
