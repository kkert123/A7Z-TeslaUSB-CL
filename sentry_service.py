#!/usr/bin/env python3
"""
TeslaUSB-Neo 哨兵服务主程序
==========================

功能：
1. 集成哨兵监控、微信推送、视频预览
2. 位置感知上传策略
3. 用户确认机制
4. 系统服务管理

作者: TeslaUSB-Neo 项目
版本: 1.0.0
"""

import os
import sys
import json
import argparse
import logging
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict

# 导入项目模块
from sentry_watchdog import SentryWatchdog, SentryEvent, SentryEventStatus, init_watchdog
from weixin_notifier import WeixinNotifier, WeComConfig

try:
    from video_preview import VideoPreviewGenerator
except ImportError:
    VideoPreviewGenerator = None

try:
    from location_detector import init_location_detector
except ImportError:
    init_location_detector = None

try:
    from wifi_switcher import WifiSwitcher
except ImportError:
    WifiSwitcher = None

# 配置日志
# Configure logging (FileHandler wrapped in try/except for permissions)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
try:
    fh = logging.FileHandler('/var/log/teslausb-sentry.log', mode='a')
    logging.getLogger().addHandler(fh)
except (PermissionError, FileNotFoundError) as e:
    logging.warning(f"Cannot open log file: {e}, using console only")
logger = logging.getLogger('sentry_service')


class SentryService:
    """
    哨兵服务主类
    
    整合所有功能：
    - 哨兵监控
    - 微信推送
    - 视频预览
    - 位置检测
    - WiFi 切换
    """
    
    DEFAULT_CONFIG = {
        # 哨兵配置 — 占位，实际路径在 __init__ 中通过 config.PARTITIONS 动态赋值
        'sentry_clips_path': '/mnt/teslacam/TeslaCam/SentryClips',
        'state_file': '/opt/radxa_data/data/sentry_events.json',
        'home_delay_minutes': 30,
        'away_confirm_timeout_minutes': 30,
        'scan_interval_seconds': 10,
        
        # 企业微信配置
        'wecom_webhook_key': '',
        'wecom_webhook_url': '',
        
        # 位置检测
        'teslamate_url': 'http://100.64.0.11:7777',
        'home_location': '家',
        'home_wifi_ssids': [],
        'hotspot_ssids': [],
        'wifi_interface': 'wlan0',
        
        # NAS 配置
        'nas_base_path': '/mnt/nas/TeslaSentry',
        
        # 预览配置
        'preview_enabled': True,
        'watermark_enabled': True,
        
        # 调试
        'debug': False,
    }
    
    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化服务
        
        Args:
            config_path: 配置文件路径
        """
        self.config = self._load_config(config_path)
        self._setup_logging()
        
        # 组件
        self.watchdog: Optional[SentryWatchdog] = None
        self.notifier: Optional[WeixinNotifier] = None
        self.sentry_notifier: Optional[WeixinNotifier] = None  # 哨兵事件专用机器人
        self.preview_generator: Optional[VideoPreviewGenerator] = None
        self.location_detector = None
        self.wifi_switcher = None
        self.upload_scheduler = None
        self.sei_client = None
        
        # 运行状态
        self._running = False
        
        logger.info("哨兵服务初始化完成")
    
    def _load_config(self, config_path: Optional[Path]) -> Dict:
        """加载配置"""
        config = dict(self.DEFAULT_CONFIG)

        # 用 config.PARTITIONS 动态覆盖默认路径，避免用户名硬编码
        try:
            from config import PARTITIONS as _PARTITIONS
            cam_root = _PARTITIONS["cam"]
            config["sentry_clips_path"] = os.path.join(cam_root, "TeslaCam", "SentryClips")
        except Exception:
            pass  # 导入失败时保持 DEFAULT_CONFIG 中的默认值

        if config_path and config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    config.update(loaded)
                logger.info(f"配置已加载: {config_path}")
            except Exception as e:
                logger.error(f"加载配置失败: {e}")
        
        # 环境变量覆盖
        if os.environ.get('WECOM_WEBHOOK_KEY'):
            config['wecom_webhook_key'] = os.environ['WECOM_WEBHOOK_KEY']
        
        return config
    
    def _setup_logging(self):
        """设置日志级别"""
        if self.config.get('debug'):
            logging.getLogger().setLevel(logging.DEBUG)
            logger.info("调试模式已启用")
    
    def _init_components(self):
        """初始化所有组件"""
        logger.info("初始化组件...")
        
        # 1. 微信推送（双机器人架构）
        # 状态机器人：上传进度、完成/失败通知
        status_key = self.config.get('wecom_status_webhook_key') or self.config.get('wecom_webhook_key', '')
        status_url = self.config.get('wecom_webhook_url', '')
        if status_key or status_url:
            self.notifier = WeixinNotifier(
                webhook_key=status_key,
                webhook_url=status_url or None,
                bot_name="系统通知"
            )
            logger.info("微信状态通知组件已初始化")
        else:
            logger.warning("未配置微信状态通知")

        # 哨兵事件机器人：哨兵检测事件通知
        sentry_key = self.config.get('wecom_sentry_webhook_key', '')
        if sentry_key:
            self.sentry_notifier = WeixinNotifier(webhook_key=sentry_key, bot_name="哨兵事件")
            logger.info("微信哨兵事件通知组件已初始化")
        else:
            logger.info("未配置专用哨兵事件机器人，哨兵事件将通过状态机器人发送")
        
        # 2. 视频预览
        self.preview_generator = VideoPreviewGenerator(
            watermark_enabled=self.config.get('watermark_enabled', True)
        )
        logger.info("视频预览组件已初始化")
        
        # 3. 位置检测
        try:
            self.location_detector = init_location_detector({
                'teslamate_url': self.config.get('teslamate_url'),
                'home_location': self.config.get('home_location'),
                'home_wifi_ssids': self.config.get('home_wifi_ssids', []),
                'hotspot_ssids': self.config.get('hotspot_ssids', []),
                'wifi_interface': self.config.get('wifi_interface', 'wlan0'),
                'teslamate_password': self.config.get('teslamate_password'),
            })
            logger.info("位置检测组件已初始化")
        except Exception as e:
            logger.error(f"位置检测初始化失败: {e}")
        
        # 4. WiFi 切换
        try:
            if WifiSwitcher is not None:
                self.wifi_switcher = WifiSwitcher()
                logger.info('WiFi 切换组件已初始化')
            else:
                logger.info('WiFi 切换模块未安装，跳过初始化')
        except Exception as e:
            logger.error(f"WiFi 切换初始化失败: {e}")
        
        # 5. 哨兵监控
        watchdog_config = {
            'sentry_clips_path': self.config.get('sentry_clips_path'),
            'state_file': self.config.get('state_file'),
            'home_delay_minutes': self.config.get('home_delay_minutes', 30),
            'away_confirm_timeout_minutes': self.config.get('away_confirm_timeout_minutes', 30),
            'scan_interval_seconds': self.config.get('scan_interval_seconds', 10),
            'preview_enabled': self.config.get('preview_enabled', True),
            'watermark_enabled': self.config.get('watermark_enabled', True),
            'nas_base_path': self.config.get('nas_base_path'),
            'upload_enabled': self.config.get('upload_enabled', True),
        }
        
        # 初始化上传调度器（通过单例获取/创建）
        try:
            from upload_scheduler import get_upload_scheduler
            self.upload_scheduler = get_upload_scheduler()
            logger.info("上传调度器已获取")
        except Exception as e:
            logger.error(f"获取上传调度器失败: {e}")
        
        # 初始化 SEI 决策客户端
        try:
            from sei_service import get_sei_client
            self.sei_client = get_sei_client()
            logger.info("SEI 决策客户端已获取")
        except Exception as e:
            logger.warning(f"获取 SEI 客户端失败: {e}")
            self.sei_client = None
        
        self.watchdog = init_watchdog(
            config=watchdog_config,
            location_detector=self.location_detector,
            wifi_switcher=self.wifi_switcher,
            upload_scheduler=self.upload_scheduler,
            sei_client=self.sei_client
        )
        
        # 设置回调
        self.watchdog.on_new_event = self._on_new_event
        self.watchdog.on_upload_start = self._on_upload_start
        self.watchdog.on_upload_complete = self._on_upload_complete
        self.watchdog.on_upload_failed = self._on_upload_failed
        self.watchdog.on_confirm_request = self._on_confirm_request
        
        logger.info("哨兵监控组件已初始化")
    
    # ============= 回调处理 =============
    
    def _safe_read_event_json(self, event_json_path):
        """安全读取 event.json（处理二进制/非UTF-8格式）
        
        Tesla 约 5% 的 event.json 为 protobuf 二进制格式，首字节非 '{'。
        直接 open(f, 'r') 会导致 UnicodeDecodeError。
        此方法先检测二进制首字节，多编码回退，失败返回空 dict。
        
        Returns:
            dict: 解析后的 JSON 数据（失败返回 {}）
        """
        try:
            if not event_json_path.exists():
                return {}
            # 先读二进制检测格式
            with open(event_json_path, 'rb') as f:
                raw = f.read()
            if not raw:
                return {}
            # 检测首字节：0x7b = '{' (JSON), 其他 = binary/protobuf
            if raw[0] != 0x7b:
                logger.debug(f"event.json 非 JSON 格式 (首字节 0x{raw[0]:02x}): {event_json_path}")
                return {}
            # 多编码尝试解码
            for encoding in ('utf-8', 'utf-16', 'latin-1'):
                try:
                    text = raw.decode(encoding)
                    return json.loads(text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            logger.warning(f"event.json 无法解码: {event_json_path}")
            return {}
        except Exception as e:
            logger.warning(f"读取 event.json 失败: {event_json_path}: {e}")
            return {}

    def _read_event_location(self, event):
        """读取 event.json 获取真实位置信息"""
        real_location = event.location_status
        event_reason = None
        event_coords = None
        try:
            event_json_path = event.folder_path / 'event.json'
            ev_data = self._safe_read_event_json(event_json_path)
            if ev_data:
                city = ev_data.get('city', '')
                street = ev_data.get('street', '')
                if city:
                    real_location = f"{city} {street}".strip() if street else city
                if ev_data.get('est_lat') and ev_data.get('est_lon'):
                    event_coords = f"{ev_data['est_lat']}, {ev_data['est_lon']}"
                event_reason = ev_data.get('reason')
        except Exception as e:
            logger.warning(f"读取 event.json 失败: {e}")
        return real_location, event_reason, event_coords

    def _on_new_event(self, event):

        """新事件回调 - 生成预览 + 推送微信通知
        
        注意：外出模式(away)事件的通知由 _on_confirm_request 回调专门负责发送，
        因为外出事件需要包含 6 位确认码；本回调仅处理在家模式(home)事件的通知。
        但预览图生成对两种模式都需要执行。
        """
        logger.info(f"【新事件】{event.id} @ {event.location_status}")

        # 空事件过滤：跳过无视频的事件（Tesla 可能创建空事件后立即取消）
        if event.file_count == 0:
            logger.info(f"跳过空事件（无视频文件）: {event.id}")
            return

        # 读取 event.json 获取真实位置信息
        real_location, event_reason, event_coords = self._read_event_location(event)

        # 生成四宫格预览图（使用真实位置信息）—— 所有模式都需要
        preview_path = None
        if self.preview_generator:
            try:
                results = self.preview_generator.generate_sentry_grid_preview(
                    event_folder=event.folder_path,
                    event_id=event.id,
                    timestamp=event.timestamp,
                    location=real_location  # 真实地址而非 home/away
                )
                preview_path = results.get('grid_preview')
                if preview_path:
                    event.preview_path = preview_path
                    logger.info(f"预览图生成成功: {preview_path}")

                    # 标记 preview_queue.json（如果存在），避免后台重复处理
                    import json
                    from pathlib import Path
                    queue_file = Path('/opt/teslausb-web/data/preview_queue.json')
                    if queue_file.exists():
                        try:
                            with open(queue_file, 'r', encoding='utf-8') as f:
                                queue = json.load(f)
                            queue = [e for e in queue if e.get('event_id') != event.id]
                            with open(queue_file, 'w', encoding='utf-8') as f:
                                json.dump(queue, f, indent=2, ensure_ascii=False)
                            logger.debug(f"已从队列移除: {event.id}")
                        except Exception as qe:
                            logger.warning(f"更新队列失败: {qe}")
            except Exception as e:
                logger.error(f"预览生成失败: {e}")

        # 外出模式(away)：跳过通知，_on_confirm_request 将发送含确认码的通知
        # 此时预览图已生成，_on_confirm_request 会复用 event.preview_path
        if event.location_status != "home":
            logger.info(f"外出事件 {event.id} 跳过基础通知，等待确认码生成后由 _on_confirm_request 推送")
            return

        # 在家模式(home)：直接推送通知（无需确认码）
        notifier = self.sentry_notifier or self.notifier
        if notifier:
            try:
                success = notifier.send_sentry_detected(
                    event_id=event.id,
                    location=real_location,
                    file_count=event.file_count,
                    reason=event_reason,
                    coordinates=event_coords,
                    preview_path=preview_path
                )
                if success:
                    # 推送成功 → 标记已通知（供对账补发去重，避免重复补发）
                    self._mark_notified(event.id)
                else:
                    # 发送失败 → 加入重试队列
                    self._enqueue_notification(event, real_location, event_reason,
                                               event_coords, preview_path, None)

            except Exception as e:
                logger.error(f"哨兵事件通知失败: {e}")
                self._enqueue_notification(event, real_location, event_reason,
                                           event_coords, preview_path, None)

    def _on_upload_start(self, event):
        """上传开始回调"""
        logger.info(f"【上传开始】{event.id}")
        
        # 微信通知
        if self.notifier:
            try:
                self.notifier.send_text(
                    f"哨兵视频开始上传\n事件: {event.id[:8]}...\n文件数: {event.file_count}"
                )
            except Exception as e:
                logger.error(f"上传通知失败: {e}")
    
    def _on_upload_complete(self, event):
        """上传完成回调"""
        logger.info(f"【上传完成】{event.id}")
        
        # 微信通知
        if self.notifier:
            try:
                self.notifier.send_upload_complete(
                    event_id=event.id,
                    file_count=event.file_count,
                    total_size="N/A",
                    nas_path=event.nas_path
                )
            except Exception as e:
                logger.error(f"完成通知失败: {e}")
    
    def _on_upload_failed(self, event, error):
        """上传失败回调"""
        logger.error(f"【上传失败】{event.id}: {error}")
        
        # 微信通知
        if self.notifier:
            try:
                self.notifier.send_upload_failed(
                    event_id=event.id,
                    error=error
                )
            except Exception as e:
                logger.error(f"失败通知失败: {e}")
    
    def _on_confirm_request(self, event, confirmation_code):
        """确认请求回调"""
        logger.info(f"【确认请求】{event.id}, 码: {confirmation_code}")

        # 空事件过滤：跳过无视频的确认请求
        if event.file_count == 0:
            logger.info(f"跳过空事件确认请求（无视频文件）: {event.id}")
            return

        # 读取真实位置信息
        real_location, event_reason, event_coords = self._read_event_location(event)

        # 预览图已在 _on_new_event 中生成，直接使用
        preview_path = getattr(event, 'preview_path', None)

        # 微信推送（优先使用哨兵事件机器人）
        notifier = self.sentry_notifier or self.notifier
        if notifier:
            try:
                success = notifier.send_sentry_detected(
                    event_id=event.id,
                    location=real_location,
                    file_count=event.file_count,
                    reason=event_reason,
                    coordinates=event_coords,
                    preview_path=preview_path,
                    confirmation_code=confirmation_code
                )
                if success:
                    self._mark_notified(event.id)
                else:
                    self._enqueue_notification(event, real_location, event_reason,
                                               event_coords, preview_path, confirmation_code)

            except Exception as e:
                logger.error(f"确认请求通知失败: {e}")
                self._enqueue_notification(event, real_location, event_reason,
                                           event_coords, preview_path, confirmation_code)

    def _enqueue_notification(self, event, real_location, event_reason, event_coords,
                              preview_path, confirmation_code):
        """将失败的通知加入重试队列"""
        try:
            from sentry_notify_queue import SentryNotifyQueue
            queue = SentryNotifyQueue()
            queue.enqueue(
                event_id=event.id,
                location=real_location,
                file_count=event.file_count,
                confirmation_code=confirmation_code,
                reason=event_reason,
                coordinates=event_coords,
                preview_path=preview_path
            )
            logger.info(f"通知已加入重试队列: {event.id}")
        except Exception as e:
            logger.error(f"加入通知队列失败: {e}")

    def _mark_notified(self, event_id):
        """标记事件已成功通知（供对账补发去重，避免重复补发已通知的事件）"""
        try:
            from sentry_notify_queue import SentryNotifyQueue
            SentryNotifyQueue().mark_notified(event_id)
        except Exception as e:
            logger.warning(f"标记已通知失败: {e}")

    def _is_tesla_wifi(self) -> bool:
        """
        检查当前是否连接到 Tesla 车机 WiFi

        Returns:
            True 如果 SSID 以 TESLA_SSID_PREFIX 开头，或配置了 disable_ssid_check
        """
        # 紧急关断开关：配置文件设置了 disable_ssid_check
        if self.config.get("disable_ssid_check", False):
            logger.info("SSID 检查已通过配置禁用 (disable_ssid_check=True)")
            return True

        try:
            from config import TESLA_SSID_PREFIX
            output = subprocess.check_output(
                ['iwgetid', '-r'], stderr=subprocess.DEVNULL, text=True
            ).strip()
            if not output:
                logger.warning("未连接到任何 WiFi")
                return False
            is_tesla = output.lower().startswith(TESLA_SSID_PREFIX.lower())
            if not is_tesla:
                logger.info(f"当前 SSID='{output}' 不匹配前缀 '{TESLA_SSID_PREFIX}'")
            return is_tesla
        except FileNotFoundError:
            # iwgetid 不可用（非 Linux 或开发环境），默认放行
            logger.debug("iwgetid 不可用，默认放行 SSID 检查")
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(f"获取 SSID 失败: {e}")
            return False
        except Exception as e:
            logger.warning(f"SSID 检查异常: {e}，默认放行")
            return True

    def start(self):
        """启动服务"""
        if self._running:
            logger.warning("服务已在运行")
            return
        
        # ── SSID 门控：仅 Tesla WiFi 下启动哨兵监控 ──
        if not self._is_tesla_wifi():
            logger.info("未连接到 Tesla WiFi，跳过哨兵监控启动")
            return False
        
        logger.info("="*60)
        logger.info("启动 TeslaUSB-Neo 哨兵服务")
        logger.info("="*60)
        
        self._init_components()
        
        if self.watchdog:
            self.watchdog.start()
            self._running = True
            logger.info("服务已启动")
        else:
            logger.error("服务启动失败: 哨兵监控未初始化")
    
    def stop(self):
        """停止服务"""
        if not self._running:
            return
        
        logger.info("停止服务...")
        self._running = False
        
        if self.watchdog:
            self.watchdog.stop()
        
        logger.info("服务已停止")
    
    def run(self):
        """运行服务（阻塞）"""
        self.start()
        
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到中断信号")
        finally:
            self.stop()
    
    # ============= 命令处理 =============
    
    def handle_confirm(self, event_id: str, code: str) -> bool:
        """
        处理用户确认
        
        Args:
            event_id: 事件ID
            code: 确认码
            
        Returns:
            是否成功
        """
        if not self.watchdog:
            logger.error("服务未运行")
            return False
        
        return self.watchdog.confirm_upload(event_id, code)
    
    def get_status(self) -> Dict:
        """获取服务状态"""
        if not self.watchdog:
            return {'status': 'stopped', 'events': []}
        
        return {
            'status': 'running' if self._running else 'stopped',
            'events': self.watchdog.get_all_events(),
            'pending_confirm': len(self.watchdog.get_all_events(SentryEventStatus.PENDING_CONFIRM)),
            'uploading': len(self.watchdog.get_all_events(SentryEventStatus.UPLOADING)),
        }


# 命令行入口
def main():
    parser = argparse.ArgumentParser(description='TeslaUSB-Neo 哨兵服务')
    parser.add_argument('--config', '-c', type=Path, 
                       default=Path('/opt/radxa_data/teslausb/config/sentry.json'),
                       help='配置文件路径')
    parser.add_argument('--command', type=str, choices=['start', 'stop', 'status', 'test'],
                       default='start', help='命令')
    parser.add_argument('--event-id', type=str, help='事件ID（确认命令）')
    parser.add_argument('--code', type=str, help='确认码（确认命令）')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    
    args = parser.parse_args()
    
    service = SentryService(config_path=args.config)
    
    if args.command == 'start':
        service.run()
    
    elif args.command == 'status':
        status = service.get_status()
        print(json.dumps(status, indent=2, ensure_ascii=False))
    
    elif args.command == 'test':
        service._init_components()
        if service.notifier:
            print("发送测试消息...")
            service.notifier.send_test_message()
        else:
            print("未配置微信推送")
    
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
