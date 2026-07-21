# routes/cleanup_routes.py
from flask import Blueprint, jsonify, request, current_app
import os
import json
import time
import subprocess
from datetime import datetime
from utils.hardware_stats import get_all_disks

cleanup_bp = Blueprint('cleanup', __name__, url_prefix='')

CLEANUP_LOG = '/opt/radxa_data/teslausb/data/cleanup_history.json'
THUMBNAIL_DIR = '/opt/radxa_data/teslausb/static/thumbnails'


def _format_size(size_bytes):
    """格式化字节大小"""
    if not size_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def _get_cleanup_partitions():
    """获取分区状态（含阈值评估）"""
    parts = {}
    disks = get_all_disks()
    for name, info in disks.items():
        mount_ok = os.path.ismount(info.get('mount', ''))
        pct = info.get('percent', 0)
        if not mount_ok:
            # 有缓存数据时提供完整字段
            total = info.get('total', 0)
            if total > 0:
                parts[name] = {
                    'status': '未挂载',
                    'path': info.get('mount', ''),
                    'percent': info.get('percent', 0),
                    'total_gb': round(total / (1024**3), 1),
                    'used_gb': round(info.get('used', 0) / (1024**3), 1),
                    'free_gb': round(info.get('free', 0) / (1024**3), 1),
                    'total_fmt': _format_size(total)
                }
            else:
                parts[name] = {'status': '未挂载', 'path': info.get('mount', ''), 'percent': 0}
        else:
            status_label = '正常'
            if pct > 95:
                status_label = '紧急'
            elif pct > 90:
                status_label = '严重'
            elif pct > 85:
                status_label = '警告'
            parts[name] = {
                'status': status_label,
                'path': info.get('mount', ''),
                'percent': pct,
                'used_gb': round(info.get('used', 0) / (1024**3), 1),
                'total_gb': round(info.get('total', 0) / (1024**3), 1),
                'free_gb': round(info.get('free', 0) / (1024**3), 1)
            }
    return parts


@cleanup_bp.route('/api/cleanup/status')
def api_cleanup_status():
    """清理策略和分区状态（v2 per-folder 策略）"""
    try:
        import auto_cleanup
        cleaner = auto_cleanup.AutoCleaner(dry_run=True)
        policies_dict = {name: p.to_dict() for name, p in cleaner.policies.items()}
        gs = auto_cleanup.get_global_settings()
        return jsonify({
            'success': True,
            'disk_thresholds': {
                'warning': gs.get('disk_threshold_warning', auto_cleanup.DISK_THRESHOLD_WARNING),
                'critical': gs.get('disk_threshold_critical', auto_cleanup.DISK_THRESHOLD_CRITICAL),
                'emergency': gs.get('disk_threshold_emergency', auto_cleanup.DISK_THRESHOLD_EMERGENCY),
            },
            'retention': {
                'previews_days': gs.get('preview_max_age_days', auto_cleanup.PREVIEW_MAX_AGE_DAYS),
                'temp_days': gs.get('temp_max_age_days', auto_cleanup.TEMP_MAX_AGE_DAYS),
                'logs_days': gs.get('log_max_age_days', auto_cleanup.LOG_MAX_AGE_DAYS),
            },
            'policies': policies_dict,
            'partitions': _get_cleanup_partitions()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# 保持旧 API 兼容
@cleanup_bp.route('/api/cleanup/policies')
def api_cleanup_policies():
    return api_cleanup_status()


@cleanup_bp.route('/api/cleanup/preview', methods=['POST'])
def api_cleanup_preview():
    """预览清理计划（v2 per-folder age/size/count 策略）"""
    try:
        import auto_cleanup
        force_all = request.json.get('force_all', False) if request.is_json else False

        cleaner = auto_cleanup.AutoCleaner(dry_run=True)
        cam_path = "/mnt/teslacam"

        # 检测当前模式
        is_ro = False
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    if cam_path in line:
                        parts = line.split()
                        if len(parts) >= 4:
                            opts = parts[3]
                            is_ro = "ro" in opts.split(",")
                        break
        except Exception:
            pass
        is_present = os.path.ismount(cam_path) and is_ro
        mode_label = "Present (只读)" if is_present else "Edit (读写)" if os.path.ismount(cam_path) else "未挂载"

        lines = ['=== 清理预览（v2 per-folder 策略）===', '']
        lines.append(f'📡 当前模式: {mode_label}')
        if is_present:
            lines.append('   ⚠ Present 模式下 cam 分区为只读，只预览不执行')
        lines.append('')

        # 1. 过期预览/临时/日志文件
        preview_dir = auto_cleanup.PREVIEW_DIR
        cutoff_preview = time.time() - auto_cleanup.PREVIEW_MAX_AGE_DAYS * 86400
        preview_expired = cleaner._count_expired(preview_dir, cutoff_preview)
        preview_total = len(os.listdir(preview_dir)) if os.path.isdir(preview_dir) else 0
        lines.append(f'📸 预览图: {preview_expired} 个过期 / {preview_total} 总计 (> {auto_cleanup.PREVIEW_MAX_AGE_DAYS}天)')
        
        # 预览孤儿统计（无对应视频/事件的缩略图）
        try:
            import video_service as _vs, re as _re
            _prefixes = {'SEN', 'SAV', 'REC'}
            _map = {'SEN': 'SentryClips', 'SAV': 'SavedClips', 'REC': 'RecentClips'}
            _existing = {}
            for _ft, _info in _vs.VIDEO_FOLDERS.items():
                _fp = _info['path']
                if not os.path.isdir(_fp):
                    _existing[_ft] = set()
                    continue
                if _ft == 'RecentClips':
                    _existing[_ft] = set()
                    for _fn in os.listdir(_fp):
                        _m = _re.match(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(front|back|left_repeater|right_repeater)\.mp4$', _fn, _re.IGNORECASE)
                        if _m: _existing[_ft].add(_m.group(1))
                else:
                    _existing[_ft] = {d for d in os.listdir(_fp) if os.path.isdir(os.path.join(_fp, d))}
            _orphan_count = 0
            if os.path.isdir(preview_dir):
                _thumb_re = _re.compile(r'^(SEN|SAV|REC)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:-[a-z_]+)?_grid\.jpg$')
                for _fn in os.listdir(preview_dir):
                    _m = _thumb_re.match(_fn)
                    if _m:
                        _pfx, _eid = _m.group(1), _m.group(2)
                        _ft = _map.get(_pfx)
                        if _ft and _ft in _existing and _eid not in _existing[_ft]:
                            _orphan_count += 1
            if _orphan_count > 0:
                lines.append(f'   ⚠ 其中 {_orphan_count} 张为孤儿缩略图（视频已不存在）')
        except Exception:
            pass
        lines.append('')
        data_dir = auto_cleanup.DATA_DIR
        cutoff_temp = time.time() - auto_cleanup.TEMP_MAX_AGE_DAYS * 86400
        temp_expired = cleaner._count_expired(data_dir, cutoff_temp, pattern='*.tmp')
        lines.append(f'🗑 临时文件: {temp_expired} 个过期 (> {auto_cleanup.TEMP_MAX_AGE_DAYS}天)')

        log_expired = 0
        if os.path.isdir('/var/log'):
            cutoff_log = time.time() - auto_cleanup.LOG_MAX_AGE_DAYS * 86400
            for fname in os.listdir('/var/log'):
                if 'teslausb' in fname and (fname.endswith('.log') or '.log' in fname):
                    try:
                        if os.stat(os.path.join('/var/log', fname)).st_mtime < cutoff_log:
                            log_expired += 1
                    except OSError:
                        pass
        lines.append(f'📋 项目日志: {log_expired} 个过期 (> {auto_cleanup.LOG_MAX_AGE_DAYS}天)')
        lines.append('')

        # 2. Per-folder 清理计划
        plan = cleaner.calculate_cleanup_plan(cam_path, respect_enabled=not force_all)
        disk = cleaner.get_disk_usage(cam_path)

        lines.append('--- 视频清理计划 ---')
        lines.append(f'清理模式: {"所有文件夹" if force_all else "仅 enabled 文件夹"}')
        lines.append('')

        if plan['total_count'] == 0:
            lines.append('✅ 所有文件夹在策略范围内，无需清理')
        else:
            for folder_name, info in plan['breakdown'].items():
                policy = info.get('policy', {})
                lines.append(f'📁 {folder_name}:')
                lines.append(f'   策略: age≤{policy.get("age_days","?")}天, size≤{policy.get("max_gb","?")}GB, count≤{policy.get("max_count","?")}个')
                lines.append(f'   待清理: {info["count"]} 个文件, {info["size_gb"]} GB')
            lines.append(f'📦 总计: {plan["total_count"]} 个文件, {plan["total_size_gb"]} GB')
            lines.append(f'🛡 受保护: {plan["protected_count"]} 个文件, {plan["protected_size_gb"]} GB')

        lines.append('')
        if disk:
            pct = disk['percent']
            status = '紧急' if pct >= 95 else '严重' if pct >= 90 else '警告' if pct >= 85 else '正常'
            used_gb = round(disk['used']/(1024**3), 1)
            total_gb = round(disk['total']/(1024**3), 1)
            lines.append(f'💾 磁盘: {used_gb}/{total_gb} GB ({pct}%) [{status}]')

            if plan['total_size_gb'] > 0:
                new_used = used_gb - plan['total_size_gb']
                new_pct = int((new_used / total_gb) * 100) if total_gb else 0
                lines.append(f'📊 清理后预计: {new_used:.1f}/{total_gb} GB ({new_pct}%)')

        if plan['total_count'] > 0 and is_present:
            lines.append('')
            lines.append('⚠ 提示: 当前为 Present 模式（只读），切换到 Edit 模式后执行清理')

        return jsonify({
            'success': True,
            'output': '\n'.join(lines),
            'plan': {
                'total_count': plan['total_count'],
                'total_size_gb': plan['total_size_gb'],
                'breakdown': {k: {'count': v['count'], 'size_gb': v['size_gb']}
                              for k, v in plan['breakdown'].items()},
                'protected_count': plan['protected_count'],
            },
            'mode': 'present' if is_present else 'edit' if os.path.ismount(cam_path) else 'unmounted',
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@cleanup_bp.route('/api/cleanup/execute', methods=['POST'])
def api_cleanup_execute():
    """执行清理（v2 per-folder age/size/count 策略）"""
    try:
        import auto_cleanup
        import subprocess
        force_all = request.json.get('force_all', False) if request.is_json else False

        cleaner = auto_cleanup.AutoCleaner(dry_run=False)
        cam_path = "/mnt/teslacam"

        lines = ['=== 清理执行（v2 per-folder 策略）===', '']

        # 检查 TeslaCam 是否可访问
        teslacam_dir = os.path.join(cam_path, "TeslaCam")
        if not os.path.isdir(teslacam_dir):
            lines.append(f'❌ TeslaCam 目录不可访问: {teslacam_dir}')
            lines.append('   请确认 Tesla USB 已连接且已切换到 Edit 模式')
            # 尝试列出挂载点
            try:
                mounts = [l for l in open("/proc/mounts").read().split('\n') if cam_path in l]
                if mounts:
                    lines.append(f'   当前挂载: {mounts[0][:120]}')
                else:
                    lines.append(f'   {cam_path} 未挂载')
            except Exception:
                pass
            return jsonify({
                'success': False,
                'output': '\n'.join(lines),
                'error': 'TeslaCam 目录不可访问',
            })

        # 检查分区是否只读，如果是则尝试 remount 读写
        try:
            is_ro = False
            with open("/proc/mounts") as f:
                for line in f:
                    if cam_path in line:
                        parts = line.split()
                        if len(parts) >= 4:
                            is_ro = "ro" in parts[3].split(",")
                        break
            if is_ro:
                lines.append('🔓 检测到只读挂载，正在 remount 读写...')
                try:
                    subprocess.run(
                        ["sudo", "-n", "mount", "-o", "remount,rw", cam_path],
                        capture_output=True, text=True, timeout=10,
                    )
                    # 验证
                    is_ro_after = False
                    with open("/proc/mounts") as f:
                        for line in f:
                            if cam_path in line:
                                p = line.split()
                                if len(p) >= 4:
                                    is_ro_after = "ro" in p[3].split(",")
                                break
                    if is_ro_after:
                        subprocess.run(
                            ["sudo", "-n", "mount", "-o", "rw,remount", cam_path],
                            capture_output=True, text=True, timeout=10,
                        )
                    lines.append('   ✅ 已切换为读写模式')
                except Exception as e:
                    lines.append(f'   ⚠ remount 失败: {e}')
        except Exception:
            pass

        # 1. 非视频文件清理
        freed_preview = cleaner.cleanup_previews()
        freed_temp = cleaner.cleanup_temp_files()
        freed_logs = cleaner.cleanup_logs()

        non_video_freed = freed_preview + freed_temp + freed_logs
        for label, f in [("预览图", freed_preview), ("临时文件", freed_temp), ("日志文件", freed_logs)]:
            if f > 0:
                lines.append(f'  ✅ 清理{label}: {_format_size(f)}')

        # 2. 视频文件清理（per-folder 策略）
        plan = cleaner.calculate_cleanup_plan(cam_path, respect_enabled=not force_all)

        if plan['total_count'] > 0:
            result = cleaner.execute_cleanup(plan)
            for action in result.get('actions', []):
                lines.append(f'  ✅ {action}')
            # 报告跳过和缺失
            skipped = cleaner.stats.get('skipped_files', 0)
            missing = cleaner.stats.get('missing_files', 0)
            if skipped:
                lines.append(f'  ⚠ 跳过 {skipped} 个文件（被保护或占用）')
            if missing:
                lines.append(f'  ⚠ {missing} 个文件已不存在（可能已被删除）')
        else:
            lines.append('  ℹ 没有需要清理的视频文件')

        total_deleted = cleaner.stats.get('deleted_files', 0)
        total_freed = cleaner.stats.get('freed_bytes', 0) + non_video_freed

        if total_deleted == 0 and non_video_freed == 0:
            lines.append('  ℹ 没有需要清理的文件')

        # 报告错误
        errors = cleaner.stats.get('errors', [])
        if errors:
            lines.append('')
            lines.append('⚠ 错误信息:')
            for e in errors[:5]:
                lines.append(f'   • {str(e)[:200]}')

        lines.append('')
        lines.append(f'📊 总计: 删除 {total_deleted} 个文件, 释放 {_format_size(total_freed)}')

        # 保存历史
        _save_cleanup_history({
            'timestamp': datetime.now().isoformat(),
            'deleted_count': total_deleted,
            'freed_bytes': total_freed,
            'force_all': force_all,
            'breakdown': cleaner.stats.get('breakdown', {}),
        })

        return jsonify({
            'success': True,
            'output': '\n'.join(lines),
            'deleted_count': total_deleted,
            'freed_bytes': total_freed,
            'freed_gb': round(total_freed / (1024**3), 2),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleanup_bp.route('/api/cleanup/policy', methods=['POST'])
def api_cleanup_save_policy():
    """保存清理策略（per-folder + 全局阈值）"""
    try:
        import auto_cleanup
        data = request.get_json(force=True, silent=True) or {}

        # 保存 per-folder 策略
        policies_update = data.get('policies', {})
        if policies_update:
            policies = auto_cleanup.load_policies()
            for name in auto_cleanup.ALLOWED_FOLDER_NAMES:
                if name in policies_update:
                    p = policies_update[name]
                    policy = policies[name]
                    policy.enabled = bool(p.get('enabled', policy.enabled))
                    policy.age_days = int(p.get('age_days', policy.age_days))
                    policy.max_gb = float(p.get('max_gb', policy.max_gb))
                    policy.max_count = int(p.get('max_count', policy.max_count))
                    policy.protect_unsynced = bool(p.get('protect_unsynced', policy.protect_unsynced))
            auto_cleanup.save_policies(policies)

        # 保存全局阈值
        global_settings = data.get('global_settings', {})
        if global_settings:
            auto_cleanup.save_global_settings(global_settings)

        # 返回最新状态
        gs = auto_cleanup.get_global_settings()
        policies = auto_cleanup.load_policies()
        return jsonify({
            'success': True,
            'message': '清理策略已保存',
            'disk_thresholds': {
                'warning': gs.get('disk_threshold_warning', auto_cleanup.DISK_THRESHOLD_WARNING),
                'critical': gs.get('disk_threshold_critical', auto_cleanup.DISK_THRESHOLD_CRITICAL),
                'emergency': gs.get('disk_threshold_emergency', auto_cleanup.DISK_THRESHOLD_EMERGENCY),
            },
            'retention': {
                'previews_days': gs.get('preview_max_age_days', auto_cleanup.PREVIEW_MAX_AGE_DAYS),
                'temp_days': gs.get('temp_max_age_days', auto_cleanup.TEMP_MAX_AGE_DAYS),
                'logs_days': gs.get('log_max_age_days', auto_cleanup.LOG_MAX_AGE_DAYS),
            },
            'policies': {name: p.to_dict() for name, p in policies.items()},
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _save_cleanup_history(entry):
    """保存清理历史记录"""
    try:
        os.makedirs(os.path.dirname(CLEANUP_LOG), exist_ok=True)
        history = []
        if os.path.exists(CLEANUP_LOG):
            with open(CLEANUP_LOG, 'r') as f:
                history = json.load(f)
        history.insert(0, entry)
        # 最多保留 50 条
        history = history[:50]
        with open(CLEANUP_LOG, 'w') as f:
            json.dump(history, f, indent=2)
    except:
        pass


@cleanup_bp.route('/api/cleanup/history')
def api_cleanup_history():
    """清理历史记录（最近 20 条）"""
    try:
        history = []
        if os.path.exists(CLEANUP_LOG):
            with open(CLEANUP_LOG, 'r') as f:
                history = json.load(f)
        # 只返回最近 20 条，避免响应过大
        history = history[:20] if isinstance(history, list) else []
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleanup_bp.route('/api/thumbnails/cleanup', methods=['POST'])
def api_cleanup_thumbnails():
    """清除所有缩略图缓存（用于修复货不对板问题后强制重新生成）"""
    try:
        deleted = 0
        errors = 0
        prefixes = ['REC_', 'SEN_', 'SAV_']
        for fname in os.listdir(THUMBNAIL_DIR):
            if not fname.endswith('_grid.jpg'):
                continue
            for prefix in prefixes:
                if fname.startswith(prefix):
                    try:
                        os.unlink(os.path.join(THUMBNAIL_DIR, fname))
                        deleted += 1
                    except OSError:
                        errors += 1
                    break
        return jsonify({'success': True, 'deleted': deleted, 'errors': errors})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
