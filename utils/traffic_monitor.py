"""traffic_monitor.py — 独立网络流量统计（M64，2026-09-05）

设计目标（用户需求）：
- 独立模块：不依赖页面访问，由 broadcaster 周期驱动，不受服务重启/升级影响
- 持久化：数据文件放在版本目录外 /opt/radxa_data/data/（旧文件在 teslausb/data/
  下，升级切换版本目录即丢失——"重启就清零"的主因）
- 分类：**按"当前连的是哪个网络"** 归到 家庭 WiFi / 车辆热点（配置见
  config/sentry.json 的 home_wifi_ssids / hotspot_ssids）。
  v0.3.1.65（教训 M105）：旧实现只看「A7Z 自己的 AP 是否激活」——车里连着
  车载热点时（STA 联网、自己 AP 关着）会被算成"家庭 WiFi"，两栏实际是反的。
- 余量与正确性：计数器回退（接口重置）按增量补记；跨月归档保留 3 个月

数据来源：/proc/net/dev 的 wlan0 计数器（两种角色共用同一物理接口，
tailscale0 流量物理上仍经 wlan0，不重复计）。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime

from utils.ssid_match import as_list, matches

logger = logging.getLogger(__name__)

STATE_FILE = "/opt/radxa_data/data/traffic_stats.json"
LEGACY_FILE = "/opt/radxa_data/teslausb/data/monthly_traffic.json"
SSID_CONFIG_FILE = "/opt/radxa_data/teslausb/config/sentry.json"
CATEGORIES = ("home_wifi", "hotspot", "other")
MONTHS_KEEP = 3
_WRITE_MIN_INTERVAL = 5.0   # 落盘最小间隔（broadcaster 5s 一拍）
_AP_CACHE_TTL = 60.0        # AP 状态缓存（systemctl 调用有开销）
_WIFI_CACHE_TTL = 60.0      # 当前网络/SSID 列表缓存（nmcli 调用有开销）

_lock = threading.Lock()
_last_counters = None       # (rx, tx) 上次采样
_last_write_ts = 0.0
_ap_cache = {"value": False, "ts": 0.0}
# measured=False 表示"这次没拿到权威结论"（探测抛异常），此时保留上一份有效值
_wifi_cache = {"ts": 0.0, "ssid": "", "conn": "", "home": (), "hotspot": (),
               "measured": False}
_last_category = None       # 上次确定的分类：探测不出当前网络时沿用，避免流量进不可见桶
_warned = set()             # 一次性告警去重（别每拍刷日志）
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


def _warn_once(key, msg):
    """同一类告警只打一次，避免 5s 一拍刷爆日志"""
    if key in _warned:
        return
    _warned.add(key)
    logger.warning("traffic_monitor: %s", msg)


def _wifi_context():
    """(缓存 60s) 当前 STA 的 SSID / NM 连接名 + 配置里的家庭·热点 SSID 列表

    关键设计（审查 S1/G3）：**探测失败时绝不写入空值覆盖上一份有效上下文**——
    否则一次 nmcli 抖动/配置被写到一半，就会让后续 60s 的流量全部落进
    前端不展示的 `other` 桶，表现成"统计整个坏掉"，比旧的"数字反了"更难排查。
    失败时也不刷新 ts，下一拍（5s）自然重试。
    """
    global _wifi_cache
    now = time.time()
    if now - _wifi_cache["ts"] < _WIFI_CACHE_TTL:
        return _wifi_cache

    ssid = conn = None          # None = 本次探测无结论
    try:
        import wifi_service
        info = wifi_service.get_current_wifi() or {}
        if info.get("connected"):
            ssid = info.get("ssid") or ""
        else:
            ssid = ""
        conn = wifi_service._get_active_connection_name() or ""
    except Exception as e:
        _warn_once("probe", "读取当前 WiFi 失败（保留上次有效值）: %s" % e)

    home = hotspot = None       # None = 本次读取无结论
    try:
        with open(SSID_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            home = as_list(cfg.get("home_wifi_ssids"))
            hotspot = as_list(cfg.get("hotspot_ssids"))
    except Exception as e:
        _warn_once("cfg", "读取 SSID 配置失败（保留上次有效值）: %s" % e)

    if ssid is None and home is None:
        return _wifi_cache        # 两路都没结论：不改 ts，下一拍重试

    _wifi_cache = {
        "ts": now,
        "ssid": ssid if ssid is not None else _wifi_cache["ssid"],
        "conn": conn if conn is not None else _wifi_cache["conn"],
        "home": home if home is not None else _wifi_cache["home"],
        "hotspot": hotspot if hotspot is not None else _wifi_cache["hotspot"],
        "measured": ssid is not None,
    }
    return _wifi_cache


def _current_category():
    """当前流量的归属分类（v0.3.1.65，教训 M105）。**本函数保证不抛异常。**

    按**现在连的是哪个网络**判（配置 `home_wifi_ssids` / `hotspot_ssids`）：
      ① 命中 home_wifi_ssids                     → home_wifi（在家）
      ② 命中 hotspot_ssids                       → hotspot（在车，走车载热点）
      ③ 两个列表已配置、且**确知**当前连着一个未列出的网络 → other（页面单列）
      ④ 自己在当 AP（没有 STA 关联）              → hotspot
      ⑤ 两个列表都没配置 → 完全回退旧行为（AP 激活=hotspot，否则 home_wifi）
      ⑥ 探测不出当前网络 → 沿用上次分类，实在没有则 home_wifi

    旧实现只有 ⑤ —— 于是「连着车载热点」（自己 AP 关着）的流量全被算进
    了家庭 WiFi，两栏实际是反的。
    """
    global _last_category
    try:
        ctx = _wifi_context()
    except Exception as e:            # 兜底：保证本函数对调用方"永不抛"
        _warn_once("ctx", "读取 WiFi 上下文异常: %s" % e)
        cat = _last_category or ("hotspot" if _ap_active() else "home_wifi")
        _last_category = cat
        return cat
    names = (ctx["ssid"], ctx["conn"])
    has_cfg = bool(ctx["home"] or ctx["hotspot"])

    if not has_cfg:
        cat = "hotspot" if _ap_active() else "home_wifi"
    elif matches(names, ctx["home"]):
        cat = "home_wifi"
    elif matches(names, ctx["hotspot"]):
        cat = "hotspot"
    elif ctx["ssid"] or ctx["conn"]:
        # 确知连着一个两列表都没列出的网络（如手机热点）→ other，页面会单列出来
        cat = "other"
        _warn_once("other:" + str(names), "当前网络未在配置中列出，流量记入 other: %r" % (names,))
    elif _ap_active():
        cat = "hotspot"
    else:
        cat = _last_category or "home_wifi"

    _last_category = cat
    return cat


def update():
    """采样并累计当月分类流量（由 broadcaster 周期调用）"""
    global _last_counters, _last_write_ts
    counters = _read_wlan0_counters()
    if counters is None:
        return
    rx, tx = counters

    # ── 先在锁外取出计数器基线 ──
    with _lock:
        state = _load_state()
        last = state.get("last_counters")
        if last and _last_counters is None:
            # 进程重启：以文件中的 last_counters 续算（服务重启不清零）
            _last_counters = (int(last[0]), int(last[1]))
        prev = _last_counters

    delta = None
    if prev is not None:
        last_rx, last_tx = prev
        # 计数器回退（接口重置/驱动重载）→ 增量按当前值补记
        d_rx = rx - last_rx if rx >= last_rx else rx
        d_tx = tx - last_tx if tx >= last_tx else tx
        if d_rx > 0 or d_tx > 0:
            delta = (d_rx, d_tx)

    # ── 分类也在锁外算 ──
    # _current_category() 可能触发 nmcli / systemctl 子进程（各带超时，最坏合计十几秒）；
    # 放在锁内会连带阻塞 get_monthly()（页面与 SSE 渲染共用同一把非重入锁）。
    category = None
    if delta is not None:
        try:
            category = _current_category()
        except Exception as e:      # _current_category 已保证不抛，这里只是最后兜底
            logger.warning("traffic_monitor: 分类失败，本拍不归集（增量并入下一拍）: %s", e)
            return                  # 不推进计数器 → 该段增量留到下一拍，不丢量
        if category not in CATEGORIES:
            category = None

    with _lock:
        state = _load_state()
        month_key = datetime.now().strftime("%Y-%m")
        months = state.setdefault("months", {})
        month = months.setdefault(month_key, {c: {"rx": 0, "tx": 0} for c in CATEGORIES})
        for c in CATEGORIES:
            month.setdefault(c, {"rx": 0, "tx": 0})

        if delta is not None and category:
            bucket = month[category]
            bucket["rx"] += delta[0]
            bucket["tx"] += delta[1]

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
