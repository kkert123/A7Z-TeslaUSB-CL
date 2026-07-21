#!/bin/bash
# AP Control Script — TeslaUSB 无线热点管理
# 用法:
#   ap_control.sh start   启动 AP 热点
#   ap_control.sh stop    停止 AP 热点
#   ap_control.sh status  查看 AP 状态

set -e

AP_CONF="/etc/hostapd/hostapd.conf"
DNSMASQ_CONF="/etc/dnsmasq.d/ap.conf"

start_ap() {
    echo "Starting AP hotspot..."

    # 确保 hostapd 配置存在
    if [ ! -f "$AP_CONF" ]; then
        # 使用默认配置
        cat > "$AP_CONF" << 'EOF'
interface=wlan0
driver=nl80211
ssid=TeslaUSB-Setup
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=CHANGE_ME_AP_PASSWORD
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF
    fi

    # 停止 wpa_supplicant 避免冲突
    systemctl stop wpa_supplicant 2>/dev/null || true

    # 启动 hostapd
    systemctl start hostapd

    # 配置 DHCP (dnsmasq)
    cat > "$DNSMASQ_CONF" << 'EOF'
interface=wlan0
dhcp-range=192.168.42.10,192.168.42.100,12h
dhcp-option=3,192.168.42.1
dhcp-option=6,192.168.42.1
EOF
    systemctl restart dnsmasq

    # 设置静态 IP
    ip addr add 192.168.42.1/24 dev wlan0 2>/dev/null || true

    echo "AP started on 192.168.42.1"
}

stop_ap() {
    echo "Stopping AP hotspot..."

    systemctl stop hostapd 2>/dev/null || true
    systemctl stop dnsmasq 2>/dev/null || true

    # 清理 IP
    ip addr del 192.168.42.1/24 dev wlan0 2>/dev/null || true

    # 恢复 NetworkManager 管理
    systemctl restart NetworkManager 2>/dev/null || true

    echo "AP stopped"
}

status_ap() {
    if systemctl is-active --quiet hostapd; then
        echo "AP is running"
        echo "SSID: TeslaUSB-Setup"
        echo "IP:   192.168.42.1"
    else
        echo "AP is not running"
    fi
}

case "${1:-}" in
    start)
        start_ap
        ;;
    stop)
        stop_ap
        ;;
    status)
        status_ap
        ;;
    restart)
        stop_ap
        sleep 2
        start_ap
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
