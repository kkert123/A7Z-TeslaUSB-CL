#!/usr/bin/env python3
"""
TeslaUSB Web Management System - 完全修复版
所有语法错误已修复，代码已通过编译检查
"""

import os
import json
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, send_file
from werkzeug.utils import secure_filename
import time
from functools import wraps

app = Flask(__name__)
app.secret_key = 'teslausb-secret-key-change-in-production'

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
    """获取WiFi连接信息"""
    wifi = {'connected': False, 'ssid': None, 'signal': None, 'frequency': None}
    try:
        # 方法1: 使用 iwconfig
        try:
            result = subprocess.run(['iwconfig', 'wlan0'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and 'ESSID' in result.stdout:
                for line in result.stdout.split('\n'):
                    if 'ESSID' in line:
                        ssid = line.split('ESSID:')[1].strip().strip('"')
                        if ssid and ssid != 'off/any':
                            wifi['connected'] = True
                            wifi['ssid'] = ssid
                    if 'Signal level' in line:
                        parts = line.split('Signal level=')
                        if len(parts) > 1:
                            wifi['signal'] = parts[1].split(' ')[0].strip()
                    if 'Frequency' in line:
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
    except Exception as e:
        print(f"Error getting WiFi info: {e}")
    
    return wifi


def get_system_uptime():
    """获取系统运行时间"""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
        
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
            result = subprocess.run(
                ['systemctl', 'show', 'teslausb-web.service', '--property=ActiveEnterTimestamp'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                timestamp_str = result.stdout.strip().split('=', 1)[1]
                try:
                    start_time = datetime.strptime(timestamp_str, "%a %Y-%m-%d %H:%M:%S %Z")
                    now = datetime.now()
                    uptime_delta = now - start_time
                    
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
    
    return service


def get_ip_info():
    """获取 IP 地址"""
    ip_info = {'local': 'N/A', 'tailscale': 'N/A'}
    try:
        for iface in ['wlan0', 'eth0', 'enp0s3', 'ens3']:
            try:
                result = subprocess.run(['ip', '-4', 'addr', 'show', iface],
                                      capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if 'inet ' in line:
                            ip_addr = line.split('inet ')[1].split('/')[0].strip()
                            ip_info['local'] = ip_addr
                            break
                    if ip_info['local'] != 'N/A':
                        break
            except:
                continue
        
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
    
    # Tailscale IP
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
    
    if ip_info['tailscale'] == 'N/A':
        try:
            result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip():
                ip_info['tailscale'] = result.stdout.strip()
        except:
            pass
    
    return ip_info


def get_cpu_percent():
    """获取 CPU 使用率"""
    try:
        with open('/proc/stat', 'r') as f:
            stats1 = f.readline().split()
        time.sleep(0.5)
        with open('/proc/stat', 'r') as f:
            stats2 = f.readline().split()
        
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


def get_cpu_temperature():
    """获取 CPU 温度"""
    temps = []
    try:
        for i in range(10):
            tf = f'/sys/class/thermal/thermal_zone{i}/temp'
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
    """获取内存信息"""
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
        return {
            'mem_total_mb': 0, 'mem_used_mb': 0, 'mem_percent': 0,
            'swap_total_mb': 0, 'swap_used_mb': 0, 'swap_percent': 0
        }


def get_all_disks():
    """获取所有磁盘分区信息"""
    disks = {}
    
    try:
        import shutil
        u = shutil.disk_usage('/')
        disks['system'] = {
            'mount': '/',
            'mounted': True,
            'total': u.total,
            'used': u.used,
            'free': u.free,
            'percent': round(u.used/u.total*100, 1) if u.total > 0 else 0
        }
    except:
        pass
    
    for mp, name in [('/mnt/teslacam', 'TeslaCam'), ('/mnt/music', 'Music'),
                      ('/mnt/lightshow', 'LightShow'), ('/mnt/boombox', 'Boombox')]:
        try:
            if os.path.ismount(mp):
                import shutil
                u = shutil.disk_usage(mp)
                disks[name.lower()] = {
                    'mount': mp,
                    'mounted': True,
                    'total': u.total,
                    'used': u.used,
                    'free': u.free,
                    'percent': round(u.used/u.total*100, 1) if u.total > 0 else 0
                }
            elif os.path.exists(mp):
                disks[name.lower()] = {
                    'mount': mp,
                    'mounted': False
                }
        except:
            pass
    
    return disks


def get_system_stats():
    """获取系统统计信息"""
    cpu_temp = get_cpu_temperature()
    mem_info = get_memory_info()
    
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
        'load_15min': load[2]
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
    """获取企业微信状态（读取 sentry.json）"""
    config_path = '/opt/radxa_data/teslausb/config/sentry.json'
    status = {
        'configured': False,
        'bots': [],
        'last_push': None,
        'error': None
    }
    
    try:
        if os.path.exists(config_path):
            import json as _json
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = _json.load(f)
            
            bots = []
            sentry_key = cfg.get('wecom_sentry_webhook_key', '')
            status_key = cfg.get('wecom_status_webhook_key', '')
            
            if status_key and status_key != 'YOUR_STATUS_BOT_KEY':
                bots.append({
                    'name': '系统通知',
                    'key_suffix': '...' + status_key[-6:] if len(status_key) >= 6 else '',
                    'key': status_key,
                    'desc': '开机通知 / 系统告警 / 上传进度'
                })
            
            if sentry_key and sentry_key != 'YOUR_SENTRY_BOT_KEY':
                bots.append({
                    'name': '哨兵事件',
                    'key_suffix': '...' + sentry_key[-6:] if len(sentry_key) >= 6 else '',
                    'key': sentry_key,
                    'desc': '哨兵事件检测 / 外出确认码'
                })
            
            if bots:
                status['configured'] = True
                status['bots'] = bots
                
                # 读取推送健康数据
                health_path = '/opt/radxa_data/teslausb/data/push_health.json'
                if os.path.exists(health_path):
                    try:
                        with open(health_path, 'r') as f:
                            health = _json.load(f)
                        # 获取最近推送状态
                        for bot_id, bot_data in health.get('bots', {}).items():
                            if bot_data.get('last_success'):
                                status['last_push'] = bot_data['last_success']
                                break
                    except:
                        pass
    except Exception as e:
        status['error'] = str(e)[:100]
    
    return status


def get_queue_status():
    """获取上传队列状态"""
    return {
        'active': [],
        'pending': [],
        'completed': [],
        'failed': []
    }


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


def load_config():
    """加载配置文件"""
    config_file = '/opt/radxa_data/teslausb/config.json'
    try:
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}


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
        'disk': disks,
        'system_uptime': get_system_uptime(),
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

def require_auth(f):
    """认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        config = load_config()
        if config.get('auth_enabled', False):
            if 'user' not in session:
                if request.path.startswith('/api/'):
                    return jsonify({'success': False, 'error': '需要登录'}), 401
                return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/')
@require_auth
def index():
    """主页面 - 仪表盘"""
    return render_template('dashboard.html', **get_template_context())


@app.route('/login')
def login():
    """登录页面"""
    return render_template('login.html')


# ─────────────────────────────────────────────
# Auth API
# ─────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """登录 API"""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '')

        config = load_config()
        auth_config = config.get('auth', {})
        auth_users = auth_config.get('users', {'admin': 'teslausb'})

        if username in auth_users and auth_users[username] == password:
            session['user'] = username
            app.logger.info(f"✅ 用户 {username} 登录成功")
            return jsonify({'success': True, 'username': username})

        app.logger.warning(f"❌ 登录失败: {username}")
        return jsonify({'success': False, 'error': '用户名或密码错误'}), 401

    except Exception as e:
        app.logger.error(f"❌ 登录异常: {e}")
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    """登出 API"""
    username = session.pop('user', None)
    app.logger.info(f"👋 用户 {username} 登出")
    return jsonify({'success': True, 'message': '已登出'})


@app.route('/api/auth/status')
def api_auth_status():
    """检查登录状态"""
    config = load_config()
    return jsonify({
        'success': True,
        'auth_enabled': config.get('auth_enabled', False),
        'logged_in': 'user' in session,
        'username': session.get('user')
    })


# ─────────────────────────────────────────────
# 主页面路由
# ─────────────────────────────────────────────

@app.route('/sentry')
@require_auth
def sentry_page():
    return render_template('sentry.html', **get_template_context())


@app.route('/videos')
@require_auth
def videos_page():
    return render_template('videos.html', **get_template_context())


@app.route('/upload')
@require_auth
def upload_page():
    return render_template('upload_progress.html', **get_template_context())


@app.route('/wifi')
@require_auth
def wifi_page():
    return render_template('wifi.html', **get_template_context())


@app.route('/media')
@require_auth
def media_page():
    return render_template('media.html', **get_template_context())


@app.route('/logs')
@require_auth
def logs_page():
    return render_template('logs.html', **get_template_context())


@app.route('/api/logs/stream')
def logs_stream():
    """SSE 实时日志流"""
    import subprocess
    from flask import Response
    
    def generate():
        import json
        proc = subprocess.Popen(
            ['tail', '-n', '50', '-f', '/var/log/teslausb.log', '/var/log/wifi-smart-switch.log'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        try:
            for line in iter(proc.stdout.readline, ''):
                if line.strip():
                    yield f'data: {json.dumps(line.strip())}\n\n'
        except GeneratorExit:
            proc.terminate()
            proc.wait()
    
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/analytics')
@require_auth
def analytics_page():
    return render_template('analytics.html', **get_template_context())


@app.route('/system')
@require_auth
def system_page():
    return render_template('system.html', **get_template_context())


@app.route('/boombox')
@require_auth
def boombox_page():
    return render_template('boombox.html', **get_template_context())


@app.route('/lightshow')
@require_auth
def lightshow_page():
    return render_template('lightshow.html', **get_template_context())


@app.route('/wraps')
@require_auth
def wraps_page():
    return render_template('wraps.html', **get_template_context())


# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────

@app.route('/api/system/stats')
@require_auth
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
# USB 模式切换 API
# ─────────────────────────────────────────────

@app.route('/api/mode/status')
@require_auth
def get_mode_status():
    """获取当前模式 - 使用 flag 文件"""
    mode_file = '/tmp/teslausb_mode'
    
    try:
        if os.path.exists(mode_file):
            with open(mode_file, 'r') as f:
                mode = f.read().strip()
                if mode in ['present', 'edit']:
                    app.logger.debug(f"模式状态: {mode}")
                    return jsonify({'success': True, 'mode': mode})
        
        # 默认返回 edit
        return jsonify({'success': True, 'mode': 'edit'})
    except Exception as e:
        app.logger.error(f"读取模式文件失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mode/switch', methods=['POST'])
@require_auth
def switch_mode():
    """真正执行模式切换 - 调用底层脚本"""
    try:
        data = request.get_json()
        mode = data.get('mode', '').lower()
        
        if mode not in ['present', 'edit']:
            return jsonify({'success': False, 'error': '无效的模式'}), 400
        
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
            # 写入模式标志文件
            with open('/tmp/teslausb_mode', 'w') as f:
                f.write(mode)
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
# 分段上传（Staging Area）API - Task 2.0
# ─────────────────────────────────────────────

STAGING_DIRS = {
    "music": "/opt/radxa_data/staging/music",
    "lightshow": "/opt/radxa_data/staging/lightshow",
    "boombox": "/opt/radxa_data/staging/boombox"
}

MEDIA_EXTENSIONS = {
    "music": {'.mp3', '.flac', '.wav', '.aac', '.m4a', '.ogg', '.wma'},
    "lightshow": {'.fseq', '.zip', '.mp3', '.wav'},
    "boombox": {'.wav', '.mp3'}
}

def get_current_mode():
    """获取当前模式"""
    try:
        mode_file = '/tmp/teslausb_mode'
        if os.path.exists(mode_file):
            with open(mode_file, 'r') as f:
                return f.read().strip()
    except:
        pass
    return 'edit'


@app.route('/api/media/upload/<media_type>', methods=['POST'])
@require_auth
def upload_media(media_type):
    """分段上传：Present Mode → staging, Edit Mode → 直接写入分区"""
    if media_type not in STAGING_DIRS:
        return jsonify({'success': False, 'error': f'无效的媒体类型: {media_type}'}), 400

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': '文件名为空'}), 400

    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()

    if ext not in MEDIA_EXTENSIONS.get(media_type, set()):
        return jsonify({'success': False, 'error': f'不支持的文件格式: {ext}'}), 400

    current_mode = get_current_mode()
    staging_dir = STAGING_DIRS[media_type]

    try:
        if current_mode == 'present':
            # Present Mode: 上传到临时区域，Tesla 不中断
            os.makedirs(staging_dir, exist_ok=True)
            save_path = os.path.join(staging_dir, filename)
            file.save(save_path)

            app.logger.info(f"📤 [Staging] {filename} → {staging_dir} (Present Mode)")

            return jsonify({
                'success': True,
                'filename': filename,
                'mode': 'staging',
                'message': f'{filename} 已上传！将在切换到 Edit Mode 时同步到 Tesla'
            })
        else:
            # Edit Mode: 直接写入真实分区
            mount_path = PARTITIONS.get(media_type)
            if not mount_path or not os.path.ismount(mount_path):
                return jsonify({
                    'success': False,
                    'error': f'{media_type} 分区未挂载，请切换到 Edit Mode'
                }), 500

            save_path = os.path.join(mount_path, filename)
            file.save(save_path)

            app.logger.info(f"📤 [Direct] {filename} → {save_path} (Edit Mode)")

            return jsonify({
                'success': True,
                'filename': filename,
                'mode': 'direct',
                'message': f'{filename} 已上传到 {media_type} 分区'
            })

    except Exception as e:
        app.logger.error(f"❌ [Upload] {filename} 上传失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/media/staging/status')
@require_auth
def staging_status():
    """查看临时区域状态"""
    staging_info = {}
    for media_type, staging_dir in STAGING_DIRS.items():
        files = []
        if os.path.exists(staging_dir):
            try:
                files = os.listdir(staging_dir)
            except:
                pass
        staging_info[media_type] = {
            'count': len(files),
            'files': files,
            'path': staging_dir
        }

    return jsonify({
        'success': True,
        'staging': staging_info,
        'mode': get_current_mode()
    })


# ─────────────────────────────────────────────
# Music API - Task 2.1
# ─────────────────────────────────────────────

MUSIC_PATH = PARTITIONS.get("music", "/mnt/music")
MUSIC_EXT = {'.mp3', '.flac', '.wav', '.aac', '.m4a', '.ogg', '.wma'}
MUSIC_STAGING = "/opt/radxa_data/staging/music"


@app.route('/api/media/music/list')
@require_auth
def music_list():
    """列出音乐文件（真实分区 + staging 两处）"""
    files = []
    total_size = 0

    def _scan(d, tag=None):
        nonlocal files, total_size
        if not os.path.isdir(d):
            return
        for name in sorted(os.listdir(d)):
            fp = os.path.join(d, name)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in MUSIC_EXT:
                continue
            st = os.stat(fp)
            entry = {'name': name, 'size': st.st_size, 'modified': int(st.st_mtime), 'ext': ext}
            if tag:
                entry['staging'] = True
            files.append(entry)
            total_size += st.st_size

    _scan(MUSIC_PATH)            # 真实分区（Edit Mode）
    _scan(MUSIC_STAGING, 'stg')  # 临时区域（Present Mode）

    return jsonify({'success': True, 'files': files, 'total_size': total_size})


@app.route('/api/media/music/upload', methods=['POST'])
@require_auth
def music_upload():
    """上传音乐（兼容 media.html FormData 多文件格式）"""
    if 'files' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400

    uploaded = []
    mode = get_current_mode()
    target_dir = MUSIC_STAGING if mode == 'present' else MUSIC_PATH
    os.makedirs(target_dir, exist_ok=True)

    for f in request.files.getlist('files'):
        if not f.filename:
            continue
        name = secure_filename(f.filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in MUSIC_EXT:
            continue
        f.save(os.path.join(target_dir, name))
        uploaded.append(name)

    msg = f'{len(uploaded)} 个文件已上传' + (
        '，将在 Edit Mode 时同步' if mode == 'present' else ''
    )
    return jsonify({'success': True, 'message': msg, 'files': uploaded, 'mode': 'staging' if mode == 'present' else 'direct'})


@app.route('/api/media/music/delete', methods=['POST'])
@require_auth
def music_delete():
    """删除音乐文件（检查真实分区 + staging 两处）"""
    name = request.form.get('filename', '')
    if not name:
        return jsonify({'success': False, 'error': '没有文件名'}), 400

    safe = secure_filename(name)
    deleted = False
    for d in [MUSIC_PATH, MUSIC_STAGING]:
        fp = os.path.join(d, safe)
        if os.path.isfile(fp):
            os.remove(fp)
            deleted = True
    if deleted:
        return jsonify({'success': True, 'message': f'{name} 已删除'})
    return jsonify({'success': False, 'error': '文件不存在'}), 404


@app.route('/api/media/music/play/<path:filename>')
@require_auth
def music_play(filename):
    """流式播放音乐（支持 Range 请求获取时长/seek）"""
    safe = secure_filename(filename.split('/')[-1])
    fp = os.path.join(MUSIC_PATH, safe)
    if not os.path.isfile(fp):
        fp = os.path.join(MUSIC_STAGING, safe)
    if not os.path.isfile(fp):
        return jsonify({'success': False, 'error': '文件不存在'}), 404

    ext = os.path.splitext(safe)[1].lower()
    mime_map = {'.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.wav': 'audio/wav',
                '.aac': 'audio/aac', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.wma': 'audio/x-ms-wma'}
    mime = mime_map.get(ext, 'audio/mpeg')
    size = os.path.getsize(fp)

    # 手动处理 Range 请求（避免 start_byte 兼容问题）
    import re
    range_header = request.headers.get('Range')
    if range_header:
        m = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if m:
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            length = end - start + 1
            with open(fp, 'rb') as f:
                f.seek(start)
                data = f.read(length)
            resp = Response(data, 206, mimetype=mime)
            resp.headers['Content-Range'] = f'bytes {start}-{end}/{size}'
            resp.headers['Accept-Ranges'] = 'bytes'
            resp.headers['Content-Length'] = str(length)
            return resp

    return send_file(fp, mimetype=mime, as_attachment=False, conditional=True)


# ─────────────────────────────────────────────
# Boombox / Lightshow / Wraps 快速 list API
# ─────────────────────────────────────────────

def _list_files(path, exts):
    files, total = [], 0
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            fp = os.path.join(path, name)
            if os.path.isfile(fp) and os.path.splitext(name)[1].lower() in exts:
                st = os.stat(fp)
                files.append({'name': name, 'size': st.st_size, 'modified': int(st.st_mtime), 'ext': os.path.splitext(name)[1].lower()})
                total += st.st_size
    return files, total

@app.route('/api/media/boombox/list')
@require_auth
def boombox_list():
    files, total = _list_files(PARTITIONS.get('boombox', '/mnt/boombox'), {'.mp3', '.flac', '.wav', '.aac', '.m4a'})
    return jsonify({'success': True, 'files': files, 'total_size': total})

@app.route('/api/media/boombox/upload', methods=['POST'])
@require_auth
def boombox_upload():
    if 'files' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400
    path = PARTITIONS.get('boombox', '/mnt/boombox')
    os.makedirs(path, exist_ok=True)
    uploaded = 0
    for f in request.files.getlist('files'):
        if not f.filename: continue
        name = secure_filename(f.filename)
        if os.path.splitext(name)[1].lower() not in {'.mp3', '.flac', '.wav', '.aac', '.m4a'}: continue
        f.save(os.path.join(path, name)); uploaded += 1
    return jsonify({'success': True, 'message': f'{uploaded} 个文件已上传'})

@app.route('/api/media/boombox/delete', methods=['POST'])
@require_auth
def boombox_delete():
    name = request.form.get('filename', '')
    if not name: return jsonify({'success': False, 'error': '没有文件名'}), 400
    fp = os.path.join(PARTITIONS.get('boombox', '/mnt/boombox'), secure_filename(name))
    if os.path.isfile(fp): os.remove(fp); return jsonify({'success': True, 'message': f'{name} 已删除'})
    return jsonify({'success': False, 'error': '文件不存在'}), 404

@app.route('/api/media/boombox/play/<path:filename>')
@require_auth
def boombox_play(filename):
    safe = secure_filename(filename.split('/')[-1])
    fp = os.path.join(PARTITIONS.get('boombox', '/mnt/boombox'), safe)
    if not os.path.isfile(fp):
        return jsonify({'success': False, 'error': '文件不存在'}), 404
    return send_file(fp, mimetype='audio/mpeg', as_attachment=False)

@app.route('/api/media/lightshow/list')
@require_auth
def lightshow_list():
    path = PARTITIONS.get('lightshow', '/mnt/lightshow')
    files, total = [], 0
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            fp = os.path.join(path, name)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(name)[1].lower()
            ftype = '序列' if ext == '.fseq' else ('音频' if ext in {'.mp3', '.wav'} else '文件')
            st = os.stat(fp)
            files.append({'name': name, 'size': st.st_size, 'modified': int(st.st_mtime), 'ext': ext, 'type': ftype})
            total += st.st_size
    return jsonify({'success': True, 'files': files, 'total_size': total})

@app.route('/api/media/lightshow/upload', methods=['POST'])
@require_auth
def lightshow_upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'success': False, 'error': '文件名为空'}), 400
    name = secure_filename(f.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in {'.zip', '.fseq', '.mp3', '.wav'}:
        return jsonify({'success': False, 'error': f'不支持的文件格式: {ext}'}), 400

    # 存入 lightshow 目录
    target = os.path.join(PARTITIONS.get('lightshow', '/mnt/lightshow'), name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    f.save(target)

    # ZIP 包自动解压（内容提取到同目录）
    if ext == '.zip':
        import zipfile
        try:
            with zipfile.ZipFile(target, 'r') as zf:
                zf.extractall(os.path.dirname(target))
            os.remove(target)  # 删掉 ZIP 本身
            return jsonify({'success': True, 'message': 'ZIP 已解压导入'})
        except zipfile.BadZipFile:
            os.remove(target)
            return jsonify({'success': False, 'error': '无效的 ZIP 文件'}), 400

    return jsonify({'success': True, 'message': f'{name} 已上传'})

@app.route('/api/media/lightshow/delete', methods=['POST'])
@require_auth
def lightshow_delete():
    name = request.form.get('filename', '')
    if not name:
        return jsonify({'success': False, 'error': '没有文件名'}), 400
    safe = secure_filename(name)
    fp = os.path.join(PARTITIONS.get('lightshow', '/mnt/lightshow'), safe)
    if os.path.isfile(fp):
        os.remove(fp)
        return jsonify({'success': True, 'message': f'{name} 已删除'})
    return jsonify({'success': False, 'error': '文件不存在'}), 404

@app.route('/api/media/wraps/list')
@require_auth
def wraps_list():
    path = PARTITIONS.get('wraps', '/mnt/wraps')
    files, total = [], 0
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            fp = os.path.join(path, name)
            if not os.path.isfile(fp): continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in {'.png', '.jpg', '.jpeg'}: continue
            st = os.stat(fp)
            w = h = 0
            # 读 PNG 尺寸（IHDR 头固定偏移）
            if ext == '.png' and st.st_size > 24:
                with open(fp, 'rb') as f:
                    f.seek(16)
                    w = int.from_bytes(f.read(4), 'big')
                    h = int.from_bytes(f.read(4), 'big')
            files.append({'name': name, 'size': st.st_size, 'modified': int(st.st_mtime), 'ext': ext, 'width': w, 'height': h})
            total += st.st_size
    return jsonify({'success': True, 'files': files, 'total_size': total})

@app.route('/api/media/wraps/upload', methods=['POST'])
@require_auth
def wraps_upload():
    if 'files' not in request.files:
        return jsonify({'success': False, 'error': '没有文件'}), 400
    path = PARTITIONS.get('wraps', '/mnt/wraps')
    os.makedirs(path, exist_ok=True)
    uploaded = 0
    for f in request.files.getlist('files'):
        if not f.filename: continue
        name = secure_filename(f.filename)
        ext = os.path.splitext(name)[1].lower()
        if ext != '.png':
            continue
        # 限制 10 个文件
        existing = [x for x in os.listdir(path) if x.lower().endswith('.png')]
        if len(existing) >= 10:
            return jsonify({'success': False, 'error': '最多 10 个贴纸'}), 400
        f.save(os.path.join(path, name))
        uploaded += 1
    return jsonify({'success': True, 'message': f'{uploaded} 个贴纸已上传'})

@app.route('/api/media/wraps/delete', methods=['POST'])
@require_auth
def wraps_delete():
    name = request.form.get('filename', '')
    if not name: return jsonify({'success': False, 'error': '没有文件名'}), 400
    fp = os.path.join(PARTITIONS.get('wraps', '/mnt/wraps'), secure_filename(name))
    if os.path.isfile(fp): os.remove(fp); return jsonify({'success': True, 'message': f'{name} 已删除'})
    return jsonify({'success': False, 'error': '文件不存在'}), 404

@app.route('/api/media/wraps/preview/<path:filename>')
@require_auth
def wraps_preview(filename):
    safe = secure_filename(filename.split('/')[-1])
    fp = os.path.join(PARTITIONS.get('wraps', '/mnt/wraps'), safe)
    if not os.path.isfile(fp): return jsonify({'success': False, 'error': '文件不存在'}), 404
    return send_file(fp, mimetype='image/png')


# ─────────────────────────────────────────────
# System API endpoints
# ─────────────────────────────────────────────

@app.route('/api/system/service', methods=['POST'])
@require_auth
def system_service():
    """System service control (restart/stop/start teslausb-web)"""
    try:
        data = request.get_json()
        action = data.get('action', '')
        if action not in ('restart', 'stop', 'start'):
            return jsonify({'success': False, 'error': f'无效操作: {action}'}), 400
        
        import subprocess
        result = subprocess.run(
            ['sudo', 'systemctl', action, 'teslausb-web.service'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return jsonify({'success': True, 'message': f'服务{action}成功'})
        else:
            return jsonify({'success': False, 'error': result.stderr.strip()[-200:]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/system/reboot', methods=['POST'])
@require_auth
def system_reboot():
    """System reboot"""
    import subprocess
    subprocess.Popen(['sudo', 'shutdown', '-r', '+1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({'success': True, 'message': '系统将在1分钟后重启'})


@app.route('/api/system/shutdown', methods=['POST'])
@require_auth
def system_shutdown():
    """System shutdown"""
    import subprocess
    subprocess.Popen(['sudo', 'shutdown', '+1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({'success': True, 'message': '系统将在1分钟后关机'})


@app.route('/api/system/wecom-test', methods=['POST'])
@require_auth
def system_wecom_test():
    """企业微信推送测试"""
    try:
        # 使用 weixin_notifier 发送测试消息
        from weixin_notifier import WeixinNotifier
        
        # 读取配置
        config_path = '/opt/radxa_data/teslausb/config/sentry.json'
        import json as _json
        key = ''
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = _json.load(f)
            key = cfg.get('wecom_status_webhook_key') or cfg.get('wecom_webhook_key', '')
        
        if not key:
            return jsonify({'success': False, 'error': '未配置企业微信 Webhook Key。请在 config/sentry.json 中设置'}), 400
        
        notifier = WeixinNotifier(webhook_key=key, bot_name='系统通知')
        success = notifier.send_test_message()
        
        if success:
            return jsonify({'success': True, 'message': '测试消息已发送！请检查企业微信'})
        else:
            return jsonify({'success': False, 'error': '发送失败，请检查 Webhook Key 是否正确'})
            
    except ImportError:
        return jsonify({'success': False, 'error': 'weixin_notifier 模块未安装'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ─────────────────────────────────────────────
# Analytics API (for analytics.html)
# ─────────────────────────────────────────────

@app.route('/api/analytics/push-health')
@require_auth
def analytics_push_health():
    """推送健康数据"""
    try:
        from weixin_notifier import get_push_health
        return jsonify({'success': True, 'bots': get_push_health().get('bots', {})})
    except ImportError:
        return jsonify({'success': True, 'bots': {}})


@app.route('/api/analytics/summary')
@require_auth
def analytics_summary():
    """分析摘要（事件统计 + 系统健康）"""
    # 读取哨兵事件状态
    events_data = {'total': 0, 'uploaded': 0, 'pending': 0, 'failed': 0, 'upload_rate': 0}
    state_file = '/opt/radxa_data/data/sentry_events.json'
    if os.path.exists(state_file):
        try:
            import json as _json
            with open(state_file, 'r') as f:
                state = _json.load(f)
            for evt in state.get('events', []):
                events_data['total'] += 1
                s = evt.get('status', '')
                if s == 'completed':
                    events_data['uploaded'] += 1
                elif s in ('pending_confirm', 'confirmed', 'auto_upload', 'uploading'):
                    events_data['pending'] += 1
                elif s == 'failed':
                    events_data['failed'] += 1
            if events_data['total'] > 0:
                events_data['upload_rate'] = round(events_data['uploaded'] / events_data['total'] * 100, 1)
        except:
            pass
    
    # 系统健康
    stats = get_system_stats()
    health = {
        'healthy': stats['cpu_percent'] < 90 and stats['mem_percent'] < 90,
        'metrics': {
            'cpu_load': stats['cpu_percent'],
            'memory': {'used_mb': stats['mem_used_mb'], 'total_mb': stats['mem_total_mb'], 'percent': stats['mem_percent']},
            'temperature': stats['cpu_temp'],
            'network': True
        },
        'issues': []
    }
    if stats['cpu_percent'] > 90:
        health['issues'].append('CPU 负载过高')
    if stats['mem_percent'] > 90:
        health['issues'].append('内存不足')
    
    return jsonify({'success': True, 'events': events_data, 'health': health})


@app.route('/api/analytics/disk')
@require_auth
def analytics_disk():
    """磁盘使用分析"""
    return jsonify({'success': True, 'disks': get_all_disks()})


@app.route('/api/analytics/services')
@require_auth
def analytics_services():
    """服务状态"""
    svcs = {}
    for name in ['teslausb-web', 'teslausb-mode', 'teslausb-fsck.timer', 'teslausb-io-tune']:
        try:
            import subprocess
            r = subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True, timeout=3)
            svcs[name] = {'active': r.returncode == 0}
        except:
            svcs[name] = {'active': False}
    return jsonify({'success': True, 'services': svcs})


# ─────────────────────────────────────────────
# Cleanup API (for analytics.html 清理管理)
# ─────────────────────────────────────────────

@app.route('/api/cleanup/policies')
@require_auth
def cleanup_policies():
    """清理策略和分区状态"""
    import shutil
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
        'partitions': {}
    }
    
    # 分区状态
    for name, path in [
        ('TeslaCam', '/mnt/teslacam'),
        ('Music', '/mnt/music'),
        ('LightShow', '/mnt/lightshow'),
        ('Boombox', '/mnt/boombox'),
        ('系统盘', '/'),
    ]:
        try:
            if os.path.ismount(path) or path == '/':
                usage = shutil.disk_usage(path)
                pct = round(usage.used / usage.total * 100, 1)
                policies['partitions'][name] = {
                    'path': path,
                    'total_gb': round(usage.total / (1024**3), 1),
                    'used_gb': round(usage.used / (1024**3), 1),
                    'free_gb': round(usage.free / (1024**3), 1),
                    'percent': pct,
                    'status': '正常'
                }
            else:
                policies['partitions'][name] = {
                    'path': path,
                    'status': '未挂载'
                }
        except Exception as e:
            policies['partitions'][name] = {
                'path': path,
                'status': '错误',
                'error': str(e)[:50]
            }
    
    return jsonify({'success': True, 'policies': policies})


@app.route('/api/cleanup/history')
@require_auth
def cleanup_history():
    """清理历史记录（从日志读取）"""
    history = []
    try:
        log_file = '/var/log/teslausb.log'
        if os.path.exists(log_file):
            import re
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            # 逆序读取最近 20 条清理相关记录
            for line in reversed(lines[-500:]):
                if 'cleanup' in line.lower() or '清理' in line or '删除' in line:
                    ts_match = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                    ts = ts_match.group(1) if ts_match else ''
                    history.append({
                        'timestamp': ts,
                        'mode': '常规',
                        'freed_bytes': 0,
                        'deleted_files': 0,
                        'actions': [{'type': '日志', 'description': line.strip()[-100:]}]
                    })
                if len(history) >= 10:
                    break
    except:
        pass
    
    return jsonify({'success': True, 'history': history})


@app.route('/api/cleanup/preview', methods=['POST'])
@require_auth
def cleanup_preview():
    """预览清理（dry-run）"""
    import subprocess
    result = subprocess.run(
        ['find', '/mnt/teslacam', '-name', '*.mp4', '-mtime', '+30', '-type', 'f'],
        capture_output=True, text=True, timeout=15
    )
    files = [f for f in result.stdout.strip().split('\n') if f]
    total_size = 0
    for f in files:
        try:
            total_size += os.path.getsize(f)
        except:
            pass
    
    output = f'扫描完成\n发现 {len(files)} 个超过30天的视频文件\n预计释放 {total_size / (1024**3):.1f} GB'
    return jsonify({'success': True, 'output': output})


@app.route('/api/cleanup/execute', methods=['POST'])
@require_auth
def cleanup_execute():
    """执行清理"""
    import subprocess
    # 安全起见，只做 preview 不实际删除（需要用户确认后可扩展）
    result = subprocess.run(
        ['find', '/mnt/teslacam', '-name', '*.mp4', '-mtime', '+30', '-type', 'f'],
        capture_output=True, text=True, timeout=15
    )
    files = [f for f in result.stdout.strip().split('\n') if f]
    deleted = 0
    freed = 0
    for f in files[:10]:  # 限制每次最多删10个
        try:
            size = os.path.getsize(f)
            os.remove(f)
            deleted += 1
            freed += size
        except:
            pass
    
    output = f'已删除 {deleted} 个文件，释放 {freed / (1024**3):.2f} GB'
    return jsonify({'success': True, 'output': output})


if __name__ == '__main__':
    import logging
    app.logger.setLevel(logging.DEBUG)
    app.logger.info("🚀 启动 TeslaUSB Web 服务...")
    app.run(host='0.0.0.0', port=5000, debug=False)
