#!/usr/bin/env python3
"""
TeslaUSB Neo - 硬件看门狗模块
==============================
功能：
1. 检测系统状态（CPU、内存、磁盘）
2. 监控关键服务健康
3. 触发硬件看门狗喂狗
4. 系统异常时自动重启

设计原理：
- Linux 标准硬件看门狗定时器
- 需要在 /boot/config.txt 启用: dtparam=watchdog=on
- systemd 可以配置 WatchdogSec 参数实现服务看门狗
- 本模块提供更细粒度的健康检查

作者: TeslaUSB-Neo 项目
"""

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 看门狗配置
# ═══════════════════════════════════════════════════════════

# 健康检查阈值
CPU_LOAD_THRESHOLD = 80      # CPU 负载百分比阈值
MEMORY_THRESHOLD = 85        # 内存使用百分比阈值
DISK_THRESHOLD = 95          # 磁盘使用百分比阈值
RESPONSE_TIMEOUT = 3        # 服务响应超时（秒），缩短避免阻塞

# 关键服务列表（需监控）
# 注意：只列入常驻服务，teslausb-gadget 仅在 USB 连接 Tesla 时运行，不列入
CRITICAL_SERVICES = [
    "teslausb-web",
    "teslausb-sentry",
]

# 状态文件
HEALTH_STATUS_FILE = "/opt/radxa_data/teslausb/data/health_status.json"
LOG_FILE = "/var/log/teslausb-watchdog.log"


class HardwareWatchdog:
    """硬件看门狗监控器"""

    def __init__(self):
        self.watchdog_dev = "/dev/watchdog"
        self.watchdog_fd = None   # 保持打开的看门狗文件描述符
        self.status = {
            "healthy": True,
            "last_check": None,
            "issues": [],
            "metrics": {},
        }

    def is_watchdog_available(self) -> bool:
        """检查硬件看门狗设备是否可用（不打开设备）"""
        if os.path.exists(self.watchdog_dev):
            logger.info("硬件看门狗设备存在: %s", self.watchdog_dev)
            return True
        else:
            logger.warning("硬件看门狗设备不存在: %s", self.watchdog_dev)
            return False

    def start_watchdog(self) -> bool:
        """打开看门狗设备，启动硬件定时器（保持 fd 打开）"""
        if self.watchdog_fd is not None:
            logger.debug("看门狗已经启动")
            return True
        try:
            self.watchdog_fd = os.open(self.watchdog_dev, os.O_WRONLY)
            logger.info("硬件看门狗已启动，超时=16秒")
            return True
        except Exception as e:
            logger.error("打开看门狗失败: %s", e)
            self.watchdog_fd = None
            return False

    def stop_watchdog(self):
        """安全关闭看门狗（写入 magic char 'V' 后 close）"""
        if self.watchdog_fd is not None:
            try:
                os.write(self.watchdog_fd, b"V")
                os.close(self.watchdog_fd)
                logger.info("硬件看门狗已安全关闭（magic close）")
            except Exception as e:
                logger.error("关闭看门狗失败: %s", e)
                try:
                    os.close(self.watchdog_fd)
                except Exception:
                    pass
            finally:
                self.watchdog_fd = None

    def pet_watchdog(self) -> bool:
        """喂狗（向已打开的 fd 写入数据，重启定时器）"""
        if self.watchdog_fd is None:
            logger.warning("看门狗未启动，无法喂狗")
            return False
        try:
            os.write(self.watchdog_fd, b"\n")
            return True
        except Exception as e:
            logger.error("喂狗失败: %s", e)
            return False

    def get_cpu_load(self) -> float:
        """获取 CPU 负载（1分钟平均）"""
        try:
            with open("/proc/loadavg", "r") as f:
                load = float(f.read().split()[0])
            # 获取 CPU 核心数
            with open("/proc/cpuinfo", "r") as f:
                cores = f.read().count("processor")
            if cores > 0:
                return (load / cores) * 100
            return load * 100
        except Exception as e:
            logger.error("获取 CPU 负载失败: %s", e)
            return 0.0

    def get_memory_usage(self) -> Dict:
        """获取内存使用情况"""
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            mem_info = {}
            for line in lines:
                parts = line.split()
                key = parts[0].rstrip(":")
                value = int(parts[1])
                mem_info[key] = value

            total = mem_info.get("MemTotal", 0)
            available = mem_info.get("MemAvailable", mem_info.get("MemFree", 0))
            used = total - available
            percent = (used / total * 100) if total > 0 else 0

            return {
                "total_mb": total // 1024,
                "used_mb": used // 1024,
                "available_mb": available // 1024,
                "percent": percent,
            }
        except Exception as e:
            logger.error("获取内存使用率失败: %s", e)
            return {"total_mb": 0, "used_mb": 0, "available_mb": 0, "percent": 0}

    def get_disk_usage(self, path: str = "/") -> Optional[Dict]:
        """获取磁盘使用情况"""
        try:
            stat = os.statvfs(path)
            total = stat.f_blocks * stat.f_frsize
            used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            return {
                "total_gb": total // (1024**3),
                "used_gb": used // (1024**3),
                "free_gb": free // (1024**3),
                "percent": int((stat.f_blocks - stat.f_bfree) * 100 / stat.f_blocks),
            }
        except Exception as e:
            logger.error("获取磁盘使用率失败: %s", e)
            return None

    def get_temperature(self) -> Optional[float]:
        """获取 CPU 温度（摄氏度）"""
        temp_paths = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ]
        for path in temp_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        raw = int(f.read().strip())
                        # 通常以毫度为单位
                        return raw / 1000.0
                except Exception:
                    continue
        return None

    def check_service_status(self, service_name: str) -> Dict:
        """检查 systemd 服务状态"""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True,
                text=True,
                timeout=RESPONSE_TIMEOUT,
            )
            active = result.returncode == 0
            status = result.stdout.strip()

            # 获取更多详情
            result = subprocess.run(
                ["systemctl", "show", service_name, "--property=ActiveState,SubState,MainPID"],
                capture_output=True,
                text=True,
                timeout=RESPONSE_TIMEOUT,
            )
            details = {}
            for line in result.stdout.strip().split("\n"):
                if "=" in line:
                    key, value = line.split("=", 1)
                    details[key] = value

            return {
                "active": active,
                "status": status,
                "details": details,
            }
        except subprocess.TimeoutExpired:
            return {"active": False, "status": "timeout", "details": {}}
        except Exception as e:
            logger.error("检查服务 %s 失败: %s", service_name, e)
            return {"active": False, "status": "error", "error": str(e), "details": {}}

    def check_network_connectivity(self) -> bool:
        """检查网络连通性"""
        test_hosts = ["8.8.8.8", "1.1.1.1", "baidu.com"]
        for host in test_hosts:
            try:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "2", host],
                    capture_output=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    def check_web_service(self, port: int = 5000) -> bool:
        """检查 Web 服务是否响应"""
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 f"http://localhost:{port}/", "--connect-timeout", "3", "--max-time", "3"],
                capture_output=True,
                text=True,
                timeout=RESPONSE_TIMEOUT,
            )
            code = result.stdout.strip()
            return code.startswith("2") or code.startswith("3")
        except Exception as e:
            logger.error("检查 Web 服务失败: %s", e)
            return False

    def run_health_check(self) -> Dict:
        """
        执行完整健康检查

        返回健康状态字典
        """
        self.status = {
            "healthy": True,
            "last_check": datetime.now().isoformat(),
            "issues": [],
            "metrics": {},
        }

        # 1. CPU 负载检查
        cpu_load = self.get_cpu_load()
        self.status["metrics"]["cpu_load"] = cpu_load
        if cpu_load > CPU_LOAD_THRESHOLD:
            self.status["issues"].append(f"CPU 负载过高: {cpu_load:.1f}%")
            if cpu_load > CPU_LOAD_THRESHOLD + 20:
                self.status["healthy"] = False

        # 2. 内存检查
        mem = self.get_memory_usage()
        self.status["metrics"]["memory"] = mem
        if mem["percent"] > MEMORY_THRESHOLD:
            self.status["issues"].append(f"内存使用过高: {mem['percent']:.1f}%")
            if mem["percent"] > MEMORY_THRESHOLD + 10:
                self.status["healthy"] = False

        # 3. 磁盘检查
        disk = self.get_disk_usage("/")
        if disk:
            self.status["metrics"]["disk_root"] = disk
            if disk["percent"] > DISK_THRESHOLD:
                self.status["issues"].append(f"根分区磁盘使用过高: {disk['percent']}%")
        # 检查 cam 分区（从容错配置读取，失败使用默认值）
        cam_path = "/mnt/teslacam"
        try:
            from config import PARTITIONS
            cam_path = PARTITIONS.get("cam", cam_path)
        except Exception:
            pass
        if os.path.ismount(cam_path):
            disk_cam = self.get_disk_usage(cam_path)
            if disk_cam:
                self.status["metrics"]["disk_cam"] = disk_cam

        # 4. 温度检查
        temp = self.get_temperature()
        if temp:
            self.status["metrics"]["temperature"] = temp
            if temp > 80:
                self.status["issues"].append(f"CPU 温度过高: {temp:.1f}°C")

        # 5. 关键服务检查
        self.status["metrics"]["services"] = {}
        service_all_ok = True
        for service in CRITICAL_SERVICES:
            svc_status = self.check_service_status(service)
            self.status["metrics"]["services"][service] = svc_status
            if not svc_status.get("active"):
                self.status["issues"].append("服务 %s 未运行" % service)
                service_all_ok = False
        if not service_all_ok:
            self.status["healthy"] = False

        # 6. 网络检查
        network = self.check_network_connectivity()
        self.status["metrics"]["network"] = network
        if not network:
            self.status["issues"].append("网络不可达")

        # 7. Web 服务检查
        web_ok = self.check_web_service()
        self.status["metrics"]["web_service"] = web_ok
        if not web_ok:
            self.status["issues"].append("Web 服务无响应")

        # 保存状态
        self._save_status()

        return self.status

    def _save_status(self):
        """保存健康状态到文件"""
        try:
            os.makedirs(os.path.dirname(HEALTH_STATUS_FILE), exist_ok=True)
            with open(HEALTH_STATUS_FILE, "w") as f:
                json.dump(self.status, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("保存健康状态失败: %s", e)

    def run_daemon(self, interval: int = 60) -> None:
        """
        以守护进程方式运行看门狗

        Args:
            interval: 健康检查间隔（秒），必须 < 16秒（看门狗超时）
        """
        if interval >= 16:
            logger.warning("健康检查间隔(%ds) 超过看门狗超时(16s)，强制改为5s", interval)
            interval = 5

        # 启动前等待系统完全启动（避免系统启动阶段超时）
        logger.info("等待系统完成启动（30秒）...")
        time.sleep(30)

        logger.info("看门狗守护进程启动，检查间隔: %ds", interval)

        # 启动看门狗（打开 fd，启动定时器）
        watchdog_active = self.start_watchdog()
        if not watchdog_active:
            logger.warning("硬件看门狗不可用，仅进行健康监控")

        consecutive_failures = 0
        max_failures = 3
        want_reboot = False

        try:
            while True:
                try:
                    # 已决定重启，不再喂狗，等待硬件看门狗超时
                    if want_reboot:
                        logger.info("已触发重启条件，等待硬件看门狗超时复位（16秒）...")
                        time.sleep(20)  # 等待超过16秒，让硬件复位
                        logger.warning("硬件看门狗未触发，执行软重启")
                        subprocess.run(["reboot"], check=False)
                        break

                    # 喂狗（确保看门狗在健康检查前已被喂过）
                    if watchdog_active:
                        self.pet_watchdog()

                    status = self.run_health_check()

                    if status["healthy"]:
                        consecutive_failures = 0
                        logger.info(
                            "健康检查通过，CPU: %.1f%%, 内存: %.1f%%",
                            status["metrics"].get("cpu_load", 0),
                            status["metrics"].get("memory", {}).get("percent", 0),
                        )
                    else:
                        consecutive_failures += 1
                        logger.warning(
                            "健康检查失败 (%d/%d): %s",
                            consecutive_failures,
                            max_failures,
                            ", ".join(status["issues"]),
                        )

                        if consecutive_failures >= max_failures:
                            logger.critical(
                                "连续 %d 次健康检查失败，准备触发硬件重启...",
                                max_failures,
                            )
                            # 不喂狗、不关闭看门狗，让硬件看门狗超时触发重启
                            want_reboot = True

                    time.sleep(interval)

                except Exception as e:
                    consecutive_failures += 1
                    logger.error(
                        "看门狗运行异常(#%d/%d): %s",
                        consecutive_failures,
                        max_failures,
                        e,
                    )

                    if consecutive_failures >= max_failures:
                        logger.critical(
                            "连续 %d 次异常，准备触发硬件重启...",
                            max_failures,
                        )
                        want_reboot = True

                    # 如果还没触发重启，等待下次检查
                    if not want_reboot:
                        time.sleep(interval)

        except KeyboardInterrupt:
            logger.info("收到退出信号，停止看门狗")
        finally:
            # 只有正常退出时才关闭看门狗
            # 如果 want_reboot=True（触发重启），不关闭看门狗，让硬件超时复位
            if not want_reboot:
                logger.info("看门狗守护进程退出，安全关闭看门狗")
                self.stop_watchdog()


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="TeslaUSB Neo 硬件看门狗")
    parser.add_argument("--check", action="store_true", help="执行单次健康检查")
    parser.add_argument("--daemon", action="store_true", help="以守护进程方式运行")
    parser.add_argument("--interval", type=int, default=60, help="检查间隔（秒）")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, mode="a"),
        ],
    )

    watchdog = HardwareWatchdog()

    if args.check:
        status = watchdog.run_health_check()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        exit(0 if status["healthy"] else 1)

    if args.daemon:
        watchdog.run_daemon(interval=args.interval)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
