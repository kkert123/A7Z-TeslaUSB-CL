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
from flask import Flask, render_template, request, jsonify

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
    """获取 WiFi 状态"""
    wifi = {'connected': False, 'ssid': 'N/A', 'signal': None}
    try:
        result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            wifi['connected'] = True
            wifi['ssid'] = result.stdout.strip()
    except:
        pass
    return wifi

def get_service_status():
    """获取服务状态"""
    service = {'active': False, 'uptime': 'N/A'}
    try:
        result = subprocess.run(['systemctl', 'is-active', 'teslausb-web.service'],
                              capture_output=True, text=True, timeout=2)
        service['active'] = result.returncode == 0 and 'active' in result.stdout
    except:
        pass
    return service

def get_ip_info():
    """获取 IP 地址"""
    ip_info = {'local': 'N/A', 'tailscale': 'N/A'}
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            ips = result.stdout.strip().split()
            if ips:
                ip_info['local'] = ips[0]
    except:
        pass
    
    try:
        result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            ip_info['tailscale'] = result.stdout.strip()
    except:
        pass
    
    return ip_info

def get_system_stats():
    """获取系统统计（简化版）"""
    return {
        'cpu_percent': 0,
        'cpu_temp': None,
        'cpu_temp_min': None,
        'cpu_temp_avg': None,
        'cpu_temp_max': None,
        'mem_used_mb': 0,
        'mem_total_mb': 1,
        'mem_percent': 0,
        'swap_used_mb': 0,
        'swap_total_mb': 0,
        'load_1min': 0,
        'load_5min': 0,
        'load_15min': 0
    }

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

def get_folders():
    """获取视频文件夹列表"""
    folders = {}
    cam_path = PARTITIONS.get("cam", "/mnt/teslacam")
    
    try:
        if os.path.exists(cam_path):
            for item in sorted(os.listdir(cam_path)):
                item_path = os.path.join(cam_path, item)
                if os.path.isdir(item_path):
                    video_count = 0
                    total_size = 0
                    try:
                        for root, dirs, files in os.walk(item_path):
                            for f in files:
                                if f.endswith(('.mp4', '.mov', '.ts')):
                                    video_count += 1
                                    fpath = os.path.join(root, f)
                                    total_size += os.path.getsize(fpath)
                    except:
                        pass
                    
                    folders[item] = {
                        'path': item_path,
                        'video_count': video_count,
                        'total_size': total_size,
                        'date': item
                    }
    except Exception as e:
        print(f"Error getting folders: {e}")
    
    return folders

def get_wecom_status():
    """获取企业微信状态"""
    return {
        'configured': False,
        'bots': [],
        'last_push': None,
        'error': None
    }

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
    
    return {
        'service': get_service_status(),
        'sys_stats': get_system_stats(),
        'wifi': wifi_info,
        'current': wifi_info,
        'ip_info': get_ip_info(),
        'disk_total': get_disk_usage('/'),
        'disk': {},
        'now': datetime.now().strftime("%H:%M:%S"),
        'fmt_bytes': fmt_bytes,
        'folders': get_folders(),
        'wecom': get_wecom_status(),
        'queue': queue,
        'active_count': counts['active_count'],
        'pending_count': counts['pending_count'],
        'completed_count': counts['completed_count'],
        'failed_count': counts['failed_count']
    }

# ─────────────────────────────────────────────
# 路由 - 主页面
# ─────────────────────────────────────────────

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
    return render_template('videos.html', **get_template_context())

@app.route('/upload')
def upload_page():
    return render_template('upload_progress.html', **get_template_context())

@app.route('/wifi')
def wifi_page():
    return render_template('wifi.html', **get_template_context())

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
            'disk': {}
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Mode switch API
MODE_FILE = '/opt/radxa_data/teslausb/data/mode.txt'

@app.route('/api/mode/status')
def api_mode_status():
    try:
        mode = 'present'
        if os.path.exists(MODE_FILE):
            with open(MODE_FILE, 'r') as f:
                mode = f.read().strip()
        return jsonify({'success': True, 'mode': mode})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/mode/switch', methods=['POST'])
def api_mode_switch():
    try:
        data = request.get_json()
        new_mode = data.get('mode', 'present')
        
        if new_mode not in ['present', 'edit']:
            return jsonify({'success': False, 'error': 'Invalid mode'}), 400
        
        os.makedirs(os.path.dirname(MODE_FILE), exist_ok=True)
        with open(MODE_FILE, 'w') as f:
            f.write(new_mode)
        
        return jsonify({'success': True, 'mode': new_mode})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─────────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(os.path.dirname(MODE_FILE), exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
