#!/usr/bin/env python3
"""
TeslaUSB-Neo CPU 自适应预览生成系统
=====================================

功能：
1. CPU 空闲时（≤30%）自动扫描四个目录，预生成缩略图
2. 解决 /videos 页面打开时需要实时生成缩略图慢的问题
3. 支持断点续传，CPU 过高（≥50%）时暂停

作者: TeslaUSB-Neo 项目
版本: 1.0.0 (移植自老项目 preview_generator.py)
"""

import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, List

# 尝试导入 psutil（CPU 检测用）
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('preview_generator')

# 常量
BASE_CAM_PATH = Path('/media/cnlvan/cam/TeslaCam')
QUEUE_FILE = Path('/opt/teslausb-web/data/preview_queue.json')
THUMB_DIR = Path('/opt/teslausb-web/static/thumbnails')
PREVIEW_DIR = Path('/opt/teslausb-web/data/previews')

# CPU 阈值
CPU_START_THRESHOLD = 30.0   # ≤30% 开始工作
CPU_STOP_THRESHOLD = 50.0     # ≥50% 暂停

# 间隔配置
CPU_CHECK_INTERVAL = 10    # CPU 过高时等待秒数
QUEUE_EMPTY_INTERVAL = 5   # 队列空时重新扫描间隔
EVENT_GAP_INTERVAL = 2      # 处理完一个事件后的休息时间
MAX_RETRIES = 3            # 单个事件最大重试次数
FFMPEG_TIMEOUT = 60         # ffmpeg 超时秒数


# ---------------------------------------------------------------------------
# CPUMonitor：CPU 自适应检测
# ---------------------------------------------------------------------------

class CPUMonitor:
    """CPU 使用率监控，控制生成节奏"""

    def __init__(self, start_threshold: float = CPU_START_THRESHOLD,
                 stop_threshold: float = CPU_STOP_THRESHOLD):
        self.start_threshold = start_threshold
        self.stop_threshold = stop_threshold
        self.is_available = PSUTIL_AVAILABLE

        if not self.is_available:
            logger.warning("psutil 未安装，CPU 检测不可用，将始终允许工作")
            logger.warning("安装方式: /opt/teslausb-web/venv/bin/pip install psutil")

    def is_idle(self) -> bool:
        """
        CPU 空闲（≤ start_threshold）返回 True
        此时可以开始处理队列
        """
        if not self.is_available:
            return True
        try:
            cpu = psutil.cpu_percent(interval=1)
            return cpu <= self.start_threshold
        except Exception as e:
            logger.warning(f"CPU 检测失败: {e}")
            return True

    def should_stop(self) -> bool:
        """
        CPU 过高（≥ stop_threshold）返回 True
        此时应暂停所有生成工作
        """
        if not self.is_available:
            return False
        try:
            cpu = psutil.cpu_percent(interval=1)
            return cpu >= self.stop_threshold
        except Exception as e:
            logger.warning(f"CPU 检测失败: {e}")
            return False

    def get_cpu(self) -> Optional[float]:
        """获取当前 CPU 使用率，返回 None 表示不可用"""
        if not self.is_available:
            return None
        try:
            return psutil.cpu_percent(interval=1)
        except Exception:
            return None

    @staticmethod
    def format_cpu(cpu: Optional[float]) -> str:
        """安全格式化 CPU 使用率"""
        if cpu is None:
            return 'N/A'
        return f'{cpu:.1f}%'


# ---------------------------------------------------------------------------
# ThumbnailQueue：缩略图生成队列（持久化 JSON）
# ---------------------------------------------------------------------------

class ThumbnailQueue:
    """
    管理待生成缩略图的事件队列
    持久化到 JSON 文件，支持断点续传
    """

    def __init__(self, queue_file: Path = QUEUE_FILE):
        self.queue_file = queue_file
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self.entries: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        """从 JSON 加载队列"""
        if self.queue_file.exists():
            try:
                with open(self.queue_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception as e:
                logger.warning(f"加载队列失败: {e}，使用空队列")
        return []

    def _save(self):
        """保存队列到 JSON"""
        try:
            with open(self.queue_file, 'w', encoding='utf-8') as f:
                json.dump(self.entries, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存队列失败: {e}")

    def scan(self):
        """
        扫描四个目录，将没有缩略图的事件加入队列
        按事件时间倒序（最新的优先处理）
        """
        new_entries = []
        seen_ids = {e.get('event_id') for e in self.entries}

        # 扫描配置：(目录名, 缩略图路径函数, event_id 提取函数)
        scan_configs = [
            ('SentryClips', self._check_sentry, self._extract_event_id_from_dir),
            ('SavedClips', self._check_saved, self._extract_event_id_from_dir),
            ('RecentClips', self._check_recent, self._extract_event_id_from_filename),
            ('EncryptedClips', self._check_encrypted, self._extract_event_id_from_dir),
        ]

        for folder_name, check_func, id_extract_func in scan_configs:
            folder_path = BASE_CAM_PATH / folder_name
            if not folder_path.exists():
                continue

            events = check_func(folder_path, id_extract_func)
            for event in events:
                event_id = event['event_id']
                # 跳过已在队列中的事件
                if event_id in seen_ids:
                    continue
                # 跳过已完成的事件（缩略图已存在）
                if self._thumbnail_exists(event):
                    continue
                event['status'] = 'pending'
                event['retries'] = 0
                event['added_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                new_entries.append(event)
                seen_ids.add(event_id)

        if new_entries:
            self.entries.extend(new_entries)
            self._save()
            logger.info(f"扫描完成，新增 {len(new_entries)} 个待处理事件")

    def _check_sentry(self, folder_path: Path, id_extract_func) -> List[Dict]:
        """扫描 SentryClips（子目录结构，有 event.json）"""
        events = []
        for subdir in sorted(folder_path.iterdir(), reverse=True):
            if not subdir.is_dir():
                continue
            event_id = id_extract_func(subdir.name)
            events.append({
                'event_id': event_id,
                'folder_type': 'SentryClips',
                'folder_path': str(subdir),
            })
        return events

    def _check_saved(self, folder_path: Path, id_extract_func) -> List[Dict]:
        """扫描 SavedClips（子目录结构，有 event.json）"""
        events = []
        for subdir in sorted(folder_path.iterdir(), reverse=True):
            if not subdir.is_dir():
                continue
            event_id = id_extract_func(subdir.name)
            events.append({
                'event_id': event_id,
                'folder_type': 'SavedClips',
                'folder_path': str(subdir),
            })
        return events

    def _check_recent(self, folder_path: Path, id_extract_func) -> List[Dict]:
        """扫描 RecentClips（文件平铺，无子目录，无 event.json）"""
        events = []
        seen_ids = set()
        # 只取 front 摄像头文件来提取 event_id
        for f in sorted(folder_path.glob('*-front.mp4'), reverse=True):
            event_id = id_extract_func(f.name)
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            events.append({
                'event_id': event_id,
                'folder_type': 'RecentClips',
                'folder_path': str(folder_path),
            })
        return events

    def _check_encrypted(self, folder_path: Path, id_extract_func) -> List[Dict]:
        """扫描 EncryptedClips（目前结构未知，暂跳过）"""
        # TODO: 确认 Tesla 加密录像的目录结构后补充
        return []

    def _extract_event_id_from_dir(self, dirname: str) -> str:
        """从目录名提取 event_id（SentryClips/SavedClips）"""
        return dirname.strip('/\\')

    def _extract_event_id_from_filename(self, filename: str) -> str:
        """从文件名提取 event_id（RecentClips: 2026-04-13_22-02-56-front.mp4 -> 2026-04-13_22-02-56）"""
        # 去掉 -front.mp4 / -back.mp4 等后缀
        for suffix in ['-front.mp4', '-back.mp4', '-left_repeater.mp4', '-right_repeater.mp4',
                       '-left.mp4', '-right.mp4']:
            if filename.endswith(suffix):
                return filename[:-len(suffix)]
        # fallback: 去掉最后的 _camera.mp4
        parts = filename.rsplit('-', 1)
        if len(parts) == 2:
            return parts[0]
        return filename.replace('.mp4', '')

    def _thumbnail_exists(self, event: Dict) -> bool:
        """检查该事件的缩略图是否已存在"""
        event_id = event['event_id']
        folder_type = event['folder_type']

        if folder_type == 'SentryClips':
            preview_path = PREVIEW_DIR / f"{event_id}_grid_preview.jpg"
            return preview_path.exists()
        else:
            thumb_path = THUMB_DIR / f"{event_id}.jpg"
            return thumb_path.exists()

    def get_next(self) -> Optional[Dict]:
        """获取下一个待处理事件（status=pending 或 paused 且可重试的）"""
        for entry in self.entries:
            status = entry.get('status', 'pending')
            retries = entry.get('retries', 0)
            if status in ('pending', 'paused') and retries < MAX_RETRIES:
                return entry
        return None

    def mark_done(self, event_id: str):
        """标记事件完成，从队列移除"""
        self.entries = [e for e in self.entries if e.get('event_id') != event_id]
        self._save()
        logger.info(f"队列完成: {event_id}")

    def mark_failed(self, event_id: str):
        """标记事件失败（重试次数用尽），从队列移除"""
        self.entries = [e for e in self.entries if e.get('event_id') != event_id]
        self._save()
        logger.warning(f"队列移除（失败）: {event_id}")

    def pause(self, event_id: str):
        """暂停处理某个事件（CPU 过高或临时失败）"""
        for e in self.entries:
            if e.get('event_id') == event_id:
                e['status'] = 'paused'
                e['retries'] = e.get('retries', 0) + 1
                self._save()
                return
        # 如果事件不在队列中，加入并标记 paused
        self.entries.append({
            'event_id': event_id,
            'folder_type': 'unknown',
            'folder_path': '',
            'status': 'paused',
            'retries': 1,
            'added_at': time.strftime('%Y-%m-%d %H:%M:%S')
        })
        self._save()

    def count(self) -> int:
        """队列中待处理事件数量"""
        return len([e for e in self.entries
                    if e.get('status') in ('pending', 'paused')
                    and e.get('retries', 0) < MAX_RETRIES])

    def clear(self):
        """清空队列"""
        self.entries = []
        self._save()


# ---------------------------------------------------------------------------
# 预览生成核心逻辑
# ---------------------------------------------------------------------------

# 模块级路径设置（避免每次循环重复插入）
_TESLAUSB_PATH = '/opt/teslausb-web'
if _TESLAUSB_PATH not in sys.path:
    sys.path.insert(0, _TESLAUSB_PATH)


def process_sentry_event(event: Dict) -> bool:
    """
    处理 SentryClips 事件：调用现有 generate_sentry_grid_preview()
    返回是否成功
    """
    event_id = event['event_id']
    folder_path = Path(event['folder_path'])

    try:
        from video_preview import VideoPreviewGenerator

        gen = VideoPreviewGenerator()
        result = gen.generate_sentry_grid_preview(folder_path, event_id)

        return result.get('grid_preview') is not None

    except Exception as e:
        logger.error(f"处理 SentryClips 事件失败 {event_id}: {e}")
        return False


def process_thumbnail_event(event: Dict) -> bool:
    """
    处理 SavedClips/RecentClips 事件：调用 generate_thumbnail_for_event()
    返回是否成功
    """
    event_id = event['event_id']
    folder_type = event['folder_type']

    try:
        from video_preview import generate_thumbnail_for_event

        result = generate_thumbnail_for_event(folder_type, event_id)
        return result.get('success', False)

    except Exception as e:
        logger.error(f"处理缩略图事件失败 {folder_type}/{event_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def run_loop():
    """主循环：CPU 空闲时处理缩略图队列"""
    cpu_monitor = CPUMonitor()
    queue = ThumbnailQueue()

    logger.info("=" * 60)
    logger.info("CPU 自适应预览生成系统启动")
    logger.info(f"CPU 阈值: 开始≤{CPU_START_THRESHOLD}%，暂停≥{CPU_STOP_THRESHOLD}%")
    logger.info(f"psutil 可用: {PSUTIL_AVAILABLE}")
    logger.info("=" * 60)

    while True:
        try:
            # 1) CPU 过高检测
            if cpu_monitor.should_stop():
                cpu = cpu_monitor.get_cpu()
                logger.debug(f"CPU {cpu_monitor.format_cpu(cpu)} ≥ {CPU_STOP_THRESHOLD}%，暂停处理")
                time.sleep(CPU_CHECK_INTERVAL)
                continue

            # 2) CPU 空闲检测
            if not cpu_monitor.is_idle():
                time.sleep(CPU_CHECK_INTERVAL)
                continue

            # 3) 扫描队列（每次循环都重新扫描，捕获新事件）
            queue.scan()

            # 4) 获取下一个待处理事件
            event = queue.get_next()
            if not event:
                logger.debug(f"队列空，{QUEUE_EMPTY_INTERVAL}s 后重新扫描")
                time.sleep(QUEUE_EMPTY_INTERVAL)
                continue

            event_id = event['event_id']
            folder_type = event['folder_type']
            cpu = cpu_monitor.get_cpu()

            logger.info(f"处理: {folder_type}/{event_id} (CPU: {cpu_monitor.format_cpu(cpu)})")

            # 5) 根据 folder_type 调用不同的生成函数
            if folder_type == 'SentryClips':
                success = process_sentry_event(event)
            else:
                success = process_thumbnail_event(event)

            # 6) 处理结果
            if success:
                queue.mark_done(event_id)
                logger.info(f"✓ 完成: {event_id}")
            else:
                queue.pause(event_id)
                retries = event.get('retries', 0)
                logger.warning(f"✗ 失败: {event_id}（第{retries}次，将重试）")

            # 7) 处理完一个事件后短暂休息，避免 CPU 持续高负载
            time.sleep(EVENT_GAP_INTERVAL)

        except KeyboardInterrupt:
            logger.info("收到停止信号 (KeyboardInterrupt)，退出...")
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}")
            time.sleep(CPU_CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    run_loop()
