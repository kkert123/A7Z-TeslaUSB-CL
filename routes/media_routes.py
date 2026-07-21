import os, json, time, subprocess, threading
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, url_for
from app_state import state

from utils.system_info import get_ip_info
import utils.system_info

import media_service
import boombox_service
import lightshow_service
import wrap_service
import license_plate_service
import staging_service
import video_service


media_bp = Blueprint('media', __name__, url_prefix='')

# Late imports from app.py (avoid circular imports at module load)
from utils.app_helpers import get_template_context, _media_disk_info, _is_present_mode, _staging_upload, _staging_delete


@media_bp.route('/upload')
def upload_page():
    """上传进度 — 已整合到 /cloud 页面"""
    from flask import redirect as _redirect
    return _redirect('/cloud')


@media_bp.route('/media')
def media_page():
    """媒体管理页面 - 加载 media_service"""
    # 确保 media_service 已导入
    try:
        import media_service
    except ImportError:
        pass
    return render_template('media.html', **get_template_context())


@media_bp.route('/boombox')
def boombox_page():
    try:
        files = boombox_service.list_boombox_files()
        for f in files:
            f['size_kb'] = round(f['size'] / 1024, 1)
        return render_template('boombox.html', files=files, available=boombox_service.get_available(), **get_template_context())
    except Exception as e:
        return f"<h3>Error</h3><p>{e}</p>", 500


@media_bp.route('/boombox/upload', methods=['POST'])
def boombox_upload():
    try:
        if 'file' not in request.files or request.files['file'].filename == '':
            return redirect('/boombox?error=no_file')
        success, msg = boombox_service.upload_boombox(request.files['file'], request.files['file'].filename)
        return redirect(f'/boombox?{"ok" if success else "error"}={msg[:80]}')
    except Exception as e:
        return redirect(f'/boombox?error={str(e)[:80]}')


@media_bp.route('/boombox/delete/<filename>', methods=['POST'])
def boombox_delete(filename):
    try:
        success, msg = boombox_service.delete_boombox(filename)
        return redirect(f'/boombox?{"ok" if success else "error"}={msg[:80]}')
    except Exception as e:
        return redirect(f'/boombox?error={str(e)[:80]}')


@media_bp.route('/api/boombox/preview/<filename>')
def boombox_preview(filename):
    """[已统一] 重定向到 /api/media/boombox/play/<filename>"""
    return redirect(url_for('api_media_boombox_play', filename=filename))


@media_bp.route('/lightshow')
def lightshow_page():
    try:
        shows = lightshow_service.list_lightshows()
        return render_template('lightshow.html', shows=shows, available=lightshow_service.get_available(), **get_template_context())
    except Exception as e:
        return f"<h3>Error</h3><p>{e}</p>", 500


@media_bp.route('/lightshow/upload', methods=['POST'])
def lightshow_upload():
    try:
        if 'file' not in request.files or request.files['file'].filename == '':
            return redirect('/lightshow?error=no_file')
        success, msg = lightshow_service.upload_lightshow_file(request.files['file'], request.files['file'].filename)
        return redirect(f'/lightshow?{"ok" if success else "error"}={msg[:80]}')
    except Exception as e:
        return redirect(f'/lightshow?error={str(e)[:80]}')


@media_bp.route('/lightshow/upload/zip', methods=['POST'])
def lightshow_upload_zip():
    try:
        if 'zipfile' not in request.files or request.files['zipfile'].filename == '':
            return redirect('/lightshow?error=no_zip')
        success, msg, count = lightshow_service.upload_lightshow_zip(request.files['zipfile'])
        return redirect(f'/lightshow?{"ok" if success else "error"}={msg[:80]}')
    except Exception as e:
        return redirect(f'/lightshow?error={str(e)[:80]}')


@media_bp.route('/lightshow/delete/<basename>', methods=['POST'])
def lightshow_delete(basename):
    try:
        success, msg = lightshow_service.delete_lightshow(basename)
        return redirect(f'/lightshow?{"ok" if success else "error"}={msg[:80]}')
    except Exception as e:
        return redirect(f'/lightshow?error={str(e)[:80]}')


@media_bp.route('/api/lightshow/preview/<filename>')
def lightshow_preview(filename):
    """[已统一] 重定向到 /api/media/lightshow/play/<filename>"""
    return redirect(url_for('api_media_lightshow_play', filename=filename))

# ── 贴膜管理 (Custom Wrap) ────────────────────────────────────


@media_bp.route('/wraps')
def wraps_page():
    """贴膜管理页面"""
    try:
        wraps = wrap_service.list_wrap_files()
        for w in wraps:
            w['size_kb'] = round(w['size'] / 1024, 1)
        return render_template('wraps.html', wraps=wraps, available=wrap_service.get_available(), **get_template_context())
    except Exception as e:
        return f"<h3>Error</h3><p>{e}</p>", 500


@media_bp.route('/wraps/upload', methods=['POST'])
def wraps_upload():
    try:
        if 'file' not in request.files or request.files['file'].filename == '':
            return redirect('/wraps?error=no_file')
        file = request.files['file']
        success, message, _ = wrap_service.upload_wrap(file, file.filename)
        return redirect(f'/wraps?{"ok" if success else "error"}={message[:80]}')
    except Exception as e:
        return redirect(f'/wraps?error={str(e)[:80]}')


@media_bp.route('/wraps/delete/<filename>', methods=['POST'])
def wraps_delete(filename):
    try:
        success, message = wrap_service.delete_wrap(filename)
        return redirect(f'/wraps?{"ok" if success else "error"}={message[:80]}')
    except Exception as e:
        return redirect(f'/wraps?error={str(e)[:80]}')


@media_bp.route('/api/wraps/preview/<filename>')
def wraps_preview(filename):
    """[已统一] 重定向到 /api/media/wraps/preview/<filename>"""
    return redirect(url_for('media_wraps_preview', filename=filename))


@media_bp.route('/api/wraps/download/<filename>')
def wraps_download(filename):
    import os as _os
    safe_name = _os.path.basename(filename)
    file_path = _os.path.join(wrap_service.WRAP_DIR, safe_name)
    if not _os.path.isfile(file_path): return '', 404
    return send_file(file_path, mimetype='image/png', as_attachment=True, download_name=safe_name)

# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────


@media_bp.route('/license-plates')
def license_plates_page():
    """车牌管理页面"""
    try:
        plates = license_plate_service.list_plate_files()
        # 格式化文件大小
        for p in plates:
            p['size_kb'] = round(p['size'] / 1024, 1)
        return render_template('license_plates.html',
                               plates=plates,
                               available=license_plate_service.get_available(),
                               **get_template_context())
    except Exception as e:
        return f"<h3>Error</h3><p>{e}</p>", 500



@media_bp.route('/license-plates/upload', methods=['POST'])
def license_plates_upload():
    """上传车牌 PNG"""
    try:
        if 'file' not in request.files:
            return redirect('/license-plates?error=no_file')
        file = request.files['file']
        if file.filename == '':
            return redirect('/license-plates?error=no_file')

        success, message, _ = license_plate_service.upload_plate(file, file.filename)
        param = 'ok' if success else 'error'
        return redirect(f'/license-plates?{param}={message[:80]}')
    except Exception as e:
        return redirect(f'/license-plates?error={str(e)[:80]}')



@media_bp.route('/license-plates/delete/<filename>', methods=['POST'])
def license_plates_delete(filename):
    """删除车牌 PNG"""
    try:
        success, message = license_plate_service.delete_plate(filename)
        param = 'ok' if success else 'error'
        return redirect(f'/license-plates?{param}={message[:80]}')
    except Exception as e:
        return redirect(f'/license-plates?error={str(e)[:80]}')



@media_bp.route('/api/license-plates/preview/<filename>')
def license_plates_preview(filename):
    """预览车牌图片 (inline)"""
    import os as _os
    safe_name = _os.path.basename(filename)
    file_path = _os.path.join(license_plate_service.PLATE_DIR, safe_name)
    if not _os.path.isfile(file_path):
        return '', 404
    return send_file(file_path, mimetype='image/png')



@media_bp.route('/api/license-plates/download/<filename>')
def license_plates_download(filename):
    """下载车牌文件"""
    import os as _os
    safe_name = _os.path.basename(filename)
    file_path = _os.path.join(license_plate_service.PLATE_DIR, safe_name)
    if not _os.path.isfile(file_path):
        return '', 404
    return send_file(file_path, mimetype='image/png', as_attachment=True, download_name=safe_name)


# ── 系统状态 SSE 实时推送 ─────────────────────────────────────

# SSE stats_subscribers 已迁移至 app_state.py
# stats_subscribers_lock 已在 app_state.py 中创建


@media_bp.route('/api/media/wraps/preview/<filename>')
def media_wraps_preview(filename):
    """贴膜预览代理"""
    import os as _os
    safe = _os.path.basename(filename)
    fp = _os.path.join('/mnt/lightshow/Wraps', safe)
    if not _os.path.isfile(fp): return '', 404
    return send_file(fp, mimetype='image/png')


# ═══════════════════════════════════════════════════════════════
# 媒体管理 API（Boombox / Lightshow / Wraps / Music）
# ═══════════════════════════════════════════════════════════════


@media_bp.route('/api/media/staging/status')
def api_staging_status():
    """获取 staging 待同步状态"""
    return jsonify(staging_service.get_summary())


# ── Boombox API (使用 boombox_service) ──


@media_bp.route('/api/media/boombox/list')
def api_media_boombox_list():
    try:
        files = boombox_service.list_boombox_files()
        total = sum(f['size'] for f in files)
        disk = _media_disk_info('/mnt/boombox')
        return jsonify({'success': True, 'files': files, 'total_size': total, 'disk': disk})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/boombox/upload', methods=['POST'])
def api_media_boombox_upload():
    try:
        files = request.files.getlist('files')
        if not files: return jsonify({'success': False, 'error': '没有文件'}), 400
        if _is_present_mode():
            ok = 0
            for f in files:
                if f.filename:
                    r = _staging_upload('boombox', f, f.filename)
                    if r['success']: ok += 1
            return jsonify({'success': ok > 0, 'message': f'已暂存 {ok}/{len(files)} 个文件 (切换到 Edit 模式后同步)'})
        uploaded = 0
        for f in files:
            if f.filename:
                ok, _ = boombox_service.upload_boombox(f, f.filename)
                if ok: uploaded += 1
        return jsonify({'success': uploaded > 0, 'message': f'已上传 {uploaded}/{len(files)} 个文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/boombox/delete', methods=['POST'])
def api_media_boombox_delete():
    try:
        name = (request.get_json(silent=True) or {}).get('filename', '') or request.form.get('filename', '')
        if not name: return jsonify({'success': False, 'error': '文件名为空'}), 400
        if _is_present_mode():
            r = _staging_delete('boombox', name)
            return jsonify(r)
        ok, msg = boombox_service.delete_boombox(name)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/boombox/play/<path:filename>')
def api_media_boombox_play(filename):
    import os as _os
    safe = _os.path.basename(filename)
    fp = _os.path.join(boombox_service.BOOMBOX_DIR, safe)
    if not _os.path.isfile(fp): return '', 404
    return send_file(fp)


# ── Lightshow API (使用 lightshow_service) ──


@media_bp.route('/api/media/lightshow/list')
def api_media_lightshow_list():
    try:
        shows = lightshow_service.list_lightshows()
        all_files = []
        for s in shows:
            for f in s['files']:
                all_files.append({'name': f['filename'], 'size': f['size'], 'type': f['ext'].lstrip('.').upper()})
        total = sum(f['size'] for f in all_files)
        disk = _media_disk_info('/mnt/lightshow')
        return jsonify({'success': True, 'files': all_files, 'total_size': total, 'disk': disk})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/lightshow/upload', methods=['POST'])
def api_media_lightshow_upload():
    try:
        files = request.files.getlist('files')
        if not files: return jsonify({'success': False, 'error': '没有文件'}), 400
        if _is_present_mode():
            ok = 0
            for f in files:
                if f.filename:
                    r = _staging_upload('lightshow', f, f.filename)
                    if r['success']: ok += 1
            return jsonify({'success': ok > 0, 'message': f'已暂存 {ok}/{len(files)} 个文件 (切换到 Edit 模式后同步)'})
        count = 0
        for f in files:
            if not f.filename: continue
            fn = f.filename.lower()
            if fn.endswith('.zip'):
                ok, msg, c = lightshow_service.upload_lightshow_zip(f)
                if ok: count += c
            else:
                ok, msg = lightshow_service.upload_lightshow_file(f, f.filename)
                if ok: count += 1
        return jsonify({'success': count > 0, 'message': f'已导入 {count} 个文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/lightshow/delete', methods=['POST'])
def api_media_lightshow_delete():
    try:
        name = (request.get_json(silent=True) or {}).get('filename', '') or request.form.get('filename', '')
        if not name: return jsonify({'success': False, 'error': '文件名为空'}), 400
        if _is_present_mode():
            r = _staging_delete('lightshow', name)
            return jsonify(r)
        base = os.path.splitext(os.path.basename(name))[0]
        ok, msg = lightshow_service.delete_lightshow(base)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/lightshow/play/<path:filename>')
def api_media_lightshow_play(filename):
    """统一路由：播放/预览 Lightshow 文件"""
    import os as _os
    safe = _os.path.basename(filename)
    fp = _os.path.join(lightshow_service.LIGHTSHOW_DIR, safe)
    if not _os.path.isfile(fp): return '', 404
    return send_file(fp)


# ── Wraps API (使用 wrap_service) ──


@media_bp.route('/api/media/wraps/list')
def api_media_wraps_list():
    try:
        files = wrap_service.list_wrap_files()
        total = sum(f['size'] for f in files)
        return jsonify({'success': True, 'files': files, 'total_size': total})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/wraps/upload', methods=['POST'])
def api_media_wraps_upload():
    try:
        files = request.files.getlist('files')
        if not files: return jsonify({'success': False, 'error': '没有文件'}), 400
        if _is_present_mode():
            ok = 0
            for f in files:
                if f.filename:
                    r = _staging_upload('wraps', f, f.filename)
                    if r['success']: ok += 1
            return jsonify({'success': ok > 0, 'message': f'已暂存 {ok}/{len(files)} 个文件 (切换到 Edit 模式后同步)'})
        uploaded = 0
        for f in files:
            if f.filename:
                ok, _, _ = wrap_service.upload_wrap(f, f.filename)
                if ok: uploaded += 1
        return jsonify({'success': uploaded > 0, 'message': f'已上传 {uploaded}/{len(files)} 个文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/wraps/delete', methods=['POST'])
def api_media_wraps_delete():
    try:
        name = (request.get_json(silent=True) or {}).get('filename', '') or request.form.get('filename', '')
        if not name: return jsonify({'success': False, 'error': '文件名为空'}), 400
        if _is_present_mode():
            r = _staging_delete('wraps', name)
            return jsonify(r)
        ok, msg = wrap_service.delete_wrap(name)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Music API ──


@media_bp.route('/api/media/music/list')
def api_media_music_list():
    try:
        root = '/mnt/music'
        files = []
        if os.path.isdir(root):
            for fn in sorted(os.listdir(root)):
                fp = os.path.join(root, fn)
                if not os.path.isfile(fp): continue
                ext = os.path.splitext(fn)[1].lower()
                if ext not in {'.mp3','.flac','.wav','.aac','.m4a','.ogg','.wma'}: continue
                files.append({'name': fn, 'filename': fn, 'size': os.path.getsize(fp), 'ext': ext,
                              'modified': os.path.getmtime(fp)})
        total = sum(f['size'] for f in files)
        disk = _media_disk_info('/mnt/music')
        return jsonify({'success': True, 'files': files, 'total_size': total, 'disk': disk})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/music/upload', methods=['POST'])
def api_media_music_upload():
    try:
        root = '/mnt/music'
        files = request.files.getlist('files')
        if not files: return jsonify({'success': False, 'error': '没有文件'}), 400
        if _is_present_mode():
            ok = 0
            for f in files:
                if f.filename:
                    r = _staging_upload('music', f, f.filename)
                    if r['success']: ok += 1
            return jsonify({'success': ok > 0, 'message': f'已暂存 {ok}/{len(files)} 个文件 (切换到 Edit 模式后同步)'})
        os.makedirs(root, exist_ok=True)
        count = 0
        for f in files:
            if not f.filename: continue
            f.save(os.path.join(root, os.path.basename(f.filename)))
            count += 1
        return jsonify({'success': True, 'message': f'已上传 {count} 个文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/music/delete', methods=['POST'])
def api_media_music_delete():
    try:
        name = (request.get_json(silent=True) or {}).get('filename', '') or request.form.get('filename', '')
        if not name: return jsonify({'success': False, 'error': '文件名为空'}), 400
        if _is_present_mode():
            r = _staging_delete('music', name)
            return jsonify(r)
        fp = os.path.join('/mnt/music', os.path.basename(name))
        if os.path.isfile(fp): os.remove(fp)
        return jsonify({'success': True, 'message': f'{name} 已删除'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/music/play/<path:filename>')
def api_media_music_play(filename):
    fp = os.path.join('/mnt/music', os.path.basename(filename))
    if not os.path.isfile(fp): return '', 404
    return send_file(fp)


# ── 媒体 API: 车牌 ────────────────────────────────────────────


@media_bp.route('/api/media/plates/list')
def api_media_plates_list():
    try:
        from license_plate_service import list_plate_files
        plates = list_plate_files()
        total = sum(p['size'] for p in plates)
        return jsonify({'success': True, 'files': plates, 'total_size': total})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/plates/upload', methods=['POST'])
def api_media_plates_upload():
    try:
        from license_plate_service import upload_plate
        if 'files' not in request.files and 'file' not in request.files:
            return jsonify({'success': False, 'error': '没有文件'}), 400
        files = request.files.getlist('files') or [request.files['file']]
        if _is_present_mode():
            ok = 0
            for f in files:
                if f.filename:
                    r = _staging_upload('plates', f, f.filename)
                    if r['success']: ok += 1
            return jsonify({'success': ok > 0, 'message': f'已暂存 {ok}/{len(files)} 个文件 (切换到 Edit 模式后同步)'})
        results = []
        for f in files:
            if not f.filename: continue
            ok, msg, dims = upload_plate(f, f.filename)
            results.append({'filename': f.filename, 'success': ok, 'message': msg})
        ok_count = sum(1 for r in results if r['success'])
        return jsonify({'success': ok_count > 0, 'results': results, 'message': f'上传 {ok_count}/{len(results)} 个文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@media_bp.route('/api/media/plates/delete', methods=['POST'])
def api_media_plates_delete():
    try:
        from license_plate_service import delete_plate
        data = request.get_json() or request.form
        filename = data.get('filename', '')
        if _is_present_mode():
            r = _staging_delete('plates', filename)
            return jsonify(r)
        ok, msg = delete_plate(filename)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# 启动统计广播线程
