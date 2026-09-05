"""traffic_monitor.py — 独立网络流量统计（M64，2026-09-05）

设计目标（用户需求）：
- 独立模块：不依赖页面访问，由 broadcaster 周期驱动，不受服务重启/升级影响
- 持久化：数据文件放在版本目录外 /opt/radxa_data/data/（旧文件在 teslausb/data/
  下，升级切换版本目录即丢失——"重启就清零"的主因）
- 分类：家庭 WiFi（station 联网）/ 车辆热点（AP 模式，车机上传流量）分别累计
- 余量与正确性：计数器回退（接口重置）按增量补记；跨月归档保留 3 个月

数据来源：/proc/net/dev 的 wlan0 计数器（两种角色共用同一物理接口，
tailscale0 流量物理上仍经 wlan0，不重复计）。
"""
import json
import os
import threading
import time
from datetime import datetime

STATE_FILE = "/opt/radxa_data/data/traffic_stats.json"
LEGACY_FILE = "/opt/radxa_data/teslausb/data/monthly_traffic.json"
CATEGORIES = ("home_wifi", "hotspot", "other")
MONTHS_KEEP = 3
_WRITE_MIN_INTERVAL = 5.0   # 落盘最小间隔（broadcaster 5s 一拍）
_AP_CACHE_TTL = 60.0        # AP 状态缓存（systemctl 调用有开销）

_lock = threading.Lock()
_last_counters = None       # (rx, tx) 上次采样
_last_write_ts = 0.0
_ap_cache = {"value": False, "ts": 0.0}
_state = None               # 内存中的状态 dict，懒加载


def _load_state():
    """加载状态文件；首次运行时迁移旧版单分类数据到 home_wifi"""
    global _state
    if _state is not None:
        return _state
    data = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict) or "months" not in data:
        data = {"months": {}}
        # 迁移旧版 monthly_traffic.json（单分类，视为家庭 WiFi）
        try:
            if os.path.exists(LEGACY_FILE):
                with open(LEGACY_FILE, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                for month_key, entry in legacy.items():
                    if not isinstance(entry, dict):
                        continue
                    data["months"][month_key] = {
                        "home_wifi": {
                            "rx": int(entry.get("rx_cumulative", 0)),
                            "tx": int(entry.get("tx_cumulative", 0)),
                        }
                    }
        except (OSError, ValueError):
            pass
    _state = data
    return _state


def _save_state():
    """原子落盘"""
    global _last_write_ts
    if _state is None:
        return
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
        _last_write_ts = time.time()
    except OSError:
        pass


def _read_wlan0_counters():
    """读取 wlan0 收发字节计数器；接口不存在返回 None"""
    try:
        with open("/proc/net/dev", "r") as f:
            for line in f:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                if iface.strip() == "wlan0":
                    parts = rest.split()
                    if len(parts) >= 9:
                        return int(parts[0]), int(parts[8])
                    return None
    except OSError:
        pass
    return None


def _ap_active():
    """AP 模式是否激活（缓存 60s，systemctl 调用有开销）"""
    global _ap_cache
    now = time.time()
    if now - _ap_cache["ts"] < _AP_CACHE_TTL:
        return _ap_cache["value"]
    value = False
    try:
        import wifi_service
        status = wifi_service.get_ap_status()
        value = bool(status.get("ap_active"))
    except Exception:
        value = False
    _ap_cache = {"value": value, "ts": now}
    return value


def _current_category():
    """当前流量角色：AP 激活 → 车辆热点；否则按家庭 WiFi 联网"""
    return "hotspot" if _ap_active() else "home_wifi"


def update():
    """采样并累计当月分类流量（由 broadcaster 周期调用）"""
    global _last_counters, _last_write_ts
    with _lock:
        counters = _read_wlan0_counters()
        if counters is None:
            return
        rx, tx = counters
        state = _load_state()
        month_key = datetime.now().strftime("%Y-%m")
        months = state.setdefault("months", {})
        month = months.setdefault(month_key, {c: {"rx": 0, "tx": 0} for c in CATEGORIES})
        for c in CATEGORIES:
            month.setdefault(c, {"rx": 0, "tx": 0})

        last = state.get("last_counters")
        if last and _last_counters is None:
            # 进程重启：以文件中的 last_counters 续算（服务重启不清零）
            _last_counters = (int(last[0]), int(last[1]))

        if _last_counters is not None:
            last_rx, last_tx = _last_counters
            # 计数器回退（接口重置/驱动重载）→ 增量按当前值补记
            d_rx = rx - last_rx if rx >= last_rx else rx
            d_tx = tx - last_tx if tx >= last_tx else tx
            if d_rx > 0 or d_tx > 0:
                category = _current_category()
                bucket = month[category]
                bucket["rx"] += d_rx
                bucket["tx"] += d_tx

        _last_counters = (rx, tx)
        state["last_counters"] = [rx, tx]
        state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 跨月归档：只保留最近 3 个月
        if len(months) > MONTHS_KEEP:
            for k in sorted(months.keys())[:-MONTHS_KEEP]:
                months.pop(k)

        if time.time() - _last_write_ts >= _WRITE_MIN_INTERVAL:
            _save_state()


def get_monthly():
    """返回当月分类流量（只读，页面/SSE 用）"""
    with _lock:
        state = _load_state()
        month_key = datetime.now().strftime("%Y-%m")
        month = state.get("months", {}).get(month_key, {})
        result = {"month": month_key}
        for c in CATEGORIES:
            entry = month.get(c, {})
            result[c] = {"rx": int(entry.get("rx", 0)), "tx": int(entry.get("tx", 0))}
        return result


def mark_dirty_and_flush():
    """强制落盘（服务关闭钩子可用）"""
    with _lock:
        _save_state()
