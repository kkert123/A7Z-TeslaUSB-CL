"""系统信息查询模块 — WiFi、运行时间、服务状态、IP 地址"""
import os
import time
import subprocess
import socket



def _dbm_to_percent(dbm):
    """WiFi 信号 dBm → 百分比（-100→1%，-50 及以上→100%，线性插值）"""
    try:
        dbm = float(dbm)
    except (TypeError, ValueError):
        return None
    if dbm >= -50:
        return 100
    if dbm <= -100:
        return 1
    return max(1, min(100, int(round(2 * (dbm + 100)))))



def _finalize(w):
    """统一出口：dBm → 百分比（容忍 "signal: -47 dBm" 整行文本）"""
    sig = w.get("signal")
    if sig is not None:
        pct = _dbm_to_percent(sig)
        if pct is None and isinstance(sig, str):
            import re as _re2
            m = _re2.search(r'-\d+(?:\.\d+)?', sig)
            if m:
                pct = _dbm_to_percent(m.group(0))
        if pct is not None:
            w["signal_dbm"] = sig
            w["signal"] = pct
    return w


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
                            import re as _re
                            m = _re.search(r'-?\d+(?:\.\d+)?', line)
                            if m:
                                wifi['signal'] = m.group(0)
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
    
    return _finalize(wifi)


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


# ═══════════════════════════════════════════════════════════════
# TeslaCam 分区模式探测 —— 唯一权威实现（v0.3.1.63）
#
# 背景（教训 M100）：以前这套判据在多个模块里各写一套，且都用「cam 分区是否
# 只读挂载」来判 Present —— 但 Edit 模式下 `usb_gadget_init.sh stop_gadget()`
# 也是把 TeslaCam 按 **ro** 挂载（其注释明写「TeslaCam 始终只读」），于是编辑
# 模式会被误判成 Present。判据必须落在「块设备是否被独占 / 能不能放开写」上。
#
# Present 判据：
#   ① USB Gadget 已绑定 UDC（块设备被 gadget 独占，remount rw 必然失败）
#   ② cam 以 ro 挂在只读回环 /dev/loop* 上（present_usb.sh 直挂失败时的兜底
#      路径用 `losetup -r`，只读回环永远无法 remount 成可写）
# 其余 ro 直挂 = edit（此时 gadget 已解绑、块设备空闲，remount rw 可成功）。
# ═══════════════════════════════════════════════════════════════

CAM_PATH = '/mnt/teslacam'
GADGET_UDC = '/sys/kernel/config/usb_gadget/tesla_usb/UDC'


def read_cam_mount():
    """读取 cam 分区挂载状态。

    Returns:
        (mounted, is_ro, source)
    """
    mounted = False
    is_ro = False
    source = ''
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == CAM_PATH:
                    mounted = True
                    source = parts[0]
                    is_ro = 'ro' in parts[3].split(',')
                    break
    except Exception:
        pass
    return mounted, is_ro, source


def gadget_bound():
    """USB Gadget 是否已绑定 UDC。

    ⚠ 必须判「内容非空」而不是「文件存在」：configfs 里 UDC 文件在解绑
    （`echo "" > UDC`）后依然存在，只判存在会把 Edit 模式误判成 Present。
    """
    try:
        with open(GADGET_UDC) as f:
            return bool(f.read().strip())
    except Exception:
        return False


def is_loop_source(source):
    """挂载源是否为 loop 设备（只读回环无法 remount 成可写）。"""
    return str(source).startswith('/dev/loop')


def detect_cam_mode():
    """探测 cam 分区当前模式。

    Returns:
        (mode, detail)  mode ∈ {'present', 'edit', 'unmounted'}，
        detail = {'mounted', 'is_ro', 'source', 'gadget_bound'}
    """
    mounted, is_ro, source = read_cam_mount()
    bound = gadget_bound()
    if bound:
        mode = 'present'                       # 块设备被 gadget 独占
    elif mounted and is_ro and is_loop_source(source):
        mode = 'present'                       # 只读回环，无法放开写
    elif mounted:
        mode = 'edit'
    else:
        mode = 'unmounted'
    return mode, {'mounted': mounted, 'is_ro': is_ro,
                  'source': source, 'gadget_bound': bound}
