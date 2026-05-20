#!/usr/bin/env python3
"""
TeslaUSB Web Management System - Main Application
"""

import os
import sys
import re
import json
import shutil
from datetime import datetime
from pathlib import Path

# Flask imports
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
from functools import wraps

# Local imports
sys.path.append(os.path.dirname(__file__))

# Configuration
class Config:
    # Media partitions
    _MEDIA_PARTITIONS = {
        "cam": "/media/cnlvan/cam",
        "music": "/media/cnlvan/music",
        "boombox": "/media/cnlvan/boombox",
        "lightshow": "/media/cnlvan/lightshow",
        "wraps": "/media/cnlvan/wraps"
    }

    # Data directory
    DATA_DIR = "/opt/teslausb-web/data"

    # Allowed file extensions
    AUDIO_EXTENSIONS = {'.mp3', '.flac', '.wav', '.aac', '.m4a', '.ogg', '.wma'}
    IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg'}
    ZIP_EXTENSIONS = {'.zip'}

# Initialize Flask app
app = Flask(__name__)
app.config.from_object(Config)

# Global config
PARTITIONS = app.config['_MEDIA_PARTITIONS']

# Authentication decorator (simplified for this example)
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # In production, implement proper authentication
        return f(*args, **kwargs)
    return decorated_function

# Utility functions
def safe_filename(filename):
    """Secure filename handling"""
    return secure_filename(filename)

def get_disk_usage(path):
    """Get disk usage statistics"""
    try:
        if os.path.exists(path):
            usage = shutil.disk_usage(path)
            return {
                'total': usage.total,
                'used': usage.used,
                'free': usage.free,
                'percent': round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0
            }
    except Exception:
        pass
    return None

def list_files_in_directory(directory, allowed_extensions=None):
    """List files in directory with optional extension filtering"""
    files = []
    total_size = 0

    try:
        if os.path.exists(directory):
            for item in sorted(os.listdir(directory)):
                item_path = os.path.join(directory, item)
                if os.path.isfile(item_path):
                    stat = os.stat(item_path)
                    ext = os.path.splitext(item)[1].lower()

                    # Filter by extension if specified
                    if allowed_extensions and ext not in allowed_extensions:
                        continue

                    files.append({
                        'name': item,
                        'size': stat.st_size,
                        'modified': int(stat.st_mtime),
                        'ext': ext
                    })
                    total_size += stat.st_size
    except Exception as e:
        print(f"Error listing files: {e}")

    return files, total_size

# Routes
@app.route('/')
def index():
    """Main dashboard"""
    return render_template('index.html')

@app.route('/media')
def media_page():
    """Media management page"""
    return render_template('media.html')

@app.route('/analytics')
def analytics_page():
    """Analytics dashboard"""
    return render_template('analytics.html', now=datetime.now().strftime("%H:%M:%S"))

@app.route('/system')
def system_page():
    """System configuration page"""
    return render_template('system.html')

# ─────────────────────────────────────────────
# Boombox API ──
# ─────────────────────────────────────────────

BOOMBOX_PATH = PARTITIONS["boombox"]
BOOMBOX_ALLOWED_EXT = {'.mp3', '.flac', '.wav', '.aac', '.m4a'}

@app.route("/api/media/boombox/list")
@require_auth
def api_boombox_list():
    """List boombox audio files"""
    files, total_size = list_files_in_directory(BOOMBOX_PATH, BOOMBOX_ALLOWED_EXT)
    disk_info = get_disk_usage(BOOMBOX_PATH)

    return jsonify({
        'success': True,
        'files': files,
        'total_size': total_size,
        'disk': disk_info
    })

@app.route("/api/media/boombox/upload", methods=["POST"])
@require_auth
def api_boombox_upload():
    """Upload boombox audio files"""
    if 'files' not in request.files:
        return jsonify({'success': False, 'error': 'No files provided'})

    files = request.files.getlist('files')
    uploaded_count = 0

    try:
        os.makedirs(BOOMBOX_PATH, exist_ok=True)

        for file in files:
            if file.filename == '':
                continue

            filename = safe_filename(file.filename)
            if not filename:
                continue

            # Check file extension
            ext = os.path.splitext(filename)[1].lower()
            if ext not in BOOMBOX_ALLOWED_EXT:
                continue

            save_path = os.path.join(BOOMBOX_PATH, filename)
            file.save(save_path)
            uploaded_count += 1

        return jsonify({
            'success': True,
            'message': f'Successfully uploaded {uploaded_count} files',
            'count': uploaded_count
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route("/api/media/boombox/play/<path:filename>")
@require_auth
def api_boombox_play(filename):
    """Stream boombox audio file with Range support"""
    safe_name = safe_filename(filename)
    file_path = os.path.join(BOOMBOX_PATH, safe_name)

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': 'File not found'}), 404

    # Handle Range requests for seeking
    range_header = request.headers.get('Range', None)
    if range_header:
        # Parse Range header: bytes=start-end
        range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if range_match:
            start = int(range_match.group(1))
            end = range_match.group(2)
            end = int(end) if end else os.path.getsize(file_path) - 1

            return send_file(
                file_path,
                mimetype='audio/mpeg',
                as_attachment=False,
                conditional=True,
                start_byte=start,
                end_byte=end
            )

    return send_file(file_path, mimetype='audio/mpeg', as_attachment=False)

@app.route("/api/media/boombox/delete", methods=["POST"])
@require_auth
def api_boombox_delete():
    """Delete boombox audio file"""
    filename = request.form.get('filename')
    if not filename:
        return jsonify({'success': False, 'error': 'No filename provided'})

    safe_name = safe_filename(filename)
    file_path = os.path.join(BOOMBOX_PATH, safe_name)

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({'success': True, 'message': 'File deleted successfully'})
        else:
            return jsonify({'success': False, 'error': 'File not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ─────────────────────────────────────────────
# Music API ──
# ─────────────────────────────────────────────

MUSIC_PATH = PARTITIONS["music"]
MUSIC_ALLOWED_EXT = {'.mp3', '.flac', '.wav', '.aac', '.m4a', '.ogg', '.wma'}

@app.route("/api/media/music/list")
@require_auth
def api_music_list():
    """List music files"""
    files, total_size = list_files_in_directory(MUSIC_PATH, MUSIC_ALLOWED_EXT)
    disk_info = get_disk_usage(MUSIC_PATH)

    return jsonify({
        'success': True,
        'files': files,
        'total_size': total_size,
        'disk': disk_info
    })

@app.route("/api/media/music/upload", methods=["POST"])
@require_auth
def api_music_upload():
    """Upload music files"""
    if 'files' not in request.files:
        return jsonify({'success': False, 'error': 'No files provided'})

    files = request.files.getlist('files')
    uploaded_count = 0

    try:
        os.makedirs(MUSIC_PATH, exist_ok=True)

        for file in files:
            if file.filename == '':
                continue

            filename = safe_filename(file.filename)
            if not filename:
                continue

            # Check file extension
            ext = os.path.splitext(filename)[1].lower()
            if ext not in MUSIC_ALLOWED_EXT:
                continue

            save_path = os.path.join(MUSIC_PATH, filename)
            file.save(save_path)
            uploaded_count += 1

        return jsonify({
            'success': True,
            'message': f'Successfully uploaded {uploaded_count} files',
            'count': uploaded_count
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route("/api/media/music/play/<path:filename>")
@require_auth
def api_music_play(filename):
    """Stream music file with Range support"""
    safe_name = safe_filename(filename)
    file_path = os.path.join(MUSIC_PATH, safe_name)

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': 'File not found'}), 404

    # Handle Range requests for seeking
    range_header = request.headers.get('Range', None)
    if range_header:
        range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if range_match:
            start = int(range_match.group(1))
            end = range_match.group(2)
            end = int(end) if end else os.path.getsize(file_path) - 1

            return send_file(
                file_path,
                mimetype='audio/mpeg',
                as_attachment=False,
                conditional=True,
                start_byte=start,
                end_byte=end
            )

    return send_file(file_path, mimetype='audio/mpeg', as_attachment=False)

@app.route("/api/media/music/delete", methods=["POST"])
@require_auth
def api_music_delete():
    """Delete music file"""
    filename = request.form.get('filename')
    if not filename:
        return jsonify({'success': False, 'error': 'No filename provided'})

    safe_name = safe_filename(filename)
    file_path = os.path.join(MUSIC_PATH, safe_name)

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({'success': True, 'message': 'File deleted successfully'})
        else:
            return jsonify({'success': False, 'error': 'File not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# Lightshow API routes would go here...

if __name__ == '__main__':
    # Create data directory if it doesn't exist
    os.makedirs(app.config['DATA_DIR'], exist_ok=True)

    # Run the application
    app.run(host='0.0.0.0', port=5000, debug=False)