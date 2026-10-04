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

    # ── USB Gadget 健康监控（v0.3.1.55：D1 由被动改主动）──
    # 此前仅当有人打开仪表盘触发 SSE 轮询时才检测 UDC，无人看板时掉线永不重绑；
    # 现改为常驻线程每 60s 主动检测，连续 2 次掉线才重绑（防开机瞬态误判）。
    try:
        import gadget_health
        gadget_health.start_monitor(interval=60)
    except Exception as e:
        app.logger.warning(f"Gadget 健康监控启动失败（不影响主服务）: {e}")

    # ── 遗留 usb-gadget.service 启动自愈（v0.3.1.56 / M75：补齐钩子自举缺口）──
    # 升级后置钩子跑在"当前运行的旧版本进程"里，新钩子滞后一个版本才生效；改为每次
    # 启动幂等检查一次（仍 enabled 则 disable，不加 --now，避免拆掉当前 gadget）。
    # 放 daemon 线程 + 延迟，避免 systemctl（最坏数十秒）阻塞 Web 启动。
    def _legacy_gadget_selfheal():
        try:
            import time as _t
            _t.sleep(15)
            import upgrade_service
            _ok, _msg = upgrade_service.startup_self_heal()
            if _ok is True:
                app.logger.info(f"启动自愈：{_msg}")
            elif _ok is False:
                app.logger.warning(f"启动自愈失败：{_msg}")
            else:
                app.logger.info("启动自愈：无待处理项（遗留 gadget 单元 / USB 守护单元）")
        except Exception as _e:
            app.logger.warning(f"启动自愈异常（不影响主服务）: {_e}")

    threading.Thread(target=_legacy_gadget_selfheal, daemon=True,
                     name="legacy-gadget-selfheal").start()

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
    # M95（v0.3.1.62）：改为**后台线程**发送。原实现同步执行——若发送失败，
    # weixin send_text 会退避重试 10/30/60s（+请求超时），最长阻塞 ~3.5min，
    # 期间 app.run() 永不执行 → 5000 端口不监听。断网场景（如 AP fallback，
    # 本身无上行）下会导致**用户连上 A7Z 的 AP 也打不开配置页**。
    # 放到 daemon 线程后，Web 立即监听，通知自行重试。
    def _send_boot_notification():
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
                _ver = upgraded['version']
                # 模板可由用户在 /system 自定义，占位符写错不能让整条开机通知挂掉
                try:
                    _txt = get_push_template('upgrade_success').format(version=_ver)
                except Exception:
                    _txt = f"系统升级成功 V{_ver}"
                # M78: 仅在发送成功时删除标记。原实现无条件删除——若推送失败
                # （网络/Webhook 抖动），本次升级文案会永久丢失，下次启动退化为
                # 普通"开机启动"。失败时保留标记，下次启动自动重试。
                if notifier.send_text(_txt):
                    try:
                        _os.remove(marker_path)
                    except OSError:
                        pass
                    app.logger.info(f"升级成功通知已发送: v{_ver}")
                else:
                    app.logger.warning(f"升级成功通知发送失败，保留标记待下次启动重试: v{_ver}")
            else:
                notifier.send_text(get_push_template('boot'))
                app.logger.info("开机通知已发送")
        except Exception as e:
            app.logger.warning(f"开机通知发送失败: {e}")

    threading.Thread(target=_send_boot_notification, daemon=True,
                     name="boot-notification").start()

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
