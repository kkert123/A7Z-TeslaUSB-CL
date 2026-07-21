"""
TeslaUSB — 车外监控路由 (台风场景分析 v1)
=============================================
方案 A: 缩略图时间线画廊  (/recent-clips)
方案 B: GIF 时光机          (/api/camera/gif)
方案 C: 仪表盘集成          (通过 SSE 推送最新缩略图)
"""
import os
import time
from flask import Blueprint, render_template, request, jsonify, send_file

import gif_service
from utils.app_helpers import get_template_context

camera_bp = Blueprint('camera', __name__, url_prefix='')


# ═══════════════════════════════════════════════════════════════
# 方案 A: 缩略图时间线画廊
# ═══════════════════════════════════════════════════════════════

@camera_bp.route('/recent-clips')
def recent_clips_page():
    """缩略图时间线画廊页面"""
    ctx = get_template_context()
    # 注入首次加载的缩略图列表（避免页面空白等待 AJAX）
    thumbnails = gif_service.list_recent_thumbnails(limit=30)
    ctx['thumbnails'] = thumbnails
    ctx['thumbnail_count'] = len(thumbnails)
    return render_template('recent_clips.html', **ctx)


# ═══════════════════════════════════════════════════════════════
# API: 缩略图列表 (AJAX 轮询)
# ═══════════════════════════════════════════════════════════════

@camera_bp.route('/api/camera/recent-thumbnails')
def api_recent_thumbnails():
    """返回 RecentClips 缩略图 JSON 列表"""
    try:
        limit = request.args.get('limit', 30, type=int)
        limit = max(1, min(limit, 60))
        thumbnails = gif_service.list_recent_thumbnails(limit=limit)
        return jsonify({
            'success': True,
            'thumbnails': [
                {
                    'event_id': t['event_id'],
                    'filename': t['filename'],
                    'timestamp': t['timestamp'],
                    'url': '/thumbnails/' + t['filename'],
                }
                for t in thumbnails
            ],
            'count': len(thumbnails),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 方案 B: GIF 时光机
# ═══════════════════════════════════════════════════════════════

@camera_bp.route('/api/camera/gif')
def api_camera_gif():
    """
    生成并返回 GIF 动画。

    Query params:
      frames: 帧数 (默认 10, 可选 5/10/30)
      interval: 帧间隔 ms (默认 500, 范围 100~3000)
      download: 设为 1 触发下载
    """
    try:
        frames = request.args.get('frames', gif_service.DEFAULT_FRAMES, type=int)
        interval = request.args.get('interval', gif_service.DEFAULT_INTERVAL, type=int)
        download = request.args.get('download', '0')

        gif_path, error = gif_service.generate_gif(frames=frames, interval_ms=interval)

        if error:
            return jsonify({'success': False, 'error': error}), 400

        mimetype = 'image/gif'
        if download == '1':
            return send_file(
                gif_path,
                mimetype=mimetype,
                as_attachment=True,
                download_name='recent_{}f_{}ms.gif'.format(frames, interval),
            )
        else:
            return send_file(gif_path, mimetype=mimetype)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_bp.route('/api/camera/gif/clear-cache', methods=['POST'])
def api_camera_gif_clear_cache():
    """清除 GIF 缓存（手动刷新用）"""
    try:
        gif_service.clear_gif_cache()
        return jsonify({'success': True, 'message': 'GIF 缓存已清除'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 方案 C: 仪表盘数据
# ═══════════════════════════════════════════════════════════════

@camera_bp.route('/api/camera/latest')
def api_camera_latest():
    """返回最新一张缩略图信息（供仪表盘 SSE 推送或 AJAX 拉取）"""
    try:
        latest = gif_service.get_latest_thumbnail()
        if latest:
            latest['url'] = '/thumbnails/' + latest['filename']
        return jsonify({
            'success': True,
            'latest': latest or None,
            'available': bool(latest),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_bp.route('/api/camera/count')
def api_camera_count():
    """返回 RecentClips 缩略图数量"""
    try:
        thumbnails = gif_service.list_recent_thumbnails(limit=60)
        return jsonify({
            'success': True,
            'count': len(thumbnails),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
