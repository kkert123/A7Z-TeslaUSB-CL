#!/bin/bash
# present_usb.sh - 切换到 Present Mode（连接 Tesla）
# 功能：激活 USB Gadget、只读挂载分区、停止 Samba

set -e
GADGET_DIR="/sys/kernel/config/usb_gadget/tesla_usb"
LOG_TAG="present_usb"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $1" | tee -a /var/log/teslausb.log; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $1" | tee -a /var/log/teslausb.log; }
die() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $1" | tee -a /var/log/teslausb.log; exit 1; }

log "正在切换到 Present Mode（连接 Tesla）..."

# 1. 停止 Samba 服务
log "步骤 1/4: 停止 Samba 服务..."
systemctl stop smbd nmbd 2>/dev/null || true
log "  Samba 已停止"

# 2. 卸载可写挂载的分区
log "步骤 2/4: 卸载可写挂载的分区..."
for part in nvme0n1p2 nvme0n1p3 nvme0n1p4 nvme0n1p5; do
    mountpoint=$(findmnt -n -o TARGET /dev/$part 2>/dev/null || true)
    if [ -n "$mountpoint" ]; then
        log "  卸载 $part ($mountpoint)..."
        umount "$mountpoint" 2>/dev/null || umount -l "$mountpoint" 2>/dev/null || warn "  卸载 $part 失败"
    fi
done
log "  分区卸载完成"

# 2.5. 分区 fsck 检查（交给 Tesla 前必做）
log "步骤 2.5/5: exFAT 文件系统检查..."
FSK_ERROR=0
for part in nvme0n1p2 nvme0n1p3 nvme0n1p4 nvme0n1p5; do
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
    warn "部分分区 fsck 修复失败，发布到 syslog"
    logger -t teslausb "WARNING: exFAT fsck errors detected on boot"
fi
log "  文件系统检查完成"

# 3. 清理旧 Gadget（释放块设备，否则挂载会因设备忙而失败）
log "步骤 3/5: 清理旧 USB Gadget..."
if [ -d "$GADGET_DIR" ]; then
    warn "  发现残留 Gadget，先清理..."
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    for i in 0 1 2 3; do
        rmdir "$GADGET_DIR/functions/mass_storage.usb0/lun.$i" 2>/dev/null || true
    done
    rmdir "$GADGET_DIR/functions/mass_storage.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
    sleep 1
fi
log "  旧 Gadget 已清理"

# 等待块设备释放（旧 Gadget 可能还持有设备引用）
log "  等待块设备释放..."
for i in $(seq 1 10); do
    holders=$(ls /sys/block/nvme0n1/nvme0n1p2/holders 2>/dev/null | wc -l)
    if [ "$holders" -eq 0 ]; then
        log "  块设备已释放（第 ${i}s）"
        break
    fi
    sleep 1
done

# 4. 只读挂载 TeslaCam 分区（使用 losetup 创建只读 loop 设备绕过内核 busy 限制）
log "步骤 4/5: 只读挂载 TeslaCam 分区..."
mkdir -p /mnt/teslacam
# 先尝试直接挂载（快速路径）
if mount -o ro,noatime /dev/nvme0n1p2 /mnt/teslacam 2>/dev/null; then
    log "  ✅ TeslaCam 直接只读挂载成功"
else
    # 直接挂载失败（设备被 Gadget 锁定），使用 losetup 创建只读 loop 设备
    warn "  直接挂载失败，尝试 losetup 只读 loop 方式..."
    # 清理可能遗留的 loop 设备
    for lo in /dev/loop0 /dev/loop1 /dev/loop2; do
        losetup -d "$lo" 2>/dev/null || true
    done
    LO=$(losetup --show -f -r /dev/nvme0n1p2 2>/dev/null)
    if [ -n "$LO" ] && mount -o ro "$LO" /mnt/teslacam 2>/dev/null; then
        log "  ✅ TeslaCam 通过 $LO 只读挂载成功"
    else
        warn "  ⚠️ TeslaCam 只读挂载失败，Web 视频页面将无数据"
    fi
fi

# 5. 启动 USB Gadget（新版本 usb_gadget_init.sh v8+ 会保留已有 RO 挂载）
log "步骤 5/5: 启动 USB Gadget..."
if [ -d "$GADGET_DIR" ]; then
    warn "  发现残留 Gadget，先清理..."
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    for i in 0 1 2 3; do
        rmdir "$GADGET_DIR/functions/mass_storage.usb0/lun.$i" 2>/dev/null || true
    done
    rmdir "$GADGET_DIR/functions/mass_storage.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

# 调用 usb_gadget_init.sh 启动
if [ -x /opt/radxa_data/usb_gadget_init.sh ]; then
    log "  调用 usb_gadget_init.sh start..."
    bash /opt/radxa_data/usb_gadget_init.sh start
else
    die "  usb_gadget_init.sh 不存在或不可执行"
fi

# Gadget 激活后验证挂载状态
if mountpoint -q /mnt/teslacam 2>/dev/null; then
    log "  ✅ TeslaCam 只读挂载正常，Web 视频可访问"
else
    # Gadget 激活时可能卸载了 RO 挂载，尝试最后一次恢复  
    warn "  Gadget 激活后 TeslaCam 未挂载，尝试恢复..."
    sleep 2
    LO=$(losetup --show -f -r /dev/nvme0n1p2 2>/dev/null)
    if [ -n "$LO" ] && mount -o ro "$LO" /mnt/teslacam 2>/dev/null; then
        log "  ✅ TeslaCam 通过 $LO 恢复挂载成功"
    else
        warn "  TeslaCam 只读挂载失败，Web 视频页面将无数据"
    fi
fi

log "✅ Present Mode 激活成功！Tesla 可以识别 USB 设备"
log "  模式: Present Mode (连接 Tesla)"
log "  USB Gadget: 已激活"
log "  Samba: 已停止"
log "  TeslaCam: 只读挂载"

# 写入模式标志文件
echo "present" > /tmp/teslausb_mode
sync

exit 0
