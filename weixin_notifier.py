#!/usr/bin/env python3

"""
TeslaUSB-Neo 企业微信通知模块
=============================

功能：
1. 企业微信机器人消息推送
2. 支持文本、Markdown、图片消息
3. 哨兵事件推送模板
4. 上传进度通知
5. 异常告警
6. 推送健康追踪

作者: TeslaUSB-Neo 项目
版本: 1.1.0
"""

import os
import json
import base64
import hashlib
import logging
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from io import BytesIO
import threading

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('weixin_notifier')

# 推送健康数据目录
DATA_DIR = "/opt/teslausb-web/data"
PUSH_HEALTH_FILE = os.path.join(DATA_DIR, "push_health.json")


@dataclass
class WeComConfig:
    """企业微信配置"""
    webhook_key: str          # 机器人 webhook key
    webhook_url: Optional[str] = None  # 完整 webhook URL (可选)
    bot_name: str = "default"  # 机器人名称（用于健康追踪）

    def get_webhook_url(self) -> str:
        """获取 webhook URL"""
        if self.webhook_url:
            return self.webhook_url
        return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={self.webhook_key}"


class PushHealthTracker:
    """
    推送健康追踪器
    
    记录每次推送的成功/失败状态，用于监控面板显示
    """
    
    _lock = threading.Lock()
    
    @classmethod
    def _load_health_data(cls) -> dict:
        """加载健康数据"""
        if not os.path.exists(PUSH_HEALTH_FILE):
            return {"bots": {}}
        try:
            with open(PUSH_HEALTH_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载推送健康数据失败: {e}")
            return {"bots": {}}
    
    @classmethod
    def _save_health_data(cls, data: dict):
        """保存健康数据"""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(PUSH_HEALTH_FILE, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"保存推送健康数据失败: {e}")
    
    @classmethod
    def record_push(cls, bot_key: str, bot_name: str, success: bool, error_msg: str = ""):
        """
        记录一次推送
        
        Args:
            bot_key: 机器人 key (用于唯一标识)
            bot_name: 机器人名称 (用于显示)
            success: 是否成功
            error_msg: 错误信息
        """
        with cls._lock:
            data = cls._load_health_data()
            
            # 使用 key 的后 8 位作为标识
            bot_id = bot_key[-8:] if len(bot_key) >= 8 else bot_key
            
            if bot_id not in data["bots"]:
                data["bots"][bot_id] = {
                    "name": bot_name,
                    "key_suffix": bot_id,
                    "total_pushes": 0,
                    "success_count": 0,
                    "fail_count": 0,
                    "last_success": None,
                    "last_fail": None,
                    "last_error": None,
                    "recent_failures": []
                }
            
            bot = data["bots"][bot_id]
            bot["name"] = bot_name  # 更新名称
            bot["total_pushes"] += 1
            
            now = datetime.now().isoformat()
            
            if success:
                bot["success_count"] += 1
                bot["last_success"] = now
            else:
                bot["fail_count"] += 1
                bot["last_fail"] = now
                bot["last_error"] = error_msg
                
                # 保留最近 5 次失败记录
                bot["recent_failures"].append({
                    "time": now,
                    "error": error_msg
                })
                bot["recent_failures"] = bot["recent_failures"][-5:]
            
            cls._save_health_data(data)
    
    @classmethod
    def get_health_summary(cls) -> dict:
        """获取健康摘要"""
        with cls._lock:
            data = cls._load_health_data()
            return data


class WeixinNotifier:
    """
    企业微信通知器

    支持：
    - 文本消息
    - Markdown 消息
    - 图片消息
    - 图文消息 (news)
    - 推送健康追踪
    """

    def __init__(self, webhook_key: str = None, webhook_url: str = None, bot_name: str = "default"):
        """
        初始化通知器

        Args:
            webhook_key: 机器人 webhook key
            webhook_url: 完整 webhook URL
            bot_name: 机器人名称（用于健康追踪）
        """
        # 优先使用传入的参数，其次环境变量，其次配置文件
        key = webhook_key or os.environ.get('WECOM_WEBHOOK_KEY', '')
        if not key:
            try:
                from config_manager import get_wecom_keys
                keys = get_wecom_keys()
                if bot_name == "哨兵事件":
                    key = keys.get('sentry_key', '')
                elif bot_name == "系统通知":
                    key = keys.get('status_key', '')
            except Exception:
                pass
        url = webhook_url or os.environ.get('WECOM_WEBHOOK_URL', '')

        self.config = WeComConfig(
            webhook_key=key,
            webhook_url=url if url else None,
            bot_name=bot_name
        )

        self.session = requests.Session()
        self.session.headers.update({'Content-Type': 'application/json'})

        # 脱敏：只打印 key 后6位，避免完整 key 出现在日志/stdout 中
        key_display = f"...{key[-6:]}" if key and len(key) >= 6 else ("(空)" if not key else key)
        logger.info(f"企业微信通知器初始化完成 (bot_name={bot_name}, key_suffix={key_display})")

    def _send_request(self, data: dict, timeout: int = 30) -> Tuple[bool, str]:
        """
        发送请求到企业微信

        Args:
            data: 消息数据
            timeout: 超时秒数（同时用作 connect 和 read 超时）

        Returns:
            (成功, 错误信息)
        """
        try:
            url = self.config.get_webhook_url()
            # 使用 (connect_timeout, read_timeout) 元组，防止 DNS/TCP 连接卡死
            response = self.session.post(
                url,
                json=data,
                timeout=(10, timeout)  # connect=10s, read=timeout
            )

            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}"
                PushHealthTracker.record_push(
                    self.config.webhook_key,
                    self.config.bot_name,
                    False,
                    error_msg
                )
                return False, error_msg

            result = response.json()
            if result.get('errcode') == 0:
                PushHealthTracker.record_push(
                    self.config.webhook_key,
                    self.config.bot_name,
                    True
                )
                return True, ""
            else:
                error_msg = f"WeCom API Error: {result.get('errmsg')}"
                PushHealthTracker.record_push(
                    self.config.webhook_key,
                    self.config.bot_name,
                    False,
                    error_msg
                )
                return False, error_msg

        except requests.exceptions.Timeout:
            error_msg = "请求超时"
            PushHealthTracker.record_push(
                self.config.webhook_key,
                self.config.bot_name,
                False,
                error_msg
            )
            return False, error_msg
        except requests.exceptions.ConnectionError:
            error_msg = "连接失败"
            PushHealthTracker.record_push(
                self.config.webhook_key,
                self.config.bot_name,
                False,
                error_msg
            )
            return False, error_msg
        except Exception as e:
            error_msg = str(e)
            PushHealthTracker.record_push(
                self.config.webhook_key,
                self.config.bot_name,
                False,
                error_msg
            )
            return False, error_msg

    def send_text(self, text: str, mentioned_list: List[str] = None) -> bool:
        """
        发送文本消息

        Args:
            text: 文本内容
            mentioned_list: @用户列表

        Returns:
            是否发送成功
        """
        data = {
            "msgtype": "text",
            "text": {
                "content": text
            }
        }
        if mentioned_list:
            data["text"]["mentioned_list"] = mentioned_list

        success, error = self._send_request(data)
        if not success:
            logger.warning(f"发送文本消息失败: {error}")
        return success

    def send_markdown(self, content: str) -> bool:
        """
        发送 Markdown 消息

        Args:
            content: Markdown 内容

        Returns:
            是否发送成功
        """
        data = {
            "msgtype": "markdown",
            "markdown": {
                "content": content
            }
        }

        success, error = self._send_request(data)
        if not success:
            logger.warning(f"发送 Markdown 消息失败: {error}")
        return success

    def send_image(self, image_path: str) -> bool:
        """
        发送图片消息（含自动重试）

        Args:
            image_path: 图片路径

        Returns:
            是否发送成功
        """
        try:
            with open(image_path, 'rb') as f:
                image_data = f.read()

            # 检查图片大小（企业微信限制 2MB）
            if len(image_data) > 2 * 1024 * 1024:
                logger.warning(f"图片超过 2MB 限制: {image_path}")
                return False

            # Base64 编码
            base64_data = base64.b64encode(image_data).decode('utf-8')

            # 计算 MD5
            md5_hash = hashlib.md5(image_data).hexdigest()

            data = {
                "msgtype": "image",
                "image": {
                    "base64": base64_data,
                    "md5": md5_hash
                }
            }

            # 最多重试 2 次（网络不稳定时很关键）
            for attempt in range(3):
                success, error = self._send_request(data, timeout=120)
                if success:
                    return True
                if attempt < 2:
                    logger.warning(f"发送图片失败 (第{attempt+1}次): {error}, 2秒后重试...")
                    import time
                    time.sleep(2)
            logger.warning(f"发送图片消息失败(已重试3次): {error}")
            return False

        except FileNotFoundError:
            logger.error(f"图片文件不存在: {image_path}")
            return False
        except Exception as e:
            logger.error(f"发送图片消息异常: {e}")
            return False

    def send_image_from_bytes(self, image_bytes: bytes) -> bool:
        """
        从字节数据发送图片

        Args:
            image_bytes: 图片字节数据

        Returns:
            是否发送成功
        """
        try:
            # 检查图片大小
            if len(image_bytes) > 2 * 1024 * 1024:
                logger.warning("图片超过 2MB 限制")
                return False

            # Base64 编码
            base64_data = base64.b64encode(image_bytes).decode('utf-8')

            # 计算 MD5
            md5_hash = hashlib.md5(image_bytes).hexdigest()

            data = {
                "msgtype": "image",
                "image": {
                    "base64": base64_data,
                    "md5": md5_hash
                }
            }

            success, error = self._send_request(data)
            if not success:
                logger.warning(f"发送图片消息失败: {error}")
            return success

        except Exception as e:
            logger.error(f"发送图片消息异常: {e}")
            return False

    def send_news(self, articles: List[Dict]) -> bool:
        """
        发送图文消息

        Args:
            articles: 文章列表，每篇文章包含 title, url, description, picurl

        Returns:
            是否发送成功
        """
        data = {
            "msgtype": "news",
            "news": {
                "articles": articles
            }
        }

        success, error = self._send_request(data)
        if not success:
            logger.warning(f"发送图文消息失败: {error}")
        return success

    def send_sentry_detected(self, event_id: str, location: str, file_count: int,
                           confirmation_code: str = None,
                           reason: str = None,
                           coordinates: str = None,
                           temperature: float = None,
                           locked: bool = None,
                           preview_path: str = None,
                           is_reconciled: bool = False) -> bool:
        """
        发送哨兵事件检测通知（文本格式，兼容所有微信客户端）

        Args:
            event_id: 事件 ID
            location: 地点描述
            file_count: 文件数量
            confirmation_code: 确认码（外出模式）
            reason: 触发原因
            coordinates: 坐标
            temperature: 温度
            locked: 车辆锁定状态
            preview_path: 预览图路径
            is_reconciled: 是否为对账补发（reconcile 兜底补发的遗漏事件）

        Returns:
            是否发送成功
        """
        # 构建消息内容（纯文本格式）
        if is_reconciled:
            lines = [
                "🔄 哨兵事件检测【补发】",
                "👇 以下为网络中断/断网遗留的遗漏事件",
                ""
            ]
        else:
            lines = [
                "🚨 哨兵事件检测",
                ""
            ]

        # 地点
        lines.append(f"📍 地点: {location}")
        
        # 坐标
        if coordinates:
            lines.append(f"📌 坐标: {coordinates}")
        
        # 触发原因
        if reason:
            reason_labels = {
                "sentry_aware_object_detection": "物体检测",
                "sentry_aware_object_detection_sensitivity": "灵敏度检测",
                "user_detect_humans": "人体检测",
                "user_detect_dog": "狗检测",
                "user_detect_cat": "猫检测",
                "user_detect_all": "全模式检测"
            }
            label = reason_labels.get(reason, reason)
            lines.append(f"⚡ 触发: {label}")

        # 文件数量
        lines.append(f"📹 文件: {file_count} 个视频片段")

        # 温度
        if temperature is not None:
            lines.append(f"🌡️ 温度: {temperature:.1f}°C")

        # 车锁状态
        if locked is not None:
            lock_status = "🔒 已锁定" if locked else "🔓 未锁定"
            lines.append(f"🔐 状态: {lock_status}")

        # 确认码（外出模式）
        if confirmation_code:
            lines.append("")
            lines.append(f"⚠️ 确认码: {confirmation_code}")
            lines.append("回复确认码以允许上传")

        # 时间 — 从 event_id 解析事件触发时间（而非 datetime.now()）
        # event_id 格式: "2026-06-29_11-25-20"，日期和时间的分隔符不同
        try:
            parts = event_id.split('_')
            if len(parts) == 2:
                date_str = parts[0]                     # "2026-06-29"
                time_str = parts[1].replace('-', ':')   # "11:25:20"
                ts_str = f"{date_str} {time_str}"
                dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            else:
                time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, Exception):
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines.append(f"时间: {time_str}")

        content = "\n".join(lines)

        # 先发送文本消息
        text_success = self.send_text(content)

        # 如果有预览图，发送图片
        if preview_path and os.path.exists(preview_path):
            # 延迟一下，避免消息顺序颠倒
            import time
            time.sleep(0.5)
            img_success = self.send_image(preview_path)
            if not img_success:
                logger.warning(f"预览图发送失败: {preview_path}")

        return text_success

    def send_upload_progress(self, event_id: str, progress: int,
                            current_file: str = None) -> bool:
        """
        发送上传进度通知

        Args:
            event_id: 事件 ID
            progress: 进度百分比 (0-100)
            current_file: 当前上传的文件名

        Returns:
            是否发送成功
        """
        progress_bar = "█" * (progress // 10) + "░" * (10 - progress // 10)

        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        content = f"📤 上传进度\n事件: {event_id[:16]}...\n进度: {progress_bar} {progress}%\n{f'当前: {current_file}' if current_file else ''}\n时间: {ts}"
        return self.send_text(content)

    def send_upload_complete(self, event_id: str, file_count: int,
                            total_size: str, nas_path: str = None) -> bool:
        """
        发送上传完成通知

        Args:
            event_id: 事件 ID
            file_count: 文件数量
            total_size: 总大小
            nas_path: NAS 路径

        Returns:
            是否发送成功
        """
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        content = f"✅ 上传完成\n事件: {event_id[:16]}...\n文件: {file_count} 个\n大小: {total_size}\n{f'路径: {nas_path}' if nas_path else ''}\n时间: {ts}"
        return self.send_text(content)

    def send_upload_failed(self, event_id: str, error: str,
                          retry_count: int = 0) -> bool:
        """
        发送上传失败通知

        Args:
            event_id: 事件 ID
            error: 错误信息
            retry_count: 重试次数

        Returns:
            是否发送成功
        """
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        content = f"❌ 上传失败\n事件: {event_id[:16]}...\n错误: {error}\n重试: {retry_count} 次\n时间: {ts}"
        return self.send_text(content)

    def send_system_alert(self, title: str, message: str, level: str = "warning") -> bool:
        """
        发送系统告警（文本格式，兼容所有微信客户端）

        Args:
            title: 告警标题
            message: 告警内容
            level: 告警级别 (info, warning, error)

        Returns:
            是否发送成功
        """
        icons = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "🚨"
        }
        icon = icons.get(level, "⚠️")
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        content = f"{icon} 系统告警\n{title}\n\n{message}\n\n时间: {ts}"
        return self.send_text(content)

    def send_boot_notification(self, boot_time: str, cpu_info: dict,
                              memory_info: dict, disk_info: list,
                              wifi_info: dict = None, tailscale_ip: str = None,
                              services: dict = None,
                              nvme_temp: float = None,
                              local_ip: str = None) -> bool:
        """
        发送开机通知（增强版格式：NVMe 温度 / SWAP / 磁盘状态图标 / 本地 IP）

        Args:
            boot_time: 启动时间字符串（如 "05-18 18:01（启动耗时 45s）"）
            cpu_info: CPU 信息 (model, freq_mhz, temp_c)
            memory_info: 内存信息 (total_mb, used_mb, pct, swap_total_mb, swap_used_mb, swap_pct)
            disk_info: 磁盘信息列表 (name, total_gb, used_gb, percent, mounted)
            wifi_info: WiFi 信息 (ssid, signal_pct)
            tailscale_ip: Tailscale IP
            services: 服务状态
            nvme_temp: NVMe SSD 温度
            local_ip: 本地网络 IP

        Returns:
            是否发送成功
        """
        lines = [
            "🚀 TeslaUSB A7Z 已启动",
            "",
            f"⏰ {boot_time}",
            ""
        ]

        # CPU 信息（紧凑一行）
        if cpu_info:
            model = cpu_info.get('model', 'Unknown')
            freq = cpu_info.get('freq_mhz', 0)
            temp = cpu_info.get('temp_c', 0)
            parts = [model]
            if freq > 0:
                parts.append(f"{freq}MHz")
            if temp > 0:
                parts.append(f"{temp}°C")
            lines.append(f"🖥️ {' · '.join(parts)}")

        # NVMe 温度
        if nvme_temp is not None and nvme_temp > 0:
            lines.append(f"🌡️ NVMe {nvme_temp}°C")

        # 内存信息
        if memory_info:
            total = memory_info.get('total_mb', 0)
            used = memory_info.get('used_mb', 0)
            pct = memory_info.get('pct', 0)
            lines.append(f"💾 内存 {used}/{total}MB ({pct}%)")

            # SWAP
            swap_total = memory_info.get('swap_total_mb', 0)
            swap_used = memory_info.get('swap_used_mb', 0)
            swap_pct = memory_info.get('swap_pct', 0)
            if swap_total > 0:
                lines.append(f"📀 SWAP {swap_used}/{swap_total}MB ({swap_pct}%)")

        # 磁盘信息（带状态图标）
        if disk_info:
            lines.append("💿 磁盘")
            for disk in disk_info:
                name = disk.get('name', 'Unknown')
                if disk.get('mounted'):
                    used = disk.get('used_gb', 0)
                    total = disk.get('total_gb', 0)
                    pct = disk.get('percent', 0)
                    icon = "🟢" if pct < 70 else ("🟡" if pct < 90 else "🔴")
                    lines.append(f"  {name} {used}/{total}GB ({pct}%) {icon}")
                else:
                    lines.append(f"  {name} 未挂载 ⚠️")

        # WiFi 信息
        if wifi_info:
            connected = wifi_info.get('connected', False)
            ssid = wifi_info.get('ssid', '')
            signal = wifi_info.get('signal_pct', 0)
            if connected and ssid:
                lines.append(f"📶 WiFi {ssid} ({signal}%)")
            elif ssid:
                lines.append(f"📶 WiFi {ssid} (未连接)")
            else:
                lines.append("📶 WiFi 未连接")

        # 本地 IP
        if local_ip and local_ip != "N/A":
            lines.append(f"🏠 本地IP {local_ip}")

        # Tailscale IP
        if tailscale_ip and tailscale_ip != "N/A":
            lines.append(f"🌐 Tailscale {tailscale_ip}")

        # 服务状态
        if services:
            lines.append("⚙️ 服务")
            for name, status in services.items():
                status_icon = "✅" if status else "❌"
                lines.append(f"  {name} {status_icon}")

        lines.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        content = "\n".join(lines)
        return self.send_text(content)

    def send_daily_report(self, events_today: int, uploaded_today: int,
                         failed_today: int, nas_usage: str) -> bool:
        """
        发送日报

        Args:
            events_today: 今日事件数
            uploaded_today: 今日上传数
            failed_today: 今日失败数
            nas_usage: NAS 存储使用情况

        Returns:
            是否发送成功
        """
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        content = f"📊 TeslaUSB 日报\n哨兵事件: {events_today}\n上传成功: {uploaded_today} ✅\n上传失败: {failed_today} {'❌' if failed_today > 0 else '✓'}\nNAS 使用: {nas_usage}\n统计时间: {ts}"
        return self.send_text(content)

    def send_test_message(self) -> bool:
        """发送测试消息"""
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        content = f"🧪 TeslaUSB 微信推送测试\n如果您的企业微信收到这条消息，说明推送配置正确！\n时间: {ts}"
        return self.send_text(content)


def get_push_health() -> dict:
    """获取推送健康数据（供 API 调用）"""
    return PushHealthTracker.get_health_summary()


# 测试代码
if __name__ == '__main__':
    import sys

    print("="*60)
    print("企业微信推送测试")
    print("="*60)

    # 从命令行参数获取 webhook key
    if len(sys.argv) > 1:
        webhook_key = sys.argv[1]
    else:
        webhook_key = input("请输入企业微信机器人 Webhook Key: ").strip()

    if not webhook_key:
        print("错误: 未提供 webhook key")
        sys.exit(1)

    # 初始化
    notifier = WeixinNotifier(webhook_key=webhook_key, bot_name="测试机器人")

    # 发送测试消息
    print("\n发送测试消息...")
    if notifier.send_test_message():
        print("✓ 测试消息发送成功")
    else:
        print("✗ 测试消息发送失败")

    # 查看推送健康数据
    print("\n推送健康数据:")
    health = get_push_health()
    print(json.dumps(health, indent=2, ensure_ascii=False))

    print("\n" + "="*60)
    print("测试完成，请检查企业微信")
    print("="*60)
