#!/usr/bin/env python3
"""
guard_client.py — A 端自检快照 + 心跳发送（自检守护，阶段 A/B）

设计文档：docs/explanation/自检守护-B端VPS设计.md
  §3.1 心跳体快照结构   §3.3 预期离线握手   §4 B 端判据   §5 A 端配合逻辑

设计约束（每条都对应一次真实事故，改动前先读教训索引）：
  1. 默认 enabled=false → 不启线程、不发任何请求，行为与上一版二进制级等价（M116）
  2. 状态文件一律原子写（mkstemp + os.replace，tmp 名不带 pid）——M114/M122 两次踩过
  3. 计时一律用单调时钟，墙钟只做展示字段（M51）
  4. 所有探针函数"绝不抛异常"，失败降级为 None / unknown
  5. 只在后台 daemon 线程运行，绝不阻塞 app.run()（M95）
  6. 发送失败入本地补报队列，恢复后补发（与 sentry_notify_queue 同构，M90）

对外接口：
  load_config() -> dict         读 config/guard.json，失败回落默认（enabled=False）
  build_snapshot() -> dict      采集一次完整快照（不落盘、不发送）
  start_background() -> bool    按配置启动后台线程；已启用则返回 True
  get_last_status() -> dict     读最近一次落盘的 guard_status.json
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import config as app_config

logger = logging.getLogger("GuardClient")

# ─── 路径（全部复用 config.py 常量，禁止再写死一份）───
CONFIG_PATH = os.path.join(app_config.CONFIG_DIR, "guard.json")
STATUS_PATH = os.path.join(app_config.DATA_DIR, "guard_status.json")
QUEUE_PATH = os.path.join(app_config.DATA_DIR, "guard_queue.json")
USB_GUARD_STATUS = os.path.join(app_config.DATA_DIR, "usb_guard_status.json")
PREVIEW_QUEUE = os.path.join(app_config.DATA_DIR, "preview_queue.json")
PREVIEW_BLOCKED = os.path.join(app_config.DATA_DIR, "preview_blocked.json")
MODE_FILE = "/tmp/teslausb_mode"
WIFI_IFACE = "wlan0"

# 自检阈值
THUMB_PENDING_WARN = 50          # 待生成缩略图积压阈值
WECHAT_OK_MAX_AGE = 24 * 3600    # 24h 内有过成功推送算健康
WECHAT_FAIL_STREAK = 3           # 连续失败次数达此值判 fail
GADGET_STALE_S = 300             # usb_guard_status.json 超过此年龄视为守护停摆

DEFAULTS: Dict[str, Any] = {
    "enabled": False,            # 未配置即关闭：保证升级零行为变化
    "token": "",
    "endpoints": {"ts": "", "https": ""},
    "ca_path": "",               # 自签 CA（HTTPS 通道 pinning 用）
    "interval_s": 60,
    "ts_timeout_s": 8,
    "https_timeout_s": 10,
    "probe_every_s": 150,        # 低频 HTTPS 双发探针（必须 < B 的 ALARM=5T=300s）
    "states_interval_s": 300,    # TeslaMate /states 轮询（配额克制）
    "queue_max": 200,
    "offline_default_s": 3600,   # going_offline 的默认窗口
}

# 模块级缓存（线程安全由调用方保证：仅在后台线程使用）
_TS_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_TM_CACHE: Dict[str, Any] = {"at": 0.0, "ok": None}
_STATES_CACHE: Dict[str, Any] = {"at": 0.0, "data": None, "err": None}
_STATES_INFLIGHT = threading.Event()   # 防 /states 拉取线程堆积
_SC_CACHE: Dict[str, tuple] = {}       # systemctl is-active 缓存 {unit: (bool|None, mono)}
_WIFI_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}

_started_lock = threading.Lock()
_sender = None


# ═══════════════════════════════════════════════════════════
# 基础工具
# ═══════════════════════════════════════════════════════════

def _read_text(path: str, limit: int = 4096) -> str:
    """读文本，失败返回空串。不抛异常。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except (OSError, IOError):
        return ""


def _read_json(path: str) -> Optional[Any]:
    """读 JSON，失败/损坏返回 None。不抛异常。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, IOError, ValueError):
        return None


def _fsync_dir(directory: str) -> None:
    """替换后 fsync 目录：车载 ACC 直接断电时，rename 才真正持久（POSIX 才有意义）。"""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write_json(path: str, obj: Any, mode: int = 0o600) -> bool:
    """原子写 JSON：同目录 mkstemp → fsync → os.replace。

    tmp 名由 mkstemp 生成（**禁止用 pid 拼名**：同进程多线程会撞名，M114）。
    """
    directory = os.path.dirname(path) or "."
    tmp = ""
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".atomic-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        # Windows 上 os.replace 撞并发读会抛 WinError 5（Linux 是原子替换不会）。
        # 真机虽为 Linux，但同一份代码会在 Windows 本地跑测试，故加短重试（同 M114 处理）。
        last_err = None
        for _ in range(5):
            try:
                os.replace(tmp, path)
                _fsync_dir(directory)
                return True
            except OSError as e:
                last_err = e
                time.sleep(0.03)
        raise last_err if last_err else OSError("replace failed")
    except Exception as e:
        logger.warning("原子写失败 %s: %s", path, e)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def _tri(value: Any) -> Optional[bool]:
    """三态归一化（M62）：true/false/null/字符串 "nil"/"true"/"false"/数字。

    服务端会把 nil 序列化成字符串 "nil"，**不能写 if value**。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    """严格布尔：**绝不**用 bool(value)——`bool("false") is True`。

    配置是手写的，写 `"enabled": "false"` 是很自然的事；若按真值判断，
    守护会在用户以为「已关闭」的状态下真的往外发心跳（M116 反例）。
    非法值一律回落到 default（enabled 处 default=False，保守方向）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off", ""):
            return False
    return default


def _iso_age(iso_str: Any) -> Optional[float]:
    """ISO 本地时间字符串 → 距今年龄（秒）。解析失败返回 None。"""
    if not isinstance(iso_str, str) or not iso_str:
        return None
    try:
        from datetime import datetime
        ts = datetime.fromisoformat(iso_str).timestamp()
        age = time.time() - ts
        # 时钟回跳会给出负值 → 视为不可信
        return age if age >= 0 else None
    except (ValueError, OSError, OverflowError):
        return None


def _tcp_ok(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (socket.timeout, OSError):
        return False


def _systemctl_active(unit: str) -> Optional[bool]:
    """systemctl is-active → True/False/None(无法判定)。3s 超时，60s 缓存。

    每 60s fork 一个 systemctl 在这台机器上并不便宜（还要跑视频/哨兵），故缓存。
    """
    cached = _SC_CACHE.get(unit)
    now = time.monotonic()
    if cached and now - cached[1] < 60:
        return cached[0]

    result: Optional[bool] = None
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=3,
        )
        out = (r.stdout or "").strip()
        if out == "active":
            result = True
        elif out in ("inactive", "failed", "activating", "deactivating", "unknown"):
            result = False
    except Exception:
        result = None
    _SC_CACHE[unit] = (result, now)
    return result


# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

def load_config() -> Dict[str, Any]:
    """读 config/guard.json；缺失或损坏 → 返回默认（enabled=False）。"""
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝
    raw = _read_json(CONFIG_PATH)
    if not isinstance(raw, dict):
        return cfg

    for key, default in DEFAULTS.items():
        if key == "endpoints":
            continue
        if key in raw:
            cfg[key] = raw[key]

    eps = raw.get("endpoints")
    if isinstance(eps, dict):
        for k in ("ts", "https"):
            v = eps.get(k)
            if isinstance(v, str):
                cfg["endpoints"][k] = v.strip()

    # 类型兜底 + 上下限夹取：配置文件是手写的，一次手滑就可能让守护静默失效
    # （timeout<=0 会让 requests 抛 ValueError → 永久入队；states_interval=0 会打爆 TeslaMate 配额）
    _CLAMP = {
        "interval_s":       (15, 60),        # >60 会让 B 的 3T(180s) 余量不足
        "ts_timeout_s":     (2, 30),
        "https_timeout_s":  (2, 30),
        # 探针周期必须 < B 的 ALARM(5T=300s)：否则 TS 断开时
        # age_https 会随机跨过 300 → 被误判成「设备失联」（见设计 §2 vs §4 的矛盾）
        "probe_every_s":    (60, 150),
        "states_interval_s": (120, 1800),
        "queue_max":        (1, 1000),
        "offline_default_s": (60, 21600),
    }
    for key, (lo, hi) in _CLAMP.items():
        try:
            val = int(cfg[key])
        except (TypeError, ValueError):
            val = DEFAULTS[key]
        if val < lo or val > hi:
            logger.warning("guard.json 的 %s=%s 越界，已夹到 [%s, %s] 内的 %s",
                           key, cfg.get(key), lo, hi, min(max(val, lo), hi))
            val = min(max(val, lo), hi)
        cfg[key] = val

    cfg["enabled"] = _as_bool(cfg.get("enabled"), default=False)
    cfg["token"] = str(cfg.get("token") or "")
    cfg["ca_path"] = str(cfg.get("ca_path") or "")
    return cfg


def _has_channel(cfg: Dict[str, Any]) -> bool:
    eps = cfg.get("endpoints") or {}
    return bool((eps.get("ts") or "").strip() or (eps.get("https") or "").strip())


# ═══════════════════════════════════════════════════════════
# 探针（全部不抛异常）
# ═══════════════════════════════════════════════════════════

def tailscale_state(force: bool = False) -> Dict[str, Any]:
    """`tailscale status --json` → {backend, exit_node, exit_online, ok}，30s 缓存。

    注意字段名：`ExitNodeStatus` 是 Go 的 `ipnstate.ExitNodeStatus{ID, Online, TailscaleIPs}`，
    取 `ID`（不是 `ExitNodeID`——那是 `debug prefs` 顶层字段，两者别混）。
    """
    now = time.monotonic()
    if (not force and _TS_CACHE["data"] is not None
            and now - _TS_CACHE["at"] < 30):
        return dict(_TS_CACHE["data"])

    out: Dict[str, Any] = {"backend": None, "exit_node": None,
                           "exit_online": None, "ok": False}
    try:
        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            data = json.loads(r.stdout)
            if isinstance(data, dict):
                out["backend"] = data.get("BackendState")
                ens = data.get("ExitNodeStatus")
                if isinstance(ens, dict):
                    out["exit_node"] = ens.get("ID") or ens.get("ExitNodeID")
                    out["exit_online"] = ens.get("Online")
                out["ok"] = True
    except Exception:
        pass

    _TS_CACHE["at"] = now
    _TS_CACHE["data"] = dict(out)
    return out


def _wifi_snapshot() -> Dict[str, Any]:
    """WiFi SSID / 信号强度：优先 nmcli，回落 iw。60s 缓存（nmcli+iw 最坏 10s）。"""
    now = time.monotonic()
    if _WIFI_CACHE["data"] is not None and now - _WIFI_CACHE["at"] < 60:
        return dict(_WIFI_CACHE["data"])

    out: Dict[str, Any] = {"wifi_ssid": None, "rssi": None, "connected": None}
    try:
        import wifi_service  # 懒加载：避免 app 启动时连带拉起重依赖
        info = wifi_service.get_current_wifi() or {}
        out["connected"] = bool(info.get("connected"))
        out["wifi_ssid"] = info.get("ssid")
        out["rssi"] = info.get("signal")
    except Exception:
        # 兜底：/proc/net/wireless 里有 wlan0 只能证明「网卡存在」，**不等于已连接**。
        # 早前误把它当 connected=True，会让断网场景的自检假绿（P2-5）。
        try:
            if "wlan0" in _read_text("/proc/net/wireless", 2048):
                out["connected"] = None      # 未知，交给上层判 warn
        except Exception:
            pass

    _WIFI_CACHE["at"] = now
    _WIFI_CACHE["data"] = dict(out)
    return out


def _internet_ok() -> Optional[bool]:
    """单点 TCP 探测外部 DNS：便宜且够用（三重投票留给 system_monitor）。"""
    return _tcp_ok("223.5.5.5", 53, timeout=1.5)


def _teslamate_ok() -> Optional[bool]:
    """TeslaMate 服务 TCP 可达性，60s 缓存。读不到 URL 返回 None。"""
    now = time.monotonic()
    if _TM_CACHE["ok"] is not None and now - _TM_CACHE["at"] < 60:
        return _TM_CACHE["ok"]

    host, port = "", 0
    try:
        cfg = _read_json(app_config.SENTRY_CONFIG_FILE) or {}
        url = str(cfg.get("teslamate_url") or "").strip()
        if url:
            from urllib.parse import urlparse
            p = urlparse(url if "://" in url else "http://" + url)
            host, port = p.hostname or "", int(p.port or 80)
    except Exception:
        host, port = "", 0

    result = _tcp_ok(host, port, timeout=1.5) if host and port else None
    _TM_CACHE["at"] = now
    _TM_CACHE["ok"] = result
    return result


def _mode() -> str:
    """当前意图模式 present/edit（由 misc_routes / usb_gadget_init.sh 维护）。"""
    m = _read_text(MODE_FILE, 32)
    return m if m in ("present", "edit") else ""


def _gadget_snapshot() -> Dict[str, Any]:
    """Gadget 状态：读 usb_guard 原子落盘的 usb_guard_status.json（零 sysfs 写）。"""
    data = _read_json(USB_GUARD_STATUS)
    snap: Dict[str, Any] = {
        "udc_bound": False,
        "udc_connected": False,
        "state": "",
        "mode": _mode(),
        "cable": None,
        "age_s": None,
    }
    if isinstance(data, dict):
        snap["udc_bound"] = bool(data.get("udc"))
        snap["state"] = str(data.get("state") or "")
        snap["udc_connected"] = snap["state"] == "configured"
        snap["cable"] = data.get("cable")
        # usb_guard 写 "YYYY-MM-DD HH:MM:SS"（空格分隔，fromisoformat 3.7+ 可解析）
        snap["age_s"] = _iso_age(str(data.get("updated_at") or "").replace(" ", "T"))
    return snap


def _thumb_snapshot() -> Dict[str, Any]:
    """缩略图链路：服务是否常驻 + 队列积压 + 被阻断数。"""
    entries = _read_json(PREVIEW_QUEUE)
    pending = 0
    if isinstance(entries, list):
        pending = sum(
            1 for e in entries
            if isinstance(e, dict) and e.get("status") in ("pending", "paused")
        )
    blocked = _read_json(PREVIEW_BLOCKED)
    blocked_n = len(blocked) if isinstance(blocked, list) else 0

    return {
        "service": _systemctl_active("teslausb-bgpreview"),
        "queue": pending,
        "blocked": blocked_n,
        # v1 缺口：bgpreview 未落盘 last_scan 时间戳（不让读取方误以为有值）
        "last_scan_age_s": None,
    }


def _wechat_snapshot() -> Dict[str, Any]:
    """企微推送健康：读 push_health.json（PushHealthTracker 写的）。

    注意该文件由 `PushHealthTracker._save_health_data` 用 `open("w")` 直写（非原子），
    恰逢推送时可能读到半截 JSON → 这里重试一次再判「无数据」，
    否则自检会因为别人的写竞态抖成 warn（M114 同类缺陷，读侧先自保）。
    """
    data = _read_json(app_config.PUSH_HEALTH_FILE)
    if data is None:
        time.sleep(0.02)
        data = _read_json(app_config.PUSH_HEALTH_FILE)
    out: Dict[str, Any] = {"last_ok_age_s": None, "consecutive_fail": 0, "bots": 0}
    if not isinstance(data, dict):
        return out
    bots = data.get("bots")
    if not isinstance(bots, dict) or not bots:
        return out

    out["bots"] = len(bots)
    best_age: Optional[float] = None
    worst_streak = 0
    for bot in bots.values():
        if not isinstance(bot, dict):
            continue
        age = _iso_age(bot.get("last_success"))
        if age is not None and (best_age is None or age < best_age):
            best_age = age
        # 连续失败：最近一次失败比最近一次成功更晚 → 取最近失败记录条数
        last_fail = _iso_age(bot.get("last_fail"))
        if last_fail is not None and (age is None or last_fail < age):
            recent = bot.get("recent_failures")
            streak = len(recent) if isinstance(recent, list) else 1
            worst_streak = max(worst_streak, streak)
    out["last_ok_age_s"] = round(best_age) if best_age is not None else None
    out["consecutive_fail"] = worst_streak
    return out


def _states_refresh(cfg: Dict[str, Any]) -> None:
    """拉一次 TeslaMate /states 写入缓存。配额克制：仅按 states_interval_s 调用。"""
    try:
        scfg = _read_json(app_config.SENTRY_CONFIG_FILE) or {}
        base_url = str(scfg.get("teslamate_url") or "").strip()
        password = str(scfg.get("teslamate_password") or "")
        if not base_url:
            _STATES_CACHE.update(at=time.monotonic(), data=None, err="no_url")
            return

        from location_detector import TeslaMateCustomAPI
        api = TeslaMateCustomAPI(
            base_url=base_url,
            password=password,
            token_cache_path=os.path.join(app_config.DATA_DIR, ".teslamate_token.json"),
        )
        states = api.get_states()
        _STATES_CACHE.update(
            at=time.monotonic(),
            data=states if isinstance(states, dict) else None,
            err=None if isinstance(states, dict) else "empty",
        )
    except Exception as e:
        _STATES_CACHE.update(at=time.monotonic(), data=None, err=str(e)[:120])
        logger.debug("拉取 /states 失败: %s", e)


def _states_refresh_guarded(cfg: Dict[str, Any]) -> None:
    """带在途闸的 /states 拉取：上一轮没结束就跳过，防线程堆积。"""
    if _STATES_INFLIGHT.is_set():
        return
    _STATES_INFLIGHT.set()
    try:
        _states_refresh(cfg)
    finally:
        _STATES_INFLIGHT.clear()


def _vehicle_snapshot(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """车辆三态（只读缓存，不在这里发网络请求，避免拖慢心跳节拍）。

    缓存过旧时必须降级为「全 unknown」：否则车已开走，B 仍按旧快照的
    locked=True/sentry=True/user_present=False 判「该在线」→ 断电后误报。
    """
    data = _STATES_CACHE["data"]
    age = None if not _STATES_CACHE["at"] else time.monotonic() - _STATES_CACHE["at"]
    # 阈值必须跟随**实际配置**：写死 DEFAULTS 时，若用户把 states_interval 调到 1800，
    # 900s 就判 stale → 车态长期 unknown → B 的判据退化成「未知按 judge」（误报风险）。
    interval = (cfg or {}).get("states_interval_s", DEFAULTS["states_interval_s"])
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = DEFAULTS["states_interval_s"]
    stale_limit = max(3 * interval, 900)
    stale = age is None or age > stale_limit

    out: Dict[str, Any] = {
        "locked": None,
        "sentry_mode": None,
        "user_present": None,
        "state": None,
        "park_time": None,
        "age_s": round(age) if age is not None else None,
        "source": "unavailable",
    }
    if not isinstance(data, dict):
        return out
    if stale:
        out["source"] = "stale"
        return out

    out["source"] = "teslamate_states"
    out["locked"] = _tri(data.get("locked"))
    out["sentry_mode"] = _tri(data.get("sentry_mode"))
    out["user_present"] = _tri(data.get("is_user_present"))
    st = data.get("state")
    out["state"] = str(st).lower() if isinstance(st, str) else None
    pt = data.get("ui_park_time_str")
    out["park_time"] = pt if isinstance(pt, str) else None
    return out


def _selfcheck(net: Dict[str, Any], gadget: Dict[str, Any],
               thumb: Dict[str, Any], wechat: Dict[str, Any]) -> Dict[str, Any]:
    """四项自检汇总：pass / warn / fail（只检测，不修复）。"""
    items: Dict[str, str] = {}

    # 「探针失效」必须显式暴露成 warn，不能因为拿不到值就当 pass——
    # 四项同时失效还报 pass，正是自检最该发现的那种失败（假绿）。
    if net.get("connected") is False:
        items["net"] = "fail"
    elif net.get("connected") is None or net.get("internet_ok") is not True:
        items["net"] = "warn"
    else:
        items["net"] = "pass"

    g_age = gadget.get("age_s")
    if gadget.get("mode") == "edit":
        items["gadget"] = "pass"          # Edit 模式解绑是预期状态（M103）
    elif gadget.get("udc_connected"):
        # udc_connected 来自 usb_guard 落盘的状态：它自己是常驻服务，
        # 若文件不再更新（进程挂了），"configured" 会永远停在最后一帧 → 假绿。
        if g_age is None or g_age > GADGET_STALE_S:
            items["gadget"] = "warn"
        else:
            items["gadget"] = "pass"
    elif gadget.get("cable") is False:
        items["gadget"] = "warn"          # 线离位 = 车睡了，不是故障
    else:
        items["gadget"] = "fail"

    if thumb.get("service") is False:
        items["thumb"] = "fail"
    elif thumb.get("service") is None or thumb.get("queue") is None:
        items["thumb"] = "warn"           # systemctl/队列文件读不到 → 未知
    elif thumb["queue"] >= THUMB_PENDING_WARN:
        items["thumb"] = "warn"
    else:
        items["thumb"] = "pass"

    age = wechat.get("last_ok_age_s")
    if (wechat.get("consecutive_fail") or 0) >= WECHAT_FAIL_STREAK:
        items["wechat"] = "fail"
    elif age is None or age > WECHAT_OK_MAX_AGE:
        items["wechat"] = "warn"
    else:
        items["wechat"] = "pass"

    if "fail" in items.values():
        result = "fail"
    elif "warn" in items.values():
        result = "warn"
    else:
        result = "pass"
    return {"result": result, "items": items}


def build_snapshot(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """采集一次完整快照（不发送、不落盘）。任何探针失败都降级为 None。"""
    wifi = _wifi_snapshot()
    ts = tailscale_state()
    net = {
        "wifi_ssid": wifi.get("wifi_ssid"),
        "rssi": wifi.get("rssi"),
        "connected": wifi.get("connected"),
        "tailscale": ts.get("backend"),
        "exit_node": ts.get("exit_node"),
        "teslamate_ok": _teslamate_ok(),
        "internet_ok": _internet_ok(),
    }
    gadget = _gadget_snapshot()
    thumb = _thumb_snapshot()
    wechat = _wechat_snapshot()
    vehicle = _vehicle_snapshot(cfg)

    return {
        "seq": 0,                       # 由发送器填
        "ts": int(time.time()),
        "mono": int(time.monotonic() * 1000),
        "version": app_config.APP_VERSION,
        "net": net,
        "gadget": gadget,
        "thumb": thumb,
        "wechat": wechat,
        "vehicle": vehicle,
        "selfcheck": _selfcheck(net, gadget, thumb, wechat),
    }


# ═══════════════════════════════════════════════════════════
# 补报队列
# ═══════════════════════════════════════════════════════════

def _queue_load() -> List[Dict[str, Any]]:
    data = _read_json(QUEUE_PATH)
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _queue_push(body: Dict[str, Any], limit: int) -> None:
    if limit <= 0:                     # items[-0:] 等于全表 → 队列永不淘汰，会写满分区
        return
    items = _queue_load()
    items.append(body)
    if len(items) > limit:
        items = items[-limit:]
    atomic_write_json(QUEUE_PATH, items, mode=0o600)


# ═══════════════════════════════════════════════════════════
# 发送器
# ═══════════════════════════════════════════════════════════

class GuardSender:
    """心跳发送器：TS 优先 → HTTPS 兜底 → 入队补报。

    只在后台线程里跑；对外暴露 start()/stop()，其余方法不应被 Web 请求线程调用。
    """

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = int(time.time()) % 100000
        self._last_probe = time.monotonic()   # 首轮不额外打探针（下一轮起才到周期）
        self._last_states = 0.0
        self._prev_vehicle: Dict[str, Any] = {}
        self._pending_offline: Optional[Dict[str, Any]] = None
        self._pending_tries = 0
        self._pending_last = 0.0
        self._tx_ok = 0
        self._tx_fail = 0
        self._fail_streak = 0
        self._last_error = ""
        self._last_ok_at = 0.0            # 只记录成功，避免状态页把超时说成「刚刚成功」
        self._last_attempt_at = 0.0
        self._queue_count = 0             # 缓存计数：状态轮询不该全量读队列文件
        self._ping_fail = 0

    # ── 生命周期 ──
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="guard-heartbeat")
        self._thread.start()
        logger.info(
            "自检守护心跳已启动（interval=%ss, ts=%s, https=%s）",
            self.cfg["interval_s"],
            bool(self.cfg["endpoints"].get("ts")),
            bool(self.cfg["endpoints"].get("https")),
        )

    def stop(self) -> None:
        self._stop.set()

    # ── 主循环 ──
    def _loop(self) -> None:
        # 首次延后 20s：避开开机时的服务拉起风暴（boot_notify 同期）
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                self._tick()
            except Exception as e:                     # 单轮失败不得终止线程
                logger.warning("心跳轮次异常: %s", e)
            elapsed = time.monotonic() - began
            # 这行在 try 之外：cfg 若未经 load_config 归一化（例如测试或将来有人直接构造），
            # 一个字符串型 interval 就会抛 TypeError 把心跳线程整个打死——静默失联。
            try:
                interval = int(self.cfg.get("interval_s") or 60)
            except (TypeError, ValueError):
                interval = 60
            self._stop.wait(max(1.0, interval - elapsed))

    def _tick(self) -> None:
        now_mono = time.monotonic()

        # TeslaMate /states 独立节奏（300s），不进主节拍
        if now_mono - self._last_states >= self.cfg["states_interval_s"]:
            self._last_states = now_mono
            threading.Thread(target=_states_refresh_guarded, args=(self.cfg,),
                             daemon=True, name="guard-states").start()

        snap = build_snapshot(self.cfg)
        self._seq += 1
        snap["seq"] = self._seq

        atomic_write_json(STATUS_PATH, snap, mode=0o644)

        self._maybe_going_offline(snap)
        self._retry_pending_offline()

        # 先补报、再发新鲜心跳：反过来的话，B 的 last_body 会停留在最旧那条 replay，
        # 车态判据在 ≤60s 窗口内用的是过期快照（P2-1）。
        # 但只在「上一轮确实成功」时补，否则通道不可用时每轮白吃两次超时。
        if self._tx_ok and self._fail_streak == 0:
            self._flush_queue()

        ok, err = self._send(snap, channel=None)
        self._last_attempt_at = time.time()
        if ok:
            self._tx_ok += 1
            self._last_ok_at = self._last_attempt_at
            if self._fail_streak:
                logger.info("心跳已恢复（此前连续失败 %d 次，最后错误: %s）",
                            self._fail_streak, self._last_error)
            self._fail_streak = 0
            self._last_error = ""
        else:
            self._tx_fail += 1
            self._fail_streak += 1
            if err != self._last_error or self._fail_streak == 1:
                # 错误态变化才打 warning：配置错到永远好不了的状态必须有人看见，
                # 否则守护自己坏了，只有翻 /api/guard/status 才发现。
                logger.warning("心跳发送失败（第 %d 次，已入队）: %s",
                               self._fail_streak, err)
            self._last_error = err
            _queue_push(snap, self.cfg["queue_max"])
        self._queue_count = len(_queue_load()) if self._fail_streak else self._queue_count

        # 低频 HTTPS 双发探针 + 反向 ping：让 B 在正常态也知道两条通道的健康
        eps = self.cfg["endpoints"]
        if now_mono - self._last_probe >= self.cfg["probe_every_s"]:
            self._last_probe = now_mono
            if eps.get("https") and eps.get("ts"):
                probe = dict(snap)
                probe["probe"] = True
                self._post(eps["https"], probe, "https", self.cfg["https_timeout_s"])
            self._ping()

    def _ping(self) -> None:
        """反向心跳（设计 §3.2）：GET /guard/ping，确认 B 自己是活的。

        A 只知道「我发不出去」是不够的——B 进程挂了同样发不出去，
        但那是 B 的问题（改 B 就能修），与「A 断网」处置完全不同。

        通道语义必须与 `_post` 完全一致：
          - TS 通道允许 `http://`（走 WireGuard 隧道，不落公网）；
          - HTTPS 通道**必须**是 `https://`，否则 Bearer token 会明文外发。
        """
        eps = self.cfg["endpoints"]
        use_ts = bool((eps.get("ts") or "").strip())
        base = ((eps.get("ts") if use_ts else eps.get("https")) or "").strip()
        if not base:
            return
        url = base.rstrip("/") + "/guard/ping"

        verify: Any = True
        if not use_ts and not url.startswith("https://"):
            # 与 _post 的 https_scheme_required 同一条防线：别让探活路径成为漏洞
            self._ping_fail += 1
            if self._ping_fail == 3:
                logger.warning("B 端 ping 通道配置错误（https 端点不是 https://），已停止发送")
            return
        if url.startswith("https://"):
            ca = (self.cfg.get("ca_path") or "").strip()
            if not ca or not os.path.isfile(ca):
                self._ping_fail += 1
                return
            verify = ca
        try:
            import requests
            r = requests.get(url, headers={"Authorization": "Bearer " + self.cfg["token"]},
                             timeout=5, verify=verify)
            if 200 <= r.status_code < 300:
                if self._ping_fail:
                    logger.info("B 端 ping 已恢复（此前失败 %d 次）", self._ping_fail)
                self._ping_fail = 0
            else:
                self._ping_fail += 1
        except Exception:
            self._ping_fail += 1
        if self._ping_fail == 3:
            logger.warning("B 端连续 3 次 ping 不通：可能 B 侧服务挂了（非 A 断网）")

    # ── 发送 ──
    def _post(self, base: str, body: Dict[str, Any], channel: str,
              timeout: int) -> tuple:
        """单通道投递。返回 (ok, err)。"""
        base = (base or "").strip()
        if not base:
            return False, "endpoint_empty"

        url = base.rstrip("/") + "/guard/heartbeat"
        headers = {
            "Authorization": "Bearer " + self.cfg["token"],
            "X-Guard-Channel": channel,
            "X-Guard-Seq": str(body.get("seq") or 0),
            "Content-Type": "application/json",
        }

        verify: Any = True
        if channel == "https" and not url.startswith("https://"):
            # 手滑把 https 端点写成 http:// → Bearer token 会明文过公网。
            # requests 对 http 会静默忽略 verify，所以必须在这里拦住。
            return False, "https_scheme_required"

        if url.startswith("https://"):
            ca = (self.cfg.get("ca_path") or "").strip()
            if not ca:
                # 自签 + pinning 是既定方案；没配 CA 就拒绝，绝不明文信任系统根
                return False, "https_without_ca"
            if not os.path.isfile(ca):
                return False, "ca_missing"
            verify = ca

        try:
            import requests
            r = requests.post(url, json=body, headers=headers,
                              timeout=timeout, verify=verify)
            if 200 <= r.status_code < 300:
                return True, ""
            return False, "http_%s" % r.status_code
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, str(e)[:80])

    def _send(self, body: Dict[str, Any], channel: Optional[str] = None) -> tuple:
        """按 TS → HTTPS 顺序 failover。channel 指定则只走该通道。"""
        eps = self.cfg["endpoints"]
        order: List[tuple] = []
        if channel:
            order = [(channel, self.cfg["ts_timeout_s"] if channel == "ts"
                      else self.cfg["https_timeout_s"])]
        else:
            if eps.get("ts"):
                order.append(("ts", self.cfg["ts_timeout_s"]))
            if eps.get("https"):
                order.append(("https", self.cfg["https_timeout_s"]))

        errors = []
        for ch, tmo in order:
            ok, err = self._post(eps.get(ch, ""), body, ch, tmo)
            if ok:
                return True, ""
            errors.append("%s=%s" % (ch, err))
        return False, "; ".join(errors) or "no_channel"

    def _flush_queue(self) -> None:
        """补报：通道恢复后补发积压心跳。每轮最多 3 条（防一次性轰炸 B）。

        队列顺序是「旧 → 新」，必须从队头补，否则新心跳先到、旧心跳后到，
        B 侧 last_seen 会被更旧的接收时刻覆盖（虽然 last_seen 记 B 侧时刻，
        last_body 仍会被旧快照盖掉 → 车态误判）。
        """
        items = _queue_load()
        if not items:
            return

        sent = 0
        for body in items[:3]:
            payload = dict(body)
            payload["replay"] = True
            ok, _ = self._send(payload)
            if not ok:
                break
            sent += 1

        if sent:
            remaining = items[sent:]
            atomic_write_json(QUEUE_PATH, remaining[-self.cfg["queue_max"]:], mode=0o600)
            self._queue_count = len(remaining)     # 同步缓存计数，状态页不必再读盘
            logger.info("补报 %d 条历史心跳（剩余 %d）", sent, len(remaining))

    # ── 预期离线握手 ──
    _OFFLINE_WINDOW = {"driving": None, "unlocked": 900, "user_present": 600}
    _OFFLINE_MAX_TRIES = 3

    def _offline_window(self, reason: str) -> int:
        """按 reason 分档：出车给满窗口，解锁/上车只给短窗口。

        三种跃迁共用一个 1h 窗口是错的：解锁车门每天十几次，每次换来 1h 不告警，
        设备恰好在这 1h 里真挂掉就漏报（P2-2）。
        """
        base = self._OFFLINE_WINDOW.get(reason)
        if base is None:
            return self.cfg["offline_default_s"]
        return min(base, self.cfg["offline_default_s"])

    def _maybe_going_offline(self, snap: Dict[str, Any]) -> None:
        """车态从「停着」转「开走/解锁」时先握手，B 端在窗口内静默（防误报）。"""
        v = snap.get("vehicle") or {}
        cur = {
            "locked": v.get("locked"),
            "state": v.get("state"),
            "user_present": v.get("user_present"),
        }
        prev = self._prev_vehicle

        reason = ""
        if prev:
            if prev.get("state") != "driving" and cur.get("state") == "driving":
                reason = "driving"
            elif prev.get("locked") is True and cur.get("locked") is False:
                reason = "unlocked"
            elif prev.get("user_present") is False and cur.get("user_present") is True:
                reason = "user_present"

        # 按字段合并：**只有非 None 的值才写入 prev**。
        # 不能用 cur 整体覆盖——只要有一个字段缺失（TeslaMate 那一拍没返回该字段），
        # 就会把已知状态抹成 None：
        #   · locked 被抹 → 下一轮 `prev.locked is True` 不成立 → 「解锁」跃迁永久丢失
        #   · state 被抹  → 下一轮 `prev.state != 'driving'` 恒真 → 重复判「出车」
        merged = dict(prev) if prev else {}
        for key, val in cur.items():
            if val is not None:
                merged[key] = val
        self._prev_vehicle = merged

        if not reason:
            return

        window = self._offline_window(reason)
        body = {
            "event": "going_offline",
            "seq": self._seq,
            # 相对量才是跨机安全的：A 的墙钟可能未同步（冷启动 87 分钟才 NTP），
            # 给绝对时刻会让 B 算错窗口——快 2h 就漏报 3h，慢 2h 就当场误报（M51 / P0-3）。
            "offline_s": window,
            "expected_until": int(time.time()) + window,   # 兼容旧 B（新版优先 offline_s）
            "reason": reason,
            "version": app_config.APP_VERSION,
        }
        self._pending_offline = body
        self._pending_tries = 0
        self._pending_last = 0.0
        self._try_send_offline()

    def _try_send_offline(self) -> None:
        """握手发送失败要重试：它恰好发生在车刚启动、网络正在重连的时刻。

        fire-and-forget 一次失败 = 这趟出车的误报抑制永久丢失 → 5min 后照常误报（P1-9）。
        """
        body = self._pending_offline
        if not body:
            return
        if self._pending_tries >= self._OFFLINE_MAX_TRIES:
            logger.warning("预期离线握手重试耗尽（reason=%s），放弃", body.get("reason"))
            self._pending_offline = None
            return

        self._pending_tries += 1
        self._pending_last = time.monotonic()
        ok, err = self._send(body, channel=None)
        if ok:
            logger.info("预期离线握手已送达 reason=%s window=%ss",
                        body.get("reason"), body.get("offline_s"))
            self._pending_offline = None
        else:
            logger.warning("预期离线握手失败（第 %d/%d 次），下轮重试: %s",
                           self._pending_tries, self._OFFLINE_MAX_TRIES, err)

    def _retry_pending_offline(self) -> None:
        if self._pending_offline and time.monotonic() - self._pending_last >= 30:
            self._try_send_offline()

    # ── 状态（供只读接口）──
    def stats(self) -> Dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "seq": self._seq,
            "tx_ok": self._tx_ok,
            "tx_fail": self._tx_fail,
            "fail_streak": self._fail_streak,
            "last_error": self._last_error,
            "last_ok_at": self._last_ok_at,       # 只记成功，超时不会伪装成「刚刚成功」
            "last_attempt_at": self._last_attempt_at,
            "queue": self._queue_count,           # 缓存计数，避免每次轮询全量读盘
            "ping_fail": self._ping_fail,
            "pending_offline": bool(self._pending_offline),
        }


# ═══════════════════════════════════════════════════════════
# 对外入口
# ═══════════════════════════════════════════════════════════

def start_background(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """按配置启动后台心跳线程。未启用/未配通道 → 直接返回 False（零副作用）。"""
    global _sender
    cfg = cfg or load_config()

    if not cfg.get("enabled"):
        logger.info("自检守护未启用（config/guard.json enabled=false），跳过")
        return False
    if not cfg.get("token"):
        logger.warning("自检守护已启用但 token 为空，跳过启动")
        return False
    if not _has_channel(cfg):
        logger.warning("自检守护已启用但 endpoints 全空，跳过启动")
        return False

    with _started_lock:
        if _sender is not None:
            return True
        _sender = GuardSender(cfg)
        _sender.start()
    return True


def get_last_status() -> Dict[str, Any]:
    """读最近一次落盘的快照（供 GET /api/guard/status）。

    `available=True` 必须同时满足「文件可解析」和「快照是新的」：
    心跳线程死了或历史遗留文件还在时，状态页绝不能拿三天前的快照当现状（P1-6）。
    """
    cfg = load_config()
    data = _read_json(STATUS_PATH)
    if not isinstance(data, dict):
        return {"available": False, "enabled": bool(cfg.get("enabled"))}

    out = dict(data)
    snap_ts = data.get("ts")
    age = None
    if isinstance(snap_ts, (int, float)):
        age = time.time() - float(snap_ts)
    out["age_s"] = round(age) if age is not None else None

    ttl = max(3 * int(cfg.get("interval_s") or 60), 180)
    fresh = age is not None and 0 <= age <= ttl
    out["available"] = bool(fresh and cfg.get("enabled"))
    out["stale"] = not fresh
    if _sender is not None:
        out["sender"] = _sender.stats()
    return out


def get_sender_stats() -> Dict[str, Any]:
    return _sender.stats() if _sender is not None else {"running": False}
