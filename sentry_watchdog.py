#!/usr/bin/env python3
"""
TeslaUSB-Neo 哨兵监控守护进程
============================

功能：
1. 监控哨兵事件目录 (SentryClips)
2. 根据位置状态决定上传策略
3. 在家：延迟30分钟后自动上传
4. 外出：微信推送预览，用户确认后上传
5. 管理上传队列和状态

作者: TeslaUSB-Neo 项目
版本: 1.0.0
"""

import os
import time
import json
import logging

import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Callable
from dataclasses import dataclass, asdict
from enum import Enum
import threading
import queue

# 导入项目模块
try:
    from config import SENTRY_CLIPS_PATH
    from location_detector import LocationDetector, get_location_detector, LocationState
    from wifi_switcher import WifiSwitcher
    from upload_scheduler import UploadScheduler
except ImportError as e:
    logging.warning(f"无法导入项目模块: {e}")
    LocationDetector = None
    WiFiSwitcher = None
    UploadScheduler = None
    SENTRY_CLIPS_PATH = "/mnt/teslacam/TeslaCam/SentryClips"  # fallback


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/var/log/teslausb-sentry.log', mode='a')
    ]
)
logger = logging.getLogger('sentry_watchdog')


class SentryEventStatus(Enum):
    """哨兵事件状态"""
    DETECTED = "detected"           # 刚检测到
    PENDING_CONFIRM = "pending_confirm"  # 等待用户确认（外出模式）
    CONFIRMED = "confirmed"         # 用户已确认上传
    AUTO_UPLOAD = "auto_upload"     # 自动上传中（在家模式）
    UPLOADING = "uploading"         # 正在上传
    COMPLETED = "completed"         # 上传完成
    FAILED = "failed"               # 上传失败
    EXPIRED = "expired"             # 已过期删除


@dataclass
class SentryEvent:
    """哨兵事件数据类"""
    id: str                         # 事件唯一ID (基于时间戳)
    timestamp: datetime             # 检测时间
    folder_path: Path               # 事件文件夹路径
    file_count: int                 # 文件数量
    location_status: str            # 位置状态 (home/away)
    status: SentryEventStatus       # 当前状态
    
    # 时间相关
    detect_time: datetime           # 检测时间
    confirm_deadline: Optional[datetime] = None  # 确认截止时间（外出模式）
    upload_start_time: Optional[datetime] = None  # 上传开始时间
    upload_complete_time: Optional[datetime] = None  # 上传完成时间
    
    # 上传相关
    confirmation_code: Optional[str] = None  # 6位确认码（外出模式）
    confirmed_by: Optional[str] = None       # 确认人/方式
    upload_progress: float = 0.0             # 上传进度
    
    # 预览相关
    preview_path: Optional[Path] = None      # 预览图路径
    video_samples: List[Path] = None         # 视频样本路径
    
    # 结果
    nas_path: Optional[str] = None           # NAS 上的路径
    error_message: Optional[str] = None      # 错误信息
    
    def __post_init__(self):
        if self.video_samples is None:
            self.video_samples = []
    
    def to_dict(self) -> dict:
        """转换为字典"""
        data = asdict(self)
        # 转换枚举和路径
        data['status'] = self.status.value
        data['folder_path'] = str(self.folder_path)
        data['preview_path'] = str(self.preview_path) if self.preview_path else None
        data['video_samples'] = [str(p) for p in self.video_samples]
        # 转换时间
        for key in ['timestamp', 'detect_time', 'confirm_deadline', 
                    'upload_start_time', 'upload_complete_time']:
            if data.get(key):
                data[key] = data[key].isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> 'SentryEvent':
        """从字典创建"""
        # 转换路径
        data['folder_path'] = Path(data['folder_path'])
        if data.get('preview_path'):
            data['preview_path'] = Path(data['preview_path'])
        if data.get('video_samples'):
            data['video_samples'] = [Path(p) for p in data['video_samples']]
        # 转换枚举
        data['status'] = SentryEventStatus(data['status'])
        # 转换时间
        for key in ['timestamp', 'detect_time', 'confirm_deadline',
                    'upload_start_time', 'upload_complete_time']:
            if data.get(key):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)


class SentryWatchdog:
    """
    哨兵监控守护进程
    
    核心功能：
    - 监控 TeslaUSB 挂载的 SentryClips 目录
    - 检测新哨兵事件
    - 根据位置状态执行上传策略
    - 管理事件状态和队列
    """
    
    # 默认配置
    DEFAULT_CONFIG = {
        'sentry_clips_path': SENTRY_CLIPS_PATH,
        'state_file': '/opt/teslausb-web/data/sentry_events.json',
        'home_delay_minutes': 30,           # 在家延迟上传时间
        'away_confirm_timeout_minutes': 30,  # 外出确认超时时间
        'scan_interval_seconds': 10,         # 扫描间隔
        'upload_retry_count': 10,            # 上传重试次数
        'upload_retry_interval': 30,         # 上传重试间隔（秒）
        'nas_base_path': '/mnt/nas/TeslaSentry',
        'preview_enabled': True,
        'watermark_enabled': True,
    }
    
    def __init__(self, config: Optional[Dict] = None):
        """
        初始化哨兵监控
        
        Args:
            config: 配置字典，覆盖默认配置
        """
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        
        # 状态文件路径
        self.state_file = Path(self.config['state_file'])
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 事件存储
        self.events: Dict[str, SentryEvent] = {}
        self._events_lock = threading.Lock()
        
        # 事件队列（用于处理）
        self.event_queue: queue.Queue = queue.Queue()
        
        # 回调函数
        self.on_new_event: Optional[Callable[[SentryEvent], None]] = None
        self.on_upload_start: Optional[Callable[[SentryEvent], None]] = None
        self.on_upload_complete: Optional[Callable[[SentryEvent], None]] = None
        self.on_upload_failed: Optional[Callable[[SentryEvent, str], None]] = None
        self.on_confirm_request: Optional[Callable[[SentryEvent, str], None]] = None
        
        # 模块引用
        self.location_detector: Optional[LocationDetector] = None
        self.wifi_switcher: Optional[WiFiSwitcher] = None
        self.upload_scheduler: Optional[UploadScheduler] = None
        self.sei_client = None  # SEI 遥测决策客户端
        
        # 运行状态
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        
        # 已处理事件ID（防止重复）
        self._processed_ids: set = set()
        
        # 加载历史状态
        self._load_state()
        
        # v88: 状态丢失时基线初始化，防止历史事件重放推送
        if not self._processed_ids:
            self._do_baseline_init()
        
        logger.info("哨兵监控守护进程初始化完成")
    
    def set_modules(self, location_detector=None, wifi_switcher=None, 
                    upload_scheduler=None, sei_client=None):
        """设置功能模块"""
        self.location_detector = location_detector
        self.wifi_switcher = wifi_switcher
        self.upload_scheduler = upload_scheduler
        self.sei_client = sei_client
        logger.info("功能模块已设置")
    
    def _load_state(self):
        """从文件加载事件状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                for event_data in data.get('events', []):
                    try:
                        event = SentryEvent.from_dict(event_data)
                        self.events[event.id] = event
                        self._processed_ids.add(event.id)
                    except Exception as e:
                        logger.warning(f"加载事件失败: {e}")
                
                # v88+: 从状态文件恢复 processed_ids（防止状态丢失导致历史事件重放）
                pids = data.get('processed_ids', [])
                if pids:
                    self._processed_ids.update(pids)
                
                logger.info(f"已加载 {len(self.events)} 个历史事件, {len(self._processed_ids)} 个已处理ID")
            except Exception as e:
                logger.error(f"加载状态文件失败: {e}")
    
    def _save_state(self):
        """保存事件状态到文件（原子写入，防止断电损坏）"""
        try:
            with self._events_lock:
                data = {
                    'updated_at': datetime.now().isoformat(),
                    'events': [e.to_dict() for e in self.events.values()],
                    'processed_ids': sorted(list(self._processed_ids)),
                }
            
            # 原子写入：先写临时文件，再 os.replace（POSIX 原子操作）
            tmp = str(self.state_file) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(self.state_file))
        except Exception as e:
            logger.error(f"保存状态失败: {e}")
    
    def _do_baseline_init(self):
        """状态文件丢失/首次运行时的基线初始化。
        
        当 _processed_ids 为空时（状态文件不存在/损坏/首次运行），
        扫描 SentryClips 目录并标记所有现有事件为"已处理"，
        防止将历史事件当作新事件重放推送通知。
        
        此方法仅在 _processed_ids 为空时调用一次。
        """
        sentry_path = Path(self.config['sentry_clips_path'])
        if not sentry_path.exists():
            logger.info("SentryClips 目录不存在，跳过基线初始化")
            return
        
        count = 0
        try:
            for event_folder in sentry_path.iterdir():
                if not event_folder.is_dir():
                    continue
                # 只标记包含视频文件的事件（跳过空事件/无视频目录）
                has_video = any(
                    f.is_file() and f.suffix.lower() == '.mp4'
                    for f in event_folder.iterdir()
                )
                if has_video:
                    self._processed_ids.add(event_folder.name)
                    count += 1
        except Exception as e:
            logger.error(f"基线初始化扫描失败: {e}")
            return
        
        if count > 0:
            logger.warning(
                f"⚠ 状态文件丢失/首次运行，基线初始化："
                f"标记 {count} 个现有事件为已处理（不推送通知）"
            )
        else:
            logger.info("基线初始化：未发现现有事件")
        
        # 立即持久化 processed_ids，防止下次重启再次触发基线
        self._save_state()
    
    def _generate_event_id(self, folder_path: Path) -> str:
        """生成事件唯一ID — 使用 Tesla 文件夹时间戳，不依赖变化的 mtime/ctime"""
        # Tesla 事件文件夹命名格式: 2026-06-01_12-26-01
        # 此名称全局唯一且不随文件增减而变化，比 mtime 哈希稳定
        return folder_path.name
    
    def _generate_confirmation_code(self) -> str:
        """生成6位确认码"""
        import random
        return ''.join(random.choices('0123456789', k=6))
    
    def scan_sentry_clips(self) -> List[SentryEvent]:
        """
        扫描 SentryClips 目录，检测新事件
        
        Returns:
            新检测到的事件列表
        """
        sentry_path = Path(self.config['sentry_clips_path'])
        if not sentry_path.exists():
            logger.warning(f"SentryClips 目录不存在: {sentry_path}")
            return []
        
        new_events = []
        
        try:
            # 遍历所有事件文件夹
            for event_folder in sentry_path.iterdir():
                if not event_folder.is_dir():
                    continue
                
                # 生成事件ID
                event_id = self._generate_event_id(event_folder)
                
                # 检查是否已处理
                if event_id in self._processed_ids:
                    continue
                
                # 统计视频文件（仅 .mp4，排除 event.json / thumbnails 等）
                file_count = sum(1 for f in event_folder.rglob('*')
                                if f.is_file() and f.suffix.lower() == '.mp4')
                
                # 跳过空事件文件夹（Tesla 某些事件创建后立即取消，无视频）
                if file_count == 0:
                    logger.debug(f"跳过空事件: {event_id} (0 个视频文件)")
                    self._processed_ids.add(event_id)
                    continue
                
                # 获取当前位置状态
                location_status = self._get_current_location_status()
                
                # 创建事件
                event = SentryEvent(
                    id=event_id,
                    timestamp=datetime.now(),
                    folder_path=event_folder,
                    file_count=file_count,
                    location_status=location_status,
                    status=SentryEventStatus.DETECTED,
                    detect_time=datetime.now()
                )
                
                new_events.append(event)
                
                with self._events_lock:
                    self.events[event_id] = event
                    self._processed_ids.add(event_id)
                
                logger.info(f"检测到新哨兵事件: {event_id} ({event_folder.name})")
        
        except Exception as e:
            logger.error(f"扫描 SentryClips 失败: {e}")
        
        return new_events
    
    def _get_current_location_status(self) -> str:
        """获取当前位置状态"""
        if self.location_detector:
            try:
                result = self.location_detector.check_location()
                return result.state.value
            except Exception as e:
                logger.warning(f"位置检测失败: {e}")
        
        # 默认假设在家
        return "home"
    
    def process_new_event(self, event: SentryEvent):
        """
        处理新检测到的事件
        
        Args:
            event: 新事件
        """
        logger.info(f"处理新事件 {event.id}, 位置: {event.location_status}")
        
        # 触发回调
        if self.on_new_event:
            try:
                self.on_new_event(event)
            except Exception as e:
                logger.error(f"新事件回调失败: {e}")
        
        # 根据位置状态决定处理策略
        # 注：即使 upload_enabled=False，也要处理 away 事件（生成确认码+发送通知），仅跳过自动上传
        if event.location_status == "home":
            if self.config.get('upload_enabled', True):
                self._process_home_event(event)
            else:
                logger.info(f"上传已禁用，事件 {event.id} 跳过自动上传（home）")
        else:
            self._process_away_event(event)  # 离开事件始终需要确认码通知
        
        # 保存状态
        self._save_state()
    
    def _process_home_event(self, event: SentryEvent):
        """
        处理在家模式的事件
        
        策略：延迟30分钟后自动上传
        """
        delay_minutes = self.config['home_delay_minutes']
        upload_time = datetime.now() + timedelta(minutes=delay_minutes)
        
        event.status = SentryEventStatus.AUTO_UPLOAD
        event.upload_start_time = upload_time
        
        logger.info(f"事件 {event.id} 将在 {delay_minutes} 分钟后自动上传")
        
        # 加入队列，等待延迟上传
        self.event_queue.put(('delayed_upload', event.id, upload_time))
    
    def _process_away_event(self, event: SentryEvent):
        """
        处理外出模式的事件
        
        策略：生成预览，微信推送，等待用户确认
        """
        # 生成确认码
        event.confirmation_code = self._generate_confirmation_code()
        event.status = SentryEventStatus.PENDING_CONFIRM
        
        # 设置确认截止时间
        timeout = self.config['away_confirm_timeout_minutes']
        event.confirm_deadline = datetime.now() + timedelta(minutes=timeout)
        
        logger.info(f"事件 {event.id} 等待用户确认，确认码: {event.confirmation_code}")
        
        # 生成预览图
        if self.config['preview_enabled']:
            self._generate_preview(event)
        
        # 触发确认请求回调
        if self.on_confirm_request:
            try:
                self.on_confirm_request(event, event.confirmation_code)
            except Exception as e:
                logger.error(f"确认请求回调失败: {e}")
    
    def _generate_preview(self, event: SentryEvent):
        """生成四宫格预览图（备用，主逻辑在 sentry_service._on_new_event）"""
        try:
            from video_preview import VideoPreviewGenerator
            generator = VideoPreviewGenerator()
            results = generator.generate_sentry_grid_preview(
                event_folder=event.folder_path,
                event_id=event.id,
                timestamp=event.timestamp,
                location=event.location_status
            )
            preview_path = results.get('grid_preview')
            if preview_path:
                event.preview_path = preview_path
                logger.info(f"四宫格预览图生成成功: {preview_path}")
            else:
                logger.warning(f"四宫格预览图生成失败: {results.get('error', 'unknown')}")
        except Exception as e:
            logger.error(f"生成预览失败: {e}")

    def confirm_upload(self, event_id: str, confirmation_code: str) -> bool:
        """
        用户确认上传
        
        Args:
            event_id: 事件ID
            confirmation_code: 确认码
            
        Returns:
            是否确认成功
        """
        with self._events_lock:
            event = self.events.get(event_id)
            if not event:
                logger.warning(f"事件不存在: {event_id}")
                return False
            
            if event.status != SentryEventStatus.PENDING_CONFIRM:
                logger.warning(f"事件 {event_id} 状态不正确: {event.status}")
                return False
            
            if event.confirmation_code != confirmation_code:
                logger.warning(f"事件 {event_id} 确认码错误")
                return False
            
            if datetime.now() > event.confirm_deadline:
                logger.warning(f"事件 {event_id} 确认已超时")
                event.status = SentryEventStatus.EXPIRED
                return False
            
            # 确认成功
            event.status = SentryEventStatus.CONFIRMED
            event.confirmed_by = f"user:{confirmation_code}"
            logger.info(f"事件 {event_id} 已确认上传")
            
            # 加入上传队列
            self.event_queue.put(('upload', event_id))
        
        self._save_state()
        return True
    
    def execute_upload(self, event_id: str) -> bool:
        """
        执行上传
        
        Args:
            event_id: 事件ID
            
        Returns:
            上传是否成功
        """
        # 上传开关检查
        if not self.config.get('upload_enabled', True):
            logger.info(f"上传已禁用，跳过事件 {event_id}")
            return False
        
        with self._events_lock:
            event = self.events.get(event_id)
            if not event:
                logger.error(f"上传失败：事件不存在 {event_id}")
                return False
            
            event.status = SentryEventStatus.UPLOADING
            event.upload_start_time = datetime.now()
        
        self._save_state()
        
        # 触发开始上传回调
        if self.on_upload_start:
            try:
                self.on_upload_start(event)
            except Exception as e:
                logger.error(f"上传开始回调失败: {e}")
        
        try:
            # 执行实际上传
            success = self._do_upload(event)
            
            with self._events_lock:
                if success:
                    event.status = SentryEventStatus.COMPLETED
                    event.upload_complete_time = datetime.now()
                    event.upload_progress = 100.0
                    logger.info(f"事件 {event_id} 上传完成")
                    
                    if self.on_upload_complete:
                        try:
                            self.on_upload_complete(event)
                        except Exception as e:
                            logger.error(f"上传完成回调失败: {e}")
                else:
                    event.status = SentryEventStatus.FAILED
                    logger.error(f"事件 {event_id} 上传失败")
                    
                    if self.on_upload_failed:
                        try:
                            self.on_upload_failed(event, event.error_message or "未知错误")
                        except Exception as e:
                            logger.error(f"上传失败回调失败: {e}")
            
            self._save_state()
            return success
        
        except Exception as e:
            logger.error(f"上传异常: {e}")
            with self._events_lock:
                event.status = SentryEventStatus.FAILED
                event.error_message = str(e)
            self._save_state()
            
            if self.on_upload_failed:
                try:
                    self.on_upload_failed(event, str(e))
                except Exception:
                    pass
            
            return False
    
    def _do_upload(self, event: SentryEvent) -> bool:
        """实际执行上传逻辑。
        
        优先使用 cloud_archive_service（支持 S3/SMB/GDrive 等 rclone 后端），
        未配置云服务商时回退到 upload_scheduler（仅支持 NAS rsync）。
        """
        event_path = str(event.folder_path)
        event_id = event.id
        
        # ── SEI 遥测决策：咨询 sei_service 是否应上传 ──
        if self.sei_client is not None:
            try:
                decision = self.sei_client.decide_upload(event_path, event_id)
                if not decision.get("should_upload", True):
                    logger.info(
                        f"sei: skip upload for {event_id}, "
                        f"reason={decision.get('reason')}"
                    )
                    return False
            except Exception as e:
                logger.warning(f"sei: 决策调用异常，默认放行: {e}")
        
        try:
            # ── 优先云上传（rclone 支持 S3/SMB/GDrive 等） ──
            cloud_ok = False
            try:
                import cloud_archive_service
                cfg = cloud_archive_service.load_cloud_config()
                provider = cfg.get('provider', '')
                if provider and provider != 'none':
                    logger.info(f"云上传: event={event_id}, provider={provider}")
                    result = cloud_archive_service.upload_event_to_cloud(
                        'SentryClips', event_id)
                    cloud_ok = result.get('success', False)
                    if cloud_ok:
                        logger.info(f"云上传成功: event={event_id}")
                        self._notify_upload_result(event_id, True)
                        return True
                    else:
                        logger.warning(
                            f"云上传失败: event={event_id}, "
                            f"msg={result.get('message')}, 回退到 NAS")
            except Exception as e:
                logger.warning(f"云上传异常: event={event_id}, err={e}, 回退到 NAS")
            
            # ── 回退 NAS（upload_scheduler） ──
            if self.upload_scheduler:
                success = self.upload_scheduler.upload_sentry_event(
                    event_path, event_id)
                self._notify_upload_result(event_id, success)
                return success
            else:
                if not cloud_ok:
                    logger.error(f"上传调度器不可用且云未配置，无法上传事件 {event_id}")
                return False
        
        except Exception as e:
            event.error_message = str(e)
            logger.error(f"上传失败: {e}")
            return False
    
    def _notify_upload_result(self, event_id: str, success: bool):
        """微信通知上传结果（失败不阻塞主流程）"""
        try:
            from weixin_notifier import WeixinNotifier
            notifier = WeixinNotifier(bot_name="哨兵")
            if success:
                notifier.send_text(
                    f"✅ 哨兵事件上传成功\n事件: {event_id}",
                    mentioned_list=None)
            else:
                notifier.send_text(
                    f"❌ 哨兵事件上传失败\n事件: {event_id}",
                    mentioned_list=None)
        except Exception:
            pass
    
    def _worker_loop(self):
        """工作线程循环"""
        logger.info("工作线程启动")
        
        while self._running:
            try:
                # 获取队列任务
                task = self.event_queue.get(timeout=1)
                task_type = task[0]
                
                if task_type == 'delayed_upload':
                    # 延迟上传任务
                    event_id, scheduled_time = task[1], task[2]
                    wait_seconds = (scheduled_time - datetime.now()).total_seconds()
                    
                    if wait_seconds > 0:
                        logger.info(f"等待 {wait_seconds:.0f} 秒后上传 {event_id}")
                        time.sleep(min(wait_seconds, 60))  # 最多等60秒
                        if datetime.now() < scheduled_time:
                            self.event_queue.put(task)  # 重新入队
                            continue
                    
                    self.execute_upload(event_id)
                
                elif task_type == 'upload':
                    # 立即上传任务
                    event_id = task[1]
                    self.execute_upload(event_id)
                
                self.event_queue.task_done()
            
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"工作线程异常: {e}")
                time.sleep(1)
        
        logger.info("工作线程停止")
    
    def _monitor_loop(self):
        """监控线程循环"""
        logger.info("监控线程启动")
        
        while self._running:
            try:
                # 扫描新事件
                new_events = self.scan_sentry_clips()
                for event in new_events:
                    self.process_new_event(event)
                
                # 检查超时事件（外出模式）
                self._check_expired_events()

                # 承接 web /sentry 页面写入的确认（修复页面孤立流程）
                self._reconcile_external_confirmations()

                # 定期保存状态
                self._save_state()
                
            except Exception as e:
                logger.error(f"监控线程异常: {e}")
            
            # 等待下次扫描
            time.sleep(self.config['scan_interval_seconds'])
        
        logger.info("监控线程停止")
    
    def _check_expired_events(self):
        """检查并处理超时事件"""
        with self._events_lock:
            for event in self.events.values():
                if event.status == SentryEventStatus.PENDING_CONFIRM:
                    if event.confirm_deadline and datetime.now() > event.confirm_deadline:
                        logger.info(f"事件 {event.id} 确认超时，标记为过期")
                        event.status = SentryEventStatus.EXPIRED

    def _reconcile_external_confirmations(self):
        """承接 web /sentry 页面写入的确认（状态文件 status==confirmed）。

        web 与 watchdog 分属不同进程：web 的 /api/sentry/confirm 将状态写入共享
        状态文件。若 web 已触发云上传（web_upload_handled=true），则跳过；
        否则由 watchdog 接管上传。
        """
        try:
            if not self.state_file.exists():
                return
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 筛选：已确认 且 web 未处理 的事件
            pending = []
            for ev in data.get('events', []):
                if ev.get('status') == 'confirmed' and not ev.get('web_upload_handled'):
                    pending.append(ev.get('id'))
            if not pending:
                return
            with self._events_lock:
                for event in self.events.values():
                    if (event.status == SentryEventStatus.PENDING_CONFIRM
                            and event.id in pending):
                        logger.info(f"事件 {event.id} 已被 web 页面确认（web未触发上传），watchdog 接管上传")
                        event.status = SentryEventStatus.CONFIRMED
                        event.confirmed_by = f"web:{event.confirmation_code}"
                        self.event_queue.put(('upload', event.id))
        except Exception as e:
            logger.warning(f"确认 reconciliation 失败: {e}")
    
    def start(self):
        """启动守护进程"""
        if self._running:
            logger.warning("守护进程已在运行")
            return
        
        self._running = True
        
        # 启动工作线程
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        
        # 启动监控线程
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        
        logger.info("哨兵监控守护进程已启动")
    
    def stop(self):
        """停止守护进程"""
        if not self._running:
            return
        
        self._running = False
        
        # 等待线程结束
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        
        # 保存状态
        self._save_state()
        
        logger.info("哨兵监控守护进程已停止")
    
    def get_event_status(self, event_id: str) -> Optional[dict]:
        """获取事件状态"""
        with self._events_lock:
            event = self.events.get(event_id)
            if event:
                return event.to_dict()
        return None
    
    def get_all_events(self, status: Optional[SentryEventStatus] = None) -> List[dict]:
        """
        获取所有事件
        
        Args:
            status: 按状态过滤
            
        Returns:
            事件列表
        """
        with self._events_lock:
            events = list(self.events.values())
            if status:
                events = [e for e in events if e.status == status]
            return [e.to_dict() for e in sorted(events, key=lambda x: x.timestamp, reverse=True)]


# 全局实例
_watchdog: Optional[SentryWatchdog] = None


def get_watchdog(config: Optional[Dict] = None) -> SentryWatchdog:
    """获取全局哨兵监控实例"""
    global _watchdog
    if _watchdog is None:
        _watchdog = SentryWatchdog(config)
    return _watchdog


def init_watchdog(config: Dict, location_detector=None, wifi_switcher=None, 
                  upload_scheduler=None, sei_client=None) -> SentryWatchdog:
    """
    初始化哨兵监控
    
    Args:
        config: 配置字典
        location_detector: 位置检测器
        wifi_switcher: WiFi 切换器
        upload_scheduler: 上传调度器
        sei_client: SEI 遥测决策客户端
        
    Returns:
        SentryWatchdog 实例
    """
    global _watchdog
    _watchdog = SentryWatchdog(config)
    _watchdog.set_modules(location_detector, wifi_switcher, upload_scheduler, sei_client)
    return _watchdog


# 测试代码
if __name__ == '__main__':
    # 配置
    config = {
        'sentry_clips_path': '/tmp/test_sentry',
        'state_file': '/tmp/test_sentry_events.json',
        'scan_interval_seconds': 5,
        'home_delay_minutes': 1,  # 测试用1分钟
    }
    
    # 创建测试目录
    test_path = Path(config['sentry_clips_path'])
    test_path.mkdir(parents=True, exist_ok=True)
    
    # 初始化
    watchdog = SentryWatchdog(config)
    
    # 设置回调
    def on_new(event):
        print(f"[回调] 新事件: {event.id}")
    
    def on_confirm(event, code):
        print(f"[回调] 确认请求: {event.id}, 码: {code}")
    
    watchdog.on_new_event = on_new
    watchdog.on_confirm_request = on_confirm
    
    print("="*60)
    print("哨兵监控测试")
    print("="*60)
    print(f"监控目录: {test_path}")
    print("创建测试事件目录来触发检测...")
    print("="*60)
    
    # 启动
    watchdog.start()
    
    try:
        # 运行一段时间
        for i in range(60):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止测试...")
    finally:
        watchdog.stop()
        print("测试结束")
