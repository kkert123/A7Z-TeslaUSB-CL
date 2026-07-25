import os, json, time, subprocess, threading
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, url_for
from app_state import state

from cloud_archive_service import get_sync_stats
from utils.app_helpers import get_system_stats
from utils.hardware_stats import get_all_disks, get_cpu_temperature, get_gpu_temperature_fields, get_nvme_total_disk
from utils.nvme_monitor import get_nvme_temperature_fields
from utils.system_info import get_system_uptime, get_wifi_info
import utils.system_info
import utils.hardware_stats
import utils.nvme_monitor

import video_service
import cloud_archive_service


analytics_bp = Blueprint('analytics', __name__, url_prefix='')

# Late imports from app.py (avoid circular imports at module load)
from utils.app_helpers import get_template_context, get_system_stats, _scan_video_folder


@analytics_bp.route('/analytics')
def analytics_page():
    return render_template('analytics.html', **get_template_context())


@analytics_bp.route('/api/analytics/push-health')
def api_analytics_push_health():
    """推送健康状态 - 读取 push_health.json + sentry.json 配置"""
    try:
        import os
        BASE = '/opt/radxa_data/teslausb'
        PUSH_HEALTH = os.path.join(BASE, 'data', 'push_health.json')
        SENTRY_CONFIG = os.path.join(BASE, 'config', 'sentry.json')

        # 读取推送历史
        bots = {}
        if os.path.exists(PUSH_HEALTH):
            with open(PUSH_HEALTH, 'r') as f:
                ph = json.load(f)
            raw_bots = ph.get('bots', {})
        else:
            raw_bots = {}

        # 读取配置中的 webhook keys，用于匹配机器人名称
        bot_meta = {}
        if os.path.exists(SENTRY_CONFIG):
            with open(SENTRY_CONFIG, 'r') as f:
                cfg = json.load(f)
            if cfg.get('wecom_sentry_webhook_key'):
                bot_meta[cfg['wecom_sentry_webhook_key'][-8:]] = '哨兵事件'
            if cfg.get('wecom_status_webhook_key'):
                bot_meta[cfg['wecom_status_webhook_key'][-8:]] = '系统通知'

        # 转换数据格式
        for key_suffix, bot in raw_bots.items():
            bot_id = key_suffix or 'unknown'
            # 跳过空 key_suffix 的旧记录（已知 bug：key_suffix 为空导致推送失败）
            if bot_id == 'unknown' and bot.get('total_pushes', 0) <= 1:
                continue
                display_name = bot_meta.get(bot_id, '哨兵事件' if bot.get('name') == 'sentry' else bot_id)
            else:
                display_name = bot['name']

            # 转换时间格式
            last_success = 0
            if bot.get('last_success'):
                try:
                    from datetime import datetime
                    last_success = int(datetime.fromisoformat(bot['last_success']).timestamp())
                except:
                    pass

            recent_failures = []
            for f in bot.get('recent_failures', [])[-5:]:
                try:
                    ts = int(datetime.fromisoformat(f['time']).timestamp()) if isinstance(f.get('time'), str) else f.get('time', 0)
                except:
                    ts = 0
                recent_failures.append({'time': ts, 'error': f.get('error', '')})

            bots[bot_id] = {
                'name': display_name,
                'total_pushes': bot.get('total_pushes', 0),
                'success_count': bot.get('success_count', 0),
                'fail_count': bot.get('fail_count', 0),
                'last_success_time': last_success,
                'recent_failures': recent_failures
            }

        return jsonify({'success': True, 'bots': bots})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/analytics/summary')
def api_analytics_summary():
    """哨兵事件统计摘要"""
    try:
        events = {
            'total': 0, 'uploaded': 0, 'pending': 0, 'failed': 0, 'upload_rate': 0
        }
        # 扫描视频文件夹获取事件数（_scan_video_folder 已内置 _mark_uploaded_events）
        all_evts = []
        for ft in video_service.VIDEO_FOLDERS:
            evts = video_service._scan_video_folder(ft)
            events['total'] += len(evts)
            all_evts.extend(evts)

        # 直接从扫描结果统计上传状态
        events['uploaded'] = sum(1 for e in all_evts if e.get('uploaded'))
        events['pending'] = events['total'] - events['uploaded']

        # 统计失败的同步记录数
        try:
            from cloud_archive_service import get_sync_stats
            sync_stats = get_sync_stats()
            events['failed'] = sync_stats.get('failed', 0)
        except Exception:
            pass

        if events['total'] > 0:
            events['upload_rate'] = round(events['uploaded'] / events['total'] * 100, 1)

        # 系统健康检查
        ss = get_system_stats()
        healthy = True
        issues = []
        if ss.get('cpu_percent', 0) > 90:
            healthy = False; issues.append('CPU 使用率过高')
        if ss.get('mem_percent', 0) > 90:
            healthy = False; issues.append('内存不足')
        if ss.get('cpu_temp') and ss['cpu_temp'] > 80:
            healthy = False; issues.append('CPU 温度过高')

        health = {
            'healthy': healthy,
            'issues': issues,
            'metrics': {
                'cpu_load': ss.get('cpu_percent', 0),
                'memory': {
                    'used_mb': ss.get('mem_used_mb', 0),
                    'total_mb': ss.get('mem_total_mb', 0),
                    'percent': ss.get('mem_percent', 0)
                },
                'temperature': ss.get('cpu_temp'),
                'network': get_wifi_info().get('connected', False)
            }
        }

        return jsonify({'success': True, 'events': events, 'health': health})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/analytics/disk')
def api_analytics_disk():
    """磁盘使用详情"""
    try:
        disks = get_all_disks()
        # 转换为前端友好的格式
        result = {}
        for name, info in disks.items():
            mounted = os.path.ismount(info.get('mount', ''))
            result[name] = {
                'mounted': mounted,
                'total': info.get('total', 0),
                'used': info.get('used', 0),
                'free': info.get('free', 0),
                'percent': info.get('percent', 0),
                'mount': info.get('mount', '')
            }
        return jsonify({'success': True, 'disks': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@analytics_bp.route('/api/analytics/services')
def api_analytics_services():
    """系统服务状态列表"""
    svc_list = ['teslausb-web', 'teslausb-sentry', 'teslausb-fsck.timer', 'smbd', 'cron']
    services = {}
    try:
        for svc in svc_list:
            try:
                r = subprocess.run(['systemctl', 'is-active', svc],
                                 capture_output=True, text=True, timeout=3)
                active = r.returncode == 0 and 'active' in (r.stdout or '')

                # 对于 timer，获取下次触发时间
                timer_next = None
                if svc.endswith('.timer'):
                    try:
                        tr = subprocess.run(
                            ['systemctl', 'show', svc, '--property=NextElapseUSecRealtime'],
                            capture_output=True, text=True, timeout=3
                        )
                        if tr.returncode == 0:
                            raw = tr.stdout.strip().split('=', 1)[-1]
                            if raw:
                                # 微秒时间戳 → 格式化
                                ts = int(raw) / 1_000_000
                                from datetime import datetime as dt
                                timer_next = dt.fromtimestamp(ts).strftime('%m/%d %H:%M')
                    except:
                        pass

                services[svc] = {
                    'active': active,
                    'timer_next': timer_next
                }
            except:
                services[svc] = {'active': False, 'timer_next': None}
        return jsonify({'success': True, 'services': services})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 车牌管理 (License Plate) ──────────────────────────────────

