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
import signal
import subprocess
import tempfile
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
RESPONSE_TIMEOUT = 8        # 服务响应超时（秒），放宽避免单线程排队误报

# v0.3.1.66 / M107：启动宽限期（秒）——只推迟「动作」，**不推迟「喂狗」**。
# 硬件超时固定 16s（见 start_watchdog 日志）；宽限期必须 > 0 才不会在系统
# 启动阶段误判故障而复位，但**期间必须持续喂狗**，否则宽限期本身就成了
# 「重启该服务 = 整机复位」的窗口。
STARTUP_GRACE_SECONDS = 30

# 关键服务列表（需监控）
# 注意：只列入常驻服务，teslausb-gadget 仅在 USB 连接 Tesla 时运行，不列入
CRITICAL_SERVICES = [
    "teslausb-web",
    "teslausb-sentry",
]

# 状态文件
HEALTH_STATUS_FILE = "/opt/radxa_data/teslausb/data/health_status.json"
LOG_FILE = "/var/log/teslausb-watchdog.log"

# 外部重启请求（v0.3.1.57 / M80）：usb_guard 在 L1/L2 均无法恢复 USB 链路时写入，
# 看门狗消费该文件并触发硬件复位。TTL 防陈旧文件在重启后被误消费 → 重启循环。
REBOOT_REQUEST_FILE = "/opt/radxa_data/teslausb/data/usb_guard_reboot_request.json"
REBOOT_REQUEST_TTL = 600  # 秒

# ── 网络致命判据（v0.3.1.91，2026-11-15 实证）─────────────────────
# 那天 A7Z 断网 2h12m（61 次重连全无效），看门狗 717 行日志全是「健康检查通过」：
# 网络不可达只进 issues、不改 healthy，`max_failures` 永远凑不齐，于是没有任何兜底，
# 最后靠整车下电（A7Z 断电）才恢复。
# 现在改为：连续 NET_FATAL_STREAK 次不可达（interval 5s × 30 = 150s）且护栏通过 → 致命。
#
# 护栏（缺一不可，否则「车开出地库没网」会被误重启）：
#   · A 端 guard 快照新鲜（age_s 与文件 mtime 双校验），且 vehicle 不是 driving / speed<=3
#   · 距上次「网络重启」超过当前冷却窗口（见下）
#
# ── 重启冷却（v0.3.1.91，按「重启是否有效」分级）────────────────────
# 为什么不能用一个固定 30 分钟：quick-check 是 2min/轮、阶梯攒到第 15 档要 15 轮 = 30 分钟，
# 而重启后 /var/run（tmpfs）被清空、计数归零 —— 又要 30 分钟才够再次触发。
# 固定 30 分钟冷却刚好与「设备自己攒到该重启」等长，**等于没限流**：
# MIFI 真没网时会表现为「每 30 分钟重启一次」，一天 48 次。
#
# 改为按「上次重启有没有救活」分级（判据是 A7Z 自己能不能上网，不需要外部参与）：
#   有效（重启后网络恢复）   → 30min 防抖，只防判定抖动连着重启
#   1 次无效（10min 仍无网） → 60min
#   2 次无效                → 120min
#   3 次无效                → 24h 熔断（基本可断定热点/运营商侧没网，重启 A7Z 无意义，只告警）
# 网络一恢复就把 ineffective 清零（有效重启不该累积惩罚），但保留 at 做 30min 防抖。
NET_FATAL_STREAK = 30
NET_REBOOT_MIN_GAP = 1800          # 30min：有效重启后的防抖窗口
NET_REBOOT_MAX_GAP = 14400         # 4h：指数退避上限
NET_REBOOT_HARD_GAP = 86400        # 24h：连续无效达上限后的熔断
NET_REBOOT_MAX_INVALID = 3         # 连续无效次数上限
NET_REBOOT_VERIFY_S = 600          # 重启后多久仍无网，算「这次重启无效」
NET_REBOOT_STAMP_FILE = "/opt/radxa_data/teslausb/data/watchdog_net_reboot_at"
GUARD_STATUS_FILE = "/opt/radxa_data/teslausb/data/guard_status.json"


class HardwareWatchdog:
    """硬件看门狗监控器"""

    def __init__(self):
        self.watchdog_dev = "/dev/watchdog"
        self.watchdog_fd = None   # 保持打开的看门狗文件描述符
        # v0.3.1.66 / M107：已决定重启 → 信号处理器不得 magic close（要保持武装）
        self._want_reboot = False
        # v0.3.1.91：网络连续不可达计数（ping 通一次即归零）
        self._net_fail_streak = 0
        # v0.3.1.91：本次启动是否已判定过「上次网络重启是否有效」（每次启动只判一次）
        self._reboot_verified = False
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

    def install_signal_handlers(self) -> None:
        """v0.3.1.66 / M107：把「被停止」变成安全路径。

        原实现只在 `finally`（正常退出/KeyboardInterrupt）里 magic close，
        而 systemd 停止服务发的是 **SIGTERM** —— Python 默认动作直接终止进程，
        `finally` 根本不执行 → 硬件看门狗**保持武装**（nowayout），
        加上启动后 30s 不喂狗的空窗，`systemctl restart` 变成「必然整机复位」。
        实测内核证据：`watchdog: watchdog0: watchdog did not stop!`。
        """
        def _handler(signum, _frame):
            if self._want_reboot:
                logger.info("收到信号 %s：已决定重启，保持看门狗武装", signum)
            else:
                logger.info("收到信号 %s：安全关闭看门狗后退出", signum)
                self.stop_watchdog()
            os._exit(0)

        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, name, None)   # SIGHUP 在部分平台不存在
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

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
                 f"http://localhost:{port}/", "--connect-timeout", "5", "--max-time", "5"],
                capture_output=True,
                text=True,
                timeout=RESPONSE_TIMEOUT,
            )
            code = result.stdout.strip()
            return code.startswith("2") or code.startswith("3")
        except Exception as e:
            logger.error("检查 Web 服务失败: %s", e)
            return False

    def _net_fatal_allowed(self) -> Tuple[bool, str]:
        """网络判死的护栏。返回 (是否允许, 否决原因)。

        这个判据最终会触发**整机复位**：误判的代价是打断一次录制，漏判的代价是
        「断网两小时没人管」（2026-11-15 实际发生）。所以证据缺一不可：
          1. A 端 guard 快照新鲜（age_s<=600 且文件 mtime <=600），否则本地守护也停了
          2. 车辆不在行驶（state=driving 或 speed>3）—— 车在开时脱离热点是常态
          3. 距上次「网络重启」超过当前冷却窗口（按无效次数指数退避，见常量区注释）
        """
        rec = self._net_reboot_record()
        last = float(rec.get("at") or 0)
        if last > 0:
            gap = self._net_reboot_gap(rec)
            left = gap - (time.time() - last)
            if left > 0:
                ineff = int(rec.get("ineffective") or 0)
                if ineff >= NET_REBOOT_MAX_INVALID:
                    return False, ("熔断中（剩余 %d 分钟，已连续 %d 次重启无效，"
                                   "疑似热点/运营商侧无网）"
                                   % (int(left / 60), ineff))
                return False, ("冷却中（剩余 %d 分钟，累计无效重启 %d 次）"
                               % (int(left / 60), ineff))

        try:
            with open(GUARD_STATUS_FILE, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError) as e:
            return False, "guard 快照不可读: %s" % e

        # 注意：`age_s` 是**文件内**字段，文件本身可能是几小时前写的（guard 线程死了）。
        # 只看 age_s 会把「一小时前的 age_s=60」当成新鲜证据，必须并列校验 mtime。
        try:
            if time.time() - os.path.getmtime(GUARD_STATUS_FILE) > 600:
                return False, "guard 快照文件本身过期（mtime）"
        except OSError as e:
            return False, "guard 快照 mtime 读取失败: %s" % e

        v = snap.get("vehicle") if isinstance(snap.get("vehicle"), dict) else {}
        age = v.get("age_s")
        if not isinstance(age, (int, float)) or age > 600:
            return False, "guard 车态过期（age_s=%s）" % age
        if str(v.get("state") or "").strip().lower() == "driving":
            return False, "车在行驶（state=driving）"
        sp = v.get("speed")
        if isinstance(sp, (int, float)) and sp > 3:
            return False, "车在行驶（speed=%s）" % sp
        return True, ""

    # ── 网络重启记录（v0.3.1.91）─────────────────────────────
    def _net_reboot_record(self) -> Dict:
        """读持久记录 `{"at": 上次网络重启时刻, "ineffective": 连续无效次数}`。

        兼容 v0.3.1.91 初版：文件里只有一个裸浮点时间戳。
        """
        try:
            with open(NET_REBOOT_STAMP_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            return {"at": 0.0, "ineffective": 0}
        if not raw:
            return {"at": 0.0, "ineffective": 0}
        try:
            return {"at": float(raw), "ineffective": 0}      # 旧格式（裸时间戳）
        except ValueError:
            pass
        try:
            d = json.loads(raw)
        except ValueError:
            logger.warning("网络重启记录不可解析，按「未重启过」处理: %r", raw[:80])
            return {"at": 0.0, "ineffective": 0}
        if not isinstance(d, dict):
            return {"at": 0.0, "ineffective": 0}
        try:
            at = float(d.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        try:
            ineff = int(d.get("ineffective") or 0)
        except (TypeError, ValueError):
            ineff = 0
        return {"at": max(0.0, at), "ineffective": max(0, ineff)}

    def _save_net_reboot_record(self, rec: Dict) -> None:
        """原子写（mkstemp + os.replace）。

        重启瞬间进程可能被硬件看门狗 kill，写一半的文件读回来会变成「未重启过」→
        冷却直接失效 → 立刻二次重启。所以必须原子替换。
        """
        d = os.path.dirname(NET_REBOOT_STAMP_FILE) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".netreboot-", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(rec, f)
                os.replace(tmp, NET_REBOOT_STAMP_FILE)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning("网络重启记录写入失败（冷却可能失效，但不阻止本次重启）: %s", e)

    def _net_reboot_gap(self, rec: Dict) -> float:
        """当前冷却窗口（秒）：30min → 60 → 120 → 240 封顶；无效达上限 → 24h 熔断。"""
        n = int(rec.get("ineffective") or 0)
        if n >= NET_REBOOT_MAX_INVALID:
            return float(NET_REBOOT_HARD_GAP)
        if n <= 0:
            return float(NET_REBOOT_MIN_GAP)
        return float(min(NET_REBOOT_MIN_GAP * (2 ** n), NET_REBOOT_MAX_GAP))

    def _note_net_reboot(self) -> None:
        """记录「因网络重启」—— 落持久分区（重启后仍生效），并保留无效计数。"""
        rec = self._net_reboot_record()
        rec["at"] = time.time()
        self._save_net_reboot_record(rec)

    def _verify_last_reboot(self, network_ok: bool) -> None:
        """判定「上次网络重启是否有效」，每次启动只判一次。

        时机：重启后 NET_REBOOT_VERIFY_S（10min）过的第一轮健康检查。
        判据就是「网络自己回来了没有」——A7Z 能直接观察，不需要外部参与。
        有效 → ineffective 清零（有效重启不该累积惩罚）；
        无效 → ineffective+1，冷却按 `_net_reboot_gap` 指数退避。
        """
        if self._reboot_verified:
            return
        rec = self._net_reboot_record()
        at = float(rec.get("at") or 0)
        if at <= 0:
            self._reboot_verified = True
            return
        age = time.time() - at
        if age < NET_REBOOT_VERIFY_S:
            return                      # 还没到判定窗口，下轮再看
        self._reboot_verified = True
        ineff = int(rec.get("ineffective") or 0)
        if network_ok:
            if ineff:
                logger.info("上次网络重启有效（网络已恢复），无效计数 %d → 0", ineff)
            rec["ineffective"] = 0
            self._save_net_reboot_record(rec)
            return
        rec["ineffective"] = ineff + 1
        self._save_net_reboot_record(rec)
        gap = self._net_reboot_gap(rec)
        if rec["ineffective"] >= NET_REBOOT_MAX_INVALID:
            logger.critical("上次网络重启无效（%d 分钟仍无网），已连续 %d 次 → "
                           "进入熔断，%d 小时内不再重启（疑似热点/运营商无网）",
                           int(age / 60), rec["ineffective"], int(gap / 3600))
        else:
            logger.warning("上次网络重启无效（%d 分钟仍无网），连续无效 %d 次 → 冷却升至 %d 分钟",
                           int(age / 60), rec["ineffective"], int(gap / 60))

    def _clear_net_invalid(self) -> None:
        """运行期网络恢复 → 无效计数清零（保留 `at`，30min 防抖仍然有效）。

        没有这一步会留下一个死角：熔断（24h）期间网络自己好了，`ineffective` 仍是上限值，
        下次再断网会继续按 24h 熔断 —— 相当于「一次误判罚一天」。
        """
        rec = self._net_reboot_record()
        n = int(rec.get("ineffective") or 0)
        if n <= 0:
            return
        logger.info("网络已恢复，网络重启无效计数 %d → 0", n)
        rec["ineffective"] = 0
        self._save_net_reboot_record(rec)

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

        # 6. 网络检查（v0.3.1.91：连续不可达才算故障，单次抖动不算）
        network = self.check_network_connectivity()
        self.status["metrics"]["network"] = network
        # v0.3.1.91：顺手判定「上次网络重启是否有效」（每次启动只判一次）
        self._verify_last_reboot(network)
        if not network:
            self._net_fail_streak += 1
            self.status["metrics"]["net_fail_streak"] = self._net_fail_streak
            self.status["issues"].append(
                "网络不可达（连续 %d/%d 次）" % (self._net_fail_streak, NET_FATAL_STREAK))
            if self._net_fail_streak >= NET_FATAL_STREAK:
                allowed, why = self._net_fatal_allowed()
                if allowed:
                    self.status["metrics"]["net_fatal"] = True
                    self.status["issues"].append(
                        "网络连续不可达 %d 次且护栏通过 → 判为致命" % self._net_fail_streak)
                    self.status["healthy"] = False
                else:
                    # 护栏否决：车在开 / guard 快照过期 / 冷却中 —— 都记下来，便于复盘
                    self.status["metrics"]["net_fatal_blocked"] = why
        else:
            if self._net_fail_streak:
                logger.info("网络已恢复（此前连续不可达 %d 次）", self._net_fail_streak)
                # 恢复瞬间清一次无效计数：否则熔断期内网络自己好了，
                # ineffective 仍是 3，下次断网还要白等 24h
                self._clear_net_invalid()
            self._net_fail_streak = 0
            self.status["metrics"]["net_fail_streak"] = 0

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

    def consume_reboot_request(self) -> Optional[str]:
        """消费外部重启请求（v0.3.1.57 / M80，由 usb_guard 写入）。

        返回原因字符串；无请求返回 None。

        无论请求是否有效都会删除该文件 —— 避免陈旧请求在下次开机后被重复消费
        造成重启循环。超过 TTL 的请求视为过期，只删除、不触发。
        """
        path = REBOOT_REQUEST_FILE
        if not os.path.isfile(path):
            return None

        payload = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f) or {}
        except (OSError, IOError, ValueError) as e:
            logger.warning("重启请求文件不可解析(%s)，已忽略并删除", e)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        try:
            req_at = float(payload.get("requested_at") or 0)
        except (TypeError, ValueError):
            req_at = 0
        age = (time.time() - req_at) if req_at else (REBOOT_REQUEST_TTL + 1)
        if age > REBOOT_REQUEST_TTL:
            logger.warning("忽略过期的重启请求（age=%.0fs > %ds）", age, REBOOT_REQUEST_TTL)
            return None

        reason = str(payload.get("reason") or "外部重启请求")
        logger.critical("收到外部重启请求（age=%.0fs）：%s | %s",
                        age, reason, payload.get("detail") or "")
        return reason

    def run_daemon(self, interval: int = 60) -> None:
        """
        以守护进程方式运行看门狗

        Args:
            interval: 健康检查间隔（秒），必须 < 16秒（看门狗超时）
        """
        if interval >= 16:
            logger.warning("健康检查间隔(%ds) 超过看门狗超时(16s)，强制改为5s", interval)
            interval = 5

        # v0.3.1.66 / M107：必须先挂信号处理器（让 systemd 的 SIGTERM 走 magic close），
        # 并且**立刻**打开看门狗 —— 原来的顺序是「先 sleep(30) 再 open」，
        # 于是 30s 空窗 > 16s 硬件超时 ⇒ 重启该服务必然整机复位。
        self.install_signal_handlers()

        logger.info("看门狗守护进程启动，检查间隔: %ds", interval)

        # 启动看门狗（打开 fd，启动定时器）
        watchdog_active = self.start_watchdog()
        if not watchdog_active:
            logger.warning("硬件看门狗不可用，仅进行健康监控")

        # 启动宽限期：只推迟「动作」（不在系统未就绪时判故障），期间**照常喂狗**
        if STARTUP_GRACE_SECONDS > 0:
            logger.info("等待系统完成启动（%d秒，期间持续喂狗）...", STARTUP_GRACE_SECONDS)
            grace_end = time.time() + STARTUP_GRACE_SECONDS
            while True:
                remaining = grace_end - time.time()
                if remaining <= 0:
                    break
                if watchdog_active:
                    self.pet_watchdog()
                time.sleep(min(interval, remaining))

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

                    # v0.3.1.57 / M80：外部重启请求（usb_guard 层级恢复失败）优先处理。
                    # 命中后不再喂狗 → 交由硬件看门狗超时复位。
                    if self.consume_reboot_request():
                        self._want_reboot = True
                        continue

                    status = self.run_health_check()

                    # 关键：检查完成后立即再喂一次狗。
                    # 健康检查含多个子进程（curl/ping/systemctl），可能耗时 >10s，
                    # 若等到 sleep(interval) 之后才喂，累计间隔可能超过 16s 硬件窗口 → 误触发硬件重启。
                    if watchdog_active and not want_reboot:
                        self.pet_watchdog()

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
                            # v0.3.1.91：若是「网络致命」触发，先记 4h 冷却戳——
                            # 否则 MIFI 本身没网时会变成重启风暴（重启后照样不通）
                            if (status.get("metrics") or {}).get("net_fatal"):
                                self._note_net_reboot()
                                logger.critical("本次复位由网络故障触发，已记 4h 冷却戳")
                            # 不喂狗、不关闭看门狗，让硬件看门狗超时触发重启
                            self._want_reboot = True

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
                        self._want_reboot = True

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
