#!/usr/bin/env python3
"""
TeslaUSB Web Management System - 最终修复版
所有模板变量已正确传递
"""

import os
import json
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, send_file, send_from_directory
import time
import threading
import shutil
from functools import wraps
import wifi_service
import sync_service
try:
    import media_service
    from media_service import BoomboxService, LightshowService, WrapsService, MediaService
except ImportError:
    media_service = None
    BoomboxService = None
    LightshowService = None
    WrapsService = None
    MediaService = None

app = Flask(__name__)

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────

PARTITIONS = {
    "cam": "/mnt/teslacam",
    "music": "/mnt/music",
    "boombox": "/mnt/boombox",
    "lightshow": "/mnt/lightshow",
    "wraps": "/mnt/wraps"
}

# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────

def get_wifi_info():
    """获取WiFi连接信息（修复：正确读取已连接的WiFi名称）"""
    wifi = {'connected': False, 'ssid': None, 'signal': None, 'frequency': None}
    try:
        # 方法1: 使用 iwconfig（适用于旧版wifi工具）
        try:
            result = subprocess.run(['iwconfig', 'wlan0'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and 'ESSID' in result.stdout:
                for line in result.stdout.split('\n'):
                    if 'ESSID' in line:
                        # 格式: ESSID:"WiFi名称"
                        ssid = line.split('ESSID:')[1].strip().strip('"')
                        if ssid and ssid != 'off/any':
                            wifi['connected'] = True
                            wifi['ssid'] = ssid
                    if 'Signal level' in line:
                        # 格式: Signal level=-50 dBm
                        parts = line.split('Signal level=')
                        if len(parts) > 1:
                            wifi['signal'] = parts[1].split(' ')[0].strip()
                    if 'Frequency' in line:
                        # 格式: Frequency:2.437 GHz
                        parts = line.split('Frequency:')
                        if len(parts) > 1:
                            wifi['frequency'] = parts[1].split(' ')[0].strip()
        except:
            pass
        
        # 方法2: 如果 iwconfig 失败，尝试 iw dev wlan0 link
        if not wifi['connected']:
            try:
                result = subprocess.run(['iw', 'dev', 'wlan0', 'link'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0 and 'Connected' in result.stdout:
                    wifi['connected'] = True
                    for line in result.stdout.split('\n'):
                        if line.strip().startswith('SSID:'):
                            wifi['ssid'] = line.strip().split('SSID:')[1].strip()
                        if 'signal' in line.lower():
                            wifi['signal'] = line.strip()
            except:
                pass
        
        # 方法3: 使用 nmcli（如果系统安装了NetworkManager）
        if not wifi['connected']:
            try:
                result = subprocess.run(['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION', 'device', 'status'],
                                      capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        parts = line.split(':')
                        if len(parts) >= 3 and parts[0] == 'wlan0' and parts[1] == 'connected':
                            wifi['connected'] = True
                            wifi['ssid'] = parts[2]
                            break
            except:
                pass
    except Exception as e:
        print(f"Error getting WiFi info: {e}")
        pass
    
    return wifi



def get_system_uptime():
    """获取系统运行时间"""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
        
        # 转换为天、小时、分钟
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        if days > 0:
            return f"{days}天{hours}小时{minutes}分钟"
        elif hours > 0:
            return f"{hours}小时{minutes}分钟"
        else:
            return f"{minutes}分钟"
    except:
        return "N/A"

def get_service_status():
    """获取服务状态"""
    service = {'active': False, 'uptime': 'N/A'}
    try:
        result = subprocess.run(['systemctl', 'is-active', 'teslausb-web.service'],
                              capture_output=True, text=True, timeout=2)
        service['active'] = result.returncode == 0 and 'active' in result.stdout
        
        if service['active']:
            # 获取启动时间戳
            result = subprocess.run(
                ['systemctl', 'show', 'teslausb-web.service', '--property=ActiveEnterTimestamp'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                timestamp_str = result.stdout.strip().split('=', 1)[1]
                try:
                    # 解析 "Mon 2026-05-11 17:17:29 CST"
                    start_time = datetime.strptime(timestamp_str, "%a %Y-%m-%d %H:%M:%S %Z")
                    now = datetime.now()
                    uptime_delta = now - start_time
                    
                    # 格式化为可读时间
                    days = uptime_delta.days
                    hours, remainder = divmod(uptime_delta.seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    
                    if days > 0:
                        service['uptime'] = f"{days}天 {hours}小时 {minutes}分钟"
                    elif hours > 0:
                        service['uptime'] = f"{hours}小时 {minutes}分钟"
                    else:
                        service['uptime'] = f"{minutes}分钟"
                except Exception as e:
                    print(f"Error parsing uptime: {e}")
                    service['uptime'] = timestamp_str
    except Exception as e:
        print(f"Error getting service status: {e}")
        pass
    return service


def get_ip_info():
    """获取 IP 地址（修复：正确读取 wlan0 和 tailscale0）"""
    ip_info = {'local': 'N/A', 'tailscale': 'N/A'}
    try:
        # 方法1: 使用 ip addr show 获取指定接口IP
        # 本地IP - 优先读取 wlan0，其次 eth0
        for iface in ['wlan0', 'eth0', 'enp0s3', 'ens3']:
            try:
                result = subprocess.run(['ip', '-4', 'addr', 'show', iface],
                                      capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    # 解析 "inet 192.168.0.101/24"
                    for line in result.stdout.split('\n'):
                        if 'inet ' in line:
                            ip_addr = line.split('inet ')[1].split('/')[0].strip()
                            ip_info['local'] = ip_addr
                            break
                    if ip_info['local'] != 'N/A':
                        break
            except:
                continue
        
        # 如果上面没找到，再用 hostname -I（排除loopback和tailscale）
        if ip_info['local'] == 'N/A':
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                ips = result.stdout.strip().split()
                for ip in ips:
                    if not ip.startswith('127.') and not ip.startswith('100.'):
                        ip_info['local'] = ip
                        break
    except:
        pass
    
    # Tailscale IP - 读取 tailscale0 接口
    try:
        result = subprocess.run(['ip', '-4', 'addr', 'show', 'tailscale0'],
                              capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'inet ' in line:
                    ip_addr = line.split('inet ')[1].split('/')[0].strip()
                    ip_info['tailscale'] = ip_addr
                    break
    except:
        pass
    
    # 如果上面失败，尝试 tailscale 命令
    if ip_info['tailscale'] == 'N/A':
        try:
            result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip():
                ip_info['tailscale'] = result.stdout.strip()
        except:
            pass
    
    return ip_info



# NVMe 温度历史（滑动窗口，最多 60 个采样点，约 30 分钟）
_nvme_temp_history = []
_nvme_temp_lock = threading.Lock()
NVME_TEMP_MAX_HISTORY = 60

def _read_nvme_temp_raw():
    """读取 NVMe 单次温度值"""
    try:
        result = subprocess.run(
            ['sudo', '-n', 'nvme', 'smart-log', '/dev/nvme0'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'temperature' in line.lower() and ':' in line:
                    parts = line.split(':')
                    if len(parts) >= 2:
                        val = parts[1].strip().replace(' C','').replace('°C','')
                        try:
                            return int(val)
                        except ValueError:
                            pass
    except Exception:
        pass
    return None

def get_nvme_temperature():
    """获取 NVMe 当前温度"""
    return _read_nvme_temp_raw()

def _update_nvme_temp_history():
    """更新 NVMe 温度历史（由 stats broadcaster 调用）"""
    temp = _read_nvme_temp_raw()
    if temp is not None:
        with _nvme_temp_lock:
            _nvme_temp_history.append(temp)
            if len(_nvme_temp_history) > NVME_TEMP_MAX_HISTORY:
                _nvme_temp_history.pop(0)

def get_nvme_temperature_fields():
    """获取 NVMe 温度统计：当前 / 最低 / 平均 / 最高"""
    current = _read_nvme_temp_raw()
    with _nvme_temp_lock:
        if _nvme_temp_history:
            return {
                'current': current,
                'min': round(min(_nvme_temp_history), 1),
                'avg': round(sum(_nvme_temp_history) / len(_nvme_temp_history), 1),
                'max': round(max(_nvme_temp_history), 1)
            }
    # 还没有历史数据，返回当前值填充
    if current is not None:
        return {'current': current, 'min': current, 'avg': current, 'max': current}
    return {'current': None, 'min': None, 'avg': None, 'max': None}

def get_cpu_percent():
    """获取 CPU 使用率 - 简化版"""
    try:
        # 读取 /proc/stat 两次
        with open('/proc/stat', 'r') as f:
            stats1 = f.readline().split()
        time.sleep(0.5)
        with open('/proc/stat', 'r') as f:
            stats2 = f.readline().split()
        
        # 计算 CPU 时间差值
        cpu1 = [int(x) for x in stats1[1:]]
        cpu2 = [int(x) for x in stats2[1:]]
        
        idle1, idle2 = cpu1[3], cpu2[3]
        total1, total2 = sum(cpu1), sum(cpu2)
        
        idle_delta = idle2 - idle1
        total_delta = total2 - total1
        
        if total_delta == 0:
            return 0.0
        
        usage = 100.0 * (total_delta - idle_delta) / total_delta
        return round(usage, 1)
    except Exception as e:
        print(f"CPU error: {e}")
        return 0.0


def get_disk_usage(path):
    """获取磁盘使用情况"""
    try:
        if os.path.exists(path):
            import shutil
            usage = shutil.disk_usage(path)
            return {
                'total': usage.total,
                'used': usage.used,
                'free': usage.free,
                'percent': round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0
            }
    except:
        pass
    return None

# ─────────────────────────────────────────────
# 视频管理基础设施（Task 3.2.1）
# ─────────────────────────────────────────────

VIDEO_FOLDERS = {
    'SentryClips':    {'path': '/mnt/teslacam/TeslaCam/SentryClips',    'icon': '🚨', 'desc': '哨兵事件'},
    'SavedClips':     {'path': '/mnt/teslacam/TeslaCam/SavedClips',     'icon': '⭐', 'desc': '手动保存'},
    'RecentClips':    {'path': '/mnt/teslacam/TeslaCam/RecentClips',    'icon': '🚗', 'desc': '行车记录仪'},
}

THUMBNAIL_DIR = '/opt/radxa_data/teslausb/static/thumbnails'
THUMBNAIL_SIZE = (320, 180)

# ─────────────────────────────────────────────
# 缩略图生成（四宫格 + 时间水印，参考 TeslaUSB-CL video_preview.py）
# ─────────────────────────────────────────────

# 字体路径（优先 simhei 中文，fallback DejaVu）
_FONT_CN = '/usr/share/fonts/truetype/custom/simhei.ttf'
_FONT_EN = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

def _generate_thumbnail(event_path, event_id, video_files=None):
    """生成四宫格缩略图：2x2 (前/后+左/右) + 摄像头标签 + 时间水印
    
    参考 TeslaUSB-CL video_preview.py generate_sentry_grid_preview()
    
    Args:
        event_path: 事件文件夹路径（或 RecentClips 平铺目录）
        event_id: 事件ID
        video_files: 可选，直接指定视频文件列表（用于 RecentClips 平铺结构）
    
    Returns:
        str: 缩略图 URL 路径，失败返回 None
    """
    from PIL import Image, ImageDraw, ImageFont
    import tempfile
    
    if not os.path.exists(THUMBNAIL_DIR):
        os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    
    thumbnail_file = os.path.join(THUMBNAIL_DIR, f"{event_id}_grid.jpg")
    
    # 缓存检查
    if os.path.exists(thumbnail_file) and video_files is None:
        try:
            newest_mtime = 0
            for f in os.listdir(event_path):
                fp = os.path.join(event_path, f)
                if os.path.isfile(fp) and f.lower().endswith('.mp4'):
                    newest_mtime = max(newest_mtime, os.path.getmtime(fp))
            if newest_mtime > 0 and os.path.getmtime(thumbnail_file) >= newest_mtime:
                return f"/thumbnails/{event_id}_grid.jpg"
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
    
    frames = {}  # cam_key -> (PIL.Image, label)
    
    for cam_key, (cam_label, need_flip) in camera_map.items():
        video_path = None
        
        if video_files:
            # RecentClips 模式：从提供的文件列表中查找
            for vf in video_files:
                fname_lower = os.path.basename(vf).lower()
                if f'-{cam_key}' in fname_lower:
                    video_path = vf
                    break
                if cam_key in ('left', 'right') and f'-{cam_key}_repeater' in fname_lower:
                    video_path = vf
                    break
        else:
            # 事件文件夹模式：扫描目录
            for fname in sorted(os.listdir(event_path)):
                if fname.lower().endswith('.mp4'):
                    if f'-{cam_key}' in fname.lower():
                        video_path = os.path.join(event_path, fname)
                        break
                    if cam_key in ('left', 'right') and f'-{cam_key}_repeater' in fname.lower():
                        video_path = os.path.join(event_path, fname)
                        break
        
        if not video_path:
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
        print(f"[Thumbnail] {event_id} 四宫格生成完成 ({size_kb}KB)")
        
        return f"/thumbnails/{event_id}_grid.jpg"
    
    except Exception as e:
        print(f"[Thumbnail] 四宫格生成失败 {event_id}: {e}")
        return None

def _format_size(size_bytes):
    """格式化字节大小（供视频模板使用）"""
    if not size_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"

def _to_local_time(ts_str):
    """格式化时间戳字符串用于显示（Tesla 文件名已是本地时间）
    
    输入: '2026-05-17 22-30-37' → 输出: '2026-05-17 22:30:37'
    如果解析失败，返回原始字符串。
    """
    try:
        parts = ts_str.split(' ')
        if len(parts) == 2:
            normalized = f"{parts[0]} {parts[1].replace('-', ':')}"
        else:
            normalized = ts_str.replace('-', ':')
        dt = datetime.strptime(normalized, '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return ts_str

def _scan_video_folder(folder_type):
    """扫描视频文件夹，返回事件列表（Task 3.2.1）
    
    支持两种结构：
    - 事件文件夹结构（SentryClips/SavedClips）: 每个子目录是一个事件
    - 平铺结构（RecentClips）: 视频文件按时间戳前缀分组
    """
    if folder_type not in VIDEO_FOLDERS:
        return []
    
    folder_path = VIDEO_FOLDERS[folder_type]['path']
    events = []
    
    try:
        if not os.path.exists(folder_path):
            return events
        
        items = os.listdir(folder_path)
        is_flat = (folder_type == 'RecentClips')
        
        if is_flat:
            # 平铺结构：按文件名前缀分组（YYYY-MM-DD_HH-MM-SS）
            sessions = {}
            for fname in items:
                if not fname.lower().endswith('.mp4'):
                    continue
                fpath = os.path.join(folder_path, fname)
                if not os.path.isfile(fpath):
                    continue
                # 提取时间戳
                prefix = fname.split('-front')[0].split('-left')[0].split('-right')[0].split('-back')[0]
                # 安全取值
                ts_parts = prefix.split('_')
                if len(ts_parts) >= 2:
                    session_id = f"{ts_parts[0]}_{ts_parts[1][:8]}"  # YYYY-MM-DD_HH-MM-SS
                else:
                    session_id = prefix[:19] if len(prefix) >= 19 else prefix
                
                if session_id not in sessions:
                    local_ts = _to_local_time(session_id.replace('_', ' '))
                    sessions[session_id] = {
                        'id': session_id,
                        'name': local_ts,
                        'timestamp': local_ts,
                        'file_count': 0,
                        'total_size': 0,
                        'uploaded': False,
                        'nas_path': '',
                        'thumbnail': f"/thumbnails/{session_id}_grid.jpg"
                    }
                try:
                    fsize = os.path.getsize(fpath)
                except:
                    fsize = 0
                sessions[session_id]['file_count'] += 1
                sessions[session_id]['total_size'] += fsize
            
            events = list(sessions.values())
        else:
            # 事件文件夹结构：每个子目录是一个事件
            for event_folder in sorted(items, reverse=True):
                event_path = os.path.join(folder_path, event_folder)
                if not os.path.isdir(event_path):
                    continue
                
                event_id = event_folder
                videos = []
                try:
                    for vf in os.listdir(event_path):
                        vpath = os.path.join(event_path, vf)
                        if os.path.isfile(vpath) and vf.lower().endswith('.mp4'):
                            try:
                                videos.append({'name': vf, 'size': os.path.getsize(vpath)})
                            except:
                                videos.append({'name': vf, 'size': 0})
                except:
                    pass
                
                if not videos:
                    continue
                
                total_size = sum(v['size'] for v in videos)
                
                local_ts = _to_local_time(event_folder.replace('_', ' '))
                events.append({
                    'id': event_id,
                    'name': local_ts,
                    'timestamp': local_ts,
                    'file_count': len(videos),
                    'total_size': total_size,
                    'uploaded': False,
                    'nas_path': '',
                    'thumbnail': f"/thumbnails/{event_id}_grid.jpg",
                    'videos': videos
                })
        
        # 按时间戳倒序
        events.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
    except Exception as e:
        print(f"[VideoScan] 扫描失败 {folder_type}: {e}")
    
    return events

def get_folders():
    """返回视频文件夹定义（供视频页面下拉选择器使用）"""
    return VIDEO_FOLDERS

def get_video_stats(folder_type):
    """获取指定文件夹类型的统计信息"""
    events = _scan_video_folder(folder_type)
    total_events = len(events)
    uploaded_count = sum(1 for e in events if e.get('uploaded'))
    total_size = sum(e.get('total_size', 0) for e in events)
    return {
        'total_events': total_events,
        'uploaded_count': uploaded_count,
        'total_size': _format_size(total_size)
    }

def get_wecom_status():
    """获取企业微信状态 — 从配置文件读取实际状态"""
    status = {
        'configured': False,
        'bots': [],
        'last_push': None,
        'error': None
    }
    try:
        config_path = '/opt/radxa_data/teslausb/config/sentry.json'
        if not os.path.exists(config_path):
            status['error'] = '配置文件不存在'
            return status
        
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        bots = []
        if config.get('wecom_sentry_webhook_key'):
            key = config['wecom_sentry_webhook_key']
            bots.append({
                'name': '哨兵事件',
                'key_preview': key[:8] + '...',
                'key_suffix': key[-6:],
                'desc': '哨兵模式触发时推送事件通知（含缩略图+位置）',
                'configured': True
            })
        if config.get('wecom_status_webhook_key'):
            key = config['wecom_status_webhook_key']
            bots.append({
                'name': '系统通知',
                'key_preview': key[:8] + '...',
                'key_suffix': key[-6:],
                'desc': '系统状态变更、开机通知、异常告警',
                'configured': True
            })
        if config.get('wecom_boot_webhook_key'):
            key = config['wecom_boot_webhook_key']
            bots.append({
                'name': '启动通知',
                'key_preview': key[:8] + '...',
                'key_suffix': key[-6:],
                'desc': '设备启动时推送上线通知',
                'configured': True
            })
        
        status['bots'] = bots
        status['configured'] = len(bots) > 0
        
        log_path = '/opt/radxa_data/teslausb/logs/wecom_push.log'
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r') as f:
                    lines = f.readlines()
                    if lines:
                        last_line = lines[-1].strip()
                        if last_line.startswith('[') and ']' in last_line:
                            status['last_push'] = last_line.split(']')[0][1:]
            except:
                pass
    except Exception as e:
        status['error'] = str(e)
    
    return status

def get_queue_status():
    """获取上传队列状态（修复 /upload 500 错误）"""
    # 模拟数据（实际应该从上传管理器获取）
    queue = {
        'active': [],
        'pending': [],
        'completed': [],
        'failed': []
    }
    
    return queue

def get_queue_counts():
    """获取上传队列数量统计"""
    queue = get_queue_status()
    return {
        'active_count': len(queue.get('active', [])),
        'pending_count': len(queue.get('pending', [])),
        'completed_count': len(queue.get('completed', [])),
        'failed_count': len(queue.get('failed', []))
    }

def fmt_bytes(b):
    """格式化字节数"""
    if not b:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


def get_template_context():
    """获取所有模板需要的公共变量"""
    wifi_info = get_wifi_info()
    queue = get_queue_status()
    counts = get_queue_counts()
    disks = get_all_disks()
    
    return {
        'service': get_service_status(),
        'sys_stats': get_system_stats(),
        'wifi': wifi_info,
        'current': wifi_info,
        'ip_info': get_ip_info(),
        'disk_total': get_disk_usage('/'),
        'disk': disks,  # 新增：所有磁盘分区
        'system_uptime': get_system_uptime(),
        'now': datetime.now().strftime("%H:%M:%S"),
        'fmt_bytes': fmt_bytes,
        'folders': get_folders(),
        'wecom': get_wecom_status(),
        'queue': queue,
        'active_count': counts['active_count'],
        'pending_count': counts['pending_count'],
        'completed_count': counts['completed_count'],
        'failed_count': counts['failed_count'],
        'sentry_events': sum(len(_scan_video_folder(ft)) for ft in VIDEO_FOLDERS)
    }


# ─────────────────────────────────────────────
# 路由 - 主页面
# ─────────────────────────────────────────────

def require_auth(f):
    """认证装饰器 - 如果启用了认证，则要求登录"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 从配置文件读取是否需要认证
        config = load_config()
        if config.get('auth_enabled', False):
            # 检查 session
            if 'user' not in session:
                # API 请求返回 JSON 错误
                if request.path.startswith('/api/'):
                    return jsonify({'success': False, 'error': '需要登录'}), 401
                # 页面请求重定向到登录页
                return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function



@app.route('/')
def index():
    """主页面 - 仪表盘"""
    return render_template('dashboard.html', **get_template_context())

# 子页面路由
@app.route('/sentry')
def sentry_page():
    return render_template('sentry.html', **get_template_context())

@app.route('/videos')
def videos_page():
    """视频管理页面（Task 3.2.1 修复）"""
    folder_type = request.args.get('folder', 'SentryClips')
    if folder_type not in VIDEO_FOLDERS:
        folder_type = 'SentryClips'
    
    events = _scan_video_folder(folder_type)
    stats = get_video_stats(folder_type)
    
    return render_template(
        'videos.html',
        folders=VIDEO_FOLDERS,
        current_folder=folder_type,
        events=events,
        total_events=stats['total_events'],
        uploaded_count=stats['uploaded_count'],
        total_size=stats['total_size'],
        format_size=_format_size
    )

@app.route('/upload')
def upload_page():
    return render_template('upload_progress.html', **get_template_context())

@app.route('/wifi')
def wifi_page():
    ctx = get_template_context()
    ctx['current'] = wifi_service.get_current_wifi()
    ctx['connections'] = wifi_service.get_wifi_connections()
    ctx['wifi_status'] = wifi_service.get_wifi_status()
    return render_template('wifi.html', **ctx)

@app.route('/media')
def media_page():
    return render_template('media.html', **get_template_context())

@app.route('/logs')
def logs_page():
    return render_template('logs.html', **get_template_context())

@app.route('/analytics')
def analytics_page():
    return render_template('analytics.html', **get_template_context())

@app.route('/system')
def system_page():
    return render_template('system.html', **get_template_context())

@app.route('/boombox')
def boombox_page():
    return render_template('boombox.html', **get_template_context())

@app.route('/lightshow')
def lightshow_page():
    return render_template('lightshow.html', **get_template_context())

@app.route('/wraps')
def wraps_page():
    return render_template('wraps.html', **get_template_context())

@app.route('/lockchime')
def lockchime_page():
    ctx = get_template_context()
    ctx['current_chime'] = _get_active_chime()
    ctx['holiday_chime'] = _get_holiday_chime()
    return render_template('lockchime.html', **ctx)

# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────

@app.route('/api/system/stats')
def api_system_stats():
    """API: 获取系统统计信息"""
    try:
        return jsonify({
            'success': True,
            'time': datetime.now().strftime("%H:%M:%S"),
            'service': get_service_status(),
            'sys_stats': get_system_stats(),
            'wifi': get_wifi_info(),
            'ip': get_ip_info(),
            'disk_total': get_disk_usage('/'),
        'disk': get_all_disks()
    })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─────────────────────────────────────────────
# WiFi 管理 API 路由
# ─────────────────────────────────────────────

@app.route('/api/wifi/scan')
def wifi_scan():
    """扫描周边可用 WiFi"""
    try:
        networks = wifi_service.get_available_networks(rescan=True)
        return jsonify({"success": True, "networks": networks})
    except Exception as e:
        return jsonify({"success": False, "networks": [], "error": str(e)})


@app.route('/api/wifi/switch', methods=['POST'])
def wifi_switch():
    """切换到指定 WiFi（含自动回档）"""
    data = request.get_json(silent=True) or request.form
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()
    if not ssid:
        return jsonify({"success": False, "message": "SSID 不能为空"})
    try:
        result = wifi_service.switch_wifi(ssid, password)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/wifi/priority', methods=['POST'])
def wifi_priority():
    """修改连接优先级"""
    data = request.get_json(silent=True) or request.form
    con_name = (data.get("con_name") or "").strip()
    try:
        priority = int(data.get("priority", 5))
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "优先级必须为整数"})
    if not con_name:
        return jsonify({"success": False, "message": "连接名不能为空"})
    result = wifi_service.update_wifi_priority(con_name, priority)
    return jsonify(result)


@app.route('/api/wifi/autoconnect', methods=['POST'])
def wifi_autoconnect():
    """切换连接的自动连接开关"""
    data = request.get_json(silent=True) or request.form
    con_name = (data.get("con_name") or "").strip()
    autoconnect = data.get("autoconnect")
    if not con_name:
        return jsonify({"success": False, "message": "连接名不能为空"})
    if isinstance(autoconnect, str):
        autoconnect = autoconnect.lower() in ("true", "1", "yes", "on")
    elif not isinstance(autoconnect, bool):
        return jsonify({"success": False, "message": "autoconnect 参数必须为布尔值"})
    result = wifi_service.update_connection_autoconnect(con_name, autoconnect)
    return jsonify(result)


@app.route('/api/wifi/rename', methods=['POST'])
def wifi_rename():
    """修改连接名称"""
    data = request.get_json(silent=True) or request.form
    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    if not old_name:
        return jsonify({"success": False, "message": "原连接名不能为空"})
    if not new_name:
        return jsonify({"success": False, "message": "新连接名不能为空"})
    result = wifi_service.update_connection_name(old_name, new_name)
    return jsonify(result)


@app.route('/api/wifi/delete', methods=['POST'])
def wifi_delete():
    """删除 WiFi 连接配置"""
    data = request.get_json(silent=True) or request.form
    con_name = (data.get("con_name") or "").strip()
    if not con_name:
        return jsonify({"success": False, "message": "连接名不能为空"})
    result = wifi_service.delete_wifi_connection(con_name)
    return jsonify(result)


@app.route('/api/wifi/status/dismiss', methods=['POST'])
def wifi_status_dismiss():
    """清除 WiFi 切换状态提示"""
    wifi_service.clear_wifi_status()
    return jsonify({"success": True})


@app.route('/api/wifi/details')
def wifi_connection_details():
    """API: 获取 WiFi 连接详情"""
    details = wifi_service.get_connection_details()
    return jsonify({"success": True, "data": details})


# ── AP 管理 API ──

@app.route('/api/ap/status')
def ap_status_api():
    """API: 获取 AP 状态"""
    status = wifi_service.get_ap_status()
    return jsonify({"success": True, **status})


@app.route('/api/ap/config', methods=['GET', 'POST'])
def ap_config_api():
    """API: 获取/设置 AP 配置"""
    if request.method == 'GET':
        config = wifi_service.get_ap_config()
        return jsonify({"success": True, "ssid": config.get("ssid"), "enabled": config.get("enabled", True)})
    
    data = request.get_json() or {}
    ssid = data.get("ssid", "").strip()
    passphrase = data.get("passphrase", "")
    result = wifi_service.set_ap_config(ssid, passphrase)
    return jsonify(result)


@app.route('/api/ap/mode', methods=['GET', 'POST'])
def ap_mode_api():
    """API: 获取/设置 AP 强制模式"""
    if request.method == 'GET':
        mode = wifi_service.get_ap_force_mode()
        return jsonify({"success": True, "mode": mode})
    
    data = request.get_json() or {}
    mode = data.get("mode", "auto")
    result = wifi_service.set_ap_force_mode(mode)
    return jsonify(result)


@app.route('/api/ap/control', methods=['POST'])
def ap_control_api():
    """API: 手动控制 AP 启停"""
    data = request.get_json() or {}
    action = data.get("action", "")
    
    if action == "start":
        result = wifi_service.start_ap()
    elif action == "stop":
        result = wifi_service.stop_ap()
    else:
        return jsonify({"success": False, "message": "无效的操作"}), 400
    
    return jsonify(result)


# ── 视频同步 API ──

@app.route('/api/sync/status')
def sync_status_api():
    """获取同步状态"""
    try:
        status = sync_service.get_sync_status()
        return jsonify({"success": True, **status})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/sync/history')
def sync_history_api():
    """获取同步历史"""
    try:
        history = sync_service.get_sync_history()
        return jsonify({"success": True, "history": history})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/sync/trigger', methods=['POST'])
def sync_trigger_api():
    """手动触发同步"""
    try:
        result = sync_service.run_sync()
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/sync/config', methods=['GET', 'POST'])
def sync_config_api():
    """获取/更新同步配置"""
    if request.method == 'GET':
        cfg = sync_service.load_config()
        # 隐藏密码
        safe = {k: v for k, v in cfg.items() if not k.startswith('_')}
        return jsonify({"success": True, "config": safe})
    
    try:
        data = request.get_json() or {}
        current = sync_service.load_config()
        for key in ('enabled', 'nas_ip', 'nas_share', 'nas_user', 'home_ssid',
                     'retention_days', 'delete_after_sync', 'notify_wechat', 'nas_domain'):
            if key in data:
                current[key] = data[key]
        if '_nas_pass' in data and data['_nas_pass']:
            current['_nas_pass'] = data['_nas_pass']
        result = sync_service.save_config(current)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# Mode switch API
MODE_FILE = '/opt/radxa_data/teslausb/data/mode.txt'


def get_cpu_temperature():
    temps = []
    try:
        for i in range(10):
            tf = '/sys/class/thermal/thermal_zone' + str(i) + '/temp'
            if os.path.exists(tf):
                try:
                    with open(tf, 'r') as f:
                        t = int(f.read().strip()) / 1000.0
                        if 0 < t < 150:
                            temps.append(t)
                except:
                    pass
        if temps:
            return {
                'current': round(temps[0], 1),
                'min': round(min(temps), 1),
                'avg': round(sum(temps)/len(temps), 1),
                'max': round(max(temps), 1)
            }
    except:
        pass
    return {'current': None, 'min': None, 'avg': None, 'max': None}


def get_memory_info():
    try:
        info = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(':')] = int(parts[1])
        
        mt = info.get('MemTotal', 0)
        ma = info.get('MemAvailable', info.get('MemFree', 0))
        mu = mt - ma
        
        st = info.get('SwapTotal', 0)
        sf = info.get('SwapFree', 0)
        su = st - sf
        
        return {
            'mem_total_mb': round(mt/1024, 1),
            'mem_used_mb': round(mu/1024, 1),
            'mem_percent': round(mu/mt*100, 1) if mt > 0 else 0,
            'swap_total_mb': round(st/1024, 1),
            'swap_used_mb': round(su/1024, 1),
            'swap_percent': round(su/st*100, 1) if st > 0 else 0
        }
    except:
        return {'mem_total_mb': 0, 'mem_used_mb': 0, 'mem_percent': 0,
                'swap_total_mb': 0, 'swap_used_mb': 0, 'swap_percent': 0}


def get_all_disks():
    disks = {}
    
    try:
        import shutil
        u = shutil.disk_usage('/')
        disks['system'] = {
            'mount': '/',
            'total': u.total,
            'used': u.used,
            'free': u.free,
            'percent': round(u.used/u.total*100, 1) if u.total > 0 else 0,
            'mounted': True
        }
    except:
        pass
    
    for mp, name in [('/mnt/teslacam', 'TeslaCam'), ('/mnt/music', 'Music'),
                      ('/mnt/lightshow', 'LightShow'), ('/mnt/boombox', 'Boombox')]:
        is_mounted = os.path.ismount(mp)
        try:
            if is_mounted:
                import shutil
                u = shutil.disk_usage(mp)
                disks[name.lower()] = {
                    'mount': mp,
                    'total': u.total,
                    'used': u.used,
                    'free': u.free,
                    'percent': round(u.used/u.total*100, 1) if u.total > 0 else 0,
                    'mounted': True
                }
            elif os.path.exists(mp):
                # 目录存在但未挂载（如 Present 模式下的非 TeslaCam 分区）
                cache_file = '/opt/radxa_data/teslausb/data/disk_cache.json'
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'r') as f:
                            cache = json.load(f)
                        if name.lower() in cache:
                            cached = cache[name.lower()]
                            disks[name.lower()] = {**cached, 'mounted': False, 'mount': mp}
                            continue
                    except:
                        pass
                disks[name.lower()] = {
                    'mount': mp,
                    'total': 0,
                    'used': 0,
                    'free': 0,
                    'percent': 0,
                    'mounted': False
                }
        except:
            pass
    
    return disks


def _save_disk_cache():
    """保存当前磁盘状态到缓存文件（Edit 模式下调用，合并模式保留未挂载分区）"""
    cache_file = '/opt/radxa_data/teslausb/data/disk_cache.json'
    cache_dir = os.path.dirname(cache_file)
    os.makedirs(cache_dir, exist_ok=True)
    
    # 先读取现有缓存
    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                cache = json.load(f)
        except:
            pass
    
    # 更新已挂载分区的数据
    for mp, name in [('/mnt/teslacam', 'teslacam'), ('/mnt/music', 'music'),
                      ('/mnt/lightshow', 'lightshow'), ('/mnt/boombox', 'boombox')]:
        try:
            if os.path.ismount(mp):
                import shutil
                u = shutil.disk_usage(mp)
                cache[name] = {
                    'total': u.total,
                    'used': u.used,
                    'free': u.free,
                    'percent': round(u.used/u.total*100, 1) if u.total > 0 else 0,
                    'total_fmt': _format_size(u.total),
                    'used_fmt': _format_size(u.used),
                    'free_fmt': _format_size(u.free),
                    'fs_type': 'exFAT',
                    'device': '/dev/nvme0n1p' + str({'teslacam': 2, 'music': 3, 'lightshow': 4, 'boombox': 5}[name])
                }
        except:
            pass
    
    if cache:
        with open(cache_file, 'w') as f:
            json.dump(cache, f)
        print(f"[DiskCache] 已保存 {len(cache)} 个分区状态到 {cache_file}")


def get_network_bytes():
    """读取网络接口 RX/TX 字节数（用于吞吐量计算）"""
    net = {'net_rx': 0, 'net_tx': 0, 'net_iface': 'wlan0'}
    try:
        with open('/proc/net/dev', 'r') as f:
            for line in f:
                # 跳过头部
                if ':' not in line:
                    continue
                iface = line.split(':')[0].strip()
                # 优先 wlan0，其次 eth0
                if iface in ('wlan0', 'eth0', 'tailscale0'):
                    parts = line.split(':')[1].split()
                    if len(parts) >= 9:
                        rx_bytes = int(parts[0])
                        tx_bytes = int(parts[8])
                        if iface == 'wlan0' or (net['net_iface'] != 'wlan0' and iface == 'eth0'):
                            net['net_rx'] = rx_bytes
                            net['net_tx'] = tx_bytes
                            net['net_iface'] = iface
    except:
        pass
    return net

def get_system_stats():
    cpu_temp = get_cpu_temperature()
    mem_info = get_memory_info()
    net_info = get_network_bytes()
    
    load = [0.0, 0.0, 0.0]
    try:
        with open('/proc/loadavg', 'r') as f:
            load = [float(x) for x in f.read().split()[:3]]
    except:
        pass
    
    system_uptime = get_system_uptime()
    
    return {
        'cpu_percent': get_cpu_percent(),
        'cpu_temp': cpu_temp['current'],
        'system_uptime': system_uptime,
        'cpu_temp_min': cpu_temp['min'],
        'cpu_temp_avg': cpu_temp['avg'],
        'cpu_temp_max': cpu_temp['max'],
        'mem_used_mb': mem_info['mem_used_mb'],
        'mem_total_mb': mem_info['mem_total_mb'],
        'mem_percent': mem_info['mem_percent'],
        'swap_used_mb': mem_info['swap_used_mb'],
        'swap_total_mb': mem_info['swap_total_mb'],
        'swap_percent': mem_info['swap_percent'],
        'load_1min': load[0],
        'load_5min': load[1],
        'load_15min': load[2],
        'net_rx': net_info['net_rx'],
        'net_tx': net_info['net_tx'],
        'net_iface': net_info['net_iface'],
        'nvme_temp_fields': get_nvme_temperature_fields()
    }


# ======================================================================
# Task #14 添加 - USB 模式切换 API
# ======================================================================
"""
Task #14 添加内容：USB 模式切换 API 端点
将此代码添加到 app.py 的路由定义部分（建议在 /api 路由区域）
"""

import os
import subprocess

# ─────────────────────────────────────────────
# USB 模式切换 API
# ─────────────────────────────────────────────

@app.route('/api/mode/status')
def get_mode_status():
    """获取当前模式 - 使用 flag 文件"""
    mode_file = '/tmp/teslausb_mode'
    
    try:
        if os.path.exists(mode_file):
            with open(mode_file, 'r') as f:
                mode = f.read().strip()
                if mode in ['present', 'edit']:
                    return jsonify({'success': True, 'mode': mode})
        
        # 默认返回 edit
        return jsonify({'success': True, 'mode': 'edit'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/mode/switch', methods=['POST'])
def switch_mode():
    app.logger.warning("🔍🔍🔍 进入新 switch_mode() 函数!!!")
    """真正执行模式切换 - 调用底层脚本"""
    import subprocess
    import os
    
    data = request.get_json()
    mode = data.get('mode', '').lower()
    
    if mode not in ['present', 'edit']:
        return jsonify({'success': False, 'error': f'无效模式: {mode}，仅支持 present 或 edit'}), 400
    
    try:
        # 根据模式选择脚本
        if mode == 'present':
            script_path = '/opt/radxa_data/present_usb.sh'
            mode_name = 'Present Mode (连接 Tesla)'
        else:
            script_path = '/opt/radxa_data/edit_usb.sh'
            mode_name = 'Edit Mode (网络访问)'
        
        # 检查脚本是否存在
        if not os.path.exists(script_path):
            return jsonify({
                'success': False,
                'error': f'切换脚本不存在: {script_path}'
            }), 500
        
        # 记录日志
        app.logger.info(f"🔄 开始切换到 {mode_name}...")
        
        # 执行切换脚本
        result = subprocess.run(
            ['bash', script_path],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            app.logger.info(f"✅ 成功切换到 {mode_name}")
            
            # 如果是切换到 Edit 模式，立即保存当前磁盘状态到缓存
            if mode == 'edit':
                try:
                    _save_disk_cache()
                except Exception as e:
                    app.logger.warning(f"⚠️ 保存磁盘缓存失败: {e}")
            
            return jsonify({
                'success': True,
                'mode': mode,
                'message': f'已切换到 {mode_name}'
            })
        else:
            error_msg = result.stderr or result.stdout or '未知错误'
            app.logger.error(f"❌ 切换失败: {error_msg}")
            return jsonify({
                'success': False,
                'error': error_msg[-500:]
            }), 500
            
    except Exception as e:
        app.logger.error(f"❌ 切换异常: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ─────────────────────────────────────────────
# 视频管理 API（Task 3.2.1 新增）
# ─────────────────────────────────────────────

@app.route('/api/videos/delete', methods=['POST'])
def api_videos_delete():
    """API: 删除事件文件夹"""
    data = request.get_json() or {}
    folder_type = data.get('folder', '').strip()
    event_id = data.get('event_id', '').strip()
    
    if not folder_type or folder_type not in VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': '无效的文件夹类型'}), 400
    if not event_id:
        return jsonify({'success': False, 'error': '缺少事件ID'}), 400
    if '..' in event_id or '/' in event_id:
        return jsonify({'success': False, 'error': '无效的事件ID'}), 400
    
    try:
        import shutil
        folder_path = VIDEO_FOLDERS[folder_type]['path']
        
        if folder_type == 'RecentClips':
            # 平铺结构：删除匹配前缀的文件
            deleted = 0
            if os.path.exists(folder_path):
                for fname in os.listdir(folder_path):
                    if fname.startswith(event_id) and fname.lower().endswith('.mp4'):
                        os.remove(os.path.join(folder_path, fname))
                        deleted += 1
            if deleted > 0:
                return jsonify({'success': True, 'message': f'已删除 {deleted} 个文件', 'deleted_count': deleted})
            return jsonify({'success': False, 'error': '未找到文件'}), 404
        else:
            # 事件文件夹结构：删除整个目录
            event_path = os.path.join(folder_path, event_id)
            if os.path.exists(event_path) and os.path.isdir(event_path):
                shutil.rmtree(event_path)
                return jsonify({'success': True, 'message': '事件已删除'})
            return jsonify({'success': False, 'error': '事件不存在'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/videos/list')
def api_videos_list():
    """API: 获取视频事件列表（JSON）"""
    folder_type = request.args.get('folder', 'SentryClips')
    if folder_type not in VIDEO_FOLDERS:
        return jsonify({'success': False, 'error': '无效的文件夹类型'}), 400
    
    events = _scan_video_folder(folder_type)
    return jsonify({
        'success': True,
        'folder': folder_type,
        'events': events,
        'total': len(events)
    })

@app.route('/videos/event/<folder_type>/<event_id>')
def video_event_detail(folder_type, event_id):
    """事件详情页 - 显示事件中的所有视频文件"""
    if folder_type not in VIDEO_FOLDERS:
        return "无效的文件夹类型", 404
    
    folder_path = VIDEO_FOLDERS[folder_type]['path']
    
    videos = []
    if folder_type == 'RecentClips':
        # 平铺结构：列出匹配前缀的文件
        if os.path.exists(folder_path):
            for fname in sorted(os.listdir(folder_path)):
                if fname.startswith(event_id) and fname.lower().endswith('.mp4'):
                    fpath = os.path.join(folder_path, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                    except:
                        fsize = 0
                    videos.append({
                        'name': fname,
                        'size': fsize,
                        'size_fmt': _format_size(fsize),
                        'path': f'/videos/play/{folder_type}/{fname}'
                    })
    else:
        # 事件文件夹结构
        event_path = os.path.join(folder_path, event_id)
        if os.path.exists(event_path) and os.path.isdir(event_path):
            for fname in sorted(os.listdir(event_path)):
                if fname.lower().endswith('.mp4'):
                    fpath = os.path.join(event_path, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                    except:
                        fsize = 0
                    videos.append({
                        'name': fname,
                        'size': fsize,
                        'size_fmt': _format_size(fsize),
                        'path': f'/videos/play/{folder_type}/{event_id}/{fname}'
                    })
    
    return render_template(
        'videos.html',
        folders=VIDEO_FOLDERS,
        current_folder=folder_type,
        events=[],  # 详情页不显示事件列表
        event_detail={'id': event_id, 'name': _to_local_time(event_id.replace('_', ' ')),
                      'videos': videos, 'folder': folder_type},
        total_events=0,
        uploaded_count=0,
        total_size='',
        format_size=_format_size
    )

@app.route('/videos/play/<folder_type>/<path:file_path>')
def video_play(folder_type, file_path):
    """视频播放 - 使用 Flask send_file 支持 Range 请求"""
    folder_config = VIDEO_FOLDERS.get(folder_type)
    if not folder_config:
        return "无效的文件夹类型", 404
    
    # 安全检查
    if '..' in file_path:
        return "无效的文件路径", 400
    
    base_path = folder_config['path']
    full_path = os.path.join(base_path, file_path)
    
    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        return "文件不存在", 404
    
    # 使用 send_file 支持 Range 请求（视频拖动进度条）
    from flask import send_file
    return send_file(full_path, mimetype='video/mp4')


# ═══════════════════════════════════════════════════════════════
# Task 3.2 收尾 — 13 个缺失 API 端点
# ═══════════════════════════════════════════════════════════════

# ── 日志流 (SSE) ──────────────────────────────────────────────

# 全局日志订阅者管理
_log_subscribers = []
_log_subscribers_lock = threading.Lock()

def _log_broadcaster():
    """后台线程：持续读取 journalctl 并广播到所有 SSE 订阅者"""
    import select
    proc = subprocess.Popen(
        ['sudo', '-S', 'journalctl', '-f', '-n', '50', '--no-pager', '-o', 'short-iso'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        proc.stdin.write('radxa\n')
        proc.stdin.flush()
    except:
        pass
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line.strip():
                continue
            with _log_subscribers_lock:
                dead = []
                for q in _log_subscribers:
                    try:
                        q.append(line.rstrip('\n'))
                    except:
                        dead.append(q)
                for q in dead:
                    _log_subscribers.remove(q)
    except:
        pass
    finally:
        try:
            proc.terminate()
        except:
            pass

# 启动广播线程
_log_thread = threading.Thread(target=_log_broadcaster, daemon=True)
_log_thread.start()

@app.route('/api/logs/stream')
def api_logs_stream():
    """SSE 实时日志流，支持 ?unit= 服务过滤"""
    import queue
    unit_filter = request.args.get('unit', '').strip()
    q = queue.Queue(maxsize=200)
    with _log_subscribers_lock:
        _log_subscribers.append(q)
    
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
            return ['journalctl', '-k', '-n', '50', '--no-pager', '-o', 'short-iso']
        elif unit == 'systemd':
            # systemd init (PID 1) 的所有日志
            return ['journalctl', '_PID=1', '-n', '50', '--no-pager', '-o', 'short-iso']
        elif unit == 'cron':
            # cron 可能未安装 - 检查是否有 cron 相关日志
            return ['journalctl', '_COMM=cron', '-n', '50', '--no-pager', '-o', 'short-iso']
        elif unit:
            return ['journalctl', '-u', unit, '-n', '50', '--no-pager', '-o', 'short-iso']
        else:
            return ['journalctl', '-n', '50', '--no-pager', '-o', 'short-iso']

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
            with _log_subscribers_lock:
                if q in _log_subscribers:
                    _log_subscribers.remove(q)
    
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── 系统操作 API ──────────────────────────────────────────────

@app.route('/api/system/service', methods=['POST'])
def api_system_service():
    """控制 teslausb-web 服务"""
    data = request.get_json() or {}
    action = data.get('action', '')
    if action not in ('restart', 'stop', 'start'):
        return jsonify({'success': False, 'error': f'无效操作: {action}'}), 400
    
    actions_cn = {'restart': '重启', 'stop': '停止', 'start': '启动'}
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', action, 'teslausb-web'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return jsonify({'success': True, 'message': f'服务已{actions_cn[action]}'})
        return jsonify({'success': False, 'error': result.stderr or result.stdout})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/system/reboot', methods=['POST'])
def api_system_reboot():
    """重启系统"""
    try:
        subprocess.Popen(['sudo', 'shutdown', '-r', '+1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'success': True, 'message': '系统将在1分钟后重启'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/system/shutdown', methods=['POST'])
def api_system_shutdown():
    """关机"""
    try:
        subprocess.Popen(['sudo', 'shutdown', '-h', '+1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'success': True, 'message': '系统将在1分钟后关机'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/system/wecom-test', methods=['POST'])
def api_system_wecom_test():
    """测试企业微信推送"""
    try:
        # 尝试调用 weixin_notifier（如果存在）
        script = '/home/radxa/teslausb/weixin_notifier.py'
        if os.path.exists(script):
            result = subprocess.run(
                ['python3', script, '--test'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return jsonify({'success': True, 'message': '测试推送已发送！请查看企业微信'})
            return jsonify({'success': False, 'error': result.stderr or result.stdout})
        
        # 尝试直接调用 webhook
        sentry_config = '/home/radxa/teslausb/config/sentry.json'
        if os.path.exists(sentry_config):
            with open(sentry_config, 'r') as f:
                cfg = json.load(f)
            import urllib.request
            status_key = cfg.get('wecom_status_webhook_key', '')
            if status_key:
                url = f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={status_key}'
                payload = json.dumps({
                    'msgtype': 'text',
                    'text': {'content': '🧪 TeslaUSB A7Z 测试推送\n\nWeb 管理界面测试消息发送成功！'}
                }).encode('utf-8')
                req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=10)
                return jsonify({'success': True, 'message': '测试推送已发送！请查看企业微信'})
        
        return jsonify({'success': False, 'error': '未找到推送配置（config/sentry.json 或 weixin_notifier.py）'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 系统信息 API ──────────────────────────────────────────────

@app.route('/api/system/info')
def api_system_info():
    """系统信息：发行版、内核、架构、CPU 型号、Python 版本"""
    try:
        info = {
            'distro': 'Unknown',
            'kernel': '',
            'arch': '',
            'cpu_model': '',
            'python_version': '',
            'hostname': ''
        }
        
        # 发行版
        try:
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    if line.startswith('PRETTY_NAME='):
                        info['distro'] = line.split('=', 1)[1].strip().strip('"')
                        break
        except:
            pass
        
        # 内核版本
        try:
            with open('/proc/version', 'r') as f:
                info['kernel'] = f.read().split('(')[0].strip()
        except:
            pass
        
        # 架构
        import platform
        info['arch'] = platform.machine()
        
        # CPU 型号
        try:
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
            # 尝试 x86: model name
            for line in cpuinfo.split('\n'):
                if line.startswith('model name') and ':' in line:
                    info['cpu_model'] = line.split(':', 1)[1].strip()
                    break
                if line.startswith('Model') and ':' in line:
                    info['cpu_model'] = line.split(':', 1)[1].strip()
                    break
            # ARM 回退：从 implementer/part 识别，支持 big.LITTLE
            if not info['cpu_model']:
                arm_parts = {
                    '0xc07': 'Cortex-A7', '0xc08': 'Cortex-A8', '0xc09': 'Cortex-A9',
                    '0xc0f': 'Cortex-A15', '0xc0e': 'Cortex-A17',
                    '0xd03': 'Cortex-A53', '0xd04': 'Cortex-A35',
                    '0xd05': 'Cortex-A55', '0xd07': 'Cortex-A57',
                    '0xd08': 'Cortex-A72', '0xd09': 'Cortex-A73',
                    '0xd0a': 'Cortex-A75', '0xd0b': 'Cortex-A76',
                    '0xd0c': 'Cortex-A77', '0xd0d': 'Cortex-A78',
                    '0xd41': 'Cortex-A78AE', '0xd44': 'Cortex-X1',
                    '0xd46': 'Cortex-A510', '0xd47': 'Cortex-A710',
                    '0xd48': 'Cortex-X2', '0xd49': 'Cortex-A520',
                    '0xd4a': 'Cortex-A720', '0xd4b': 'Cortex-X925',
                }
                import collections
                core_counts = collections.Counter()
                arch_val = '8'
                for line in cpuinfo.split('\n'):
                    if line.startswith('CPU part') and ':' in line:
                        p = line.split(':', 1)[1].strip()
                        core_name = arm_parts.get(p, f'ARM-0x{p}')
                        core_counts[core_name] += 1
                    if line.startswith('CPU architecture') and ':' in line:
                        arch_val = line.split(':', 1)[1].strip()
                
                arch_str = f'ARMv{int(arch_val, 16) if arch_val else "?"}-A'
                if len(core_counts) == 1:
                    name, count = list(core_counts.items())[0]
                    info['cpu_model'] = f'{arch_str} {name} ({count} cores)'
                elif len(core_counts) > 1:
                    parts = []
                    for name, count in core_counts.items():
                        parts.append(f'{name} x{count}')
                    info['cpu_model'] = f'{arch_str} {" + ".join(parts)}'
        except:
            pass
        
        # Python 版本
        import sys
        info['python_version'] = sys.version.split()[0]
        info['python'] = info['python_version']  # 兼容前端 JS 字段名
        
        # 主机名
        import socket
        try:
            info['hostname'] = socket.gethostname()
        except:
            pass
        
        # 系统运行时间
        info['uptime'] = get_system_uptime()
        
        response = {'success': True, **info}
        return jsonify(response)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/system/mounts')
def api_system_mounts():
    """系统挂载点信息（df -h + 缓存回退，支持 Present 模式下显示未挂载分区）"""
    try:
        mounts = []
        # 获取当前已挂载的文件系统
        result = subprocess.run(
            ['df', '-h', '--output=target,source,fstype,size,used,avail,pcent'],
            capture_output=True, text=True, timeout=5
        )
        mounted_paths = set()
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                for line in lines[1:]:
                    parts = line.split()
                    if len(parts) >= 7:
                        mount_point = parts[0]
                        mounted_paths.add(mount_point)
                        mounts.append({
                            'mount_point': mount_point,
                            'device': parts[1],
                            'fs_type': parts[2],
                            'total_fmt': parts[3],
                            'used_fmt': parts[4],
                            'avail': parts[5],
                            'mounted': True,
                            'mount': mount_point,
                            'source': parts[1],
                            'fstype': parts[2],
                            'size': parts[3],
                            'used': parts[4],
                            'percent': parts[6].rstrip('%')
                        })
        
        # 从缓存补充未挂载分区（Present 模式下非 teslacam 分区不可见）
        cache_file = '/opt/radxa_data/teslausb/data/disk_cache.json'
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    cache = json.load(f)
                for name, info in cache.items():
                    mp = '/mnt/' + name
                    if mp not in mounted_paths and os.path.exists(mp):
                        mounts.append({
                            'mount_point': mp,
                            'device': info.get('device', '—'),
                            'fs_type': info.get('fs_type', info.get('fstype', '—')),
                            'total_fmt': info.get('total_fmt', '—'),
                            'used_fmt': info.get('used_fmt', '—'),
                            'avail': info.get('free_fmt', '—'),
                            'mounted': False,
                            'mount': mp,
                            'source': info.get('device', '—'),
                            'fstype': info.get('fs_type', '—'),
                            'size': info.get('total_fmt', '—'),
                            'used': info.get('used_fmt', '—'),
                            'percent': str(info.get('percent', 0))
                        })
            except:
                pass
        
        return jsonify({'success': True, 'mounts': mounts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 分析数据 API ──────────────────────────────────────────────

@app.route('/api/analytics/push-health')
def api_analytics_push_health():
    """推送健康状态"""
    try:
        sentry_config = '/home/radxa/teslausb/config/sentry.json'
        bots = {}
        if os.path.exists(sentry_config):
            with open(sentry_config, 'r') as f:
                cfg = json.load(f)
            # 读取已配置的机器人
            if cfg.get('wecom_sentry_webhook_key'):
                bots['sentry'] = {
                    'name': '哨兵通知机器人',
                    'total_pushes': cfg.get('sentry_push_count', 0),
                    'success_count': cfg.get('sentry_success_count', 0),
                    'fail_count': cfg.get('sentry_fail_count', 0),
                    'last_success_time': cfg.get('sentry_last_success', 0),
                    'recent_failures': []
                }
            if cfg.get('wecom_status_webhook_key'):
                bots['status'] = {
                    'name': '状态通知机器人',
                    'total_pushes': cfg.get('status_push_count', 0),
                    'success_count': cfg.get('status_success_count', 0),
                    'fail_count': cfg.get('status_fail_count', 0),
                    'last_success_time': cfg.get('status_last_success', 0),
                    'recent_failures': []
                }
        return jsonify({'success': True, 'bots': bots})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/analytics/summary')
def api_analytics_summary():
    """哨兵事件统计摘要"""
    try:
        events = {
            'total': 0, 'uploaded': 0, 'pending': 0, 'failed': 0, 'upload_rate': 0
        }
        # 扫描视频文件夹获取事件数
        for ft in VIDEO_FOLDERS:
            evts = _scan_video_folder(ft)
            events['total'] += len(evts)
            events['uploaded'] += sum(1 for e in evts if e.get('uploaded'))
            events['pending'] += sum(1 for e in evts if not e.get('uploaded'))
        
        if events['total'] > 0:
            events['upload_rate'] = round(events['uploaded'] / events['total'] * 100, 1)
        
        # 系统健康检查
        ss = get_system_stats()
        healthy = True
        issues = []
        if ss.get('cpu_percent', 0) > 90:
            healthy = False; issues.append('CPU 使用率过高')
        if ss.get('mem_percent', 0) > 90:
            healthy = False; issues.append('内存不足')
        if ss.get('cpu_temp') and ss['cpu_temp'] > 80:
            healthy = False; issues.append('CPU 温度过高')
        
        health = {
            'healthy': healthy,
            'issues': issues,
            'metrics': {
                'cpu_load': ss.get('cpu_percent', 0),
                'memory': {
                    'used_mb': ss.get('mem_used_mb', 0),
                    'total_mb': ss.get('mem_total_mb', 0),
                    'percent': ss.get('mem_percent', 0)
                },
                'temperature': ss.get('cpu_temp'),
                'network': get_wifi_info().get('connected', False)
            }
        }
        
        return jsonify({'success': True, 'events': events, 'health': health})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/analytics/disk')
def api_analytics_disk():
    """磁盘使用详情"""
    try:
        disks = get_all_disks()
        # 转换为前端友好的格式
        result = {}
        for name, info in disks.items():
            mounted = os.path.ismount(info.get('mount', ''))
            result[name] = {
                'mounted': mounted,
                'total': info.get('total', 0),
                'used': info.get('used', 0),
                'free': info.get('free', 0),
                'percent': info.get('percent', 0),
                'mount': info.get('mount', '')
            }
        return jsonify({'success': True, 'disks': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/analytics/services')
def api_analytics_services():
    """系统服务状态列表"""
    svc_list = ['teslausb-web', 'teslausb-sentry', 'teslausb-fsck.timer', 'smbd', 'cron']
    services = {}
    try:
        for svc in svc_list:
            try:
                r = subprocess.run(['systemctl', 'is-active', svc],
                                 capture_output=True, text=True, timeout=3)
                active = r.returncode == 0 and 'active' in (r.stdout or '')
                
                # 对于 timer，获取下次触发时间
                timer_next = None
                if svc.endswith('.timer'):
                    try:
                        tr = subprocess.run(
                            ['systemctl', 'show', svc, '--property=NextElapseUSecRealtime'],
                            capture_output=True, text=True, timeout=3
                        )
                        if tr.returncode == 0:
                            raw = tr.stdout.strip().split('=', 1)[-1]
                            if raw:
                                # 微秒时间戳 → 格式化
                                ts = int(raw) / 1_000_000
                                from datetime import datetime as dt
                                timer_next = dt.fromtimestamp(ts).strftime('%m/%d %H:%M')
                    except:
                        pass
                
                services[svc] = {
                    'active': active,
                    'timer_next': timer_next
                }
            except:
                services[svc] = {'active': False, 'timer_next': None}
        return jsonify({'success': True, 'services': services})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 清理管理 API ──────────────────────────────────────────────

CLEANUP_LOG = '/opt/radxa_data/teslausb/data/cleanup_history.json'

def _get_cleanup_partitions():
    """获取分区状态（含阈值评估）"""
    parts = {}
    disks = get_all_disks()
    for name, info in disks.items():
        mount_ok = os.path.ismount(info.get('mount', ''))
        pct = info.get('percent', 0)
        if not mount_ok:
            # 有缓存数据时提供完整字段
            total = info.get('total', 0)
            if total > 0:
                parts[name] = {
                    'status': '未挂载',
                    'path': info.get('mount', ''),
                    'percent': info.get('percent', 0),
                    'total_gb': round(total / (1024**3), 1),
                    'used_gb': round(info.get('used', 0) / (1024**3), 1),
                    'free_gb': round(info.get('free', 0) / (1024**3), 1),
                    'total_fmt': _format_size(total)
                }
            else:
                parts[name] = {'status': '未挂载', 'path': info.get('mount', ''), 'percent': 0}
        else:
            status_label = '正常'
            if pct > 95:
                status_label = '紧急'
            elif pct > 90:
                status_label = '严重'
            elif pct > 85:
                status_label = '警告'
            parts[name] = {
                'status': status_label,
                'path': info.get('mount', ''),
                'percent': pct,
                'used_gb': round(info.get('used', 0) / (1024**3), 1),
                'total_gb': round(info.get('total', 0) / (1024**3), 1),
                'free_gb': round(info.get('free', 0) / (1024**3), 1)
            }
    return parts

@app.route('/api/cleanup/policies')
def api_cleanup_policies():
    """清理策略和分区状态"""
    try:
        policies = {
            'disk_thresholds': {'warning': 85, 'critical': 90},
            'retention': {'previews_days': 7, 'logs_days': 30},
            'cleanup_order': [
                '已上传成功的哨兵视频（最旧优先）',
                '过期预览图（>7天）',
                '过期临时文件（>1天）',
                '旧日志文件（>30天）',
                '未上传的旧哨兵视频（仅严重/紧急模式）'
            ],
            'partitions': _get_cleanup_partitions()
        }
        return jsonify({'success': True, 'policies': policies})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/cleanup/preview', methods=['POST'])
def api_cleanup_preview():
    """预览清理（dry-run）"""
    try:
        lines = ['=== 清理预览 (dry-run) ===', '']
        lines.append('📋 将检查以下目录:')
        
        temp_dirs = ['/tmp/teslausb_*', '/opt/radxa_data/teslausb/data/*.tmp']
        for d in temp_dirs:
            try:
                r = subprocess.run(['sh', '-c', f'find {d} -type f 2>/dev/null | wc -l'],
                                 capture_output=True, text=True, timeout=5)
                count = int(r.stdout.strip() or 0)
                if count > 0:
                    r2 = subprocess.run(['sh', '-c', f'du -sh {d} 2>/dev/null'],
                                      capture_output=True, text=True, timeout=5)
                    lines.append(f'  {d}: {count} 个文件 ({r2.stdout.strip().split()[0] if r2.stdout.strip() else "?"})')
                else:
                    lines.append(f'  {d}: 0 个文件（无需清理）')
            except:
                lines.append(f'  {d}: 检查失败')
        
        lines.append('')
        lines.append('✅ 预览完成 — 以上文件将在执行时被清理')
        return jsonify({'success': True, 'output': '\n'.join(lines)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/cleanup/execute', methods=['POST'])
def api_cleanup_execute():
    """执行清理"""
    try:
        lines = ['=== 清理执行 ===', '']
        deleted_total = 0
        freed_bytes = 0
        actions = []
        
        # 清理 /tmp/teslausb_* 临时文件
        try:
            r = subprocess.run(
                ['sh', '-c', 'find /tmp/teslausb_* -type f -mtime +1 2>/dev/null'],
                capture_output=True, text=True, timeout=10
            )
            if r.stdout.strip():
                files = r.stdout.strip().split('\n')
                for f in files:
                    try:
                        sz = os.path.getsize(f)
                        os.remove(f)
                        deleted_total += 1
                        freed_bytes += sz
                        actions.append({'type': '临时文件', 'file': f, 'size': sz})
                    except:
                        pass
                lines.append(f'🗑 清理临时文件: {len(files)} 个')
        except Exception as e:
            lines.append(f'⚠ 临时文件清理出错: {e}')
        
        if deleted_total == 0:
            lines.append('ℹ 没有需要清理的文件')
        
        lines.append(f'')
        lines.append(f'📊 总计: 删除 {deleted_total} 个文件, 释放 {_format_size(freed_bytes)}')
        
        # 记录到清理历史
        _save_cleanup_history({
            'timestamp': datetime.now().isoformat(),
            'deleted_files': deleted_total,
            'freed_bytes': freed_bytes,
            'mode': 'manual',
            'actions': actions[:20]  # 最多保留20条
        })
        
        return jsonify({
            'success': True,
            'output': '\n'.join(lines),
            'deleted_count': deleted_total,
            'freed_bytes': freed_bytes
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def _save_cleanup_history(entry):
    """保存清理历史记录"""
    try:
        os.makedirs(os.path.dirname(CLEANUP_LOG), exist_ok=True)
        history = []
        if os.path.exists(CLEANUP_LOG):
            with open(CLEANUP_LOG, 'r') as f:
                history = json.load(f)
        history.insert(0, entry)
        # 最多保留 50 条
        history = history[:50]
        with open(CLEANUP_LOG, 'w') as f:
            json.dump(history, f, indent=2)
    except:
        pass

@app.route('/api/cleanup/history')
def api_cleanup_history():
    """清理历史记录"""
    try:
        history = []
        if os.path.exists(CLEANUP_LOG):
            with open(CLEANUP_LOG, 'r') as f:
                history = json.load(f)
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 系统状态 SSE 实时推送 ─────────────────────────────────────

_stats_subscribers = []
_stats_subscribers_lock = threading.Lock()

def _stats_broadcaster():
    """后台线程：每 3 秒采集系统状态并广播到 SSE 订阅者"""
    while True:
        time.sleep(3)
        try:
            stats = {
                'time': datetime.now().strftime("%H:%M:%S"),
                'service': get_service_status(),
                'sys': get_system_stats(),
                'wifi': get_wifi_info(),
                'ip': get_ip_info(),
                'disk_total': get_disk_usage('/'),
                'disk': get_all_disks()
            }
            # 哨兵事件统计
            evt_total = 0
            for ft in VIDEO_FOLDERS:
                evts = _scan_video_folder(ft)
                evt_total += len(evts)
            stats['sentry_events'] = evt_total

            # 写入磁盘缓存供 Present 模式使用（合并模式：保留未挂载分区数据）
            try:
                cache_dir = '/opt/radxa_data/teslausb/data'
                os.makedirs(cache_dir, exist_ok=True)
                cache_file = os.path.join(cache_dir, 'disk_cache.json')
                
                # 先读取现有缓存，保留未挂载分区数据
                cache_data = {}
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'r') as f:
                            cache_data = json.load(f)
                    except:
                        pass
                
                # 更新已挂载分区的数据
                for dname in ['teslacam', 'music', 'lightshow', 'boombox']:
                    d = stats['disk'].get(dname, {})
                    if d.get('mounted'):
                        cache_data[dname] = {
                            'total': d['total'], 'used': d['used'],
                            'free': d['free'], 'percent': d['percent'],
                            'total_fmt': _format_size(d['total']),
                            'used_fmt': _format_size(d['used']),
                            'free_fmt': _format_size(d['free']),
                            'fs_type': 'exFAT',
                            'device': '/dev/nvme0n1p' + str({'teslacam': 2, 'music': 3, 'lightshow': 4, 'boombox': 5}[dname])
                        }
                
                if cache_data:
                    with open(cache_file + '.tmp', 'w') as f:
                        json.dump(cache_data, f)
                    os.replace(cache_file + '.tmp', cache_file)
            except:
                pass

            with _stats_subscribers_lock:
                dead = []
                for q in _stats_subscribers:
                    try:
                        q.put(stats)
                    except:
                        dead.append(q)
                for q in dead:
                    _stats_subscribers.remove(q)
        except:
            pass

# ─────────────────────────────────────────────
# Lock Chime API - Task 4.5
# ─────────────────────────────────────────────

LOCKCHIME_DIR = "/mnt/boombox/lock_chimes"
LOCKCHIME_ACTIVE = "/mnt/boombox/LockChime.wav"

# 节日定义：(月, 日) 开始, (月, 日) 结束, 文件名
HOLIDAYS = [
    ((12, 20), (12, 31), "xmas.wav", "🎄 圣诞"),
    ((10, 24), (10, 31), "halloween.wav", "🎃 万圣节"),
    ((1, 1),   (1, 3),   "newyear.wav", "🎆 新年"),
]

def _get_active_chime():
    """获取当前激活的 Lock Chime"""
    if os.path.exists(LOCKCHIME_ACTIVE):
        stat = os.stat(LOCKCHIME_ACTIVE)
        return {
            'active': True,
            'filename': 'LockChime.wav',
            'size': _format_size(stat.st_size),
            'modified': datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        }
    return {'active': False}

def _get_holiday_chime():
    """检查当前是否在节日范围内"""
    now = datetime.now()
    for (s_m, s_d), (e_m, e_d), filename, label in HOLIDAYS:
        start = datetime(now.year, s_m, s_d)
        end = datetime(now.year, e_m, e_d)
        if start <= now <= end:
            return {
                'in_holiday': True,
                'label': label,
                'filename': filename,
                'path': os.path.join(LOCKCHIME_DIR, filename),
                'exists': os.path.exists(os.path.join(LOCKCHIME_DIR, filename))
            }
    return {'in_holiday': False}

def _list_lockchimes():
    """列出所有 Lock Chime 文件"""
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
                files.append({
                    'filename': fn,
                    'size': _format_size(stat.st_size),
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    'active': is_active
                })
    return files


@app.route('/api/lockchime/list')
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


@app.route('/api/lockchime/upload', methods=['POST'])
def lockchime_upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': '文件名为空'}), 400
    if not file.filename.lower().endswith('.wav'):
        return jsonify({'success': False, 'error': f'仅支持 WAV 格式'}), 400
    try:
        os.makedirs(LOCKCHIME_DIR, exist_ok=True)
        save_path = os.path.join(LOCKCHIME_DIR, file.filename)
        file.save(save_path)
        app.logger.info(f"🔔 [LockChime] 上传: {file.filename}")
        return jsonify({'success': True, 'filename': file.filename,
                        'message': f'{file.filename} 已上传'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/lockchime/delete', methods=['POST'])
def lockchime_delete():
    data = request.get_json() or {}
    filename = data.get('filename', '')
    if not filename or '..' in filename or '/' in filename:
        return jsonify({'success': False, 'error': '无效的文件名'}), 400
    fp = os.path.join(LOCKCHIME_DIR, filename)
    if not os.path.exists(fp):
        return jsonify({'success': False, 'error': '文件不存在'}), 404
    try:
        # 如果删除的是激活文件，先清理 LockChime.wav
        if os.path.exists(LOCKCHIME_ACTIVE):
            try:
                if os.path.samefile(fp, LOCKCHIME_ACTIVE):
                    os.remove(LOCKCHIME_ACTIVE)
            except:
                pass
        os.remove(fp)
        app.logger.info(f"🔔 [LockChime] 删除: {filename}")
        return jsonify({'success': True, 'message': f'{filename} 已删除'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/lockchime/activate', methods=['POST'])
def lockchime_activate():
    data = request.get_json() or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'success': False, 'error': '请指定文件'}), 400
    src = os.path.join(LOCKCHIME_DIR, filename)
    if not os.path.exists(src):
        return jsonify({'success': False, 'error': '文件不存在'}), 404
    try:
        import shutil
        # 如果 LockChime.wav 是指向其他文件的软链接，先删除
        if os.path.islink(LOCKCHIME_ACTIVE):
            os.unlink(LOCKCHIME_ACTIVE)
        elif os.path.exists(LOCKCHIME_ACTIVE):
            os.remove(LOCKCHIME_ACTIVE)
        shutil.copy2(src, LOCKCHIME_ACTIVE)
        app.logger.info(f"🔔 [LockChime] 激活: {filename} → LockChime.wav")
        return jsonify({'success': True, 'filename': filename,
                        'message': f'已激活 {filename} 为 Lock Chime'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/lockchime/holiday-apply', methods=['POST'])
def lockchime_holiday_apply():
    """手动应用节日音效"""
    holiday = _get_holiday_chime()
    if not holiday['in_holiday']:
        return jsonify({'success': False, 'error': '当前不在节日范围内'}), 400
    if not holiday['exists']:
        return jsonify({'success': False, 'error': f"节日音效 {holiday['filename']} 不存在，请先上传"}), 404
    try:
        import shutil
        src = holiday['path']
        if os.path.islink(LOCKCHIME_ACTIVE):
            os.unlink(LOCKCHIME_ACTIVE)
        elif os.path.exists(LOCKCHIME_ACTIVE):
            os.remove(LOCKCHIME_ACTIVE)
        shutil.copy2(src, LOCKCHIME_ACTIVE)
        app.logger.info(f"🔔 [LockChime] 节日切换: {holiday['label']} {holiday['filename']}")
        return jsonify({'success': True,
                        'message': f"已切换为 {holiday['label']} 音效"})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 媒体管理 API（Boombox / Lightshow / Wraps / Music）
# ═══════════════════════════════════════════════════════════════

def _media_disk_info(mount_point):
    """获取媒体分区磁盘信息"""
    try:
        import shutil
        if os.path.ismount(mount_point):
            u = shutil.disk_usage(mount_point)
            return {
                'total': u.total, 'used': u.used, 'free': u.free,
                'percent': round(u.used / u.total * 100, 1) if u.total > 0 else 0
            }
    except:
        pass
    # 回退到缓存
    cache_file = '/opt/radxa_data/teslausb/data/disk_cache.json'
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                cache = json.load(f)
            name = os.path.basename(mount_point)
            if name in cache:
                c = cache[name]
                return {'total': c['total'], 'used': c['used'], 'free': c['free'], 'percent': c['percent']}
    except:
        pass
    return None


# ── Boombox ──

@app.route('/api/media/boombox/list')
def api_media_boombox_list():
    try:
        files = BoomboxService.list_audio_files() if BoomboxService else []
        total_size = sum(f.get('size', 0) for f in files)
        disk = _media_disk_info('/mnt/boombox')
        return jsonify({'success': True, 'files': files, 'total_size': total_size, 'disk': disk})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/boombox/upload', methods=['POST'])
def api_media_boombox_upload():
    try:
        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': '没有文件'}), 400
        uploaded = 0
        for file in files:
            if file.filename and BoomboxService:
                success, msg = BoomboxService.upload_audio_file(file, file.filename)
                if success:
                    uploaded += 1
        return jsonify({'success': True, 'message': f'已上传 {uploaded} 个文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/boombox/delete', methods=['POST'])
def api_media_boombox_delete():
    try:
        filename = (request.get_json() or {}).get('filename', '') or request.form.get('filename', '')
        if not filename:
            return jsonify({'success': False, 'error': '文件名为空'}), 400
        success, msg = BoomboxService.delete_audio_file(filename)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/boombox/play/<path:filename>')
def api_media_boombox_play(filename):
    root = BoomboxService.get_music_root() if BoomboxService else '/mnt/boombox'
    filepath = os.path.join(root, filename)
    if not os.path.exists(filepath):
        return 'File not found', 404
    return send_file(filepath)


# ── Lightshow ──

@app.route('/api/media/lightshow/list')
def api_media_lightshow_list():
    try:
        files = LightshowService.list_lightshow_files() if LightshowService else []
        total_size = sum(f.get('size', 0) for f in files)
        disk = _media_disk_info('/mnt/lightshow')
        return jsonify({'success': True, 'files': files, 'total_size': total_size, 'disk': disk})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/lightshow/upload', methods=['POST'])
def api_media_lightshow_upload():
    try:
        file = request.files.get('file')
        if not file or not file.filename:
            return jsonify({'success': False, 'error': '没有文件'}), 400
        success, msg, count = LightshowService.upload_zip_file(file)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/lightshow/delete', methods=['POST'])
def api_media_lightshow_delete():
    try:
        filename = (request.get_json() or {}).get('filename', '') or request.form.get('filename', '')
        if not filename:
            return jsonify({'success': False, 'error': '文件名为空'}), 400
        success, msg = LightshowService.delete_lightshow_file(filename)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Wraps ──

@app.route('/api/media/wraps/list')
def api_media_wraps_list():
    try:
        files = WrapsService.list_wrap_files() if WrapsService else []
        total_size = sum(f.get('size', 0) for f in files)
        disk = _media_disk_info('/mnt/wraps')
        return jsonify({'success': True, 'files': files, 'total_size': total_size, 'disk': disk})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/wraps/upload', methods=['POST'])
def api_media_wraps_upload():
    try:
        file = request.files.get('file')
        if not file or not file.filename:
            return jsonify({'success': False, 'error': '没有文件'}), 400
        success, msg = WrapsService.upload_wrap_file(file, file.filename)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/wraps/delete', methods=['POST'])
def api_media_wraps_delete():
    try:
        filename = (request.get_json() or {}).get('filename', '') or request.form.get('filename', '')
        if not filename:
            return jsonify({'success': False, 'error': '文件名为空'}), 400
        success, msg = WrapsService.delete_wrap_file(filename)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Music ──

@app.route('/api/media/music/list')
def api_media_music_list():
    try:
        folder = request.args.get('folder', '')
        files = BoomboxService.list_audio_files(folder or None) if BoomboxService else []
        total_size = sum(f.get('size', 0) for f in files)
        disk = _media_disk_info('/mnt/music')
        return jsonify({'success': True, 'files': files, 'total_size': total_size, 'disk': disk})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/music/upload', methods=['POST'])
def api_media_music_upload():
    try:
        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': '没有文件'}), 400
        folder = request.form.get('folder', '')
        uploaded = 0
        for file in files:
            if file.filename and BoomboxService:
                success, msg = BoomboxService.upload_audio_file(file, file.filename, folder or None)
                if success:
                    uploaded += 1
        return jsonify({'success': True, 'message': f'已上传 {uploaded} 个文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/music/delete', methods=['POST'])
def api_media_music_delete():
    try:
        data = request.get_json() or {}
        filename = data.get('filename', '') or request.form.get('filename', '')
        folder = data.get('folder', '') or request.form.get('folder', '')
        if not filename:
            return jsonify({'success': False, 'error': '文件名为空'}), 400
        success, msg = BoomboxService.delete_audio_file(filename, folder or None)
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media/music/play/<path:filename>')
def api_media_music_play(filename):
    root = BoomboxService.get_music_root() if BoomboxService else '/mnt/music'
    filepath = os.path.join(root, filename)
    if not os.path.exists(filepath):
        return 'File not found', 404
    return send_file(filepath)


# 启动统计广播线程
_stats_thread = threading.Thread(target=_stats_broadcaster, daemon=True)
_stats_thread.start()

@app.route('/api/system/stats-stream')
def api_system_stats_stream():
    """SSE 系统状态实时流（替代 30s 轮询）"""
    import queue
    q = queue.Queue(maxsize=50)
    with _stats_subscribers_lock:
        _stats_subscribers.append(q)

    def generate():
        try:
            # 立即发送一次当前状态
            try:
                _update_nvme_temp_history()
                stats = {
                    'time': datetime.now().strftime("%H:%M:%S"),
                    'service': get_service_status(),
                    'sys': get_system_stats(),
                    'wifi': get_wifi_info(),
                    'ip': get_ip_info(),
                    'disk_total': get_disk_usage('/'),
                    'disk': get_all_disks()
                }
                evt_total = 0
                for ft in VIDEO_FOLDERS:
                    evts = _scan_video_folder(ft)
                    evt_total += len(evts)
                stats['sentry_events'] = evt_total
                yield f"data: {json.dumps(stats)}\n\n"
            except:
                pass

            while True:
                try:
                    stats = q.get(timeout=30)
                    yield f"data: {json.dumps(stats)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with _stats_subscribers_lock:
                if q in _stats_subscribers:
                    _stats_subscribers.remove(q)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ═══════════════════════════════════════════════════════════════
# 缩略图服务
# ═══════════════════════════════════════════════════════════════

def _scan_missing_thumbnails():
    """扫描所有事件文件夹，为缺少缩略图的事件生成缩略图。
    跳过最近 2 分钟内修改的事件（哨兵可能正在写入）。
    
    RecentClips 特殊处理：按时间戳前缀分组，跳过最新一组（锁定/写入中）。
    """
    results = {'scanned': 0, 'generated': 0, 'skipped': 0, 'errors': []}
    now = time.time()
    cutoff = now - 120  # 2分钟内的视为活跃写入，跳过
    
    if not os.path.exists(THUMBNAIL_DIR):
        os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    
    for ft, info in VIDEO_FOLDERS.items():
        folder_path = info['path']
        if not os.path.isdir(folder_path):
            continue
        
        # ── RecentClips: 平铺文件结构，按时间戳前缀分组 ──
        if ft == 'RecentClips':
            # 收集所有 mp4 文件并按时间戳前缀分组
            groups = {}  # prefix -> [file_paths]
            for fname in sorted(os.listdir(folder_path)):
                if not fname.lower().endswith('.mp4'):
                    continue
                # 文件名格式: 2026-05-17_13-09-36-back.mp4
                # 提取前缀: 2026-05-17_13-09-36
                parts = fname.rsplit('-', 1)
                if len(parts) < 2:
                    continue
                prefix = parts[0]
                # 去掉 _left_repeater / _right_repeater 后缀
                prefix = prefix.replace('_repeater', '')
                if prefix not in groups:
                    groups[prefix] = []
                groups[prefix].append(os.path.join(folder_path, fname))
            
            if not groups:
                continue
            
            # 按时间排序，跳过最新一组（正在写入/锁定）
            sorted_prefixes = sorted(groups.keys())
            skip_prefix = sorted_prefixes[-1]  # 最新一组
            
            for prefix in sorted_prefixes:
                results['scanned'] += 1
                event_id = prefix
                thumbnail_file = os.path.join(THUMBNAIL_DIR, f"{event_id}_grid.jpg")
                
                # 跳过最新一组
                if prefix == skip_prefix:
                    # 检查是否在 2 分钟内（锁定/写入中）
                    newest_mtime = max(os.path.getmtime(fp) for fp in groups[prefix])
                    if newest_mtime > cutoff:
                        results['skipped'] += 1
                        continue
                    # 如果超出 2 分钟但仍是"最新"，也生成缩略图（旧数据）
                
                # 检查是否已有缩略图且是最新的
                if os.path.exists(thumbnail_file):
                    newest_mtime = max(os.path.getmtime(fp) for fp in groups[prefix])
                    if newest_mtime > 0 and os.path.getmtime(thumbnail_file) >= newest_mtime:
                        continue  # 缩略图是最新的
                
                # 生成缩略图 - 使用特殊路径
                try:
                    result = _generate_thumbnail(folder_path, event_id, video_files=groups[prefix])
                    if result:
                        results['generated'] += 1
                    else:
                        results['errors'].append(f"{event_id}: 生成失败 (无视频文件?)")
                except Exception as e:
                    results['errors'].append(f"{event_id}: {str(e)}")
            
            continue  # RecentClips 处理完毕
        
        # ── SentryClips / SavedClips: 事件文件夹结构 ──
        for entry in os.listdir(folder_path):
            event_path = os.path.join(folder_path, entry)
            if not os.path.isdir(event_path):
                continue
            # 跳过非事件文件夹
            has_video = any(f.lower().endswith('.mp4') for f in os.listdir(event_path) if os.path.isfile(os.path.join(event_path, f)))
            if not has_video:
                continue
            
            results['scanned'] += 1
            event_id = entry
            thumbnail_file = os.path.join(THUMBNAIL_DIR, f"{event_id}_grid.jpg")
            
            # 检查是否已有缩略图且是最新的
            if os.path.exists(thumbnail_file):
                newest_mtime = 0
                for f in os.listdir(event_path):
                    fp = os.path.join(event_path, f)
                    if os.path.isfile(fp) and f.lower().endswith('.mp4'):
                        newest_mtime = max(newest_mtime, os.path.getmtime(fp))
                if newest_mtime > 0 and os.path.getmtime(thumbnail_file) >= newest_mtime:
                    continue  # 缩略图是最新的
            
            # 跳过活跃写入中的事件
            dir_mtime = os.path.getmtime(event_path)
            if dir_mtime > cutoff:
                results['skipped'] += 1
                continue
            
            # 生成缩略图
            try:
                result = _generate_thumbnail(event_path, event_id)
                if result:
                    results['generated'] += 1
                else:
                    results['errors'].append(f"{event_id}: 生成失败 (无视频文件?)")
            except Exception as e:
                results['errors'].append(f"{event_id}: {str(e)}")
    
    return results


@app.route('/api/thumbnails/scan', methods=['POST'])
def api_scan_thumbnails():
    """主动扫描并生成缺失的缩略图"""
    try:
        results = _scan_missing_thumbnails()
        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route("/thumbnails/<path:filename>")
def serve_thumbnail(filename):
    """提供缩略图静态文件（懒生成：首次请求时触发 ffmpeg 提取帧）
    
    支持两种结构：
    - 事件文件夹（SentryClips/SavedClips）：{folder}/{event_id}/front.mp4
    - 平铺文件（RecentClips）：{folder}/{event_id}-front.mp4
    """
    thumbnail_path = os.path.join(THUMBNAIL_DIR, filename)
    
    # 如果存在，直接返回
    if os.path.exists(thumbnail_path):
        return send_from_directory(THUMBNAIL_DIR, filename)
    
    # 懒生成：从 filename 提取 event_id，找到对应事件文件
    event_id = os.path.splitext(filename)[0]  # 去掉 .jpg
    event_id = event_id.replace('_grid', '')  # 去掉 _grid 后缀得到纯 event_id
    
    # 在所有视频文件夹中搜索该事件
    event_path = None
    video_files = None  # RecentClips 平铺文件模式
    for ft, info in VIDEO_FOLDERS.items():
        folder_path = info['path']
        # 先尝试事件文件夹结构
        candidate = os.path.join(folder_path, event_id)
        if os.path.isdir(candidate):
            event_path = candidate
            break
        # 再尝试平铺文件结构（RecentClips）
        if os.path.isdir(folder_path):
            matching = []
            for fname in os.listdir(folder_path):
                if fname.startswith(event_id) and fname.lower().endswith('.mp4'):
                    matching.append(os.path.join(folder_path, fname))
            if matching:
                event_path = folder_path
                video_files = matching
                break
    
    if event_path:
        # 生成缩略图（单次 ffmpeg 调用，不会阻塞太久）
        result = _generate_thumbnail(event_path, event_id, video_files=video_files)
        if result and os.path.exists(thumbnail_path):
            return send_from_directory(THUMBNAIL_DIR, filename)
    
    # 失败或无视频 → 返回占位
    return send_file('static/placeholder.svg', mimetype='image/svg+xml')


# ═══════════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import logging
    app.logger.setLevel(logging.DEBUG)
    app.logger.info("🚀 启动 TeslaUSB Web 服务...")
    app.run(host='0.0.0.0', port=5000, debug=False)
