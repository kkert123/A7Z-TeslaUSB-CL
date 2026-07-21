"""NVMe 温度监控与健康度查询模块"""
import subprocess
import time

from app_state import state


def _refresh_nvme_cache():
    """运行一次 nvme smart-log，同时解析温度和健康数据并缓存"""
    try:
        result = subprocess.run(
            ['sudo', '-n', 'nvme', 'smart-log', '/dev/nvme0'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return
        raw_output = result.stdout
        now = time.time()

        # 解析温度
        temp = None
        health = {
            'available': False, 'percentage_used': None, 'available_spare': None,
            'critical_warning': None, 'power_on_hours': None,
            'data_units_written_gb': None, 'data_units_written_bytes': None, 'health_pct': None,
        }
        for line in raw_output.split('\n'):
            if ':' not in line:
                continue
            key_raw, val = line.split(':', 1)
            key = key_raw.strip().lower()
            val = val.strip().replace('%', '').replace(' C', '').replace(',', '').replace('°C', '')

            if key == 'temperature':
                try:
                    temp = int(val)
                except ValueError:
                    pass
            elif key == 'percentage_used':
                try:
                    health['percentage_used'] = int(val) if val.lstrip('-').isdigit() else None
                except ValueError:
                    pass
            elif key == 'available_spare':
                try:
                    health['available_spare'] = int(val) if val.isdigit() else None
                except ValueError:
                    pass
            elif key == 'critical_warning':
                try:
                    health['critical_warning'] = int(val) if val.lstrip('-').isdigit() else None
                except ValueError:
                    pass
            elif key == 'power_on_hours':
                try:
                    health['power_on_hours'] = int(val.replace(',', '')) if val.replace(',', '').isdigit() else None
                except ValueError:
                    pass
            elif key == 'data_units_written':
                raw = val.replace(',', '')
                if raw.isdigit():
                    raw_int = int(raw)
                    health['data_units_written_gb'] = round(raw_int * 1000 * 512 / (1024**3), 1)
                    health['data_units_written_bytes'] = raw_int * 1000 * 512

        if temp is not None:
            health['available'] = True
            if health['percentage_used'] is not None:
                health['health_pct'] = 100 - health['percentage_used']

        with state.nvme_cache_lock:
            state.nvme_cache = {'raw_output': raw_output, 'timestamp': now,
                           'temp': temp, 'health': health}
    except Exception:
        pass

def _get_nvme_cache():
    """获取缓存的 NVMe 数据（不主动刷新，由 broadcaster 定期刷新）"""
    with state.nvme_cache_lock:
        return state.nvme_cache.copy()

def _read_nvme_temp_raw():
    """读取 NVMe 温度（从缓存）"""
    cache = _get_nvme_cache()
    return cache.get('temp')

def get_nvme_temperature():
    """获取 NVMe 当前温度"""
    return _read_nvme_temp_raw()

def _update_nvme_temp_history():
    """更新 NVMe 温度历史（由 broadcaster 调用 — 统一刷新缓存 + 历史）"""
    _refresh_nvme_cache()
    cache = _get_nvme_cache()
    temp = cache.get('temp')
    if temp is not None:
        with state.nvme_temp_lock:
            state.nvme_temp_history.append(temp)
            if len(state.nvme_temp_history) > state.NVME_TEMP_MAX_HISTORY:
                state.nvme_temp_history.pop(0)

def get_nvme_temperature_fields():
    """获取 NVMe 温度统计：当前 / 最低 / 平均 / 最高"""
    cache = _get_nvme_cache()
    current = cache.get('temp')
    with state.nvme_temp_lock:
        if state.nvme_temp_history:
            return {
                'current': current,
                'min': round(min(state.nvme_temp_history), 1),
                'avg': round(sum(state.nvme_temp_history) / len(state.nvme_temp_history), 1),
                'max': round(max(state.nvme_temp_history), 1)
            }
    if current is not None:
        return {'current': current, 'min': current, 'avg': current, 'max': current}
    return {'current': None, 'min': None, 'avg': None, 'max': None}

def get_nvme_health():
    """获取 NVMe SMART 健康度数据（从缓存读取，由 broadcaster 定期刷新）"""
    cache = _get_nvme_cache()
    h = cache.get('health')
    if h and h.get('available'):
        return h
    return {
        'available': False, 'percentage_used': None, 'available_spare': None,
        'critical_warning': None, 'power_on_hours': None,
        'data_units_written_gb': None, 'data_units_written_bytes': None, 'health_pct': None,
    }

def fmt_power_on_hours(hours):
    """格式化通电时间: 1240h → '51天16小时' | 9500h → '1年1月10天20小时'"""
    if hours is None:
        return '—'
    hours = int(hours)
    days = hours // 24
    rem_hours = hours % 24
    if days >= 365:
        years = days // 365
        rem_days = days % 365
        months = rem_days // 30
        days_left = rem_days % 30
        parts = []
        if years > 0: parts.append(f'{years}年')
        if months > 0: parts.append(f'{months}月')
        if days_left > 0: parts.append(f'{days_left}天')
        return ''.join(parts)
    if days > 0:
        if rem_hours > 0:
            return f'{days}天{rem_hours}小时'
        return f'{days}天'
    return f'{hours}小时'
