import os, json, time, subprocess, threading, tempfile, io
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, url_for, after_this_request
from app_state import state

import video_service
import staging_service
from utils.sei_parser import _extract_sei_direct
from utils.mvhd_timestamp import get_real_event_time
from utils import video_trim


video_bp = Blueprint('video', __name__, url_prefix='')

# Late imports from app.py (avoid circular imports at module load)
from utils.app_helpers import _get_best_default_folder, _get_cached_video_scan, _set_cached_video_scan, _invalidate_video_cache, _format_size, _to_local_time, _scan_video_folder


@video_bp.route('/videos')
def videos_page():
    """视频管理页面（v2 — 使用 video_service，带 30s 扫描缓存）"""
    folder_type = request.args.get('folder', None)
    if folder_type is None:
        folder_type = _get_best_default_folder()
    elif folder_type not in video_service.VIDEO_FOLDERS:
        folder_type = _get_best_default_folder()

    # 使用缓存避免每次请求全量扫描（尤其是 RecentClips 几百个文件导致 CPU/内存拉满）
    events, stats = _get_cached_video_scan(folder_type)
    if events is None:
        events = video_service._scan_video_folder(folder_type)
        # 直接从 events 计算 stats，避免 get_video_stats 再次扫描
        total_events = len(events) if events else 0
        uploaded_count = sum(1 for e in events if e.get('uploaded')) if events else 0
        total_size = sum(e.get('total_size', 0) for e in events) if events else 0
        stats = {
            'total_events': total_events,
            'uploaded_count': uploaded_count,
            'total_size': video_service._format_size(total_size),
        }
        _set_cached_video_scan(folder_type, events, stats)

    # mvhd 时钟修正 (RecentClips)
    if folder_type == 'RecentClips':
        _apply_mvhd_clock_correction(events, folder_type)

    return render_template(
        'videos.html',
        folders=video_service.VIDEO_FOLDERS,
        current_folder=folder_type,
        events=events,
        total_events=stats['total_events'],
        uploaded_count=stats['uploaded_count'],
        total_size=stats['total_size'],
        format_size=video_service._format_size
    )


@video_bp.route('/api/videos/delete', methods=['POST'])
def api_videos_delete():
    """API: 标记事件删除（入 staging 队列，Edit 模式时 sync_all 真正删除）。

    流程：Present 模式下将删除请求写入 staging manifest →
          用户切换到 Edit 模式 → switch_mode 调用 staging_service.sync_all() →
          真正删除文件。
    """
    data = request.get_json() or {}
    folder_type = data.get('folder', '').strip()
    event_id = data.get('event_id', '').strip()

    if not folder_type or folder_type not in video_service.VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': '无效的文件夹类型'}), 400
    if not event_id:
        return jsonify({'success': False, 'error': '缺少事件ID'}), 400
    if '..' in event_id or '/' in event_id:
        return jsonify({'success': False, 'error': '无效的事件ID'}), 400

    try:
        # 将删除请求写入 staging manifest（切换 Edit 模式后执行）
        result = staging_service.add_video_event_delete(folder_type, event_id)

        if result.get('success'):
            _invalidate_video_cache(folder_type)  # 使缓存失效，下次扫描会过滤
            return jsonify({
                'success': True,
                'message': f'事件 {event_id} 已标记删除，切换到 Edit 模式后生效。',
            })
        else:
            return jsonify({'success': False, 'error': result.get('message', '标记失败')}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@video_bp.route('/api/videos/list')
def api_videos_list():
    """API: 获取视频事件列表（JSON）—— 使用 video_service + 缓存"""
    folder_type = request.args.get('folder', None)
    if folder_type is None:
        folder_type = _get_best_default_folder()
    elif folder_type not in video_service.VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': '无效的文件夹类型'}), 400

    # 使用缓存避免 API 轮询导致频繁全量扫描
    events, _stats = _get_cached_video_scan(folder_type)
    if events is None:
        events = video_service._scan_video_folder(folder_type)
        _set_cached_video_scan(folder_type, events, {})

    # ═══ RecentClips mvhd 时钟修正 ═══
    # 从 MP4 的 mvhd atom 读取 GPS 校准的录制时间，修正 Tesla 车载时钟偏差
    if folder_type == 'RecentClips':
        _apply_mvhd_clock_correction(events, folder_type)

    return jsonify({
        'success': True,
        'folder': folder_type,
        'events': events,
        'total': len(events)
    })


def _apply_mvhd_clock_correction(events, folder_type):
    """对 RecentClips 事件应用 mvhd 时钟修正。

    从每个事件的 front.mp4 读取 mvhd atom 中的 GPS 校准 UTC 时间，
    如果与文件名中的时间偏差超过 2 分钟，标注为已修正并显示真实时间。
    """
    if folder_type != 'RecentClips' or not events:
        return
    import os
    base = '/mnt/teslacam/TeslaCam/RecentClips'
    for e in events:
        eid = e.get('id', '')
        if not eid:
            continue
        try:
            front_mp4 = os.path.join(base, f'{eid}-front.mp4')
            if not os.path.isfile(front_mp4):
                continue
            from utils.mvhd_timestamp import extract_mvhd_timestamp
            real_time = extract_mvhd_timestamp(front_mp4)
            if real_time is None:
                continue
            # 解析文件名时间
            from datetime import datetime
            try:
                file_time = datetime.strptime(eid, '%Y-%m-%d_%H-%M-%S')
            except ValueError:
                continue
            # 偏差 < 120 秒 → 文件名时间可信
            if abs((real_time - file_time).total_seconds()) < 120:
                continue
            # 时钟偏差 → 修正显示名
            corrected = real_time.strftime('%Y-%m-%d %H:%M:%S')
            e['name'] = f"{eid.replace('_', ' ')} ⚡→ {corrected}"
            e['clock_corrected'] = True
        except Exception:
            pass


def _detect_camera_angle(filename: str) -> str:
    """从文件名检测摄像头角度，用于播放链接定向"""
    name_lower = filename.lower()
    for angle in ('front', 'back', 'left_repeater', 'right_repeater'):
        if angle in name_lower:
            return angle
    return ''


def _check_thumbnail_fresh(folder_type: str, event_id: str) -> bool:
    """检查缩略图是否存在且是最新的（缩略图 mtime >= 任何对应视频的 mtime）"""
    import os
    prefix = video_service._THUMB_PREFIX.get(folder_type, 'UNK_')
    tn_path = f'/opt/radxa_data/teslausb/static/thumbnails/{prefix}{event_id}_grid.jpg'
    # 兼容旧格式
    old_path = f'/opt/radxa_data/teslausb/static/thumbnails/{event_id}_grid.jpg'
    if os.path.exists(tn_path):
        actual_tn = tn_path
    elif os.path.exists(old_path):
        actual_tn = old_path
    else:
        return False

    # 获取缩略图 mtime
    try:
        tn_mtime = os.path.getmtime(actual_tn)
    except OSError:
        return False

    # 检查视频文件 mtime
    info = video_service.VIDEO_FOLDERS.get(folder_type)
    if not info:
        return True  # 未知文件夹类型，保守返回 True

    folder_path = info['path']
    if not os.path.isdir(folder_path):
        return True  # 文件夹不存在（可能未挂载），保守返回 True

    try:
        if folder_type == 'RecentClips':
            # 平铺结构，找前缀匹配的文件
            max_vid_mtime = 0
            for fname in os.listdir(folder_path):
                if fname.startswith(event_id) and fname.lower().endswith('.mp4'):
                    try:
                        mtime = os.path.getmtime(os.path.join(folder_path, fname))
                        if mtime > max_vid_mtime:
                            max_vid_mtime = mtime
                    except OSError:
                        pass
            if max_vid_mtime == 0:
                return True  # 视频已被旋转删除，缩略图是唯一的记录
            return tn_mtime >= max_vid_mtime
        else:
            # 文件夹结构
            event_path = os.path.join(folder_path, event_id)
            if not os.path.isdir(event_path):
                return True
            max_vid_mtime = 0
            for fname in os.listdir(event_path):
                if fname.lower().endswith('.mp4'):
                    try:
                        mtime = os.path.getmtime(os.path.join(event_path, fname))
                        if mtime > max_vid_mtime:
                            max_vid_mtime = mtime
                    except OSError:
                        pass
            if max_vid_mtime == 0:
                return True
            return tn_mtime >= max_vid_mtime
    except OSError:
        return True  # 出错时保守返回 True


@video_bp.route('/videos/event/<folder_type>/<event_id>')
def video_event_detail(folder_type, event_id):
    """事件详情页 —— 显示事件中的所有视频文件，含 MP4 校验"""
    if folder_type not in video_service.VIDEO_FOLDERS:
        return "无效的文件夹类型", 404

    folder_path = video_service.VIDEO_FOLDERS[folder_type]['path']

    videos = []
    valid_count = 0
    if folder_type == 'RecentClips':
        if os.path.exists(folder_path):
            for fname in sorted(os.listdir(folder_path)):
                if fname.startswith(event_id) and fname.lower().endswith('.mp4'):
                    fpath = os.path.join(folder_path, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                    except OSError:
                        fsize = 0
                    valid = video_service.is_valid_mp4(fpath)
                    if valid:
                        valid_count += 1
                    videos.append({
                        'name': fname,
                        'size': fsize,
                        'size_fmt': video_service._format_size(fsize),
                        'valid': valid,
                        'path': f'/videos/play/{folder_type}/{fname}',
                        'download_path': f'/videos/download/{folder_type}/{fname}',
                    })
    else:
        event_path = os.path.join(folder_path, event_id)
        if os.path.exists(event_path) and os.path.isdir(event_path):
            for fname in sorted(os.listdir(event_path)):
                if fname.lower().endswith('.mp4'):
                    fpath = os.path.join(event_path, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                    except OSError:
                        fsize = 0
                    valid = video_service.is_valid_mp4(fpath)
                    if valid:
                        valid_count += 1
                    videos.append({
                        'name': fname,
                        'size': fsize,
                        'size_fmt': video_service._format_size(fsize),
                        'valid': valid,
                        'path': f'/videos/play/{folder_type}/{event_id}/{fname}',
                        'download_path': f'/videos/download/{folder_type}/{event_id}/{fname}',
                    })

    return render_template(
        'videos.html',
        folders=video_service.VIDEO_FOLDERS,
        current_folder=folder_type,
        events=[],
        event_detail={
            'id': event_id,
            'name': video_service._to_local_time(event_id.replace('_', ' ')),
            'videos': videos,
            'folder': folder_type,
            'valid_count': valid_count,
            'encrypted_count': len(videos) - valid_count,
        },
        total_events=0,
        uploaded_count=0,
        total_size='',
        format_size=video_service._format_size,
        detect_camera_angle=_detect_camera_angle
    )


@video_bp.route('/videos/play/<folder_type>/<path:file_path>')
def video_play(folder_type, file_path):
    """视频播放 —— 使用 Flask send_file 支持 Range 请求，标注加密文件"""
    folder_config = video_service.VIDEO_FOLDERS.get(folder_type)
    if not folder_config:
        return "无效的文件夹类型", 404

    # 安全检查
    if '..' in file_path:
        return "无效的文件路径", 400

    base_path = folder_config['path']
    full_path = os.path.join(base_path, file_path)

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        return "文件不存在", 404

    # 检查是否被 Tesla 加密
    is_valid = video_service.is_valid_mp4(full_path)
    if not is_valid:
        return render_template(
            'videos.html',
            folders=video_service.VIDEO_FOLDERS,
            current_folder=folder_type,
            events=[],
            event_detail=None,
            total_events=0, uploaded_count=0, total_size='',
            format_size=video_service._format_size,
            encryption_warning=os.path.basename(file_path),
        )

    from flask import send_file
    return send_file(full_path, mimetype='video/mp4')


# ─────────────────────────────────────────────
# Event Player 路由（TeslaUSB-main 移植）
# ─────────────────────────────────────────────


@video_bp.route('/videos/player/<folder_type>/<event_id>')
def event_player(folder_type, event_id):
    """全屏沉浸式事件播放器 —— 多摄像头切换 + 片段导航"""
    event = video_service.get_event_cameras(folder_type, event_id)
    if not event:
        return "事件不存在或无视频", 404

    return render_template(
        'event_player.html',
        folder=folder_type,
        folder_structure='flat' if folder_type == 'RecentClips' else 'events',
        event=event,
        video_service=video_service,
    )



@video_bp.route('/videos/stream/<path:filepath>')
def stream_video(filepath):
    """HTTP Range/206 视频流媒体 —— 支持浏览器原生 seek 拖动"""
    from flask import Response

    # 安全检查并构建路径
    parts = filepath.split('/')
    sanitized = [os.path.basename(p) for p in parts]
    if not sanitized:
        return "无效路径", 400

    # 确定视频所在文件夹
    if sanitized[0] in video_service.VIDEO_FOLDERS:
        folder = sanitized[0]
        sub_path = '/'.join(sanitized[1:]) if len(sanitized) > 1 else ''
        base = video_service.VIDEO_FOLDERS[folder]['path']
        video_path = os.path.join(base, sub_path) if sub_path else None
        if not video_path or not os.path.isfile(video_path):
            # 尝试直接拼接
            video_path = os.path.join(base, *sanitized[1:])
    else:
        return "未知文件夹", 404

    if not os.path.isfile(video_path):
        return "文件不存在", 404

    file_size = os.path.getsize(video_path)
    range_header = request.headers.get('Range')
    if not range_header:
        response = send_file(video_path, mimetype='video/mp4')
        response.headers['Accept-Ranges'] = 'bytes'
        return response

    # 解析 Range: bytes=start-end
    try:
        units, rng = range_header.strip().split('=')
        if units != 'bytes':
            raise ValueError
        start_str, end_str = rng.split('-')
        if start_str == '':
            suffix = int(end_str)
            start = max(file_size - suffix, 0)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        if start < 0 or end < start or end >= file_size:
            raise ValueError
    except (ValueError, IndexError):
        return Response(status=416)

    length = end - start + 1

    def _iter_range(path, s, e, chunk=256 * 1024):
        with open(path, 'rb') as f:
            f.seek(s)
            left = e - s + 1
            while left > 0:
                data = f.read(min(chunk, left))
                if not data:
                    break
                left -= len(data)
                yield data

    resp = Response(
        _iter_range(video_path, start, end),
        status=206,
        mimetype='video/mp4',
        direct_passthrough=True,
    )
    resp.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Length'] = str(length)
    return resp



@video_bp.route('/videos/sei/<path:filepath>')
def fetch_video_for_sei(filepath):
    """完整下载视频文件用于客户端 SEI 解析（不支持 Range）"""
    parts = filepath.split('/')
    sanitized = [os.path.basename(p) for p in parts]
    if not sanitized or sanitized[0] not in video_service.VIDEO_FOLDERS:
        return "无效路径", 404

    folder = sanitized[0]
    sub_path = '/'.join(sanitized[1:]) if len(sanitized) > 1 else ''
    base = video_service.VIDEO_FOLDERS[folder]['path']
    video_path = os.path.join(base, sub_path) if sub_path else os.path.join(base, *sanitized[1:])

    if not os.path.isfile(video_path):
        return "文件不存在", 404

    response = send_file(video_path, mimetype='video/mp4', conditional=False)
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response



@video_bp.route('/videos/download_event/<folder_type>/<event_id>')
def download_event_zip(folder_type, event_id):
    """打包整个事件的所有摄像头视频为 ZIP 下载"""
    zip_data, error = video_service.create_event_zip(folder_type, event_id)
    if error:
        return error, 400
    from flask import send_file
    import io
    return send_file(
        io.BytesIO(zip_data),
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{event_id}.zip',
    )



@video_bp.route('/api/sei/<folder_type>/<event_id>/<camera>')
def api_sei_telemetry(folder_type, event_id, camera):
    """
    服务端 SEI 遥测提取 API —— 直接扫描 NAL 单元（和 dashcam-mp4.js 一致）。
    """
    import struct

    # camera 映射
    valid = {'front', 'back', 'left', 'right', 'left_repeater', 'right_repeater'}
    if camera not in valid:
        return jsonify({'success': False, 'error': f'Invalid camera: {camera}'}), 400
    cam = camera.replace('_repeater', '')

    # Find video file — supports both event folders and flat RecentClips
    base = '/mnt/teslacam/TeslaCam'
    event_dir = os.path.join(base, folder_type, event_id)
    video_path = None
    suffixes = {
        'front': ['-front.mp4', '_front.mp4'],
        'back': ['-back.mp4', '_back.mp4'],
        'left': ['-left_repeater.mp4', '_left_repeater.mp4', '-left.mp4'],
        'right': ['-right_repeater.mp4', '_right_repeater.mp4', '-right.mp4'],
    }.get(cam, [f'-{cam}.mp4'])

    if os.path.isdir(event_dir):
        # Event folder structure (SentryClips/SavedClips)
        for sfx in suffixes:
            for fname in sorted(os.listdir(event_dir)):
                if fname.endswith(sfx):
                    video_path = os.path.join(event_dir, fname)
                    break
            if video_path:
                break
    else:
        # Flat structure (RecentClips) — search by file prefix
        parent = os.path.join(base, folder_type)
        if os.path.isdir(parent):
            prefix = event_id.replace('_', '-')  # normalize
            for sfx in suffixes:
                for fname in sorted(os.listdir(parent)):
                    if fname.startswith(event_id) and fname.endswith(sfx):
                        video_path = os.path.join(parent, fname)
                        break
                if video_path:
                    break

    if not video_path or not os.path.isfile(video_path):
        return jsonify({'success': False, 'error': f'Video not found'}), 404

    try:
        data = _extract_sei_direct(video_path)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    if data is None:
        return jsonify({'success': False, 'error': 'No SEI data'}), 404

    return jsonify({'success': True, 'camera': camera, 'frames': data})



@video_bp.route('/videos/download/<folder_type>/<path:file_path>')
def video_download(folder_type, file_path):
    """强制下载视频文件（Content-Disposition: attachment）"""
    folder_config = video_service.VIDEO_FOLDERS.get(folder_type)
    if not folder_config:
        return "无效的文件夹类型", 404

    # 安全检查
    if '..' in file_path:
        return "无效的文件路径", 400

    base_path = folder_config['path']
    full_path = os.path.join(base_path, file_path)

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        return "文件不存在", 404

    # 验证是有效 MP4 文件
    if not video_service.is_valid_mp4(full_path):
        return "文件已被 Tesla 加密，无法下载", 400

    from flask import send_file
    return send_file(
        full_path,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=os.path.basename(file_path),
    )



@video_bp.route('/api/videos/zip', methods=['POST'])
def api_videos_zip():
    """API: 打包事件为 ZIP 下载"""
    data = request.get_json() or {}
    folder_type = data.get('folder', '').strip()
    event_id = data.get('event_id', '').strip()

    if not folder_type or folder_type not in video_service.VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': '无效的文件夹类型'}), 400
    if not event_id:
        return jsonify({'success': False, 'error': '缺少事件ID'}), 400
    if '..' in event_id or '/' in event_id:
        return jsonify({'success': False, 'error': '无效的事件ID'}), 400

    zip_data, filename = video_service.create_event_zip(folder_type, event_id)
    if zip_data is None:
        return jsonify({'success': False, 'error': '没有有效视频文件可打包'}), 404

    from flask import send_file
    import io
    return send_file(
        io.BytesIO(zip_data),
        mimetype='application/zip',
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────
# 单镜头下载 / 裁剪（Task A: 播放页增强）
# ─────────────────────────────────────────────

_VALID_CAMERAS = {'front', 'back', 'left', 'right', 'left_repeater', 'right_repeater'}


def _resolve_camera_path(folder_type, event_id, camera):
    """按摄像头角度解析视频物理路径；不存在/加密/非法返回 (path_or_None, error_str)。"""
    event = video_service.get_event_cameras(folder_type, event_id)
    if not event:
        return None, '事件不存在或无视频'
    if camera not in _VALID_CAMERAS:
        return None, f'无效的摄像头: {camera}'
    fname = event.get('camera_videos', {}).get(camera)
    if not fname:
        return None, f'该事件没有 {camera} 摄像头视频'

    folder_config = video_service.VIDEO_FOLDERS.get(folder_type)
    if not folder_config:
        return None, '无效的文件夹类型'
    folder_path = folder_config['path']

    # RecentClips 为平铺结构，其余为 event/<event_id>/ 子目录
    if folder_type == 'RecentClips':
        full_path = os.path.join(folder_path, fname)
    else:
        full_path = os.path.join(folder_path, event_id, fname)

    if not os.path.isfile(full_path):
        return None, '视频文件不存在'
    if not video_service.is_valid_mp4(full_path):
        return None, '文件已被 Tesla 加密，无法处理'
    return full_path, None


@video_bp.route('/videos/download_clip/<folder_type>/<event_id>')
def video_download_clip(folder_type, event_id):
    """下载单个摄像头视频文件（单独影片下载）"""
    camera = (request.args.get('camera') or '').strip()
    full_path, err = _resolve_camera_path(folder_type, event_id, camera)
    if err:
        return err, 400 if '无效' in err or '加密' in err else 404
    return send_file(
        full_path,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=os.path.basename(full_path),
    )


@video_bp.route('/videos/trim/<folder_type>/<event_id>', methods=['POST'])
def video_trim_clip(folder_type, event_id):
    """裁剪指定摄像头视频片段并流式下载（ffmpeg 流拷贝，免重编码）"""
    data = request.get_json(silent=True) or {}
    camera = (data.get('camera') or '').strip()
    start_str = str(data.get('start', ''))
    end_str = str(data.get('end', ''))

    full_path, err = _resolve_camera_path(folder_type, event_id, camera)
    if err:
        return jsonify({'success': False, 'error': err}), (
            400 if ('无效' in err or '加密' in err) else 404)

    duration = video_trim.probe_duration(full_path)
    if duration <= 0:
        return jsonify({'success': False, 'error': '无法探测视频时长，无法裁剪'}), 400

    ok, start, end, verr = video_trim.validate_trim_range(start_str, end_str, duration)
    if not ok:
        return jsonify({'success': False, 'error': verr}), 400

    # 输出临时文件，请求结束后清理
    fd, out_path = tempfile.mkstemp(suffix='.mp4', prefix=f'trim_{camera}_')
    os.close(fd)
    cmd = video_trim.build_trim_command(full_path, out_path, start, end - start)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        if os.path.exists(out_path):
            os.remove(out_path)
        err_msg = '裁剪超时（>180s）' if isinstance(e, subprocess.TimeoutExpired) else '裁剪执行失败'
        return jsonify({'success': False, 'error': err_msg}), 500

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        if os.path.exists(out_path):
            os.remove(out_path)
        detail = (proc.stderr or '')[-600:] if proc.returncode != 0 else '输出为空'
        return jsonify({'success': False, 'error': 'ffmpeg 裁剪失败', 'detail': detail}), 500

    # 读入内存后删除临时文件：避免 after_this_request 在 send_file 惰性流式
    # 读取前就删掉文件，导致返回 0 字节。
    try:
        with open(out_path, 'rb') as f:
            clip_data = f.read()
    except OSError:
        if os.path.exists(out_path):
            os.remove(out_path)
        return jsonify({'success': False, 'error': '读取裁剪结果失败'}), 500
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)

    base = os.path.basename(full_path)
    name, _ = os.path.splitext(base)
    download_name = f'{name}__{start:.0f}-{end:.0f}s.mp4'
    return send_file(
        io.BytesIO(clip_data),
        mimetype='video/mp4',
        as_attachment=True,
        download_name=download_name,
    )


# ═══════════════════════════════════════════════════════════════
# Task 3.2 收尾 — 13 个缺失 API 端点
# ═══════════════════════════════════════════════════════════════

# ── 日志流 (SSE) ──────────────────────────────────────────────

# 全局日志订阅者管理
# log_subscribers 已迁移至 app_state.py
# log_subscribers lock 已在 app_state.py 中创建


@video_bp.route('/api/player/video')
def api_player_video():
    """通过 HTTP serve TeslaCam 视频文件（TDashcam HTTP 模式）"""
    path = request.args.get('path', '')
    if not path or '..' in path:
        return 'Invalid path', 400
    full_path = path if path.startswith('/') else os.path.join('/mnt/teslacam/TeslaCam', path)
    if not os.path.exists(full_path):
        return 'File not found', 404
    return send_file(full_path, mimetype='video/mp4' if full_path.endswith('.mp4') else None)


@video_bp.route('/api/player/events')
def api_player_events():
    """返回 TeslaCam 事件列表（SentryClips/SavedClips/RecentClips）

    SentryClips/SavedClips: 事件文件夹结构，每个子目录是一个事件
    RecentClips: 平铺文件结构，按时间戳前缀分组
    """
    folder = request.args.get('folder', 'SentryClips')
    limit = min(int(request.args.get('limit', 200)), 500)

    base = os.path.join('/mnt/teslacam/TeslaCam', folder)
    if not os.path.isdir(base):
        return jsonify({'success': False, 'error': f'Folder not found: {folder}'})

    events = []
    is_flat = (folder == 'RecentClips')

    try:
        if is_flat:
            # ═══ RecentClips: 平铺文件结构，按时间戳前缀分组 ═══
            sessions = {}  # prefix -> video_files list
            for fname in sorted(os.listdir(base)):
                if not fname.lower().endswith('.mp4'):
                    continue
                fpath = os.path.join(base, fname)
                if not os.path.isfile(fpath):
                    continue
                # 提取时间戳前缀: 2026-05-17_13-09-36-back.mp4 → 2026-05-17_13-09-36
                # 用正则匹配已知摄像头后缀，避免误匹配时间戳中的 -left/-right
                import re as _re2
                match = _re2.match(
                    r'^(.+?)-(front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4$',
                    fname, _re2.IGNORECASE
                )
                if not match:
                    continue
                prefix = match.group(1)

                try:
                    sz = os.path.getsize(fpath)
                except:
                    sz = 0

                if prefix not in sessions:
                    sessions[prefix] = {'files': [], 'total_size': 0}
                sessions[prefix]['files'].append({'name': fname, 'size': sz})
                sessions[prefix]['total_size'] += sz

            # 按时间倒序，限制数量
            sorted_prefixes = sorted(sessions.keys(), reverse=True)[:limit]
            for prefix in sorted_prefixes:
                sess = sessions[prefix]
                # ═══ mvhd 时钟修正 ═══
                # Tesla 车载时钟可能不准，文件名时间戳不等于真实录制时间。
                # 从 front.mp4 的 mvhd atom 读取 GPS 校准的 UTC 录制时间。
                # id 保持原始前缀（用于文件匹配），显示名标注修正后的时间。
                display_name = prefix
                clock_corrected = False
                try:
                    real_time = get_real_event_time(prefix, base)
                    if real_time is not None:
                        corrected_str = real_time.strftime('%Y-%m-%d %H:%M:%S')
                        display_name = f"{prefix} ⚡→ {corrected_str}"
                        clock_corrected = True
                except Exception:
                    pass

                has_thumbnail = _check_thumbnail_fresh('RecentClips', prefix)
                events.append({
                    'id': prefix,
                    'name': display_name,
                    'file_count': len(sess['files']),
                    'total_size': sess['total_size'],
                    'video_files': sess['files'],
                    'thumbnail': f'/thumbnails/REC_{prefix}_grid.jpg' if has_thumbnail else None,
                    'clock_corrected': clock_corrected,
                })
        else:
            # ═══ SentryClips / SavedClips: 事件文件夹结构 ═══
            entries = sorted(os.listdir(base), reverse=True)[:limit]
            for eid in entries:
                epath = os.path.join(base, eid)
                if not os.path.isdir(epath):
                    continue
                # 统计文件数和大小 + 列出视频文件名
                file_count = 0
                total_size = 0
                video_files = []
                has_thumbnail = _check_thumbnail_fresh(folder, eid)
                try:
                    for f in sorted(os.listdir(epath)):
                        if f.lower().endswith('.mp4'):
                            fp = os.path.join(epath, f)
                            sz = os.path.getsize(fp)
                            file_count += 1
                            total_size += sz
                            video_files.append({'name': f, 'size': sz})
                except:
                    pass
                if file_count == 0:
                    continue  # 跳过空事件文件夹
                events.append({
                    'id': eid, 'name': eid,
                    'file_count': file_count,
                    'total_size': total_size,
                    'video_files': video_files,
                    'thumbnail': f'/thumbnails/{video_service._THUMB_PREFIX.get(folder, "SEN_")}{eid}_grid.jpg' if has_thumbnail else None
                })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': True, 'events': events, 'folder': folder})

# ═══════════════════════════════════════════════════════════════
# TDashcam Studio 播放器 (静态文件 serve)
# ═══════════════════════════════════════════════════════════════

