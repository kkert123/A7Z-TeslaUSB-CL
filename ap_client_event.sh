#!/bin/bash
# v0.3.1.54: hostapd_cli -a 事件回调——客户端断开 15s 宽限后让出回连 WiFi。
#
# 背景（9-8 事故）：AP 客户端断开后设备不切回 WiFi，只能断电重启。
# 根因是断开检测跑在 wifi timer（quick=2min）轮询里，timer 静默死亡后
# 永不执行。本回调由 hostapd_cli -a 事件驱动（_ap_start_client_monitor
# 以 sudo 拉起），hostapd 停止时监听自动退出，无需显式管理生命周期。
#
# hostapd_cli -a 调用约定（hostapd_cli action_cmd）：事件行按空白拆词后
# exec 本脚本 → $1=事件名，$2..=附加参数（如客户端 MAC）。注意 $1 才是
# 事件名（交叉审查 P0：曾误取 $2=MAC 导致 case 永不命中）。
#
# 防误杀设计（对应 v37「配置中的用户不被打断」语义）：
#   - 客户端(重新)连上 → 取消让出
#   - 断开后 15s 宽限（防手机漫游抖动）；期间重连则取消
#   - yield 前置检查：AP 上仍有其他关联客户端 → 取消让出（多客户端场景
#     不能因一个断开就踢掉所有人）
#   - 让出调用 wifi_service.py --yield-ap，与 timer 自愈共用 _ap_bring_down
#     单点逻辑（内部 flock + transition 标记防并发，重复触发无实害）
WIFI_SERVICE="/opt/radxa_data/teslausb/wifi_service.py"
PENDING="/var/run/ap-yield-pending"
DELAY=15
LOG="/tmp/ap-client-event.log"

EVENT="$1"
case "$EVENT" in
  AP-STA-CONNECTED*)
    # 客户端连上/重连 → 取消挂起的让出（配置中不打断）
    rm -f "$PENDING"
    ;;
  AP-STA-DISCONNECTED*)
    # 客户端断开 → 置让出标记 + 15s 宽限倒计时；期间若重连则由
    # AP-STA-CONNECTED 清标记取消。倒计时到且标记仍在 → 让出回连。
    echo "1" > "$PENDING"
    (
      sleep $DELAY
      # 双保险：宽限内重连（标记被清）或有其他客户端仍在 AP 上 → 取消
      if [ -f "$PENDING" ] && ! iw dev wlan0 station dump 2>/dev/null | grep -q "Station "; then
        rm -f "$PENDING"
        echo "$(date '+%F %T') yield-ap: 无客户端且断开超 ${DELAY}s 宽限，让出回连 WiFi" >> "$LOG"
        /usr/bin/python3 "$WIFI_SERVICE" --yield-ap >> "$LOG" 2>&1
      fi
    ) &
    ;;
esac
exit 0
