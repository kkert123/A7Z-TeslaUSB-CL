#!/usr/bin/env python3

"""
TeslaUSB Neo - 开机通知服务
============================

系统启动时收集系统信息并通过企业微信发送开机通知。

功能：
1. 收集启动时间、CPU/内存/磁盘/WiFi/Tailscale 信息
2. 等待 Tailscale 连接就绪（最多 180 秒）
3. 通过状态通知机器人推送开机通知

设计为 systemd oneshot 服务，在启动后运行一次

基于旧版 sentry_status.py 重构

作者: TeslaUSB-Neo 项目
版本: 1.0.0
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger('boot_notify')

# 路径
CONFIG_PATH = "/opt/radxa_data/teslausb/config/sentry.json"
LOG_FILE = "/var/log/teslausb-boot-notify.log"

# Tailscale 等待超时
TAILSCALE_WAIT_TIMEOUT = 180

# 分区信息 (A7Z mount points)
PARTITIONS = {
    "系统盘": "/",
    "TeslaCam": "/mnt/teslacam",
    "LightShow": "/mnt/lightshow",
    "Music": "/mnt/music",
    "Boombox": "/mnt/boombox",
}


def get_boot_time_str() -> str:
    """获取启动耗时（从开机到当前时刻）"""
    try:
        # 系统开机时间戳（/proc/stat btime）
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("btime "):
                    boot_ts = int(line.split()[1])
                    break
        
        # 直接用 /proc/uptime（始终准确，不依赖其他服务）
        with open("/proc/uptime", "r") as f:
            ready_s = float(f.read().split()[0])
        
        # 格式化
        m = int(ready_s // 60)
        s = int(ready_s % 60)
        if m > 0:
            duration = f"{m}min {s}s"
        else:
            duration = f"{s}s"
        
        # 启动时刻（btime 转可读时间）
        boot_dt = datetime.fromtimestamp(boot_ts)
        boot_str = boot_dt.strftime('%m-%d %H:%M')
        
        return f"{boot_str}（启动耗时 {duration}）"
    except Exception:
        return datetime.now().strftime('%m-%d %H:%M')


def get_cpu_info() -> Dict:
    """获取 CPU 信息（兼容 ARM/Allwinner）"""
    # Friendly name mapping for ARM SoC internal codes
    SOC_NAMES = {
        "sun60iw2": "Allwinner A733",
        "sun50iw9": "Allwinner H616",
        "sun50iw6": "Allwinner H6",
        "bcm2711": "Broadcom BCM2711 (Pi 4)",
        "bcm2837": "Broadcom BCM2837 (Pi 3)",
    }
    
    model = ""
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                # ARM boards use "model name" (Pi) or "Processor" or "Hardware"
                if line.startswith("model name") or line.startswith("Processor"):
                    model = line.split(":")[1].strip()
                    break
        # Fallback: read Hardware or SoC
        if not model:
            for soc_path in ["/proc/device-tree/model", "/sys/class/soc/machine"]:
                try:
                    with open(soc_path, "r") as f:
                        model = f.read().strip().rstrip('\x00')
                        break
                except Exception:
                    pass
        # Map internal codes to friendly names
        if model in SOC_NAMES:
            model = SOC_NAMES[model]
    except Exception:
        pass

    freq_mhz = 0
    for freq_path in [
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
        "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq",
    ]:
        try:
            with open(freq_path, "r") as f:
                freq_mhz = int(f.read().strip()) // 1000
                break
        except Exception:
            pass

    temp_c = 0.0
    for path in [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/thermal/thermal_zone1/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    temp_c = round(int(f.read().strip()) / 1000.0, 1)
                break
            except Exception:
                pass

    return {
        "model": model,
        "freq_mhz": freq_mhz,
        "temp_c": temp_c,
    }


def get_nvme_temp() -> float:
    """获取 NVMe SSD 温度"""
    # 方法1: nvme smart-log
    try:
        result = subprocess.run(
            ["sudo", "-n", "nvme", "smart-log", "/dev/nvme0"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import re
            # Match "temperature : 68 C" pattern
            m = re.search(r'temperature\s*:\s*(\d+)\s*C', result.stdout)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    
    # 方法2: hwmon sysfs
    for path in [
        "/sys/class/nvme/nvme0/device/hwmon/hwmon0/temp1_input",
        "/sys/class/nvme/nvme0/device/hwmon/hwmon1/temp1_input",
        "/sys/class/hwmon/hwmon0/temp2_input",
    ]:
        try:
            with open(path, "r") as f:
                return round(int(f.read().strip()) / 1000.0, 1)
        except Exception:
            continue
    return 0.0


def get_memory_info() -> Dict:
    """获取内存信息（含 SWAP）"""
    try:
        with open("/proc/meminfo", "r") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        total_kb = info.get("MemTotal", 0)
        available_kb = info.get("MemAvailable", 0)
        used_kb = total_kb - available_kb
        pct = (used_kb / total_kb * 100) if total_kb > 0 else 0
        
        swap_total = info.get("SwapTotal", 0)
        swap_free = info.get("SwapFree", 0)
        swap_used = swap_total - swap_free
        swap_pct = round(swap_used / swap_total * 100, 1) if swap_total > 0 else 0
        
        return {
            "total_mb": round(total_kb / 1024),
            "used_mb": round(used_kb / 1024),
            "pct": round(pct, 1),
            "swap_total_mb": round(swap_total / 1024),
            "swap_used_mb": round(swap_used / 1024),
            "swap_pct": swap_pct,
        }
    except Exception:
        return {"total_mb": 0, "used_mb": 0, "pct": 0, "swap_total_mb": 0, "swap_used_mb": 0, "swap_pct": 0}


def get_disk_info() -> list:
    """获取磁盘信息（全部显示，未挂载标注状态）"""
    import shutil
    disks = []
    for name, path in PARTITIONS.items():
        try:
            mounted = os.path.ismount(path)
            if mounted:
                usage = shutil.disk_usage(path)
                pct = round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0
                disks.append({
                    "name": name,
                    "total_gb": round(usage.total / (1024 ** 3), 1),
                    "used_gb": round(usage.used / (1024 ** 3), 1),
                    "free_gb": round(usage.free / (1024 ** 3), 1),
                    "percent": pct,
                    "mounted": True,
                })
            else:
                disks.append({"name": name, "mounted": False})
        except Exception:
            disks.append({"name": name, "mounted": False, "error": str(e)})
    return disks


def get_wifi_info() -> Dict:
    """获取 WiFi 信息（优先 nmcli，回退 /proc/net/wireless）"""
    import time
    info = {"connected": False, "ssid": "", "signal_pct": 0}
    
    # 方法1: nmcli (NetworkManager) — 加重试，启动时可能未就绪
    for attempt in range(5):
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show", "wlan0"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if line.startswith('GENERAL.CONNECTION:'):
                        ssid = line.split(':', 1)[1].strip()
                        if ssid:
                            info["connected"] = True
                            info["ssid"] = ssid
                            break
            
            # 获取信号强度
            if info["connected"]:
                result2 = subprocess.run(
                    ["nmcli", "-t", "-f", "active,signal", "dev", "wifi"],
                    capture_output=True, text=True, timeout=10
                )
                for line in result2.stdout.split('\n'):
                    if line.startswith('yes:'):
                        sig = line.split(':')[-1].strip()
                        if sig.isdigit():
                            info["signal_pct"] = int(sig)
                            break
            
            if info["connected"] and info["signal_pct"] > 0:
                break  # 成功获取，退出重试
        except Exception:
            pass
        
        if not info["connected"] or info["signal_pct"] == 0:
            time.sleep(3)  # 等 3 秒后重试
    
    # 方法2: /proc/net/wireless 回退（iwconfig 在 A7Z 上不可用）
    if info["signal_pct"] == 0 and info["connected"]:
        try:
            with open("/proc/net/wireless", "r") as f:
                lines = f.readlines()
            if len(lines) >= 3:
                # 第三行是 wlan0 数据: "wlan0: 0000   69.  -41.  -256"
                parts = lines[2].split()
                if len(parts) >= 4:
                    # link quality 是百分比形式如 "69." 
                    link_qual = float(parts[2].replace('.', ''))
                    info["signal_pct"] = int(min(100, max(0, link_qual)))
        except Exception:
            pass
    
    return info


def get_tailscale_ip() -> str:
    """获取 Tailscale IP"""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            addrs = data.get("TailscaleIPs", [])
            if addrs:
                return addrs[0]
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", "tailscale0"],
            capture_output=True, text=True, timeout=5
        )
        import re
        match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass

    return "N/A"


def get_local_ip() -> str:
    """获取本地网络 IP"""
    try:
        # 尝试各接口
        for iface in ["wlan0", "eth0", "enp0s3", "ens3"]:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", iface],
                capture_output=True, text=True, timeout=3
            )
            import re
            match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
            if match:
                ip = match.group(1)
                if not ip.startswith("127."):
                    return f"{iface} {ip}"
    except Exception:
        pass
    
    # 兜底: hostname -I
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        ips = result.stdout.strip().split()
        for ip in ips:
            if not ip.startswith("127.") and not ip.startswith("100."):
                return ip
    except Exception:
        pass
    
    return "N/A"


def wait_for_tailscale(timeout: int = TAILSCALE_WAIT_TIMEOUT) -> str:
    """等待 Tailscale 连接"""
    start = time.time()
    while (time.time() - start) < timeout:
        ip = get_tailscale_ip()
        if ip != "N/A":
            logger.info(f"Tailscale 已连接: {ip}")
            return ip
        logger.debug("等待 Tailscale 连接...")
        time.sleep(5)
    logger.warning(f"Tailscale 连接超时 ({timeout}s)")
    return "N/A"


def get_service_status() -> Dict[str, bool]:
    """获取服务状态（oneshot 服务用 ActiveState 判断，避免 is-active 误报）"""
    services = {}
    for svc in ["teslausb-web", "teslausb-sentry", "teslausb-mode", "teslausb-fsck.timer", "teslausb-io-tune"]:
        try:
            # 先用 ActiveState（对 oneshot 服务也准确）
            r = subprocess.run(
                ["systemctl", "show", "-p", "ActiveState", svc],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                state = r.stdout.strip().split("=")[-1]
                services[svc] = state == "active"
            else:
                # 回退到 is-active
                r2 = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5
                )
                services[svc] = r2.returncode == 0
        except Exception:
            services[svc] = False
    return services


def main():
    """主入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, mode="a"),
        ],
    )

    logger.info("开机通知服务启动")

    # 收集系统信息
    boot_time = get_boot_time_str()
    cpu_info = get_cpu_info()
    nvme_temp = get_nvme_temp()
    memory_info = get_memory_info()
    disk_info = get_disk_info()

    # 等待 Tailscale
    tailscale_ip = wait_for_tailscale()

    # 本地 IP
    local_ip = get_local_ip()

    # WiFi 信息
    wifi_info = get_wifi_info()

    # 服务状态
    services = get_service_status()

    # 初始化通知器
    try:
        from weixin_notifier import WeixinNotifier
    except ImportError:
        logger.error("无法导入 WeixinNotifier")
        sys.exit(1)

    if not os.path.exists(CONFIG_PATH):
        logger.error(f"配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    key = cfg.get("wecom_status_webhook_key") or cfg.get("wecom_webhook_key", "")
    if not key:
        logger.error("未配置状态机器人 webhook key")
        sys.exit(1)

    notifier = WeixinNotifier(webhook_key=key, bot_name="系统通知")

    # 发送开机通知
    success = notifier.send_boot_notification(
        boot_time=boot_time,
        cpu_info=cpu_info,
        nvme_temp=nvme_temp,
        memory_info=memory_info,
        disk_info=disk_info,
        wifi_info=wifi_info,
        local_ip=local_ip,
        tailscale_ip=tailscale_ip,
        services=services,
    )

    if success:
        logger.info("开机通知发送成功")
    else:
        logger.error("开机通知发送失败（服务继续运行）")
        # 不退出，避免 systemd Restart=on-failure 无限重启


if __name__ == "__main__":
    main()
