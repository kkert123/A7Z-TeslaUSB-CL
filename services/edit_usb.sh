#!/bin/bash
# edit_usb.sh - 切换到 Edit Mode（网络访问）
# 功能：停用 USB Gadget、可写挂载分区、启动 Samba
# 版本：v2 (修复 set -e 导致的崩溃问题)

# 不启用 set -e，改为显式错误检查
GADGET_DIR="/sys/kernel/config/usb_gadget/tesla_usb"
LOG_FILE="/var/log/teslausb.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $1" | tee -a "$LOG_FILE"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $1" | tee -a "$LOG_FILE"; }
err() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $1" | tee -a "$LOG_FILE"; }

log "正在切换到 Edit Mode（网络访问）..."

# 1. 停止 USB Gadget
log "步骤 1/6: 停止 USB Gadget..."
if [ -x /opt/radxa_data/usb_gadget_init.sh ]; then
    log "  调用 usb_gadget_init.sh stop..."
    bash /opt/radxa_data/usb_gadget_init.sh stop
else
    warn "  usb_gadget_init.sh 不存在，手动清理..."
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    for i in 0 1 2 3; do
        rmdir "$GADGET_DIR/functions/mass_storage.usb0/lun.$i" 2>/dev/null || true
    done
    rmdir "$GADGET_DIR/functions/mass_storage.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi
log "  USB Gadget 已停止"

# 2. 卸载只读挂载（如果存在）
log "步骤 2/6: 卸载只读挂载..."
if mountpoint -q /mnt/teslacam 2>/dev/null; then
    log "  卸载 /mnt/teslacam..."
    umount /mnt/teslacam 2>/dev/null || umount -l /mnt/teslacam 2>/dev/null || warn "  卸载 TeslaCam 失败"
fi
log "  只读挂载卸载完成"

# 3. 可写挂载所有分区（带错误检查）
log "步骤 3/6: 可写挂载分区..."

mount_partition() {
    local device=$1
    local mount_point=$2
    local label=$3
    
    # 如果已挂载，先卸载
    if mountpoint -q "$mount_point" 2>/dev/null; then
        log "  $label 已挂载，跳过"
        return 0
    fi
    
    # 尝试挂载
    mkdir -p "$mount_point"
    
    # 方法 1：带 UID/GID 选项
    if mount -o uid=1000,gid=1000,fmask=0111,dmask=0000 "$device" "$mount_point" 2>/dev/null; then
        log "  $label 已可写挂载: $mount_point"
        return 0
    fi
    
    # 方法 2：不带选项
    if mount "$device" "$mount_point" 2>/dev/null; then
        log "  $label 已可写挂载: $mount_point"
        return 0
    fi
    
    warn "  $label 挂载失败 ($device)"
    return 1
}

# 挂载所有分区
mount_partition /dev/nvme0n1p2 /mnt/teslacam "TeslaCam"
mount_partition /dev/nvme0n1p3 /mnt/music "Music"
mount_partition /dev/nvme0n1p4 /mnt/lightshow "LightShow"
mount_partition /dev/nvme0n1p5 /mnt/boombox "Boombox"

log "  分区挂载完成"

# 3.5. 同步 Staging Area 文件到真实分区 (Task 2.0)
log "步骤 3.5/6: 同步临时区域文件（分段上传）..."
STAGING_DIR="/opt/radxa_data/staging"

sync_staging() {
    local name=$1
    local staging_path="$STAGING_DIR/$name"
    local target_path=$2

    if [ ! -d "$staging_path" ]; then
        return 0
    fi

    local count=$(ls -1 "$staging_path" 2>/dev/null | wc -l)
    if [ "$count" -eq 0 ]; then
        return 0
    fi

    log "  同步 $name: $count 个文件..."

    for file in "$staging_path"/*; do
        [ -e "$file" ] || continue
        local filename=$(basename "$file")
        local target_file="$target_path/$filename"

        if [ -f "$target_file" ]; then
            warn "  跳过 $filename（目标已存在，保留原文件）"
            rm -f "$file"
        else
            if mv "$file" "$target_path/" 2>/dev/null; then
                log "  ✅ $filename → $target_path"
            else
                warn "  移动 $filename 失败"
            fi
        fi
    done

    log "  $name 同步完成"
}

sync_staging "music" "/mnt/music"
sync_staging "lightshow" "/mnt/lightshow"
sync_staging "boombox" "/mnt/boombox"

log "  临时区域同步完成"

# 4. 安装 Samba（如果未安装）
log "步骤 4/6: 检查 Samba..."
if ! command -v smbd &>/dev/null; then
    warn "  Samba 未安装，正在安装..."
    apt update && apt install -y samba || { err "  Samba 安装失败"; exit 1; }
fi
log "  Samba 已安装"

# 5. 配置 Samba
log "步骤 5/6: 配置 Samba..."

# 备份原配置（如果还没备份）
if [ ! -f /etc/samba/smb.conf.bak ]; then
    cp /etc/samba/smb.conf /etc/samba/smb.conf.bak 2>/dev/null || true
fi

# 写入 Samba 配置
cat > /etc/samba/smb.conf << 'SAMBA_EOF'
[global]
   workgroup = WORKGROUP
   server string = TeslaUSB A7Z
   security = user
   map to guest = Bad User
   guest account = nobody
   obey pam restrictions = yes
   unix password sync = no
   server min protocol = SMB2_02
   client min protocol = SMB2_02
   smb ports = 445

[TeslaCam]
   path = /mnt/teslacam
   browsable = yes
   writable = yes
   read only = no
   guest ok = no
   valid users = teslausb
   create mask = 0755
   directory mask = 0755
   force user = root

[MusicShow]
   path = /mnt/music
   browsable = yes
   writable = yes
   read only = no
   guest ok = no
   valid users = teslausb
   create mask = 0755
   directory mask = 0755
   force user = root
SAMBA_EOF

log "  Samba 配置已更新"

# 创建 Samba 用户（如果不存在）
if ! id teslausb &>/dev/null; then
    log "  创建系统用户 teslausb..."
    useradd -M -s /sbin/nologin teslausb 2>/dev/null || true
fi

if ! pdbedit -L | grep -q teslausb; then
    log "  创建 Samba 用户 teslausb..."
    SMB_PASS="${SMB_PASSWORD:-CHANGE_ME_SMB_PASSWORD}"; (echo "$SMB_PASS"; echo "$SMB_PASS") | smbpasswd -a -s teslausb 2>/dev/null || warn "  Samba 用户创建失败，请手动执行: smbpasswd -a teslausb"
fi

# 6. 启动 Samba（仅 SMB2+，禁用 NetBIOS）
log "步骤 6/7: 启动 Samba 服务（SMB2+）..."
systemctl stop nmbd 2>/dev/null || true
systemctl disable nmbd 2>/dev/null || true
systemctl restart smbd 2>/dev/null || true
systemctl enable smbd 2>/dev/null || true

# 验证 Samba 是否运行
sleep 2
if systemctl is-active --quiet smbd; then
    log "  Samba 已启动"
else
    warn "  Samba 启动失败，请检查日志: journalctl -u smbd"
fi

# 7. 防火墙加固（封 137-139 NetBIOS 端口）
log "步骤 7/7: 防火墙加固..."
if ! iptables -L INPUT | grep -q "dport 139"; then
    iptables -A INPUT -p tcp --dport 139 -j DROP
    iptables -A INPUT -p udp --dport 137:138 -j DROP
    # 安装持久化（如果没有）
    apt install -y iptables-persistent netfilter-persistent 2>/dev/null || true
    iptables-save > /etc/iptables/rules.v4
    log "  防火墙规则已添加: DROP 137-139"
else
    log "  防火墙规则已存在"
fi

# 8. 显示访问信息
log "✅ Edit Mode 激活成功！可以通过网络访问文件"
log "  模式: Edit Mode (网络访问)"
log "  USB Gadget: 已停用"
log "  Samba: $(systemctl is-active smbd)"
log "  访问路径:"
log "    \\\\$(hostname -I | awk '{print $1}')\\TeslaCam"
log "    \\\\$(hostname -I | awk '{print $1}')\\MusicShow"
log "  用户名: teslausb"
log "  密码: tesla"

# 写入模式标志文件
echo "edit" > /tmp/teslausb_mode
sync

exit 0
