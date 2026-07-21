"""缩略图决策中心 — 统一的缓存有效性判断、源文件查找、文件名解析

本模块是所有缩略图生成入口点的唯一决策来源。
所有入口点（serve_thumbnail / _scan_missing_thumbnails / bg_preview_generator）
统一调用本模块的公开函数来决定"是否需要重新生成缩略图"，
确保修改一处即可影响全部入口。

设计原则：
1. 所有文件夹类型统一用"文件存在 + 大小 >= 10KB"判断缓存有效性
2. RecentClips 文件被 Tesla 回收，mtime 比较不可靠，已全面移除
3. SentryClips/SavedClips 事件文件夹永不回收，一旦生成就是最终版
4. 旧格式无前缀缩略图保持向后兼容（不被误判为缺失）
"""

import os
import re
import time

from video_service import (
    THUMBNAIL_DIR, VIDEO_FOLDERS,
    _THUMB_PREFIX, get_thumbnail_path, get_thumbnail_filename,
    is_valid_mp4,
)

# ---------- 常量 ----------

_MIN_THUMBNAIL_SIZE = 10240  # 10KB，小于此值视为损坏/不完整


# =====================================================================
#  公开函数
# =====================================================================

def parse_filename(filename: str) -> tuple:
    """解析缩略图文件名，返回 (folder_type, event_id)。

    支持新旧两种格式：
    - 新格式:  REC_2026-07-13_22-42-00_grid.jpg  → ("RecentClips", "2026-07-13_22-42-00")
    - 旧格式:  2026-06-15_17-24-11_grid.jpg       → (None, "2026-06-15_17-24-11")

    旧格式无前缀 → folder_type 返回 None，调用方需自行搜索。
    """
    # 尝试新格式: PREFIX_event_id_grid.jpg
    for ft, prefix in _THUMB_PREFIX.items():
        if filename.startswith(prefix) and filename.endswith('_grid.jpg'):
            event_id = filename[len(prefix):-len('_grid.jpg')]
            return ft, event_id

    # 旧格式: event_id_grid.jpg（无前缀）
    if filename.endswith('_grid.jpg'):
        event_id = filename[:-len('_grid.jpg')]
        return None, event_id

    # 兜底：去掉扩展名
    event_id = os.path.splitext(filename)[0]
    return None, event_id


def _get_any_thumbnail_path(folder_type, event_id):
    """返回新格式缩略图路径；如不存在，尝试旧格式路径。

    Returns: (path_to_use_or_check, is_old_format: bool)
    """
    new_path = get_thumbnail_path(folder_type, event_id) if folder_type else None
    old_path = os.path.join(THUMBNAIL_DIR, f"{event_id}_grid.jpg")

    if new_path and os.path.exists(new_path):
        return new_path, False
    if os.path.exists(old_path):
        return old_path, True
    # 都不存在，返回新格式路径作为"应生成到哪"
    return (new_path if new_path else old_path), (new_path is None)


def should_regenerate(event_id: str, folder_type: str = None) -> bool:
    """判断是否需要（重新）生成缩略图。

    规则（所有文件夹类型统一）：
    1. 缩略图不存在 → True（必须生成）
    2. 缩略图存在且 >= 10KB → False（缓存有效，不重新生成）
    3. 缩略图存在但 < 10KB → True（文件损坏，需要重新生成）
    4. 规则 2/3 同时检查新格式和旧格式缩略图

    RecentClips 的 mtime 比较已被彻底移除。
    SentryClips/SavedClips 的 mtime 比较也被移除（事件文件夹永不回收）。
    """
    if not folder_type:
        # 无类型 → 检查所有可能的路径
        for ft in _THUMB_PREFIX:
            tn_path = get_thumbnail_path(ft, event_id)
            old_path = os.path.join(THUMBNAIL_DIR, f"{event_id}_grid.jpg")
            if os.path.exists(tn_path):
                return _is_damaged(tn_path)
            if os.path.exists(old_path):
                return _is_damaged(old_path)
        return True  # 都不存在

    tn_path, _ = _get_any_thumbnail_path(folder_type, event_id)
    if not os.path.exists(tn_path):
        return True
    return _is_damaged(tn_path)


def _is_damaged(path: str) -> bool:
    """检查缩略图文件是否损坏（< 10KB 视为损坏）。

    Returns: True 表示损坏/需要重新生成
    """
    try:
        return os.path.getsize(path) < _MIN_THUMBNAIL_SIZE
    except OSError:
        return True  # 文件异常，视为需要重新生成


def find_source_files(event_id: str, folder_type: str = None) -> tuple:
    """查找事件的源视频文件。

    Args:
        event_id: 事件 ID
        folder_type: 文件夹类型（None 表示未知，将搜索所有文件夹）

    Returns:
        (event_path, video_files)
        - event_path: 源文件路径（事件文件夹或平铺目录），未找到为 None
        - video_files: 视频文件列表（仅 RecentClips 平铺结构返回列表，其余为 None）
    """
    if folder_type:
        search_folders = [folder_type]
    else:
        search_folders = list(VIDEO_FOLDERS.keys())

    for ft in search_folders:
        if ft not in VIDEO_FOLDERS:
            continue
        folder_path = VIDEO_FOLDERS[ft]['path']
        if not os.path.isdir(folder_path):
            continue

        # 先尝试事件文件夹结构 (SentryClips/SavedClips)
        candidate = os.path.join(folder_path, event_id)
        if os.path.isdir(candidate):
            # 验证至少有一个 mp4 文件
            has_mp4 = any(
                f.lower().endswith('.mp4')
                for f in os.listdir(candidate)
                if os.path.isfile(os.path.join(candidate, f))
            )
            if has_mp4:
                return candidate, None

        # 再尝试平铺文件结构 (RecentClips)
        matching = []
        for fname in os.listdir(folder_path):
            fp = os.path.join(folder_path, fname)
            if not os.path.isfile(fp):
                continue
            if fname.startswith(event_id) and fname.lower().endswith('.mp4'):
                matching.append(fp)
        if matching:
            return folder_path, matching

    return None, None


def get_thumbnail_health() -> dict:
    """获取缩略图系统健康状态摘要。

    Returns:
        {
            'total': 总缩略图数,
            'by_type': {'SentryClips': N, 'SavedClips': N, 'RecentClips': N},
            'damaged': 异常文件数 (< 10KB),
            'old_format': 旧格式无前缀文件数,
            'total_size_mb': 总大小 (MB),
        }
    """
    if not os.path.isdir(THUMBNAIL_DIR):
        return {'total': 0, 'by_type': {}, 'damaged': 0, 'old_format': 0, 'total_size_mb': 0}

    by_type = {ft: 0 for ft in _THUMB_PREFIX}
    damaged = 0
    old_format = 0
    total_bytes = 0
    total = 0

    for fname in os.listdir(THUMBNAIL_DIR):
        fp = os.path.join(THUMBNAIL_DIR, fname)
        if not os.path.isfile(fp) or not fname.endswith('_grid.jpg'):
            continue
        total += 1
        try:
            fsize = os.path.getsize(fp)
        except OSError:
            continue
        total_bytes += fsize
        if fsize < _MIN_THUMBNAIL_SIZE:
            damaged += 1

        # 按前缀分类
        matched = False
        for ft, prefix in _THUMB_PREFIX.items():
            if fname.startswith(prefix):
                by_type[ft] += 1
                matched = True
                break
        if not matched:
            old_format += 1

    return {
        'total': total,
        'by_type': by_type,
        'damaged': damaged,
        'old_format': old_format,
        'total_size_mb': round(total_bytes / (1024 * 1024), 1),
    }
