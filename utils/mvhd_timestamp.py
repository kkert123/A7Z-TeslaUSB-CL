"""MP4 mvhd 时间戳提取 — 修正特斯拉车载时钟偏差

Tesla 写入的 MP4 文件在 mvhd (Movie Header) atom 中包含 GPS 校准的
UTC 录制时间。这个时间独立于车载本地时钟，是视频真实录制时间的唯一
可靠来源。

当车载时钟错误时（文件名的时间戳与实际录制时间不符），本模块从 mvhd
中提取真实时间，用于：
- 事件时间显示修正
- 缩略图文件名水印修正
- 视频排序修正

移植自 TeslaUSB-main 的 services/sei_parser.py + clock_skew_repair.py
"""
import os
import struct
from datetime import datetime, timezone


# MP4 epoch: 1904-01-01 UTC → Unix epoch: 1970-01-01
# 差值: 2082844800 秒
_MP4_EPOCH_OFFSET = 2082844800


def _find_box(data, start, end, name):
    """在字节范围内查找 MP4 box（4 字符名称）"""
    name_bytes = name.encode('ascii')
    pos = start
    while pos + 8 <= end:
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
            header_size = 16
        elif size == 0:
            size = end - pos
            header_size = 8
        else:
            header_size = 8
        if size < header_size:
            break
        if pos + size > end:
            if box_type == name_bytes:
                size = end - pos
            else:
                break
        if box_type == name_bytes:
            return {'start': pos + header_size, 'end': pos + size, 'size': size - header_size}
        pos += size
    return None


def extract_mvhd_timestamp(mp4_path):
    """从 MP4 文件的 mvhd atom 中提取 UTC 录制时间。

    Returns:
        datetime (naive, local time) — 与项目中其他时间戳格式一致
        None — 文件不存在 / 无法读取 mvhd / 时间无效
    """
    try:
        if not os.path.isfile(mp4_path) or os.path.getsize(mp4_path) < 8:
            return None
    except OSError:
        return None

    try:
        with open(mp4_path, 'rb') as f:
            data = f.read()

        moov = _find_box(data, 0, len(data), 'moov')
        if moov is None:
            return None
        mvhd = _find_box(data, moov['start'], moov['end'], 'mvhd')
        if mvhd is None or mvhd['size'] < 4:
            return None

        version = data[mvhd['start']]
        if version == 1:
            if mvhd['size'] < 4 + 16:
                return None
            creation_time = struct.unpack('>Q', data[mvhd['start'] + 4:mvhd['start'] + 12])[0]
        else:
            if mvhd['size'] < 4 + 8:
                return None
            creation_time = struct.unpack('>I', data[mvhd['start'] + 4:mvhd['start'] + 8])[0]

        if creation_time <= _MP4_EPOCH_OFFSET:
            return None  # 无效值（1970 年前）

        unix_seconds = creation_time - _MP4_EPOCH_OFFSET
        utc_dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
        # 转为本地无时区时间（与项目中其他时间戳一致）
        return utc_dt.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def get_real_event_time(event_id, recent_clips_dir='/mnt/teslacam/TeslaCam/RecentClips'):
    """获取 RecentClips 事件的真实录制时间。

    优先从 front.mp4 的 mvhd 中读取。如果 mvhd 时间与文件名时间
    偏差超过 120 秒，返回 mvhd 时间；否则返回 None（使用文件名时间即可）。

    Args:
        event_id: 文件名前缀，如 "2026-07-14_09-18-16"
        recent_clips_dir: RecentClips 目录路径

    Returns:
        datetime (naive) — 真实时间，或 None（表示文件名时间可信）
    """
    front_mp4 = os.path.join(recent_clips_dir, f"{event_id}-front.mp4")
    mvhd_time = extract_mvhd_timestamp(front_mp4)
    if mvhd_time is None:
        return None

    # 从 event_id 解析文件名时间
    try:
        filename_time = datetime.strptime(event_id, '%Y-%m-%d_%H-%M-%S')
    except ValueError:
        return mvhd_time  # 无法解析文件名，直接信任 mvhd

    # 偏差超过 2 分钟 → 文件名时间不可信
    if abs((mvhd_time - filename_time).total_seconds()) > 120:
        return mvhd_time

    return None  # 文件名时间可信
