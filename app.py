#!/usr/bin/env python3
"""
TeslaUSB Web Management System — 模块化版本 v88
app.py 仅负责：启动 Flask、注册 Blueprint、后台线程、启动入口
所有帮助函数已移至 utils/app_helpers.py
"""
import os, logging, threading
from flask import Flask
from app_state import state

# ── 导入所有帮助函数 ──
from utils.app_helpers import *
# 显式导入下划线前缀的函数（from * 不导出 _xxx）
from utils.app_helpers import _stats_broadcaster, _log_broadcaster, _generate_thumbnail
import video_service, sync_service, staging_service, cloud_archive_service, cloud_rclone_service

app = Flask(__name__)

# ─────────────────────────────────────────────
# 缓存策略：HTML 页面禁止缓存，避免浏览器沿用部署前的旧播放页
# （曾导致播放页"裁剪"按钮点击无反应——旧页里函数未定义）
# ─────────────────────────────────────────────
@app.after_request
def _no_cache_html(resp):
    if resp.mimetype == 'text/html':
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    return resp

# ─────────────────────────────────────────────
# Blueprint 注册
# ─────────────────────────────────────────────
from routes.lockchime_routes import lockchime_bp
app.register_blueprint(lockchime_bp)

from routes.wifi_routes import wifi_bp
app.register_blueprint(wifi_bp)

from routes.cleanup_routes import cleanup_bp
app.register_blueprint(cleanup_bp)

from routes.analytics_routes import analytics_bp
app.register_blueprint(analytics_bp)

from routes.cloud_routes import cloud_bp
app.register_blueprint(cloud_bp)

from routes.system_routes import system_bp
app.register_blueprint(system_bp)

from routes.video_routes import video_bp
app.register_blueprint(video_bp)

from routes.media_routes import media_bp
app.register_blueprint(media_bp)

from routes.misc_routes import misc_bp
app.register_blueprint(misc_bp)

from routes.camera_routes import camera_bp
app.register_blueprint(camera_bp)

# ─────────────────────────────────────────────
# 启动入口
# ─────────────────────────────────────────────
if __name__ == '__main__':
    app.logger.setLevel(logging.DEBUG)
    app.logger.info("🚀 启动 TeslaUSB Web 服务...")
    
    # ── 启动 SSE 广播器 ──
    bg_stats = threading.Thread(target=_stats_broadcaster, daemon=True, name="sse-stats")
    bg_stats.start()
    app.logger.info("SSE 统计广播器已启动")
    
    # ── 启动日志广播器 ──
    bg_logs = threading.Thread(target=_log_broadcaster, daemon=True, name="sse-logs")
    bg_logs.start()
    app.logger.info("SSE 日志广播器已启动")
    
    # ── 初始化上传调度器 ──
    from config import SENTRY_CLIPS_PATH, DATA_DIR
    from upload_scheduler import UploadScheduler
    upload_scheduler = UploadScheduler(
        queue_db_path=os.path.join(DATA_DIR, "sentry_queue.db"),
        sentry_path=SENTRY_CLIPS_PATH,
    )
    upload_scheduler.start()
    app.logger.info(f"上传调度器已启动 (sentry_path={SENTRY_CLIPS_PATH})")
    
    # ── 启动系统监控守护线程 ──
    from system_monitor import SystemMonitor
    system_monitor = SystemMonitor()
    monitor_thread = threading.Thread(
        target=system_monitor.run_daemon,
        kwargs={'interval': 60},
        daemon=True,
        name="system-monitor",
    )
    monitor_thread.start()
    app.logger.info("系统监控守护线程已启动")

    # ── TeslaCam 只读挂载缓存一致性任务（修复 Present 模式货不对板）──
    # Present 模式下 /mnt/teslacam 与 Gadget 可写 LUN 共享 nvme0n1p2，
    # 本地 ro 挂载的 VFS 缓存不会随特斯拉写入失效，需周期性丢弃以读到最新内容。
    try:
        from utils.cache_coherency import start_cache_coherency_task
        start_cache_coherency_task(interval=30)
        app.logger.info("TeslaCam 缓存一致性任务已启动")
    except Exception as e:
        app.logger.warning("缓存一致性任务启动失败（不影响主服务）: %s", e)
    
    # ── 开机通知 ──
    try:
        from weixin_notifier import WeixinNotifier
        notifier = WeixinNotifier(bot_name="系统通知")
        notifier.send_text("A7Z 哨兵系统已启动")
        app.logger.info("开机通知已发送")
    except Exception as e:
        app.logger.warning(f"开机通知发送失败: {e}")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
