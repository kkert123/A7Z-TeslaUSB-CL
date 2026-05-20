#!/usr/bin/env python3

"""
TeslaUSB Neo - 系统监控告警模块
================================

功能：
1. CPU 高温告警 (75°C 警告, 82°C 严重)
2. CPU 高负载告警 (85% 持续 120s)
3. 内存高使用告警 (88% 持续 120s)
4. 断网恢复通知 (离线 120s 后恢复)
5. 存储空间告警 (5GB 警告, 2GB 严重)
6. 智能心跳 (每 60 分钟, 强制每 6 小时)
7. 服务异常告警

基于旧版 sentry_monitor.py 重构，集成到当前架构

作者: TeslaUSB-Neo 项目
版本: 1.0.0
"""

import json
import logging
import os
import shutil
import subprocess
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('system_monitor')

# ═══════════════════════════════════════════════════════════
# 告警阈值配置
# ═══════════════════════════════════════════════════════════

# CPU 温度
TEMP_WARN_C = 75          # 温度警告阈值
TEMP_CRIT_C = 82          # 温度严重阈值

# CPU 负载
CPU_HIGH_PCT = 85         # CPU 高负载阈值 (%)
CPU_HIGH_DURATION_S = 120 # 持续时间 (秒)

# 内存
MEM_HIGH_PCT = 88         # 内存高使用阈值 (%)
MEM_HIGH_DURATION_S = 120 # 持续时间 (秒)

# 网络
NET_OFFLINE_MIN_S = 120   # 离线最小时长才通知恢复 (秒)

# 存储
STORAGE_WARN_GB = 5       # 存储警告阈值 (GB)
STORAGE_CRIT_GB = 2       # 存储严重阈值 (GB)

# 心跳
HEARTBEAT_CHECK_MIN = 60  # 心跳检查间隔 (分钟)
HEARTBEAT_FORCE_H = 6     # 强制心跳间隔 (小时)

# 冷却时间
COOLDOWN_S = 900          # 告警冷却时间 (15 分钟)

# 关键服务
CRITICAL_SERVICES = [
    "teslausb-web",
    "teslausb-sentry",
]

# 状态文件
DATA_DIR = "/opt/teslausb-web/data"
HEALTH_STATUS_FILE = os.path.join(DATA_DIR, "health_status.json")
LOG_FILE = "/var/log/teslausb-watchdog.log"


# ═══════════════════════════════════════════════════════════
# 告警冷却管理器
# ═══════════════════════════════════════════════════════════

class CooldownManager:
    """线程安全的告警冷却管理器"""

    def __init__(self, cooldown_seconds: int = COOLDOWN_S):
        self._cooldown = cooldown_seconds
        self._timers: Dict[str, float] = {}
        self._lock = threading.Lock()

    def can_alert(self, alert_type: str) -> bool:
        """检查是否可以发送告警（冷却是否过期）"""
        with self._lock:
            last = self._timers.get(alert_type, 0)
            return (time.time() - last) >= self._cooldown

    def record_alert(self, alert_type: str):
        """记录告警时间"""
        with self._lock:
            self._timers[alert_type] = time.time()


# ═══════════════════════════════════════════════════════════
# 网络检测
# ═══════════════════════════════════════════════════════════

class NetworkDetector:
    """三重投票网络检测器"""

    # DNS 服务器
    DNS_SERVERS = [
        ("114.114.114.114", 53),
        ("223.5.5.5", 53),
    ]

    @staticmethod
    def _tcp_test(host: str, port: int, timeout: float = 3.0) -> bool:
        """TCP 连接测试"""
        import socket
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except (socket.timeout, OSError):
            return False

    @staticmethod
    def _ping_gateway(timeout: float = 3.0) -> bool:
        """Ping 网关"""
        try:
            # 获取默认网关
            result = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                gateway = result.stdout.strip().split()[2]
                # Ping 网关
                ping_result = subprocess.run(
                    ["ping", "-c", "1", "-W", str(int(timeout)), gateway],
                    capture_output=True, timeout=timeout + 2
                )
                return ping_result.returncode == 0
        except Exception:
            pass
        return False

    @classmethod
    def check_network(cls) -> bool:
        """三重投票网络检测 (2/3 通过即为在线)"""
        votes = 0
        for host, port in cls.DNS_SERVERS:
            if cls._tcp_test(host, port):
                votes += 1
        if cls._ping_gateway():
            votes += 1
        return votes >= 2


# ═══════════════════════════════════════════════════════════
# 心跳状态
# ═══════════════════════════════════════════════════════════

class HeartbeatState:
    """智能心跳状态跟踪"""

    def __init__(self):
        self.last_forced = time.time()
        self.last_checked = time.time()
        self.prev_state: Dict = {}

    def should_check(self) -> bool:
        """是否需要检查心跳"""
        elapsed = (time.time() - self.last_checked) / 60
        return elapsed >= HEARTBEAT_CHECK_MIN

    def should_force(self) -> bool:
        """是否需要强制心跳"""
        elapsed = (time.time() - self.last_forced) / 3600
        return elapsed >= HEARTBEAT_FORCE_H

    def record_check(self, forced: bool = False):
        """记录心跳检查"""
        self.last_checked = time.time()
        if forced:
            self.last_forced = time.time()

    def diff_reasons(self, current: Dict) -> List[str]:
        """比较与上次状态的差异"""
        reasons = []
        if not self.prev_state:
            return ["首次心跳"]

        # CPU 温度变化 >5°C
        prev_temp = self.prev_state.get("cpu_temp_c", 0)
        cur_temp = current.get("cpu_temp_c", 0)
        if abs(cur_temp - prev_temp) > 5:
            reasons.append(f"温度变化: {prev_temp:.0f}°C → {cur_temp:.0f}°C")

        # CPU 负载变化 >20%
        prev_cpu = self.prev_state.get("cpu_load_pct", 0)
        cur_cpu = current.get("cpu_load_pct", 0)
        if abs(cur_cpu - prev_cpu) > 20:
            reasons.append(f"负载变化: {prev_cpu:.0f}% → {cur_cpu:.0f}%")

        # 内存变化 >10%
        prev_mem = self.prev_state.get("mem_pct", 0)
        cur_mem = current.get("mem_pct", 0)
        if abs(cur_mem - prev_mem) > 10:
            reasons.append(f"内存变化: {prev_mem:.0f}% → {cur_mem:.0f}%")

        # 网络状态变化
        prev_net = self.prev_state.get("network", None)
        cur_net = current.get("network", None)
        if prev_net is not None and prev_net != cur_net:
            reasons.append(f"网络: {'在线' if prev_net else '离线'} → {'在线' if cur_net else '离线'}")

        return reasons if reasons else ["例行心跳"]

    def update(self, current: Dict):
        """更新状态"""
        self.prev_state = current.copy()


# ═══════════════════════════════════════════════════════════
# 系统信息采集
# ═══════════════════════════════════════════════════════════

class SystemInfo:
    """系统信息采集工具"""

    @staticmethod
    def get_cpu_load() -> float:
        """获取 CPU 负载 (1分钟平均, 百分比)"""
        try:
            with open("/proc/loadavg", "r") as f:
                load = float(f.read().split()[0])
            with open("/proc/cpuinfo", "r") as f:
                cores = f.read().count("processor")
            return (load / max(cores, 1)) * 100
        except Exception:
            return 0.0

    @staticmethod
    def get_cpu_temp() -> float:
        """获取 CPU 温度 (°C)"""
        for path in ["/sys/class/thermal/thermal_zone0/temp",
                     "/sys/class/hwmon/hwmon0/temp1_input"]:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        return int(f.read().strip()) / 1000.0
                except Exception:
                    pass
        return 0.0

    @staticmethod
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
                "available_mb": round(available_kb / 1024),
                "pct": round(pct, 1),
            }
        except Exception:
            return {"total_mb": 0, "used_mb": 0, "available_mb": 0, "pct": 0}

    @staticmethod
    def get_disk_free(path: str = "/") -> Dict:
        """获取磁盘剩余空间"""
        try:
            usage = shutil.disk_usage(path)
            free_gb = usage.free / (1024 ** 3)
            pct = (usage.used / usage.total * 100) if usage.total > 0 else 0
            return {
                "free_gb": round(free_gb, 1),
                "total_gb": round(usage.total / (1024 ** 3), 1),
                "used_gb": round(usage.used / (1024 ** 3), 1),
                "percent": round(pct, 1),
            }
        except Exception:
            return {"free_gb": 0, "total_gb": 0, "used_gb": 0, "percent": 0}

    @staticmethod
    def get_wifi_info() -> Dict:
        """获取 WiFi 信息"""
        try:
            result = subprocess.run(
                ["iwconfig", "wlan0"],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout + result.stderr
            info = {"ssid": "N/A", "signal_pct": 0, "signal_dbm": 0, "freq_ghz": 0}

            # 解析 SSID
            import re
            ssid_match = re.search(r'ESSID:"([^"]*)"', output)
            if ssid_match:
                info["ssid"] = ssid_match.group(1)

            # 解析信号强度
            signal_match = re.search(r'Signal level=(-?\d+) dBm', output)
            if signal_match:
                dbm = int(signal_match.group(1))
                info["signal_dbm"] = dbm
                info["signal_pct"] = min(100, max(0, 2 * (dbm + 100)))

            # 解析频率
            freq_match = re.search(r'Frequency:([\d.]+)', output)
            if freq_match:
                info["freq_ghz"] = float(freq_match.group(1))

            return info
        except Exception:
            return {"ssid": "N/A", "signal_pct": 0, "signal_dbm": 0, "freq_ghz": 0}

    @staticmethod
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

        # 备用方法
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

    @staticmethod
    def get_boot_time() -> str:
        """获取启动时间"""
        try:
            result = subprocess.run(
                ["systemd-analyze"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                # 提取启动时间
                import re
                match = re.search(r'=\s*(.+)', result.stdout)
                if match:
                    return match.group(1).strip()
        except Exception:
            pass

        # 备用: /proc/uptime
        try:
            with open("/proc/uptime", "r") as f:
                uptime_s = float(f.read().split()[0])
            hours = int(uptime_s // 3600)
            minutes = int((uptime_s % 3600) // 60)
            return f"{hours}h {minutes}m"
        except Exception:
            return "N/A"

    @staticmethod
    def get_service_status(services: List[str]) -> Dict[str, bool]:
        """获取服务状态"""
        result = {}
        for svc in services:
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5
                )
                result[svc] = r.returncode == 0
            except Exception:
                result[svc] = False
        return result

    @staticmethod
    def get_cpu_info() -> Dict:
        """获取 CPU 详细信息"""
        model = "Unknown"
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.startswith("model name"):
                        model = line.split(":")[1].strip()
                        break
        except Exception:
            pass

        # 获取频率
        freq_mhz = 0
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "r") as f:
                freq_mhz = int(f.read().strip()) // 1000
        except Exception:
            pass

        return {
            "model": model,
            "freq_mhz": freq_mhz,
            "usage_pct": SystemInfo.get_cpu_load(),
            "temp_c": SystemInfo.get_cpu_temp(),
        }


# ═══════════════════════════════════════════════════════════
# 系统监控主类
# ═══════════════════════════════════════════════════════════

class SystemMonitor:
    """系统监控告警服务"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.cooldown = CooldownManager()
        self.heartbeat = HeartbeatState()
        self.net_detector = NetworkDetector()
        self.sys_info = SystemInfo()

        # 状态追踪
        self._cpu_high_since: Optional[float] = None
        self._mem_high_since: Optional[float] = None
        self._net_offline_since: Optional[float] = None
        self._was_online: Optional[bool] = None

        # 通知器
        self._notifier = None
        self._running = False

    def _init_notifier(self):
        """初始化微信通知器"""
        try:
            from weixin_notifier import WeixinNotifier
            sentry_cfg_path = os.path.join(os.path.dirname(__file__), "config", "sentry.json")
            if os.path.exists(sentry_cfg_path):
                with open(sentry_cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                key = cfg.get("wecom_status_webhook_key") or cfg.get("wecom_webhook_key", "")
                if key:
                    self._notifier = WeixinNotifier(webhook_key=key, bot_name="系统通知")
                    logger.info("系统监控通知器初始化成功")
                else:
                    logger.warning("未找到状态机器人 webhook key")
            else:
                logger.warning(f"配置文件不存在: {sentry_cfg_path}")
        except Exception as e:
            logger.error(f"初始化通知器失败: {e}")

    def _send_alert(self, title: str, message: str, level: str = "warning"):
        """发送告警通知"""
        if not self._notifier:
            return
        try:
            self._notifier.send_system_alert(title=title, message=message, level=level)
        except Exception as e:
            logger.error(f"发送告警失败: {e}")

    # ═════════════════════════════════════════════════════════
    # 告警检查
    # ═════════════════════════════════════════════════════════

    def check_cpu_temp(self):
        """检查 CPU 温度"""
        temp = self.sys_info.get_cpu_temp()
        if temp >= TEMP_CRIT_C:
            if self.cooldown.can_alert("cpu_temp_crit"):
                self._send_alert(
                    "🚨 CPU 温度过高",
                    f"当前温度: {temp:.1f}°C (严重阈值: {TEMP_CRIT_C}°C)\n"
                    f"请检查散热状况",
                    level="error"
                )
                self.cooldown.record_alert("cpu_temp_crit")
        elif temp >= TEMP_WARN_C:
            if self.cooldown.can_alert("cpu_temp_warn"):
                self._send_alert(
                    "⚠️ CPU 温度偏高",
                    f"当前温度: {temp:.1f}°C (警告阈值: {TEMP_WARN_C}°C)",
                    level="warning"
                )
                self.cooldown.record_alert("cpu_temp_warn")

    def check_cpu_load(self):
        """检查 CPU 负载"""
        load = self.sys_info.get_cpu_load()
        now = time.time()

        if load >= CPU_HIGH_PCT:
            if self._cpu_high_since is None:
                self._cpu_high_since = now
            elif (now - self._cpu_high_since) >= CPU_HIGH_DURATION_S:
                if self.cooldown.can_alert("cpu_high"):
                    duration = int(now - self._cpu_high_since)
                    self._send_alert(
                        "⚠️ CPU 高负载",
                        f"当前负载: {load:.1f}% (阈值: {CPU_HIGH_PCT}%)\n"
                        f"已持续: {duration // 60} 分钟",
                        level="warning"
                    )
                    self.cooldown.record_alert("cpu_high")
        else:
            self._cpu_high_since = None

    def check_memory(self):
        """检查内存使用"""
        mem = self.sys_info.get_memory_info()
        pct = mem.get("pct", 0)
        now = time.time()

        if pct >= MEM_HIGH_PCT:
            if self._mem_high_since is None:
                self._mem_high_since = now
            elif (now - self._mem_high_since) >= MEM_HIGH_DURATION_S:
                if self.cooldown.can_alert("mem_high"):
                    duration = int(now - self._mem_high_since)
                    self._send_alert(
                        "⚠️ 内存使用过高",
                        f"当前: {mem['used_mb']}MB / {mem['total_mb']}MB ({pct:.1f}%)\n"
                        f"阈值: {MEM_HIGH_PCT}%\n"
                        f"已持续: {duration // 60} 分钟",
                        level="warning"
                    )
                    self.cooldown.record_alert("mem_high")
        else:
            self._mem_high_since = None

    def check_network(self):
        """检查网络状态"""
        online = self.net_detector.check_network()
        now = time.time()

        if online:
            if self._net_offline_since is not None:
                offline_duration = int(now - self._net_offline_since)
                self._net_offline_since = None
                if offline_duration >= NET_OFFLINE_MIN_S:
                    if self.cooldown.can_alert("net_restored"):
                        minutes = offline_duration // 60
                        self._send_alert(
                            "✅ 网络已恢复",
                            f"离线时长: {minutes} 分钟\n"
                            f"所有服务已恢复正常",
                            level="info"
                        )
                        self.cooldown.record_alert("net_restored")
            self._was_online = True
        else:
            if self._was_online is not False:
                self._net_offline_since = now
            self._was_online = False

    def check_storage(self):
        """检查存储空间（仅监控 TeslaCam 分区 /media/cnlvan/cam）"""
        path = "/media/cnlvan/cam"
        name = "TeslaCam"
        disk = self.sys_info.get_disk_free(path)
        free_gb = disk.get("free_gb", 0)

        if free_gb <= STORAGE_CRIT_GB:
            if self.cooldown.can_alert(f"storage_crit_{name}"):
                self._send_alert(
                    "🚨 存储空间严重不足",
                    f"{name}: 剩余 {free_gb}GB (严重阈值: {STORAGE_CRIT_GB}GB)\n"
                    f"请立即清理 TeslaCam 文件",
                    level="error"
                )
                self.cooldown.record_alert(f"storage_crit_{name}")
        elif free_gb <= STORAGE_WARN_GB:
            if self.cooldown.can_alert(f"storage_warn_{name}"):
                self._send_alert(
                    "⚠️ 存储空间不足",
                    f"{name}: 剩余 {free_gb}GB (警告阈值: {STORAGE_WARN_GB}GB)",
                    level="warning"
                )
                self.cooldown.record_alert(f"storage_warn_{name}")

    def check_heartbeat(self):
        """智能心跳"""
        if not self.heartbeat.should_check() and not self.heartbeat.should_force():
            return

        is_forced = self.heartbeat.should_force()

        # 收集当前状态
        current = {
            "cpu_temp_c": self.sys_info.get_cpu_temp(),
            "cpu_load_pct": self.sys_info.get_cpu_load(),
            "mem_pct": self.sys_info.get_memory_info().get("pct", 0),
            "network": self.net_detector.check_network(),
        }

        reasons = self.heartbeat.diff_reasons(current)

        if is_forced or reasons != ["例行心跳"]:
            self._send_heartbeat(current, reasons, forced=is_forced)

        self.heartbeat.record_check(forced=is_forced)
        self.heartbeat.update(current)

    def _send_heartbeat(self, current: Dict, reasons: List[str], forced: bool = False):
        """发送心跳通知"""
        if not self._notifier:
            return

        temp = current.get("cpu_temp_c", 0)
        cpu = current.get("cpu_load_pct", 0)
        mem_pct = current.get("mem_pct", 0)
        net = current.get("network", False)

        title = "📡 系统心跳" + (" (强制)" if forced else "")
        message = (
            f"CPU: {cpu:.1f}% | 温度: {temp:.1f}°C\n"
            f"内存: {mem_pct:.1f}%\n"
            f"网络: {'✅ 在线' if net else '❌ 离线'}\n"
            f"原因: {', '.join(reasons)}"
        )

        try:
            self._notifier.send_system_alert(
                title=title,
                message=message,
                level="info"
            )
        except Exception as e:
            logger.error(f"发送心跳失败: {e}")

    def save_health_status(self):
        """保存健康状态到文件"""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)

            status = {
                "healthy": True,
                "last_check": datetime.now().isoformat(),
                "issues": [],
                "metrics": {
                    "cpu_load": self.sys_info.get_cpu_load(),
                    "temperature": self.sys_info.get_cpu_temp(),
                    "memory": self.sys_info.get_memory_info(),
                    "network": self.net_detector.check_network(),
                }
            }

            # 检查是否有问题
            if status["metrics"]["temperature"] >= TEMP_WARN_C:
                status["healthy"] = False
                status["issues"].append(f"CPU 温度过高: {status['metrics']['temperature']:.1f}°C")
            if status["metrics"]["cpu_load"] >= CPU_HIGH_PCT:
                status["healthy"] = False
                status["issues"].append(f"CPU 负载过高: {status['metrics']['cpu_load']:.1f}%")
            if status["metrics"]["memory"]["pct"] >= MEM_HIGH_PCT:
                status["healthy"] = False
                status["issues"].append(f"内存使用过高: {status['metrics']['memory']['pct']:.1f}%")
            if not status["metrics"]["network"]:
                status["issues"].append("网络离线")

            with open(HEALTH_STATUS_FILE, "w") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)

        except Exception as e:
            logger.error(f"保存健康状态失败: {e}")

    # ═════════════════════════════════════════════════════════
    # 运行主循环
    # ═════════════════════════════════════════════════════════

    def run_check_cycle(self):
        """执行一轮完整检查"""
        self.check_cpu_temp()
        self.check_cpu_load()
        self.check_memory()
        self.check_network()
        self.check_storage()
        self.check_heartbeat()
        self.save_health_status()

    def run_daemon(self, interval: int = 60):
        """守护进程模式运行"""
        logger.info("系统监控守护进程启动")
        self._init_notifier()

        # 启动延迟 90 秒（避免与开机通知重叠）
        logger.info("等待 90 秒启动延迟...")
        time.sleep(90)

        self._running = True
        while self._running:
            try:
                self.run_check_cycle()
            except Exception as e:
                logger.error(f"检查周期异常: {e}")
            time.sleep(interval)

    def run_single_check(self) -> Dict:
        """执行单次健康检查"""
        self.run_check_cycle()
        try:
            with open(HEALTH_STATUS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {"healthy": True, "issues": [], "metrics": {}}


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(description="TeslaUSB Neo 系统监控")
    parser.add_argument("--check", action="store_true", help="执行单次健康检查")
    parser.add_argument("--daemon", action="store_true", help="以守护进程方式运行")
    parser.add_argument("--interval", type=int, default=60, help="检查间隔（秒）")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, mode="a"),
        ],
    )

    monitor = SystemMonitor()

    if args.check:
        status = monitor.run_single_check()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        exit(0 if status.get("healthy", True) else 1)

    if args.daemon:
        monitor.run_daemon(interval=args.interval)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
