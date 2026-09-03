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


def _sync_recent_clips_cache():
    """recent-clips 读取前同步 VFS 缓存（v0.3.1.41 B）。

    读取驱动缺口修复：/api/camera/recent-thumbnails 与 /recent-clips 页面此前
    只列「已生成的 JPG」、从不调 ensure_fresh → 车机新写事件若未被 bg_preview
    感知（v0.3.1.39/40 A1 延迟刷下无人访问时 readdir 走冻结 page cache）则 JPG
    永不生成、列表永显旧集合。前置前台 ensure_fresh 解锁缓存 → bg_preview scan
    30s 内感知新事件并生成 → 下一轮列表出现。

    生效时效（交叉审查修正）：listdir 冻结期指纹恒等 → 前台分支不触发，实际
    靠 ensure_fresh 的 S3 兜底（非 dirty 距上次刷新 ≥120s 强制刷）生效 →
    页面打开/轮询期间最坏 ~120s 内解锁并出现新缩略图（有界，不再"永不"）；
    listdir 可穿透（内存压力）时前台分支立即刷（30s TTL）→ ~30s 内可见。
    Present 模式才生效（ensure_fresh 内部判断），Edit 模式零开销。
    """
    try:
        from utils.cache_coherency import ensure_fresh
        ensure_fresh()  # 前台语义：检测到车机写入立即刷（30s TTL 防抖）
    except Exception:
        # 刷新失败不影响列表返回，仅可能短暂陈旧
        pass


# ═══════════════════════════════════════════════════════════════
# 方案 A: 缩略图时间线画廊
# ═══════════════════════════════════════════════════════════════

@camera_bp.route('/recent-clips')
def recent_clips_page():
    """缩略图时间线画廊页面"""
    _sync_recent_clips_cache()  # v0.3.1.41 B：打开页面即同步缓存（解锁后台生成）
    ctx = get_template_context()
    # 注入首次加载的缩略图列表（避免页面空白等待 AJAX）
    thumbnails = gif_service.list_recent_thumbnails(limit=12)
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
        _sync_recent_clips_cache()  # v0.3.1.41 B：30s 轮询每次同步（页面开着=自动更新闭环）
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
