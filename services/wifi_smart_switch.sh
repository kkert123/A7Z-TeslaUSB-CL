#!/bin/bash

# WiFi智能切换脚本（v2.6 修复版）
# 用法：
#   wifi_smart_switch.sh --quick  # 快速检测
#   wifi_smart_switch.sh --full   # 完整检测

# WiFi优先级配置（不使用关联数组，使用普通数组）
WIFI_PRIORITY_LIST="CD:400 C12345:300 HP-00J6O:200 C123:100 189-AP:50"

LOG_FILE="/var/log/wifi-smart-switch.log"
LOCK_FILE="/var/run/wifi-smart-switch.lock"
STATE_FILE="/var/run/wifi-smart-switch.state"
FAILURE_COUNT_FILE="/var/run/wifi-failure-count"
SWITCH_COOLDOWN=300
SIGNAL_THRESHOLD=30

# 获取WiFi优先级
get_wifi_priority() {
    local ssid="$1"
    for item in $WIFI_PRIORITY_LIST; do
        local config_ssid="${item%:*}"
        local config_priority="${item#*:}"
        if [ "$config_ssid" = "$ssid" ]; then
            echo "$config_priority"
            return 0
        fi
    done
    echo "0"
}

log_message() { 
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"
}

get_last_switch_time() { 
    if [ -f "$STATE_FILE" ]; then 
        cat "$STATE_FILE"
    else
        echo "0"
    fi
}

set_last_switch_time() { 
    date +%s > "$STATE_FILE"
}

can_switch() {
    local last_switch=$(get_last_switch_time)
    local current_time=$(date +%s)
    local time_diff=$((current_time - last_switch))
    
    if [ $time_diff -lt $SWITCH_COOLDOWN ]; then
        return 1
    else
        return 0
    fi
}

get_current_wifi() {
    local wifi=$(nmcli -t -f DEVICE,STATE,CONNECTION device status 2>/dev/null | grep '^wlan0:connected:' | cut -d':' -f3)
    if [ -z "$wifi" ]; then
        wifi=$(nmcli -t -f SSID,ACTIVE device wifi 2>/dev/null | grep ':yes$' | cut -d':' -f1)
    fi
    if [ -z "$wifi" ]; then
        if command -v iwgetid &>/dev/null; then
            wifi=$(iwgetid -r 2>/dev/null)
        fi
    fi
    echo "$wifi"
}

check_wifi_connection() {
    local temp_file=$(mktemp)
    local pids=""
    for target in "12.127.12.8" "12.127.12.245" "baidu.com"; do
        (ping -c 2 -W 3 "$target" > /dev/null 2>&1 && echo "success" >> "$temp_file") &
        pids="$pids $!"
    done
    
    for i in {1..40}; do
        if [ -s "$temp_file" ]; then
            break
        fi
        sleep 0.1
    done
    
    for p in $pids; do
        kill -9 $p 2>/dev/null || true
    done
    
    local res="false"
    if [ -s "$temp_file" ]; then
        res="true"
    fi
    
    rm -f "$temp_file"
    echo "$res"
}

get_available_wifis() {
    local seen_list="|"
    
    while IFS= read -r line; do
        if [ -z "$line" ] || [ "$line" = "--" ]; then
            continue
        fi
        
        # 处理SSID（可能包含冒号）
        local ssid=$(echo "$line" | sed 's/\\:/===COLON===/g' | awk -F':' '{for(i=1; i<NF; i++) printf "%s%s", $i, (i<NF-1)?":":""}' | sed 's/===COLON===/:/g' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        local signal=$(echo "$line" | sed 's/\\:/===COLON===/g' | awk -F':' '{print $NF}' | tr -d '[:space:]')
        
        # 验证信号强度是否为数字
        if ! [[ "$signal" =~ ^[0-9]+$ ]]; then
            signal=0
        fi
        
        # 去重
        if [[ "$seen_list" == *"|$ssid|"* ]]; then
            continue
        fi
        
        seen_list="${seen_list}${ssid}|"
        
        # 获取优先级
        local priority=$(get_wifi_priority "$ssid")
        
        if [ "$priority" -gt 0 ]; then
            echo "${priority}:${signal}:${ssid}"
        fi
    done < <(nmcli -t -f SSID,SIGNAL device wifi list 2>/dev/null) | sort -t':' -k1,1rn -k2,2rn | cut -d':' -f3-
}

switch_to_wifi() {
    local target_wifi="$1"
    
    if ! can_switch; then
        log_message "切换冷却中，跳过切换到 $target_wifi"
        return 1
    fi
    
    log_message "正在切换到WiFi: $target_wifi"
    nmcli device disconnect wlan0 2>/dev/null
    sleep 2
    
    for i in {1..3}; do
        if nmcli device wifi connect "$target_wifi" 2>&1 >/dev/null; then
            log_message "切换成功"
            set_last_switch_time
            return 0
        fi
        sleep 2
    done
    
    log_message "切换失败: $target_wifi"
    return 1
}

quick_check() {
    log_message "开始快速检测..."
    
    if [ "$(check_wifi_connection)" = "true" ]; then
        rm -f "$FAILURE_COUNT_FILE"
        log_message "网络正常"
    else
        local count=0
        if [ -f "$FAILURE_COUNT_FILE" ]; then
            count=$(cat "$FAILURE_COUNT_FILE")
        fi
        
        count=$((count + 1))
        echo "$count" > "$FAILURE_COUNT_FILE"
        log_message "网络不通 (失败 $count 次)"
        
        if [ $count -ge 2 ]; then
            full_check
        fi
    fi
}

full_check() {
    log_message "开始完整检测..."
    local cur_wifi=$(get_current_wifi)
    local is_conn=$(check_wifi_connection)
    local cur_priority=$(get_wifi_priority "$cur_wifi")
    
    if [ "$is_conn" = "true" ]; then
        log_message "当前连接: $cur_wifi (优先级: $cur_priority)"
        
        # 获取可用WiFi列表
        local avail_wifis=$(get_available_wifis)
        local found_better=false
        
        while IFS= read -r item; do
            if [ -z "$item" ]; then
                continue
            fi
            
            local item_priority=$(get_wifi_priority "$item")
            
            if [ "$item_priority" -gt "$cur_priority" ]; then
                # 获取信号强度
                local signal_line=$(echo "$avail_wifis" | grep ":$item$" | head -1)
                local signal_strength=0
                if [ -n "$signal_line" ]; then
                    signal_strength=$(echo "$signal_line" | cut -d':' -f2)
                fi
                
                if [ "$signal_strength" -ge "$SIGNAL_THRESHOLD" ]; then
                    log_message "发现更优网络: $item (优先级: $item_priority, 信号: $signal_strength)"
                    if switch_to_wifi "$item"; then
                        found_better=true
                        break
                    fi
                fi
            fi
        done <<< "$(echo "$avail_wifis" | cut -d':' -f3-)"
        
        if [ "$found_better" = false ]; then
            log_message "未找到更优网络"
        fi
    else
        log_message "网络连接异常，尝试重连..."
        
        # 按优先级尝试连接可用WiFi
        local avail_wifis=$(get_available_wifis)
        
        while IFS= read -r line; do
            if [ -z "$line" ]; then
                continue
            fi
            
            local signal_strength=$(echo "$line" | cut -d':' -f2)
            local ssid=$(echo "$line" | cut -d':' -f3)
            
            if [ "$signal_strength" -ge "$SIGNAL_THRESHOLD" ]; then
                log_message "尝试连接: $ssid (信号: $signal_strength)"
                if nmcli device wifi connect "$ssid" 2>&1 >/dev/null; then
                    log_message "重连 $ssid 成功"
                    break
                fi
            fi
        done <<< "$avail_wifis"
    fi
    
    log_message "完整检测完成"
}

main() {
    # 检查锁文件
    if [ -f "$LOCK_FILE" ]; then
        local pid=$(cat "$LOCK_FILE" 2>/dev/null)
        if ps -p "$pid" >/dev/null 2>&1; then
            log_message "脚本已在运行 (PID: $pid)，退出"
            exit 1
        else
            # 清理旧的锁文件
            rm -f "$LOCK_FILE"
        fi
    fi
    
    # 创建锁文件
    echo $$ > "$LOCK_FILE"
    trap "rm -f $LOCK_FILE" EXIT
    
    # 创建日志目录
    mkdir -p "$(dirname "$LOG_FILE")"
    
    if [ "$1" = "--quick" ]; then
        quick_check
    else
        full_check
    fi
}

# 脚本入口
if [ $# -eq 0 ]; then
    echo "用法: $0 [--quick|--full]"
    echo "  --quick   快速检测网络连接"
    echo "  --full    完整检测并优化WiFi连接"
    exit 1
fi

main "$@"
