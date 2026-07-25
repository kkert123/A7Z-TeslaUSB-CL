import os, json, time, subprocess, threading
from datetime import datetime
from flask import current_app, Blueprint, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, url_for
from app_state import state

from utils.app_helpers import fmt_bytes, get_queue_counts, get_queue_status, get_wecom_status

import cloud_archive_service
import cloud_rclone_service
import cloud_oauth_service
import sync_service
import video_service
import config


cloud_bp = Blueprint('cloud', __name__, url_prefix='')

# Late imports from app.py (avoid circular imports at module load)
from utils.app_helpers import get_template_context


@cloud_bp.route('/api/sync/status')
def sync_status_api():
    """获取同步状态"""
    try:
        status = sync_service.get_sync_status()
        return jsonify({"success": True, **status})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})



@cloud_bp.route('/api/sync/history')
def sync_history_api():
    """获取同步历史"""
    try:
        history = sync_service.get_sync_history()
        return jsonify({"success": True, "history": history})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})



@cloud_bp.route('/api/sync/trigger', methods=['POST'])
def sync_trigger_api():
    """手动触发同步"""
    try:
        result = sync_service.run_sync()
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})



@cloud_bp.route('/api/sync/config', methods=['GET', 'POST'])
def sync_config_api():
    """获取/更新同步配置"""
    if request.method == 'GET':
        cfg = sync_service.load_config()
        # 隐藏密码
        safe = {k: v for k, v in cfg.items() if not k.startswith('_')}
        return jsonify({"success": True, "config": safe})

    try:
        data = request.get_json() or {}
        current = sync_service.load_config()
        for key in ('enabled', 'nas_ip', 'nas_share', 'nas_user', 'home_ssid',
                     'retention_days', 'delete_after_sync', 'notify_wechat', 'nas_domain'):
            if key in data:
                current[key] = data[key]
        if '_nas_pass' in data and data['_nas_pass']:
            current['_nas_pass'] = data['_nas_pass']
        result = sync_service.save_config(current)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ── Cloud Archive API (Plan A) ──────────────────────────


@cloud_bp.route('/cloud')
def cloud_page():
    """云归档管理页面"""
    return render_template('cloud_archive.html', **get_template_context())



@cloud_bp.route('/api/cloud/status')
def cloud_status_api():
    """获取云归档系统状态。?fast=true 跳过慢速 rclone 调用（about/lsjson），快速返回基本状态"""
    try:
        fast = request.args.get('fast', '').lower() in ('1', 'true', 'yes')
        status = cloud_archive_service.get_cloud_status(fast=fast)
        status['_fast'] = fast  # 告知前端这是快速模式，需要再获取完整数据
        return jsonify({"success": True, **status})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@cloud_bp.route('/api/cloud/oauth/authorize', methods=['POST'])
def cloud_oauth_authorize():
    """启动 OAuth 授权流程，返回 Google 授权 URL"""
    try:
        result = cloud_archive_service.start_oauth_flow()
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/oauth/complete', methods=['POST'])
def cloud_oauth_complete():
    """完成 OAuth 授权：用授权码换取 token"""
    data = request.get_json() or {}
    auth_code = data.get('code', '').strip()
    if not auth_code:
        return jsonify({"success": False, "message": "授权码不能为空"}), 400
    try:
        result = cloud_archive_service.complete_oauth(auth_code)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/oauth/revoke', methods=['POST'])
def cloud_oauth_revoke():
    """撤销 OAuth 授权"""
    try:
        result = cloud_archive_service.revoke_auth()
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/providers/list')
def cloud_providers_list():
    """列出支持的云服务提供商"""
    return jsonify({
        "success": True,
        "providers": cloud_rclone_service.RCLONE_PROVIDERS,
    })



@cloud_bp.route('/api/cloud/provider/configure', methods=['POST'])
def cloud_provider_configure():
    """配置云服务提供商"""
    data = request.get_json(silent=True) or {}
    provider_id = data.get("provider_id", "").strip()
    if not provider_id:
        return jsonify({"success": False, "message": "缺少 provider_id"}), 400
    try:
        ok, msg = cloud_rclone_service.configure_provider(provider_id, data)
        if ok:
            # 同步更新 cloud.json: provider 类型 + remote_name + remote_path
            try:
                current_cfg = cloud_archive_service.load_cloud_config()
                current_cfg["provider"] = provider_id
                # remote_name 默认用 provider_id，除非前端指定
                current_cfg["remote_name"] = data.get("remote_name", provider_id)
                # remote_path：S3/云存储 用 "TeslaUSB/"；NAS 用用户填的路径
                if provider_id in ("smb", "sftp", "ftp", "webdav"):
                    if provider_id == "smb":
                        path = data.get("path", "").strip("/")
                        current_cfg["remote_path"] = (path + "/") if path else ""
                    else:
                        current_cfg["remote_path"] = ""
                else:
                    # S3/OAuth 等云存储：S3兼容端点需带桶名前缀
                    if provider_id in ("s3compat",):
                        bucket = data.get("bucket", "").strip()
                        prefix = f"{bucket}/" if bucket else ""
                        current_cfg["remote_path"] = f"{prefix}TeslaUSB/"
                    else:
                        current_cfg["remote_path"] = "TeslaUSB/"
                cloud_archive_service.save_cloud_config(current_cfg)
                current_app.logger.info(f"cloud.json 同步更新: provider={provider_id}, remote_name={current_cfg['remote_name']}, remote_path={current_cfg['remote_path']}")
            except Exception as sync_e:
                current_app.logger.warning(f"cloud.json 更新失败（不影响 rclone 配置）: {sync_e}")
        return jsonify({"success": ok, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/provider/disconnect', methods=['POST'])
def cloud_provider_disconnect():
    """断开云服务连接"""
    try:
        ok, msg = cloud_rclone_service.delete_rclone_config()
        if ok:
            # 同步清理 cloud.json
            try:
                current_cfg = cloud_archive_service.load_cloud_config()
                current_cfg["provider"] = ""
                current_cfg["remote_name"] = "gdrive"
                cloud_archive_service.save_cloud_config(current_cfg)
            except Exception:
                pass
        return jsonify({"success": ok, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/files')
def cloud_files_api():
    """列出云端文件"""
    path = request.args.get('path', '')
    try:
        result = cloud_archive_service.list_cloud_files(path)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@cloud_bp.route('/api/cloud/upload/event', methods=['POST'])
def cloud_upload_event():
    """上传单个事件到云端"""
    data = request.get_json() or {}
    folder_type = data.get('folder', '').strip()
    event_id = data.get('event_id', '').strip()

    if not folder_type or not event_id:
        return jsonify({"success": False, "message": "缺少 folder 或 event_id 参数"}), 400
    if '..' in event_id or '/' in event_id:
        return jsonify({"success": False, "message": "无效的事件ID"}), 400

    try:
        result = cloud_archive_service.upload_event_to_cloud(folder_type, event_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/sync/progress')
def cloud_sync_progress():
    """获取当前同步进度（供前端轮询）"""
    return jsonify({"success": True, **cloud_archive_service.get_sync_progress()})



@cloud_bp.route('/api/cloud/sync/trigger', methods=['POST'])
def cloud_sync_trigger():
    """手动触发完整云同步"""
    import time
    t0 = time.time()
    try:
        result = cloud_archive_service.sync_teslacam_to_cloud()
        elapsed = round(time.time() - t0, 1)
        cloud_archive_service.add_sync_record({
            "trigger": "manual",
            "success": result.get("success", False),
            "message": result.get("message", ""),
            "files": result.get("stats", {}).get("files_synced", result.get("stats", {}).get("files", 0)),
            "bytes": result.get("stats", {}).get("bytes_transferred", result.get("stats", {}).get("bytes", 0)),
            "duration_sec": elapsed,
        })
        result["duration_sec"] = elapsed
        return jsonify(result)
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        cloud_archive_service.add_sync_record({
            "trigger": "manual", "success": False,
            "message": str(e), "duration_sec": elapsed,
        })
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/config', methods=['GET', 'POST'])
def cloud_config_api():
    """获取/更新云归档配置"""
    if request.method == 'GET':
        cfg = cloud_archive_service.load_cloud_config()
        # 隐藏 secret
        safe = cfg.copy()
        if safe.get('google_client_secret'):
            safe['google_client_secret'] = '***' + safe['google_client_secret'][-4:]
        return jsonify({"success": True, "config": safe})

    try:
        data = request.get_json() or {}
        current = cloud_archive_service.load_cloud_config()
        for key in ('enabled', 'google_client_id', 'google_client_secret',
                     'remote_name', 'remote_path', 'auto_sync_enabled',
                     'sync_retention_days', 'provider', 'sync_folders', 'sync_order',
                     'bandwidth_limit_kb'):
            if key in data:
                current[key] = data[key]
        result = cloud_archive_service.save_cloud_config(current)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@cloud_bp.route('/api/cloud/history')
def cloud_history_api():
    """获取同步历史"""
    limit = request.args.get('limit', 20, type=int)
    stats = cloud_archive_service.get_sync_stats()
    history = cloud_archive_service.get_sync_history(limit)
    return jsonify({"success": True, "stats": stats, "history": history})



@cloud_bp.route('/api/cloud/stats')
def cloud_stats_api():
    """获取同步统计"""
    stats = cloud_archive_service.get_sync_stats()
    return jsonify({"success": True, "stats": stats})



@cloud_bp.route('/api/cloud/queue-stats')
def cloud_queue_stats_api():
    """获取上传队列统计（融合云同步 + 哨兵上传数据）

    修复：仅统计 sync_folders 配置范围内的文件夹，避免夸大数据。
    active 返回同步进度百分比 (0-100)，前端显示为百分比。
    """
    try:
        # 云同步统计
        sync_stats = cloud_archive_service.get_sync_stats()
        sync_progress = cloud_archive_service.get_sync_progress()

        # 获取同步范围内的文件夹（尊重 cloud.json 配置）
        cfg = cloud_archive_service.load_cloud_config()
        sync_folders = cfg.get('sync_folders', ['SentryClips', 'SavedClips', 'ArchivedClips'])
        # 过滤掉不存在的文件夹类型
        valid_folders = [ft for ft in sync_folders if ft in video_service.VIDEO_FOLDERS]
        if not valid_folders:
            valid_folders = ['SentryClips', 'SavedClips']

        # 仅扫描同步范围内的文件夹
        all_evts = []
        for ft in valid_folders:
            evts = video_service._scan_video_folder(ft)
            all_evts.extend(evts)
        uploaded = sum(1 for e in all_evts if e.get('uploaded'))
        total_events = len(all_evts)
        pending_events = max(0, total_events - uploaded)

        # active: 同步进行中则返回进度百分比 (0-100)，否则 0
        sync_running = sync_progress.get('running', False)
        active_progress = 0
        if sync_running:
            raw = sync_progress.get('progress', 0)
            # progress 可能为 -1（失败/取消），限制到 0-100 范围
            active_progress = max(0, min(100, raw))

        return jsonify({"success": True, "stats": {
            "active": active_progress,
            "pending": pending_events,
            "completed": uploaded,
            "failed": sync_stats.get('failed', 0),
            "total": total_events,
            "sync_running": sync_running,
            "sync_message": sync_progress.get('message', ''),
            "sync_progress": active_progress,
            "sync_folders": valid_folders,  # 前端可据此显示同步范围
        }})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@cloud_bp.route('/api/cloud/worker/start', methods=['POST'])
def cloud_worker_start():
    """启动自动同步 Worker"""
    ok, msg = cloud_archive_service.start_sync_worker()
    return jsonify({"success": ok, "message": msg})



@cloud_bp.route('/api/cloud/worker/stop', methods=['POST'])
def cloud_worker_stop():
    """停止自动同步 Worker"""
    ok, msg = cloud_archive_service.stop_sync_worker()
    return jsonify({"success": ok, "message": msg})



@cloud_bp.route('/api/cloud/sync/cancel', methods=['POST'])
def cloud_sync_cancel():
    """取消正在进行的同步"""
    ok, msg = cloud_archive_service.cancel_sync()
    return jsonify({"success": ok, "message": msg})



@cloud_bp.route('/api/cloud/worker/status')
def cloud_worker_status():
    """查询 Worker 运行状态"""
    return jsonify({"success": True, "running": cloud_archive_service.is_worker_running()})


# Mode switch API
