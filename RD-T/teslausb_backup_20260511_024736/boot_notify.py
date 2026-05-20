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
CONFIG_PATH = "/opt/teslausb-web/config/sentry.json"
LOG_FILE = "/var/log/teslausb-boot-notify.log"

# Tailscale 等待超时
TAILSCALE_WAIT_TIMEOUT = 180

# 分区信息
PARTITIONS = {
    "系统盘": "/data",
    "TeslaCam": "/media/cnlvan/cam",
    "LightShow": "/media/cnlvan/lightshow",
    "Music": "/media/cnlvan/music",
}


def get_boot_time_str() -> str:
    """获取启动时间"""
    try:
        result = subprocess.run(
            ["systemd-analyze"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            import re
            match = re.search(r'=\s*(.+)', result.stdout)
            if match:
                return match.group(1).strip()
    except Exception:
        pass

    # 备用
    try:
        with open("/proc/uptime", "r") as f:
            uptime_s = float(f.read().split()[0])
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)
        seconds = int(uptime_s % 60)
        return f"{hours}h {minutes}m {seconds}s"
    except Exception:
        return "N/A"


def get_cpu_info() -> Dict:
    """获取 CPU 信息"""
    model = "Unknown"
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("model name"):
                    model = line.split(":")[1].strip()
                    break
    except Exception:
        pass

    freq_mhz = 0
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "r") as f:
            freq_mhz = int(f.read().strip()) // 1000
    except Exception:
        pass

    temp_c = 0.0
    for path in ["/sys/class/thermal/thermal_zone0/temp",
                 "/sys/class/hwmon/hwmon0/temp1_input"]:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    temp_c = int(f.read().strip()) / 1000.0
                break
            except Exception:
                pass

    return {
        "model": model,
        "freq_mhz": freq_mhz,
        "temp_c": temp_c,
    }


def get_memory_info() -> Dict:
    """获取内存信息"""
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
        return {
            "total_mb": round(total_kb / 1024),
            "used_mb": round(used_kb / 1024),
            "pct": round(pct, 1),
        }
    except Exception:
        return {"total_mb": 0, "used_mb": 0, "pct": 0}


def get_disk_info() -> list:
    """获取磁盘信息"""
    import shutil
    disks = []
    for name, path in PARTITIONS.items():
        try:
            if os.path.ismount(path):
                usage = shutil.disk_usage(path)
                pct = round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0
                disks.append({
                    "name": name,
                    "total_gb": round(usage.total / (1024 ** 3), 1),
                    "used_gb": round(usage.used / (1024 ** 3), 1),
                    "free_gb": round(usage.free / (1024 ** 3), 1),
                    "percent": pct,
                })
            else:
                disks.append({"name": name, "total_gb": 0, "used_gb": 0, "percent": 0, "note": "未挂载"})
        except Exception:
            disks.append({"name": name, "total_gb": 0, "used_gb": 0, "percent": 0, "note": "无法读取"})
    return disks


def get_wifi_info() -> Dict:
    """获取 WiFi 信息"""
    try:
        result = subprocess.run(
            ["iwconfig", "wlan0"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout + result.stderr
        info = {"ssid": "N/A", "signal_pct": 0, "freq_ghz": 0}

        import re
        ssid_match = re.search(r'ESSID:"([^"]*)"', output)
        if ssid_match:
            info["ssid"] = ssid_match.group(1)

        signal_match = re.search(r'Signal level=(-?\d+) dBm', output)
        if signal_match:
            dbm = int(signal_match.group(1))
            info["signal_pct"] = min(100, max(0, 2 * (dbm + 100)))

        return info
    except Exception:
        return {"ssid": "N/A", "signal_pct": 0, "freq_ghz": 0}


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
    """获取服务状态"""
    services = {}
    for svc in ["teslausb-web", "teslausb-sentry", "teslausb-watchdog"]:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5
            )
            services[svc] = r.returncode == 0
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
    memory_info = get_memory_info()
    disk_info = get_disk_info()

    # 等待 Tailscale
    tailscale_ip = wait_for_tailscale()

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
        memory_info=memory_info,
        disk_info=disk_info,
        wifi_info=wifi_info,
        tailscale_ip=tailscale_ip,
        services=services,
    )

    if success:
        logger.info("开机通知发送成功")
    else:
        logger.error("开机通知发送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
