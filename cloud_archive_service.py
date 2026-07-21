#!/usr/bin/env python3
"""
TeslaUSB A7Z — Cloud Archive 服务（Plan A 最小化）
===================================================

协调 OAuth 认证和 rclone 操作，提供统一的云归档接口。

功能：
  - OAuth 授权流程管理
  - rclone 配置同步
  - 手动上传事件到云端
  - 手动同步 TeslaCam 目录
  - 云端文件查询

架构：
  cloud_oauth_service → cloud_rclone_service → cloud_archive_service → Flask routes

与 TeslaUSB-main 的区别：
  - 无自动流水线（archive_worker, archive_watchdog, archive_queue 等）
  - 无 Pi Zero 专项优化（SDIO 竞争、loadavg 保护）
  - 无 file_safety / crypto_utils 硬件绑定
  - 仅保留核心 OAuth + rclone + 手动触发

作者：TeslaUSB A7Z 项目
版本：1.0.0 (Plan A)
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional, Tuple

from cloud_oauth_service import (
    get_auth_url,
    exchange_code,
    get_oauth_status,
    get_stored_token,
    delete_token,
    refresh_token,
)
from cloud_rclone_service import (
    check_rclone_installed,
    configure_rclone,
    list_remote_files,
    upload_file,
    upload_directory,
    get_remote_usage,
    get_configured_provider,
    _fmt_size,
)

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────

CLOUD_CONFIG_FILE = "/opt/radxa_data/teslausb/config/cloud.json"
SOURCE_DIR = "/mnt/teslacam"

# ── Sync Worker ──────────────────────────────────────────

_sync_worker_running = False
_sync_worker_thread = None
_sync_interval_sec = 3600  # 1 小时
_sync_retry_count = 0
SYNC_RETRY_MAX = 5
SYNC_RETRY_DELAY = 60  # 重试间隔秒数

# 实时同步状态（供前端轮询）
_sync_state = {
    "running": False,
    "progress": 0,       # 0-100, -1=失败
    "message": "",
    "started_at": None,  # Unix timestamp
    "cancelled": False,  # 是否被用户取消
}
_sync_lock = threading.Lock()

def cancel_sync():
    """取消正在进行的同步"""
    from cloud_rclone_service import request_sync_cancel
    if _sync_state.get("running"):
        request_sync_cancel()
        _sync_state["cancelled"] = True
        return True, "取消请求已发送，正在终止同步..."
    return False, "当前没有正在进行的同步"

def _check_cloud_ready():
    """检查云服务是否就绪（OAuth provider 检查授权，NAS/S3 provider 检查 rclone.conf）。
    
    Returns:
        (ready: bool, error_msg: str or None)
    """
    from cloud_rclone_service import RCLONE_CONFIG_FILE
    cfg = load_cloud_config()
    provider = cfg.get("provider", "")
    needs_oauth = provider in ("gdrive", "onedrive", "dropbox")
    
    if needs_oauth:
        auth_info = get_oauth_status()
        if not auth_info.get("authorized"):
            return False, "未授权，请先完成 OAuth 授权"
    else:
        if not os.path.exists(RCLONE_CONFIG_FILE):
            return False, "未配置云服务，请先选择并保存云服务商"
    
    installed, _ = check_rclone_installed()
    if not installed:
        return False, "rclone 未安装"
    
    return True, None


def start_sync_worker():
    """启动后台同步 Worker"""
    global _sync_worker_running, _sync_worker_thread
    if _sync_worker_running:
        return False, "Worker 已在运行"
    _sync_worker_running = True
    _sync_worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="cloud-sync-worker")
    _sync_worker_thread.start()
    logger.info("Cloud sync worker started")
    return True, "自动同步已启动"


def stop_sync_worker():
    """停止后台同步 Worker"""
    global _sync_worker_running
    _sync_worker_running = False
    logger.info("Cloud sync worker stopped")
    return True, "自动同步已停止"


def is_worker_running() -> bool:
    return _sync_worker_running


def _worker_loop():
    global _sync_retry_count
    logger.info("Cloud sync worker loop started (interval: %ds)", _sync_interval_sec)
    while _sync_worker_running:
        try:
            # 检查配置
            cfg = load_cloud_config()
            if not cfg.get("auto_sync_enabled"):
                time.sleep(_sync_interval_sec)
                continue

            # 检查授权 — OAuth provider 需要授权，NAS provider 只需 rclone.conf
            from cloud_rclone_service import RCLONE_CONFIG_FILE
            auth_info = get_oauth_status()
            remote_configured = os.path.exists(RCLONE_CONFIG_FILE)
            provider = cfg.get("provider", "")
            # OAuth 类型 (drive/onedrive/dropbox) 需要授权；NAS/S3 类型只需要 rclone.conf
            needs_oauth = provider in ("gdrive", "onedrive", "dropbox")
            if needs_oauth and not auth_info.get("authorized"):
                time.sleep(_sync_interval_sec)
                continue
            if not needs_oauth and not remote_configured:
                time.sleep(_sync_interval_sec)
                continue

            installed, _ = check_rclone_installed()
            if not installed:
                time.sleep(_sync_interval_sec)
                continue

            # 检查源目录
            if not os.path.exists(SOURCE_DIR):
                time.sleep(_sync_interval_sec)
                continue

            # 按配置的文件夹顺序同步（sync_order 定义优先级）
            folders = cfg.get("sync_folders", ["SentryClips", "SavedClips", "ArchivedClips"])
            sync_order = cfg.get("sync_order", ["SentryClips", "SavedClips", "ArchivedClips"])
            # 按 sync_order 排序：order 中越靠前的优先级越高（先上传）
            if sync_order and len(sync_order) > 0:
                rank = {f: i for i, f in enumerate(sync_order)}
                folders = sorted(folders, key=lambda f: rank.get(f, 999))
            remote = cfg.get("remote_name", "gdrive")
            remote_path = cfg.get("remote_path", "TeslaUSB/")
            bwlimit = cfg.get("bandwidth_limit_kb", 0)  # 0 = 无限制

            t0 = time.time()
            total_files = 0
            total_bytes = 0
            all_ok = True

            for folder in folders:
                if not _sync_worker_running:
                    break
                src = os.path.join(SOURCE_DIR, "TeslaCam", folder)
                if not os.path.exists(src):
                    continue
                # 统一路径：TeslaUSB/TeslaCam/{folder}/ (与手动同步一致)
                dst = remote_path.rstrip('/') + '/TeslaCam/' + folder
                ok, msg, stats = upload_directory(src, remote, dst, bwlimit=bwlimit)
                if ok:
                    total_files += stats.get("files", 0)
                    total_bytes += stats.get("bytes", 0)
                else:
                    all_ok = False
                # 每次上传间隔 10 秒，降低连续负载
                if _sync_worker_running:
                    time.sleep(10)

            elapsed = round(time.time() - t0, 1)
            _sync_retry_count = 0 if all_ok else _sync_retry_count + 1
            add_sync_record({
                "trigger": "auto",
                "success": all_ok,
                "message": f"自动同步 {len(folders)} 个文件夹",
                "files": total_files,
                "bytes": total_bytes,
                "duration_sec": elapsed,
            })

            if not all_ok and _sync_retry_count < SYNC_RETRY_MAX:
                logger.warning(f"同步部分失败，{SYNC_RETRY_DELAY}s 后重试 ({_sync_retry_count}/{SYNC_RETRY_MAX})")
                time.sleep(SYNC_RETRY_DELAY)
            else:
                time.sleep(_sync_interval_sec)

        except Exception as e:
            logger.error(f"Sync worker error: {e}")
            time.sleep(300)  # 出错等 5 分钟

# 默认配置
DEFAULT_CLOUD_CONFIG = {
    "enabled": False,
    "provider": "google",
    "google_client_id": "",
    "google_client_secret": "",
    "remote_name": "gdrive",
    "remote_path": "TeslaUSB/",
    "auto_sync_enabled": False,
    "sync_retention_days": 7,
    "sync_folders": ["SentryClips", "SavedClips", "ArchivedClips"],
    "sync_order": ["SentryClips", "SavedClips", "ArchivedClips"],
    "bandwidth_limit_kb": 0,
}


# ── 同步历史 ────────────────────────────────────────────

SYNC_HISTORY_FILE = "/opt/radxa_data/teslausb/data/cloud_sync_history.json"

def get_sync_history(limit: int = 20) -> list:
    """获取同步历史"""
    try:
        if os.path.exists(SYNC_HISTORY_FILE):
            with open(SYNC_HISTORY_FILE, 'r') as f:
                history = json.load(f)
            return history[:limit] if isinstance(history, list) else []
    except:
        pass
    return []


def add_sync_record(record: dict):
    """添加同步记录"""
    try:
        os.makedirs(os.path.dirname(SYNC_HISTORY_FILE), exist_ok=True)
        history = get_sync_history(500)
        record.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        history.insert(0, record)
        with open(SYNC_HISTORY_FILE, 'w') as f:
            json.dump(history[:200], f, indent=2)
    except Exception as e:
        logger.error(f"保存同步历史失败: {e}")


def get_sync_stats() -> dict:
    """获取同步统计摘要"""
    history = get_sync_history(100)
    total_synced = 0
    total_files = 0
    total_bytes = 0
    failed = 0
    for h in history:
        total_synced += 1
        total_files += h.get("files", 0)
        total_bytes += h.get("bytes", 0)
        if not h.get("success", True):
            failed += 1
    
    # 获取上传队列中待处理的任务数
    pending = 0
    try:
        from upload_scheduler import get_upload_scheduler
        scheduler = get_upload_scheduler()
        if scheduler:
            tasks = scheduler.get_all_tasks()
            pending = len([t for t in tasks if t.status.value in ('pending_confirm', 'confirmed', 'uploading')])
    except Exception:
        pass
    
    return {
        "total_synced": total_synced,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_bytes_fmt": _fmt_size(total_bytes),
        "failed": failed,
        "pending": pending,
    }


# ── 配置管理 ────────────────────────────────────────────


def load_cloud_config() -> dict:
    """加载云端配置"""
    try:
        if os.path.exists(CLOUD_CONFIG_FILE):
            with open(CLOUD_CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
                merged = DEFAULT_CLOUD_CONFIG.copy()
                merged.update(cfg)
                return merged
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_CLOUD_CONFIG.copy()


def save_cloud_config(cfg: dict) -> dict:
    """保存云端配置"""
    try:
        os.makedirs(os.path.dirname(CLOUD_CONFIG_FILE), exist_ok=True)
        with open(CLOUD_CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
        return {"success": True, "message": "配置已保存"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ── 授权流程 ────────────────────────────────────────────


def start_oauth_flow() -> dict:
    """
    启动 OAuth 授权流程，返回授权 URL。

    用户需要：
    1. 在浏览器打开返回的 auth_url
    2. 授权 A7Z TeslaUSB 应用访问 Google Drive
    3. 复制授权码
    4. 在 Web UI 输入授权码调用 complete_oauth()

    Returns:
        {"success": bool, "auth_url": str, "message": str}
    """
    cfg = load_cloud_config()
    client_id = cfg.get('google_client_id', '').strip()

    if not client_id:
        return {
            "success": False,
            "auth_url": "",
            "message": "未配置 Google Client ID，请先在云归档设置中配置",
        }

    if not cfg.get('google_client_secret', '').strip():
        return {
            "success": False,
            "auth_url": "",
            "message": "未配置 Google Client Secret",
        }

    auth_url = get_auth_url(client_id)
    return {
        "success": True,
        "auth_url": auth_url,
        "message": "请在浏览器中打开授权链接",
    }


def complete_oauth(auth_code: str) -> dict:
    """
    完成 OAuth 授权：用授权码换取 token，配置 rclone。

    Args:
        auth_code: 用户从 Google 授权页面获取的授权码

    Returns:
        {"success": bool, "message": str, "provider": str}
    """
    cfg = load_cloud_config()
    client_id = cfg.get('google_client_id', '').strip()
    client_secret = cfg.get('google_client_secret', '').strip()

    if not client_id or not client_secret:
        return {"success": False, "message": "Client ID/Secret 未配置"}

    if not auth_code or not auth_code.strip():
        return {"success": False, "message": "授权码为空"}

    # 换取 token
    success, message, token_data = exchange_code(client_id, client_secret, auth_code.strip())
    if not success:
        return {"success": False, "message": message}

    # 配置 rclone
    remote_name = cfg.get('remote_name', 'gdrive')
    rclone_ok, rclone_msg = configure_rclone(
        client_id=client_id,
        client_secret=client_secret,
        access_token=token_data['access_token'],
        refresh_token=token_data['refresh_token'],
        expires_at=token_data['expires_at'],
        remote_name=remote_name,
    )

    if not rclone_ok:
        return {
            "success": False,
            "message": f"Token 已获取，但 rclone 配置失败: {rclone_msg}",
        }

    return {
        "success": True,
        "message": "授权成功！rclone 已配置",
        "provider": cfg.get('provider', 'google'),
    }


def revoke_auth() -> dict:
    """撤销 OAuth 授权，删除 token 和 rclone 配置"""
    cfg = load_cloud_config()
    provider = cfg.get('provider', 'google')

    delete_token(provider)

    # 删除 rclone 配置
    from cloud_rclone_service import RCLONE_CONFIG_FILE
    try:
        if os.path.exists(RCLONE_CONFIG_FILE):
            os.remove(RCLONE_CONFIG_FILE)
    except OSError:
        pass

    return {"success": True, "message": "授权已撤销"}


# ── 系统状态 ────────────────────────────────────────────


def get_cloud_status(fast: bool = False) -> dict:
    """
    获取云归档系统完整状态（供 Web UI Dashboard 使用）。

    Args:
        fast: True=仅快速检查（跳过 rclone about/lsjson），False=完整检查
    """
    status = {
        "rclone_installed": False,
        "rclone_version": "",
        "oauth_authorized": False,
        "oauth_expires_sec": 0,
        "remote_configured": False,
        "configured_provider": None,
        "remote_usage": {},
        "last_sync": None,
        "files_in_cloud": 0,
        "enabled": False,
    }

    cfg = load_cloud_config()
    status["enabled"] = cfg.get("enabled", False)

    # rclone 检查
    installed, version = check_rclone_installed()
    status["rclone_installed"] = installed
    status["rclone_version"] = version

    # OAuth 检查
    auth_info = get_oauth_status()
    status["oauth_authorized"] = auth_info["authorized"]
    status["oauth_expires_sec"] = auth_info["expires_in_sec"]

    # 远程连接检查（OAuth 或 NAS 配置均可）
    from cloud_rclone_service import RCLONE_CONFIG_FILE
    if installed and os.path.exists(RCLONE_CONFIG_FILE):
        status["remote_configured"] = True
        remote_name = cfg.get('remote_name', 'gdrive')

        # 检测当前配置的 provider
        prov = get_configured_provider()
        status["configured_provider"] = prov
        
        # 自动同步 cloud.json：将 rclone.conf 中的 provider 信息和 remote_name 写入 cloud.json
        # （修复旧配置中 provider="google" 应为 "gdrive"、remote_name 不匹配等问题）
        if prov.get("provider_id"):
            needs_update = False
            if cfg.get("provider") != prov["provider_id"]:
                cfg["provider"] = prov["provider_id"]
                needs_update = True
            if cfg.get("remote_name") != prov.get("remote_name"):
                cfg["remote_name"] = prov["remote_name"]
                needs_update = True
            if needs_update:
                try:
                    save_cloud_config(cfg)
                    logger.info(f"cloud.json auto-synced: provider={cfg['provider']}, remote_name={cfg['remote_name']}")
                except Exception as e:
                    logger.warning(f"cloud.json auto-sync failed: {e}")
        
        # OAuth 状态（仅对 OAuth provider 有意义）
        status["oauth_authorized"] = auth_info["authorized"]
        status["oauth_expires_sec"] = auth_info["expires_in_sec"]

        # 获取使用情况（连接可用时才有效）— fast 模式跳过慢速 rclone 调用
        if not fast:
            try:
                ok, usage = get_remote_usage(remote_name)
                if ok:
                    status["remote_usage"] = usage
            except:
                pass

        # 统计云端文件数 — fast 模式跳过慢速 rclone 调用
        if not fast:
            try:
                ok, files = list_remote_files(remote_name, cfg.get('remote_path', 'TeslaUSB/'))
                if ok:
                    status["files_in_cloud"] = len(files)
            except:
                pass

    # 最后同步时间
    sync_log = "/opt/radxa_data/teslausb/data/cloud_sync_log.json"
    try:
        if os.path.exists(sync_log):
            with open(sync_log, 'r') as f:
                log = json.load(f)
                entries = log.get("entries", [])
                if entries:
                    status["last_sync"] = entries[-1].get("time", None)
    except (OSError, json.JSONDecodeError):
        pass

    return status


# ── 上传操作 ────────────────────────────────────────────


def upload_event_to_cloud(folder_type: str, event_id: str) -> dict:
    """
    手动上传单个事件到云端。

    使用 rclone copy 上传整个事件文件夹。

    Args:
        folder_type: 视频文件夹类型（SentryClips/SavedClips/RecentClips）
        event_id: 事件 ID

    Returns:
        {"success": bool, "message": str, "stats": dict}
    """
    ready, err = _check_cloud_ready()
    if not ready:
        return {"success": False, "message": err}

    # 确定源路径
    from video_service import VIDEO_FOLDERS
    if folder_type not in VIDEO_FOLDERS:
        return {"success": False, "message": f"无效的文件夹类型: {folder_type}"}

    folder_path = VIDEO_FOLDERS[folder_type]['path']

    if folder_type == 'RecentClips':
        # 平铺结构：创建临时目录，收集匹配文件后上传
        import tempfile
        import shutil

        tmp_dir = tempfile.mkdtemp(prefix='cloud_upload_')
        try:
            count = 0
            for fname in os.listdir(folder_path):
                if fname.startswith(event_id) and fname.lower().endswith(('.mp4',)):
                    src = os.path.join(folder_path, fname)
                    dst = os.path.join(tmp_dir, fname)
                    shutil.copy2(src, dst)
                    count += 1

            if count == 0:
                shutil.rmtree(tmp_dir)
                return {"success": False, "message": "未找到视频文件"}

            cfg = load_cloud_config()
            remote = cfg.get('remote_name', 'gdrive')
            remote_path = f"{cfg.get('remote_path', 'TeslaUSB/')}{folder_type}/{event_id}"

            ok, msg, stats = upload_directory(tmp_dir, remote, remote_path)

            # 记录同步日志
            _log_sync("upload", event_id, ok, msg, stats)

            return {
                "success": ok,
                "message": msg,
                "stats": stats,
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        # 事件文件夹结构
        event_path = os.path.join(folder_path, event_id)
        if not os.path.isdir(event_path):
            return {"success": False, "message": "事件不存在"}

        cfg = load_cloud_config()
        remote = cfg.get('remote_name', 'gdrive')
        remote_path = f"{cfg.get('remote_path', 'TeslaUSB/')}{folder_type}/{event_id}"

        ok, msg, stats = upload_directory(event_path, remote, remote_path)

        _log_sync("upload", event_id, ok, msg, stats)

        return {
            "success": ok,
            "message": msg,
            "stats": stats,
        }


def get_sync_progress() -> dict:
    """返回当前同步进度（供前端轮询）。
    
    Returns:
        {"running": bool, "progress": int, "message": str, "started_at": float|None}
        progress: 0-100（钳制在此范围），-1=失败已完成
    """
    state = dict(_sync_state)
    # 钳制进度值在有效范围
    p = state.get('progress', 0)
    if p > 100:
        state['progress'] = 100
    elif p < 0 and p != -1:
        state['progress'] = 0
    return state


def sync_teslacam_to_cloud() -> dict:
    """
    手动同步 TeslaCam 目录到云端。

    使用 rclone sync（增量同步），只上传新文件。
    使用锁防止并发同步。

    Returns:
        {"success": bool, "message": str, "stats": dict}
    """
    global _sync_state
    
    ready, err = _check_cloud_ready()
    if not ready:
        return {"success": False, "message": err}

    if not os.path.exists(SOURCE_DIR):
        return {"success": False, "message": f"源目录不存在: {SOURCE_DIR}"}

    # 互斥锁：防止并发同步
    if not _sync_lock.acquire(blocking=False):
        return {"success": False, "message": "同步正在进行中，请稍后再试"}
    
    try:
        # 重置取消标志
        from cloud_rclone_service import reset_sync_cancel, is_sync_cancelled
        reset_sync_cancel()
        
        cfg = load_cloud_config()
        remote = cfg.get('remote_name', 'gdrive')
        remote_path = cfg.get('remote_path', 'TeslaUSB/')
        bwlimit = cfg.get('bandwidth_limit_kb', 0)

        # 按优先级排序文件夹
        folders = cfg.get("sync_folders", ["SentryClips", "SavedClips", "ArchivedClips"])
        sync_order = cfg.get("sync_order", ["SentryClips", "SavedClips", "ArchivedClips"])
        if sync_order and len(sync_order) > 0:
            rank = {f: i for i, f in enumerate(sync_order)}
            folders = sorted(folders, key=lambda f: rank.get(f, 999))

        _sync_state["running"] = True
        _sync_state["progress"] = 0
        _sync_state["message"] = f"准备上传 {len(folders)} 个文件夹..."
        _sync_state["started_at"] = time.time()
        _sync_state["cancelled"] = False

        total_files = 0
        total_bytes = 0
        all_ok = True
        folder_count = 0
        
        for idx, folder in enumerate(folders):
            if is_sync_cancelled():
                all_ok = False
                break
            
            folder_count += 1
            src = os.path.join(SOURCE_DIR, "TeslaCam", folder)
            dst = remote_path.rstrip('/') + '/TeslaCam/' + folder
            
            if not os.path.exists(src):
                continue

            # 每文件夹的进度偏移：已完成的文件夹占 (idx/len) 的权重
            base_pct = int((idx / len(folders)) * 100) if folders else 0
            folder_pct_range = int(100 / len(folders)) if folders else 100

            def _make_folder_callback(folder_name, base, pct_range):
                def _cb(pct):
                    if pct < 0:
                        _sync_state["progress"] = pct
                        _sync_state["message"] = f"[{folder_name}] 同步失败"
                    elif pct >= 100:
                        scaled = min(base + pct_range, 99)
                        _sync_state["progress"] = scaled
                        _sync_state["message"] = f"[{folder_name}] 完成 ✅"
                    else:
                        # 避免小百分比舍入为 0：至少移动 1%
                        step = int(pct * pct_range / 100)
                        if pct > 0 and step == 0:
                            step = 1
                        scaled = min(base + step, 99)
                        _sync_state["progress"] = scaled
                        _sync_state["message"] = f"[{folder_name}] {pct}%"
                return _cb

            _sync_state["message"] = f"[{folder}] 正在上传 ({folder_count}/{len(folders)})..."
            
            ok, msg, stats = upload_directory(src, remote, dst,
                                              bwlimit=bwlimit,
                                              progress_callback=_make_folder_callback(
                                                  folder, base_pct, folder_pct_range))
            if ok:
                total_files += stats.get("files_synced", stats.get("files", 0))
                total_bytes += stats.get("bytes_transferred", stats.get("bytes", 0))
            else:
                all_ok = False
                if is_sync_cancelled():
                    break

        if is_sync_cancelled():
            _sync_state["message"] = "同步已取消"
            _sync_state["progress"] = -1
            _log_sync("sync_all", "", False, "同步已取消", {"files": total_files, "bytes": total_bytes})
            return {"success": False, "message": "同步已取消", "stats": {"files": total_files, "bytes": total_bytes}}
        
        _sync_state["progress"] = 100
        _sync_state["message"] = f"同步完成 ({total_files} 文件)"
        
        duration = round(time.time() - _sync_state["started_at"], 1)
        summary_stats = {
            "duration_sec": duration,
            "files_synced": total_files,
            "bytes_transferred": total_bytes,
            "bytes_fmt": _fmt_size(total_bytes),
        }
        _log_sync("sync_all", "", all_ok, f"同步 {len(folders)} 个文件夹", summary_stats)
        return {
            "success": all_ok,
            "message": f"同步完成 ({duration:.0f}s, {total_files} 文件, {folder_count} 文件夹)",
            "stats": summary_stats,
        }
    finally:
        _sync_state["running"] = False
        _sync_lock.release()


# ── 云端浏览 ────────────────────────────────────────────


def list_cloud_files(path: str = "") -> dict:
    """
    列出云端的文件和目录。

    Args:
        path: 云端子路径

    Returns:
        {"success": bool, "files": list, "path": str}
    """
    ready, err = _check_cloud_ready()
    if not ready:
        return {"success": False, "files": [], "message": err}

    cfg = load_cloud_config()
    remote = cfg.get('remote_name', 'gdrive')
    remote_path = f"{cfg.get('remote_path', 'TeslaUSB/')}{path}"
    # 保持末尾斜杠：rclone lsjson 需要目录路径末尾有 / 才能正确列出
    if not remote_path.endswith('/'):
        remote_path += '/'

    ok, files = list_remote_files(remote, remote_path)
    return {
        "success": ok,
        "files": files,
        "path": path,
        "remote_path": remote_path,
    }


# ── 内部工具 ────────────────────────────────────────────


def _log_sync(action: str, event_id: str, success: bool, message: str, stats: dict = None):
    """记录同步日志到本地 JSON 文件"""
    log_file = "/opt/radxa_data/teslausb/data/cloud_sync_log.json"
    try:
        log = {"entries": []}
        if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
            with open(log_file, 'r') as f:
                log = json.load(f)

        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "event_id": event_id,
            "success": success,
            "message": message,
            "stats": stats or {},
        }
        log.setdefault("entries", []).append(entry)
        # 保留最近 100 条
        log["entries"] = log["entries"][-100:]

        with open(log_file, 'w') as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        logger.warning(f"写入同步日志失败: {e}")
