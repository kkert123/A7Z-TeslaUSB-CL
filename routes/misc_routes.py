import os, json, time, subprocess, threading
from datetime import datetime
from flask import current_app, Blueprint, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, url_for
from app_state import state

from utils.app_helpers import fmt_bytes, get_cached_sentry_events, get_folders, get_queue_counts, get_queue_status, get_system_stats, get_template_context, get_wecom_status

import video_service
import staging_service


misc_bp = Blueprint('misc', __name__, url_prefix='')

# Late imports from app.py (avoid circular imports at module load)
from utils.app_helpers import get_template_context, _scan_missing_thumbnails, get_system_stats, THUMBNAIL_DIR, TDASHCAM_DIR, _update_temp_histories, _update_nvme_temp_history, _update_disk_io, _update_sentry_count, get_cached_sentry_events, _save_disk_cache, _generate_thumbnail
from utils.thumbnail_decision import parse_filename, should_regenerate, find_source_files
from utils import sentry_state


@misc_bp.route('/')
def index():
    """主页面 - 仪表盘"""
    return render_template('dashboard.html', **get_template_context())

# ─────────────────────────────────────────────
# 哨兵事件页面（Task B: 补齐 /sentry 孤立流程）
# ─────────────────────────────────────────────

def _fmt_sentry_dt(iso_str):
    """将状态文件中的 ISO 时间格式化为可读字符串；解析失败原样返回。"""
    if not iso_str:
        return ''
    try:
        return datetime.fromisoformat(iso_str).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return str(iso_str)


@misc_bp.route('/sentry')
def sentry_page():
    """哨兵事件确认页 —— 凭确认码查看事件、预览并确认上传"""
    code = (request.args.get('code') or '').strip()
    if not code:
        ctx = get_template_context()
        ctx.update(code='', event=None, error=None, preview_url=None,
                   detect_time=None, deadline=None)
        return render_template('sentry.html', **ctx)

    event = sentry_state.find_event_by_code(code)
    if not event:
        ctx = get_template_context()
        ctx.update(code=code, event=None, error='无效的确认码或事件已过期',
                   preview_url=None, detect_time=None, deadline=None)
        return render_template('sentry.html', **ctx), 404

    # deadline 保持 ISO 格式（前端 new Date() 解析），detect_time 格式化用于展示
    preview_url = sentry_state.build_preview_url(event.get('preview_path'))
    detect_time = _fmt_sentry_dt(event.get('detect_time'))
    deadline = event.get('confirm_deadline') or ''

    ctx = get_template_context()
    ctx.update(code=code, event=event, preview_url=preview_url,
               detect_time=detect_time, deadline=deadline, error=None)
    return render_template('sentry.html', **ctx)


@misc_bp.route('/api/sentry/preview/<path:filename>')
def api_sentry_preview(filename):
    """提供哨兵事件预览图（grid_preview.jpg）"""
    safe = os.path.basename(filename)
    full = os.path.join(sentry_state.PREVIEW_DIR, safe)
    if not os.path.isfile(full):
        return jsonify({'error': '预览图不存在'}), 404
    resp = send_file(full, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


@misc_bp.route('/api/sentry/confirm', methods=['POST'])
def api_sentry_confirm():
    """确认上传 —— 翻转状态文件为 confirmed，并触发云上传"""
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'success': False, 'error': '缺少确认码'}), 400

    event = sentry_state.find_event_by_code(code)
    if not event:
        return jsonify({'success': False, 'error': '无效的确认码或事件已过期'}), 404

    status = event.get('status')
    if status != 'pending_confirm':
        return jsonify({'success': False, 'error': f'事件状态不正确: {status}',
                        'status': status}), 400

    # 截止时间校验
    deadline = event.get('confirm_deadline')
    if deadline:
        try:
            if datetime.now() > datetime.fromisoformat(deadline):
                return jsonify({'success': False, 'error': '确认已超时',
                                'status': 'expired'}), 400
        except (ValueError, TypeError):
            pass

    event_id = event['id']

    # 写入 confirmed 并标记 web 已处理（watchdog 看到此标记会跳过）
    if not sentry_state.set_event_status(event_id, 'confirmed',
                                         extra={'confirmed_by': f'web:{code}',
                                                'web_upload_handled': True}):
        return jsonify({'success': False, 'error': '状态写入失败'}), 500

    # ── 触发云上传（后台线程，避免阻塞 API 响应） ──
    # 只要配置了云服务商就触发，不限 enabled 字段
    upload_triggered = False
    upload_msg = ''
    try:
        import cloud_archive_service
        cfg = cloud_archive_service.load_cloud_config()
        provider = cfg.get('provider', '')
        if provider and provider != 'none':
            def _do_cloud_upload():
                try:
                    result = cloud_archive_service.upload_event_to_cloud('SentryClips', event_id)
                    current_app.logger.info(
                        f'sentry confirm 云上传: event={event_id}, provider={provider}, '
                        f'ok={result.get("success")}, msg={result.get("message")}')
                except Exception as e:
                    current_app.logger.error(
                        f'sentry confirm 云上传异常: event={event_id}, err={e}', exc_info=True)
            threading.Thread(target=_do_cloud_upload, daemon=True).start()
            upload_triggered = True
            upload_msg = f'已触发{provider}上传'
        else:
            upload_msg = '未配置云服务商，仅标记已确认'
    except Exception as e:
        current_app.logger.warning(f'sentry confirm 云上传触发失败: {e}')
        upload_msg = f'上传触发异常: {e}'

    return jsonify({
        'success': True,
        'event_id': event_id,
        'status': 'confirmed',
        'upload_triggered': upload_triggered,
        'upload_msg': upload_msg,
    })


# 默认文件夹缓存已迁移到 app_state.py: state.best_default_folder_cache
# _BEST_FOLDER_CACHE_TTL 已迁移到 app_state.py: state.BEST_FOLDER_CACHE_TTL


@misc_bp.route('/logs')
def logs_page():
    return render_template('logs.html', **get_template_context())


@misc_bp.route('/api/mode/status')
def get_mode_status():
    """获取当前模式 - 使用 flag 文件 + 系统状态检测做兜底"""
    mode_file = '/tmp/teslausb_mode'

    try:
        if os.path.exists(mode_file):
            with open(mode_file, 'r') as f:
                mode = f.read().strip()
                if mode in ['present', 'edit']:
                    return jsonify({'success': True, 'mode': mode})

        # flag 文件丢失 → 检测实际状态
        # Present 模式特征: TeslaCam 分区以 ro 挂载 且 gadget 功能块存在
        cam_ro = False
        try:
            with open('/proc/mounts', 'r') as f:
                for line in f:
                    if '/mnt/teslacam' in line and 'ro,' in line:
                        cam_ro = True
                        break
        except Exception:
            pass

        gadget_active = os.path.exists('/sys/kernel/config/usb_gadget/g1/UDC')

        mode = 'present' if (cam_ro or gadget_active) else 'edit'
        return jsonify({'success': True, 'mode': mode})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@misc_bp.route('/api/system/cache-coherency')
def cache_coherency_status():
    """TeslaCam 只读挂载 VFS 缓存一致性任务状态（修复 Present 模式货不对板）。"""
    try:
        from utils.cache_coherency import get_coherency_status
        status = get_coherency_status()
        # 补充可读字段：上次刷新距今秒数
        import time as _t
        if status.get("last_refresh_ts"):
            status["seconds_since_refresh"] = round(_t.time() - status["last_refresh_ts"], 1)
        return jsonify({'success': True, **status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@misc_bp.route('/api/mode/switch', methods=['POST'])
def switch_mode():
    current_app.logger.warning("🔍🔍🔍 进入新 switch_mode() 函数!!!")
    """真正执行模式切换 - 调用底层脚本"""
    import subprocess
    import os

    data = request.get_json()
    mode = data.get('mode', '').lower()

    if mode not in ['present', 'edit']:
        return jsonify({'success': False, 'error': f'无效模式: {mode}，仅支持 present 或 edit'}), 400

    try:
        # usb_gadget_init.sh 参数：start=Present模式，stop=Edit模式
        script_path = '/opt/radxa_data/teslausb/usb_gadget_init.sh'
        if mode == 'present':
            gadget_arg = 'start'
            mode_name = 'Present Mode (连接 Tesla)'
        else:
            gadget_arg = 'stop'
            mode_name = 'Edit Mode (网络访问)'

        # 检查脚本是否存在
        if not os.path.exists(script_path):
            return jsonify({
                'success': False,
                'error': f'切换脚本不存在: {script_path}'
            }), 500

        # 记录日志
        current_app.logger.info(f"🔄 开始切换到 {mode_name}...")

        # 执行切换脚本
        # 方案1：尝试 sudo -n（无需密码）
        # 方案2：如果失败，尝试直接以 root 运行（systemd 服务本身是 root 运行）
        try:
            result = subprocess.run(
                ['sudo', '-n', 'bash', script_path, gadget_arg],
                capture_output=True,
                text=True,
                timeout=60
            )
            # 如果 sudo -n 失败（returncode != 0），尝试直接运行
            if result.returncode != 0:
                current_app.logger.warning(f"sudo -n 失败，尝试直接运行脚本: {result.stderr}")
                result = subprocess.run(
                    ['bash', script_path, gadget_arg],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
        except Exception as e:
            current_app.logger.error(f"执行脚本异常: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

        if result.returncode == 0:
            current_app.logger.info(f"✅ 成功切换到 {mode_name}")

            # 写入模式标志文件（让 get_mode_status() 能读到新状态）
            try:
                with open('/tmp/teslausb_mode', 'w') as f:
                    f.write(mode)
                current_app.logger.info(f"✅ 模式标志已写入: {mode}")
            except Exception as e:
                current_app.logger.warning(f"⚠️ 写入模式标志失败: {e}")

            # 如果是切换到 Edit 模式，同步 staging 文件 + 保存磁盘缓存 + 启动 Samba + 启动自动切回定时器
            if mode == 'edit':
                try:
                    sync_result = staging_service.sync_all()
                    current_app.logger.info(f"📦 staging sync: {sync_result['synced']} ok, {sync_result['failed']} failed")
                except Exception as e:
                    current_app.logger.warning(f"⚠️ staging 同步失败: {e}")
                try:
                    _save_disk_cache()
                except Exception as e:
                    current_app.logger.warning(f"⚠️ 保存磁盘缓存失败: {e}")
                # Edit 模式启动 Samba 文件共享（供网络访问 TeslaCam 文件）
                try:
                    subprocess.run(['sudo', '-n', 'systemctl', 'start', 'smbd'],
                                   capture_output=True, timeout=10)
                    current_app.logger.info("✅ smbd 已启动")
                except Exception as e:
                    current_app.logger.warning(f"⚠️ 启动 smbd 失败: {e}")
                # v92: 启动 Auto Present 倒计时
                try:
                    import auto_present_service as aps
                    aps.start_countdown()
                except Exception as e:
                    current_app.logger.warning(f"⚠️ 启动 Auto Present 倒计时失败: {e}")
            else:
                # 切换到 Present 模式：停止 Samba（USB Gadget 活跃时不应有 Samba 冲突）
                try:
                    subprocess.run(['sudo', '-n', 'systemctl', 'stop', 'smbd'],
                                   capture_output=True, timeout=10)
                    current_app.logger.info("✅ smbd 已停止（Present 模式）")
                except Exception as e:
                    current_app.logger.warning(f"⚠️ 停止 smbd 失败: {e}")
                # 切换到 Present 模式时取消倒计时
                try:
                    import auto_present_service as aps
                    aps.cancel_countdown()
                except Exception:
                    pass

            return jsonify({
                'success': True,
                'mode': mode,
                'message': f'已切换到 {mode_name}'
            })
        else:
            error_msg = result.stderr or result.stdout or '未知错误'
            current_app.logger.error(f"❌ 切换失败: {error_msg}")
            return jsonify({
                'success': False,
                'error': error_msg[-500:]
            }), 500

    except Exception as e:
        current_app.logger.error(f"❌ 切换异常: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ─────────────────────────────────────────────
# 视频管理 API（Task 3.2.1 新增）
# ─────────────────────────────────────────────


@misc_bp.route('/api/logs/stream')
def api_logs_stream():
    """SSE 实时日志流，支持 ?unit= 服务过滤"""
    import queue
    unit_filter = request.args.get('unit', '').strip()
    q = queue.Queue(maxsize=200)
    with state.log_subscribers_lock:
        state.log_subscribers.append(q)

    def line_matches_unit(line, unit):
        """检查日志行是否匹配指定 unit"""
        if not unit:
            return True
        # systemd 日志格式: 时间戳 主机名 服务名[PID]: 消息
        # 匹配服务名模式: unit[PID] 或 unit:
        import re
        pattern = re.escape(unit) + r'(\[\d+\])?:'
        return bool(re.search(pattern, line))

    def _get_historical_cmd(unit):
        """根据 unit 类型返回正确的 journalctl 历史日志命令"""
        if unit == 'kernel':
            return ['journalctl', '-k', '-n', '500', '--no-pager', '-o', 'short-iso']
        elif unit == 'systemd':
            # systemd init (PID 1) 的所有日志
            return ['journalctl', '_PID=1', '-n', '500', '--no-pager', '-o', 'short-iso']
        elif unit == 'cron':
            # cron 可能未安装 - 检查是否有 cron 相关日志
            return ['journalctl', '_COMM=cron', '-n', '500', '--no-pager', '-o', 'short-iso']
        elif unit:
            return ['journalctl', '-u', unit, '-n', '500', '--no-pager', '-o', 'short-iso']
        else:
            return ['journalctl', '-n', '500', '--no-pager', '-o', 'short-iso']

    def generate():
        try:
            # 先发送最近的日志（根据 unit 类型使用正确的 journalctl 命令）
            try:
                cmd = _get_historical_cmd(unit_filter)
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.stdout and '-- No entries --' not in result.stdout:
                    for line in result.stdout.strip().split('\n'):
                        if line.strip():
                            yield f"data: {line}\n\n"
                elif unit_filter in ('cron', 'smbd'):
                    # cron/smbd 可能未安装
                    msg = f'{unit_filter} 服务未安装或未配置，无日志记录'
                    yield f"data: {json.dumps({'text': msg, 'timestamp': '⚠️ 系统提示'})}\n\n"
            except:
                pass

            while True:
                try:
                    line = q.get(timeout=30)
                    if line_matches_unit(line, unit_filter):
                        yield f"data: {line}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with state.log_subscribers_lock:
                if q in state.log_subscribers:
                    state.log_subscribers.remove(q)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@misc_bp.route('/api/logs/history')
def api_logs_history():
    """获取指定日期的历史日志，支持 ?date=YYYY-MM-DD&unit= 服务过滤&limit=N"""
    from datetime import datetime, timedelta
    import re as _re

    # 参数解析
    date_str = request.args.get('date', '').strip()
    unit = request.args.get('unit', '').strip()
    try:
        limit = int(request.args.get('limit', '500'))
        limit = max(1, min(limit, 2000))  # 限制 1-2000 行
    except (ValueError, TypeError):
        limit = 500

    # 日期验证：必须为 YYYY-MM-DD 格式，且在最近 7 天内
    if not _re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'error': '日期格式无效，请使用 YYYY-MM-DD'}), 400
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': '日期不存在'}), 400

    now = datetime.now()
    seven_days_ago = now - timedelta(days=7)
    if target_date.date() < seven_days_ago.date() or target_date.date() > now.date():
        return jsonify({'error': '日期超出范围（仅支持最近 7 天）'}), 400

    since_str = target_date.strftime('%Y-%m-%d 00:00:00')
    until_str = target_date.strftime('%Y-%m-%d 23:59:59')

    # 构建 journalctl 命令（白名单方式防止注入）
    # unit 仅允许字母数字、连字符、下划线、@ 和点
    if unit and not _re.match(r'^[a-zA-Z0-9\-_.@]+$', unit):
        return jsonify({'error': '服务名包含非法字符'}), 400

    # 使用 --reverse 使最新日志在前（与实时流方向一致）
    # 不设置 -n 限制，返回时间段内所有日志
    if unit == 'kernel':
        cmd = ['journalctl', '-k', '--since', since_str, '--until', until_str,
               '--reverse', '--no-pager', '-o', 'short-iso']
    elif unit == 'systemd':
        cmd = ['journalctl', '_PID=1', '--since', since_str, '--until', until_str,
               '--reverse', '--no-pager', '-o', 'short-iso']
    elif unit == 'cron':
        cmd = ['journalctl', '_COMM=cron', '--since', since_str, '--until', until_str,
               '--reverse', '--no-pager', '-o', 'short-iso']
    elif unit:
        cmd = ['journalctl', '-u', unit, '--since', since_str, '--until', until_str,
               '--reverse', '--no-pager', '-o', 'short-iso']
    else:
        cmd = ['journalctl', '--since', since_str, '--until', until_str,
               '--reverse', '--no-pager', '-o', 'short-iso']

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = []
        if result.stdout and '-- No entries --' not in result.stdout:
            lines = [ln for ln in result.stdout.strip().split('\n') if ln.strip()]
        # 服务端安全限制：最多返回 50000 行
        total_lines = len(lines)
        if total_lines > 50000:
            lines = lines[:50000]
        return jsonify({
            'date': date_str,
            'unit': unit,
            'lines': lines,
            'count': len(lines),
            'total': total_lines,
            'has_more': total_lines > 50000,
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': '日志查询超时（日期范围太大）'}), 504
    except Exception as e:
        current_app.logger.error(f"历史日志查询失败: {e}")
        return jsonify({'error': f'查询失败: {str(e)}'}), 500


# ── 文件日志 API（非 journalctl 的日志文件）────────────────────

# 可访问的日志文件映射：名称 → 文件路径
LOG_FILE_MAP = {
    'bg_preview': '/var/log/teslausb-bgpreview.log',
    'sentry': '/var/log/teslausb-sentry.log',
    'wifi': '/var/log/wifi-smart-switch.log',
    'boot': '/var/log/teslausb-boot-notify.log',
    'notify': '/var/log/teslausb-notify-retry.log',
    'web_log': '/var/log/teslausb.log',
}


@misc_bp.route('/api/logs/file-stream')
def api_logs_file_stream():
    """SSE 实时日志流 — 基于日志文件（非 journalctl）。
    支持 ?name=bg_preview|sentry|wifi|boot|notify|web_log
    """
    name = request.args.get('name', '').strip()
    if name not in LOG_FILE_MAP:
        return jsonify({'error': '未知日志源，可用: ' + ', '.join(LOG_FILE_MAP.keys())}), 400

    filepath = LOG_FILE_MAP[name]

    def generate():
        try:
            # 先发送最后 200 行历史
            if os.path.isfile(filepath):
                try:
                    with open(filepath, 'r', errors='replace') as f:
                        lines = f.readlines()
                        for line in lines[-200:]:
                            if line.strip():
                                yield f"data: {line.rstrip()}\n\n"
                except OSError:
                    pass

            # tail -f 实时跟踪
            proc = subprocess.Popen(
                ['tail', '-f', '-n', '0', filepath],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            try:
                for line in iter(proc.stdout.readline, ''):
                    if line.strip():
                        yield f"data: {line.rstrip()}\n\n"
            finally:
                proc.terminate()
        except GeneratorExit:
            pass

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@misc_bp.route('/api/logs/sizes')
def api_logs_sizes():
    """返回所有日志文件的大小摘要"""
    result = {}
    for name, path in LOG_FILE_MAP.items():
        try:
            size = os.path.getsize(path)
            result[name] = {'path': path, 'size': size, 'size_fmt': _fmt_size(size)}
        except OSError:
            result[name] = {'path': path, 'size': 0, 'size_fmt': 'N/A'}
    return jsonify(result)


def _fmt_size(size):
    """格式化文件大小"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


@misc_bp.route('/api/logs/download')
def api_logs_download():
    """下载日志文件"""
    name = request.args.get('name', '').strip()
    if name not in LOG_FILE_MAP:
        return jsonify({'error': '未知日志源'}), 400
    filepath = LOG_FILE_MAP[name]
    if not os.path.isfile(filepath):
        return jsonify({'error': '日志文件不存在'}), 404
    return send_file(filepath, as_attachment=True, download_name=f"{name}.log")


@misc_bp.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    """清除日志文件内容（保留文件，清空内容）"""
    name = request.args.get('name', '').strip()
    if name not in LOG_FILE_MAP:
        return jsonify({'error': '未知日志源'}), 400
    filepath = LOG_FILE_MAP[name]
    try:
        with open(filepath, 'w') as f:
            f.truncate(0)
        return jsonify({'success': True, 'message': f'{name} 日志已清除'})
    except OSError as e:
        return jsonify({'error': f'清除失败: {str(e)}'}), 500


# ── 系统操作 API ──────────────────────────────────────────────


@misc_bp.route('/api/thumbnails/scan', methods=['POST'])
def api_scan_thumbnails():
    """主动扫描并生成缺失的缩略图"""
    try:
        results = _scan_missing_thumbnails()
        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@misc_bp.route('/api/thumbnails/reset', methods=['POST'])
def api_reset_thumbnails():
    """重置指定时间范围内的缩略图（删除后由 bg_preview 自动重新生成）"""
    try:
        data = request.get_json(silent=True) or {}
        minutes = int(data.get('minutes', 60))
        minutes = max(1, min(minutes, 1440))  # 限制 1 分钟到 24 小时
        import time as _t
        cutoff = _t.time() - minutes * 60
        deleted = 0
        errors = []
        for fname in os.listdir(THUMBNAIL_DIR):
            fpath = os.path.join(THUMBNAIL_DIR, fname)
            if not os.path.isfile(fpath) or not fname.endswith('_grid.jpg'):
                continue
            try:
                if os.path.getmtime(fpath) >= cutoff:
                    os.remove(fpath)
                    deleted += 1
            except OSError as e:
                errors.append(fname)
        result = {
            'success': True,
            'deleted': deleted,
            'errors': errors[:10],
            'minutes': minutes,
        }
        # 不在此处重新扫描（会很慢），bg_preview 30s 内自动补全
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



@misc_bp.route("/thumbnails/<path:filename>")
def serve_thumbnail(filename):
    """提供缩略图静态文件（懒生成：首次请求时触发 ffmpeg 提取帧）

    缓存策略由 utils.thumbnail_decision.should_regenerate() 统一管理。
    支持新旧两种命名格式：
    - 新格式: REC_2026-06-15_17-24-11_grid.jpg
    - 旧格式: 2026-06-15_17-24-11_grid.jpg (向后兼容)
    """
    thumbnail_path = os.path.join(THUMBNAIL_DIR, filename)

    # 1. 快速检查：最近 5 秒内生成的缩略图直接返回
    if os.path.exists(thumbnail_path):
        if time.time() - os.path.getmtime(thumbnail_path) < 5:
            return send_from_directory(THUMBNAIL_DIR, filename)

    # 2. 解析文件名 → (folder_type, event_id)
    folder_type, event_id = parse_filename(filename)

    # 3. 旧格式兼容：检查是否已有带前缀的新格式版本
    if folder_type is None and event_id:
        for ft, prefix in video_service._THUMB_PREFIX.items():
            alt_path = os.path.join(THUMBNAIL_DIR, f"{prefix}{event_id}_grid.jpg")
            if os.path.exists(alt_path):
                return send_from_directory(THUMBNAIL_DIR, f"{prefix}{event_id}_grid.jpg")

    # 4. 缓存有效性检查：缓存有效则直接返回
    if not should_regenerate(event_id, folder_type):
        # should_regenerate 返回 False 表示缓存有效
        # 确保返回正确的路径（可能是旧格式）
        if os.path.exists(thumbnail_path):
            return send_from_directory(THUMBNAIL_DIR, filename)
        # 如果原路径不存在但 should_regenerate 说可以，检查旧格式路径
        if folder_type:
            alt = video_service.get_thumbnail_path(folder_type, event_id)
            alt_filename = os.path.basename(alt)
            if os.path.exists(alt):
                return send_from_directory(THUMBNAIL_DIR, alt_filename)

    # 5. 查找源视频文件
    event_path, video_files = find_source_files(event_id, folder_type)

    # 6. RecentClips 加密检测：所有文件已加密时跳过生成
    if video_files:
        valid_count = sum(1 for vf in video_files if video_service.is_valid_mp4(vf))
        if valid_count == 0:
            # 文件已加密，无法生成缩略图。如果有缓存则返回缓存。
            if os.path.exists(thumbnail_path):
                return send_from_directory(THUMBNAIL_DIR, filename)
            event_path = None  # 跳过生成

    # 7. 源文件未找到，但有缓存 → 返回缓存
    if not event_path:
        if os.path.exists(thumbnail_path):
            return send_from_directory(THUMBNAIL_DIR, filename)
        # 无缓存无源文件 → 返回占位图（旧格式兼容）
        print(f"[Thumbnail] 未找到事件: {event_id}")

    # 8. 生成缩略图
    if event_path:
        try:
            result = _generate_thumbnail(event_path, event_id, video_files=video_files, folder_type=folder_type)
            if result and os.path.exists(thumbnail_path):
                return send_from_directory(THUMBNAIL_DIR, filename)
            print(f"[Thumbnail] 懒生成失败: {event_id} (result={result})")
        except Exception as e:
            print(f"[Thumbnail] 懒生成异常: {event_id} - {e}")

    # 9. 兜底：返回占位图
    placeholder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'placeholder.svg')
    if os.path.exists(placeholder):
        return send_file(placeholder, mimetype='image/svg+xml')
    return Response(
        '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">'
        '<rect width="100%" height="100%" fill="#1a1a2e"/>'
        '<text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="14">No Preview</text>'
        '</svg>',
        mimetype='image/svg+xml'
    )


# ═══════════════════════════════════════════════════════════════
# TDashcam HTTP API — 视频文件 serve + 事件列表 (替代 player_routes)
# ═══════════════════════════════════════════════════════════════


@misc_bp.route('/tdashcam/')
@misc_bp.route('/tdashcam/<path:filename>')
def serve_tdashcam(filename='index.html'):
    """Serve TDashcam Studio static files"""
    if not os.path.isdir(TDASHCAM_DIR):
        return "TDashcam Studio 未安装，请先在 A7Z 上执行 git clone", 503
    return send_from_directory(TDASHCAM_DIR, filename)


# ═══════════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════════

