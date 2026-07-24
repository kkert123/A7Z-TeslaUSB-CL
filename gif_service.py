#!/usr/bin/env python3
"""
GIF 时光机服务 — 将 RecentClips 缩略图合成 GIF 动画
======================================================
依赖 Pillow (PIL)，纯 Python 实现，不依赖 ffmpeg。
A7Z Allwinner A733 测试：10帧合成约 0.5-1s，CPU 无压力。

设计参数（来自台风场景分析）:
  - 默认 10 帧（5太短看不清变化，30帧生成慢）
  - 间隔 500ms（1s 太慢看不出连续变化）
  - 缓存 30 秒（避免每次刷新都重新合成）
  - 格式 GIF 256色（缩略图本身就是小图，原生支持）
"""
import logging
import os
import time

logger = logging.getLogger("GifService")

# ── 路径常量 ──
THUMBNAIL_DIR = '/opt/radxa_data/teslausb/static/thumbnails'
GIF_CACHE_DIR = '/opt/radxa_data/teslausb/data/gif_cache'

# GIF 合成参数
DEFAULT_FRAMES = 10        # 默认帧数
DEFAULT_INTERVAL = 500     # 默认帧间隔 (ms)
CACHE_TTL = 30             # 缓存有效期 (s) — 5 帧/30 帧用（变化快）
CACHE_TTL_FAST = 60        # 10 帧模式用（默认 5s 循环，缩略图分钟级更新）

# GIF 输出尺寸：缩到 800x450，原图 1920x1080 grid.jpg
# 节省 5x 内存与 CPU，30 帧生成从 ~1s 降到 ~0.2s
GIF_OUTPUT_WIDTH = 800
GIF_OUTPUT_HEIGHT = 450

# 缩略图对象内存缓存：(sorted_event_ids_tuple, mtime) -> [(PIL.Image, event_id), ...]
# 仅缓存已 resize 的小图，避免每 30s 重新从磁盘打开 ~10MB 大图
_frame_cache = {}  # key: (frames, interval_ms) -> (sorted_ids_tuple, [(img, event_id)])


def _ensure_cache_dir():
    """确保 GIF 缓存目录存在"""
    os.makedirs(GIF_CACHE_DIR, exist_ok=True)


def _parse_event_time(event_id: str) -> str:
    """将 event_id 解析为可读时间字符串
    '2026-06-15_17-24-11' → '2026-06-15 17:24:11'
    """
    # 格式: YYYY-MM-DD_HH-MM-SS
    # 只把时间部分的 - 替换为 :，保留日期中的 -
    parts = event_id.split('_', 1)
    if len(parts) == 2:
        return parts[0] + ' ' + parts[1].replace('-', ':')
    return event_id.replace('_', ' ')


def list_recent_thumbnails(limit: int = 30) -> list:
    """
    列出 RecentClips 缩略图文件，按 mtime 倒序（最新在前）。
    
    返回 [{event_id, filename, path, mtime, timestamp}, ...]
    """
    results = []
    if not os.path.isdir(THUMBNAIL_DIR):
        return results

    # 使用 os.scandir() 避免 listdir 全量排序开销
    try:
        with os.scandir(THUMBNAIL_DIR) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                fname = entry.name
                if not fname.startswith('REC_') or not fname.endswith('_grid.jpg'):
                    continue

                # REC_2026-06-15_17-24-11_grid.jpg → 2026-06-15_17-24-11
                event_id = fname[4:-9]
                mtime = entry.stat().st_mtime

                results.append({
                    'event_id': event_id,
                    'filename': fname,
                    'path': entry.path,
                    'mtime': mtime,
                    'timestamp': _parse_event_time(event_id),
                })
    except OSError:
        return results

    # 按 mtime 倒序
    results.sort(key=lambda x: x['mtime'], reverse=True)
    return results[:limit]


def generate_gif(frames: int = DEFAULT_FRAMES, interval_ms: int = DEFAULT_INTERVAL):
    """
    用 RecentClips 缩略图合成 GIF 动画。
    
    参数:
        frames: 帧数 (5/10/30)
        interval_ms: 帧间隔毫秒 (100~3000)
    
    返回:
        (str | None, str | None) = (gif文件路径, 错误消息)
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.error("Pillow 未安装")
        return None, "Pillow 未安装 (pip install Pillow)"

    # 参数安全限制
    frames = max(3, min(frames, 60))
    interval_ms = max(100, min(interval_ms, 3000))

    _ensure_cache_dir()

    # 检查磁盘缓存
    cache_key = "recent_{}f_{}ms.gif".format(frames, interval_ms)
    cache_path = os.path.join(GIF_CACHE_DIR, cache_key)
    # 10 帧模式用更长缓存（视觉变化慢），5/30 帧用短缓存
    ttl = CACHE_TTL_FAST if frames == DEFAULT_FRAMES else CACHE_TTL
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < ttl:
            logger.debug("使用缓存 GIF: %s (age=%.0fs)", cache_path, age)
            return cache_path, None

    # 获取缩略图列表（最新在前，供 frame_gallery 等其他用途）
    thumbnails = list_recent_thumbnails(limit=frames)
    if len(thumbnails) < 2:
        return None, "缩略图不足 (需要至少2张，当前{}张)".format(len(thumbnails))

    # ⚠️ GIF 帧需要按时间升序排列（旧→新），而非 mtime 倒序
    # event_id 格式: 2026-07-13_23-46-47，字典序等价于时间顺序
    thumbnails.sort(key=lambda x: x['event_id'])

    # 检查内存缓存：如果帧列表未变，复用已 resize 的 PIL Image
    sorted_ids = tuple(t['event_id'] for t in thumbnails)
    cache_lookup_key = (frames, interval_ms)
    cached = _frame_cache.get(cache_lookup_key)
    if cached and cached[0] == sorted_ids:
        logger.debug("使用内存帧缓存: %d帧", len(cached[1]))
        images = [img for img, _ in cached[1]]
    else:
        # 加载字体
        font = None
        for font_path in [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/custom/simhei.ttf',
        ]:
            try:
                font = ImageFont.truetype(font_path, 14)
                break
            except (OSError, IOError):
                continue

        if font is None:
            font = ImageFont.load_default()

        # 加载图片 + resize + 叠加时间戳水印
        images = []
        cache_entry = []
        loaded_count = 0
        for tn in thumbnails:
            try:
                img = Image.open(tn['path']).convert('RGBA')

                # ⚡ 关键优化：把 1920x1080 grid.jpg 缩到 800x450
                # 30 帧合成从 ~1s 降到 ~0.2s，内存从 ~10MB 降到 ~1.4MB
                if img.size != (GIF_OUTPUT_WIDTH, GIF_OUTPUT_HEIGHT):
                    img = img.resize((GIF_OUTPUT_WIDTH, GIF_OUTPUT_HEIGHT), Image.LANCZOS)

                # 底部半透明时间条
                ts_display = tn['event_id'].replace('_', ' ')
                draw_tmp = ImageDraw.Draw(img)

                # 使用 getbbox 或 textlength 测量文字
                try:
                    bbox = draw_tmp.textbbox((0, 0), ts_display, font=font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                except (AttributeError, TypeError):
                    tw = len(ts_display) * 8  # 估算
                    th = 16

                bar_h = th + 10
                bar_y = img.height - bar_h

                # 半透明黑底
                overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                overlay_draw = ImageDraw.Draw(overlay)
                overlay_draw.rectangle(
                    [0, bar_y, img.width, img.height],
                    fill=(0, 0, 0, 130)
                )
                img = Image.alpha_composite(img, overlay)

                # 白色时间戳文字
                draw = ImageDraw.Draw(img)
                draw.text((8, bar_y + 5), ts_display, fill=(255, 255, 255, 245), font=font)

                rgb = img.convert('RGB')
                images.append(rgb)
                cache_entry.append((rgb, tn['event_id']))
                loaded_count += 1
            except Exception as e:
                logger.warning("加载缩略图失败 %s: %s", tn.get('filename', '?'), e)
                continue

        if loaded_count < 2:
            return None, "可加载的缩略图不足 (需要至少2张，成功{}张)".format(loaded_count)

        # 写入内存缓存（供 30s 内下次请求复用，跳过打开+resize+水印）
        _frame_cache[cache_lookup_key] = (sorted_ids, cache_entry)

    # 合成为 GIF
    try:
        images[0].save(
            cache_path,
            save_all=True,
            append_images=images[1:],
            duration=interval_ms,
            loop=0,       # 无限循环
            optimize=True,
        )
        logger.info(
            "GIF 合成完成: %s (%d帧, %dms间隔, %.0fKB)",
            cache_path, len(images), interval_ms, os.path.getsize(cache_path) / 1024
        )
        return cache_path, None
    except Exception as e:
        logger.error("GIF 合成失败: %s", e)
        return None, "GIF 合成失败: {}".format(str(e))


def get_latest_thumbnail() -> dict:
    """
    获取最新一张 RecentClips 缩略图信息（供仪表盘卡片使用）。
    
    返回 {'event_id', 'filename', 'timestamp'} 或 {}
    """
    thumbnails = list_recent_thumbnails(limit=1)
    if thumbnails:
        tn = thumbnails[0]
        return {
            'event_id': tn['event_id'],
            'filename': tn['filename'],
            'timestamp': tn['timestamp'],
        }
    return {}


def clear_gif_cache():
    """清除所有 GIF 缓存文件（磁盘空间管理）"""
    # 清内存帧缓存（避免指向已删除的 PIL Image 对象）
    _frame_cache.clear()

    if not os.path.isdir(GIF_CACHE_DIR):
        return
    deleted = 0
    for fname in os.listdir(GIF_CACHE_DIR):
        if fname.endswith('.gif'):
            try:
                os.remove(os.path.join(GIF_CACHE_DIR, fname))
                deleted += 1
            except OSError:
                pass
    if deleted:
        logger.info("清除 %d 个 GIF 缓存文件", deleted)
