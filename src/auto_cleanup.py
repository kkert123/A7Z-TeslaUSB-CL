#!/usr/bin/env python3
"""
TeslaUSB Neo - 自动清理模块
============================
功能：
1. 监控分区磁盘使用率
2. 空间不足时按策略清理旧视频
3. 定期清理过期的预览图和临时文件
4. 清理日志文件

清理策略（按优先级）：
1. 已上传成功的哨兵视频（最旧优先）
2. 过期的预览图（>7天）
3. 过期的临时文件（>1天）
4. 旧的日志文件（>30天）

触发条件：
- 磁盘使用率 > 85%: 清理已上传成功的视频
- 磁盘使用率 > 90%: 激进清理（包括未上传的视频，发送通知）
- 磁盘使用率 > 95%: 紧急清理（删除最旧文件，发送告警）

作者: TeslaUSB-Neo 项目
"""

import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import PARTITIONS

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 清理配置
# ═══════════════════════════════════════════════════════════

# 磁盘使用率阈值
DISK_THRESHOLD_WARNING = 85    # 警告 - 开始清理已上传视频
DISK_THRESHOLD_CRITICAL = 90   # 严重 - 激进清理
DISK_THRESHOLD_EMERGENCY = 95  # 紧急 - 紧急清理

# 文件保留时间
PREVIEW_MAX_AGE_DAYS = 7       # 预览图最多保留7天
TEMP_MAX_AGE_DAYS = 1          # 临时文件最多保留1天
LOG_MAX_AGE_DAYS = 30          # 日志文件最多保留30天

# 清理目标 - 每次清理至少释放的空间 (MB)
MIN_FREE_TARGET_MB = 500       # 每次清理至少释放 500MB

# 路径配置
SENTRY_CLIPS_PATH = os.path.join(PARTITIONS.get("cam", "/media/cnlvan/cam"), "TeslaCam", "SentryClips")
SAVED_CLIPS_PATH = os.path.join(PARTITIONS.get("cam", "/media/cnlvan/cam"), "TeslaCam", "SavedClips")
PREVIEW_DIR = "/opt/teslausb-web/data/previews"
DATA_DIR = "/opt/teslausb-web/data"
LOG_DIR = "/data/logs"

# 哨兵事件状态文件
SENTRY_EVENTS_FILE = os.path.join(DATA_DIR, "sentry_events.json")

# 清理记录文件
CLEANUP_LOG_FILE = os.path.join(DATA_DIR, "cleanup_history.json")


class AutoCleaner:
    """自动清理器"""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.stats = {
            "deleted_files": 0,
            "freed_bytes": 0,
            "skipped_files": 0,
            "errors": [],
        }

    def get_disk_usage(self, path: str) -> Optional[Dict]:
        """获取磁盘使用情况"""
        if not os.path.ismount(path):
            logger.warning(f"路径未挂载: {path}")
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

    def get_uploaded_events(self) -> set:
        """获取已上传成功的哨兵事件 ID"""
        try:
            if not os.path.exists(SENTRY_EVENTS_FILE):
                return set()
            with open(SENTRY_EVENTS_FILE, "r") as f:
                data = json.load(f)
            uploaded = set()
            for event in data.get("events", []):
                status = event.get("status", "")
                if status in ("done", "uploaded", "confirmed"):
                    uploaded.add(event.get("id", ""))
            return uploaded
        except Exception as e:
            logger.error(f"读取哨兵事件状态失败: {e}")
            return set()

    def scan_clips_dir(self, base_dir: str) -> List[Tuple[str, float, float]]:
        """
        扫描哨兵目录，返回 (文件路径, 修改时间, 文件大小) 列表
        按修改时间升序排列（最旧的在前）
        """
        clips = []
        if not os.path.isdir(base_dir):
            return clips
        for root, dirs, files in os.walk(base_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    stat = os.stat(fpath)
                    clips.append((fpath, stat.st_mtime, stat.st_size))
                except OSError:
                    continue
        # 最旧的排前面
        clips.sort(key=lambda x: x[1])
        return clips

    def delete_file(self, fpath: str) -> bool:
        """删除单个文件（包括空父目录）"""
        try:
            fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0
            if not self.dry_run:
                os.remove(fpath)
                # 尝试删除空父目录
                parent = os.path.dirname(fpath)
                while parent and os.path.isdir(parent):
                    try:
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    except OSError:
                        break
            self.stats["deleted_files"] += 1
            self.stats["freed_bytes"] += fsize
            return True
        except Exception as e:
            self.stats["errors"].append(f"删除失败 {fpath}: {e}")
            logger.error(f"删除文件失败: {fpath}: {e}")
            return False

    def cleanup_uploaded_clips(self, max_free_bytes: int = 0) -> int:
        """清理已上传成功的哨兵视频"""
        uploaded_ids = self.get_uploaded_events()
        if not uploaded_ids:
            logger.info("没有已上传的事件，跳过视频清理")
            return 0

        freed = 0
        for clip_dir in [SENTRY_CLIPS_PATH, SAVED_CLIPS_PATH]:
            clips = self.scan_clips_dir(clip_dir)
            for fpath, mtime, fsize in clips:
                # 检查是否属于已上传事件
                fname = os.path.basename(fpath)
                event_id = fname.split("-")[0] if "-" in fname else ""
                parent_name = os.path.basename(os.path.dirname(fpath))

                is_uploaded = (
                    event_id in uploaded_ids or
                    parent_name in uploaded_ids or
                    any(event_id.startswith(eid[:8]) for eid in uploaded_ids)
                )

                if is_uploaded:
                    if self.delete_file(fpath):
                        freed += fsize
                        logger.info(f"清理已上传视频: {fpath}")
                    if max_free_bytes > 0 and freed >= max_free_bytes:
                        break
            if max_free_bytes > 0 and freed >= max_free_bytes:
                break

        return freed

    def cleanup_previews(self) -> int:
        """清理过期的预览图"""
        if not os.path.isdir(PREVIEW_DIR):
            return 0

        cutoff = time.time() - PREVIEW_MAX_AGE_DAYS * 86400
        freed = 0
        for root, dirs, files in os.walk(PREVIEW_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    stat = os.stat(fpath)
                    if stat.st_mtime < cutoff:
                        if self.delete_file(fpath):
                            freed += stat.st_size
                except OSError:
                    continue
        return freed

    def cleanup_temp_files(self) -> int:
        """清理临时文件"""
        freed = 0
        # 清理数据目录下的 .tmp 文件
        if os.path.isdir(DATA_DIR):
            cutoff = time.time() - TEMP_MAX_AGE_DAYS * 86400
            for fname in os.listdir(DATA_DIR):
                if fname.startswith(".tmp_") or fname.endswith(".tmp"):
                    fpath = os.path.join(DATA_DIR, fname)
                    try:
                        stat = os.stat(fpath)
                        if stat.st_mtime < cutoff:
                            if self.delete_file(fpath):
                                freed += stat.st_size
                    except OSError:
                        continue
        # 清理系统 /tmp 下的 teslausb 相关临时文件
        for tmp_dir in ["/tmp"]:
            if os.path.isdir(tmp_dir):
                for fname in os.listdir(tmp_dir):
                    if "teslausb" in fname.lower():
                        fpath = os.path.join(tmp_dir, fname)
                        try:
                            stat = os.stat(fpath)
                            if stat.st_mtime < cutoff:
                                if self.delete_file(fpath):
                                    freed += stat.st_size
                        except OSError:
                            continue
        return freed

    def cleanup_logs(self) -> int:
        """清理旧日志文件"""
        freed = 0
        for log_dir in [LOG_DIR, "/var/log"]:
            if not os.path.isdir(log_dir):
                continue
            cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
            for fname in os.listdir(log_dir):
                if not fname.endswith(".log") and not fname.endswith(".log.gz"):
                    continue
                if fname in ("teslausb.log",):  # 保留活跃日志
                    continue
                fpath = os.path.join(log_dir, fname)
                try:
                    stat = os.stat(fpath)
                    if stat.st_mtime < cutoff:
                        if self.delete_file(fpath):
                            freed += stat.st_size
                except OSError:
                    continue
        return freed

    def run(self) -> Dict:
        """
        执行清理任务

        返回清理统计信息
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
        }

        # 检查 cam 分区使用率
        cam_path = PARTITIONS.get("cam", "/media/cnlvan/cam")
        disk = self.get_disk_usage(cam_path)

        if disk is None:
            logger.warning(f"cam 分区未挂载 ({cam_path})，跳过清理")
            return self.stats

        percent = disk["percent"]
        logger.info(f"cam 分区使用率: {percent}% ({disk['used']}/{disk['total']})")

        # 1. 始终清理过期预览和临时文件
        freed_preview = self.cleanup_previews()
        freed_temp = self.cleanup_temp_files()
        freed_logs = self.cleanup_logs()

        if freed_preview > 0:
            self.stats["actions"].append(f"清理预览图: {self._fmt_bytes(freed_preview)}")
            logger.info(f"清理预览图释放: {self._fmt_bytes(freed_preview)}")
        if freed_temp > 0:
            self.stats["actions"].append(f"清理临时文件: {self._fmt_bytes(freed_temp)}")
            logger.info(f"清理临时文件释放: {self._fmt_bytes(freed_temp)}")
        if freed_logs > 0:
            self.stats["actions"].append(f"清理日志文件: {self._fmt_bytes(freed_logs)}")
            logger.info(f"清理日志文件释放: {self._fmt_bytes(freed_logs)}")

        # 2. 根据磁盘使用率决定是否清理视频
        if percent >= DISK_THRESHOLD_WARNING:
            target = MIN_FREE_TARGET_MB * 1024 * 1024
            if percent >= DISK_THRESHOLD_EMERGENCY:
                target *= 3
                level = "紧急"
            elif percent >= DISK_THRESHOLD_CRITICAL:
                target *= 2
                level = "严重"
            else:
                level = "警告"

            logger.info(f"[{level}] 磁盘使用率 {percent}%，目标清理: {self._fmt_bytes(target)}")
            freed_clips = self.cleanup_uploaded_clips(max_free_bytes=target)
            if freed_clips > 0:
                self.stats["actions"].append(f"[{level}] 清理已上传视频: {self._fmt_bytes(freed_clips)}")
                logger.info(f"[{level}] 清理已上传视频释放: {self._fmt_bytes(freed_clips)}")

        # 记录清理历史
        self._save_cleanup_history()

        logger.info(f"=== 自动清理完成: 删除 {self.stats['deleted_files']} 个文件, "
                     f"释放 {self._fmt_bytes(self.stats['freed_bytes'])} ===")

        return self.stats

    def _save_cleanup_history(self):
        """保存清理历史记录"""
        try:
            os.makedirs(os.path.dirname(CLEANUP_LOG_FILE), exist_ok=True)
            history = []
            if os.path.exists(CLEANUP_LOG_FILE):
                with open(CLEANUP_LOG_FILE, "r") as f:
                    history = json.load(f)
            history.insert(0, self.stats)
            # 只保留最近 50 条记录
            history = history[:50]
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


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="TeslaUSB Neo 自动清理工具")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不实际删除文件")
    parser.add_argument("--preview-only", action="store_true", help="仅清理过期预览")
    parser.add_argument("--temp-only", action="store_true", help="仅清理临时文件")
    parser.add_argument("--log-only", action="store_true", help="仅清理日志")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cleaner = AutoCleaner(dry_run=args.dry_run)

    if args.preview_only:
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
