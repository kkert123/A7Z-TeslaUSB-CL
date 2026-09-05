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
       v0.3.1.40：后台兜底固定走 A1 延迟刷新（background=True，静止 ≥60s
       才刷）——无人浏览时零 drop_caches（UI_a112 防护）；前台读取入口
       （video_routes 等）默认 background=False 检测到写入立即刷
       （读取时刻=刷新时刻，v0.3.1.39 缓存冻结回归修复）。

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
# 修复（v0.3.1.39）：检测到车机写入仅标记 dirty（不立即刷），等车机停止写入
# ≥ 该时长后才执行 drop_caches。
# v0.3.1.40 分流：该延迟窗口仅适用于【后台兜底】（background=True，无人浏览
# 时零 drop → UI_a112 防护）；【前台读取入口】（background=False，video_routes
# 列表/播放/缩略图等用户操作）检测到写入立即刷 —— 恢复「读取时刻 = 刷新时刻」，
# 修复 v0.3.1.39 把前台一并延迟导致的缓存冻结回归（9-2 22:25 无缩略图+播放失败）。
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


def ensure_fresh(path: Optional[str] = None, background: bool = False, force: bool = False) -> bool:
    """读取前调用：确保目标路径（默认 RecentClips）内容最新。

    v0.3.1.40 双语义（修复 v0.3.1.39 A1 把前台读取一并延迟导致的缓存冻结
    回归 —— 9-2 22:25 实证 13.6h 不刷 → 无缩略图 + 播放失败）：

    - background=False（默认，前台读取入口：video_routes 列表/播放/流/缩略图/
      下载等用户操作）：
      **检测到车机写入（指纹变化）→ 立即 drop_caches**，恢复「读取时刻 =
      刷新时刻」语义——用户看到的必然当前盘上内容，绝不因冻结读到已被车机
      回收重写的旧文件。30s TTL 防抖仍生效（连发读取不重复刷）。
      dirty 悬挂时前台读取同样立即刷（用户主动操作优先于后台延迟窗口）。
    - background=True（后台兜底 _coherency_loop 调用）：
      维持 v0.3.1.39 A1 延迟刷新——写入仅标记 dirty，静止 ≥ WRITE_STABLE_SEC
      （60s）才刷 → 无人浏览时零 drop_caches → UI_a112（车机写路径 IO 抖动）
      防护不回归。

    公共约束（两语义一致）：
      - 30s TTL：距上次刷新不足 30s 直接跳过
      - listdir 指纹：文件集合未变（车机未写）→ 跳过，零开销
      - S3 兜底：非 dirty 且距上次刷新 >120s 强制刷一次
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
        # 30s TTL：节流窗口内不重复刷（force=True 绕过：缩略图旧簇校验重试专用，M64）
        if not force and now - _ensure_state["last_ts"] < ENSURE_TTL_SEC:
            return False
        if force:
            # 强制刷：跳过指纹/ dirty 判定，直接 drop（旧簇帧校验失败后的重试路径）
            fp = _dir_fingerprint(target)
            return _do_refresh(fp)
        fp = _dir_fingerprint(target)
        # 目录不可读 → 保守立即刷（维持原行为）
        if fp is None:
            return _do_refresh(fp)
        # 首跑（启动后第一次调用）：直接刷一次建立基准指纹，
        # 避免 last_fingerprint=None 被误判为"写入"而多等延迟窗口
        if _ensure_state["last_ts"] == 0:
            return _do_refresh(fp)
        if fp != _ensure_state["last_fingerprint"]:
            # 检测到车机写入（新文件/回收重写）：
            if background:
                # 后台：仅标记 dirty + 记录时间，静止达标后由后续轮次刷新（A1）
                _ensure_state["dirty"] = True
                _ensure_state["last_write_ts"] = now
                _ensure_state["last_fingerprint"] = fp
                logger.debug("后台检测到车机写入，标记待刷（延迟窗口 %ds）",
                             WRITE_STABLE_SEC)
                return False
            # 前台：立即刷（读取时刻 = 刷新时刻）
            logger.debug("前台读取检测到车机写入，立即刷新")
            return _do_refresh(fp)
        # 指纹未变：
        if _ensure_state["dirty"]:
            if background:
                # 后台：车机静止 ≥60s 才执行刷新
                if (now - _ensure_state["last_write_ts"]) >= WRITE_STABLE_SEC:
                    return _do_refresh(fp)
                # 车机仍在写（或刚停不足窗口）→ 继续等待，不刷
                return False
            # 前台：dirty 悬挂期间用户主动读取 → 立即刷（用户操作优先）
            logger.debug("前台读取（dirty 悬挂中）立即刷新")
            return _do_refresh(fp)
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
                # 后台兜底固定 background=True（A1 延迟刷：写入仅标记 dirty，
                # 静止 ≥60s 才刷）。前台读取即时性由各入口 ensure_fresh() 默认
                # 参数（background=False → 立即刷）保证。
                ensure_fresh(background=True)
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
