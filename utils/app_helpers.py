"""
utils/app_helpers.py — 应用帮助函数集合

从 app.py 提取出的所有非路由帮助函数、缓存管理、SSE 广播器等。
"""
import os, json, subprocess, threading, time, struct, io, queue, select, logging, traceback
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response
from app_state import state
import video_service
import sync_service
import staging_service
import cloud_archive_service
import cloud_rclone_service
from utils.hardware_stats import (get_cpu_percent, get_cpu_temperature, get_gpu_temperature_fields,
    get_memory_info, get_all_disks, get_nvme_total_disk, _save_disk_cache, get_network_bytes,
    _update_disk_io, get_disk_io, _get_monthly_traffic, get_gpu_npu_status, get_fan_status,
    _detect_thermal_zones, _update_temp_histories)
from utils.system_info import get_wifi_info, get_system_uptime, get_service_status, get_ip_info
from utils.nvme_monitor import (_refresh_nvme_cache, _get_nvme_cache, get_nvme_temperature,
    _update_nvme_temp_history, get_nvme_temperature_fields, get_nvme_health, fmt_power_on_hours)
from utils.thumbnail_utils import _generate_thumbnail
from utils.thumbnail_decision import should_regenerate, find_source_files, get_thumbnail_health

_stats_logger = logging.getLogger("StatsBroadcaster")

# ═══════════════════════════════════════════════════════════════

PARTITIONS = {
    "cam": "/mnt/teslacam",
    "music": "/mnt/music",
    "boombox": "/mnt/boombox",
    "lightshow": "/mnt/lightshow",
}

# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────

def get_disk_usage(path):
    """获取磁盘使用情况"""
    try:
        if os.path.exists(path):
            import shutil
            usage = shutil.disk_usage(path)
            return {
                'total': usage.total,
                'used': usage.used,
                'free': usage.free,
                'percent': round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0
            }
    except:
        pass
    return None

# ─────────────────────────────────────────────
# 视频管理基础设施（Task 3.2.1）
# ─────────────────────────────────────────────

THUMBNAIL_DIR = '/opt/radxa_data/teslausb/static/thumbnails'

def _format_size(size_bytes):
    """格式化字节大小（供视频模板使用）"""
    if not size_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"

def _to_local_time(ts_str):
    """格式化时间戳字符串用于显示（Tesla 文件名已是本地时间）
    
    输入: '2026-05-17 22-30-37' → 输出: '2026-05-17 22:30:37'
    如果解析失败，返回原始字符串。
    """
    try:
        parts = ts_str.split(' ')
        if len(parts) == 2:
            normalized = f"{parts[0]} {parts[1].replace('-', ':')}"
        else:
            normalized = ts_str.replace('-', ':')
        dt = datetime.strptime(normalized, '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return ts_str

# ═══ 注意：_scan_video_folder 已迁移到 video_service._scan_video_folder ═══
# app.py 中的旧版本（以下）存在 RecentClips 文件名解析 bug（split 误匹配），
# 且缺少 _mark_uploaded_events 调用。所有调用方已迁移到 video_service。
# 保留此委托确保向后兼容。
def _scan_video_folder(folder_type):
    """[DEPRECATED] 委托到 video_service._scan_video_folder"""
    import video_service as _vs
    return _vs._scan_video_folder(folder_type)

def get_folders():
    """返回视频文件夹定义（供视频页面下拉选择器使用）—— 委托给 video_service"""
    return video_service.VIDEO_FOLDERS

def get_video_stats(folder_type):
    """获取指定文件夹类型的统计信息 —— 委托给 video_service"""
    return video_service.get_video_stats(folder_type)

WECOM_CONFIG_PATH = '/opt/radxa_data/teslausb/config/sentry.json'

# 企业微信机器人定义（顺序即 UI 展示顺序）
WECOM_BOTS = [
    {'bot_key': 'sentry', 'name': '哨兵事件', 'config_key': 'wecom_sentry_webhook_key',
     'desc': '哨兵模式触发时推送事件通知（含缩略图+位置）'},
    {'bot_key': 'status', 'name': '系统通知', 'config_key': 'wecom_status_webhook_key',
     'desc': '系统状态变更、开机通知、异常告警'},
    {'bot_key': 'boot', 'name': '启动通知', 'config_key': 'wecom_boot_webhook_key',
     'desc': '设备启动时推送上线通知'},
]

def get_wecom_status():
    """获取企业微信机器人状态 — 从配置文件读取

    enabled 机制：禁用时把 key 从主字段 {base} 挪到 {base}_disabled，
    这样所有 cfg.get(base) 的推送点自动失效（禁用真正生效），且可逆。
    """
    status = {
        'configured': False,
        'bots': [],
        'last_push': None,
        'error': None
    }
    try:
        config = {}
        if os.path.exists(WECOM_CONFIG_PATH):
            with open(WECOM_CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)

        any_configured = False
        for spec in WECOM_BOTS:
            base = spec['config_key']
            active_key = config.get(base) or ''
            disabled_key = config.get(base + '_disabled') or ''
            key = active_key or disabled_key
            configured = bool(key)
            if configured:
                any_configured = True
            status['bots'].append({
                'bot_key': spec['bot_key'],
                'name': spec['name'],
                'config_key': base,
                'desc': spec['desc'],
                'configured': configured,
                'enabled': bool(active_key),       # 主字段有值 = 启用
                'key_suffix': key[-6:] if key else '',
            })

        status['configured'] = any_configured

        log_path = '/opt/radxa_data/teslausb/logs/wecom_push.log'
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r') as f:
                    lines = f.readlines()
                    if lines:
                        last_line = lines[-1].strip()
                        if last_line.startswith('[') and ']' in last_line:
                            status['last_push'] = last_line.split(']')[0][1:]
            except Exception:
                pass
    except Exception as e:
        status['error'] = str(e)

    return status

def get_queue_status():
    """获取上传队列状态"""
    try:
        from upload_scheduler import get_upload_scheduler
        scheduler = get_upload_scheduler()
        if not scheduler:
            return {"tasks": [], "active": [], "pending": [], "completed": [], "failed": []}
        
        raw_tasks = scheduler.get_all_tasks()
        tasks = [t.to_dict() for t in raw_tasks]
        
        # 按状态分类，与旧格式兼容
        active = [t for t in tasks if t.get('status') == 'uploading']
        pending = [t for t in tasks if t.get('status') in ('pending_confirm', 'confirmed')]
        completed = [t for t in tasks if t.get('status') == 'done']
        failed = [t for t in tasks if t.get('status') == 'failed']
        
        return {
            "tasks": tasks,
            "active": active,
            "pending": pending,
            "completed": completed,
            "failed": failed,
        }
    except Exception as e:
        app.logger.warning(f"获取上传队列状态失败: {e}")
        return {"tasks": [], "active": [], "pending": [], "completed": [], "failed": [], "error": str(e)}

def get_queue_counts():
    """获取上传队列数量统计"""
    queue = get_queue_status()
    return {
        'active_count': len(queue.get('active', [])),
        'pending_count': len(queue.get('pending', [])),
        'completed_count': len(queue.get('completed', [])),
        'failed_count': len(queue.get('failed', []))
    }

def fmt_bytes(b):
    """格式化字节数（十进制 GB/MB，与 df -H 一致）"""
    if not b:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1000.0:
            return f"{b:.2f} {unit}"
        b /= 1000.0
    return f"{b:.2f} PB"

def _get_teslacam_health():
    """获取 TeslaCam 文件系统健康状态（带 60s 缓存）
    
    检测 SentryClips/SavedClips/RecentClips 目录是否可访问，
    发现 exFAT 元数据损坏时返回损坏目录列表。
    """
    now = time.time()
    if state.teslacam_health_cache and (now - state.teslacam_health_cache_time) < 60:
        return state.teslacam_health_cache
    try:
        import video_service as _vs
        state.teslacam_health_cache = _vs.check_teslacam_health()
    except Exception as e:
        state.teslacam_health_cache = {'healthy': True, 'corrupted_dirs': [], 'accessible_dirs': [],
                                   'missing_dirs': [], 'details': [], 'check_error': str(e)}
    state.teslacam_health_cache_time = now
    return state.teslacam_health_cache

def _get_location_status():
    """获取车辆位置状态（带 30s 缓存，避免频繁调 TeslaMate API）
    
    自动从 config/sentry.json 加载配置，确保使用 WiFi 页面配置的参数。
    
    Returns:
        dict: {state: 'home'|'away'|'unknown', source: 'teslamate'|'wifi'|'unknown',
               raw_location: str, wifi_connected: str, confidence: float}
    """
    now = time.time()
    if state.location_status_cache and (now - state.location_status_cache_time) < 30:
        return state.location_status_cache
    state.location_status_cache_time = now
    try:
        # 从 config/sentry.json 加载配置并初始化 detector（确保使用 WiFi 页面配置）
        import json as _json
        from location_detector import init_location_detector, get_location_detector
        
        config_path = '/opt/radxa_data/teslausb/config/sentry.json'
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = _json.load(f)
            init_location_detector({
                'teslamate_url': cfg.get('teslamate_url', 'http://100.111.252.121:7777'),
                'home_location': cfg.get('home_location', '家'),
                'home_wifi_ssids': cfg.get('home_wifi_ssids', []),
                'hotspot_ssids': cfg.get('hotspot_ssids', []),
                'wifi_interface': cfg.get('wifi_interface', 'wlan0'),
                'teslamate_password': cfg.get('teslamate_password'),
            })
        except Exception as e:
            pass  # 配置加载失败则使用默认配置
        
        detector = get_location_detector()
        info = detector.check_location()
        state.location_status_cache = {
            'state': info.state.value if info else 'unknown',
            'source': info.location_source if info else 'unknown',
            'raw_location': info.raw_location if info else '',
            'wifi_connected': info.wifi_connected if info else '',
            'confidence': info.confidence if info else 0,
        }
    except Exception as e:
        state.location_status_cache = {
            'state': 'unknown', 'source': 'unknown',
            'raw_location': '', 'wifi_connected': '',
            'confidence': 0, 'error': str(e)
        }
    return state.location_status_cache

def get_template_context():
    """获取所有模板需要的公共变量"""
    wifi_info = get_wifi_info()
    queue = get_queue_status()
    counts = get_queue_counts()
    disks = get_all_disks()
    
    return {
        'service': get_service_status(),
        'sys_stats': get_system_stats(),
        'wifi': wifi_info,
        'current': wifi_info,
        'ip_info': get_ip_info(),
        'disk_total': get_disk_usage('/'),
        'disk': disks,  # 新增：所有磁盘分区
        'system_uptime': get_system_uptime(),
        'now': datetime.now().strftime("%H:%M:%S"),
        'fmt_bytes': fmt_bytes,
        'folders': get_folders(),
        'wecom': get_wecom_status(),
        'queue': queue,
        'active_count': counts['active_count'],
        'pending_count': counts['pending_count'],
        'completed_count': counts['completed_count'],
        'failed_count': counts['failed_count'],
        'sentry_events': get_cached_sentry_events(),
        'preview_status': _get_preview_status(),
        'teslacam_health': _get_teslacam_health(),
        'location_status': _get_location_status(),
    }
# 哨兵/预览/健康/视频 缓存已迁移到 app_state.py

def _get_cached_video_scan(folder_type: str):
    """获取缓存的视频扫描结果，过期或不存在返回 None"""
    with state.videos_scan_cache_lock:
        entry = state.videos_scan_cache.get(folder_type)
        if entry and (time.time() - entry['ts']) < state.VIDEOS_SCAN_CACHE_TTL:
            return entry['events'], entry['stats']
    return None, None

def _set_cached_video_scan(folder_type: str, events: list, stats: dict):
    """写入视频扫描缓存"""
    with state.videos_scan_cache_lock:
        state.videos_scan_cache[folder_type] = {
            'events': events, 'stats': stats, 'ts': time.time()
        }

def _invalidate_video_cache(folder_type: str = None):
    """使视频扫描缓存失效（folder_type 为 None 则清空全部）"""
    with state.videos_scan_cache_lock:
        if folder_type is None:
            state.videos_scan_cache.clear()
        else:
            state.videos_scan_cache.pop(folder_type, None)

def _update_sentry_count():
    """更新缓存的哨兵事件总数（由 broadcaster 调用）
    
    使用 file_index 增量扫描：仅处理 mtime 发生变化的事件。
    """
    now = time.time()
    if now - state.last_sentry_scan_time < 60 and state.cached_sentry_events > 0:
        return
    state.last_sentry_scan_time = now
    try:
        from file_index import incremental_scan, get_event_count
        # 增量扫描：仅处理变化文件
        incremental_scan()
        total = 0
        for ft in video_service.VIDEO_FOLDERS:
            total += get_event_count(ft)
        with state.cached_sentry_lock:
            state.cached_sentry_events = total
    except Exception:
        # file_index 不可用时回退到全量扫描
        try:
            total = 0
            for ft in video_service.VIDEO_FOLDERS:
                total += len(video_service._scan_video_folder(ft))
            with state.cached_sentry_lock:
                state.cached_sentry_events = total
        except:
            pass

def get_cached_sentry_events():
    """获取缓存的哨兵事件总数（不触发扫描）"""
    with state.cached_sentry_lock:
        return state.cached_sentry_events

def _get_preview_status():
    """获取后台预览生成器状态（10 秒 TTL 缓存）"""
    now = time.time()
    if now - state.preview_status_cache_time < 10:
        return state.preview_status_cache
    state.preview_status_cache_time = now
    import subprocess
    queue_file = '/opt/radxa_data/teslausb/data/preview_queue.json'
    
    # 检查服务是否运行
    try:
        r = subprocess.run(['systemctl', 'is-active', 'teslausb-bgpreview'],
                         capture_output=True, text=True, timeout=5)
        svc_active = r.stdout.strip() == 'active'
    except Exception:
        svc_active = False

    if not svc_active:
        return {'state': 'stopped'}

    # 读取队列文件
    total = 0
    pending = 0
    done = 0
    queue = []
    try:
        if os.path.exists(queue_file):
            with open(queue_file, 'r') as f:
                queue = json.load(f)
            total = len(queue)
            pending = sum(1 for e in queue if e.get('status') in ('pending', 'paused'))
            done = sum(1 for e in queue if e.get('status') == 'done')
    except Exception:
        pass

    # 服务运行中，队列为空 → idle
    if total == 0:
        state.preview_status_cache = {'state': 'idle', 'total': 0, 'pending': 0, 'progress_pct': 0}
        return state.preview_status_cache

    pct = round(done / total * 100) if total > 0 else 0

    if pending > 0:
        # 卡死检测：最老 pending 超过 10 分钟无进展
        try:
            oldest = min(
                (datetime.fromisoformat(e['added_at']).timestamp()
                 for e in queue if e.get('added_at') and e.get('status') in ('pending', 'paused')),
                default=0
            )
            if oldest > 0 and time.time() - oldest > 600:  # 10分钟无进展
                state.preview_status_cache = {'state': 'idle', 'total': total, 'pending': pending,
                                         'progress_pct': pct, 'stale': True}
                return state.preview_status_cache
        except (ValueError, KeyError):
            pass
        state.preview_status_cache = {'state': 'generating', 'total': total, 'pending': pending, 'progress_pct': pct}
        return state.preview_status_cache
    else:
        state.preview_status_cache = {'state': 'done', 'total': total, 'pending': 0, 'progress_pct': 100}
        return state.preview_status_cache

# ─────────────────────────────────────────────
# 路由 - 主页面
# ─────────────────────────────────────────────

def require_auth(f):
    """认证装饰器 - 如果启用了认证，则要求登录"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 从配置文件读取是否需要认证
        config = sync_service.load_config()
        if config.get('auth_enabled', False):
            # 检查 session
            if 'user' not in session:
                # API 请求返回 JSON 错误
                if request.path.startswith('/api/'):
                    return jsonify({'success': False, 'error': '需要登录'}), 401
                # 页面请求重定向到登录页
                return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def _get_best_default_folder() -> str:
    """
    智能选择默认文件夹：优先选择有实际视频文件的文件夹。
    默认顺序：RecentClips → SentryClips → SavedClips
    如果默认文件夹为空，自动回退到有数据的文件夹。
    结果缓存 10 秒以减少文件系统 I/O。
    """
    now = time.time()
    if now - state.best_default_folder_cache['ts'] < state.BEST_FOLDER_CACHE_TTL:
        return state.best_default_folder_cache['folder']

    default_order = ['SentryClips', 'RecentClips', 'SavedClips']
    for ftype in default_order:
        if ftype not in video_service.VIDEO_FOLDERS:
            continue
        folder_path = video_service.VIDEO_FOLDERS[ftype]['path']
        if not os.path.isdir(folder_path):
            continue
        try:
            items = os.listdir(folder_path)
        except OSError:
            continue
        if not items:
            continue
        # 检查是否有实际视频文件
        for item in items:
            item_path = os.path.join(folder_path, item)
            if ftype == 'RecentClips':
                if item.lower().endswith('.mp4') and os.path.isfile(item_path):
                    state.best_default_folder_cache = {'folder': ftype, 'ts': now}
                    return ftype
            else:
                if not os.path.isdir(item_path):
                    continue
                try:
                    for f in os.listdir(item_path):
                        if f.lower().endswith('.mp4'):
                            state.best_default_folder_cache = {'folder': ftype, 'ts': now}
                            return ftype
                except OSError:
                    continue

    result = 'SentryClips'
    state.best_default_folder_cache = {'folder': result, 'ts': now}
    return result

import boombox_service

import lightshow_service

import wrap_service

MODE_FILE = '/opt/radxa_data/teslausb/data/mode.txt'

def get_system_stats():
    cpu_temp = get_cpu_temperature()
    gpu_temp = get_gpu_temperature_fields()
    mem_info = get_memory_info()
    net_info = get_network_bytes()
    nvme_health = get_nvme_health()
    disk_io = get_disk_io()
    gpu_npu = get_gpu_npu_status()
    
    load = [0.0, 0.0, 0.0]
    try:
        with open('/proc/loadavg', 'r') as f:
            load = [float(x) for x in f.read().split()[:3]]
    except:
        pass
    
    system_uptime = get_system_uptime()
    
    return {
        'cpu_percent': get_cpu_percent(),
        'cpu_temp': cpu_temp['current'],
        'cpu_temp_fields': {k: cpu_temp[k] for k in ['current','min','avg','max']},
        'gpu_temp_fields': {k: gpu_temp[k] for k in ['current','min','avg','max']},
        'system_uptime': system_uptime,
        'cpu_temp_min': cpu_temp['min'],
        'cpu_temp_avg': cpu_temp['avg'],
        'cpu_temp_max': cpu_temp['max'],
        'mem_used_mb': mem_info['mem_used_mb'],
        'mem_total_mb': mem_info['mem_total_mb'],
        'mem_percent': mem_info['mem_percent'],
        'swap_used_mb': mem_info['swap_used_mb'],
        'swap_total_mb': mem_info['swap_total_mb'],
        'swap_percent': mem_info['swap_percent'],
        'load_1min': load[0],
        'load_5min': load[1],
        'load_15min': load[2],
        'net_rx': net_info['net_rx'],
        'net_tx': net_info['net_tx'],
        'net_iface': net_info['net_iface'],
        'nvme_temp_fields': get_nvme_temperature_fields(),
        'gpu_npu': gpu_npu,
        'nvme_health': nvme_health,
        'nvme_written_fmt': fmt_bytes(nvme_health.get('data_units_written_bytes', 0)) if nvme_health.get('available') else '—',
        'disk_io': disk_io,
        'nvme_total_disk': get_nvme_total_disk(),
        'power_on_hours_fmt': fmt_power_on_hours(nvme_health.get('power_on_hours')),
        'monthly_traffic': _get_monthly_traffic(net_info['net_rx'], net_info['net_tx']),
        'fan_status': get_fan_status(),
    }

# ======================================================================
# Task #14 添加 - USB 模式切换 API
# ======================================================================
"""
Task #14 添加内容：USB 模式切换 API 端点
将此代码添加到 app.py 的路由定义部分（建议在 /api 路由区域）
"""

import os
import subprocess

# ─────────────────────────────────────────────
# USB 模式切换 API
# ─────────────────────────────────────────────

def _log_broadcaster():
    """后台线程：持续读取 journalctl 并广播到所有 SSE 订阅者"""
    import select
    proc = subprocess.Popen(
        ['sudo', '-S', 'journalctl', '-f', '-n', '50', '--no-pager', '-o', 'short-iso'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        proc.stdin.write('radxa\n')
        proc.stdin.flush()
    except:
        pass
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line.strip():
                continue
            with state.log_subscribers_lock:
                dead = []
                for q in state.log_subscribers:
                    try:
                        q.append(line.rstrip('\n'))
                    except:
                        dead.append(q)
                for q in dead:
                    state.log_subscribers.remove(q)
    except:
        pass
    finally:
        try:
            proc.terminate()
        except:
            pass

# 启动广播线程
_log_thread = threading.Thread(target=_log_broadcaster, daemon=True)
_log_thread.start()

import license_plate_service

def _stats_broadcaster():
    """后台线程：每 5 秒采集系统状态并广播到 SSE 订阅者"""
    import gc
    _last_disk_write = 0  # 磁盘缓存写入间隔（每 30s 一次）
    _last_gc = 0
    while True:
        time.sleep(5)
        # 每 5 分钟 GC 一次，避免内存碎片累积
        now = time.time()
        if now - _last_gc > 300:
            gc.collect()
            _last_gc = now
        try:
            _update_temp_histories()
            _update_nvme_temp_history()
            _update_disk_io()
            stats = {
                'time': datetime.now().strftime("%H:%M:%S"),
                'service': get_service_status(),
                'sys': (sys_stats := get_system_stats()),
                'wifi': get_wifi_info(),
                'ip': get_ip_info(),
                'disk_total': get_disk_usage('/'),
                'disk': get_all_disks(),
                'preview_status': _get_preview_status(),
                'teslacam_health': _get_teslacam_health(),
                'location_status': _get_location_status(),
            }
            # SSE 顶层兼容 JS updateDashboard 直接访问 d.nvme_total_disk 等
            stats['nvme_total_disk'] = sys_stats.get('nvme_total_disk')
            stats['nvme_written_fmt'] = sys_stats.get('nvme_written_fmt')
            stats['power_on_hours_fmt'] = sys_stats.get('power_on_hours_fmt')
            stats['monthly_traffic'] = sys_stats.get('monthly_traffic')
            stats['gpu_npu'] = sys_stats.get('gpu_npu')
            # 哨兵事件统计（使用缓存扫描）
            _update_sentry_count()
            stats['sentry_events'] = get_cached_sentry_events()

            # 写入磁盘缓存供 Present 模式使用（每 30 秒一次，减少 I/O）
            now = time.time()
            if now - _last_disk_write > 30:
                _last_disk_write = now
                try:
                    cache_dir = '/opt/radxa_data/teslausb/data'
                    os.makedirs(cache_dir, exist_ok=True)
                    cache_file = os.path.join(cache_dir, 'disk_cache.json')
                    
                    # 先读取现有缓存，保留未挂载分区数据
                    cache_data = {}
                    if os.path.exists(cache_file):
                        try:
                            with open(cache_file, 'r') as f:
                                cache_data = json.load(f)
                        except:
                            pass
                    
                    # 更新已挂载分区的数据
                    for dname in ['teslacam', 'music', 'lightshow', 'boombox']:
                        d = stats['disk'].get(dname, {})
                        if d.get('mounted'):
                            cache_data[dname] = {
                                'total': d['total'], 'used': d['used'],
                                'free': d['free'], 'percent': d['percent'],
                                'total_fmt': _format_size(d['total']),
                                'used_fmt': _format_size(d['used']),
                                'free_fmt': _format_size(d['free']),
                                'fs_type': 'exFAT',
                                'device': '/dev/nvme0n1p' + str({'teslacam': 2, 'music': 3, 'lightshow': 4, 'boombox': 5}[dname])
                            }
                    
                    if cache_data:
                        with open(cache_file + '.tmp', 'w') as f:
                            json.dump(cache_data, f)
                        os.replace(cache_file + '.tmp', cache_file)
                except:
                    pass

            # 车外监控数据 (方案C — 60s TTL 缓存，避免频繁扫描)
            _camera_cache = getattr(state, '_camera_latest_cache', None)
            _camera_cache_time = getattr(state, '_camera_latest_cache_time', 0)
            if time.time() - _camera_cache_time > 60:
                try:
                    import gif_service
                    latest = gif_service.get_latest_thumbnail()
                    count = len(gif_service.list_recent_thumbnails(limit=60))
                    _camera_cache = {
                        'available': bool(latest),
                        'latest': latest if latest else None,
                        'count': count,
                    }
                    if latest:
                        _camera_cache['latest']['url'] = '/thumbnails/' + latest['filename']
                    state._camera_latest_cache = _camera_cache
                    state._camera_latest_cache_time = time.time()
                except Exception:
                    pass
            stats['camera_latest'] = _camera_cache or {'available': False, 'latest': None, 'count': 0}

            # USB Gadget 健康状态 (2026-07-11: UDC 解绑后自动恢复监控)
            try:
                import gadget_health
                stats['gadget_status'] = gadget_health.get_gadget_status()
            except Exception:
                stats['gadget_status'] = {'udc_bound': None, 'last_error': 'gadget_health 加载失败'}

            # 缩略图健康状态 (2026-07-13: v149 重构后新增)
            try:
                stats['thumbnail_health'] = get_thumbnail_health()
            except Exception:
                pass

            with state.stats_subscribers_lock:
                dead = []
                for q in state.stats_subscribers:
                    try:
                        q.put_nowait(stats)
                    except:
                        dead.append(q)
                for q in dead:
                    state.stats_subscribers.remove(q)
        except Exception:
            _stats_logger.error(f"SSE stats collection failed:\n{traceback.format_exc()}")

def _media_disk_info(mount_point):
    """获取媒体分区磁盘信息"""
    try:
        import shutil
        if os.path.ismount(mount_point):
            u = shutil.disk_usage(mount_point)
            return {
                'total': u.total, 'used': u.used, 'free': u.free,
                'percent': round(u.used / u.total * 100, 1) if u.total > 0 else 0
            }
    except:
        pass
    # 回退到缓存
    cache_file = '/opt/radxa_data/teslausb/data/disk_cache.json'
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                cache = json.load(f)
            name = os.path.basename(mount_point)
            if name in cache:
                c = cache[name]
                return {'total': c['total'], 'used': c['used'], 'free': c['free'], 'percent': c['percent']}
    except:
        pass
    return None

# ── 媒体上传：Present/Edit 模式感知 ──────────────────────────

import staging_service

def _staging_upload(partition, file_obj, filename):
    """Present 模式: 暂存文件到 staging 目录"""
    data = file_obj.read()
    file_obj.seek(0)
    return staging_service.add_upload(partition, filename, data)

def _staging_delete(partition, filename):
    """Present 模式: 标记待删除"""
    return staging_service.add_delete(partition, filename)

def _is_present_mode():
    return staging_service.is_present()

_stats_thread = threading.Thread(target=_stats_broadcaster, daemon=True)
_stats_thread.start()

def _scan_missing_thumbnails():
    """扫描所有事件文件夹，为缺少缩略图的事件生成缩略图。
    跳过最近 2 分钟内修改的事件（哨兵可能正在写入）。
    
    RecentClips 特殊处理：按时间戳前缀分组，跳过最新一组（锁定/写入中）。
    """
    results = {'scanned': 0, 'generated': 0, 'skipped': 0, 'errors': []}
    now = time.time()
    cutoff = now - 120  # 2分钟内的视为活跃写入，跳过
    
    if not os.path.exists(THUMBNAIL_DIR):
        os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    
    for ft, info in video_service.VIDEO_FOLDERS.items():
        folder_path = info['path']
        if not os.path.isdir(folder_path):
            continue
        
        # ── RecentClips: 平铺文件结构，按时间戳前缀分组 ──
        if ft == 'RecentClips':
            # 收集所有 mp4 文件并按时间戳前缀分组
            groups = {}  # prefix -> [file_paths]
            for fname in sorted(os.listdir(folder_path)):
                if not fname.lower().endswith('.mp4'):
                    continue
                # 文件名格式: 2026-05-17_13-09-36-back.mp4
                # 用正则匹配已知摄像头后缀提取纯时间戳前缀
                import re as _re
                match = _re.match(
                    r'^(.+?)-(front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4$',
                    fname, _re.IGNORECASE
                )
                if not match:
                    continue
                prefix = match.group(1)
                if prefix not in groups:
                    groups[prefix] = []
                groups[prefix].append(os.path.join(folder_path, fname))
            
            if not groups:
                continue
            
            # 按时间排序，跳过最新一组（正在写入/锁定）
            sorted_prefixes = sorted(groups.keys())
            skip_prefix = sorted_prefixes[-1]  # 最新一组
            
            for prefix in sorted_prefixes:
                results['scanned'] += 1
                event_id = prefix
                
                # 跳过最新一组
                if prefix == skip_prefix:
                    # 检查是否在 2 分钟内（锁定/写入中）
                    newest_mtime = max(os.path.getmtime(fp) for fp in groups[prefix])
                    if newest_mtime > cutoff:
                        results['skipped'] += 1
                        continue
                    # 如果超出 2 分钟但仍是"最新"，也生成缩略图（旧数据）
                
                # 检查缓存有效性 — 统一委托 thumbnail_decision.should_regenerate()
                if not should_regenerate(event_id, ft):
                    continue  # 缓存有效，跳过
                
                # 生成缩略图 - 使用特殊路径
                try:
                    result = _generate_thumbnail(folder_path, event_id, video_files=groups[prefix], folder_type=ft)
                    if result:
                        results['generated'] += 1
                    else:
                        results['errors'].append(f"{event_id}: 生成失败 (无视频文件?)")
                except Exception as e:
                    results['errors'].append(f"{event_id}: {str(e)}")
            
            continue  # RecentClips 处理完毕
        
        # ── SentryClips / SavedClips: 事件文件夹结构 ──
        for entry in os.listdir(folder_path):
            event_path = os.path.join(folder_path, entry)
            if not os.path.isdir(event_path):
                continue
            # 跳过非事件文件夹
            has_video = any(f.lower().endswith('.mp4') for f in os.listdir(event_path) if os.path.isfile(os.path.join(event_path, f)))
            if not has_video:
                continue
            
            results['scanned'] += 1
            event_id = entry
            
            # 检查缓存有效性 — 统一委托 thumbnail_decision.should_regenerate()
            if not should_regenerate(event_id, ft):
                continue  # 缓存有效，跳过
            
            # 跳过活跃写入中的事件
            dir_mtime = os.path.getmtime(event_path)
            if dir_mtime > cutoff:
                results['skipped'] += 1
                continue
            
            # 生成缩略图
            try:
                result = _generate_thumbnail(event_path, event_id, folder_type=ft)
                if result:
                    results['generated'] += 1
                else:
                    results['errors'].append(f"{event_id}: 生成失败 (无视频文件?)")
            except Exception as e:
                results['errors'].append(f"{event_id}: {str(e)}")
    
    return results

TDASHCAM_DIR = '/opt/radxa_data/tdashcam/src'

