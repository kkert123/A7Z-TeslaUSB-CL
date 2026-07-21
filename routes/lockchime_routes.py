# routes/lockchime_routes.py
from flask import Blueprint, render_template, request, jsonify, send_file, current_app
from app_state import state
import os
import json
import time
import shutil
from datetime import datetime

# Must import get_template_context from misc_routes at registration time
# to avoid circular imports. We'll use a lazy import pattern.
_get_template_context = None

lockchime_bp = Blueprint('lockchime', __name__, url_prefix='')

# ─────────────────────────────────────────────
# Lock Chime API - A7Z 移植版 (路径修正: lightshow 分区)
# ─────────────────────────────────────────────

LOCKCHIME_DIR = "/mnt/lightshow/Chimes"
LOCKCHIME_ACTIVE = "/mnt/lightshow/LockChime.wav"

HOLIDAYS = [
    ((12, 20), (12, 31), "xmas.wav", "🎄 圣诞"),
    ((10, 24), (10, 31), "halloween.wav", "🎃 万圣节"),
    ((1, 1),   (1, 3),   "newyear.wav", "🎆 新年"),
]

import staging_service

def _staging_upload_local(partition, file_obj, filename):
    """Present 模式: 暂存文件到 staging 目录"""
    data = file_obj.read()
    file_obj.seek(0)
    return staging_service.add_upload(partition, filename, data)

def _staging_delete_local(partition, filename):
    """Present 模式: 标记待删除"""
    return staging_service.add_delete(partition, filename)

def _is_present_mode_local():
    return staging_service.is_present()

def _format_size(size_bytes):
    """格式化字节大小"""
    if not size_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"

def _get_active_chime():
    if os.path.exists(LOCKCHIME_ACTIVE):
        stat = os.stat(LOCKCHIME_ACTIVE)
        return {'active': True, 'filename': 'LockChime.wav',
                'size': _format_size(stat.st_size),
                'modified': datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")}
    return {'active': False}

def _get_holiday_chime():
    now = datetime.now()
    for (s_m, s_d), (e_m, e_d), filename, label in HOLIDAYS:
        start = datetime(now.year, s_m, s_d)
        end = datetime(now.year, e_m, e_d)
        if start <= now <= end:
            fp = os.path.join(LOCKCHIME_DIR, filename)
            return {'in_holiday': True, 'label': label, 'filename': filename,
                    'path': fp, 'exists': os.path.exists(fp)}
    return {'in_holiday': False}

def _list_lockchimes():
    files = []
    if os.path.exists(LOCKCHIME_DIR):
        for fn in sorted(os.listdir(LOCKCHIME_DIR)):
            if fn.lower().endswith('.wav'):
                fp = os.path.join(LOCKCHIME_DIR, fn)
                stat = os.stat(fp)
                is_active = False
                if os.path.exists(LOCKCHIME_ACTIVE):
                    try:
                        is_active = (os.path.samefile(fp, LOCKCHIME_ACTIVE) or
                                     os.path.getsize(fp) == os.path.getsize(LOCKCHIME_ACTIVE) and
                                     os.path.getmtime(fp) == os.path.getmtime(LOCKCHIME_ACTIVE))
                    except:
                        is_active = (fn == 'LockChime.wav')
                files.append({'filename': fn, 'size': _format_size(stat.st_size),
                              'modified': datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                              'active': is_active})
    return files


@lockchime_bp.route('/lockchime')
def lockchime_page():
    # Lazy import to avoid circular dependency
    from routes.misc_routes import get_template_context
    ctx = get_template_context()
    ctx['current_chime'] = _get_active_chime()
    ctx['holiday_chime'] = _get_holiday_chime()
    return render_template('lockchime.html', **ctx)


@lockchime_bp.route('/api/lockchime/list')
def lockchime_list():
    try:
        files = _list_lockchimes()
        return jsonify({
            'success': True,
            'files': files,
            'active': _get_active_chime(),
            'holiday': _get_holiday_chime(),
            'holidays': [{'label': h[3], 'filename': h[2],
                          'start': f"{h[0][0]:02d}-{h[0][1]:02d}",
                          'end': f"{h[1][0]:02d}-{h[1][1]:02d}"} for h in HOLIDAYS]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lockchime_bp.route('/api/lockchime/upload', methods=['POST'])
def lockchime_upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': '文件名为空'}), 400
    if not file.filename.lower().endswith('.wav'):
        return jsonify({'success': False, 'error': f'仅支持 WAV 格式'}), 400
    try:
        if _is_present_mode_local():
            r = _staging_upload_local('lockchime', file, file.filename)
            return jsonify(r)
        os.makedirs(LOCKCHIME_DIR, exist_ok=True)
        save_path = os.path.join(LOCKCHIME_DIR, file.filename)
        file.save(save_path)
        current_app.logger.info(f"🔔 [LockChime] 上传: {file.filename}")
        return jsonify({'success': True, 'filename': file.filename,
                        'message': f'{file.filename} 已上传'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lockchime_bp.route('/api/lockchime/delete', methods=['POST'])
def lockchime_delete():
    data = request.get_json() or {}
    filename = data.get('filename', '')
    if not filename or '..' in filename or '/' in filename:
        return jsonify({'success': False, 'error': '无效的文件名'}), 400
    try:
        if _is_present_mode_local():
            r = _staging_delete_local('lockchime', filename)
            return jsonify(r)
        fp = os.path.join(LOCKCHIME_DIR, filename)
        if not os.path.exists(fp):
            return jsonify({'success': False, 'error': '文件不存在'}), 404
        if os.path.exists(LOCKCHIME_ACTIVE):
            try:
                if os.path.samefile(fp, LOCKCHIME_ACTIVE):
                    os.remove(LOCKCHIME_ACTIVE)
            except:
                pass
        os.remove(fp)
        current_app.logger.info(f"🔔 [LockChime] 删除: {filename}")
        return jsonify({'success': True, 'message': f'{filename} 已删除'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lockchime_bp.route('/api/lockchime/activate', methods=['POST'])
def lockchime_activate():
    data = request.get_json() or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'success': False, 'error': '请指定文件'}), 400
    src = os.path.join(LOCKCHIME_DIR, filename)
    if not os.path.exists(src):
        return jsonify({'success': False, 'error': '文件不存在'}), 404
    try:
        # 如果 LockChime.wav 是指向其他文件的软链接，先删除
        if os.path.islink(LOCKCHIME_ACTIVE):
            os.unlink(LOCKCHIME_ACTIVE)
        elif os.path.exists(LOCKCHIME_ACTIVE):
            os.remove(LOCKCHIME_ACTIVE)
        shutil.copy2(src, LOCKCHIME_ACTIVE)
        current_app.logger.info(f"🔔 [LockChime] 激活: {filename} → LockChime.wav")
        return jsonify({'success': True, 'filename': filename,
                        'message': f'已激活 {filename} 为 Lock Chime'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@lockchime_bp.route('/api/lockchime/holiday-apply', methods=['POST'])
def lockchime_holiday_apply():
    """手动应用节日音效"""
    holiday = _get_holiday_chime()
    if not holiday['in_holiday']:
        return jsonify({'success': False, 'error': '当前不在节日范围内'}), 400
    if not holiday['exists']:
        return jsonify({'success': False, 'error': f"节日音效 {holiday['filename']} 不存在，请先上传"}), 404
    try:
        src = holiday['path']
        if os.path.islink(LOCKCHIME_ACTIVE):
            os.unlink(LOCKCHIME_ACTIVE)
        elif os.path.exists(LOCKCHIME_ACTIVE):
            os.remove(LOCKCHIME_ACTIVE)
        shutil.copy2(src, LOCKCHIME_ACTIVE)
        current_app.logger.info(f"🔔 [LockChime] 节日切换: {holiday['label']} {holiday['filename']}")
        return jsonify({'success': True,
                        'message': f"已切换为 {holiday['label']} 音效"})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@lockchime_bp.route('/api/lockchime/preview/<filename>')
def lockchime_preview(filename):
    """试听锁车音 WAV 文件"""
    safe = os.path.basename(filename)
    fp = os.path.join(LOCKCHIME_DIR, safe)
    if not os.path.isfile(fp): return '', 404
    return send_file(fp, mimetype='audio/wav')
