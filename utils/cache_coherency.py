#!/usr/bin/env python3
"""
cache_coherency.py — TeslaCam 只读挂载的 VFS 缓存一致性修复

== 问题根因（货不对板的真正原因）==
  Present 模式下，/dev/nvme0n1p2 同时被两处使用：
    1. USB Gadget 把它作为「可写 LUN」绑定给特斯拉实时写入；
    2. 本地以只读方式挂载在 /mnt/teslacam，供 Web 播放 / 缩略图 / SCP 读取。
  Tesla 通过 Gadget 写入后，本地 ro 挂载的 VFS dentry/inode 缓存
  （文件名 → 簇的映射）不会自动失效。当特斯拉回收 RecentClips 文件名
  并重写内容时，本地读取仍命中「旧簇」→ 视频字节货不对板
  （例如看到的是下午画面，磁盘上其实已轮转到夜间）。

  Edit 模式因为拆除了 Gadget 并对分区重新 rw 挂载（+fsck），缓存从
  磁盘重建，所以内容正确。两种模式读的是同一块盘，差异纯粹来自
  「只读挂载的 VFS 缓存是否过期」。

== 修复策略 ==
  在 Present 模式下周期性丢弃内核的 dentry/inode 缓存
  （echo 2 > /proc/sys/vm/drop_caches），强制本地只读挂载在下次访问时
  重新解析目录项，读到特斯拉最新写入的内容。该操作是系统级的，因此
  Web 服务、后台缩略图进程（bg_preview）以及 SCP 读取都会同时受益。
  丢弃缓存为只读、非破坏性操作，不会影响正在进行的文件读写。
"""

import os
import time
import logging
import threading
import subprocess

logger = logging.getLogger("CacheCoherency")

# 模式标志文件：present / edit
MODE_FILE = "/tmp/teslausb_mode"

# Present 模式下刷新 VFS 缓存的间隔（秒）。
# 该值决定「货不对板」的最大滞后窗口，30s 对行车记录仪回放完全可接受。
DEFAULT_REFRESH_INTERVAL = 30

# ── 可观测性状态（供 /api/system/cache-coherency 上报，确认任务真实运行）──
_state = {
    "running": False,
    "present_mode": False,
    "last_refresh_ts": 0.0,   # 最近一次成功刷新缓存的时间戳
    "refresh_count": 0,       # 累计成功刷新次数
    "last_success": False,    # 最近一次刷新是否成功
    "last_error": "",         # 最近一次失败原因
}
_state_lock = threading.Lock()


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


def _coherency_loop(interval: int):
    """后台循环：仅 Present 模式下周期性刷新缓存并使视频扫描元数据失效。"""
    with _state_lock:
        _state["running"] = True

    # 启动时立即刷新一次，避免重启后残留的陈旧缓存
    refreshed = False
    try:
        if _is_present_mode():
            refreshed = drop_vfs_caches()
            if refreshed:
                _invalidate_video_scan_cache()
                _record_success()
    except Exception as e:
        _record_error(str(e))
        logger.warning("初始缓存刷新异常: %s", e)

    while True:
        try:
            present = _is_present_mode()
            with _state_lock:
                _state["present_mode"] = present
            if present:
                if drop_vfs_caches():
                    # 同时让视频扫描元数据缓存失效，避免事件列表陈旧
                    _invalidate_video_scan_cache()
                    _record_success()
                else:
                    _record_error("drop_vfs_caches 返回失败")
            else:
                # Edit 模式挂载为 rw 且已重建缓存，无需刷新
                if refreshed:
                    logger.debug("已离开 Present 模式，停止刷新缓存")
                    refreshed = False
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
    """启动缓存一致性后台任务（守护线程，随 teslausb-web 重启生效）。"""
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
    logger.info("缓存一致性后台任务已启动 (间隔 %ds, 仅 Present 模式生效)", DEFAULT_REFRESH_INTERVAL)
