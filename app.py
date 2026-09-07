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

# analytics routes (blueprint kept for internal API usage, page removed from nav)
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
    # v0.3.1.31：由"每 30s 无条件全刷"改为"读取驱动 + 后台 60s 兜底"。
    # v0.3.1.35：写入检测由 stat mtime（被 inode 缓存冻结，8-30 实锤失效）
    # 改为 listdir 指纹（文件数+最新文件名，dentry miss 强制读盘可靠）。
    # 读取即时性由各读取入口调用 ensure_fresh() 保证；后台任务仅 listdir 指纹
    # 检测，车机在写才刷（无写入零开销），消除 8-28 的 drop_caches 风暴。
    try:
        from utils.cache_coherency import start_cache_coherency_task
        start_cache_coherency_task(interval=60)
        app.logger.info("TeslaCam 缓存一致性兜底任务已启动")
    except Exception as e:
        app.logger.warning("缓存一致性任务启动失败（不影响主服务）: %s", e)

    # ── 云归档自动同步恢复（v0.3.1.38 F3-7/F5-1）──
    # 按 cloud.json auto_sync_enabled 恢复 worker，修复 web 重启后自动同步静默丢失
    try:
        from cloud_archive_service import init_auto_sync
        init_auto_sync()
        app.logger.info("云归档自动同步恢复检查完成")
    except Exception as e:
        app.logger.warning(f"云归档自动同步恢复失败: {e}")
    
    # ── 开机通知（M72：升级成功走模板推送版本号，普通启动推固定文案）──
    try:
        import json as _json
        import os as _os
        from weixin_notifier import WeixinNotifier
        from utils.app_helpers import get_push_template
        notifier = WeixinNotifier(bot_name="系统通知")
        marker_path = '/opt/radxa_data/teslausb/data/upgrade_success.json'
        upgraded = None
        try:
            with open(marker_path, 'r', encoding='utf-8') as _f:
                upgraded = _json.load(_f)
        except Exception:
            upgraded = None
        if upgraded and upgraded.get('version'):
            notifier.send_text(get_push_template('upgrade_success').format(version=upgraded['version']))
            try:
                _os.remove(marker_path)
            except OSError:
                pass
            app.logger.info(f"升级成功通知已发送: v{upgraded['version']}")
        else:
            notifier.send_text(get_push_template('boot'))
            app.logger.info("开机通知已发送")
    except Exception as e:
        app.logger.warning(f"开机通知发送失败: {e}")
    
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
