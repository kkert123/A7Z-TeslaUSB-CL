"""
TeslaUSB Neo Web - 上传调度器

位置感知的智能上传调度，支持重试机制、进度追踪、用户确认流程。

Features:
    - 在家自动上传（延迟策略）
    - 外出确认上传（微信推送 + 随机码）
    - 断点续传与重试机制
    - 上传队列持久化
    - 延迟删除队列管理
"""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from config_manager import ConfigManager, get_config_manager
from location_detector import LocationInfo, LocationState, get_location_detector

logger = logging.getLogger(__name__)

from config import SENTRY_CLIPS_PATH

DEFAULT_QUEUE_DB = "/data/sentry_queue.db"
DEFAULT_SENTRY_PATH = SENTRY_CLIPS_PATH


class UploadStatus(Enum):
    """上传状态枚举"""
    PENDING_CONFIRM = "pending_confirm"  # 等待用户确认（外出模式）
    CONFIRMED = "confirmed"              # 用户已确认
    UPLOADING = "uploading"              # 正在上传
    DONE = "done"                        # 上传完成
    FAILED = "failed"                    # 上传失败（已重试完）
    CANCELLED = "cancelled"              # 用户取消
    EXPIRED = "expired"                  # 确认超时


class FileStatus(Enum):
    """单个文件上传状态"""
    PENDING = "pending"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class FileProgress:
    """单个文件上传进度"""
    filename: str
    total_size: int = 0
    uploaded_size: int = 0
    status: FileStatus = FileStatus.PENDING
    error_message: str = ""
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    
    @property
    def progress_percent(self) -> float:
        if self.total_size == 0:
            return 0.0
        return round(self.uploaded_size / self.total_size * 100, 1)
    
    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "total_size": self.total_size,
            "uploaded_size": self.uploaded_size,
            "progress_percent": self.progress_percent,
            "status": self.status.value,
            "error_message": self.error_message,
            "last_updated": self.last_updated,
        }


@dataclass
class UploadTask:
    """上传任务"""
    task_id: str  # 格式: 2026-04-11_10-00-00
    event_path: str  # 哨兵事件完整路径
    event_name: str  # 事件名称（目录名）
    status: UploadStatus
    preview_code: str  # 6位随机码（外出确认用）
    created_at: str
    confirmed_at: Optional[str] = None
    upload_started_at: Optional[str] = None
    completed_at: Optional[str] = None
    retry_count: int = 0
    error_message: str = ""
    at_home: bool = False
    user_decision: Optional[str] = None  # retry, skip, cancel
    file_progress: dict[str, FileProgress] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "event_path": self.event_path,
            "event_name": self.event_name,
            "status": self.status.value,
            "preview_code": self.preview_code,
            "created_at": self.created_at,
            "confirmed_at": self.confirmed_at,
            "upload_started_at": self.upload_started_at,
            "completed_at": self.completed_at,
            "retry_count": self.retry_count,
            "error_message": self.error_message,
            "at_home": self.at_home,
            "user_decision": self.user_decision,
            "file_progress": {
                k: v.to_dict() for k, v in self.file_progress.items()
            },
        }


@dataclass
class DeleteTask:
    """延迟删除任务"""
    file_path: str
    scheduled_delete_at: str
    upload_verified: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class UploadSchedulerError(Exception):
    """上传调度器异常"""
    pass


class UploadScheduler:
    """
    上传调度器
    
    管理上传队列，执行上传任务，处理重试和用户确认。
    
    Args:
        config_manager: 配置管理器实例
        queue_db_path: 队列数据库路径
        sentry_path: 哨兵视频目录路径
    """
    
    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        queue_db_path: str = DEFAULT_QUEUE_DB,
        sentry_path: str = DEFAULT_SENTRY_PATH,
    ):
        self.config = config_manager or get_config_manager()
        self.queue_db_path = queue_db_path
        self.sentry_path = Path(sentry_path)
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._on_upload_complete: Optional[Callable[[UploadTask], None]] = None
        self._on_upload_failed: Optional[Callable[[UploadTask, str], None]] = None
        
        self._init_database()
    
    def _init_database(self) -> None:
        """初始化 SQLite 数据库"""
        try:
            db_dir = Path(self.queue_db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            
            conn = sqlite3.connect(self.queue_db_path)
            cursor = conn.cursor()
            
            # 上传任务表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS upload_tasks (
                    task_id TEXT PRIMARY KEY,
                    event_path TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    preview_code TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    upload_started_at TEXT,
                    completed_at TEXT,
                    retry_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    at_home BOOLEAN DEFAULT 0,
                    user_decision TEXT,
                    file_progress TEXT,  -- JSON
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 删除队列表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS delete_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    scheduled_delete_at TEXT NOT NULL,
                    upload_verified BOOLEAN DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 完成记录表（防止重复处理）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS completed_events (
                    event_name TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL
                )
            """)
            
            # 索引
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON upload_tasks(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_delete_time ON delete_queue(scheduled_delete_at)")
            
            conn.commit()
            conn.close()
            logger.info(f"队列数据库初始化成功: {self.queue_db_path}")
            
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}", exc_info=True)
            raise UploadSchedulerError(f"数据库初始化失败: {e}")
    
    def _get_db(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.queue_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式，提高并发性能
        return conn
    
    def set_callbacks(
        self,
        on_upload_complete: Optional[Callable[[UploadTask], None]] = None,
        on_upload_failed: Optional[Callable[[UploadTask, str], None]] = None,
    ) -> None:
        """设置回调函数"""
        self._on_upload_complete = on_upload_complete
        self._on_upload_failed = on_upload_failed
    
    def create_task(
        self,
        event_path: str,
        at_home: bool,
        preview_code: str = "",
    ) -> UploadTask:
        """
        创建上传任务
        
        Args:
            event_path: 哨兵事件路径
            at_home: 是否在家
            preview_code: 确认码（外出模式需要）
            
        Returns:
            UploadTask 任务对象
        """
        event_name = Path(event_path).name
        
        # 检查是否已存在
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT task_id FROM upload_tasks WHERE event_name = ?", (event_name,))
        if cursor.fetchone():
            conn.close()
            raise UploadSchedulerError(f"任务已存在: {event_name}")
        conn.close()
        
        # 确定初始状态
        status = UploadStatus.PENDING_CONFIRM if not at_home else UploadStatus.CONFIRMED
        
        task = UploadTask(
            task_id=event_name,
            event_path=event_path,
            event_name=event_name,
            status=status,
            preview_code=preview_code if not at_home else "",
            created_at=datetime.now().isoformat(),
            at_home=at_home,
        )
        
        # 扫描文件
        self._scan_files(task)
        
        # 保存到数据库
        self._save_task(task)
        
        logger.info(f"创建上传任务: {event_name}, 状态: {status.value}, 在家: {at_home}")
        return task
    
    def _scan_files(self, task: UploadTask) -> None:
        """扫描事件目录下的视频文件"""
        event_path = Path(task.event_path)
        if not event_path.exists():
            return
        
        video_extensions = {".mp4", ".mov", ".avi"}
        
        for file_path in event_path.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in video_extensions:
                rel_path = file_path.relative_to(event_path)
                task.file_progress[str(rel_path)] = FileProgress(
                    filename=str(rel_path),
                    total_size=file_path.stat().st_size,
                    status=FileStatus.PENDING,
                )
        
        logger.debug(f"扫描到 {len(task.file_progress)} 个视频文件: {task.event_name}")
    
    def _save_task(self, task: UploadTask) -> None:
        """保存任务到数据库"""
        conn = self._get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO upload_tasks
            (task_id, event_path, event_name, status, preview_code, created_at,
             confirmed_at, upload_started_at, completed_at, retry_count,
             error_message, at_home, user_decision, file_progress)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.task_id,
            task.event_path,
            task.event_name,
            task.status.value,
            task.preview_code,
            task.created_at,
            task.confirmed_at,
            task.upload_started_at,
            task.completed_at,
            task.retry_count,
            task.error_message,
            task.at_home,
            task.user_decision,
            json.dumps({k: asdict(v) for k, v in task.file_progress.items()}),
        ))
        
        conn.commit()
        conn.close()
    
    def confirm_upload(self, preview_code: str) -> Optional[UploadTask]:
        """
        用户确认上传（通过预览码）
        
        Args:
            preview_code: 6位确认码
            
        Returns:
            确认的任务，如果码无效返回 None
        """
        conn = self._get_db()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT * FROM upload_tasks WHERE preview_code = ? AND status = ?",
            (preview_code, UploadStatus.PENDING_CONFIRM.value),
        )
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        task = self._row_to_task(row)
        task.status = UploadStatus.CONFIRMED
        task.confirmed_at = datetime.now().isoformat()
        self._save_task(task)
        
        logger.info(f"用户确认上传: {task.event_name}")
        return task
    
    def _row_to_task(self, row: sqlite3.Row) -> UploadTask:
        """数据库行转换为 UploadTask"""
        file_progress_data = json.loads(row["file_progress"] or "{}")
        file_progress = {
            k: FileProgress(**v) for k, v in file_progress_data.items()
        }
        
        return UploadTask(
            task_id=row["task_id"],
            event_path=row["event_path"],
            event_name=row["event_name"],
            status=UploadStatus(row["status"]),
            preview_code=row["preview_code"] or "",
            created_at=row["created_at"],
            confirmed_at=row["confirmed_at"],
            upload_started_at=row["upload_started_at"],
            completed_at=row["completed_at"],
            retry_count=row["retry_count"] or 0,
            error_message=row["error_message"] or "",
            at_home=bool(row["at_home"]),
            user_decision=row["user_decision"],
            file_progress=file_progress,
        )
    
    def get_pending_tasks(self) -> list[UploadTask]:
        """获取待处理的任务列表"""
        conn = self._get_db()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT * FROM upload_tasks WHERE status IN (?, ?) ORDER BY created_at",
            (UploadStatus.CONFIRMED.value, UploadStatus.PENDING_CONFIRM.value),
        )
        rows = cursor.fetchall()
        conn.close()
        
        return [self._row_to_task(row) for row in rows]
    
    def get_task(self, task_id: str) -> Optional[UploadTask]:
        """获取指定任务"""
        conn = self._get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM upload_tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        conn.close()
        
        return self._row_to_task(row) if row else None
    
    def get_all_tasks(self) -> list[UploadTask]:
        """获取所有非终端状态的任务（含 uploading / pending / failed / done）"""
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM upload_tasks "
            "WHERE status NOT IN (?, ?, ?) "
            "ORDER BY created_at",
            (UploadStatus.EXPIRED.value,
             UploadStatus.CANCELLED.value,
             UploadStatus.DONE.value),
        )
        rows = cursor.fetchall()
        conn.close()
        return [self._row_to_task(row) for row in rows]
    
    def upload_sentry_event(self, event_path, event_id: str) -> bool:
        """
        上传哨兵事件
        
        通过调度器创建任务并执行上传，支持断点续传和队列管理。
        
        Args:
            event_path: 事件文件夹路径
            event_id: 事件ID
            
        Returns:
            上传是否成功
        """
        try:
            task = self.create_task(event_path, at_home=True)
            return self._execute_upload(task)
        except UploadSchedulerError as e:
            logger.error(f"创建上传任务失败 {event_id}: {e}")
            return False
    
    def schedule_delete(self, file_path: str, delay_seconds: int, upload_verified: bool = True) -> None:
        """
        安排延迟删除
        
        Args:
            file_path: 文件路径
            delay_seconds: 延迟秒数
            upload_verified: 是否已验证上传成功
        """
        delete_at = (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
        
        conn = self._get_db()
        cursor = conn.cursor()
        
        cursor.execute(
            "INSERT INTO delete_queue (file_path, scheduled_delete_at, upload_verified) VALUES (?, ?, ?)",
            (file_path, delete_at, upload_verified),
        )
        
        conn.commit()
        conn.close()
        
        logger.debug(f"安排删除: {file_path}, 时间: {delete_at}")
    
    def process_delete_queue(self) -> int:
        """
        处理删除队列
        
        Returns:
            删除的文件数量
        """
        now = datetime.now().isoformat()
        
        conn = self._get_db()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT * FROM delete_queue WHERE scheduled_delete_at <= ?",
            (now,),
        )
        rows = cursor.fetchall()
        
        deleted_count = 0
        for row in rows:
            file_path = row["file_path"]
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"删除文件: {file_path}")
                    deleted_count += 1
                
                cursor.execute("DELETE FROM delete_queue WHERE id = ?", (row["id"],))
            except Exception as e:
                logger.error(f"删除文件失败 {file_path}: {e}")
        
        conn.commit()
        conn.close()
        
        return deleted_count
    
    def start(self) -> None:
        """启动调度器线程"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("上传调度器已启动")
    
    def stop(self) -> None:
        """停止调度器"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("上传调度器已停止")
    
    def _run_loop(self) -> None:
        """调度器主循环"""
        while self._running:
            try:
                # 检查配置更新
                self.config.reload_if_changed()
                
                # 处理删除队列
                self.process_delete_queue()
                
                # 处理上传任务
                self._process_uploads()
                
                # 等待下一轮
                time.sleep(10)
                
            except Exception as e:
                logger.error(f"调度循环异常: {e}", exc_info=True)
                time.sleep(30)  # 异常后延长等待
    
    def _process_uploads(self) -> None:
        """处理待上传任务"""
        tasks = self.get_pending_tasks()
        
        for task in tasks:
            if task.status == UploadStatus.CONFIRMED:
                self._execute_upload(task)
    
    def _execute_upload(self, task: UploadTask) -> bool:
        """
        执行上传任务
        
        Returns:
            是否成功
        """
        config = self.config.get_config()
        
        task.status = UploadStatus.UPLOADING
        task.upload_started_at = datetime.now().isoformat()
        self._save_task(task)
        
        success = False
        final_error = ""
        
        for attempt in range(1, config.upload.max_retries + 1):
            try:
                if self._upload_to_nas(task):
                    success = True
                    break
            except Exception as e:
                final_error = str(e)
                logger.warning(f"上传失败 (尝试 {attempt}/{config.upload.max_retries}): {e}")
                task.retry_count = attempt
                
                if attempt < config.upload.max_retries:
                    time.sleep(config.upload.retry_interval_seconds)
        
        if success:
            task.status = UploadStatus.DONE
            task.completed_at = datetime.now().isoformat()
            self._save_task(task)
            
            # 如果在家，安排延迟删除
            if task.at_home:
                for rel_path, progress in task.file_progress.items():
                    file_path = os.path.join(task.event_path, rel_path)
                    self.schedule_delete(file_path, config.upload.delete_delay_seconds)
            
            if self._on_upload_complete:
                self._on_upload_complete(task)
                
            logger.info(f"上传完成: {task.event_name}")
            
            # ── 微信通知：队列清空检查 ──
            remaining = len(self.get_pending_tasks())
            if remaining == 0:
                try:
                    from weixin_notifier import WeixinNotifier
                    notifier = WeixinNotifier(bot_name="哨兵")
                    notifier.send_text("📤 上传队列已全部清空，所有哨兵事件处理完毕")
                except Exception:
                    pass
        else:
            task.status = UploadStatus.FAILED
            task.error_message = f"已重试 {config.upload.max_retries} 次: {final_error}"
            self._save_task(task)
            
            if self._on_upload_failed:
                self._on_upload_failed(task, task.error_message)
                
            logger.error(f"上传失败: {task.event_name}, {task.error_message}")
        
        return success
    
    def _upload_to_nas(self, task: UploadTask) -> bool:
        """
        上传文件到 NAS
        
        使用 rsync 或 cp 实现上传，支持断点续传。
        
        Returns:
            是否成功
        """
        config = self.config.get_config()
        nas = config.nas
        
        if nas.type != "smb":
            raise NotImplementedError(f"不支持的 NAS 类型: {nas.type}")
        
        # 构建目标路径
        nas_mount = Path(nas.mount_point)
        target_dir = nas_mount / "SentryClips" / task.event_name
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # 使用 rsync 上传
        for rel_path, progress in task.file_progress.items():
            if progress.status == FileStatus.DONE:
                continue  # 跳过已完成的
            
            source = Path(task.event_path) / rel_path
            target = target_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            
            try:
                # 使用 rsync 部分传输
                result = subprocess.run(
                    ["rsync", "-avP", "--partial", str(source), str(target)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                
                if result.returncode == 0:
                    progress.status = FileStatus.DONE
                    progress.uploaded_size = progress.total_size
                    progress.last_updated = datetime.now().isoformat()
                else:
                    progress.status = FileStatus.FAILED
                    progress.error_message = result.stderr[:200]
                    raise UploadSchedulerError(f"rsync 失败: {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                progress.status = FileStatus.FAILED
                raise UploadSchedulerError("上传超时")
        
        # 保存进度
        self._save_task(task)
        
        return True


# 单例实例
_scheduler: Optional[UploadScheduler] = None


def get_upload_scheduler() -> UploadScheduler:
    """获取全局上传调度器实例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = UploadScheduler()
    return _scheduler
