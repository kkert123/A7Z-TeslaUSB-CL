"""系统信息查询模块 — WiFi、运行时间、服务状态、IP 地址"""
import os
import time
import subprocess
import socket


def get_wifi_info():
    """获取WiFi连接信息（修复：正确读取已连接的WiFi名称）"""
    wifi = {'connected': False, 'ssid': None, 'signal': None, 'frequency': None}
    try:
        # 方法1: 使用 iwconfig（适用于旧版wifi工具）
        try:
            result = subprocess.run(['iwconfig', 'wlan0'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and 'ESSID' in result.stdout:
                for line in result.stdout.split('\n'):
                    if 'ESSID' in line:
                        # 格式: ESSID:"WiFi名称"
                        ssid = line.split('ESSID:')[1].strip().strip('"')
                        if ssid and ssid != 'off/any':
                            wifi['connected'] = True
                            wifi['ssid'] = ssid
                    if 'Signal level' in line:
                        # 格式: Signal level=-50 dBm
                        parts = line.split('Signal level=')
                        if len(parts) > 1:
                            wifi['signal'] = parts[1].split(' ')[0].strip()
                    if 'Frequency' in line:
                        # 格式: Frequency:2.437 GHz
                        parts = line.split('Frequency:')
                        if len(parts) > 1:
                            wifi['frequency'] = parts[1].split(' ')[0].strip()
        except:
            pass
        
        # 方法2: 如果 iwconfig 失败，尝试 iw dev wlan0 link
        if not wifi['connected']:
            try:
                result = subprocess.run(['iw', 'dev', 'wlan0', 'link'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0 and 'Connected' in result.stdout:
                    wifi['connected'] = True
                    for line in result.stdout.split('\n'):
                        if line.strip().startswith('SSID:'):
                            wifi['ssid'] = line.strip().split('SSID:')[1].strip()
                        if 'signal' in line.lower():
                            wifi['signal'] = line.strip()
            except:
                pass
        
        # 方法3: 使用 nmcli（如果系统安装了NetworkManager）
        if not wifi['connected']:
            try:
                result = subprocess.run(['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION', 'device', 'status'],
                                      capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        parts = line.split(':')
                        if len(parts) >= 3 and parts[0] == 'wlan0' and parts[1] == 'connected':
                            wifi['connected'] = True
                            wifi['ssid'] = parts[2]
                            break
            except:
                pass
    except Exception as e:
        print(f"Error getting WiFi info: {e}")
        pass
    
    return wifi


def get_system_uptime():
    """获取系统运行时间"""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
        
        # 转换为天、小时、分钟
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        if days > 0:
            return f"{days}天{hours}小时{minutes}分钟"
        elif hours > 0:
            return f"{hours}小时{minutes}分钟"
        else:
            return f"{minutes}分钟"
    except:
        return "N/A"

def get_service_status():
    """获取服务状态（使用单调时钟，不受 NTP/时间跳变影响）"""
    service = {'active': False, 'uptime': 'N/A'}
    try:
        result = subprocess.run(['systemctl', 'is-active', 'teslausb-web.service'],
                              capture_output=True, text=True, timeout=2)
        service['active'] = result.returncode == 0 and 'active' in result.stdout
        
        if service['active']:
            # 用 ActiveEnterTimestampMonotonic（微秒级单调时钟）
            # 不受墙上时钟跳变（NTP/时区/手动）影响，与 /proc/uptime 同一时钟源
            result = subprocess.run(
                ['systemctl', 'show', 'teslausb-web.service',
                 '--property=ActiveEnterTimestampMonotonic'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                line = result.stdout.strip()
                if '=' in line:
                    try:
                        monotonic_us = int(line.split('=', 1)[1])
                        # 读取当前系统运行时间（同源单调时钟）
                        with open('/proc/uptime', 'r') as f:
                            current_uptime_s = float(f.readline().split()[0])
                        service_uptime_s = current_uptime_s - monotonic_us / 1_000_000
                        if service_uptime_s < 0:
                            service_uptime_s = 0  # 容错：刚重启时可能的精度偏差
                        
                        days = int(service_uptime_s // 86400)
                        hours = int((service_uptime_s % 86400) // 3600)
                        minutes = int((service_uptime_s % 3600) // 60)
                        
                        if days > 0:
                            service['uptime'] = f"{days}天 {hours}小时 {minutes}分钟"
                        elif hours > 0:
                            service['uptime'] = f"{hours}小时 {minutes}分钟"
                        else:
                            service['uptime'] = f"{minutes}分钟"
                    except (ValueError, OSError) as e:
                        print(f"Error computing service uptime: {e}")
    except Exception as e:
        print(f"Error getting service status: {e}")
        pass
    return service


def get_ip_info():
    """获取 IP 地址（修复：正确读取 wlan0 和 tailscale0）"""
    ip_info = {'local': 'N/A', 'tailscale': 'N/A'}
    try:
        # 方法1: 使用 ip addr show 获取指定接口IP
        # 本地IP - 优先读取 wlan0，其次 eth0
        for iface in ['wlan0', 'eth0', 'enp0s3', 'ens3']:
            try:
                result = subprocess.run(['ip', '-4', 'addr', 'show', iface],
                                      capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    # 解析 "inet 192.168.0.101/24"
                    for line in result.stdout.split('\n'):
                        if 'inet ' in line:
                            ip_addr = line.split('inet ')[1].split('/')[0].strip()
                            ip_info['local'] = ip_addr
                            break
                    if ip_info['local'] != 'N/A':
                        break
            except:
                continue
        
        # 如果上面没找到，再用 hostname -I（排除loopback和tailscale）
        if ip_info['local'] == 'N/A':
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                ips = result.stdout.strip().split()
                for ip in ips:
                    if not ip.startswith('127.') and not ip.startswith('100.'):
                        ip_info['local'] = ip
                        break
    except:
        pass
    
    # Tailscale IP - 读取 tailscale0 接口
    try:
        result = subprocess.run(['ip', '-4', 'addr', 'show', 'tailscale0'],
                              capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'inet ' in line:
                    ip_addr = line.split('inet ')[1].split('/')[0].strip()
                    ip_info['tailscale'] = ip_addr
                    break
    except:
        pass
    
    # 如果上面失败，尝试 tailscale 命令
    if ip_info['tailscale'] == 'N/A':
        try:
            result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip():
                ip_info['tailscale'] = result.stdout.strip()
        except:
            pass
    
    return ip_info
