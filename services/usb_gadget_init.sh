#!/bin/bash
# usb_gadget_init.sh - A7Z USB Gadget v8 (Present 模式只读挂载所有分区)
GADGET_NAME="tesla_usb"
GADGET_DIR="/sys/kernel/config/usb_gadget/$GADGET_NAME"
CONFIG_NAME="c.1"

LUN_DEVICES=("/dev/nvme0n1p2" "/dev/nvme0n1p3" "/dev/nvme0n1p4" "/dev/nvme0n1p5")
LUN_LABELS=("TESLACAM" "MUSIC" "LIGHTSHOW" "BOOMBOX")
# 分区 -> 挂载点映射
MOUNT_POINTS=(
    "/mnt/teslacam"
    "/mnt/music"
    "/mnt/lightshow"
    "/mnt/boombox"
)

green()  { echo -e "\e[32m$*\e[0m"; }
yellow() { echo -e "\e[33m$*\e[0m"; }
red()    { echo -e "\e[31m$*\e[0m"; }
die()    { red "X 致命错误: $*"; exit 1; }

get_udc() {
    for name in $(ls -1 /sys/class/udc/ 2>/dev/null); do
        state=$(cat /sys/class/udc/$name/state 2>/dev/null)
        [ "$state" = "not attached" ] || continue
        [[ "$name" == *xhci* ]] && { echo "$name"; return 0; }
    done
    for name in $(ls -1 /sys/class/udc/ 2>/dev/null); do
        state=$(cat /sys/class/udc/$name/state 2>/dev/null)
        [ "$state" = "not attached" ] && { echo "$name"; return 0; }
    done
    return 1
}

cleanup() {
    [ -d "$GADGET_DIR" ] || return 0
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    sleep 1
    rm -f "$GADGET_DIR/configs/$CONFIG_NAME/"* 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/$CONFIG_NAME" 2>/dev/null || true
    for i in 0 1 2 3; do rmdir "$GADGET_DIR/functions/mass_storage.usb0/lun.$i" 2>/dev/null || true; done
    rmdir "$GADGET_DIR/functions/mass_storage.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
}

# 只读挂载所有分区（Present 模式：Tesla 通过 USB 写入，A7Z 本地只读读取）
remount_all_ro() {
    green "  重新只读挂载分区（本地访问）..."
    for i in "${!LUN_DEVICES[@]}"; do
        dev="${LUN_DEVICES[$i]}"
        mp="${MOUNT_POINTS[$i]}"
        [ -z "$mp" ] && continue
        mkdir -p "$mp"
        # 先尝试 umount（可能之前已挂载）
        umount "$dev" 2>/dev/null || umount -l "$mp" 2>/dev/null || true
        sleep 0.5
        # 只读挂载；失败说明 Tesla 正在写入，跳过
        if mount -o ro,noatime "$dev" "$mp" 2>/dev/null; then
            green "    $dev -> $mp (ro)"
        else
            yellow "    $mp 跳过 (设备忙，Tesla 可能正在写入)"
        fi
    done
}

start_gadget() {
    green "=== USB Gadget v8 (Present 模式，全分区只读挂载) ==="

    echo "  步骤 1/9: ConfigFS..."
    mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config

    echo "  步骤 2/9: 加载内核模块..."
    modprobe libcomposite 2>/dev/null && echo "    libcomposite" || true
    modprobe usb_f_mass_storage 2>/dev/null && echo "    usb_f_mass_storage" || true

    echo "  步骤 3/9: 清理旧配置..."
    cleanup; sleep 1

    echo "  步骤 4/9: 检测 UDC 控制器..."
    UDC=$(get_udc)
    [ -z "$UDC" ] && die "未找到可用 UDC"
    green "    选中: $UDC"

    echo "  步骤 5/9: 卸载分区..."
    for dev in "${LUN_DEVICES[@]}"; do
        mp=$(findmnt -n -o TARGET "$dev" 2>/dev/null)
        [ -n "$mp" ] && umount "$dev" 2>/dev/null || umount -l "$dev" 2>/dev/null || true
    done
    sync; sleep 3

    echo "  步骤 6/9: 验证分区已释放..."
    for dev in "${LUN_DEVICES[@]}"; do
        sn=$(basename "$dev")
        findmnt "$dev" &>/dev/null && die "$dev 仍在挂载中"
        h=$(ls /sys/block/nvme0n1/"$sn"/holders 2>/dev/null)
        [ -n "$h" ] && die "$dev 被占用: $h"
        echo "    $dev 空闲"
    done

    echo "  步骤 7/9: 创建 USB Gadget..."
    mkdir -p "$GADGET_DIR"
    echo "0x1d6b" > "$GADGET_DIR/idVendor"
    echo "0x0104" > "$GADGET_DIR/idProduct"
    echo "0x0100" > "$GADGET_DIR/bcdDevice"
    echo "0x0200" > "$GADGET_DIR/bcdUSB"
    mkdir -p "$GADGET_DIR/strings/0x409"
    echo "Radxa"          > "$GADGET_DIR/strings/0x409/manufacturer"
    echo "TeslaUSB A7Z"   > "$GADGET_DIR/strings/0x409/product"
    echo "A7Z-TESLA-0001" > "$GADGET_DIR/strings/0x409/serialnumber"

    echo "  步骤 8/9: 绑定分区 (nofua=1, removable=1)..."
    mkdir -p "$GADGET_DIR/functions/mass_storage.usb0"
    for i in "${!LUN_DEVICES[@]}"; do
        device="${LUN_DEVICES[$i]}"
        label="${LUN_LABELS[$i]}"
        lun_dir="$GADGET_DIR/functions/mass_storage.usb0/lun.$i"
        mkdir -p "$lun_dir"
        echo "$device" > "$lun_dir/file" || { red "    LUN $i 绑定失败"; continue; }
        echo "$label"  > "$lun_dir/inquiry_string" 2>/dev/null || true
        echo 0         > "$lun_dir/ro"              2>/dev/null || true
        echo 1         > "$lun_dir/removable"       2>/dev/null || true
        echo 1         > "$lun_dir/nofua"           2>/dev/null || true
        green "    LUN $i: $device -> $label"
    done

    echo "  步骤 9/9: 激活 Gadget..."
    mkdir -p "$GADGET_DIR/configs/$CONFIG_NAME"
    ln -sf "$GADGET_DIR/functions/mass_storage.usb0" "$GADGET_DIR/configs/$CONFIG_NAME/"
    sleep 1
    echo "$UDC" > "$GADGET_DIR/UDC" || die "UDC 绑定失败"
    sleep 2
    [ "$(cat "$GADGET_DIR/UDC" 2>/dev/null)" = "$UDC" ] || die "UDC 验证失败"

    # === 关键修复：重新只读挂载所有分区 ===
    remount_all_ro

    green ""
    green "  USB Gadget 已激活! UDC=$UDC (nofua)"
    green "  连接特斯拉车机或 PC 即可识别"
    echo ""
    for i in "${!LUN_DEVICES[@]}"; do
        echo "    LUN $i: ${LUN_DEVICES[$i]} -> ${LUN_LABELS[$i]}"
    done
    yellow ""
    yellow "  提示: 所有分区已只读挂载到 /mnt/*，Web 界面可正常访问"
    yellow "  恢复: sudo bash $0 stop"
}

stop_gadget() {
    echo "停止 USB Gadget..."

    # 先卸载本地只读挂载
    for mp in "${MOUNT_POINTS[@]}"; do
        mountpoint -q "$mp" && umount -l "$mp" 2>/dev/null || true
    done

    cleanup
    sync; sleep 2

    # fsck 非 TeslaCam 分区（p3/p4/p5 由 A7Z 管理，需 fsck）
    # TeslaCam (p2) 由 Tesla 车机写入，A7Z 运行 fsck 可能修改其文件系统元数据，
    # 导致车机 "其他" 容量显示异常。Tesla 车机会自行处理 fsck。
    echo "exFAT 检查（跳过 TeslaCam，仅检查 A7Z 管理的分区）..."
    for dev in /dev/nvme0n1p3 /dev/nvme0n1p4 /dev/nvme0n1p5; do
        if fsck.exfat -y "$dev" 2>/dev/null; then
            echo "  OK $dev"
        else
            echo "  SKIP $dev"
        fi
    done

    # 恢复挂载: TeslaCam 始终只读（A7Z 仅读取，不写入 TeslaCam 分区）
    # 先挂载 TeslaCam ro，再 mount -a 挂载其余分区（已挂载的 TeslaCam 会被跳过）
    mkdir -p /mnt/teslacam /mnt/music /mnt/lightshow /mnt/boombox
    mount -o ro,noatime /dev/nvme0n1p2 /mnt/teslacam 2>/dev/null || true
    mount -a 2>/dev/null || true
    swapon -a 2>/dev/null || true
    green "已停止，TeslaCam 已只读挂载，其他分区已恢复读写"
}

show_status() {
    echo "=== USB Gadget v8 ==="
    if [ -d "$GADGET_DIR" ]; then
        UDC=$(cat "$GADGET_DIR/UDC" 2>/dev/null || echo "未绑定")
        echo "  UDC: $UDC"
        for i in 0 1 2 3; do
            f="$GADGET_DIR/functions/mass_storage.usb0/lun.$i/file"
            if [ -f "$f" ]; then
                dev=$(cat "$f" 2>/dev/null || echo "?")
                nofua=$(cat "$GADGET_DIR/functions/mass_storage.usb0/lun.$i/nofua" 2>/dev/null || echo "?")
                echo "  LUN $i: $dev -> ${LUN_LABELS[$i]:-?} (nofua=$nofua)"
            fi
        done
        # 显示本地只读挂载状态
        echo "  本地挂载状态:"
        for i in "${!MOUNT_POINTS[@]}"; do
            mp="${MOUNT_POINTS[$i]}"
            if mountpoint -q "$mp" 2>/dev/null; then
                ro=$(findmnt -n -o OPTIONS "$mp" 2>/dev/null | grep -o 'ro' | head -1)
                echo "    $mp: 已挂载 ${ro:+($ro)}"
            else
                echo "    $mp: 未挂载"
            fi
        done
        green "  运行中"
    else
        yellow "  未运行"
        for dev in "${LUN_DEVICES[@]}"; do
            mp=$(findmnt -n -o TARGET "$dev" 2>/dev/null || echo "未挂载")
            echo "  $dev: $mp"
        done
    fi
}

case "${1:-start}" in
    start)   start_gadget ;;
    stop)    stop_gadget ;;
    status)  show_status ;;
    restart) stop_gadget; sleep 2; start_gadget ;;
    *)       echo "用法: $0 {start|stop|status|restart}" ;;
esac
