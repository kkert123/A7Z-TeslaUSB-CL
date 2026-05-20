#!/bin/bash
# usb_gadget_init.sh - A7Z USB Gadget v7
GADGET_NAME="tesla_usb"
GADGET_DIR="/sys/kernel/config/usb_gadget/$GADGET_NAME"
CONFIG_NAME="c.1"

LUN_DEVICES=("/dev/nvme0n1p2" "/dev/nvme0n1p3" "/dev/nvme0n1p4" "/dev/nvme0n1p5")
LUN_LABELS=("TESLACAM" "MUSIC" "LIGHTSHOW" "BOOMBOX")

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

start_gadget() {
    green "=== USB Gadget v7 (nofua + fsck) ==="

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

    green ""
    green "  USB Gadget 已激活! UDC=$UDC (nofua)"
    green "  连接特斯拉车机或 PC 即可识别"
    echo ""
    for i in "${!LUN_DEVICES[@]}"; do
        echo "    LUN $i: ${LUN_DEVICES[$i]} -> ${LUN_LABELS[$i]}"
    done
    yellow ""
    yellow "  提示: 分区已被 USB 独占，只读挂载可恢复: mount -o ro /dev/nvme0n1p2 /mnt/teslacam"
    yellow "  恢复: sudo bash $0 stop"
}

stop_gadget() {
    echo "停止 USB Gadget..."
    cleanup
    sync; sleep 2

    # ⚡ 关键：先 fsck（分区此时未挂载），再 mount
    echo "exFAT 检查（自动修复 nofua 残留）..."
    for dev in /dev/nvme0n1p2 /dev/nvme0n1p3 /dev/nvme0n1p4 /dev/nvme0n1p5; do
        if fsck.exfat -y "$dev" 2>/dev/null; then
            echo "  OK $dev"
        else
            # 未格式化或其他错误
            echo "  SKIP $dev (will format on mount)"
        fi
    done

    mount -a 2>/dev/null || true
    swapon -a 2>/dev/null || true
    green "已停止，分区已恢复本地挂载"
}

show_status() {
    echo "=== USB Gadget v7 ==="
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
