#!/usr/bin/env python3
"""
TeslaUSB CL — 哨兵通知重试队列
================================
当网络故障导致哨兵事件通知（文本+截图）发送失败时，
将通知存入持久化队列，待网络恢复后自动重试。

策略：
- 每 30 秒检查网络连通性
- 网络恢复后按 FIFO 顺序重试队列中的通知
- 最多重试 10 次，间隔 5 分钟
- 队列持久化到 JSON 文件，支持断点续传
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

QUEUE_FILE = Path('/opt/radxa_data/teslausb/data/sentry_notify_queue.json')

# 网络连通性检查目标（HTTP HEAD 优先，速度快；ping 做 fallback）
HTTP_CHECK_URLS = [
    'http://www.baidu.com',
    'http://www.qq.com',
    'http://www.aliyun.com',
]
PING_TARGETS = ['8.8.8.8']
CHECK_INTERVAL_SEC = 30
MAX_RETRIES = 10
RETRY_DELAY_SEC = 300
HTTP_TIMEOUT_SEC = 3

# ── 对账补发（reconcile）：以文件系统为真相源，补发"已检测但从未通知"的孤儿事件 ──
# 背景：watchdog 扫到事件即标记 processed 并持久化，若断电/进程中断导致通知
# 既未成功也未入队，事件就永久停留在"已处理但从未通知"状态，重启后被 processed_ids
# 跳过，永不补发。reconcile 独立于 watchdog 的 processed_ids，直接对比文件系统。
NOTIFIED_FILE = Path('/opt/radxa_data/teslausb/data/sentry_notified_events.json')
SENTRY_CONFIG = Path('/opt/radxa_data/teslausb/config/sentry.json')
SENTRY_CLIPS_FALLBACK = '/mnt/teslacam/TeslaCam/SentryClips'
THUMB_DIR = Path('/opt/radxa_data/teslausb/static/thumbnails')
RECONCILE_MAX_AGE_DAYS = 3        # 只补扫最近 N 天的事件，避免历史全量轰炸
# 跳过"较新"的事件：必须给 watchdog 正常推送(发文本+编码上传缩略图+延迟)及随后
# 的 mark_notified 留足时间，否则 reconcile 会抢在 watchdog 完成标记前把"正在被
# 正常推送的事件"误判为孤儿而重复补发。44 文件大事件推送可达数分钟，故取 30 分钟。
# reconcile 只是兜底极端情况(watchdog 标记 processed 后、通知完成前进程崩溃)，
# 延迟补发可接受；断电期间事件由 watchdog 重启后扫描自行正常推送，不依赖此窗口。
RECONCILE_MIN_AGE_SEC = 1800      # 30 分钟：远超 watchdog 任何正常推送耗时
NOTIFIED_MAX_KEEP = 2000          # 已通知记录最多保留条数（FIFO 裁剪）

logger = logging.getLogger("SentryNotifyQueue")
logger.setLevel(logging.INFO)


class SentryNotifyQueue:
    """哨兵事件通知失败重试队列"""

    def __init__(self, queue_file=QUEUE_FILE):
        self.queue_file = queue_file
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self.entries: List[Dict] = self._load()
        self._notifier = None
        self.notified_file = NOTIFIED_FILE

    def _load(self) -> List[Dict]:
        if self.queue_file.exists():
            try:
                with open(self.queue_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except Exception as e:
                logger.warning(f"加载通知队列失败: {e}")
        return []

    def _save(self):
        try:
            with open(self.queue_file, 'w', encoding='utf-8') as f:
                json.dump(self.entries, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存通知队列失败: {e}")

    def enqueue(self, event_id: str, location: str, file_count: int,
                confirmation_code: str = None, reason: str = None,
                coordinates: str = None, preview_path: str = None,
                is_reconciled: bool = False):
        """将失败的通知加入队列（is_reconciled 标记是否为对账补发的遗漏事件）"""
        entry = {
            'event_id': event_id,
            'location': location,
            'file_count': file_count,
            'confirmation_code': confirmation_code,
            'reason': reason,
            'coordinates': coordinates,
            'preview_path': str(preview_path) if preview_path else None,
            'retry_count': 0,
            'added_at': datetime.now().isoformat(),
            'is_reconciled': is_reconciled,
            'last_retry': None,
            'status': 'pending'
        }
        self.entries.append(entry)
        self._save()
        logger.info(f"通知已加入重试队列: {event_id} (队列大小: {len(self.entries)})")

    # ═══════════════════════════════════════════════════════════════
    # 已通知记录 + 对账补发（reconcile）
    # ═══════════════════════════════════════════════════════════════

    def _load_notified(self) -> set:
        """加载已成功通知的事件 ID 集合"""
        if self.notified_file.exists():
            try:
                with open(self.notified_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return set(data)
                    if isinstance(data, dict):
                        return set(data.get('event_ids', []))
            except Exception as e:
                logger.warning(f"加载已通知记录失败: {e}")
        return set()

    def _save_notified(self, notified: set):
        """保存已通知事件 ID 集合（原子写入，FIFO 裁剪）"""
        try:
            ids = list(notified)
            if len(ids) > NOTIFIED_MAX_KEEP:
                ids = ids[-NOTIFIED_MAX_KEEP:]
            self.notified_file.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入：先写临时文件，再 os.replace（POSIX 原子操作，防止断电损坏）
            tmp = str(self.notified_file) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(ids, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(self.notified_file))
        except Exception as e:
            logger.error(f"保存已通知记录失败: {e}")

    def mark_notified(self, event_id: str):
        """标记事件已成功通知（推送成功 / 重试成功后调用，供对账去重）"""
        if not event_id:
            return
        notified = self._load_notified()
        if event_id not in notified:
            notified.add(event_id)
            self._save_notified(notified)

    def _generate_thumbnail_fallback(self, event_folder: Path, event_id: str) -> Optional[str]:
        """补发场景缩略图兜底生成：用 ffmpeg 提取第一个 mp4 文件的首帧。
        
        返回缩略图路径，失败返回 None。
        """
        thumb_path = THUMB_DIR / f"SEN_{event_id}_grid.jpg"
        
        # 已有缩略图则不重复生成
        if thumb_path.exists():
            return str(thumb_path)
        
        # 找第一个有效的 mp4 文件
        first_mp4 = None
        for p in sorted(event_folder.iterdir()):
            if p.is_file() and p.suffix.lower() == '.mp4':
                first_mp4 = p
                break
        if not first_mp4:
            return None
        
        try:
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            # 提取第 2 帧（跳过可能的黑帧/关键帧头）
            result = subprocess.run(
                ['ffmpeg', '-y', '-i', str(first_mp4), '-vf',
                 'select=eq(n\\,2)', '-vframes', '1', '-q:v', '3',
                 str(thumb_path)],
                capture_output=True, text=True, timeout=15,
            )
            if thumb_path.exists() and thumb_path.stat().st_size > 0:
                logger.info(f"补发缩略图生成成功: {event_id}")
                return str(thumb_path)
            else:
                logger.warning(f"补发缩略图生成失败(ffmpeg): {event_id} {result.stderr[-200:]}")
                # 清理空文件
                if thumb_path.exists():
                    thumb_path.unlink()
                return None
        except FileNotFoundError:
            logger.debug("ffmpeg 不可用，跳过补发缩略图生成")
            return None
        except Exception as e:
            logger.warning(f"补发缩略图生成异常: {event_id} {e}")
            return None

    def _safe_read_event_json(self, event_json_path: Path) -> dict:
        """安全读取 event.json（兼容 Tesla 约 5% 的 protobuf 二进制格式）"""
        try:
            if not event_json_path.exists():
                return {}
            with open(event_json_path, 'rb') as f:
                raw = f.read()
            if not raw or raw[0] != 0x7b:  # 0x7b = '{'，非此为二进制/protobuf
                return {}
            for encoding in ('utf-8', 'utf-16', 'latin-1'):
                try:
                    return json.loads(raw.decode(encoding))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
        except Exception as e:
            logger.debug(f"读取 event.json 失败 {event_json_path}: {e}")
        return {}

    def _read_event_meta(self, event_folder: Path):
        """从 event.json 读取位置/原因/坐标（与 sentry_service 解析逻辑一致）"""
        location = '未知位置'
        reason = None
        coords = None
        ev = self._safe_read_event_json(event_folder / 'event.json')
        if ev:
            city = ev.get('city', '')
            street = ev.get('street', '')
            if city:
                location = f"{city} {street}".strip() if street else city
            if ev.get('est_lat') and ev.get('est_lon'):
                coords = f"{ev['est_lat']}, {ev['est_lon']}"
            reason = ev.get('reason')
        return location, reason, coords

    def _sentry_clips_path(self) -> str:
        """获取 SentryClips 目录路径（优先读 sentry.json 配置）"""
        try:
            if SENTRY_CONFIG.exists():
                with open(SENTRY_CONFIG, encoding='utf-8') as f:
                    cfg = json.load(f)
                p = cfg.get('sentry_clips_path')
                if p:
                    return p
        except Exception:
            pass
        return SENTRY_CLIPS_FALLBACK

    def reconcile_missed_events(self) -> int:
        """
        对账补发：以文件系统为真相源，补发"已检测但从未通知"的孤儿事件。

        解决 watchdog 标记 processed 与通知送达解耦导致的丢失：断电/进程中断后，
        事件停留在 state_file 的 processed_ids 中，重启永不补发；本方法直接扫描
        SentryClips，对比已通知记录与当前队列，把遗漏事件补入重试队列。

        首次运行（无已通知记录）执行基线初始化：把当前所有历史事件标记为已通知，
        不补发，避免部署瞬间通知轰炸；仅补发基线建立之后新出现的孤儿事件。
        """
        sentry_path = Path(self._sentry_clips_path())
        if not sentry_path.exists():
            return 0

        now = time.time()
        cutoff = now - RECONCILE_MAX_AGE_DAYS * 86400

        # 扫描最近 N 天、有视频、且非正在写入的事件
        candidates = []  # (event_id, folder)
        try:
            for folder in sentry_path.iterdir():
                if not folder.is_dir():
                    continue
                try:
                    mtime = folder.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                if (now - mtime) < RECONCILE_MIN_AGE_SEC:
                    continue  # 跳过 2 分钟内的最新事件（可能仍在写入）
                has_video = any(
                    p.is_file() and p.suffix.lower() == '.mp4'
                    for p in folder.iterdir()
                )
                if has_video:
                    candidates.append((folder.name, folder))
        except Exception as e:
            logger.error(f"对账扫描 SentryClips 失败: {e}")
            return 0

        notified = self._load_notified()

        # 损坏检测：文件存在但 loaded set 为空 + 有候选事件 → 文件可能在断电中损坏
        # （JSON 被截断/清空导致加载失败返回空集，但文件路径仍存在）
        if self.notified_file.exists() and not notified and candidates:
            logger.warning(
                f"已通知记录文件可能已损坏（文件存在但内容为空），"
                f"执行基线初始化：标记 {len(candidates)} 个候选事件为已通知（不补发）"
            )
            for ev_id, _ in candidates:
                notified.add(ev_id)
            self._save_notified(notified)
            return 0

        # 首次运行：基线初始化，不补发历史
        if not self.notified_file.exists():
            for ev_id, _ in candidates:
                notified.add(ev_id)
            self._save_notified(notified)
            logger.info(f"对账首次运行：基线初始化 {len(candidates)} 个历史事件（不补发）")
            return 0

        queued_ids = {e['event_id'] for e in self.entries}
        added = 0
        for ev_id, folder in candidates:
            if ev_id in notified or ev_id in queued_ids:
                continue
            # 发现孤儿事件 → 补入重试队列
            location, reason, coords = self._read_event_meta(folder)
            file_count = sum(
                1 for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() == '.mp4'
            )
            thumb = THUMB_DIR / f"SEN_{ev_id}_grid.jpg"
            preview = str(thumb) if thumb.exists() else None
            # 如果缩略图不存在，尝试 on-demand 生成（提取第一个视频帧）
            if not preview:
                preview = self._generate_thumbnail_fallback(folder, ev_id)
            self.enqueue(
                event_id=ev_id,
                location=location,
                file_count=file_count,
                reason=reason,
                coordinates=coords,
                preview_path=preview,
                is_reconciled=True,
            )
            added += 1

        if added:
            logger.info(f"对账补发：发现 {added} 个遗漏的哨兵事件，已补入重试队列")
        return added

    def _check_network(self) -> bool:
        """检查网络连通性（HTTP HEAD 优先，速度快；ping 做 fallback）"""
        import urllib.request
        
        # 1. HTTP HEAD 检查（2-3 秒内完成）
        for url in HTTP_CHECK_URLS:
            try:
                req = urllib.request.Request(url, method='HEAD')
                urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC)
                return True
            except Exception:
                continue
        
        # 2. Fallback: ping 检查
        for target in PING_TARGETS:
            try:
                result = subprocess.run(
                    ['ping', '-c', '1', '-W', '3', target],
                    capture_output=True, timeout=4
                )
                if result.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    def _get_notifier(self):
        """获取哨兵事件通知器（延迟导入）"""
        if self._notifier is None:
            try:
                from weixin_notifier import WeixinNotifier
                # 读取哨兵事件 webhook key
                config_path = '/opt/radxa_data/teslausb/config/sentry.json'
                with open(config_path) as f:
                    cfg = json.load(f)
                sentry_key = cfg.get('wecom_sentry_webhook_key', '')
                if sentry_key:
                    self._notifier = WeixinNotifier(webhook_key=sentry_key, bot_name="哨兵事件")
                else:
                    logger.warning("未配置哨兵事件 webhook，无法重试通知")
            except Exception as e:
                logger.error(f"初始化通知器失败: {e}")
        return self._notifier

    def process_pending(self) -> int:
        """
        处理所有待重试的通知，返回成功发送的数量。
        仅在网络连通时调用。
        """
        if not self._check_network():
            return 0

        notifier = self._get_notifier()
        if not notifier:
            return 0

        sent_count = 0
        remaining = []

        for entry in self.entries:
            if entry['status'] != 'pending':
                remaining.append(entry)
                continue

            # 检查重试间隔（避免短时间内反复重试）
            if entry.get('last_retry'):
                last = datetime.fromisoformat(entry['last_retry'])
                if (datetime.now() - last).total_seconds() < RETRY_DELAY_SEC:
                    remaining.append(entry)
                    continue

            event_id = entry['event_id']
            try:
                logger.info(f"重试通知: {event_id} (第 {entry['retry_count'] + 1}/{MAX_RETRIES} 次)")

                # 检查截图是否仍存在
                preview_path = entry.get('preview_path')
                if preview_path and not os.path.exists(preview_path):
                    preview_path = None

                success = notifier.send_sentry_detected(
                    event_id=event_id,
                    location=entry.get('location', '未知'),
                    file_count=entry.get('file_count', 0),
                    confirmation_code=entry.get('confirmation_code'),
                    reason=entry.get('reason'),
                    coordinates=entry.get('coordinates'),
                    preview_path=preview_path,
                    is_reconciled=entry.get('is_reconciled', False),
                )

                if success:
                    sent_count += 1
                    self.mark_notified(event_id)
                    logger.info(f"✅ 通知重试成功: {event_id}")
                    continue  # 不加入 remaining = 已删除

                # 发送失败，增加重试计数
                entry['retry_count'] += 1
                entry['last_retry'] = datetime.now().isoformat()
                if entry['retry_count'] >= MAX_RETRIES:
                    entry['status'] = 'abandoned'
                    logger.warning(f"通知 {event_id} 已放弃（超过最大重试次数）")
                remaining.append(entry)

            except Exception as e:
                entry['retry_count'] += 1
                entry['last_retry'] = datetime.now().isoformat()
                logger.error(f"重试通知异常 {event_id}: {e}")
                remaining.append(entry)

        self.entries = remaining
        self._save()

        if sent_count > 0:
            logger.info(f"本次重试完成: {sent_count} 成功, {len(remaining)} 剩余")
        return sent_count

    def size(self) -> int:
        return len([e for e in self.entries if e['status'] == 'pending'])

    def run_once(self):
        """执行一次：对账补扫遗漏事件 + 网络恢复后重试队列"""
        # 1. 对账补发：以文件系统为真相源，补入"已检测但从未通知"的孤儿事件
        #    （独立于网络，孤儿事件先入队，待网络恢复再发）
        try:
            self.reconcile_missed_events()
        except Exception as e:
            logger.error(f"对账补发异常: {e}")

        # 2. 队列为空则无需重试
        if self.size() == 0:
            return

        # 3. 网络可达才重试
        if not self._check_network():
            logger.debug("网络不可达，跳过重试")
            return

        logger.info(f"网络已恢复，开始处理 {self.size()} 条待重试通知...")
        self.process_pending()


# ═══════════════════════════════════════════════════════════════
# 独立守护进程模式
# ═══════════════════════════════════════════════════════════════

def run_daemon():
    """后台持续运行重试循环"""
    logger.info("=== 哨兵通知重试队列启动 ===")
    logger.info(f"队列文件: {QUEUE_FILE}")
    logger.info(f"检查间隔: {CHECK_INTERVAL_SEC}s, 最大重试: {MAX_RETRIES} 次")

    queue = SentryNotifyQueue()

    while True:
        try:
            queue.run_once()
            time.sleep(CHECK_INTERVAL_SEC)
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
            break
        except Exception as e:
            logger.error(f"守护进程异常: {e}")
            time.sleep(CHECK_INTERVAL_SEC)

    logger.info("哨兵通知重试队列已停止")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='TeslaUSB CL 哨兵通知重试队列')
    parser.add_argument('--daemon', action='store_true', help='后台守护进程模式')
    parser.add_argument('--once', action='store_true', help='执行一次检查后退出')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(handler)

    if args.once:
        q = SentryNotifyQueue()
        q.run_once()
    elif args.daemon:
        run_daemon()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
