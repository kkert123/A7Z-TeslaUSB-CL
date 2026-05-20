#!/usr/bin/env python3
"""
Tesla 风格视频播放器 API 路由模块。

通过 Flask Blueprint 加载，不修改 app.py。
部署: 将此文件放到 /opt/radxa_data/teslausb/ 并在 app.py 末尾添加:
    from player_routes import register_player_routes
    register_player_routes(app)
"""
import os
import json
import subprocess
import threading
import uuid
import logging
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, send_file

try:
    import sei_service
except ImportError:
    sei_service = None
    logging.warning("sei_service not available")

logger = logging.getLogger('player_routes')

player_bp = Blueprint('player', __name__)


@player_bp.before_request
def _check_player_auth():
    """检查 player API 认证（与 app.py require_auth 逻辑一致）"""
    try:
        import json as _json
        config_file = '/opt/radxa_data/teslausb/config.json'
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                config = _json.load(f)
            if config.get('auth_enabled', False):
                from flask import session as _session, redirect, url_for
                if 'user' not in _session:
                    if request.path.startswith('/api/'):
                        return jsonify({'success': False, 'error': '需要登录'}), 401
                    return redirect(url_for('login', next=request.path))
    except Exception:
        pass  # 配置读取失败时不阻止访问


# Configuration
VIDEO_FOLDERS = {
    'SentryClips':    {'path': '/mnt/teslacam/TeslaCam/SentryClips',    'icon': '🚨', 'desc': '哨兵事件'},
    'SavedClips':     {'path': '/mnt/teslacam/TeslaCam/SavedClips',     'icon': '⭐', 'desc': '手动保存'},
    'RecentClips':    {'path': '/mnt/teslacam/TeslaCam/RecentClips',    'icon': '🚗', 'desc': '行车记录仪'},
}

PLAYER_DATA_DIR = '/opt/radxa_data/teslausb/data'
PLAYER_SCREENSHOT_DIR = os.path.join(PLAYER_DATA_DIR, 'screenshots')
PLAYER_CLIP_DIR = os.path.join(PLAYER_DATA_DIR, 'clips')
PLAYER_TASKS = {}
PLAYER_TASKS_LOCK = threading.Lock()

# Camera suffix mapping
CAMERA_SUFFIXES = {
    'front': ['-front.mp4', '_front.mp4'],
    'back': ['-back.mp4', '_back.mp4'],
    'left': ['-left_repeater.mp4', '_left_repeater.mp4', '-left.mp4'],
    'right': ['-right_repeater.mp4', '_right_repeater.mp4', '-right.mp4'],
}

THUMBNAIL_DIR = '/opt/radxa_data/teslausb/static/thumbnails'


def _find_video(folder, event_id, camera):
    """Find video file path for a camera."""
    base = VIDEO_FOLDERS[folder]['path']
    event_dir = os.path.join(base, event_id)
    event_parent = base

    suffixes = CAMERA_SUFFIXES.get(camera, [f'-{camera}.mp4'])

    if os.path.isdir(event_dir):
        for sfx in suffixes:
            for fname in sorted(os.listdir(event_dir)):
                if fname.lower().endswith(sfx.lower()):
                    return os.path.join(event_dir, fname)
        return None

    if os.path.isdir(event_parent):
        for sfx in suffixes:
            for fname in sorted(os.listdir(event_parent)):
                if fname.startswith(event_id) and (
                    fname.lower().endswith(sfx.lower())
                ):
                    return os.path.join(event_parent, fname)
        return None

    return None


def _find_cameras(folder, event_id):
    """List available cameras for an event."""
    available = []
    for cam in ['front', 'back', 'left', 'right']:
        if _find_video(folder, event_id, cam):
            available.append(cam)
    return available


def _format_size(size_bytes):
    """Format file size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ═══════════════════════════════════════════════════════════
# Page Route
# ═══════════════════════════════════════════════════════════

@player_bp.route('/player')
def player_page():
    """Tesla style video player entry."""
    folder = request.args.get('folder', 'SentryClips')
    event_id = request.args.get('event', '')
    return render_template('player.html',
                          initial_folder=folder,
                          initial_event=event_id,
                          folders=VIDEO_FOLDERS)


# ═══════════════════════════════════════════════════════════
# API Routes
# ═══════════════════════════════════════════════════════════

@player_bp.route('/api/player/events')
def api_player_events():
    """List events in a folder."""
    folder = request.args.get('folder', 'SentryClips')
    if folder not in VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': 'Invalid folder type'}), 400

    folder_path = VIDEO_FOLDERS[folder]['path']
    events = []

    try:
        if not os.path.exists(folder_path):
            return jsonify({'success': True, 'folder': folder, 'events': []})

        items = sorted(os.listdir(folder_path), reverse=True)

        if folder == 'RecentClips':
            seen = set()
            for fname in items:
                if not fname.lower().endswith('.mp4'):
                    continue
                parts = fname.rsplit('-', 1)
                if len(parts) < 2:
                    continue
                eid = parts[0]
                if eid in seen:
                    continue
                seen.add(eid)

                cams = _find_cameras(folder, eid)
                thumb = None
                thumb_path = os.path.join(THUMBNAIL_DIR, f"{eid}_grid_preview.jpg")
                if os.path.exists(thumb_path):
                    thumb = f"/thumbnails/{eid}_grid_preview.jpg"

                try:
                    ts_str = eid.replace('_', ' ')
                    from datetime import datetime as dt
                    ts = dt.strptime(ts_str, '%Y-%m-%d %H-%M-%S')
                    ts_display = ts.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    ts_display = eid[:16]

                events.append({
                    'id': eid, 'name': eid, 'timestamp': ts_display,
                    'camera_count': len(cams), 'cameras': cams,
                    'has_telemetry': False, 'thumbnail': thumb, 'location': None
                })
        else:
            for fname in items:
                event_path = os.path.join(folder_path, fname)
                if not os.path.isdir(event_path):
                    continue
                eid = fname
                cams = _find_cameras(folder, eid)

                thumb = None
                thumb_path = os.path.join(THUMBNAIL_DIR, f"{eid}_grid_preview.jpg")
                if os.path.exists(thumb_path):
                    thumb = f"/thumbnails/{eid}_grid_preview.jpg"

                location = None
                try:
                    ej_path = os.path.join(event_path, 'event.json')
                    if os.path.exists(ej_path):
                        with open(ej_path, 'rb') as f:
                            fb = f.read(1)
                        if fb and fb[0] == 0x7b:
                            with open(ej_path, 'r', encoding='utf-8') as f:
                                ej = json.load(f)
                            city = ej.get('city', '').strip()
                            if city:
                                location = city
                except Exception:
                    pass

                try:
                    ts_str = eid.replace('_', '-', 2).replace('_', ' ')
                    from datetime import datetime as dt
                    ts = dt.strptime(ts_str, '%Y-%m-%d-%H-%M-%S')
                    ts_display = ts.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    ts_display = eid[:16]

                events.append({
                    'id': eid, 'name': eid, 'timestamp': ts_display,
                    'camera_count': len(cams), 'cameras': cams,
                    'has_telemetry': False, 'thumbnail': thumb, 'location': location
                })

        return jsonify({'success': True, 'folder': folder, 'events': events})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@player_bp.route('/api/player/event/<folder>/<event_id>')
def api_player_event_detail(folder, event_id):
    """Get event detail with camera list and event.json."""
    if folder not in VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': 'Invalid folder type'}), 400

    try:
        cameras = {}
        for cam in ['front', 'back', 'left', 'right']:
            video_path = _find_video(folder, event_id, cam)
            if video_path:
                size = 0
                try:
                    size = os.path.getsize(video_path)
                except OSError:
                    pass
                base = VIDEO_FOLDERS[folder]['path']
                rel = os.path.relpath(video_path, base)
                play_url = f"/videos/play/{folder}/{rel.replace(os.sep, '/')}"
                cameras[cam] = {
                    'name': os.path.basename(video_path),
                    'size': size,
                    'size_fmt': _format_size(size),
                    'play_url': play_url
                }

        event_info = {}
        folder_path = VIDEO_FOLDERS[folder]['path']
        event_dir = os.path.join(folder_path, event_id)
        if os.path.isdir(event_dir):
            ej_path = os.path.join(event_dir, 'event.json')
            if os.path.exists(ej_path):
                try:
                    with open(ej_path, 'rb') as f:
                        fb = f.read(1)
                    if fb and fb[0] == 0x7b:
                        with open(ej_path, 'r', encoding='utf-8') as f:
                            event_info = json.load(f)
                except Exception:
                    pass

        return jsonify({
            'success': True,
            'event_id': event_id, 'folder': folder,
            'cameras': cameras,
            'event_json': event_info,
            'has_telemetry': len(cameras) > 0
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@player_bp.route('/api/player/telemetry/<folder>/<event_id>/<camera>')
def api_player_telemetry(folder, event_id, camera):
    """Get SEI telemetry for a camera."""
    if folder not in VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': 'Invalid folder type'}), 400
    if camera not in ('front', 'back', 'left', 'right'):
        return jsonify({'success': False, 'error': 'Invalid camera'}), 400

    try:
        if sei_service is None:
            return jsonify({'success': True, 'available': False,
                          'camera': camera, 'frames': [],
                          'message': 'SEI service not available'})

        frames = sei_service.get_telemetry(folder, event_id, camera)
        if frames is None:
            return jsonify({'success': True, 'available': False,
                          'camera': camera, 'frames': [],
                          'message': 'No SEI telemetry in this video'})

        return jsonify({'success': True, 'available': True,
                      'camera': camera, 'sample_rate': 5,
                      'frame_count': len(frames), 'frames': frames})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Background tasks
def _run_screenshot(task_id, folder, event_id, camera, time_offset):
    try:
        video_path = _find_video(folder, event_id, camera)
        if not video_path:
            with PLAYER_TASKS_LOCK:
                PLAYER_TASKS[task_id] = {'status': 'error',
                                         'error': f'Video not found: {folder}/{event_id}/{camera}'}
            return

        fname = f"{event_id}_{camera}_{time_offset:.1f}s.jpg"
        output = os.path.join(PLAYER_SCREENSHOT_DIR, fname)
        os.makedirs(PLAYER_SCREENSHOT_DIR, exist_ok=True)

        cmd = ['ffmpeg', '-y', '-ss', str(time_offset), '-i', video_path,
               '-vframes', '1', '-q:v', '5', output]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and os.path.exists(output):
            with PLAYER_TASKS_LOCK:
                PLAYER_TASKS[task_id] = {
                    'status': 'done', 'result_path': output,
                    'result_url': f'/api/player/download/screenshot/{fname}'
                }
        else:
            with PLAYER_TASKS_LOCK:
                PLAYER_TASKS[task_id] = {'status': 'error',
                    'error': f'ffmpeg failed: {proc.stderr[:200]}'}
    except subprocess.TimeoutExpired:
        with PLAYER_TASKS_LOCK:
            PLAYER_TASKS[task_id] = {'status': 'error', 'error': 'Screenshot timeout (60s)'}
    except Exception as e:
        with PLAYER_TASKS_LOCK:
            PLAYER_TASKS[task_id] = {'status': 'error', 'error': str(e)}


def _run_clip(task_id, folder, event_id, camera, key_offset):
    try:
        video_path = _find_video(folder, event_id, camera)
        if not video_path:
            with PLAYER_TASKS_LOCK:
                PLAYER_TASKS[task_id] = {'status': 'error',
                                         'error': f'Video not found: {folder}/{event_id}/{camera}'}
            return

        fname = f"{event_id}_{camera}_{key_offset:.1f}s_clip.mp4"
        output = os.path.join(PLAYER_CLIP_DIR, fname)
        os.makedirs(PLAYER_CLIP_DIR, exist_ok=True)

        cmd = ['ffmpeg', '-y', '-ss', str(max(0, key_offset - 15)), '-i', video_path,
               '-t', '30', '-c', 'copy', output]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(output):
            with PLAYER_TASKS_LOCK:
                PLAYER_TASKS[task_id] = {
                    'status': 'done', 'result_path': output,
                    'result_url': f'/api/player/download/clip/{fname}'
                }
        else:
            with PLAYER_TASKS_LOCK:
                PLAYER_TASKS[task_id] = {'status': 'error',
                    'error': f'ffmpeg failed: {proc.stderr[:200]}'}
    except subprocess.TimeoutExpired:
        with PLAYER_TASKS_LOCK:
            PLAYER_TASKS[task_id] = {'status': 'error', 'error': 'Clip extraction timeout (120s)'}
    except Exception as e:
        with PLAYER_TASKS_LOCK:
            PLAYER_TASKS[task_id] = {'status': 'error', 'error': str(e)}


@player_bp.route('/api/player/screenshot', methods=['POST'])
def api_player_screenshot():
    data = request.get_json(silent=True) or {}
    folder = data.get('folder', 'SentryClips')
    event_id = data.get('event_id', '')
    camera = data.get('camera', 'front')
    time_offset = float(data.get('time_offset', 5.0))

    if not event_id:
        return jsonify({'success': False, 'error': 'Missing event_id'}), 400

    task_id = f"screenshot_{uuid.uuid4().hex[:8]}"
    with PLAYER_TASKS_LOCK:
        PLAYER_TASKS[task_id] = {'status': 'processing'}

    t = threading.Thread(target=_run_screenshot,
                         args=(task_id, folder, event_id, camera, time_offset),
                         daemon=True)
    t.start()

    return jsonify({'success': True, 'task_id': task_id, 'status': 'processing'})


@player_bp.route('/api/player/extract-clip', methods=['POST'])
def api_player_extract_clip():
    data = request.get_json(silent=True) or {}
    folder = data.get('folder', 'SentryClips')
    event_id = data.get('event_id', '')
    camera = data.get('camera', 'front')
    key_offset = float(data.get('key_offset', 15.0))

    if not event_id:
        return jsonify({'success': False, 'error': 'Missing event_id'}), 400

    task_id = f"clip_{uuid.uuid4().hex[:8]}"
    with PLAYER_TASKS_LOCK:
        PLAYER_TASKS[task_id] = {'status': 'processing'}

    t = threading.Thread(target=_run_clip,
                         args=(task_id, folder, event_id, camera, key_offset),
                         daemon=True)
    t.start()

    return jsonify({'success': True, 'task_id': task_id, 'status': 'processing'})


@player_bp.route('/api/player/task/<task_id>')
def api_player_task_status(task_id):
    with PLAYER_TASKS_LOCK:
        task = PLAYER_TASKS.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': 'Task not found'}), 404
    return jsonify({'success': True, 'task_id': task_id, **task})


@player_bp.route('/api/player/download/screenshot/<filename>')
def api_player_download_screenshot(filename):
    fp = os.path.join(PLAYER_SCREENSHOT_DIR, filename)
    if not os.path.exists(fp):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    return send_file(fp, mimetype='image/jpeg',
                     as_attachment=True, download_name=filename)


@player_bp.route('/api/player/download/clip/<filename>')
def api_player_download_clip(filename):
    fp = os.path.join(PLAYER_CLIP_DIR, filename)
    if not os.path.exists(fp):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    return send_file(fp, mimetype='video/mp4',
                     as_attachment=True, download_name=filename)


def register_player_routes(app):
    """Register player blueprint on the Flask app."""
    app.register_blueprint(player_bp)
    logger.info("Player routes registered at /player")
