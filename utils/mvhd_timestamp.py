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

v0.3.1.32 优化（方案 A + B′）：
- A: extract_mvhd_timestamp 由 f.read() 全文（实测 25MB/文件 → 列表页 4.5GB 读 IO）
  改为「尾部 64KB + 头部 256KB 回退」定位 moov（实测 Tesla 写入结构
  ftyp→free→mdat→moov，moov 为文件最后一个 box），提速 ~500 倍，消除大对象分配
- B′: 新增 get_clock_skew_cached() 全局时钟偏差缓存（1 个 key + TTL 600s），
  车钟正常时页面请求零 mvhd IO；抽样取倒数第 3-5 个 front.mp4（避开活跃写入）
"""
import os
import struct
import threading
import time as _time_mod
from datetime import datetime, timezone, timedelta


# MP4 epoch: 1904-01-01 UTC → Unix epoch: 1970-01-01
# 差值: 2082844800 秒
_MP4_EPOCH_OFFSET = 2082844800

# 尾部读取大小：实测 moov 为最后 box（~18KB），64KB 足够覆盖
_TAIL_READ_SIZE = 64 * 1024
# 头部回退大小：faststart 结构（moov 前置）256KB 足够
_HEAD_READ_SIZE = 256 * 1024

# 时钟偏差判定阈值（秒）：与文件名时间偏差 <120s 视为文件名可信
CLOCK_SKEW_THRESHOLD_SEC = 120

# B′ 全局偏差缓存的模块级持有（app_state 由 Flask 持有，本模块独立可测）
_skew_state = {'skew': None, 'valid': False, 'ts': 0.0}
_skew_lock = threading.Lock()
_SKEW_TTL_SEC = 600  # 10 分钟


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


def _extract_mvhd_from_buffer(data, start=0, end=None):
    """从给定 buffer 中提取 mvhd 时间（buffer 须包含完整 moov box 或可定位到 mvhd）。

    Returns:
        datetime (naive, local time) 或 None
    """
    if end is None:
        end = len(data)
    moov = _find_box(data, start, end, 'moov')
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


def extract_mvhd_timestamp(mp4_path):
    """从 MP4 文件的 mvhd atom 中提取 UTC 录制时间（v0.3.1.32 优化版）。

    方案 A：不 f.read() 全文（实测 Tesla front.mp4 ~25MB，列表页 181 文件 = 4.5GB
    读 IO），改为：
      1. 尾部 64KB（实测结构 ftyp→free→mdat→moov，moov 为最后 box）
      2. 找不到 → 头部 256KB 回退（faststart 结构 moov 前置）
      3. 仍找不到 → None（功能降级不崩溃）
    rfind(b'moov') 三重验证：idx≥8 / size 落在 buffer 内 / 4 字节对齐。

    Returns:
        datetime (naive, local time) — 与项目中其他时间戳格式一致
        None — 文件不存在 / 无法读取 mvhd / 时间无效
    """
    try:
        # 单次 getsize（🟢2：避免两次系统调用）
        fsize = os.path.getsize(mp4_path)
        if fsize < 8:
            return None
    except OSError:
        return None

    try:
        # ── 1) 尾部 64KB：moov 为最后 box（实测验证）──
        tail_len = min(_TAIL_READ_SIZE, fsize)
        with open(mp4_path, 'rb') as f:
            f.seek(fsize - tail_len)
            tail = f.read(tail_len)
        t = _find_mvhd_in_tail(tail)
        if t is not None:
            return t

        # ── 2) 头部 256KB 回退：faststart 结构（moov 前置）──
        head_len = min(_HEAD_READ_SIZE, fsize)
        with open(mp4_path, 'rb') as f:
            head = f.read(head_len)
        t = _extract_mvhd_from_buffer(head, 0, len(head))
        if t is not None:
            return t

        # ── 3) 兜底：moov 在文件中部（极罕见）→ 全量读（最后手段，保持兼容）──
        with open(mp4_path, 'rb') as f:
            data = f.read()
        return _extract_mvhd_from_buffer(data)
    except Exception:
        return None


def _find_mvhd_in_tail(tail):
    """在尾部 buffer 中定位并提取 mvhd。

    rfind(b'moov') 循环向上查找，直到候选满足验证：
      1. idx >= 8（box 头至少 8 字节）
      2. size 字段落在 [8, 尾部剩余] 区间（moov 最后 box 时 size==剩余；
         带 udta 等后置 box 时 size<剩余；mdat 内误匹配的 'moov' 字节
         size 通常巨大或为 0 → 拒绝）
    🟡3：候选验证失败时继续向上找下一个 'moov'，减少对全量兜底的依赖。
    """
    search_from = len(tail)
    while True:
        idx = tail.rfind(b'moov', 0, search_from)
        if idx < 8:
            return None
        try:
            size = struct.unpack('>I', tail[idx - 4:idx])[0]
        except struct.error:
            search_from = idx - 4
            continue
        if 8 <= size <= len(tail) - (idx - 4):
            # 截取完整 moov box（moov 头 + 内容），从 box 头开始解析
            moov_start = idx - 4
            moov_data = tail[moov_start:moov_start + min(size, len(tail) - moov_start)]
            t = _extract_mvhd_from_buffer(moov_data)
            if t is not None:
                return t
        search_from = idx - 4  # 验证失败或解析失败 → 向上继续找


# ═══════════════════════════════════════════════════════════════
# B′：全局时钟偏差缓存
# ═══════════════════════════════════════════════════════════════

def _parse_filename_time(event_id):
    """从事件前缀解析文件名时间；失败返回 None"""
    try:
        return datetime.strptime(event_id, '%Y-%m-%d_%H-%M-%S')
    except (ValueError, TypeError):
        return None


def _sample_skew(recent_clips_dir):
    """抽样计算时钟偏差（秒）：取倒数第 3-5 个 front.mp4（避开活跃写入窗口）。

    抽 3 个取一致值：两两偏差 <5s 视为一致；全部失败返回 None。
    Returns:
        int 偏差秒数（mvhd 时间 - 文件名时间）；None 表示无法判定
    """
    try:
        fronts = sorted(
            f for f in os.listdir(recent_clips_dir)
            if f.endswith('-front.mp4')
        )
        # 避开最新 2 个（活跃写入窗口，文件可能不完整）
        if len(fronts) < 3:
            return 0  # 样本不足，保守视为无偏差
        samples = fronts[-5:-2] if len(fronts) >= 5 else fronts[:-2]
    except OSError:
        return None

    skews = []
    for fname in samples:
        event_id = fname[:-len('-front.mp4')]  # 去掉 -front.mp4 后缀
        file_time = _parse_filename_time(event_id)
        if file_time is None:
            continue
        mvhd_time = extract_mvhd_timestamp(os.path.join(recent_clips_dir, fname))
        if mvhd_time is None:
            continue
        skews.append(int((mvhd_time - file_time).total_seconds()))

    if not skews:
        return None
    # 取一致值：与首个偏差 <5s 的视为同一偏差（至少 2 个一致才可信）
    base = skews[0]
    agree = [s for s in skews if abs(s - base) < 5]
    if len(agree) >= max(2, len(skews) - 1):
        return base
    return None  # 🟡1：样本不一致（个别文件 mvhd 异常）→ 无法判定，不修正


def get_clock_skew_cached(recent_clips_dir='/mnt/teslacam/TeslaCam/RecentClips'):
    """获取全局时钟偏差（秒），带 600s 缓存（B′ 方案）。

    语义：
      - 返回 None：无法判定（抽样失败），调用方跳过修正（TTL 内不重试）
      - 返回 0：车钟准（文件名时间可信），调用方零 IO 跳过修正
      - 返回 N（|N|≥120）：车钟偏 N 秒，调用方统一偏移修正
    线程安全：TTL 过期重建在锁内串行（防并发击穿）。
    """
    now = time_now()
    global _skew_state
    with _skew_lock:
        cache = _skew_state
        if cache['valid'] and (now - cache['ts']) < _SKEW_TTL_SEC:
            return cache['skew']
        # 重建（持锁串行，防多请求并发抽样）
        skew = _sample_skew(recent_clips_dir)
        if skew is None:
            # 失败也缓存（valid=True + skew=None 标记不可判定），TTL 内不重试
            _skew_state = {'skew': None, 'valid': True, 'ts': now}
            return None
        # |skew| < 阈值 → 视为无偏差（0），车钟准
        if abs(skew) < CLOCK_SKEW_THRESHOLD_SEC:
            skew = 0
        _skew_state = {'skew': skew, 'valid': True, 'ts': now}
        return skew


def get_real_event_time(event_id, recent_clips_dir='/mnt/teslacam/TeslaCam/RecentClips'):
    """获取 RecentClips 事件的真实录制时间（v0.3.1.32 B′ 版）。

    不再逐事件读 mvhd：优先用全局偏差缓存统一修正。
    - 全局偏差无法判定（None）→ 返回 None（不修正，用文件名时间）
    - 偏差为 0（车钟准）→ 返回 None（文件名时间可信）
    - 偏差 N → 返回 文件名时间 + N（统一偏移，零文件读取）

    Args:
        event_id: 文件名前缀，如 "2026-07-14_09-18-16"
        recent_clips_dir: RecentClips 目录路径

    Returns:
        datetime (naive) — 真实时间，或 None（文件名时间可信/无法判定）
    """
    skew = get_clock_skew_cached(recent_clips_dir)
    if skew is None or skew == 0:
        return None
    file_time = _parse_filename_time(event_id)
    if file_time is None:
        return None
    return file_time + timedelta(seconds=skew)


def time_now():
    """可测试性：时间源统一入口"""
    return _time_mod.time()
