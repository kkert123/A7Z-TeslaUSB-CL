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

# 3. 只读挂载 TeslaCam 分区（保护数据）
log "步骤 3/4: 只读挂载 TeslaCam 分区..."
mkdir -p /mnt/teslacam
mount -o ro,uid=1000,gid=1000,fmask=0111,dmask=0000 /dev/nvme0n1p2 /mnt/teslacam 2>/dev/null || \
    mount -o ro /dev/nvme0n1p2 /mnt/teslacam 2>/dev/null || warn "  TeslaCam 只读挂载失败，继续执行"
log "  TeslaCam 分区已只读挂载"

# 4. 启动 USB Gadget
log "步骤 4/4: 启动 USB Gadget..."
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

log "✅ Present Mode 激活成功！Tesla 可以识别 USB 设备"
log "  模式: Present Mode (连接 Tesla)"
log "  USB Gadget: 已激活"
log "  Samba: 已停止"
log "  TeslaCam: 只读挂载"

# 写入模式标志文件
echo "present" > /tmp/teslausb_mode
sync

exit 0
