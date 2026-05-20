#!/bin/bash

# WiFi调试脚本
LOG_FILE="/var/log/wifi_debug.log"

# 记录日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    echo "$1"
}

# 检查WiFi设备状态
check_wifi_device() {
    log "=== 检查WiFi设备状态 ==="
    log "1. WiFi无线电状态:"
    nmcli radio wifi
    
    log "2. 网络设备状态:"
    nmcli device status
    
    log "3. 详细设备信息:"
    nmcli device show wlan0
}

# 检查连接配置
check_connection_config() {
    log "=== 检查连接配置 ==="
    local wifi_name="$1"
    
    log "检查连接 '$wifi_name' 的配置:"
    nmcli connection show "$wifi_name" | head -30
    
    # 检查是否有保存的密码
    log "检查WiFi安全设置:"
    nmcli -s connection show "$wifi_name" | grep -E "(802-11-wireless-security|wifi-sec)" || log "没有安全设置"
}

# 尝试连接并记录详细输出
debug_connect_wifi() {
    local wifi_name="$1"
    
    log "=== 尝试连接 '$wifi_name' ==="
    
    # 1. 先检查设备状态
    log "连接前设备状态:"
    nmcli -t -f GENERAL.STATE device show wlan0 2>/dev/null
    
    # 2. 检查连接是否已激活
    log "检查连接是否已激活:"
    nmcli -t connection show --active | grep "$wifi_name" && log "连接已激活" || log "连接未激活"
    
    # 3. 尝试连接并捕获所有输出
    log "执行连接命令: nmcli connection up \"$wifi_name\""
    CONNECT_OUTPUT=$(timeout 30 nmcli connection up "$wifi_name" 2>&1)
    CONNECT_RESULT=$?
    
    log "连接命令退出码: $CONNECT_RESULT"
    log "连接命令输出: $CONNECT_OUTPUT"
    
    # 4. 检查连接后状态
    sleep 3
    log "连接后设备状态:"
    nmcli -t -f GENERAL.STATE device show wlan0 2>/dev/null
    
    log "连接后活跃连接:"
    nmcli -t connection show --active | grep wlan0
    
    if [ $CONNECT_RESULT -eq 0 ]; then
        log "连接命令执行成功"
        
        # 检查是否真的连接上了
        sleep 2
        local connected=$(nmcli -t -f GENERAL.STATE device show wlan0 2>/dev/null | grep "connected")
        if [ -n "$connected" ]; then
            log "WiFi设备已连接"
            
            # 获取IP地址
            log "IP地址信息:"
            ip addr show wlan0 | grep "inet " || log "没有获取到IP地址"
            
            return 0
        else
            log "警告: 连接命令成功但设备未连接"
            return 1
        fi
    else
        log "连接命令失败"
        return 1
    fi
}

# 扫描WiFi网络
debug_scan_wifi() {
    log "=== 扫描WiFi网络 ==="
    
    log "执行扫描命令:"
    SCAN_OUTPUT=$(nmcli device wifi rescan 2>&1)
    log "扫描输出: $SCAN_OUTPUT"
    
    sleep 3
    
    log "可用WiFi网络列表:"
    nmcli -t -f SSID,SIGNAL,SECURITY device wifi list
    
    log "我们关注的WiFi:"
    nmcli -t -f SSID,SIGNAL,SECURITY device wifi list | grep -E "(C12345|C123|189-AP|CD)"
}

# 主函数
main() {
    log "======= WiFi连接调试开始 ======="
    
    # 确保WiFi开启
    log "开启WiFi:"
    nmcli radio wifi on
    
    # 检查设备状态
    check_wifi_device
    
    # 扫描网络
    debug_scan_wifi
    
    # 检查目标WiFi的配置
    for wifi in "C12345" "189-AP" "C123" "CD"; do
        if nmcli connection show "$wifi" >/dev/null 2>&1; then
            check_connection_config "$wifi"
        else
            log "连接配置 '$wifi' 不存在"
        fi
    done
    
    # 尝试连接C12345
    debug_connect_wifi "C12345"
    
    # 如果失败，尝试连接189-AP
    if [ $? -ne 0 ]; then
        log "C12345连接失败，尝试189-AP..."
        debug_connect_wifi "189-AP"
    fi
    
    # 最终状态
    log "=== 最终状态 ==="
    nmcli device status
    nmcli connection show --active
    
    log "======= WiFi连接调试结束 ======="
}

# 运行主函数
main