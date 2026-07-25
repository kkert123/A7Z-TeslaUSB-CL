#!/usr/bin/env python3
"""
TeslaUSB A7Z — 视频管理服务
============================

从 app.py 提取的视频核心逻辑，新增 TeslaUSB-main 特性：
- MP4 文件头验证（ftyp 魔数，检测 Tesla 加密文件）
- 事件 ZIP 打包下载
- 强制下载（Content-Disposition: attachment）

架构：
  视频扫描 → 缩略图生成 → 事件管理 → 文件导出

作者：TeslaUSB A7Z 项目
版本：2.0.0（Blueprint 就绪）
"""

import io
import json
import os
import re
import struct
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ── 常量 ─────────────────────────────────────────────────

# 视频文件夹定义
VIDEO_FOLDERS = {
    'SentryClips': {'path': '/mnt/teslacam/TeslaCam/SentryClips', 'icon': '\U0001f6a8', 'desc': '\u54e8\u5175\u4e8b\u4ef6'},
    'SavedClips':  {'path': '/mnt/teslacam/TeslaCam/SavedClips',  'icon': '\u2b50', 'desc': '\u624b\u52a8\u4fdd\u5b58'},
    'RecentClips': {'path': '/mnt/teslacam/TeslaCam/RecentClips', 'icon': '\U0001f697', 'desc': '\u884c\u8f66\u8bb0\u5f55\u4eea'},
}

THUMBNAIL_DIR = '/opt/radxa_data/teslausb/static/thumbnails'
THUMBNAIL_SIZE = (320, 180)

# 缩略图文件名文件夹类型前缀（防止不同文件夹下相同时间戳的事件共用同一个缩略图）
_THUMB_PREFIX = {
    'SentryClips': 'SEN_',
    'SavedClips': 'SAV_',
    'RecentClips': 'REC_',
}

def get_thumbnail_filename(folder_type: str, event_id: str) -> str:
    """返回缩略图文件名（含文件夹类型前缀，避免命名冲突）"""
    prefix = _THUMB_PREFIX.get(folder_type, 'UNK_')
    return f"{prefix}{event_id}_grid.jpg"

def get_thumbnail_path(folder_type: str, event_id: str) -> str:
    """返回缩略图完整路径"""
    return os.path.join(THUMBNAIL_DIR, get_thumbnail_filename(folder_type, event_id))

def get_thumbnail_url(folder_type: str, event_id: str) -> str:
    """返回缩略图 URL 路径"""
    return f"/thumbnails/{get_thumbnail_filename(folder_type, event_id)}"

def infer_folder_type(event_path: str) -> str:
    """从路径推断文件夹类型
    - /mnt/.../SentryClips/event → 'SentryClips'
    - /mnt/.../RecentClips → 'RecentClips'
    """
    norm = os.path.normpath(event_path)
    parts = norm.split(os.sep)
    # 找 TeslaCam 后的第一个目录名
    try:
        idx = [p.lower() for p in parts].index('teslacam')
        if idx + 1 < len(parts):
            return parts[idx + 1]
    except ValueError:
        pass
    # 兜底：取倒数第二级（如果是事件子目录）或最后一级（如果是平铺目录）
    parent = os.path.basename(os.path.dirname(norm))
    if parent in _THUMB_PREFIX:
        return parent
    basename = os.path.basename(norm)
    if basename in _THUMB_PREFIX:
        return basename
    return 'SentryClips'  # 默认

# 字体路径
_FONT_CN = '/usr/share/fonts/truetype/custom/simhei.ttf'
_FONT_EN = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

# MP4 ftyp 魔数签名
MP4_FTYP_SIGNATURE = b'ftyp'

# 支持的视频扩展名
VIDEO_EXTENSIONS = ('.mp4',)

# ── 工具函数 ────────────────────────────────────────────


def is_valid_mp4(filepath: str) -> bool:
    """
    检查文件是否包含有效的 MP4 ftyp 头（未被 Tesla 加密）。

    Tesla 会加密 RecentClips 中的某些摄像头角度（尤其是侧摄像头），
    直到用户手动保存。这些文件虽然有 .mp4 扩展名，但无法播放。

    通过检查文件头 12 字节中是否包含 'ftyp' 魔数来判断。

    注意：在 Present 模式下，Tesla 通过 USB gadget 直接写入块设备，
    内核缓冲区可能缓存旧数据。对最近修改的文件先 sync 再读取。

    Args:
        filepath: 视频文件路径

    Returns:
        True 表示文件有有效 MP4 头，False 表示已加密或损坏
    """
    try:
        # 对最近 5 分钟内修改的文件，先尝试驱逐页缓存再读取
        mtime = os.path.getmtime(filepath)
        if time.time() - mtime < 300:
            try:
                fd = os.open(filepath, os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                except (AttributeError, OSError):
                    pass
                os.close(fd)
            except (OSError, AttributeError):
                pass

        with open(filepath, 'rb') as f:
            header = f.read(12)
            if len(header) < 12:
                return False
            return MP4_FTYP_SIGNATURE in header
    except (OSError, IOError):
        return False


def _format_size(size_bytes: Optional[int]) -> str:
    """格式化字节大小为人类可读字符串"""
    if not size_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def _to_local_time(ts_str: str) -> str:
    """
    格式化 Tesla 时间戳字符串为显示格式。

    Tesla 文件名已是本地时间（CST），无需 UTC 偏移。
    输入: '2026-05-17 22-30-37' → 输出: '2026-05-17 22:30:37'
    """
    try:
        parts = ts_str.split(' ')
        if len(parts) == 2:
            normalized = f"{parts[0]} {parts[1].replace('-', ':')}"
        else:
            normalized = ts_str.replace('-', ':')
        dt = datetime.strptime(normalized, '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ts_str


# ── 视频扫描 ────────────────────────────────────────────


def _scan_video_folder(folder_type: str) -> list:
    """
    扫描视频文件夹，返回事件列表。

    支持两种结构：
    - 事件文件夹结构（SentryClips/SavedClips）：每个子目录是一个事件
    - 平铺结构（RecentClips）：视频文件按时间戳前缀分组

    Args:
        folder_type: VIDEO_FOLDERS 中的键名

    Returns:
        事件字典列表，包含 id, name, timestamp, file_count, total_size,
        thumbnail, uploaded, nas_path, videos（仅事件结构）
    """
    if folder_type not in VIDEO_FOLDERS:
        return []

    folder_path = VIDEO_FOLDERS[folder_type]['path']
    events = []

    try:
        if not os.path.exists(folder_path):
            return events

        items = os.listdir(folder_path)
        is_flat = (folder_type == 'RecentClips')

        if is_flat:
            # 平铺结构：按文件名前缀分组（YYYY-MM-DD_HH-MM-SS）
            sessions = {}
            for fname in items:
                if not fname.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                fpath = os.path.join(folder_path, fname)
                if not os.path.isfile(fpath):
                    continue

                # 提取时间戳前缀（使用正则匹配已知摄像头后缀，避免误匹配时间戳中的 -left/-right）
                match = re.match(
                    r'^(.+?)-(front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4$',
                    fname, re.IGNORECASE
                )
                if match:
                    prefix = match.group(1)
                else:
                    # 兜底：尝试用 -front/-back/-left/-right 拆分（可能误匹配时间戳）
                    prefix = fname.split('-front')[0].split('-left_repeater')[0].split('-right_repeater')[0]
                    for cam in ('-front', '-back'):
                        if cam in prefix:
                            prefix = prefix.split(cam)[0]
                            break
                ts_parts = prefix.split('_')
                if len(ts_parts) >= 2:
                    session_id = f"{ts_parts[0]}_{ts_parts[1]}"
                else:
                    session_id = prefix[:19] if len(prefix) >= 19 else prefix

                if session_id not in sessions:
                    local_ts = _to_local_time(session_id.replace('_', ' '))
                    sessions[session_id] = {
                        'id': session_id,
                        'name': local_ts,
                        'timestamp': local_ts,
                        'file_count': 0,
                        'total_size': 0,
                        'uploaded': False,
                        'nas_path': '',
                        'thumbnail': get_thumbnail_url(folder_type, session_id),
                    }
                try:
                    fsize = os.path.getsize(fpath)
                except OSError:
                    fsize = 0
                sessions[session_id]['file_count'] += 1
                sessions[session_id]['total_size'] += fsize

            events = list(sessions.values())
            
            # ── RecentClips 防抖：跳过最新一组（Tesla 可能正在写入）──
            # 最新一组可能文件不完整或被锁定，跳过可防止"货不对板"
            if len(events) > 1:
                events.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
                latest = events[0]
                # 检查最新组是否在 2 分钟内（活跃写入窗口）
                try:
                    latest_ts = datetime.strptime(latest['id'], '%Y-%m-%d_%H-%M-%S')
                    age_sec = (datetime.now() - latest_ts).total_seconds()
                    if age_sec < 120:  # 2 分钟内视为活跃写入
                        events = events[1:]  # 跳过最新组
                except (ValueError, KeyError):
                    pass  # 无法解析时间戳则保留全部
        else:
            # 事件文件夹结构：每个子目录是一个事件
            for event_folder in sorted(items, reverse=True):
                event_path = os.path.join(folder_path, event_folder)
                if not os.path.isdir(event_path):
                    continue

                event_id = event_folder
                videos = []
                has_valid_videos = False

                try:
                    for vf in os.listdir(event_path):
                        vpath = os.path.join(event_path, vf)
                        if os.path.isfile(vpath) and vf.lower().endswith(VIDEO_EXTENSIONS):
                            try:
                                fsize = os.path.getsize(vpath)
                            except OSError:
                                fsize = 0
                            valid = is_valid_mp4(vpath)
                            videos.append({
                                'name': vf,
                                'size': fsize,
                                'size_fmt': _format_size(fsize),
                                'valid': valid,
                            })
                            if valid:
                                has_valid_videos = True
                except OSError:
                    pass

                if not videos:
                    continue

                total_size = sum(v['size'] for v in videos)
                local_ts = _to_local_time(event_folder.replace('_', ' '))

                events.append({
                    'id': event_id,
                    'name': local_ts,
                    'timestamp': local_ts,
                    'file_count': len(videos),
                    'total_size': total_size,
                    'size_fmt': _format_size(total_size),
                    'uploaded': False,
                    'nas_path': '',
                    'thumbnail': get_thumbnail_url(folder_type, event_id),
                    'videos': videos,
                    'has_valid': has_valid_videos,
                })

        # 按时间戳倒序
        events.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    except OSError as e:
        # 区分目录损坏 (EIO) vs 权限/不存在等普通错误
        import errno as _errno
        err_code = getattr(e, 'errno', 0)
        if err_code == _errno.EIO:
            print(f"[VideoScan] ❌ 目录损坏 {folder_type} (I/O 错误 — 需运行 fsck.exfat 修复): {e}")
        elif err_code == _errno.ENOENT:
            print(f"[VideoScan] 目录不存在 {folder_type}: {e}")
        else:
            print(f"[VideoScan] 扫描失败 {folder_type} (OSError {err_code}): {e}")
    except Exception as e:
        print(f"[VideoScan] 扫描失败 {folder_type}: {e}")

    # ── 上传状态判定：根据云同步历史标记已上传事件 ──
    _mark_uploaded_events(events, folder_type)

    # ── 过滤 staging 中待删除的事件 ──
    _filter_pending_deletes(events, folder_type)

    return events


def check_teslacam_health() -> dict:
    """检查 TeslaCam 文件系统健康状态。
    
    检测每个视频子目录是否可访问。exFAT 元数据损坏的目录会导致
    os.listdir() 抛出 OSError (errno 5 = EIO)。

    Returns:
        {
            'healthy': bool,
            'corrupted_dirs': [str],      # 损坏的目录名列表
            'accessible_dirs': [str],      # 可访问的目录名列表
            'missing_dirs': [str],         # 不存在的目录名列表
            'details': [{folder, path, accessible, error}]
        }
    """
    import errno as _errno

    result = {
        'healthy': True,
        'corrupted_dirs': [],
        'accessible_dirs': [],
        'missing_dirs': [],
        'details': []
    }

    for ft, info in VIDEO_FOLDERS.items():
        folder_path = info['path']
        detail = {
            'folder': ft,
            'path': folder_path,
            'accessible': False,
            'error': None
        }

        if not os.path.exists(folder_path):
            # ═══ exFAT 特殊性：损坏的目录条目 os.path.exists() 返回 False ═══
            # 但父目录的 readdir 仍能看到它（d?????????）。通过检查
            # 父目录列表来区分"真正缺失"和"目录条目存在但不可 stat"
            parent_dir = os.path.dirname(folder_path)
            dir_name = os.path.basename(folder_path)
            is_corrupted = False
            if os.path.isdir(parent_dir):
                try:
                    parent_items = os.listdir(parent_dir)
                    if dir_name in parent_items:
                        is_corrupted = True
                except OSError:
                    pass  # 父目录本身不可读

            if is_corrupted:
                detail['error'] = '目录损坏 (条目存在但不可访问 — 需运行 fsck.exfat 修复)'
                result['healthy'] = False
                result['corrupted_dirs'].append(ft)
            else:
                detail['error'] = '目录不存在'
                result['healthy'] = False
                result['missing_dirs'].append(ft)
            result['details'].append(detail)
            continue

        try:
            # 尝试列出目录内容 — 损坏的 exFAT 目录会在这里抛 OSError(EIO)
            os.listdir(folder_path)
            detail['accessible'] = True
            result['accessible_dirs'].append(ft)
        except OSError as e:
            err_code = getattr(e, 'errno', 0)
            if err_code == _errno.EIO:
                detail['error'] = f'目录损坏 (I/O 错误 — 需运行 fsck.exfat 修复): {e}'
            else:
                detail['error'] = f'OSError({err_code}): {e}'
            result['healthy'] = False
            result['corrupted_dirs'].append(ft)
        except Exception as e:
            detail['error'] = str(e)
            result['healthy'] = False
            result['corrupted_dirs'].append(ft)

        result['details'].append(detail)

    return result


def _mark_uploaded_events(events: list, folder_type: str):
    """根据云同步历史，标记事件的上传状态。
    
    检查最近一次成功的同步记录，同步时间之前的事件标记为已上传。
    """
    if not events:
        return
    try:
        from cloud_archive_service import get_sync_history
        
        # 获取最近一次成功的同步时间
        sync_history = get_sync_history(limit=50)
        last_sync_time = None
        for entry in sync_history:
            if entry.get('success'):
                ts_str = entry.get('time', '')
                try:
                    if 'T' in ts_str:
                        last_sync_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    else:
                        last_sync_time = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                    break
                except (ValueError, TypeError):
                    pass
        
        if not last_sync_time:
            return  # 从未成功同步过，全部未上传
        
        # 根据文件夹路径计算事件 mtime
        folder_path = VIDEO_FOLDERS.get(folder_type, {}).get('path', '')
        if not folder_path or not os.path.isdir(folder_path):
            return
        
        for event in events:
            event_path = os.path.join(folder_path, event['id'])
            try:
                if os.path.exists(event_path):
                    mtime = os.path.getmtime(event_path)
                    event['uploaded'] = (mtime <= last_sync_time.timestamp())
                else:
                    # RecentClips 平铺结构：检查任意代表性文件
                    # 尝试多个摄像头后缀，front 优先
                    for suffix in ('-front.mp4', '-back.mp4', '-left_repeater.mp4',
                                   '-right_repeater.mp4', '-left_pillar.mp4', '-right_pillar.mp4'):
                        rep_file = os.path.join(folder_path, f"{event['id']}{suffix}")
                        if os.path.exists(rep_file):
                            mtime = os.path.getmtime(rep_file)
                            event['uploaded'] = (mtime <= last_sync_time.timestamp())
                            break
            except OSError:
                pass
    except Exception:
        pass  # 云服务不可用时保持 uploaded=False


def _filter_pending_deletes(events: list, folder_type: str):
    """过滤 staging 中已标记删除的事件（从列表中移除）。
    
    调用的时机：每次扫描视频文件夹后，在返回事件列表前过滤。
    只在 Present 模式下生效（Edit 模式时 sync_all 已清空 manifest）。
    """
    if not events:
        return
    try:
        from staging_service import get_pending_video_deletes
        pending = get_pending_video_deletes()
        if not pending:
            return
        # 构建待删除集合（folder_type: {event_ids}）
        pending_set = set(eid for ft, eid in pending if ft == folder_type)
        if pending_set:
            before = len(events)
            events[:] = [e for e in events if e['id'] not in pending_set]
            after = len(events)
            if before != after:
                print(f"[VideoScan] 过滤待删除事件 {folder_type}: {before - after} 个")
    except Exception:
        pass  # staging 服务不可用时不过滤


def get_folders() -> dict:
    """返回视频文件夹定义（供视频页面下拉选择器使用）"""
    return VIDEO_FOLDERS


def get_video_stats(folder_type: str) -> dict:
    """获取指定文件夹类型的统计信息"""
    events = _scan_video_folder(folder_type)
    total_events = len(events)
    uploaded_count = sum(1 for e in events if e.get('uploaded'))
    total_size = sum(e.get('total_size', 0) for e in events)
    return {
        'total_events': total_events,
        'uploaded_count': uploaded_count,
        'total_size': _format_size(total_size),
    }


# ── 事件详情 ────────────────────────────────────────────


def get_event_files(folder_type: str, event_id: str) -> List[dict]:
    """
    获取事件中的所有视频文件详情。

    Args:
        folder_type: 文件夹类型键名
        event_id: 事件 ID

    Returns:
        文件字典列表，包含 name, size, size_fmt, valid, path
    """
    if folder_type not in VIDEO_FOLDERS:
        return []

    folder_path = VIDEO_FOLDERS[folder_type]['path']
    videos = []

    try:
        if folder_type == 'RecentClips':
            # 平铺结构：列出匹配前缀的文件
            if os.path.exists(folder_path):
                for fname in sorted(os.listdir(folder_path)):
                    if fname.startswith(event_id) and fname.lower().endswith(VIDEO_EXTENSIONS):
                        fpath = os.path.join(folder_path, fname)
                        try:
                            fsize = os.path.getsize(fpath)
                        except OSError:
                            fsize = 0
                        videos.append({
                            'name': fname,
                            'size': fsize,
                            'size_fmt': _format_size(fsize),
                            'valid': is_valid_mp4(fpath),
                            'path': f'/videos/play/{folder_type}/{fname}',
                            'dl_path': f'/videos/download/{folder_type}/{fname}',
                        })
        else:
            # 事件文件夹结构
            event_path = os.path.join(folder_path, event_id)
            if os.path.exists(event_path) and os.path.isdir(event_path):
                for fname in sorted(os.listdir(event_path)):
                    if fname.lower().endswith(VIDEO_EXTENSIONS):
                        fpath = os.path.join(event_path, fname)
                        try:
                            fsize = os.path.getsize(fpath)
                        except OSError:
                            fsize = 0
                        videos.append({
                            'name': fname,
                            'size': fsize,
                            'size_fmt': _format_size(fsize),
                            'valid': is_valid_mp4(fpath),
                            'path': f'/videos/play/{folder_type}/{event_id}/{fname}',
                            'dl_path': f'/videos/download/{folder_type}/{event_id}/{fname}',
                        })
    except OSError:
        pass

    return videos


# ── ZIP 打包下载 ────────────────────────────────────────


def create_event_zip(folder_type: str, event_id: str) -> Tuple[Optional[bytes], str]:
    """
    将事件中的所有有效视频文件打包为 ZIP。

    只包含通过 is_valid_mp4() 验证的文件（排除 Tesla 加密文件）。
    保留原始文件名和目录结构。

    Args:
        folder_type: 文件夹类型键名
        event_id: 事件 ID

    Returns:
        (zip_bytes, filename) — zip_bytes 为 None 表示失败
    """
    if folder_type not in VIDEO_FOLDERS:
        return None, ''

    folder_path = VIDEO_FOLDERS[folder_type]['path']
    zip_buffer = io.BytesIO()
    file_count = 0

    try:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            if folder_type == 'RecentClips':
                # 平铺结构
                if os.path.exists(folder_path):
                    for fname in sorted(os.listdir(folder_path)):
                        if fname.startswith(event_id) and fname.lower().endswith(VIDEO_EXTENSIONS):
                            fpath = os.path.join(folder_path, fname)
                            if is_valid_mp4(fpath):
                                zf.write(fpath, fname)
                                file_count += 1
            else:
                # 事件文件夹结构
                event_path = os.path.join(folder_path, event_id)
                if os.path.exists(event_path) and os.path.isdir(event_path):
                    for fname in sorted(os.listdir(event_path)):
                        if fname.lower().endswith(VIDEO_EXTENSIONS):
                            fpath = os.path.join(event_path, fname)
                            if is_valid_mp4(fpath):
                                # 保留原始文件名
                                arcname = f"{event_id}/{fname}" if not folder_type == 'RecentClips' else fname
                                zf.write(fpath, arcname)
                                file_count += 1

                    # 也打包 event.json（如果存在）
                    json_path = os.path.join(event_path, 'event.json')
                    if os.path.exists(json_path) and os.path.isfile(json_path):
                        try:
                            with open(json_path, 'r') as f:
                                json.load(f)  # 验证是合法 JSON
                            zf.write(json_path, f"{event_id}/event.json")
                        except (OSError, json.JSONDecodeError):
                            pass

        if file_count == 0:
            return None, ''

        zip_data = zip_buffer.getvalue()
        safe_name = re.sub(r'[^\w\-.]', '_', event_id)
        filename = f"{folder_type}_{safe_name}.zip"

        return zip_data, filename

    except Exception as e:
        print(f"[ZIP] \u6253\u5305\u5931\u8d25 {event_id}: {e}")
        return None, ''


# ── 缩略图生成 ──────────────────────────────────────────


def _generate_thumbnail(event_path: str, event_id: str,
                        video_files: Optional[List[str]] = None) -> Optional[str]:
    """
    生成四宫格缩略图：2x2 (前/后+左/右) + 摄像头标签 + 时间水印。

    参考 TeslaUSB-CL video_preview.py generate_sentry_grid_preview()

    Args:
        event_path: 事件文件夹路径（或 RecentClips 平铺目录）
        event_id: 事件ID
        video_files: 可选，直接指定视频文件列表（用于 RecentClips 平铺结构）

    Returns:
        缩略图 URL 路径，失败返回 None
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[Thumbnail] PIL \u672a\u5b89\u88c5")
        return None

    if not os.path.exists(THUMBNAIL_DIR):
        os.makedirs(THUMBNAIL_DIR, exist_ok=True)

    folder_type = infer_folder_type(event_path)
    thumbnail_file = get_thumbnail_path(folder_type, event_id)

    # 缓存检查
    if os.path.exists(thumbnail_file) and video_files is None:
        try:
            newest_mtime = 0
            for f in os.listdir(event_path):
                fp = os.path.join(event_path, f)
                if os.path.isfile(fp) and f.lower().endswith(VIDEO_EXTENSIONS):
                    newest_mtime = max(newest_mtime, os.path.getmtime(fp))
            if newest_mtime > 0 and os.path.getmtime(thumbnail_file) >= newest_mtime:
                return get_thumbnail_url(folder_type, event_id)
        except OSError:
            pass

    # 1) 读取 event.json 获取时间戳
    key_timestamp = None
    event_json_path = os.path.join(event_path, 'event.json')
    if os.path.exists(event_json_path):
        try:
            with open(event_json_path, 'r') as f:
                event_data = json.load(f)
            ts_str = event_data.get('timestamp')
            if ts_str:
                key_timestamp = datetime.fromisoformat(ts_str)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    if not key_timestamp:
        try:
            ts_str = event_id.replace('_', ' ')[:19]
            key_timestamp = datetime.strptime(ts_str, '%Y-%m-%d %H-%M-%S')
        except ValueError:
            key_timestamp = datetime.now()

    # 2) 解析文件夹名获取视频起始时间
    folder_name = os.path.basename(event_path)
    video_start = None
    try:
        ts_str = folder_name.replace('_', ' ')[:19]
        video_start = datetime.strptime(ts_str, '%Y-%m-%d %H-%M-%S')
    except ValueError:
        pass

    if not video_start:
        try:
            ts_str = event_id.replace('_', ' ')[:19]
            video_start = datetime.strptime(ts_str, '%Y-%m-%d %H-%M-%S')
        except ValueError:
            pass

    # 3) 加载字体
    try:
        font_cn = ImageFont.truetype(_FONT_CN, 24)
    except Exception:
        font_cn = ImageFont.load_default()
    try:
        font_time = ImageFont.truetype(_FONT_EN, 36)
    except Exception:
        font_time = font_cn

    # 4) 四个摄像头配置
    camera_map = {
        'front': ('\u524d\u6444\u50cf\u5934', False),
        'back':  ('\u540e\u6444\u50cf\u5934', False),
        'left':  ('\u5de6\u6444\u50cf\u5934', True),
        'right': ('\u53f3\u6444\u50cf\u5934', True),
    }

    frames = {}

    for cam_key, (cam_label, need_flip) in camera_map.items():
        video_path = None

        if video_files:
            for vf in video_files:
                fname = os.path.basename(vf)
                fname_lower = fname.lower()
                # 严格过滤：只使用属于当前 event_id 的文件
                if not fname.startswith(event_id):
                    continue
                if f'-{cam_key}' in fname_lower:
                    video_path = vf
                    break
                if cam_key in ('left', 'right') and f'-{cam_key}_repeater' in fname_lower:
                    video_path = vf
                    break
        else:
            try:
                is_flat_dir = os.path.basename(event_path) in ('RecentClips', 'SavedClips')
                for fname in sorted(os.listdir(event_path)):
                    if fname.lower().endswith(VIDEO_EXTENSIONS):
                        if is_flat_dir and not fname.startswith(event_id):
                            continue
                        if f'-{cam_key}' in fname.lower():
                            video_path = os.path.join(event_path, fname)
                            break
                        if cam_key in ('left', 'right') and f'-{cam_key}_repeater' in fname.lower():
                            video_path = os.path.join(event_path, fname)
                            break
            except OSError:
                continue

        if not video_path:
            continue

        # 计算时间偏移
        time_offset = 3.0
        if video_start and key_timestamp:
            delta = (key_timestamp - video_start).total_seconds()
            if 0 < delta < 60:
                time_offset = delta

        # ffmpeg 提取帧
        frame_img = None
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix='.jpg')
            os.close(tmp_fd)

            cmd = [
                'ffmpeg', '-y',
                '-ss', str(time_offset),
                '-i', video_path,
                '-vframes', '1',
                '-q:v', '5',
                '-pix_fmt', 'yuvj420p',
                tmp_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
            if proc.returncode == 0 and os.path.exists(tmp_path):
                frame_img = Image.open(tmp_path)
                if need_flip:
                    frame_img = frame_img.transpose(Image.FLIP_LEFT_RIGHT)
                frames[cam_key] = (frame_img, cam_label)
        except Exception as e:
            print(f"[Thumbnail] \u63d0\u53d6 {cam_key} \u5e27\u5931\u8d25: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    if not frames:
        return None

    # 5) 构建四宫格
    try:
        first_frame = list(frames.values())[0][0]
        cell_w, cell_h = first_frame.size
        gap = 4

        grid_w = cell_w * 2 + gap
        grid_h = cell_h * 2 + gap

        grid = Image.new('RGB', (grid_w, grid_h), (30, 30, 30))
        draw = ImageDraw.Draw(grid)

        grid_layout = [
            [('front', 0, 0), ('back', cell_w + gap, 0)],
            [('left', 0, cell_h + gap), ('right', cell_w + gap, cell_h + gap)],
        ]

        for row in grid_layout:
            for cam_key, x, y in row:
                if cam_key in frames:
                    frame_img, cam_label = frames[cam_key]
                    if frame_img.size != (cell_w, cell_h):
                        frame_img = frame_img.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
                    grid.paste(frame_img, (x, y))

                    label_font_size = max(24, cell_h // 25)
                    try:
                        label_font = ImageFont.truetype(_FONT_CN, label_font_size)
                    except Exception:
                        label_font = font_cn

                    bbox = draw.textbbox((0, 0), cam_label, font=label_font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    pad = 8
                    lx, ly = x + 10, y + 10

                    overlay = Image.new('RGBA', grid.size, (0, 0, 0, 0))
                    overlay_draw = ImageDraw.Draw(overlay)
                    overlay_draw.rectangle(
                        [lx, ly, lx + tw + pad * 2, ly + th + pad * 2],
                        fill=(0, 0, 0, 160),
                    )
                    grid = Image.alpha_composite(grid.convert('RGBA'), overlay)
                    draw = ImageDraw.Draw(grid)
                    draw.text((lx + pad, ly + pad), cam_label, fill=(255, 255, 255), font=label_font)
                else:
                    draw.rectangle([x, y, x + cell_w, y + cell_h], fill=(50, 50, 50))
                    draw.text((x + cell_w // 2 - 30, y + cell_h // 2 - 14),
                              'N/A', fill=(120, 120, 120), font=font_cn)

        # 6) 缩放到目标宽度
        target_w = 1000
        target_h = int(grid_h * target_w / grid_w)
        grid = grid.resize((target_w, target_h), Image.Resampling.LANCZOS)

        # 7) 右下角时间水印
        draw = ImageDraw.Draw(grid)
        time_str = key_timestamp.strftime('%Y-%m-%d %H:%M:%S')

        bbox = draw.textbbox((0, 0), time_str, font=font_time)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 10
        margin = 16
        wm_w, wm_h = tw + pad * 2, th + pad * 2
        wm_x, wm_y = target_w - wm_w - margin, target_h - wm_h - margin

        overlay = Image.new('RGBA', grid.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle([wm_x, wm_y, wm_x + wm_w, wm_y + wm_h], fill=(0, 0, 0, 170))
        grid = Image.alpha_composite(grid.convert('RGBA'), overlay)
        draw = ImageDraw.Draw(grid)
        draw.text((wm_x + pad, wm_y + pad), time_str, fill=(255, 255, 255), font=font_time)

        # 8) 保存
        grid_rgb = grid.convert('RGB')
        grid_rgb.save(thumbnail_file, 'JPEG', quality=82)

        size_kb = os.path.getsize(thumbnail_file) // 1024
        print(f"[Thumbnail] {event_id} \u56db\u5bab\u683c\u751f\u6210\u5b8c\u6210 ({size_kb}KB)")

        return get_thumbnail_url(folder_type, event_id)

    except Exception as e:
        print(f"[Thumbnail] \u56db\u5bab\u683c\u751f\u6210\u5931\u8d25 {event_id}: {e}")
        return None


def serve_thumbnail(event_id: str) -> Optional[str]:
    """
    获取已有缩略图的 URL 路径（不触发生成）。
    兼容新旧格式：优先返回新格式（含文件夹前缀），否则返回旧格式。
    """
    # 先检查新格式（含文件夹前缀）
    for ft in _THUMB_PREFIX:
        path = get_thumbnail_path(ft, event_id)
        if os.path.exists(path):
            return get_thumbnail_url(ft, event_id)
    # 兼容旧格式
    old_path = os.path.join(THUMBNAIL_DIR, f"{event_id}_grid.jpg")
    if os.path.exists(old_path):
        return f"/thumbnails/{event_id}_grid.jpg"
    return None


# ── 事件摄像头角度检测（Event Player 支持）──────────────

# Tesla 摄像头角度定义
CAMERA_ANGLES = ('front', 'back', 'left_repeater', 'right_repeater',
                 'left_pillar', 'right_pillar')

CAMERA_LABELS = {
    'front': '前视', 'back': '后视',
    'left_repeater': '左后视', 'right_repeater': '右后视',
    'left_pillar': '左B柱', 'right_pillar': '右B柱',
}

CAMERA_ICONS = {
    'front': '\u2b06\ufe0f',      # ⬆️
    'back': '\u2b07\ufe0f',       # ⬇️
    'left_repeater': '\u2b05\ufe0f',  # ⬅️
    'right_repeater': '\u27a1\ufe0f',  # ➡️
    'left_pillar': '\u2199\ufe0f',  # ↙️
    'right_pillar': '\u2198\ufe0f',  # ↘️
}


def get_event_cameras(folder_type: str, event_id: str) -> Optional[dict]:
    """
    解析事件文件夹，按摄像头角度分组视频文件。

    用于 Event Player 多摄像头切换播放。

    Args:
        folder_type: 文件夹类型（SentryClips/SavedClips/RecentClips）
        event_id: 事件 ID

    Returns:
        {
            'name': event_id,
            'datetime': 格式化时间戳,
            'folder': folder_type,
            'camera_videos': {angle: filename, ...},
            'encrypted_videos': {angle: True, ...},
            'clips': [...],        # SavedClips 多时间戳片段
            'size_mb': 总大小 MB,
            'city': 来自 event.json 的城市,
            'reason': 事件原因,
        }
        如果事件不存在或无视频返回 None
    """
    if folder_type not in VIDEO_FOLDERS:
        return None

    folder_path = VIDEO_FOLDERS[folder_type]['path']

    if folder_type == 'RecentClips':
        return _cameras_from_flat(folder_path, event_id)
    else:
        return _cameras_from_event(folder_path, event_id, folder_type)


def _empty_camera_dict():
    return {angle: None for angle in CAMERA_ANGLES}


def _cameras_from_event(folder_path: str, event_id: str,
                        folder_type: str) -> Optional[dict]:
    """从事件文件夹结构（SentryClips/SavedClips）解析摄像头角度"""
    import json
    event_path = os.path.join(folder_path, event_id)
    if not os.path.isdir(event_path):
        return None

    # 解析 event.json
    event_meta = {}
    event_json_path = os.path.join(event_path, 'event.json')
    if os.path.exists(event_json_path):
        try:
            with open(event_json_path, 'rb') as f:
                raw = f.read()
            if raw.startswith(b'{'):
                event_meta = json.loads(raw.decode('utf-8', errors='replace'))
        except Exception:
            pass

    camera_videos = _empty_camera_dict()
    encrypted_videos = {}
    total_size = 0
    clips = []

    try:
        for entry in os.scandir(event_path):
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(('.mp4',)):
                continue

            try:
                fsize = entry.stat().st_size
            except OSError:
                fsize = 0
            total_size += fsize

            name_lower = entry.name.lower()
            for angle in CAMERA_ANGLES:
                if angle in name_lower and camera_videos[angle] is None:
                    camera_videos[angle] = entry.name
                    if not is_valid_mp4(entry.path):
                        encrypted_videos[angle] = True
                    break
    except OSError:
        pass

    if not any(camera_videos.values()):
        return None

    # SavedClips/SentryClips: 多时间戳片段
    if folder_type in ('SavedClips', 'SentryClips'):
        clips = _parse_clips_from_event(event_path)

    # 格式化时间
    local_ts = _to_local_time(event_id.replace('_', ' '))

    return {
        'name': event_id,
        'datetime': local_ts,
        'folder': folder_type,
        'camera_videos': camera_videos,
        'encrypted_videos': encrypted_videos,
        'clips': clips,
        'size_mb': round(total_size / (1024 * 1024), 2),
        'city': event_meta.get('city', ''),
        'reason': event_meta.get('reason', ''),
    }


def _cameras_from_flat(folder_path: str, event_id: str) -> Optional[dict]:
    """从平铺结构（RecentClips）扫描匹配前缀的文件"""
    if not os.path.isdir(folder_path):
        return None

    camera_videos = _empty_camera_dict()
    encrypted_videos = {}
    total_size = 0

    try:
        for entry in os.scandir(folder_path):
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(('.mp4',)):
                continue
            if not entry.name.startswith(event_id):
                continue

            try:
                fsize = entry.stat().st_size
            except OSError:
                fsize = 0
            total_size += fsize

            name_lower = entry.name.lower()
            for angle in CAMERA_ANGLES:
                if angle in name_lower and camera_videos[angle] is None:
                    camera_videos[angle] = entry.name
                    if not is_valid_mp4(entry.path):
                        encrypted_videos[angle] = True
                    break
    except OSError:
        pass

    if not any(camera_videos.values()):
        return None

    local_ts = _to_local_time(event_id.replace('_', ' '))

    return {
        'name': event_id,
        'datetime': local_ts,
        'folder': 'RecentClips',
        'camera_videos': camera_videos,
        'encrypted_videos': encrypted_videos,
        'clips': [],
        'size_mb': round(total_size / (1024 * 1024), 2),
        'city': '',
        'reason': '',
    }


def _parse_clips_from_event(event_path: str) -> list:
    """
    解析 SavedClips 中多时间戳的片段。
    返回按时间排序的片段列表，每个片段包含各摄像头角度视频。
    """
    clips_by_ts = {}
    try:
        for entry in os.scandir(event_path):
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(('.mp4',)):
                continue

            name_lower = entry.name.lower()
            # 提取时间戳前缀: YYYY-MM-DD_HH-MM-SS
            prefix = entry.name.split('-front')[0].split('-back')[0]
            prefix = prefix.split('-left')[0].split('-right')[0]
            ts_str = prefix.strip()

            if ts_str not in clips_by_ts:
                try:
                    dt = datetime.strptime(ts_str, '%Y-%m-%d_%H-%M-%S')
                except ValueError:
                    continue
                clips_by_ts[ts_str] = {
                    'timestamp': dt.timestamp(),
                    'datetime': dt.strftime('%Y-%m-%d %H:%M:%S'),
                    'camera_videos': _empty_camera_dict(),
                    'encrypted_videos': {},
                }

            for angle in CAMERA_ANGLES:
                if angle in name_lower and clips_by_ts[ts_str]['camera_videos'][angle] is None:
                    clips_by_ts[ts_str]['camera_videos'][angle] = entry.name
                    full_path = os.path.join(event_path, entry.name)
                    if not is_valid_mp4(full_path):
                        clips_by_ts[ts_str]['encrypted_videos'][angle] = True
                    break
    except OSError:
        pass

    clips = sorted(clips_by_ts.values(), key=lambda c: c['timestamp'])
    return clips
