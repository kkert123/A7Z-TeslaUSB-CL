#!/bin/bash
# io_tuning.sh - TeslaUSB A7Z I/O 调度优化
# 策略: kyber (MMC) + VM 参数调优 + NVMe 队列优化
# 由 teslausb-io-tune.service 开机自动应用

set -e

log()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [IO-TUNE] $1"; }
warn()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [IO-TUNE] WARN: $1"; }
apply() { echo "$2" > "$1" 2>/dev/null && log "  $1 = $2" || warn "  $1 设置失败"; }

log "TeslaUSB I/O 调度优化开始..."

# ═══════════════════════════════════════════════
# 1. MMC (eMMC) 调度器 → kyber
#    kyber 对混合读写场景（系统盘 + 日志）延迟更可控
# ═══════════════════════════════════════════════
MMC_DEV="mmcblk0"
if [ -f "/sys/block/$MMC_DEV/queue/scheduler" ]; then
    CUR=$(cat "/sys/block/$MMC_DEV/queue/scheduler")
    log "MMC 当前调度器: $CUR"
    if echo "$CUR" | grep -q 'kyber'; then
        echo "kyber" > "/sys/block/$MMC_DEV/queue/scheduler" 2>/dev/null && \
            log "  MMC → kyber" || warn "  MMC kyber 切换失败"
    else
        warn "  kyber 不可用，保持 $CUR"
    fi
    # MMC read-ahead: 增大提升顺序读性能
    apply "/sys/block/$MMC_DEV/queue/read_ahead_kb" 256
fi

# ═══════════════════════════════════════════════
# 2. NVMe 调度器保持 none（NVMe 原生最快）
#    但优化队列深度避免写饥饿
# ═══════════════════════════════════════════════
NVME_DEV="nvme0n1"
if [ -f "/sys/block/$NVME_DEV/queue/nr_requests" ]; then
    # 减少队列深度：127→64，降低延迟
    apply "/sys/block/$NVME_DEV/queue/nr_requests" 64
    # 保持 read-ahead 128KB
    apply "/sys/block/$NVME_DEV/queue/read_ahead_kb" 128
    # 启用 IO 统计（监控需要）
    apply "/sys/block/$NVME_DEV/queue/iostats" 1
    # 保持合并（顺序写优化）
    apply "/sys/block/$NVME_DEV/queue/nomerges" 0
    log "NVMe 保持 none 调度器 + 队列 64"
fi

# ═══════════════════════════════════════════════
# 3. VM 参数调优（通过 /proc/sys/vm/）
# ═══════════════════════════════════════════════
log "VM 参数调优..."

# dirty_ratio: 50→10 (%)
# 只有 1GB RAM，50% = 480MB 脏页会导致巨大 I/O 风暴
# 10% = ~96MB 脏页上限，刷新更平滑
apply "/proc/sys/vm/dirty_ratio" 10

# dirty_background_ratio: 1→5 (%)
# 给后台刷新更多余地，减少小刷新次数
apply "/proc/sys/vm/dirty_background_ratio" 5

# dirty_expire_centisecs: 3000→1500 (30s→15s)
# 脏数据更快过期，避免积压
apply "/proc/sys/vm/dirty_expire_centisecs" 1500

# dirty_writeback_centisecs: 500→300 (5s→3s)
# 更频繁的小批量刷新，而非偶尔的大批量
apply "/proc/sys/vm/dirty_writeback_centisecs" 300

# vfs_cache_pressure: 500→100 (CRITICAL FIX)
# 500 激进回收 dentry/inode → 频繁读盘
# 100 默认值，保留缓存提升性能
apply "/proc/sys/vm/vfs_cache_pressure" 100

# swappiness: 100→10
# A7Z 有 zram + NVMe swap 但不应该滥用
# 10 = 仅在内存真正不足时 swap
apply "/proc/sys/vm/swappiness" 10

# page-cluster: 3→0
# 单次 page fault 预读 2^3=8 页 → 改为 2^0=1 页
# 减少不必要的预读开销（低内存设备）
apply "/proc/sys/vm/page-cluster" 0

log "VM 参数已优化"

# ═══════════════════════════════════════════════
# 4. Web 服务 I/O 优先级降低
#    Tesla 录像 > 系统日志 > Web 服务
# ═══════════════════════════════════════════════
WEB_PID=$(pidof -s python3 2>/dev/null || true)
if [ -n "$WEB_PID" ]; then
    ionice -c 2 -n 4 -p "$WEB_PID" 2>/dev/null && \
        log "  Web 服务 (pid=$WEB_PID) I/O 优先级: best-effort/nice=4" || \
        warn "  ionice 设置失败"
fi

# ═══════════════════════════════════════════════
# 5. 验证输出
# ═══════════════════════════════════════════════
log "=== 优化完成 ==="
echo "  MMC scheduler : $(cat /sys/block/mmcblk0/queue/scheduler 2>/dev/null)"
echo "  NVMe scheduler: $(cat /sys/block/nvme0n1/queue/scheduler 2>/dev/null)"
echo "  NVMe requests : $(cat /sys/block/nvme0n1/queue/nr_requests 2>/dev/null)"
echo "  dirty_ratio   : $(cat /proc/sys/vm/dirty_ratio)"
echo "  dirty_bg_ratio: $(cat /proc/sys/vm/dirty_background_ratio)"
echo "  cache_pressure: $(cat /proc/sys/vm/vfs_cache_pressure)"
echo "  swappiness    : $(cat /proc/sys/vm/swappiness)"

exit 0
