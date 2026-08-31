#!/usr/bin/env python3
"""
cache_coherency.py — TeslaCam 只读挂载的 VFS 缓存一致性修复

== 问题根因（货不对板的真正原因）==
  Present 模式下，/dev/nvme0n1p2 同时被两处使用：
    1. USB Gadget 把它作为「可写 LUN」绑定给特斯拉实时写入；
    2. 本地以只读方式挂载在 /mnt/teslacam，供 Web 播放 / 缩略图 / SCP 读取。
  Tesla 通过 Gadget 写入后，本地 ro 挂载的 VFS dentry/inode 缓存
  （文件名 → 簇的映射）不会自动失效。当特斯拉回收 RecentClips 文件名
  并重写内容时，本地读取仍命中「旧簇」→ 视频字节货不对板。

  Edit 模式因为拆除了 Gadget 并对分区重新 rw 挂载（+fsck），缓存从
  磁盘重建，所以内容正确。两种模式读的是同一块盘，差异纯粹来自
  「只读挂载的 VFS 缓存是否过期」。

== 修复策略（v0.3.1.31 重构：从"周期暴力"到"读取驱动"）==
  原实现每 30s 无条件 drop_caches=2（系统级清空所有 dentry/inode 缓存），
  实测 3332 次/天 → 全系统 IO 重走磁盘 → 8-28 系统僵死事故。

  重构后两条路径（严格遵守 Mount Safety：禁止 umount/remount，仅 drop_caches=2）：
    1. 主保障（读取驱动 ensure_fresh）：所有 teslacam 读取入口（缩略图/
       播放/SEI/列表）在读取前调用 ensure_fresh() —— 30s TTL 节流 + listdir
       指纹检测（文件数+最新文件名；8-30 实测 stat mtime 被 inode 缓存冻结
       不可用，listdir 因 dentry miss 强制读盘可实时感知特斯拉新文件）。
       读取时刻 = 刷新时刻 → 用户看到的必然最新（满足
       "任何时刻读取都最新"的原始需求，且优于原周期方案的 30s 滞后窗口）。
    2. 兜底（后台 60s 检测）：后台线程每 60s listdir 指纹检测 RecentClips，
       车机在写（指纹变化）才刷，无写入时零开销（仅一次 listdir），
       覆盖漏接 ensure_fresh 的读取入口（如后台缩略图扫描）。

  开销对比（业务堆积的根治）：
    - 车机写 + 有人读：读取时刷一次（30s 节流）≈ 原方案
    - 车机写 + 无人读：60s 指纹检测，变化才刷（原方案每 30s 全刷）
    - 车机不写：零 drop_caches（原方案每 30s 全刷 → 3332 次/天风暴消除）
"""

import os
import time
import logging
import threading
import subprocess
from typing import Optional

logger = logging.getLogger("CacheCoherency")

# 模式标志文件：present / edit
MODE_FILE = "/tmp/teslausb_mode"

# RecentClips 目录（车机循环写入，文件名被回收 → 货不对板高危区）
RECENTCLIPS_DIR = "/mnt/teslacam/TeslaCam/RecentClips"

# Present 模式下刷新 VFS 缓存的节流窗口（秒）。
# 30s 内只允许一次 drop_caches：读取连发/后台兜底高频触发时防抖。
ENSURE_TTL_SEC = 30

# S3 兜底：指纹检测失效场景（如「同名文件覆盖重写」O_TRUNC，文件名集合
# 不变）的最长不刷新窗口。超过该时长即使指纹未变也强制刷一次。
# 8-30 实测：Present 模式 stat 目录 mtime 被 inode 缓存冻结，指纹已改用
# listdir（可靠），此兜底仅覆盖指纹感知不到的罕见场景。
S3_FORCE_TTL_SEC = 120

# v0.3.1.39（A1 延迟刷新）：车机写入静止判定窗口（秒）。
# 背景：v0.3.1.35 listdir 指纹实时感知车机写入 → 车机写哨兵期间每 30s 触发
# 一次全局 drop_caches=2（实证 refresh_count=338）→ 959MB 小内存系统缓存回收
# + 本地读重新落盘 → NVMe/USB gadget 写路径抖动 → 诱发 dwc3 ep1out 端点
# 禁用 → 车机 UI_a112「USB设备故障 - I/O错误」（8-31 诊断实证）。
# 修复：检测到车机写入仅标记 dirty（不立即刷），等车机停止写入 ≥ 该时长后
# 才执行 drop_caches。读取驱动 ensure_fresh 仍保证「读取时刻 = 刷新时刻」
# （货不对板修复不回归）——车机停写后任意读取会触发刷新，读到最新完整文件。
WRITE_STABLE_SEC = 60

# 后台兜底检测间隔（秒）：仅 listdir 指纹检测，车机在写才刷，无写入零开销。
DEFAULT_REFRESH_INTERVAL = 60

# ── 可观测性状态（供 /api/system/cache-coherency 上报，确认任务真实运行）──
_state = {
    "running": False,
    "present_mode": False,
    "last_refresh_ts": 0.0,   # 最近一次成功刷新缓存的时间戳
    "refresh_count": 0,       # 累计成功刷新次数
    "last_success": False,    # 最近一次刷新是否成功
    "last_error": "",         # 最近一次失败原因
    "ensure_ttl": ENSURE_TTL_SEC,
}
_state_lock = threading.Lock()

# ensure_fresh 节流状态（TTL + listdir 指纹 + v0.3.1.39 dirty 延迟刷新）
_ensure_lock = threading.Lock()
_ensure_state = {
    "last_ts": 0.0,
    "last_fingerprint": None,
    "dirty": False,        # v0.3.1.39: 检测到车机写入但尚未刷新（延迟窗口内）
    "last_write_ts": 0.0,  # v0.3.1.39: 最近一次检测到车机写入的时间
}


def get_coherency_status() -> dict:
    """返回缓存一致性任务的实时状态，供 HTTP 接口上报。"""
    with _state_lock:
        s = dict(_state)
    s["interval"] = DEFAULT_REFRESH_INTERVAL
    return s


def _is_present_mode() -> bool:
    """当前是否处于 Present 模式（连接特斯拉、Gadget 可写 + 本地只读挂载）。

    优先读取模式标志文件；文件缺失/为空时回退到挂载状态检测，
    与 routes/misc_routes.get_mode_status 的兜底逻辑保持一致：
    /mnt/teslacam 以只读挂载即为 Present 模式（Edit 模式为 rw 挂载）。
    """
    try:
        if os.path.exists(MODE_FILE):
            with open(MODE_FILE, "r") as f:
                mode = f.read().strip()
            if mode == "present":
                return True
            if mode == "edit":
                return False
    except Exception:
        pass
    # 回退：解析 /proc/mounts，/mnt/teslacam 只读挂载即 Present 模式
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "/mnt/teslacam":
                    opts = parts[3].split(",")
                    if "ro" in opts:
                        return True
    except Exception:
        pass
    return False


def drop_vfs_caches() -> bool:
    """丢弃内核 dentry/inode 缓存，强制只读挂载重新解析目录项。

    优先直接写 /proc（web 以 root 运行时）；失败则回退 sudo -n。
    返回是否成功；失败仅记录日志，不影响主服务。
    """
    # 方法 1：直接写（web 以 root 身份运行时）
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("2\n")
        logger.debug("已刷新 VFS 缓存 (drop_caches=2)")
        return True
    except Exception:
        pass

    # 方法 2：回退 sudo -n（无密码 sudo 已用于模式切换）
    try:
        result = subprocess.run(
            ["sudo", "-n", "bash", "-c", "echo 2 > /proc/sys/vm/drop_caches"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.debug("已刷新 VFS 缓存 (sudo drop_caches=2)")
            return True
        logger.warning("刷新 VFS 缓存失败: %s", (result.stderr or result.stdout).strip())
    except Exception as e:
        logger.warning("刷新 VFS 缓存异常: %s", e)
    return False


def _dir_fingerprint(path: str):
    """目录写入指纹：文件数 + 最新文件名。

    8-30 实测（特斯拉活跃写入窗口）：
      - stat 目录 mtime 走 VFS inode 缓存 → 被 drop_caches 失效的对象 → 写入后
        不失效 → 永远返回旧值（mtime 指纹检测"鸡生蛋"失效，货不对板复燃根因）
      - listdir 因新文件名的 dentry miss 会强制读盘 → 能实时感知特斯拉新文件
    特斯拉 RecentClips 每次录制产生新文件名（时间戳），故用
    「文件数 + 最新文件名」作指纹，变化即代表有新写入。

    返回 (文件数, 最新文件名)；目录不可读时返回 None（调用方保守触发刷新）。
    """
    try:
        files = sorted(f for f in os.listdir(path) if f.lower().endswith('.mp4'))
        return (len(files), files[-1]) if files else (0, "")
    except Exception:
        return None


def ensure_fresh(path: Optional[str] = None) -> bool:
    """读取前调用：确保目标路径（默认 RecentClips）内容最新。

    主保障（读取驱动）：
      - 30s TTL 节流：距上次刷新不足 30s 直接跳过（缓存仍新鲜）
      - listdir 指纹：文件集合未变（车机未写）→ 跳过，零开销
      - v0.3.1.39（A1 延迟刷新）：检测到车机写入（指纹变化）→ 仅标记 dirty
        并记录 last_write_ts，**不立即 drop_caches**；待车机停止写入 ≥
        WRITE_STABLE_SEC（60s）后才执行刷新。消除「车机写哨兵期间每 30s
        全局 drop_caches」→ 系统 IO 抖动 → dwc3 ep1out 端点异常 → UI_a112
        的诱发链。
        新鲜度取舍（已明确接受）：车机停写后 60s 内读取拿到的是上一版完整
        文件（避免读半截新文件）；停写 ≥60s 后任意读取/后台兜底触发刷新，
        读到最新完整文件。车机持续写入期间不刷新（dirty 恒真屏蔽 S3 兜底），
        陈旧度由读取方容忍——写期间读旧缓存优于读半截 + 系统抖动。
      - S3 兜底：非 dirty 状态下距上次刷新 >120s 强制刷一次
        （覆盖「同名覆盖重写」等指纹感知不到的罕见场景）
      - 仅 Present 模式生效（Edit 模式 rw 挂载、缓存随磁盘重建，无需刷）
    返回是否执行了刷新（供调用方/日志参考）。
    """
    global _ensure_state
    if not _is_present_mode():
        return False

    target = path or RECENTCLIPS_DIR
    now = time.time()
    with _ensure_lock:
        # 30s TTL：节流窗口内不重复刷
        if now - _ensure_state["last_ts"] < ENSURE_TTL_SEC:
            return False
        fp = _dir_fingerprint(target)
        # 目录不可读 → 保守立即刷（维持原行为）
        if fp is None:
            return _do_refresh(fp)
        # 首跑（启动后第一次调用）：直接刷一次建立基准指纹，
        # 避免 last_fingerprint=None 被误判为"写入"而多等 60s 延迟窗口
        if _ensure_state["last_ts"] == 0:
            return _do_refresh(fp)
        if fp != _ensure_state["last_fingerprint"]:
            # 检测到车机写入：标记 dirty + 记录时间，不立即刷（A1 延迟）
            _ensure_state["dirty"] = True
            _ensure_state["last_write_ts"] = now
            _ensure_state["last_fingerprint"] = fp
            logger.debug("检测到车机写入，标记待刷（延迟窗口 %ds）", WRITE_STABLE_SEC)
            return False
        # 指纹未变：
        if _ensure_state["dirty"]:
            if (now - _ensure_state["last_write_ts"]) >= WRITE_STABLE_SEC:
                # 车机已静止 ≥60s → 执行刷新
                return _do_refresh(fp)
            # 车机仍在写（或刚停不足窗口）→ 继续等待，不刷
            return False
        # 非 dirty：S3 兜底（距上次刷新 >120s 强制刷一次）
        if _ensure_state["last_ts"] > 0 \
                and (now - _ensure_state["last_ts"]) >= S3_FORCE_TTL_SEC:
            return _do_refresh(fp)
        return False


def _do_refresh(fp) -> bool:
    """执行一次实际刷新（drop_caches + 元数据缓存失效 + 状态更新）。"""
    if not drop_vfs_caches():
        _record_error("drop_vfs_caches 返回失败")
        return False
    # 同时让视频扫描元数据缓存失效，避免事件列表陈旧
    _invalidate_video_scan_cache()
    now = time.time()
    _ensure_state["last_ts"] = now
    _ensure_state["last_fingerprint"] = fp
    _ensure_state["dirty"] = False
    _record_success()
    return True


def _coherency_loop(interval: int):
    """后台兜底循环：60s 检测 RecentClips mtime，车机在写才刷（覆盖漏接入口）。"""
    with _state_lock:
        _state["running"] = True

    while True:
        try:
            present = _is_present_mode()
            with _state_lock:
                _state["present_mode"] = present
            if present:
                # ensure_fresh 内部自带 30s TTL + listdir 指纹：无写入时仅一次
                # listdir，零开销
                ensure_fresh()
        except Exception as e:
            _record_error(str(e))
            logger.warning("缓存一致性任务异常: %s", e)
        time.sleep(interval)


def _record_success():
    with _state_lock:
        _state["last_refresh_ts"] = time.time()
        _state["refresh_count"] += 1
        _state["last_success"] = True
        _state["last_error"] = ""


def _record_error(msg: str):
    with _state_lock:
        _state["last_success"] = False
        _state["last_error"] = msg


def _invalidate_video_scan_cache():
    """使视频扫描元数据缓存失效（复用 app_helpers 已有函数）。"""
    try:
        from utils.app_helpers import _invalidate_video_cache
        _invalidate_video_cache()
    except Exception:
        # 缓存失效失败不影响主流程，仅可能导致列表短暂陈旧
        pass


def start_cache_coherency_task(interval: int = DEFAULT_REFRESH_INTERVAL):
    """启动缓存一致性后台任务（守护线程，随 teslausb-web 重启生效）。

    注意：interval 现为「后台兜底检测间隔」（默认 60s），仅 stat mtime，
    车机在写才刷。真正的读取即时性由各读取入口调用 ensure_fresh() 保证。
    """
    if interval and interval > 0:
        global DEFAULT_REFRESH_INTERVAL
        DEFAULT_REFRESH_INTERVAL = interval
    t = threading.Thread(
        target=_coherency_loop,
        args=(DEFAULT_REFRESH_INTERVAL,),
        name="CacheCoherency",
        daemon=True,
    )
    t.start()
    logger.info("缓存一致性后台兜底已启动 (检测间隔 %ds, 仅 Present 模式生效)", DEFAULT_REFRESH_INTERVAL)
