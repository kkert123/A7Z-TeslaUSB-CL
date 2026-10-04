#!/bin/bash
# A7Z AP+STA 并发验证 —— 回滚脚本（幂等）。由 systemd-run 定时器兜底触发。
# 用法: systemd-run --on-active=<N> --unit=a7z-apitest-guard /bin/bash /this.sh
LOG=/tmp/a7z_apitest.log
{
echo "=== ROLLBACK start $(date '+%F %T') ==="
IFNAME="$(cat /tmp/a7z_apitest_ifname 2>/dev/null)"

# 1) 杀测试 hostapd / dnsmasq
[ -f /tmp/a7z_apitest_hostapd.pid ] && kill "$(cat /tmp/a7z_apitest_hostapd.pid)" 2>/dev/null && echo "killed hostapd(pid)"
pkill -f 'a7z_apitest_hostapd.conf' 2>/dev/null && echo "pkill hostapd"
[ -f /tmp/a7z_apitest_dnsmasq.pid ] && kill "$(cat /tmp/a7z_apitest_dnsmasq.pid)" 2>/dev/null && echo "killed dnsmasq(pid)"
pkill -f 'a7z_apitest_dnsmasq' 2>/dev/null
sleep 1

# 2) 移除测试加的 iptables 规则
if [ -n "$IFNAME" ]; then
  iptables -t nat -D POSTROUTING -s 192.168.42.0/24 -o wlan0 -j MASQUERADE 2>/dev/null && echo "del MASQUERADE"
  iptables -D FORWARD -i "$IFNAME" -o wlan0 -j ACCEPT 2>/dev/null && echo "del FORWARD in"
  iptables -D FORWARD -i wlan0 -o "$IFNAME" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null && echo "del FORWARD out"
fi

# 3) 删除除 wlan0 外的一切命名 vif
for IF in $(/sbin/iw dev | awk '/Interface /{print $2}'); do
  case "$IF" in
    wlan0) ;;
    *) echo "delete vif $IF"; ip link set "$IF" down 2>/dev/null; /sbin/iw dev "$IF" del 2>/dev/null;;
  esac
done

# 4) 恢复 wlan0（禁止 restart NetworkManager —— M53）
nmcli device set wlan0 managed yes 2>/dev/null
ip link set wlan0 up 2>/dev/null
/sbin/iw dev wlan0 set type managed 2>/dev/null
sleep 2
nmcli device connect wlan0 2>/dev/null
nmcli connection up C12345 2>/dev/null      # ← 改成你的 STA SSID
sleep 2
echo "--- after ---"
/sbin/iw dev
ip -4 addr show wlan0 2>/dev/null | grep -E 'inet |state'
/sbin/iw dev wlan0 link 2>/dev/null | head -3
iptables -t nat -S 2>/dev/null | grep 192.168.42 || echo "(no leftover NAT rule)"
echo "=== ROLLBACK done $(date '+%F %T') ==="
} >> "$LOG" 2>&1
