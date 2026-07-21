#!/usr/bin/env python3
"""
TeslaUSB CL — 后台预览图生成器 (CPU 自适应)
=============================================
在 CPU 空闲时主动扫描并生成视频预览图，避免用户打开视频页面时等待。

源自 TeslaUSB-CL (D:\teslausb\a2) 的 preview_generator.py，适配 A7Z 路径。

策略：
- CPU ≤60%: 开始处理队列（A7Z 空闲基线约 50-65%）
- CPU ≥85%: 暂停处理，等待 10 秒
- 每 30 秒扫描一次目录（无论 CPU 是否空闲，保证新事件被发现）
- 每个事件最多重试 3 次
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from utils.thumbnail_decision import should_regenerate, find_source_files

# ── MP4 文件有效性检查 ──
MP4_FTYP_SIGNATURE = b'ftyp'

def _is_valid_mp4(filepath):
    """检查 MP4 文件头是否包含 ftyp 魔数（未加密/未损坏）。
    
    在 Present 模式下，Tesla 通过 USB gadget 直接写入块设备，
    内核缓冲区可能缓存旧数据。对最近修改的文件先驱逐页缓存再读取。
    """
    try:
        # 对最近 5 分钟内修改的文件，先尝试驱逐页缓存
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
            return len(header) >= 12 and MP4_FTYP_SIGNATURE in header
    except (OSError, IOError):
        return False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# ── 路径配置 ──
BASE_CAM_PATH = Path('/mnt/teslacam/TeslaCam')
QUEUE_FILE = Path('/opt/radxa_data/teslausb/data/preview_queue.json')
BLOCKED_FILE = Path('/opt/radxa_data/teslausb/data/preview_blocked.json')
THUMB_DIR = Path('/opt/radxa_data/teslausb/static/thumbnails')
PREVIEW_DIR = Path('/opt/teslausb-web/data/previews')

# ── CPU 阈值 ──
CPU_START_THRESHOLD = 60.0   # CPU 低于此值才开始生成（A7Z 空闲基线约 50-65%）
CPU_STOP_THRESHOLD = 85.0    # CPU 超过此值暂停生成

# ── 间隔 ──
CPU_CHECK_INTERVAL = 10
QUEUE_EMPTY_INTERVAL = 30
EVENT_GAP_INTERVAL = 10       # 事件间休息 10 秒（降低 CPU 毛刺）
MAX_RETRIES = 3
PENDING_THRESHOLD = 10        # 队列积压阈值：累计 N 个才触发生成（批量模式）
MAX_BATCH_PER_CYCLE = 1       # 每个扫描周期最多处理 N 个（避免一次处理太多）
REC_MAX_PER_SCAN = 5          # RecentClips 快速通道：每次扫描最多直接生成 N 张
REC_THROTTLE_SEC = 1          # REC 快速通道事件间隔（秒）
MIN_THUMBNAIL_SIZE = 10240    # 有效缩略图最小字节数（<10KB 视为损坏，可重新生成）

logger = logging.getLogger("BgPreviewGenerator")
logger.setLevel(logging.INFO)


# ═══════════════════════════════════════════════════════════════
# CPUMonitor
# ═══════════════════════════════════════════════════════════════

class CPUMonitor:
    """CPU 使用率监控，控制生成节奏。单次采样避免重复阻塞"""

    def __init__(self, start_threshold=CPU_START_THRESHOLD, stop_threshold=CPU_STOP_THRESHOLD):
        self.start_threshold = start_threshold
        self.stop_threshold = stop_threshold
        self.is_available = PSUTIL_AVAILABLE
        if not self.is_available:
            logger.warning("psutil 未安装，CPU 检测不可用，将始终允许工作")

    def sample(self) -> float:
        """单次采样 CPU 使用率，阻塞 1 秒"""
        if not self.is_available:
            return 0.0
        try:
            return psutil.cpu_percent(interval=1)
        except Exception:
            return 0.0

    def is_idle_for(self, cpu_pct: float) -> bool:
        """CPU 是否足够空闲以开始工作"""
        if not self.is_available:
            return True
        return cpu_pct <= self.start_threshold

    def should_stop_at(self, cpu_pct: float) -> bool:
        """CPU 是否过高需要暂停"""
        if not self.is_available:
            return False
        return cpu_pct >= self.stop_threshold


# ═══════════════════════════════════════════════════════════════
# ThumbnailQueue — 持久化 JSON 队列
# ═══════════════════════════════════════════════════════════════

class ThumbnailQueue:
    """管理待生成缩略图的事件队列，持久化到 JSON 文件"""

    def __init__(self, queue_file=QUEUE_FILE):
        self.queue_file = queue_file
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self.entries: List[Dict] = self._load()
        # 已放弃事件（加密/损坏文件），持久化到 blocked.json
        self.blocked: set = self._load_blocked()

    def _load(self) -> List[Dict]:
        if self.queue_file.exists():
            try:
                with open(self.queue_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except Exception as e:
                logger.warning(f"加载队列失败: {e}")
        return []

    def _load_blocked(self) -> set:
        if BLOCKED_FILE.exists():
            try:
                with open(BLOCKED_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return set(data) if isinstance(data, list) else set()
            except Exception:
                pass
        return set()

    def _save_blocked(self):
        try:
            with open(BLOCKED_FILE, 'w', encoding='utf-8') as f:
                json.dump(sorted(self.blocked), f)
        except Exception as e:
            logger.error(f"保存阻止列表失败: {e}")

    def _save(self):
        try:
            with open(self.queue_file, 'w', encoding='utf-8') as f:
                json.dump(self.entries, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存队列失败: {e}")

    def _thumbnail_exists(self, folder_type: str, event_id: str) -> bool:
        """检查缩略图是否已存在且有效。

        委托 thumbnail_decision.should_regenerate() 统一判断。
        should_regenerate() 返回 True 表示需要（重新）生成，
        本函数返回相反值（True = 缩略图有效，不需要生成）。
        """
        return not should_regenerate(event_id, folder_type)

    def scan(self):
        """扫描目录，将缺少缩略图的事件加入队列
        
        跳过最近 2 分钟内修改的事件（Tesla 可能正在写入/锁定文件，
        导致 ffmpeg 无法读取 moov atom）。
        """
        # 构建已见集合 + 清理过期条目
        # 逻辑：done/failed 条目如果缩略图已不存在（被手动删除），
        # 则同时移除该条目并允许重新入队，防止重复条目。
        stale_eids = set()
        seen_ids = set()
        for e in self.entries:
            eid = e.get('event_id', '')
            st = e.get('status', '')
            if st in ('done', 'failed'):
                ft = e.get('folder_type', '')
                if ft and eid and not self._thumbnail_exists(ft, eid):
                    # 缩略图已不存在 → 移除旧条目，允许重新扫描入队
                    stale_eids.add(eid)
                    continue
            seen_ids.add(eid)

        # 删掉过期的 done/failed 条目
        if stale_eids:
            self.entries = [e for e in self.entries if e.get('event_id') not in stale_eids]
            self._save()
        now = time.time()
        active_cutoff = now - 120  # 2分钟内的视为活跃写入

        folders = ['SentryClips', 'SavedClips', 'RecentClips']
        for folder_name in folders:
            folder_path = BASE_CAM_PATH / folder_name
            if not folder_path.is_dir():
                continue

            # ── RecentClips: 平铺文件结构，按 session 前缀分组入队 ──
            if folder_name == 'RecentClips':
                sessions = {}
                for mp4_file in sorted(folder_path.iterdir()):
                    if not mp4_file.is_file() or not mp4_file.suffix.lower() == '.mp4':
                        continue
                    # 按摄像头后缀拆分: 2026-07-11_16-33-07-front.mp4 → prefix = 2026-07-11_16-33-07
                    name = mp4_file.stem
                    for cam_suffix in ('-front', '-back', '-left_repeater', '-right_repeater',
                                       '-left_pillar', '-right_pillar'):
                        if name.endswith(cam_suffix):
                            session_id = name[:-len(cam_suffix)]
                            break
                    else:
                        session_id = name[:19] if len(name) >= 19 else name

                    if session_id not in sessions:
                        sessions[session_id] = {'files': [], 'newest_mtime': 0}
                    mtime = mp4_file.stat().st_mtime
                    sessions[session_id]['files'].append(str(mp4_file))
                    if mtime > sessions[session_id]['newest_mtime']:
                        sessions[session_id]['newest_mtime'] = mtime

                for session_id, info in sessions.items():
                    if info['newest_mtime'] > active_cutoff:
                        continue  # 跳过最近 2 分钟
                    # ⚠️ 必须等 4 个摄像头文件都写入才能生成缩略图
                    # Tesla 分 camera 写入，扫描时可能只看到 1-3 个 → 生成不完整缩略图
                    if len(info['files']) < 4:
                        continue  # 摄像头不齐全，等待下次扫描
                    if session_id in seen_ids or session_id in self.blocked:
                        continue
                    if self._thumbnail_exists(folder_name, session_id):
                        continue
                    seen_ids.add(session_id)
                    self.entries.append({
                        'event_id': session_id,
                        'folder_type': folder_name,
                        'folder_path': str(folder_path),
                        'video_files': info['files'],
                        'status': 'pending',
                        'retry_count': 0,
                        'added_at': datetime.now().isoformat()
                    })
                continue

            # ── SentryClips / SavedClips: 事件文件夹结构 ──
            for event_dir in sorted(folder_path.iterdir(), reverse=True):
                if not event_dir.is_dir():
                    continue

                event_id = event_dir.name
                mp4s = list(event_dir.glob('*.mp4'))
                if not mp4s:
                    continue

                if event_id not in seen_ids and event_id not in self.blocked and not self._thumbnail_exists(folder_name, event_id):
                    seen_ids.add(event_id)
                    self.entries.append({
                        'event_id': event_id,
                        'folder_type': folder_name,
                        'folder_path': str(event_dir),
                        'status': 'pending',
                        'retry_count': 0,
                        'added_at': datetime.now().isoformat()
                    })

        self._save()
        pending = sum(1 for e in self.entries if e['status'] == 'pending')
        logger.info(f"队列扫描完成: {len(self.entries)} 总计, {pending} 待处理")

    def get_next(self) -> Optional[Dict]:
        """获取下一个待处理事件"""
        for e in self.entries:
            if e['status'] in ('pending', 'paused') and e.get('retry_count', 0) < MAX_RETRIES:
                return e
        return None

    def mark_done(self, event_id: str):
        """标记事件已完成（保留在队列中以支持进度追踪）"""
        for e in self.entries:
            if e['event_id'] == event_id:
                e['status'] = 'done'
                self._save()
                return
        self._save()  # fallback: persist any in-memory changes

    def oldest_pending_age(self):
        """返回最老的 pending 事件的年龄（秒），无 pending 返回 None"""
        oldest = None
        for e in self.entries:
            if e.get('status') in ('pending', 'paused') and e.get('added_at'):
                try:
                    ts = datetime.fromisoformat(e['added_at']).timestamp()
                    if oldest is None or ts < oldest:
                        oldest = ts
                except (ValueError, KeyError):
                    pass
        if oldest is None:
            return None
        return time.time() - oldest

    def cleanup_done(self, max_age_min: int = 60):
        """清理超过指定时间的已完成/失败条目（防止队列无限增长）"""
        cutoff = datetime.now().timestamp() - (max_age_min * 60)
        removed = 0
        new_entries = []
        for e in self.entries:
            st = e.get('status')
            if st in ('done', 'failed'):
                try:
                    added_at = datetime.fromisoformat(e.get('added_at', ''))
                    if added_at.timestamp() < cutoff:
                        removed += 1
                        continue
                except (ValueError, TypeError):
                    removed += 1
                    continue
            new_entries.append(e)
        if removed > 0:
            self.entries = new_entries
            self._save()
            logger.info(f"清理 {removed} 个过期队列条目")
        # Always trim to 500 max
        if len(self.entries) > 500:
            old_len = len(self.entries)
            self.entries = self.entries[-500:]
            self._save()
            logger.info(f"队列截断: {old_len} -> 500")

    def block(self, event_id: str):
        """标记事件为不可生成（加密/损坏），持久化并移除队列"""
        self.blocked.add(event_id)
        self.entries = [e for e in self.entries if e['event_id'] != event_id]
        self._save()
        self._save_blocked()
        logger.info(f"事件 {event_id} 已加入阻止列表（加密或损坏）")

    def pause(self, event_id: str):
        """暂停事件（增加重试计数），超过上限则加入阻止列表"""
        for e in self.entries:
            if e['event_id'] == event_id:
                e['retry_count'] = e.get('retry_count', 0) + 1
                if e['retry_count'] >= MAX_RETRIES:
                    self.block(event_id)
                    logger.info(f"事件 {event_id} 已放弃（超过最大重试次数）")
                else:
                    e['status'] = 'paused'
                    self._save()
                return

    def size(self) -> int:
        return len(self.entries)

    def count_pending(self) -> int:
        """统计待处理（pending + paused）数量，用于积压检测"""
        return sum(1 for e in self.entries if e.get('status') in ('pending', 'paused'))


# ═══════════════════════════════════════════════════════════════
# 后台生成器
# ═══════════════════════════════════════════════════════════════

class BgPreviewGenerator:
    """CPU 自适应后台预览图生成器"""

    def __init__(self):
        self.cpu = CPUMonitor()
        self.queue = ThumbnailQueue()

    def _process_sentry_event(self, entry: Dict) -> bool:
        """生成哨兵事件四宫格缩略图（用于视频列表页）"""
        try:
            from utils.thumbnail_utils import _generate_thumbnail
            result = _generate_thumbnail(entry['folder_path'], entry['event_id'],
                                         folder_type=entry.get('folder_type', 'SentryClips'))
            return bool(result)
        except Exception as e:
            logger.error(f"生成缩略图失败 {entry['event_id']}: {e}")
            return False

    def _process_thumbnail_event(self, entry: Dict) -> bool:
        """生成单事件缩略图（SavedClips/RecentClips，用于视频列表页）
        
        RecentClips 是平铺文件结构，需按 event_id 前缀匹配视频文件。
        
        优先级：
        1. 使用队列条目中缓存的 video_files（避免重扫描竞态）
        2. 回退到目录扫描（仅在缓存的路径全部失效时）
        """
        try:
            from utils.thumbnail_utils import _generate_thumbnail

            event_path = entry['folder_path']
            event_id = entry['event_id']
            video_files = None

            if entry.get('folder_type') == 'RecentClips':
                # ── 优先使用队列缓存的文件列表 ──
                # 队列在 _build_queue 中按 session_id 精确分组，
                # 使用缓存列表可避免处理时重扫描的竞态条件
                #   (Tesla 可能在队列构建与处理之间写入/旋转文件)
                cached_files = entry.get('video_files')
                if cached_files:
                    # 验证缓存文件：去重、验证存在性、验证 MP4 有效性、验证事件归属
                    verified_files = []
                    seen_stems = set()
                    for fpath in cached_files:
                        # 跳过不存在的文件
                        if not os.path.isfile(fpath):
                            continue
                        # 去重（按 stem）
                        stem = os.path.splitext(os.path.basename(fpath))[0]
                        if stem in seen_stems:
                            continue
                        # 验证归属：文件名必须以 event_id 开头
                        if not stem.startswith(event_id):
                            logger.warning(f"文件归属不匹配: {stem} not startswith {event_id}, 跳过")
                            continue
                        # 验证 MP4 有效性
                        if not _is_valid_mp4(fpath):
                            continue
                        seen_stems.add(stem)
                        verified_files.append(fpath)

                    if len(verified_files) >= 2:
                        video_files = verified_files
                        logger.debug(f"使用缓存文件: {event_id} ({len(verified_files)} 个有效)")
                    else:
                        logger.warning(f"缓存文件不足: {event_id} ({len(verified_files)} 有效/{len(cached_files)} 缓存), 回退目录扫描")

                # ── 回退：委托 thumbnail_decision.find_source_files() ──
                if video_files is None:
                    _, found_files = find_source_files(event_id, entry.get('folder_type'))
                    if found_files:
                        # 验证 MP4 有效性（bg_preview 有更严格的加密检测）
                        video_files = [f for f in found_files if _is_valid_mp4(f)]
                        if video_files:
                            # 更新缓存到条目中，下次处理时直接使用
                            entry['video_files'] = video_files
                            logger.debug(f"目录扫描: {event_id} ({len(video_files)} 个视频)")
                    if not video_files:
                        logger.warning(f"RecentClips 无有效视频: {event_id} (已加密或损坏)")
                        return False

            result = _generate_thumbnail(event_path, event_id, video_files=video_files,
                                         folder_type=entry.get('folder_type'))
            return bool(result)
        except Exception as e:
            logger.error(f"生成缩略图失败 {entry['event_id']}: {e}")
            return False

    def run(self):
        """主循环 — 主动+被动互补生成
        
        被动模式（后台）：
        - 定期扫描，将缺失缩略图的事件加入队列
        - 仅当队列积压 ≥ PENDING_THRESHOLD 时才触发生成（批量模式）
        - CPU 空闲时处理，每个周期最多处理 MAX_BATCH_PER_CYCLE 个
        
        主动模式（用户打开页面）：
        - serve_thumbnail() 在首次请求时直接懒生成（无需等到后台触发）
        - _scan_missing_thumbnails() 可手动触发全量生成
        """
        logger.info("=== 后台预览图生成器启动 ===")
        logger.info(f"CPU 阈值: 开始≤{CPU_START_THRESHOLD}%, 暂停≥{CPU_STOP_THRESHOLD}%")
        logger.info(f"积压阈值: {PENDING_THRESHOLD} 个事件触发生成")
        logger.info(f"每周期最多: {MAX_BATCH_PER_CYCLE} 个")
        logger.info(f"队列文件: {QUEUE_FILE}")
        logger.info(f"扫描间隔: 每 {QUEUE_EMPTY_INTERVAL}s")

        last_scan_time = 0.0
        batch_count = 0  # 当前扫描周期已处理的个数

        while True:
            try:
                now = time.time()

                # 1. 定期扫描（无论 CPU 状态）—— 保证新事件被及时发现
                if now - last_scan_time >= QUEUE_EMPTY_INTERVAL:
                    self.queue.scan()
                    last_scan_time = time.time()
                    batch_count = 0  # 新周期重置计数器
                    self.queue.cleanup_done(max_age_min=30)
                    
                    # 显示队列状态
                    pending = self.queue.count_pending()
                    total = self.queue.size()
                    if total > 0:
                        logger.info(f"扫描完成: {total} 总计, {pending} 待处理"
                                    f"{' (积压未达阈值,等待中)' if pending < PENDING_THRESHOLD else ''}")

                    # ── RecentClips 快速通道：直接生成，不经过队列 ──
                    for _ in range(REC_MAX_PER_SCAN):
                        rec_entry = next(
                            (e for e in self.queue.entries
                             if e.get('folder_type') == 'RecentClips'
                             and e.get('status') in ('pending', 'paused')),
                            None
                        )
                        if rec_entry is None:
                            break
                        rec_entry['status'] = 'processing'
                        success = self._process_thumbnail_event(rec_entry)
                        if success:
                            rec_entry['status'] = 'done'
                            rec_entry['done_at'] = datetime.now().isoformat()
                        else:
                            rec_entry['retry_count'] = rec_entry.get('retry_count', 0) + 1
                            rec_entry['status'] = 'pending' if rec_entry['retry_count'] < MAX_RETRIES else 'failed'
                        self.queue._save()
                        time.sleep(REC_THROTTLE_SEC)  # 事件间隔

                # 2. 检查是否需要处理：队列数 < 阈值 → 等待积累
                pending = self.queue.count_pending()
                total = self.queue.size()
                if total == 0:
                    time.sleep(min(10, QUEUE_EMPTY_INTERVAL))
                    continue
                
                if pending < PENDING_THRESHOLD and batch_count == 0:
                    # 积压不足，等待更多事件入队
                    # 但是如果最老的 pending 事件已经超过 5 分钟，
                    # 说明不会有更多事件入队了 → 直接处理
                    oldest_age = self.queue.oldest_pending_age()
                    if oldest_age is not None and oldest_age > 300:
                        logger.info("积压不足 (%d < %d) 但最老 pending 已 %.0f 秒 → 直接处理",
                                    pending, PENDING_THRESHOLD, oldest_age)
                    else:
                        time.sleep(min(5, QUEUE_EMPTY_INTERVAL))
                        continue
                
                # 3. 本周期已处理够多 → 等待下个扫描周期
                if batch_count >= MAX_BATCH_PER_CYCLE:
                    time.sleep(min(5, QUEUE_EMPTY_INTERVAL))
                    continue

                # 4. 获取下一个待处理事件
                entry = self.queue.get_next()
                if entry is None:
                    time.sleep(min(10, QUEUE_EMPTY_INTERVAL))
                    continue

                # 5. 单次 CPU 采样（生成缩略图前才检查 CPU）
                cpu_pct = self.cpu.sample()

                if self.cpu.should_stop_at(cpu_pct):
                    logger.debug(f"CPU 过高 ({cpu_pct:.0f}%), 暂停 {CPU_CHECK_INTERVAL}s")
                    time.sleep(CPU_CHECK_INTERVAL)
                    continue

                if not self.cpu.is_idle_for(cpu_pct):
                    logger.debug(f"CPU 忙碌 ({cpu_pct:.0f}%), 等待 {CPU_CHECK_INTERVAL}s")
                    time.sleep(CPU_CHECK_INTERVAL)
                    continue

                # 6. CPU 空闲 → 处理事件
                event_id = entry['event_id']
                folder_type = entry['folder_type']
                logger.info(f"处理 {folder_type}/{event_id} (CPU: {cpu_pct:.0f}%)")

                if folder_type == 'SentryClips':
                    success = self._process_sentry_event(entry)
                else:
                    success = self._process_thumbnail_event(entry)

                batch_count += 1  # 本周期已处理计数

                if success:
                    self.queue.mark_done(event_id)
                    logger.info(f"OK {folder_type}/{event_id} ({batch_count}/{MAX_BATCH_PER_CYCLE} 本周期)")
                else:
                    self.queue.pause(event_id)
                    logger.warning(f"FAIL {folder_type}/{event_id} 重试 {entry.get('retry_count', 0)}/{MAX_RETRIES}")

                # 7. 处理间隙
                time.sleep(EVENT_GAP_INTERVAL)

            except KeyboardInterrupt:
                logger.info("收到中断信号，退出")
                break
            except Exception as e:
                logger.error(f"主循环异常: {e}")
                time.sleep(5)

        logger.info("后台预览图生成器已停止")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='TeslaUSB CL 后台预览图生成器')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(handler)

    gen = BgPreviewGenerator()
    gen.run()


if __name__ == '__main__':
    main()
