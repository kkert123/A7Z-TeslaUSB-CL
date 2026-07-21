#!/usr/bin/env python3
"""
A7Z Staging Service — Present/Edit 模式分段上传
================================================
Present 模式下写入临时区域 /opt/radxa_data/staging/
Edit 模式时同步到真实分区，完成后清除 manifest。

数据模型 (manifest.json):
{
  "pending": [
    {"action":"upload", "partition":"boombox", "filename":"horn.mp3",
     "staging_path":"/opt/radxa_data/staging/boombox/horn.mp3", "size":524288, "time":"..."},
    {"action":"delete", "partition":"wraps", "filename":"old.png", "time":"..."}
  ]
}
"""

import json
import os
import shutil
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

STAGING_ROOT = "/opt/radxa_data/staging"
MANIFEST_FILE = os.path.join(STAGING_ROOT, "manifest.json")

# A7Z 分区映射
PARTITION_MAP = {
    "music":     "/mnt/music",
    "boombox":   "/mnt/boombox/Boombox",
    "lightshow": "/mnt/lightshow/LightShow",
    "wraps":     "/mnt/lightshow/Wraps",
    "plates":    "/mnt/lightshow/LicensePlate",
    "lockchime": "/mnt/lightshow/Chimes",
}

# TeslaCam 视频文件夹路径（用于 video_delete 操作）
TESCAM_FOLDER_MAP = {
    "RecentClips":  "/mnt/teslacam/TeslaCam/RecentClips",
    "SentryClips":  "/mnt/teslacam/TeslaCam/SentryClips",
    "SavedClips":   "/mnt/teslacam/TeslaCam/SavedClips",
}


def _get_mode() -> str:
    """获取当前模式: present / edit"""
    try:
        with open("/tmp/teslausb_mode", "r") as f:
            return f.read().strip()
    except:
        return "edit"


def is_present() -> bool:
    return _get_mode() == "present"


def _read_manifest() -> dict:
    if not os.path.exists(MANIFEST_FILE):
        return {"pending": []}
    try:
        with open(MANIFEST_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"pending": []}


def _write_manifest(data: dict):
    os.makedirs(STAGING_ROOT, exist_ok=True)
    tmp = MANIFEST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST_FILE)


def add_upload(partition: str, filename: str, file_data: bytes) -> dict:
    """Present 模式: 将文件暂存到 staging 目录，记录到 manifest。
    返回 {'success': bool, 'message': str, 'staging_path': str}
    """
    if partition not in PARTITION_MAP:
        return {"success": False, "message": f"未知分区: {partition}"}

    target_dir = os.path.join(STAGING_ROOT, partition)
    os.makedirs(target_dir, exist_ok=True)

    safe_name = os.path.basename(filename)
    staging_path = os.path.join(target_dir, safe_name)

    try:
        with open(staging_path, "wb") as f:
            f.write(file_data)
        size = os.path.getsize(staging_path)
    except Exception as e:
        return {"success": False, "message": f"暂存写入失败: {e}"}

    manifest = _read_manifest()
    manifest["pending"].append({
        "action": "upload",
        "partition": partition,
        "filename": safe_name,
        "staging_path": staging_path,
        "size": size,
        "time": datetime.now().isoformat(),
    })
    _write_manifest(manifest)

    logger.info(f"[staging] upload {partition}/{safe_name} ({_fmt_size(size)})")
    return {"success": True, "message": f"已暂存 {safe_name} (同步后生效)", "staging_path": staging_path}


def add_delete(partition: str, filename: str) -> dict:
    """Present 模式: 记录待删除文件到 manifest。
    返回 {'success': bool, 'message': str}
    """
    if partition not in PARTITION_MAP:
        return {"success": False, "message": f"未知分区: {partition}"}

    manifest = _read_manifest()
    safe_name = os.path.basename(filename)

    # 如果存在同文件的待上传操作，直接取消（还没写入分区）
    manifest["pending"] = [
        p for p in manifest["pending"]
        if not (p["action"] == "upload" and p["partition"] == partition and p["filename"] == safe_name)
    ]

    manifest["pending"].append({
        "action": "delete",
        "partition": partition,
        "filename": safe_name,
        "time": datetime.now().isoformat(),
    })
    _write_manifest(manifest)

    logger.info(f"[staging] delete {partition}/{safe_name}")
    return {"success": True, "message": f"已标记删除 {safe_name} (同步后生效)"}


def add_video_event_delete(folder_type: str, event_id: str) -> dict:
    """标记视频事件删除（Present 模式入队，Edit 模式时 sync_all 真正删除）。

    folder_type: RecentClips / SentryClips / SavedClips
    event_id: 事件 ID（RecentClips 的 time-group 或 SentryClips 的子目录名）
    """
    if folder_type not in TESCAM_FOLDER_MAP:
        return {"success": False, "message": f"未知文件夹类型: {folder_type}"}

    manifest = _read_manifest()

    # 去重：避免重复标记同一个事件
    for p in manifest["pending"]:
        if p.get("action") == "video_delete" and p.get("folder_type") == folder_type and p.get("event_id") == event_id:
            return {"success": True, "message": f"事件 {event_id} 已在删除队列中"}

    manifest["pending"].append({
        "action": "video_delete",
        "folder_type": folder_type,
        "event_id": event_id,
        "time": datetime.now().isoformat(),
    })
    _write_manifest(manifest)

    logger.info(f"[staging] video_delete {folder_type}/{event_id}")
    return {"success": True, "message": f"已标记删除事件 {event_id}（同步后生效）"}


def get_pending_video_deletes() -> list:
    """获取待删除的视频事件列表，用于扫描时过滤。

    返回 [(folder_type, event_id), ...]
    """
    manifest = _read_manifest()
    result = []
    for p in manifest.get("pending", []):
        if p.get("action") == "video_delete":
            result.append((p["folder_type"], p["event_id"]))
    return result


def get_summary() -> dict:
    """获取待同步摘要: counts + per-partition breakdown + per-file details"""
    manifest = _read_manifest()
    pending = manifest.get("pending", [])
    breakdown = {}
    uploads = []
    deletes = []
    video_deletes = []
    total_size = 0

    for p in pending:
        action = p.get("action", "")

        if action == "video_delete":
            video_deletes.append(p)
            part = p.get("folder_type", "videos")
            dest_path = TESCAM_FOLDER_MAP.get(part, "/mnt/teslacam")
            if dest_path not in breakdown:
                breakdown[dest_path] = {"upload": 0, "delete": 0}
            breakdown[dest_path]["delete"] += 1
            continue

        if action == "upload":
            part = p.get("partition", "")
            if part not in breakdown:
                breakdown[part] = {"upload": 0, "delete": 0}
            breakdown[part]["upload"] += 1
            uploads.append(p)
            total_size += p.get("size", 0)
        elif action == "delete":
            part = p.get("partition", "")
            if part not in breakdown:
                breakdown[part] = {"upload": 0, "delete": 0}
            breakdown[part]["delete"] += 1
            deletes.append(p)

    return {
        "mode": _get_mode(),
        "is_present": is_present(),
        "total_pending": len(pending),
        "total_uploads": len(uploads),
        "total_deletes": len(deletes),
        "total_video_deletes": len(video_deletes),
        "pending_size": total_size,
        "pending_size_fmt": _fmt_size(total_size),
        "breakdown": breakdown,
        "uploads": uploads,
        "deletes": deletes,
        "video_deletes": video_deletes,
    }


def sync_all() -> dict:
    """Edit 模式: 将 staging 中所有待处理操作同步到真实分区。
    返回 {'success': bool, 'synced': int, 'failed': int, 'errors': [...]}
    """
    manifest = _read_manifest()
    pending = manifest.get("pending", [])
    if not pending:
        return {"success": True, "synced": 0, "failed": 0, "errors": []}

    synced = 0
    failed = 0
    errors = []

    for entry in pending:
        action = entry.get("action", "")

        # ── 视频事件删除 ──
        if action == "video_delete":
            folder_type = entry.get("folder_type", "")
            event_id = entry.get("event_id", "")
            folder_path = TESCAM_FOLDER_MAP.get(folder_type)

            if not folder_path:
                failed += 1
                errors.append(f"video_delete 未知文件夹: {folder_type}")
                continue

            try:
                deleted_count = 0
                if folder_type == "RecentClips":
                    # 平铺结构：删除匹配前缀的 mp4 文件
                    if os.path.isdir(folder_path):
                        for fname in os.listdir(folder_path):
                            if fname.startswith(event_id) and fname.lower().endswith(".mp4"):
                                os.remove(os.path.join(folder_path, fname))
                                deleted_count += 1
                else:
                    # 事件目录结构（SentryClips/SavedClips）：删除整个子目录
                    event_path = os.path.join(folder_path, event_id)
                    if os.path.isdir(event_path):
                        shutil.rmtree(event_path)
                        deleted_count = 1

                logger.info(f"[staging] synced video_delete: {folder_type}/{event_id} ({deleted_count} files)")
                synced += 1
            except Exception as e:
                failed += 1
                errors.append(f"video_delete {folder_type}/{event_id}: {e}")
                logger.error(f"[staging] video_delete sync failed: {folder_type}/{event_id} {e}")
            continue

        # ── 媒体文件操作 ──
        part = entry.get("partition", "")
        target_dir = PARTITION_MAP.get(part)
        if not target_dir:
            failed += 1
            errors.append(f"未知分区: {part}")
            continue

        try:
            if action == "upload":
                os.makedirs(target_dir, exist_ok=True)
                dest = os.path.join(target_dir, entry["filename"])
                shutil.copy2(entry["staging_path"], dest)
                os.remove(entry["staging_path"])
                logger.info(f"[staging] synced upload: {part}/{entry['filename']}")
                synced += 1

            elif action == "delete":
                dest = os.path.join(target_dir, entry["filename"])
                if os.path.isfile(dest):
                    os.remove(dest)
                    logger.info(f"[staging] synced delete: {part}/{entry['filename']}")
                synced += 1

        except Exception as e:
            failed += 1
            errors.append(f"{action} {part}/{entry['filename']}: {e}")
            logger.error(f"[staging] sync failed: {entry} {e}")

    # 清空 manifest
    _write_manifest({"pending": []})

    # 清理空目录
    for part in PARTITION_MAP:
        part_dir = os.path.join(STAGING_ROOT, part)
        try:
            if os.path.isdir(part_dir) and not os.listdir(part_dir):
                os.rmdir(part_dir)
        except OSError:
            pass

    logger.info(f"[staging] sync done: {synced} ok, {failed} failed")
    return {"success": True, "synced": synced, "failed": failed, "errors": errors}


def _fmt_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
