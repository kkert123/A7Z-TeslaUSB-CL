#!/usr/bin/env python3
"""
TeslaUSB Neo - 自动清理模块 v2
================================
基于 mphacker/TeslaUSB 的 per-folder age/size/count 策略重写。

核心改进（v2 vs v1）：
  v1: 单一的 85%/90%/95% 磁盘使用率阈值 → 触发清理已上传视频
  v2: 每个 TeslaCam 子文件夹独立 age/size/count 策略 + 磁盘阈值作为辅助触发

策略体系（per-folder）：
  ┌──────────────┬──────────┬──────────┬───────────┬──────────────┐
  │ 文件夹        │ age_days │ max_gb   │ max_count │ 默认状态      │
  ├──────────────┼──────────┼──────────┼───────────┼──────────────┤
  │ SentryClips   │ 90       │ 100      │ 1000      │ disabled     │
  │ SavedClips    │ 365      │ 100      │ 1000      │ disabled     │
  │ RecentClips   │ 7        │ 20       │ 200       │ enabled      │
  └──────────────┴──────────┴──────────┴───────────┴──────────────┘

保护机制：
  1. 1 小时时间窗口保护（文件可能仍在录制/被读取）
  2. 未上传事件保护（从 sentry_events.json 读取状态）
  3. 文件锁检测（尝试独占打开）

工作流程：
  1. calculate_cleanup_plan() → 预览清理计划（不删除）
  2. preview_cleanup_impact() → 展示影响（前后对比）
  3. execute_cleanup() → 执行清理

磁盘阈值（辅助触发）：
  - > 85%: 触发 per-folder 策略清理（仅 enabled=True 的文件夹）
  - > 90%: 触发所有文件夹清理（忽略 enabled 标志）
  - > 95%: 紧急清理所有文件夹 + 发送告警

参考: mphacker/TeslaUSB scripts/web/services/cleanup_service.py
作者: TeslaUSB-Neo 项目
"""

import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from config import (
    PARTITIONS, CONFIG_DIR, DATA_DIR,
    SENTRY_CLIPS_PATH, RECENT_CLIPS_PATH, SAVED_CLIPS_PATH,
    SENTRY_STATE_FILE, APP_CONFIG_FILE,
)
import video_service

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 默认策略配置
# ═══════════════════════════════════════════════════════════

DEFAULT_POLICIES: Dict[str, Dict] = {
    "SentryClips": {
        "enabled": False,          # 哨兵事件需人工审查，默认不自动清理
        "age_days": 90,
        "max_gb": 100.0,
        "max_count": 1000,
        "protect_unsynced": True,  # 保护未上传/未确认的事件
    },
    "SavedClips": {
        "enabled": False,          # 手动保存的重要片段，默认保护
        "age_days": 365,
        "max_gb": 100.0,
        "max_count": 1000,
        "protect_unsynced": True,
    },
    "RecentClips": {
        "enabled": True,           # Tesla 自己也在滚动覆盖，积极清理
        "age_days": 7,
        "max_gb": 20.0,
        "max_count": 200,
        "protect_unsynced": False, # RecentClips 不追踪同步状态
    },
}

# 允许的文件夹名称（白名单，防止配置污染）
ALLOWED_FOLDER_NAMES = ("SentryClips", "SavedClips", "RecentClips")

# ─── 磁盘使用率阈值（辅助触发） ───
DISK_THRESHOLD_WARNING = 85    # 警告 - 触发 enabled 文件夹清理
DISK_THRESHOLD_CRITICAL = 90   # 严重 - 触发所有文件夹清理
DISK_THRESHOLD_EMERGENCY = 95  # 紧急 - 紧急清理 + 告警

# ─── 文件保留时间（非视频文件） ───
PREVIEW_MAX_AGE_DAYS = 7       # 预览图最多保留7天
TEMP_MAX_AGE_DAYS = 1          # 临时文件最多保留1天
LOG_MAX_AGE_DAYS = 30          # 日志文件最多保留30天

# ─── 路径配置 ───
# 使用 config.py 中的路径
PREVIEW_DIR = "/opt/radxa_data/teslausb/static/thumbnails"
LOG_DIR = "/var/log"

# 清理记录文件
CLEANUP_LOG_FILE = os.path.join(DATA_DIR, "cleanup_history.json")

# 清理策略持久化文件
CLEANUP_POLICY_FILE = os.path.join(CONFIG_DIR, "cleanup_policies.json")

# 全局阈值持久化文件（磁盘阈值 + 保留天数等）
CLEANUP_GLOBAL_FILE = os.path.join(CONFIG_DIR, "cleanup_global.json")

# ═══════════════════════════════════════════════════════════
# 全局默认值（仅作为 fallback，实际值从文件加载）
# ═══════════════════════════════════════════════════════════
_GLOBAL_DEFAULTS = {
    "disk_threshold_warning": 85,
    "disk_threshold_critical": 90,
    "disk_threshold_emergency": 95,
    "preview_max_age_days": 7,
    "temp_max_age_days": 1,
    "log_max_age_days": 30,
}

# 运行时缓存（惰加载）
_global_cache: Optional[Dict] = None

# 上传完成的状态值（与 sentry_service 的状态体系对齐）
UPLOADED_STATUSES = ("completed", "auto_upload", "done", "uploaded", "confirmed")

# 每次清理至少释放的空间 (bytes)
MIN_FREE_TARGET = 500 * 1024 * 1024  # 500MB

# 视频文件扩展名
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".webm"}


# ═══════════════════════════════════════════════════════════
# 清理策略管理
# ═══════════════════════════════════════════════════════════

class CleanupPolicy:
    """单个文件夹的清理策略"""

    def __init__(self, folder_name: str, policy_dict: Optional[Dict] = None):
        self.folder_name = folder_name
        d = policy_dict or DEFAULT_POLICIES.get(folder_name, DEFAULT_POLICIES.get("_default", {}))
        self.enabled = d.get("enabled", False)
        self.age_days = d.get("age_days", 90)
        self.max_gb = d.get("max_gb", 50.0)
        self.max_count = d.get("max_count", 500)
        self.protect_unsynced = d.get("protect_unsynced", True)

    def to_dict(self) -> Dict:
        return {
            "enabled": self.enabled,
            "age_days": self.age_days,
            "max_gb": self.max_gb,
            "max_count": self.max_count,
            "protect_unsynced": self.protect_unsynced,
        }

    def __repr__(self):
        return (f"CleanupPolicy({self.folder_name}: enabled={self.enabled}, "
                f"age={self.age_days}d, size={self.max_gb}GB, count={self.max_count})")


def load_policies() -> Dict[str, CleanupPolicy]:
    """从持久化文件加载策略，合并默认值"""
    policies = {}

    # 先加载默认值
    for name in ALLOWED_FOLDER_NAMES:
        policies[name] = CleanupPolicy(name)

    # 尝试加载持久化配置
    user_policies = {}
    if os.path.exists(CLEANUP_POLICY_FILE):
        try:
            with open(CLEANUP_POLICY_FILE, "r") as f:
                user_policies = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"读取清理策略文件失败: {e}，使用默认值")

    # 合并用户配置
    for name in ALLOWED_FOLDER_NAMES:
        if name in user_policies:
            policies[name] = CleanupPolicy(name, user_policies[name])

    return policies


def save_policies(policies: Dict[str, CleanupPolicy]):
    """持久化清理策略"""
    try:
        os.makedirs(os.path.dirname(CLEANUP_POLICY_FILE), exist_ok=True)
        data = {name: p.to_dict() for name, p in policies.items()}
        # 原子写入
        tmp_file = CLEANUP_POLICY_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, CLEANUP_POLICY_FILE)
        logger.info(f"清理策略已保存到 {CLEANUP_POLICY_FILE}")
    except Exception as e:
        logger.error(f"保存清理策略失败: {e}")


def get_global_settings() -> Dict:
    """获取全局清理阈值设置（惰加载 + 缓存）"""
    global _global_cache
    if _global_cache is not None:
        return dict(_global_cache)

    settings = dict(_GLOBAL_DEFAULTS)
    try:
        if os.path.exists(CLEANUP_GLOBAL_FILE):
            with open(CLEANUP_GLOBAL_FILE, "r") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"读取全局清理设置失败: {e}，使用默认值")

    _global_cache = dict(settings)
    return dict(settings)


def save_global_settings(settings: Dict) -> bool:
    """持久化全局清理阈值设置"""
    global _global_cache
    try:
        os.makedirs(os.path.dirname(CLEANUP_GLOBAL_FILE), exist_ok=True)
        # 只保留白名单键
        clean = {}
        for key in _GLOBAL_DEFAULTS:
            if key in settings:
                clean[key] = int(settings[key])
        # 原子写入
        tmp_file = CLEANUP_GLOBAL_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, CLEANUP_GLOBAL_FILE)
        _global_cache = dict(clean)
        logger.info(f"全局清理设置已保存到 {CLEANUP_GLOBAL_FILE}")
        return True
    except Exception as e:
        logger.error(f"保存全局清理设置失败: {e}")
        return False


def reload_global_settings():
    """强制重新加载全局设置（用于模块热更新）"""
    global _global_cache
    _global_cache = None
    return get_global_settings()


def detect_folders(partition_path: str) -> List[str]:
    """检测 TeslaCam 下实际存在的子文件夹"""
    teslacam = os.path.join(partition_path, "TeslaCam")
    if not os.path.isdir(teslacam):
        logger.warning(f"TeslaCam 目录不存在: {teslacam}")
        return []
    folders = []
    for name in ALLOWED_FOLDER_NAMES:
        if os.path.isdir(os.path.join(teslacam, name)):
            folders.append(name)
    return folders


# ═══════════════════════════════════════════════════════════
# AutoCleaner 核心类
# ═══════════════════════════════════════════════════════════

class AutoCleaner:
    """自动清理器 v2 — per-folder age/size/count 策略"""

    def __init__(self, dry_run: bool = False, config_path: Optional[str] = None):
        self.dry_run = dry_run
        self.policies = load_policies()
        self.uploaded_folders: Set[str] = set()
        self.stats = {
            "deleted_files": 0,
            "freed_bytes": 0,
            "skipped_files": 0,
            "errors": [],
            "actions": [],
            "breakdown": {},
        }

    # ─── 磁盘信息 ───

    def get_disk_usage(self, path: str) -> Optional[Dict]:
        """获取磁盘使用情况"""
        if not os.path.ismount(path):
            return None
        try:
            stat = os.statvfs(path)
            total = stat.f_blocks * stat.f_frsize
            used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            return {
                "total": total,
                "used": used,
                "free": free,
                "percent": int((stat.f_blocks - stat.f_bfree) * 100 / stat.f_blocks) if stat.f_blocks else 0,
            }
        except Exception as e:
            logger.error(f"获取磁盘使用率失败: {e}")
            return None

    # ─── 事件状态 ───

    def get_uploaded_events(self) -> Set[str]:
        """获取已完成上传的哨兵事件文件夹名"""
        try:
            if not os.path.exists(SENTRY_STATE_FILE):
                return set()
            with open(SENTRY_STATE_FILE, "r") as f:
                data = json.load(f)
            uploaded = set()
            for event in data.get("events", []):
                status = event.get("status", "")
                if status in UPLOADED_STATUSES:
                    folder_path = event.get("folder_path", "")
                    if folder_path:
                        uploaded.add(os.path.basename(folder_path))
            return uploaded
        except Exception as e:
            logger.error(f"读取哨兵事件状态失败: {e}")
            return set()

    # ─── 视频扫描 ───

    @staticmethod
    def _is_video_file(filepath: str) -> bool:
        """判断是否为视频文件"""
        ext = os.path.splitext(filepath)[1].lower()
        return ext in VIDEO_EXTENSIONS

    def scan_videos_in_folder(self, folder_path: str) -> List[Dict]:
        """
        递归扫描文件夹，返回视频文件列表。
        每个元素: {path, size, mtime, folder_name}
        按修改时间升序排列（最旧的在前）。
        """
        videos = []
        if not os.path.isdir(folder_path):
            return videos
        for root, dirs, files in os.walk(folder_path):
            for fname in files:
                fpath = os.path.join(root, fname)
                if not self._is_video_file(fpath):
                    continue
                try:
                    stat = os.stat(fpath)
                    videos.append({
                        "path": fpath,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "folder_name": os.path.basename(folder_path),
                    })
                except OSError:
                    continue
        videos.sort(key=lambda v: v["mtime"])
        return videos

    # ─── 保护过滤器 ───

    def _is_protected(self, video: Dict, policy: CleanupPolicy) -> bool:
        """
        检查视频是否受保护，不应删除。

        保护条件：
        1. 文件修改时间 < 1 小时前（可能还在录制/写入）
        2. 保护未同步事件 & 该视频所在事件未上传（仅 SentryClips/SavedClips）
        3. 文件被其他进程占用（文件锁检测）
        """
        fpath = video["path"]

        # 1. 时间窗口保护：1 小时内的文件不删除
        one_hour_ago = time.time() - 3600
        if video["mtime"] > one_hour_ago:
            return True

        # 2. 未上传事件保护
        if policy.protect_unsynced and policy.folder_name in ("SentryClips", "SavedClips"):
            # 从文件路径提取事件文件夹名
            # 路径格式: /mnt/teslacam/TeslaCam/SentryClips/2026-05-09_17-47-03/xxx.mp4
            parts = fpath.replace("\\", "/").split("/")
            try:
                # 找到 SentryClips/SavedClips 后的第一个目录
                idx = -1
                for i, p in enumerate(parts):
                    if p in ("SentryClips", "SavedClips"):
                        idx = i + 1
                        break
                if idx > 0 and idx < len(parts):
                    event_folder = parts[idx]
                    if event_folder not in self.uploaded_folders:
                        return True  # 事件未上传，保护
            except (ValueError, IndexError):
                pass

        # 3. 文件锁检测
        try:
            fd = os.open(fpath, os.O_RDONLY | os.O_EXCL)
            os.close(fd)
        except (IOError, OSError):
            return True  # 文件被占用

        return False

    # ═════════════════════════════════════════════════════════
    # 核心算法：calculate_cleanup_plan
    # ═════════════════════════════════════════════════════════

    def calculate_cleanup_plan(
        self,
        partition_path: str,
        respect_enabled: bool = True,
    ) -> Dict[str, Any]:
        """
        计算清理计划（不删除任何文件）。

        Args:
            partition_path: TeslaCam 分区挂载点
            respect_enabled: True=只处理 enabled=True 的文件夹 (自动模式)

        Returns:
            {
                "total_count": int,
                "total_size": int,
                "total_size_gb": float,
                "breakdown": {
                    "SentryClips": {"count": N, "size": B, "size_gb": G, "videos": [...]},
                    ...
                },
                "protected_count": int,
                "protected_size": int,
            }
        """
        candidates = []
        breakdown = {}
        protected_count = 0
        protected_size = 0

        # 确保已上传事件集合已加载
        if not self.uploaded_folders:
            self.uploaded_folders = self.get_uploaded_events()

        teslacam = os.path.join(partition_path, "TeslaCam")
        if not os.path.isdir(teslacam):
            logger.warning(f"TeslaCam 目录不存在: {teslacam}")
            return self._empty_plan()

        for folder_name, policy in self.policies.items():
            # 自动模式下跳过 disabled 文件夹
            if respect_enabled and not policy.enabled:
                continue

            folder_path = os.path.join(teslacam, folder_name)
            videos = self.scan_videos_in_folder(folder_path)
            if not videos:
                continue

            folder_candidates: List[Dict] = []

            # --- 策略 1: Age-based（基于年龄） ---
            if policy.age_days > 0:
                cutoff = time.time() - policy.age_days * 86400
                age_matches = [v for v in videos if v["mtime"] < cutoff]
                folder_candidates.extend(age_matches)

            # --- 策略 2: Size-based（基于大小） ---
            if policy.max_gb > 0:
                max_bytes = int(policy.max_gb * 1024**3)
                total_size = sum(v["size"] for v in videos)
                if total_size > max_bytes:
                    # videos 已按时间升序排列，从最旧开始删
                    current_size = total_size
                    for v in videos:
                        if current_size <= max_bytes:
                            break
                        folder_candidates.append(v)
                        current_size -= v["size"]

            # --- 策略 3: Count-based（基于数量） ---
            if policy.max_count > 0 and len(videos) > policy.max_count:
                # videos 已按时间升序排列，保留最新的 max_count 个
                excess = videos[:-policy.max_count]
                folder_candidates.extend(excess)

            # 去重（同一文件可能命中多个策略）
            seen = {}
            for v in folder_candidates:
                seen[v["path"]] = v
            unique_candidates = list(seen.values())

            # 应用保护过滤器
            deletable = []
            for v in unique_candidates:
                if self._is_protected(v, policy):
                    protected_count += 1
                    protected_size += v["size"]
                else:
                    deletable.append(v)

            candidates.extend(deletable)
            breakdown[folder_name] = {
                "count": len(deletable),
                "size": sum(v["size"] for v in deletable),
                "size_gb": round(sum(v["size"] for v in deletable) / (1024**3), 2),
                "videos": deletable,
                "policy": policy.to_dict(),
            }

        total_size = sum(v["size"] for v in candidates)
        return {
            "total_count": len(candidates),
            "total_size": total_size,
            "total_size_gb": round(total_size / (1024**3), 2),
            "breakdown": breakdown,
            "protected_count": protected_count,
            "protected_size": protected_size,
            "protected_size_gb": round(protected_size / (1024**3), 2),
        }

    @staticmethod
    def _empty_plan() -> Dict:
        return {
            "total_count": 0,
            "total_size": 0,
            "total_size_gb": 0.0,
            "breakdown": {},
            "protected_count": 0,
            "protected_size": 0,
            "protected_size_gb": 0.0,
        }

    # ═════════════════════════════════════════════════════════
    # preview_cleanup_impact
    # ═════════════════════════════════════════════════════════

    def preview_cleanup_impact(
        self,
        plan: Dict,
        disk_usage: Optional[Dict] = None,
    ) -> Dict:
        """
        预览清理影响（前后对比）。

        Returns:
            {
                "before": {"percent": P, "total_gb": T, "used_gb": U, "free_gb": F},
                "after":  {"percent": P, "total_gb": T, "used_gb": U, "free_gb": F},
                "freed_gb": G,
                "folders_affected": [...],
            }
        """
        result = {
            "freed_gb": round(plan["total_size"] / (1024**3), 2),
            "folders_affected": list(plan["breakdown"].keys()),
        }

        if disk_usage:
            total = disk_usage["total"]
            used = disk_usage["used"]
            result["before"] = {
                "percent": disk_usage["percent"],
                "total_gb": round(total / (1024**3), 1),
                "used_gb": round(used / (1024**3), 1),
                "free_gb": round(disk_usage["free"] / (1024**3), 1),
            }
            new_used = used - plan["total_size"]
            new_percent = int(new_used * 100 / total) if total else 0
            result["after"] = {
                "percent": new_percent,
                "total_gb": round(total / (1024**3), 1),
                "used_gb": round(new_used / (1024**3), 1),
                "free_gb": round((total - new_used) / (1024**3), 1),
            }

        return result

    # ═════════════════════════════════════════════════════════
    # execute_cleanup
    # ═════════════════════════════════════════════════════════

    def _delete_file_safe(self, fpath: str) -> Tuple[bool, int]:
        """安全删除文件 + 清理空父目录。返回 (成功, 文件大小)"""
        try:
            if not os.path.exists(fpath):
                # 文件已不存在（可能已被其他进程删除或分区未挂载）
                self.stats.setdefault("missing_files", 0)
                self.stats["missing_files"] += 1
                return False, 0
            fsize = os.path.getsize(fpath)
            if not self.dry_run:
                os.remove(fpath)
                # 尝试删除空父目录链
                parent = os.path.dirname(fpath)
                for _ in range(3):
                    if not parent or parent == "/":
                        break
                    try:
                        if os.path.isdir(parent) and not os.listdir(parent):
                            os.rmdir(parent)
                            parent = os.path.dirname(parent)
                        else:
                            break
                    except OSError:
                        break
            return True, fsize
        except PermissionError as e:
            self.stats["errors"].append(f"权限不足 {fpath}: {e}")
            if self.stats.get("missing_files", 0) > 100:
                self.stats["errors"].append(
                    "⚠️ 大量文件缺失 — 可能 TeslaCam 分区未正确挂载，请检查 /mnt/teslacam/")
            return False, 0
        except Exception as e:
            self.stats["errors"].append(f"删除失败 {fpath}: {e}")
            logger.error(f"删除文件失败: {fpath}: {e}")
            return False, 0

    def execute_cleanup(self, plan: Dict) -> Dict:
        """
        执行清理计划。

        Args:
            plan: calculate_cleanup_plan() 的返回值

        Returns:
            清理统计信息
        """
        self.stats = {
            "timestamp": datetime.now().isoformat(),
            "deleted_files": 0,
            "freed_bytes": 0,
            "skipped_files": 0,
            "errors": [],
            "actions": [],
            "breakdown": {},
        }

        logger.info("=== 清理执行开始 ===")
        if self.dry_run:
            logger.info("[DRY RUN 模式 - 不会实际删除文件]")

        folder_stats = {}
        # 记录被删除视频所在的事件文件夹（仅记录成功删除的）
        _affected_events = set()
        for folder_name, info in plan.get("breakdown", {}).items():
            folder_deleted = 0
            folder_freed = 0
            for video in info.get("videos", []):
                success, size = self._delete_file_safe(video["path"])
                if success:
                    folder_deleted += 1
                    folder_freed += size
                    self.stats["deleted_files"] += 1
                    self.stats["freed_bytes"] += size
                    # 记录事件文件夹：仅当目录名匹配 timestamp 格式（如 2026-07-18_14-23-17）
                    parent = os.path.dirname(video["path"])
                    pname = os.path.basename(parent)
                    if len(pname) >= 17 and pname[:4].isdigit() and '-' in pname and '_' in pname:
                        _affected_events.add(parent)
                else:
                    self.stats["skipped_files"] += 1
            folder_stats[folder_name] = {
                "deleted": folder_deleted,
                "freed_bytes": folder_freed,
                "freed_gb": round(folder_freed / (1024**3), 2),
            }
            if folder_deleted > 0:
                self.stats["actions"].append(
                    f"清理 {folder_name}: {folder_deleted} 个文件, {self._fmt_bytes(folder_freed)}"
                )

        # ── 清理遗留的非视频文件和空事件文件夹 ──
        # 仅清理本次删除过程涉及的事件目录（非全局扫描）
        # 约束条件：目录中无任何 mp4 文件、目录名符合事件时间戳格式
        orphan_count = 0
        orphan_size = 0
        for evt_dir in _affected_events:
            if not os.path.isdir(evt_dir):
                continue
            try:
                entries = os.listdir(evt_dir)
                if not entries:
                    # 已空目录：直接删除
                    if not self.dry_run:
                        os.rmdir(evt_dir)
                        logger.info(f"清理空事件目录: {evt_dir}")
                    continue
                # 检查是否仍包含视频文件
                has_video = any(
                    any(e.lower().endswith(ext) for ext in VIDEO_EXTENSIONS)
                    for e in entries
                )
                if has_video:
                    continue  # 仍有视频文件保护，跳过
                # 只清理已知的事件辅助文件（event.json / thumb.png）
                KNOWN_AUX = {"event.json", "thumb.png"}
                for entry_name in list(entries):
                    if entry_name.lower() not in KNOWN_AUX:
                        # 非已知辅助文件，跳过（安全原则）
                        continue
                    epath = os.path.join(evt_dir, entry_name)
                    if os.path.isfile(epath):
                        try:
                            fsz = os.path.getsize(epath)
                            if not self.dry_run:
                                os.remove(epath)
                            orphan_count += 1
                            orphan_size += fsz
                        except OSError as e:
                            logger.warning(f"清理残留文件失败: {epath}: {e}")
                # 尝试删除空目录
                try:
                    if not self.dry_run and os.path.isdir(evt_dir) and not os.listdir(evt_dir):
                        os.rmdir(evt_dir)
                except OSError:
                    pass
            except OSError:
                continue

        if orphan_count > 0:
            self.stats["deleted_files"] += orphan_count
            self.stats["freed_bytes"] += orphan_size
            self.stats["actions"].append(
                f"清理残留文件: {orphan_count} 个 (event.json/thumb.png), {self._fmt_bytes(orphan_size)}"
            )
            logger.info(f"清理残留文件完成: {orphan_count} 个, {self._fmt_bytes(orphan_size)}")

        self.stats["breakdown"] = folder_stats

        # 保存历史记录
        self._save_cleanup_history()

        total = self.stats["deleted_files"]
        freed = self.stats["freed_bytes"]
        logger.info(f"=== 清理完成: 删除 {total} 个文件, 释放 {self._fmt_bytes(freed)} ===")

        return self.stats

    # ═════════════════════════════════════════════════════════
    # 非视频文件清理（预览图、临时文件、日志）
    # ═════════════════════════════════════════════════════════

    def _count_expired(self, path: str, cutoff: float, pattern: Optional[str] = None) -> int:
        """统计过期文件数量（用于 preview）"""
        count = 0
        if not os.path.isdir(path):
            if os.path.isfile(path):
                try:
                    if os.stat(path).st_mtime < cutoff:
                        return 1
                except OSError:
                    pass
            return 0
        import fnmatch
        for fname in os.listdir(path):
            if pattern and not fnmatch.fnmatch(fname, pattern):
                continue
            fpath = os.path.join(path, fname)
            try:
                if os.path.isfile(fpath) and os.stat(fpath).st_mtime < cutoff:
                    count += 1
            except OSError:
                continue
        return count

    def cleanup_previews(self) -> int:
        """清理过期的预览图（按 mtime 年龄 + 孤儿检测）"""
        freed = 0
        freed += self._cleanup_previews_by_age()
        freed += self._cleanup_orphan_previews()
        return freed
    
    def _cleanup_previews_by_age(self) -> int:
        """按文件年龄清理预览图"""
        if not os.path.isdir(PREVIEW_DIR):
            return 0
        gs = get_global_settings()
        cutoff = time.time() - gs.get('preview_max_age_days', PREVIEW_MAX_AGE_DAYS) * 86400
        freed = 0
        for root, dirs, files in os.walk(PREVIEW_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    stat = os.stat(fpath)
                    if stat.st_mtime < cutoff:
                        success, size = self._delete_file_safe(fpath)
                        if success:
                            freed += size
                except OSError:
                    continue
        return freed
    
    def _cleanup_orphan_previews(self) -> int:
        """清理无对应事件的孤儿缩略图。
        
        缩略图命名使用短前缀: SEN_/SAV_/REC_ + 时间戳 + 可选摄像头后缀 + _grid.jpg
        例如: REC_2026-07-16_21-10-14-front_grid.jpg
        如果对应的视频文件/事件已不存在，删除该缩略图。
        """
        if not os.path.isdir(PREVIEW_DIR):
            return 0
        
        import re
        freed = 0
        
        # 短前缀 → 文件夹类型映射（与 video_service._THUMB_PREFIX 一致）
        PREFIX_MAP = {'SEN': 'SentryClips', 'SAV': 'SavedClips', 'REC': 'RecentClips'}
        
        _thumb_re = re.compile(
            r'^(SEN|SAV|REC)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:-[a-z_]+)?_grid\.jpg$'
        )
        
        # 为各类型构建已存在的事件ID集合（一次扫描，避免每个文件都扫描磁盘）
        existing_events = {}  # folder_type -> set of event_ids
        for ft, info in video_service.VIDEO_FOLDERS.items():
            folder_path = info['path']
            if not os.path.isdir(folder_path):
                existing_events[ft] = set()
                continue
            
            if ft == 'RecentClips':
                groups = set()
                for fname in os.listdir(folder_path):
                    if not fname.lower().endswith('.mp4'):
                        continue
                    m = re.match(
                        r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(front|back|left_repeater|right_repeater)\.mp4$',
                        fname, re.IGNORECASE)
                    if m:
                        groups.add(m.group(1))
                existing_events[ft] = groups
            else:
                existing_events[ft] = {
                    d for d in os.listdir(folder_path)
                    if os.path.isdir(os.path.join(folder_path, d))
                }
        
        # 扫描缩略图，删除无对应视频/事件的
        for fname in os.listdir(PREVIEW_DIR):
            fpath = os.path.join(PREVIEW_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            m = _thumb_re.match(fname)
            if not m:
                continue
            prefix, event_id = m.group(1), m.group(2)
            ft = PREFIX_MAP.get(prefix)
            if not ft:
                continue
            if ft in existing_events and event_id not in existing_events[ft]:
                success, size = self._delete_file_safe(fpath)
                if success:
                    freed += size
        
        if freed > 0:
            count_estimate = sum(1 for _ in (None for _ in [None]) if freed)  # placeholder
            logger.info(f"清理孤儿缩略图释放: {self._fmt_bytes(freed)}")
        return freed

    def cleanup_temp_files(self) -> int:
        """清理临时文件"""
        freed = 0
        gs = get_global_settings()
        cutoff = time.time() - gs.get('temp_max_age_days', TEMP_MAX_AGE_DAYS) * 86400
        # 数据目录下的 .tmp 文件
        if os.path.isdir(DATA_DIR):
            for fname in os.listdir(DATA_DIR):
                if fname.startswith(".tmp_") or fname.endswith(".tmp"):
                    fpath = os.path.join(DATA_DIR, fname)
                    try:
                        st = os.stat(fpath)
                        if os.path.isfile(fpath) and st.st_mtime < cutoff:
                            success, size = self._delete_file_safe(fpath)
                            if success:
                                freed += size
                    except OSError:
                        continue
        # /tmp 下的 teslausb 相关临时文件
        if os.path.isdir("/tmp"):
            for fname in os.listdir("/tmp"):
                if "teslausb" in fname.lower():
                    fpath = os.path.join("/tmp", fname)
                    try:
                        st = os.stat(fpath)
                        if os.path.isfile(fpath) and st.st_mtime < cutoff:
                            success, size = self._delete_file_safe(fpath)
                            if success:
                                freed += size
                    except OSError:
                        continue
        return freed

    def cleanup_logs(self) -> int:
        """清理旧日志文件"""
        freed = 0
        if not os.path.isdir(LOG_DIR):
            return freed
        gs = get_global_settings()
        cutoff = time.time() - gs.get('log_max_age_days', LOG_MAX_AGE_DAYS) * 86400
        for fname in os.listdir(LOG_DIR):
            if not (fname.endswith(".log") or fname.endswith(".log.gz")):
                continue
            if "teslausb" not in fname:
                continue
            fpath = os.path.join(LOG_DIR, fname)
            try:
                stat = os.stat(fpath)
                if stat.st_mtime < cutoff:
                    success, size = self._delete_file_safe(fpath)
                    if success:
                        freed += size
            except OSError:
                continue
        return freed

    # ═════════════════════════════════════════════════════════
    # run() — 自动清理入口（向后兼容）
    # ═════════════════════════════════════════════════════════

    def run(self) -> Dict:
        """
        自动清理入口（向后兼容 v1 API）。

        流程：
        1. 始终清理过期预览/临时文件/日志
        2. 检查 cam 分区磁盘使用率
        3. 根据阈值决定清理级别
        4. 使用 per-folder 策略计算并执行清理
        """
        logger.info("=== 自动清理开始 ===")
        if self.dry_run:
            logger.info("[DRY RUN 模式 - 不会实际删除文件]")

        self.stats = {
            "timestamp": datetime.now().isoformat(),
            "deleted_files": 0,
            "freed_bytes": 0,
            "skipped_files": 0,
            "errors": [],
            "actions": [],
            "breakdown": {},
        }

        cam_path = PARTITIONS.get("cam", "/mnt/teslacam")
        disk = self.get_disk_usage(cam_path)

        # 1. 始终清理过期预览/临时/日志
        freed_preview = self.cleanup_previews()
        freed_temp = self.cleanup_temp_files()
        freed_logs = self.cleanup_logs()

        for label, freed in [("预览图", freed_preview), ("临时文件", freed_temp), ("日志文件", freed_logs)]:
            if freed > 0:
                self.stats["actions"].append(f"清理{label}: {self._fmt_bytes(freed)}")
                logger.info(f"清理{label}释放: {self._fmt_bytes(freed)}")

        # 2. 根据磁盘使用率执行视频清理
        gs = get_global_settings()
        warn_threshold = gs.get('disk_threshold_warning', DISK_THRESHOLD_WARNING)
        if disk is None or disk["percent"] < warn_threshold:
            logger.info("磁盘使用率正常，跳过视频清理")
            self._save_cleanup_history()
            return self.stats

        percent = disk["percent"]
        logger.info(f"cam 分区使用率: {percent}% ({disk['used']}/{disk['total']})")

        # 决定清理级别
        emerg_threshold = gs.get('disk_threshold_emergency', DISK_THRESHOLD_EMERGENCY)
        crit_threshold = gs.get('disk_threshold_critical', DISK_THRESHOLD_CRITICAL)
        if percent >= emerg_threshold:
            respect_enabled = False  # 紧急：清理所有文件夹
            level = "紧急"
        elif percent >= crit_threshold:
            respect_enabled = False  # 严重：清理所有文件夹
            level = "严重"
        else:
            respect_enabled = True   # 警告：只清理 enabled=True 的
            level = "警告"

        # 计算并执行清理计划
        plan = self.calculate_cleanup_plan(cam_path, respect_enabled=respect_enabled)
        logger.info(f"[{level}] 清理计划: {plan['total_count']} 个文件, {plan['total_size_gb']} GB")

        if plan["total_count"] > 0:
            result = self.execute_cleanup(plan)
            if result["deleted_files"] > 0:
                self.stats["deleted_files"] += result["deleted_files"]
                self.stats["freed_bytes"] += result["freed_bytes"]
                self.stats["errors"].extend(result.get("errors", []))
                self.stats["breakdown"] = result.get("breakdown", {})
                self.stats["actions"].extend([
                    f"[{level}] {a}" for a in result.get("actions", [])
                ])
                logger.info(f"[{level}] 清理视频释放: {self._fmt_bytes(result['freed_bytes'])}")

        self._save_cleanup_history()
        total_freed = self.stats["freed_bytes"]
        logger.info(f"=== 自动清理完成: 删除 {self.stats['deleted_files']} 个文件, "
                     f"释放 {self._fmt_bytes(total_freed)} ===")
        return self.stats

    # ═════════════════════════════════════════════════════════
    # 辅助方法
    # ═════════════════════════════════════════════════════════

    def _save_cleanup_history(self):
        """保存清理历史记录"""
        try:
            os.makedirs(os.path.dirname(CLEANUP_LOG_FILE), exist_ok=True)
            history = []
            if os.path.exists(CLEANUP_LOG_FILE):
                with open(CLEANUP_LOG_FILE, "r") as f:
                    history = json.load(f)
            history.insert(0, self.stats)
            history = history[:50]  # 最多保留 50 条
            with open(CLEANUP_LOG_FILE, "w") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存清理历史失败: {e}")

    @staticmethod
    def _fmt_bytes(size: int) -> str:
        """格式化字节数"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="TeslaUSB Neo 自动清理工具 v2")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不实际删除文件")
    parser.add_argument("--plan", action="store_true", help="计算并显示清理计划（不执行）")
    parser.add_argument("--preview-only", action="store_true", help="仅清理过期预览")
    parser.add_argument("--temp-only", action="store_true", help="仅清理临时文件")
    parser.add_argument("--log-only", action="store_true", help="仅清理日志")
    parser.add_argument("--all", action="store_true", help="清理所有文件夹（忽略 enabled 标志）")
    parser.add_argument("--folder", type=str, help="只清理指定文件夹 (SentryClips/SavedClips/RecentClips)")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cleaner = AutoCleaner(dry_run=args.dry_run)

    if args.plan:
        # 只计算清理计划，不执行
        cam_path = PARTITIONS.get("cam", "/mnt/teslacam")
        plan = cleaner.calculate_cleanup_plan(cam_path, respect_enabled=not args.all)
        disk = cleaner.get_disk_usage(cam_path)
        impact = cleaner.preview_cleanup_impact(plan, disk)
        print(json.dumps({"plan": plan, "impact": impact}, ensure_ascii=False, indent=2))
    elif args.folder:
        # 只清理指定文件夹
        cam_path = PARTITIONS.get("cam", "/mnt/teslacam")
        # 临时对特定文件夹启用
        for name, policy in cleaner.policies.items():
            if name == args.folder:
                policy.enabled = True
        plan = cleaner.calculate_cleanup_plan(cam_path, respect_enabled=True)
        if plan["total_count"] > 0:
            result = cleaner.execute_cleanup(plan)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("没有需要清理的文件")
    elif args.preview_only:
        freed = cleaner.cleanup_previews()
        print(f"清理预览图释放: {AutoCleaner._fmt_bytes(freed)}")
    elif args.temp_only:
        freed = cleaner.cleanup_temp_files()
        print(f"清理临时文件释放: {AutoCleaner._fmt_bytes(freed)}")
    elif args.log_only:
        freed = cleaner.cleanup_logs()
        print(f"清理日志文件释放: {AutoCleaner._fmt_bytes(freed)}")
    else:
        result = cleaner.run()
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
