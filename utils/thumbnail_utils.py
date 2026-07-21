"""缩略图生成模块 — 四宫格 + 时间水印"""
import os
import json
import subprocess
import time
from datetime import datetime

import video_service
from video_service import THUMBNAIL_DIR, THUMBNAIL_SIZE, _FONT_CN, _FONT_EN


def _generate_thumbnail(event_path, event_id, video_files=None, folder_type=None):
    """生成四宫格缩略图：2x2 (前/后+左/右) + 摄像头标签 + 时间水印
    
    参考 TeslaUSB-CL video_preview.py generate_sentry_grid_preview()
    
    Args:
        event_path: 事件文件夹路径（或 RecentClips 平铺目录）
        event_id: 事件ID
        video_files: 可选，直接指定视频文件列表（用于 RecentClips 平铺结构）
        folder_type: 可选，文件夹类型 (SentryClips/SavedClips/RecentClips)，用于缩略图命名
    
    Returns:
        str: 缩略图 URL 路径，失败返回 None
    """
    from PIL import Image, ImageDraw, ImageFont
    import tempfile
    
    if not os.path.exists(THUMBNAIL_DIR):
        os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    
    # 推断文件夹类型
    if not folder_type:
        folder_type = video_service.infer_folder_type(event_path)
    
    thumbnail_file = video_service.get_thumbnail_path(folder_type, event_id)
    
    # 缓存检查（含 RecentClips 平铺文件结构支持）
    # 
    # ⚠️ RecentClips 特殊处理：Tesla USB Gadget 循环覆盖文件，
    # mtime 比较不可靠（文件名可能被回收写入新内容）。
    # 只要缩略图存在且有效（≥10KB），直接返回缓存。
    if os.path.exists(thumbnail_file):
        if folder_type == 'RecentClips':
            try:
                if os.path.getsize(thumbnail_file) >= 10240:
                    return video_service.get_thumbnail_url(folder_type, event_id)
            except OSError:
                pass
        else:
            # SentryClips/SavedClips：事件文件夹结构不会被回收，mtime 比较可靠
            try:
                if video_files:
                    newest_mtime = max((os.path.getmtime(vf) for vf in video_files), default=0)
                else:
                    newest_mtime = 0
                    for f in os.listdir(event_path):
                        fp = os.path.join(event_path, f)
                        if os.path.isfile(fp) and f.lower().endswith('.mp4'):
                            newest_mtime = max(newest_mtime, os.path.getmtime(fp))
                if newest_mtime > 0 and os.path.getmtime(thumbnail_file) >= newest_mtime:
                    return video_service.get_thumbnail_url(folder_type, event_id)
            except:
                pass
    
    # 1) 读取 event.json 获取时间戳
    key_timestamp = None
    event_json_path = os.path.join(event_path, 'event.json')
    if os.path.exists(event_json_path):
        try:
            with open(event_json_path, 'r') as f:
                event_data = json.load(f)
            ts_str = event_data.get('timestamp')
            if ts_str:
                key_timestamp = datetime.fromisoformat(ts_str)
        except:
            pass
    if not key_timestamp:
        # 从 event_id/文件名 解析时间戳
        try:
            ts_str = event_id.replace('_', ' ')[:19]
            key_timestamp = datetime.strptime(ts_str, '%Y-%m-%d %H-%M-%S')
        except:
            key_timestamp = datetime.now()
    
    # 2) 解析文件夹名获取视频起始时间
    folder_name = os.path.basename(event_path)
    video_start = None
    try:
        ts_str = folder_name.replace('_', ' ')[:19]
        video_start = datetime.strptime(ts_str, '%Y-%m-%d %H-%M-%S')
    except:
        pass
    
    # 如果没从文件夹名解析到（RecentClips），用 event_id
    if not video_start:
        try:
            ts_str = event_id.replace('_', ' ')[:19]
            video_start = datetime.strptime(ts_str, '%Y-%m-%d %H-%M-%S')
        except:
            pass
    
    # 3) 加载字体
    try:
        font_cn = ImageFont.truetype(_FONT_CN, 24)
    except:
        font_cn = ImageFont.load_default()
    try:
        font_time = ImageFont.truetype(_FONT_EN, 36)
    except:
        font_time = font_cn
    
    # 4) 四个摄像头配置
    camera_map = {
        'front': ('前摄像头', False),
        'back':  ('后摄像头', False),
        'left':  ('左摄像头', True),
        'right': ('右摄像头', True),
    }
    
    # ── Present 模式 VFS 缓存一致性：生成前强制刷新只读挂载的 dentry/inode 缓存 ──
    # RecentClips 文件名被 Tesla 循环回收，只读挂载 /mnt/teslacam 的 VFS 缓存
    # （文件名→簇的映射）在 Tesla 写入后不会自动失效。若此处不刷新，
    # 下方 ffmpeg 会抽到「旧簇」的帧 → 缩略图货不对板，且因 >=10KB 被永久缓存。
    # 仅在 RecentClips（会被回收的文件名）触发；复用 cache_coherency.drop_vfs_caches()
    # （web / bg_preview 以 root 运行，直接写 /proc/sys/vm/drop_caches 生效）。
    if folder_type == 'RecentClips':
        try:
            from utils.cache_coherency import drop_vfs_caches
            drop_vfs_caches()
        except Exception:
            # 刷新失败不影响生成主流程，仅可能仍读到陈旧帧
            pass

    frames = {}  # cam_key -> (PIL.Image, label)

    for cam_key, (cam_label, need_flip) in camera_map.items():
        video_path = None
        
        if video_files:
            # RecentClips 模式：从提供的文件列表中查找
            # 先去重 + 只保留属于此 event_id 的文件
            valid_videos = []
            seen_stems = set()
            for vf in video_files:
                stem = os.path.splitext(os.path.basename(vf))[0]
                if stem in seen_stems:
                    continue
                if not stem.startswith(event_id):
                    continue
                seen_stems.add(stem)
                valid_videos.append(vf)
            
            for vf in valid_videos:
                fname_lower = os.path.basename(vf).lower()
                if f'-{cam_key}' in fname_lower:
                    video_path = vf
                    break
                if cam_key in ('left', 'right') and f'-{cam_key}_repeater' in fname_lower:
                    video_path = vf
                    break
        else:
            # 事件文件夹模式：扫描目录
            # 仅 RecentClips 等平铺目录需按 event_id 前缀过滤
            is_flat_dir = os.path.basename(event_path) in ('RecentClips', 'SavedClips')
            for fname in sorted(os.listdir(event_path)):
                if fname.lower().endswith('.mp4'):
                    if is_flat_dir and not fname.startswith(event_id):
                        continue
                    if f'-{cam_key}' in fname.lower():
                        video_path = os.path.join(event_path, fname)
                        break
                    if cam_key in ('left', 'right') and f'-{cam_key}_repeater' in fname.lower():
                        video_path = os.path.join(event_path, fname)
                        break
        
        if not video_path:
            continue
        
        # 跳过加密/损坏的文件（Tesla 会加密 RecentClips，无法解码）
        if not video_service.is_valid_mp4(video_path):
            continue
        
        # 计算时间偏移
        time_offset = 3.0
        if video_start and key_timestamp:
            delta = (key_timestamp - video_start).total_seconds()
            if 0 < delta < 60:
                time_offset = delta
        
        # ffmpeg 提取帧
        frame_img = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix='.jpg')
            os.close(tmp_fd)
            
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(time_offset),
                '-i', video_path,
                '-vframes', '1',
                '-q:v', '5',
                '-pix_fmt', 'yuvj420p',
                tmp_path
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
            if proc.returncode == 0 and os.path.exists(tmp_path):
                frame_img = Image.open(tmp_path)
                if need_flip:
                    frame_img = frame_img.transpose(Image.FLIP_LEFT_RIGHT)
                frames[cam_key] = (frame_img, cam_label)
            
            try:
                os.unlink(tmp_path)
            except:
                pass
        except Exception as e:
            print(f"[Thumbnail] 提取 {cam_key} 帧失败: {e}")
    
    if not frames:
        return None
    
    # 5) 构建四宫格
    try:
        first_frame = list(frames.values())[0][0]
        cell_w, cell_h = first_frame.size
        gap = 4
        
        grid_w = cell_w * 2 + gap
        grid_h = cell_h * 2 + gap
        
        grid = Image.new('RGB', (grid_w, grid_h), (30, 30, 30))
        draw = ImageDraw.Draw(grid)
        
        # 布局: 上排 [front, back], 下排 [left, right]
        grid_layout = [
            [('front', 0, 0), ('back', cell_w + gap, 0)],
            [('left', 0, cell_h + gap), ('right', cell_w + gap, cell_h + gap)],
        ]
        
        for row in grid_layout:
            for cam_key, x, y in row:
                if cam_key in frames:
                    frame_img, cam_label = frames[cam_key]
                    if frame_img.size != (cell_w, cell_h):
                        frame_img = frame_img.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
                    grid.paste(frame_img, (x, y))
                    
                    # 摄像头标签
                    label_font_size = max(24, cell_h // 25)
                    try:
                        label_font = ImageFont.truetype(_FONT_CN, label_font_size)
                    except:
                        label_font = font_cn
                    
                    bbox = draw.textbbox((0, 0), cam_label, font=label_font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    pad = 8
                    lx, ly = x + 10, y + 10
                    
                    overlay = Image.new('RGBA', grid.size, (0, 0, 0, 0))
                    overlay_draw = ImageDraw.Draw(overlay)
                    overlay_draw.rectangle(
                        [lx, ly, lx + tw + pad * 2, ly + th + pad * 2],
                        fill=(0, 0, 0, 160)
                    )
                    grid = Image.alpha_composite(grid.convert('RGBA'), overlay)
                    draw = ImageDraw.Draw(grid)
                    draw.text((lx + pad, ly + pad), cam_label, fill=(255, 255, 255), font=label_font)
                else:
                    draw.rectangle([x, y, x + cell_w, y + cell_h], fill=(50, 50, 50))
                    draw.text((x + cell_w // 2 - 30, y + cell_h // 2 - 14),
                             'N/A', fill=(120, 120, 120), font=font_cn)
        
        # 6) 缩放到目标宽度
        target_w = 1000
        target_h = int(grid_h * target_w / grid_w)
        grid = grid.resize((target_w, target_h), Image.Resampling.LANCZOS)
        
        # 7) 右下角时间水印
        draw = ImageDraw.Draw(grid)
        time_str = key_timestamp.strftime('%Y-%m-%d %H:%M:%S')
        
        bbox = draw.textbbox((0, 0), time_str, font=font_time)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 10
        margin = 16
        wm_w, wm_h = tw + pad * 2, th + pad * 2
        wm_x, wm_y = target_w - wm_w - margin, target_h - wm_h - margin
        
        overlay = Image.new('RGBA', grid.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle([wm_x, wm_y, wm_x + wm_w, wm_y + wm_h], fill=(0, 0, 0, 170))
        grid = Image.alpha_composite(grid.convert('RGBA'), overlay)
        draw = ImageDraw.Draw(grid)
        draw.text((wm_x + pad, wm_y + pad), time_str, fill=(255, 255, 255), font=font_time)
        
        # 8) 保存
        grid_rgb = grid.convert('RGB')
        grid_rgb.save(thumbnail_file, 'JPEG', quality=82)
        
        size_kb = os.path.getsize(thumbnail_file) // 1024
        print(f"[Thumbnail] {event_id} \u56db\u5bab\u683c\u751f\u6210\u5b8c\u6210 ({size_kb}KB)")
        
        return video_service.get_thumbnail_url(folder_type, event_id)
    
    except Exception as e:
        print(f"[Thumbnail] 四宫格生成失败 {event_id}: {e}")
        return None
