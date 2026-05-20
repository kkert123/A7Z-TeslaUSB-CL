#!/bin/bash
# fsck_check.sh - 定期 exFAT 文件系统检查
# 由 teslausb-fsck.timer 触发（每周日凌晨 3:00）

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $1"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $1"; }

log "开始定期 exFAT 文件系统检查..."

# 只在 Edit Mode 时做 fsck（Present Mode 时分区被 Tesla 占用）
if [ -f /tmp/teslausb_mode ] && [ "$(cat /tmp/teslausb_mode)" = "present" ]; then
    warn "当前为 Present Mode，跳过 fsck（分区正被 Tesla 使用）"
    exit 0
fi

FSK_ERROR=0
for part in nvme0n1p2 nvme0n1p3 nvme0n1p4 nvme0n1p5; do
    if mount | grep -q "/dev/$part"; then
        warn "  /dev/$part 已挂载，跳过 fsck"
        continue
    fi
    
    log "  fsck /dev/$part..."
    if ! fsck.exfat -p /dev/$part 2>&1 | tee -a /var/log/teslausb.log; then
        warn "  fsck /dev/$part 发现问题，尝试 -y 自动修复..."
        if ! fsck.exfat -y /dev/$part 2>&1 | tee -a /var/log/teslausb.log; then
            warn "  fsck /dev/$part 修复失败！"
            FSK_ERROR=1
        fi
    fi
done

if [ "$FSK_ERROR" -eq 1 ]; then
    warn "部分分区 fsck 修复失败"
    logger -t teslausb "WARNING: Scheduled exFAT fsck found unfixable errors"
    exit 1
fi

log "定期 exFAT 文件系统检查完成"
exit 0
