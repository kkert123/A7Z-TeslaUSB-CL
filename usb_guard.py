#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usb_guard.py — USB Gadget 链路守护（独立常驻服务）
==================================================

背景（2026-09-19 实测）
----------------------
触碰/插拔 USB 线后，车机可能读不到 U 盘，且**不会自愈**，必须物理拔插一次
才恢复。实测两次故障：

    ① 16:04:01 – 16:04:57 (57s)   state=not attached
    ② 16:05:51 – 16:08:18 (147s)  state=default

两者共同签名：

    线在位(online=1, 5V) + UDC state != 'configured'

而原 gadget_health.py 只覆盖「UDC 完全未绑定」这一种情况，对「已绑定但链路
卡住」（state=default / not attached）完全落在盲区 → 全程零日志零动作。

判据（09-19 探针锁定）
----------------------
- 线在位: /sys/class/power_supply/tcpm-source-psy-14-004e/online  (1=在位 / 0=离位)
- 健康:   /sys/class/udc/<udc>/state == 'configured'

*注意1* current_speed **不能**单独当健康判据 —— 故障②卡死时它仍显示
        super-speed-plus，看着完全正常。
*注意2* 正常枚举瞬态实测仅 1–4s（16:05:04→05、16:05:32→33、16:08:18→22），
        故 15s 确认阈值留足余量，拔线/轻触抖动都不会误触发。

安全边界（五条）
----------------
1. 拔线闸: online=0 → 一律不动作（专治"拔线触发无限制重启、一夜约 160 次"）
2. 熔断:   **仅 L2/L3 这类重动作**计数——breaker_window 内重动作达上限
           （MAX_HEAVY_ACTIONS_PER_WINDOW）后，新故障期只记录不动作。
           ① L1（无损 soft rebind）不计数，可反复修复轻量故障
              （2026-09-20 离线回放实证：L1 若也计数，同窗口第二次故障被完全剥夺）；
           ② 上限为 3 而非 1——连续两次故障都应被救；真正的重启环（一夜数百次）
              仍会被卡住（2026-09-20 回放场景A 实证：上限 1 会让第二次故障零动作）
3. 同故障期内 L1/L2/L3 各最多一次，恢复即清零（只升不重来）
4. 所有 gadget 写操作在 /var/run/usb_gadget_init.lock 全局锁内（M74 防抢绑）
5. DRY-RUN: 环境变量 USB_GUARD_DRY_RUN=1 时只判定不动作

处置阶梯（从故障起点计时）
--------------------------
    t >= confirm_l1 → L1 soft_connect 循环（不可用则 UDC 重绑回原控制器）
    t >= confirm_l2 → L2 usb_gadget_init.sh restart（完整兜底）
    t >= confirm_l3 → L3 告警 + 写重启请求 → 硬件看门狗复位 A7Z

回滚
----
    systemctl disable --now teslausb-usb-guard.service
"""
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [USBGuard] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout,
)
logger = logging.getLogger("USBGuard")

# ── 路径常量 ──
GADGET_NAME = "tesla_usb"
GADGET_DIR = "/sys/kernel/config/usb_gadget/" + GADGET_NAME
UDC_FILE = GADGET_DIR + "/UDC"
UDC_CLASS_DIR = "/sys/class/udc"
GADGET_INIT_SCRIPT = "/opt/radxa_data/usb_gadget_init.sh"
LOCK_FILE = "/var/run/usb_gadget_init.lock"

# 线在位判据：ET7304 (i2c 14-004e) 的 TCPM 电源节点。
# online=1/voltage=5000000 表示车机侧 VBUS 在位；online=0/voltage=0 表示线离位。
PSU_DIR = "/sys/class/power_supply/tcpm-source-psy-14-004e"
PSU_ONLINE = PSU_DIR + "/online"
PSU_VOLTAGE = PSU_DIR + "/voltage_now"

SENTRY_CONFIG = "/opt/radxa_data/teslausb/config/sentry.json"
DATA_DIR = "/opt/radxa_data/teslausb/data"
STATUS_FILE = DATA_DIR + "/usb_guard_status.json"
REBOOT_REQUEST_FILE = DATA_DIR + "/usb_guard_reboot_request.json"

# 重启请求有效期（秒）：看门狗只认这个窗口内的请求，避免陈旧文件在重启后被消费。
REBOOT_REQUEST_TTL = 600

# 配置默认值（与 templates/system.html + config/sentry.json 的 usb_guard 段一致）
DEFAULT_CFG = {
    "enabled": True,
    "poll_interval_seconds": 3,
    "confirm_l1_seconds": 15,
    "confirm_l2_seconds": 30,
    "confirm_l3_seconds": 90,
    "breaker_window_seconds": 1800,
    "push_cooldown_seconds": 30,
}
# 数值键的合法区间（与 routes/system_routes.py 的 USB_GUARD_RANGES 保持一致）
CFG_RANGES = {
    "poll_interval_seconds": (1, 60),
    "confirm_l1_seconds": (5, 600),
    "confirm_l2_seconds": (6, 900),
    "confirm_l3_seconds": (7, 1800),
    "breaker_window_seconds": (60, 86400),
    "push_cooldown_seconds": (1, 3600),
}
CFG_RELOAD_INTERVAL = 60  # 秒，定期重读配置，UI 改动免重启生效

# 重动作阶梯集合：只有这些计入熔断（L1 无损，不计数）。
HEAVY_STAGES = ("L2", "L3")
# 熔断上限：breaker_window 内允许的重动作次数，超过才静默。
# 取 3 而非 1 —— 连续两次故障都应被救；真正的重启环（一夜数百次）仍被卡住。
MAX_HEAVY_ACTIONS_PER_WINDOW = 3

DRY_RUN = os.environ.get("USB_GUARD_DRY_RUN", "") == "1"


# ═══════════════════════════════════════════════════════════
# 基础读取
# ═══════════════════════════════════════════════════════════
def _read_text(path: str, limit: int = 256) -> str:
    """读取 sysfs/普通文本文件，失败/不存在返回空串（绝不抛异常）。"""
    try:
        with open(path, "r") as f:
            return f.read(limit).strip()
    except (OSError, IOError):
        return ""


def _load_cfg() -> dict:
    """读取 sentry.json 的 usb_guard 段，缺失/非法键回落到默认值。"""
    cfg = dict(DEFAULT_CFG)
    try:
        with open(SENTRY_CONFIG, "r", encoding="utf-8") as f:
            raw = json.load(f)
        sect = raw.get("usb_guard") or {}
        if isinstance(sect, dict):
            for k, default in DEFAULT_CFG.items():
                v = sect.get(k, default)
                if isinstance(default, bool):
                    cfg[k] = bool(v)
                else:
                    lo, hi = CFG_RANGES.get(k, (1, 10 ** 9))
                    try:
                        cfg[k] = min(max(int(v), lo), hi)
                    except (TypeError, ValueError):
                        cfg[k] = default
    except (OSError, IOError, ValueError):
        pass
    # 阶梯必须单调递增，否则修正（防 UI 填错导致 L2 早于 L1）
    l1 = cfg["confirm_l1_seconds"]
    cfg["confirm_l2_seconds"] = max(cfg["confirm_l2_seconds"], l1 + 1)
    cfg["confirm_l3_seconds"] = max(cfg["confirm_l3_seconds"], cfg["confirm_l2_seconds"] + 1)
    return cfg


def _pick_udc() -> str:
    """挑一个 UDC 控制器名，优先 *xhci*（车机连的那根，与 usb_gadget_init.sh/D2 对齐）。"""
    try:
        names = os.listdir(UDC_CLASS_DIR)
    except OSError:
        return ""
    if not names:
        return ""
    xhci = [u for u in names if "xhci" in u]
    other = [u for u in names if "xhci" not in u]
    return (xhci + other)[0]


# online 不可读告警节流（模块级；5 分钟内只提醒一次，避免每 3s 刷屏）
_ONLINE_WARN_AT = [0.0]


def probe() -> dict:
    """只读采样当前链路状态。返回 dict，绝不抛异常。"""
    udc = _read_text(UDC_FILE)
    state = _read_text("%s/%s/state" % (UDC_CLASS_DIR, udc)) if udc else ""
    online = _read_text(PSU_ONLINE)
    voltage = _read_text(PSU_VOLTAGE)

    if online in ("0", "1"):
        cable = online == "1"
        cable_src = "online"
    else:
        # online 节点不可读 → 保守判「线离位」、一律不动作（拔线闸安全边界）。
        # 宁可漏动作也不误动作：拔线触发无限制重启正是本守护要消灭的原始故障。
        # 告警节流（5min）以便发现电源节点异常导致的「守护静默失效」。
        cable = False
        cable_src = "online_unreadable"
        _now = time.time()
        if _now - _ONLINE_WARN_AT[0] > 300:
            _ONLINE_WARN_AT[0] = _now
            logger.warning("online 节点不可读（%s），保守判定线离位、不动作", PSU_ONLINE)

    return {
        "udc": udc,
        "state": state,
        "online": online,
        "voltage": voltage,
        "cable": cable,
        "cable_src": cable_src,
        "healthy": bool(udc) and state == "configured",
        "gadget_ok": os.path.isdir(GADGET_DIR),
    }


# ═══════════════════════════════════════════════════════════
# 全局锁与 gadget 写操作
# ═══════════════════════════════════════════════════════════
def _op_l1(target: str) -> tuple:
    """
    L1 处置：最轻量的"模拟拔插"。
    先试 soft_connect 循环（只断/接 UDC，不动 configfs）；不可用则解绑再绑回原控制器。
    调用方须已持有全局锁。
    返回 (成功, 说明)。
    """
    if not target:
        return False, "无可绑定的 UDC 控制器"

    sc_path = "%s/%s/soft_connect" % (UDC_CLASS_DIR, target)
    sc_err = ""
    if os.path.exists(sc_path):
        try:
            with open(sc_path, "w") as f:
                f.write("disconnect")
            time.sleep(2)
            with open(sc_path, "w") as f:
                f.write("connect")
            return True, "soft_connect 循环 (%s)" % target
        except (OSError, IOError) as e:
            sc_err = str(e)

    # 回退：UDC 解绑 → 重新绑回原控制器（不卸载分区，不 fsck）
    try:
        with open(UDC_FILE, "w") as f:
            f.write("\n")
        time.sleep(2)
        with open(UDC_FILE, "w") as f:
            f.write(target)
    except (OSError, IOError) as e:
        return False, "UDC 重绑异常: %s" % e

    bound = _read_text(UDC_FILE)
    if bound == target:
        note = "UDC 重绑回 %s" % target
        if sc_err:
            note += "（soft_connect 不可用: %s）" % sc_err
        return True, note
    return False, "UDC 重绑验证失败: 期望 %s 实际 %s" % (target, bound or "(空)")


def _with_gadget_lock(fn, *args, **kwargs):
    """
    在 /var/run/usb_gadget_init.lock 全局锁内执行 gadget 写操作（M74 防并发抢绑）。
    拿不到锁 → 返回 None（本轮让行，绝不抢绑）。
    锁文件不可用 → fail-open 照常执行（与 usb_gadget_init.sh 的失败开放策略一致，
    避免"锁不可用 → 永不恢复"的连带故障）。
    """
    fd = None
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        logger.warning("全局锁不可用(%s)，fail-open 不加锁执行", e)
        return fn(*args, **kwargs)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.info("未取得 gadget 全局锁（另有实例在操作），本轮让行")
            return None
        return fn(*args, **kwargs)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _run_l2_restart() -> tuple:
    """
    L2 处置：完整 restart（stop → sleep 2 → start）。

    *注意* 这里**故意不持外层锁**：usb_gadget_init.sh 内部自带 flock -n，
    若外层已持锁，脚本会判定"另一实例在跑"而直接 exit 0 —— 看起来成功、
    实际什么都没做。故必须让脚本自己去拿锁。
    """
    if not os.path.isfile(GADGET_INIT_SCRIPT):
        return False, "初始化脚本不存在: %s" % GADGET_INIT_SCRIPT
    try:
        r = subprocess.run(
            ["/bin/bash", GADGET_INIT_SCRIPT, "restart"],
            capture_output=True, timeout=90, text=True,
        )
        if r.returncode == 0:
            return True, "usb_gadget_init.sh restart"
        return False, "restart rc=%d %s" % (r.returncode, (r.stderr or "")[:160])
    except subprocess.TimeoutExpired:
        return False, "restart 超时 (>90s)"
    except Exception as e:
        return False, "restart 异常: %s" % e


def _write_json_atomic(path: str, payload: dict) -> bool:
    """原子写 JSON（tmp + os.replace），避免看门狗读到半截文件。"""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, IOError, TypeError, ValueError) as e:
        logger.error("写 JSON 失败 %s: %s", path, e)
        return False


# ═══════════════════════════════════════════════════════════
# 守护主体
# ═══════════════════════════════════════════════════════════
class UsbGuard(object):
    """USB Gadget 链路守护状态机。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cfg_loaded_at = time.time()
        self.fault_since = None       # 本次故障期起点（None = 无故障）
        self.stages_done = set()      # 本故障期已执行的阶梯 {L1,L2,L3}
        self.heavy_action_times = []  # 窗口内重动作时刻列表（L2/L3；L1 不计数）
        self.last_push_at = 0.0       # 上次告警推送时刻（冷却用）
        self.last_state = ""          # 上一轮 state，用于变化日志
        self.episode_suppressed = False  # 本故障期是否因熔断被抑制

    # ── 告警推送（带冷却） ──
    def _push(self, text: str) -> None:
        cd = self.cfg["push_cooldown_seconds"]
        now = time.time()
        if now - self.last_push_at < cd:
            logger.info("告警冷却中（%.0fs < %ds），跳过推送", now - self.last_push_at, cd)
            return
        self.last_push_at = now
        if DRY_RUN:
            logger.info("[DRY-RUN] 本应推送: %s", text)
            return
        try:
            from weixin_notifier import WeixinNotifier
            ok = WeixinNotifier(bot_name="系统通知").send_text(text)
            logger.info("告警推送%s", "成功" if ok else "失败")
        except Exception as e:
            logger.warning("告警推送异常: %s", e)

    # ── 熔断判定（按"窗口内重动作次数"计，而非"距上次动作时长"）──
    def _prune_heavy(self, now: float) -> None:
        """丢掉滑出窗口的重动作记录。"""
        win = self.cfg["breaker_window_seconds"]
        self.heavy_action_times = [t for t in self.heavy_action_times if t >= now - win]

    def _breaker_open(self, now: float) -> bool:
        """熔断是否生效——窗口内重动作（L2/L3）次数达上限才生效。
        L1（无损 soft rebind）不计数，可反复修复轻量故障。"""
        self._prune_heavy(now)
        return len(self.heavy_action_times) >= MAX_HEAVY_ACTIONS_PER_WINDOW

    # ── L1 / L2 / L3 ──
    # 返回值约定：True=已实际执行（计入本故障期阶梯 + 重动作计数）；
    #            False=本轮未执行（让行/写失败），不计入、下一 tick 重试。
    def _do_l1(self, snap: dict) -> bool:
        target = snap["udc"] or _pick_udc()
        logger.warning("L1 触发：轻量重绑（目标 UDC=%s, state=%s）",
                       target or "(无)", snap["state"] or "(空)")
        if DRY_RUN:
            logger.info("[DRY-RUN] 本应执行 L1：soft_connect 循环 / UDC 重绑")
            return True
        res = _with_gadget_lock(_op_l1, target)
        if res is None:
            logger.warning("L1 未取得全局锁（另有实例在操作），本轮让行、下轮重试")
            return False
        ok, note = res
        logger.warning("L1 结果：%s — %s", "成功" if ok else "失败", note)
        return True

    def _do_l2(self, snap: dict) -> bool:
        logger.warning("L2 触发：完整 restart gadget")
        if DRY_RUN:
            logger.info("[DRY-RUN] 本应执行 L2：%s restart", GADGET_INIT_SCRIPT)
            return True
        ok, note = _run_l2_restart()
        logger.warning("L2 结果：%s — %s", "成功" if ok else "失败", note)
        return True

    def _do_l3(self, snap: dict) -> bool:
        logger.critical("L3 触发：L1/L2 均未恢复链路，请求看门狗硬件复位 A7Z")
        detail = "udc=%s state=%s online=%s voltage=%s" % (
            snap["udc"] or "(无)", snap["state"] or "(空)",
            snap["online"] or "(无)", snap["voltage"] or "(无)")
        if DRY_RUN:
            logger.info("[DRY-RUN] 本应写重启请求 + 告警：%s", detail)
            return True
        wrote = _write_json_atomic(REBOOT_REQUEST_FILE, {
            "requested_at": time.time(),
            "requested_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "usb_guard: 线在位但 UDC 链路持续未 configured，L1/L2 无效",
            "detail": detail,
        })
        if not wrote:
            logger.error("重启请求写入失败，本轮不计入、下轮重试")
            return False
        self._push(
            "【TeslaUSB】USB 存储链路卡死且软件层恢复无效，已请求重启设备。\n"
            "现象：线在位但车机识别不到 U 盘（%s）。\n"
            "已尝试：UDC 重绑 → gadget restart，均未恢复。" % detail)
        return True

    # ── 故障期管理 ──
    def _reset_fault(self, snap: dict) -> None:
        if self.fault_since is not None:
            dur = time.time() - self.fault_since
            if snap["healthy"]:
                logger.info("链路恢复正常（故障持续 %.0fs，已执行阶梯: %s）",
                            dur, ",".join(sorted(self.stages_done)) or "无")
            else:
                logger.info("线已离位，故障期清零（持续 %.0fs）", dur)
        self.fault_since = None
        self.stages_done = set()
        self.episode_suppressed = False

    def tick(self) -> None:
        # 定期重载配置（UI 改动免重启生效）
        now = time.time()
        if now - self.cfg_loaded_at >= CFG_RELOAD_INTERVAL:
            self.cfg = _load_cfg()
            self.cfg_loaded_at = now

        snap = probe()

        # 状态变化时留痕（web 日志里能直接看到链路抖动）
        if snap["state"] != self.last_state:
            logger.info("UDC state: %s → %s（cable=%s/%s online=%s）",
                        self.last_state or "(空)", snap["state"] or "(空)",
                        snap["cable"], snap["cable_src"], snap["online"] or "(无)")
            self.last_state = snap["state"]

        if not self.cfg["enabled"]:
            return

        # 拔线闸 / 健康 → 一律不动作
        if (not snap["cable"]) or snap["healthy"]:
            self._reset_fault(snap)
            self._write_status(snap, "normal", 0.0)
            return

        # ── 进入故障期 ──
        now = time.time()
        if self.fault_since is None:
            self.fault_since = now
            self.stages_done = set()
            if self._breaker_open(now):
                self.episode_suppressed = True
                logger.warning(
                    "检测到故障（线在位但 state=%s），但熔断生效中"
                    "（窗口 %ds 内重动作已达 %d/%d 次）——本期只记录不动作",
                    snap["state"] or "(空)", self.cfg["breaker_window_seconds"],
                    len(self.heavy_action_times), MAX_HEAVY_ACTIONS_PER_WINDOW)
            else:
                logger.warning(
                    "检测到故障：线在位(online=%s) 但 UDC state=%s（udc=%s, gadget_ok=%s），"
                    "进入确认期（L1@%ds / L2@%ds / L3@%ds）",
                    snap["online"] or "(无)", snap["state"] or "(空)",
                    snap["udc"] or "(未绑定)", snap["gadget_ok"],
                    self.cfg["confirm_l1_seconds"], self.cfg["confirm_l2_seconds"],
                    self.cfg["confirm_l3_seconds"])

        elapsed = now - self.fault_since
        self._write_status(snap, "fault", elapsed)

        if self.episode_suppressed:
            return

        # 阶梯：到点且本故障期未执行过 → 执行一次（熔断在动作前复查）
        ladder = (
            ("L1", self.cfg["confirm_l1_seconds"], self._do_l1),
            ("L2", self.cfg["confirm_l2_seconds"], self._do_l2),
            ("L3", self.cfg["confirm_l3_seconds"], self._do_l3),
        )
        for name, threshold, action in ladder:
            if elapsed < threshold or name in self.stages_done:
                continue
            if action(snap) is False:
                # 让行（未取得全局锁）或写失败：本轮不计入已执行、不计重动作次数，
                # 下一 tick 重试同一级——绝不因"没做过的事"提前升到更重的 L2/L3。
                break
            self.stages_done.add(name)
            if name in HEAVY_STAGES:
                # 只有重动作计数：L1 无损可反复做；窗口内允许 MAX 次重动作，
                # 超限才静默（防重启环，但不再"一次就锁死整个窗口"）。
                self.heavy_action_times.append(time.time())
            break  # 一轮只升一级，下一 tick 再评估

    # ── 状态落盘（供 /system 页与排障） ──
    def _write_status(self, snap: dict, phase: str, elapsed: float) -> None:
        _now = time.time()
        self._prune_heavy(_now)
        _write_json_atomic(STATUS_FILE, {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "phase": phase,
            "cable": snap["cable"],
            "cable_source": snap["cable_src"],
            "online": snap["online"],
            "voltage": snap["voltage"],
            "udc": snap["udc"],
            "state": snap["state"],
            "healthy": snap["healthy"],
            "gadget_ok": snap["gadget_ok"],
            "fault_elapsed_seconds": round(elapsed, 1),
            "stages_done": sorted(self.stages_done),
            "breaker_open": self._breaker_open(_now),
            "heavy_actions_in_window": len(self.heavy_action_times),
            "max_heavy_actions_per_window": MAX_HEAVY_ACTIONS_PER_WINDOW,
            "dry_run": DRY_RUN,
        })

    def run_forever(self) -> None:
        interval = self.cfg["poll_interval_seconds"]
        logger.info("USB Guard 启动（interval=%ds, DRY_RUN=%s, 判据: online + state==configured）",
                    interval, DRY_RUN)
        logger.info("配置: %s", json.dumps(self.cfg, ensure_ascii=False))
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.error("守护循环异常: %s", e, exc_info=True)
            time.sleep(self.cfg["poll_interval_seconds"])


def main() -> int:
    cfg = _load_cfg()
    if not cfg["enabled"]:
        logger.warning("usb_guard 配置为 disabled，仍启动但只做只读监测（不动作）")
    guard = UsbGuard(cfg)
    try:
        guard.run_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
