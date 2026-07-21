"""硬件状态统计模块 — CPU/GPU 温度、内存、磁盘、网络、NVMe 健康度、风扇"""
import os
import json
import time
import subprocess
import threading
from datetime import datetime

from app_state import state


# ─────────────────────────────────────────────
# 格式化辅助
# ─────────────────────────────────────────────

def _format_size(size_bytes):
    """格式化字节大小（供视频模板使用）"""
    if not size_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


# ─────────────────────────────────────────────
# CPU 使用率
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Thermal Zone 温度读写
# ─────────────────────────────────────────────

def _read_thermal_zone(index):
    """读取指定 thermal zone 温度（°C）"""
    tf = '/sys/class/thermal/thermal_zone' + str(index) + '/temp'
    if os.path.exists(tf):
        try:
            with open(tf, 'r') as f:
                t = int(f.read().strip()) / 1000.0
                if 0 < t < 150:
                    return t
        except:
            pass
    return None

def _read_thermal_zone_type(index):
    """读取 thermal zone 类型名称"""
    tf = '/sys/class/thermal/thermal_zone' + str(index) + '/type'
    if os.path.exists(tf):
        try:
            with open(tf, 'r') as f:
                return f.read().strip()
        except:
            pass
    return None

def _detect_thermal_zones():
    """检测 CPU/GPU thermal zone 索引"""
    cpu_idx, gpu_idx = 0, None
    for i in range(10):
        t = _read_thermal_zone_type(i)
        if t:
            if 'gpu' in t.lower():
                gpu_idx = i
            elif cpu_idx == 0 and ('cpu' in t.lower() or 'soc' in t.lower()):
                cpu_idx = i
    return cpu_idx, gpu_idx

state.thermal_cpu_idx, state.thermal_gpu_idx = _detect_thermal_zones()

def _update_temp_histories():
    """更新 CPU/GPU 温度历史"""
    cpu_t = _read_thermal_zone(state.thermal_cpu_idx)
    gpu_t = _read_thermal_zone(state.thermal_gpu_idx) if state.thermal_gpu_idx is not None else None
    with state.temp_history_lock:
        if cpu_t is not None:
            state.cpu_temp_history.append(cpu_t)
            if len(state.cpu_temp_history) > state.TEMP_MAX_HISTORY:
                state.cpu_temp_history.pop(0)
        if gpu_t is not None:
            state.gpu_temp_history.append(gpu_t)
            if len(state.gpu_temp_history) > state.TEMP_MAX_HISTORY:
                state.gpu_temp_history.pop(0)

def _make_temp_fields(history, current):
    if history:
        return {
            'current': round(current, 1) if current is not None else None,
            'min': round(min(history), 1),
            'avg': round(sum(history) / len(history), 1),
            'max': round(max(history), 1)
        }
    if current is not None:
        cv = round(current, 1)
        return {'current': cv, 'min': cv, 'avg': cv, 'max': cv}
    return {'current': None, 'min': None, 'avg': None, 'max': None}

def get_cpu_temperature():
    """兼容旧接口：返回 thermal_zone0 温度统计"""
    current = _read_thermal_zone(state.thermal_cpu_idx)
    with state.temp_history_lock:
        return _make_temp_fields(state.cpu_temp_history, current)

def get_gpu_temperature_fields():
    """获取 GPU 温度统计"""
    current = _read_thermal_zone(state.thermal_gpu_idx) if state.thermal_gpu_idx is not None else None
    with state.temp_history_lock:
        return _make_temp_fields(state.gpu_temp_history, current)


# ─────────────────────────────────────────────
# 内存信息
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# 磁盘信息
# ─────────────────────────────────────────────

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


def get_nvme_total_disk():
    """汇总 NVMe 四个数据分区 (teslacam/music/lightshow/boombox) 的磁盘使用"""
    partitions = get_all_disks()
    total = used = 0
    for name in ('teslacam', 'music', 'lightshow', 'boombox'):
        info = partitions.get(name, {})
        if info.get('mounted', False) or info.get('total', 0) > 0:
            total += info.get('total', 0)
            used += info.get('used', 0)
    if total > 0:
        return {'total': total, 'used': used, 'free': total - used, 'percent': round(used/total*100, 1)}
    return {'total': 0, 'used': 0, 'free': 0, 'percent': 0}


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


# ─────────────────────────────────────────────
# 网络流量
# ─────────────────────────────────────────────

def get_network_bytes():
    """读取网络接口 RX/TX 字节数（用于吞吐量计算）
    
    优先 wlan0 主接口，同时累加 tailscale0 VPN 流量。
    """
    net = {'net_rx': 0, 'net_tx': 0, 'net_iface': 'wlan0'}
    try:
        with open('/proc/net/dev', 'r') as f:
            for line in f:
                if ':' not in line:
                    continue
                iface = line.split(':')[0].strip()
                if iface in ('wlan0', 'eth0', 'tailscale0'):
                    parts = line.split(':')[1].split()
                    if len(parts) >= 9:
                        rx_bytes = int(parts[0])
                        tx_bytes = int(parts[8])
                        if iface == 'wlan0':
                            # wlan0 是主接口
                            net['net_rx'] = rx_bytes
                            net['net_tx'] = tx_bytes
                            net['net_iface'] = iface
                        elif iface == 'tailscale0':
                            # tailscale VPN 流量叠加到主接口
                            net['net_rx'] += rx_bytes
                            net['net_tx'] += tx_bytes
    except:
        pass
    return net


# ─────────────────────────────────────────────
# 磁盘 I/O
# ─────────────────────────────────────────────

def _update_disk_io():
    """从 /proc/diskstats 更新磁盘 I/O 速率（由 broadcaster 调用）"""
    try:
        read_sectors = 0
        write_sectors = 0
        now = time.time()
        with open('/proc/diskstats', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 14 and parts[2] == 'nvme0n1':
                    read_sectors = int(parts[5])
                    write_sectors = int(parts[9])
                    break
        with state.disk_io_lock:
            prev = state.disk_io_prev
            if prev['timestamp'] > 0 and read_sectors >= prev['read_sectors']:
                dt = now - prev['timestamp']
                if dt > 0:
                    state.disk_io_cur['rate_read'] = int((read_sectors - prev['read_sectors']) * 512 / dt)
                    state.disk_io_cur['rate_write'] = int((write_sectors - prev['write_sectors']) * 512 / dt)
            state.disk_io_prev = {'read_sectors': read_sectors, 'write_sectors': write_sectors, 'timestamp': now}
    except Exception:
        pass

def get_disk_io():
    """获取当前磁盘 I/O 速率"""
    with state.disk_io_lock:
        return state.disk_io_cur.copy()


# ─────────────────────────────────────────────
# 月度流量追踪
# ─────────────────────────────────────────────

def _get_monthly_traffic(rx_counter, tx_counter):
    """追踪当月累计流量。rx_counter/tx_counter 来自 /proc/net/dev (开机累计字节)"""
    month_key = datetime.now().strftime('%Y-%m')
    
    with state.monthly_traffic_lock:
        data = {}
        if os.path.exists(state.MONTHLY_TRAFFIC_FILE):
            try:
                with open(state.MONTHLY_TRAFFIC_FILE, 'r') as f:
                    data = json.load(f)
            except:
                pass
        
        entry = data.get(month_key, {'rx_cumulative': 0, 'tx_cumulative': 0, 'last_rx_counter': 0, 'last_tx_counter': 0})
        
        # 仅当计数器递增时累加大于 last 的部分
        # 如果计数器回退（接口重置/重启），假设从 0 重新开始，累加当前值
        if entry['last_rx_counter'] > 0:
            if rx_counter >= entry['last_rx_counter']:
                entry['rx_cumulative'] += rx_counter - entry['last_rx_counter']
            else:
                # 计数器回退：接口重置或重启，从零开始
                entry['rx_cumulative'] += rx_counter
        if entry['last_tx_counter'] > 0:
            if tx_counter >= entry['last_tx_counter']:
                entry['tx_cumulative'] += tx_counter - entry['last_tx_counter']
            else:
                entry['tx_cumulative'] += tx_counter
        
        entry['last_rx_counter'] = rx_counter
        entry['last_tx_counter'] = tx_counter
        data[month_key] = entry
        
        # 清理旧月份（仅保留当月）
        data = {k: v for k, v in data.items() if k == month_key}
        
        try:
            os.makedirs(os.path.dirname(state.MONTHLY_TRAFFIC_FILE), exist_ok=True)
            with open(state.MONTHLY_TRAFFIC_FILE, 'w') as f:
                json.dump(data, f)
        except:
            pass
        
        return {
            'month': month_key,
            'rx_bytes': entry['rx_cumulative'],
            'tx_bytes': entry['tx_cumulative'],
        }


# ─────────────────────────────────────────────
# GPU / NPU 状态
# ─────────────────────────────────────────────

def get_gpu_npu_status():
    """读取 GPU/NPU 状态。A733: PowerVR GPU 无利用率计数器，用 active_time 占比作代理"""
    result = {'gpu': {'pct': 0, 'status': '—'}, 'npu': '—'}
    try:
        gpu_base = '/sys/devices/platform/soc@3000000/1800000.gpu/power'
        active_path = f'{gpu_base}/runtime_active_time'
        suspended_path = f'{gpu_base}/runtime_suspended_time'
        status_path = f'{gpu_base}/runtime_status'
        if os.path.exists(active_path) and os.path.exists(suspended_path):
            with open(active_path, 'r') as f:
                active = int(f.read().strip())
            with open(suspended_path, 'r') as f:
                suspended = int(f.read().strip())
            total = active + suspended
            pct = round(active / total * 100, 1) if total > 0 else 0
            if os.path.exists(status_path):
                with open(status_path, 'r') as f:
                    result['gpu']['status'] = f.read().strip()
            else:
                result['gpu']['status'] = 'active' if pct > 0 else 'suspended'
            result['gpu']['pct'] = pct
    except:
        pass
    try:
        # NPU: 读取当前频率 (Hz → GHz)
        npu_freq = '/sys/class/devfreq/3600000.npu/cur_freq'
        if os.path.exists(npu_freq):
            with open(npu_freq, 'r') as f:
                hz = int(f.read().strip())
                result['npu'] = f'{hz/1e9:.2f} GHz'
    except:
        pass
    return result


# ─────────────────────────────────────────────
# CPU 风扇状态
# ─────────────────────────────────────────────

def get_fan_status():
    """获取 CPU 风扇 PWM 占空比。A7Z 风扇无 RPM 传感器，仅 PWM 控制。"""
    result = {'pwm': None, 'pwm_pct': None, 'rpm': None}
    try:
        # hwmon9 = pwmfan
        pwm_path = '/sys/class/hwmon/hwmon9/pwm1'
        if os.path.exists(pwm_path):
            with open(pwm_path, 'r') as f:
                pwm = int(f.read().strip())
            result['pwm'] = pwm
            result['pwm_pct'] = round(pwm / 255 * 100, 1)
    except Exception:
        pass
    return result
