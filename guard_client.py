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
import posixpath
import re
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional

import config as app_config

logger = logging.getLogger("GuardClient")

# ─── 路径（全部复用 config.py 常量，禁止再写死一份）───
CONFIG_PATH = os.path.join(app_config.CONFIG_DIR, "guard.json")
SENTRY_CONFIG = os.path.join(app_config.CONFIG_DIR, "sentry.json")
# A7Z 守护机器人的 key 放在 sentry.json（与其它 3 个 bot 同机制），
# 好处：直接复用现成的 GET/POST /api/system/wecom-config、WECOM_BOTS 校验、
# `_disabled` 后缀的禁用语义，UI 只需多一个 bot 条目。
GUARD_BOT_CONFIG_KEY = "wecom_guard_webhook_key"
STATUS_PATH = os.path.join(app_config.DATA_DIR, "guard_status.json")
# 本模块**自己**的推送健康（真实发送路径）。
# 不能借用 push_health.json：那个文件只由 weixin_notifier（哨兵事件推送）写，
# 而自检报告/告警走的是这里的 _push_wecom。判据读别人的文件，就会出现
# 「明明收到了推送，自检却说 172 天没成功过」——M127：先验后用必须同一份数据。
PUSH_HEALTH_OWN = os.path.join(app_config.DATA_DIR, "guard_push_health.json")
_PUSH_HEALTH_LOCK = threading.Lock()
# 报告补发队列**落盘**（v0.3.1.83 / M136）。
# 原来只在内存：A 离家无网时锁车，报告重试 3 档（30/120/600s）后丢弃；
# 车一睡断电（或 web 重启）队列直接消失 → 那条「停车自检」永久丢失。
# 落盘后回网/下次开机补发，并在正文里标注「补发 + 原生成时刻」。
REPORT_QUEUE_PATH = os.path.join(app_config.DATA_DIR, "guard_report_queue.json")
REPORT_QUEUE_MAX = 8              # 与 _push_report 的裁剪口径一致
REPORT_QUEUE_TTL_S = 7 * 86400    # 一周前的补发只会让人困惑，不如不补
QUEUE_PATH = os.path.join(app_config.DATA_DIR, "guard_queue.json")
USB_GUARD_STATUS = os.path.join(app_config.DATA_DIR, "usb_guard_status.json")
PREVIEW_QUEUE = os.path.join(app_config.DATA_DIR, "preview_queue.json")
PREVIEW_BLOCKED = os.path.join(app_config.DATA_DIR, "preview_blocked.json")
PREVIEW_SCAN = os.path.join(app_config.DATA_DIR, "preview_scan.json")
# 僚机版本同步：本地 B 包存放目录 / 升级窗口标记 / 签名身份
PKG_DIR = os.path.join(app_config.DATA_DIR, "guard_pkg")
UPGRADE_WINDOW_PATH = os.path.join(app_config.DATA_DIR, "guard_upgrade_window.json")
PKG_SIGNER_IDENTITY = "a7z-guard"
PKG_SIGN_NAMESPACE = "a7z-guard-pkg"      # 与 A 自身升级包的 namespace `file` 隔离
PKG_MAX_BYTES = 8 * 1024 * 1024
MODE_FILE = "/tmp/teslausb_mode"
WIFI_IFACE = "wlan0"

# 自检阈值
THUMB_PENDING_WARN = 50          # 待生成缩略图积压阈值
WECHAT_OK_MAX_AGE = 24 * 3600    # 24h 内有过成功推送算健康
WECHAT_FAIL_STREAK = 3           # 连续失败次数达此值判 fail
WECHAT_FAIL_WARN_S = 1800        # 单次失败只在这么近的时间内才算「可疑」
GADGET_STALE_S = 300             # usb_guard_status.json 超过此年龄视为守护停摆
THUMB_SCAN_STALE_S = 600         # preview_scan.json 超过此年龄视为扫描循环停摆
                                 # 正常每 30s 扫一次；但一批缩略图的 ffmpeg 可能阻塞
                                 # 扫描循环好几分钟，300s 会在"正在干活"时报假 warn

DEFAULTS: Dict[str, Any] = {
    "enabled": False,            # 未配置即关闭：保证升级零行为变化
    "token": "",
    "endpoints": {"ts": "", "https": ""},
    "ca_path": "",               # 自签 CA（HTTPS 通道 pinning 用）
    "interval_s": 60,
    "ts_timeout_s": 8,
    "https_timeout_s": 10,
    "probe_every_s": 150,        # 低频 HTTPS 双发探针（必须 < B 的 ALARM=5T=300s）
    # 键名保留 states_interval_s（兼容旧配置），实际拉的是 /msg：
    # /states 返回的是服务配置状态，与车辆无关（M133）
    "states_interval_s": 300,
    "queue_max": 200,
    "offline_default_s": 3600,   # going_offline 的默认窗口
    # ── 报告（A 端直连企微，不依赖 B）──
    "report_transition": True,   # 自检结果跨档/恢复时推送
    "report_daily_at": "20:00",  # 每日摘要时刻（空串 = 关闭）
    "report_cooldown_s": 1800,   # 同一档位的去重窗口
    # 锁车后必推一条「停车自检」（用户明确要求：无论正常与否都要收到）。
    # 与 report_transition 是两条独立通道：那条只在档位变化时发，这条是事件型。
    "report_on_lock": True,
    "lock_report_min_gap_s": 600,   # 同一把锁之后 N 秒内不再重复（下车拿东西又锁一次）
    "lock_detect_fallback": True,   # TeslaMate 拿不到车态时，用 SEI / 行车记录仪文件兜底
    "ping_fail_threshold": 3,    # 连续 N 次 ping 不通判「B 侧失联」
    # ── 僚机版本同步 ──
    # B 包的 Ed25519 公钥（**离线签名机**持有私钥；A 只用来预验签）
    # 格式：允许带或不带 principal 前缀，统一按 `a7z-guard <key>` 使用。
    "pkg_pubkey": "",
}

# 模块级缓存（线程安全由调用方保证：仅在后台线程使用）
_TS_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_TM_CACHE: Dict[str, Any] = {"at": 0.0, "ok": None}
# 车辆实时状态缓存。数据源是 /msg 的 raw_data（扁平 95 键），**不是** /states ——
# 后者名字骗人，返回的是「服务配置状态」（auto_del / buttons / schedule …20 键）。
# 这个坑导致 v0.3.1.80 及以前 vehicle.locked/state 恒为 null，
# 连带 "出车/解锁自动握手" 从未生效过（M133）。
_VEHICLE_CACHE: Dict[str, Any] = {"at": 0.0, "data": None, "err": None}
_VEHICLE_INFLIGHT = threading.Event()   # 防 /msg 拉取线程堆积

# 行车记录仪目录（Tesla 命名 2026-11-09_09-37-02-front.mp4，字典序即时间序）
_TESLA_CLIP_DIRS = {
    "SentryClips": "/mnt/teslacam/TeslaCam/SentryClips",
    "SavedClips": "/mnt/teslacam/TeslaCam/SavedClips",
    "RecentClips": "/mnt/teslacam/TeslaCam/RecentClips",
}
_CLIPS_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_CLIPS_TTL_S = 60          # 每拍（60s）最多扫一次目录
_SEI_CACHE: Dict[str, Any] = {"path": "", "at": 0.0, "data": None}
_SEI_BAD: Dict[str, float] = {}      # path -> 最近一次解析失败的时刻（负缓存）
_SEI_BAD_TTL_S = 300
_SEI_MAX_BYTES = 80 * 1024 * 1024   # 超过就跳过：全量读入内存解析，太大不值得
_SC_CACHE: Dict[str, tuple] = {}       # systemctl is-active 缓存 {unit: (bool|None, mono)}
_WIFI_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}

_started_lock = threading.Lock()
# 队列文件的读-改-写必须串行：`apply_config` 换代会短暂出现新旧两个 sender，
# 它们不是同一个对象、拿不到同一把实例锁，只能靠模块级锁保证「只有一个写者」。
_QUEUE_LOCK = threading.Lock()
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


def _load_report_queue() -> List[Dict[str, Any]]:
    """读补发队列。坏文件一律当空处理（读侧必须假设内容坏了，M114/M122）。

    单调钟 `at` 不跨重启，故一律重置为 `None`（=立刻允许补发一次）。
    `wall` 是给用户看的「原生成时刻」，必须落盘。
    """
    raw = _read_json(REPORT_QUEUE_PATH)
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("items")
    if not isinstance(raw, list):
        logger.warning("补发队列格式异常（非 list），忽略并按空处理")
        return []
    now = time.time()
    out: List[Dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "")
        if not text:
            continue
        try:
            wall = float(it.get("wall") or 0.0)
        except (TypeError, ValueError):
            wall = 0.0
        if wall and (now - wall) > REPORT_QUEUE_TTL_S:
            continue                      # 一周前的补发没有意义
        out.append({
            "text": text,
            "critical": bool(it.get("critical")),
            "tries": int(it.get("tries") or 0),
            "at": None,                   # 立即允许重试（见上方注释）
            "wall": wall,
        })
    return out[:REPORT_QUEUE_MAX]


def _save_report_queue(items: List[Dict[str, Any]]) -> None:
    """落盘补发队列；空队列直接删文件（别留一个空壳）。"""
    if not items:
        try:
            os.unlink(REPORT_QUEUE_PATH)
        except OSError:
            pass
        return
    atomic_write_json(REPORT_QUEUE_PATH, {"ver": 1, "items": items}, mode=0o644)


def _report_text(item: Dict[str, Any]) -> str:
    """补发时的正文：标注「补发 + 原生成时刻」。

    不标注的话，用户在几小时后收到一条「停车自检」会以为是刚刚发生的事，
    进而怀疑车态判断错乱——比不补发更糟。
    """
    text = str(item.get("text") or "")
    try:
        wall = float(item.get("wall") or 0.0)
    except (TypeError, ValueError):
        wall = 0.0
    if wall <= 0:
        return text
    return text + "\n\n（补发：本条生成于 %s，当时无网络；现已补发）" % time.strftime(
        "%m-%d %H:%M", time.localtime(wall))


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


_LOCK_CONFLICT_WARN_AT = 0.0


def _warn_lock_conflict() -> None:
    """车态矛盾告警限流（30 分钟一条）。

    每拍都会走到这里，不限流就是每 60s 一条 warning，把日志刷满。
    """
    global _LOCK_CONFLICT_WARN_AT
    now = time.monotonic()
    if now - _LOCK_CONFLICT_WARN_AT < 1800:
        return
    _LOCK_CONFLICT_WARN_AT = now
    logger.warning("车态矛盾：sentry_mode=true 但 locked=false，按「已锁」处理"
                   "（TeslaMate 的 locked 陈旧/漂移，M134）")


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
    return normalize_config(_read_json(CONFIG_PATH))


def normalize_config(raw: Any) -> Dict[str, Any]:
    """把任意 dict（文件内容或 POST 上来的表单）归一化成**可直接使用**的配置。

    所有类型兜底与上下限夹取都在这里：POST 保存时走同一条路径，
    保证「落盘的值」和「运行时的值」永远一致（否则 UI 显示 600 实际跑 150）。
    """
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝
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
        "report_cooldown_s": (300, 7200),
        "lock_report_min_gap_s": (120, 7200),
        "ping_fail_threshold": (2, 6),
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
    cfg["report_transition"] = _as_bool(cfg.get("report_transition"), default=True)
    cfg["report_daily_at"] = _norm_hhmm(cfg.get("report_daily_at"))
    cfg["report_on_lock"] = _as_bool(cfg.get("report_on_lock"), default=True)
    cfg["lock_detect_fallback"] = _as_bool(cfg.get("lock_detect_fallback"), default=True)
    cfg["token"] = str(cfg.get("token") or "")
    cfg["ca_path"] = str(cfg.get("ca_path") or "")
    # 公钥可能是用户从终端粘贴的整行（含 principal 前缀），统一剥成裸 key
    pk = str(cfg.get("pkg_pubkey") or "")
    if pk.startswith(PKG_SIGNER_IDENTITY + " "):
        pk = pk[len(PKG_SIGNER_IDENTITY) + 1:]
    cfg["pkg_pubkey"] = pk.strip()
    return cfg


def _norm_hhmm(value: Any) -> str:
    """`"HH:MM"` 归一化；空 → 关闭日报；非法 → 回落默认（并告警，不静默）。"""
    if value is None or value == "":
        return ""
    s = str(value).strip()
    try:
        parts = s.split(":")
        h, m = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        logger.warning("guard.json 的 report_daily_at=%r 不是 HH:MM，已回落到 %s",
                       value, DEFAULTS["report_daily_at"])
        return str(DEFAULTS["report_daily_at"])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        logger.warning("guard.json 的 report_daily_at=%r 时刻越界，已回落到 %s",
                       value, DEFAULTS["report_daily_at"])
        return str(DEFAULTS["report_daily_at"])
    return "%02d:%02d" % (h, m)


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
    """缩略图链路：服务是否常驻 + 队列积压 + 被阻断数 + 上次扫描年龄。"""
    entries = _read_json(PREVIEW_QUEUE)
    pending = 0
    if isinstance(entries, list):
        pending = sum(
            1 for e in entries
            if isinstance(e, dict) and e.get("status") in ("pending", "paused")
        )
    blocked = _read_json(PREVIEW_BLOCKED)
    blocked_n = len(blocked) if isinstance(blocked, list) else 0

    # 扫描心跳（bg_preview 每 30s 原子写一次）。同机两进程，墙钟可比——
    # M51 禁的是「跨机」用墙钟比大小，不是禁用墙钟本身。
    scan_age: Optional[float] = None
    scan_at = None
    scan = _read_json(PREVIEW_SCAN)
    if isinstance(scan, dict):
        ts = scan.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            scan_age = max(0.0, time.time() - float(ts))
            scan_at = int(ts)

    return {
        "service": _systemctl_active("teslausb-bgpreview"),
        "queue": pending,
        "blocked": blocked_n,
        "last_scan_age_s": round(scan_age, 1) if scan_age is not None else None,
        "last_scan_at": scan_at,
    }


def _num(v, default: float = 0.0) -> float:
    """把任意值安全转成 float（配置文件被手改/跨版本恢复都可能塞进字符串）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _note_push(ok: bool, count: bool = True) -> None:
    """记录一次**真实发送尝试**的结果，供自检的 wechat 项判定。

    写自己的文件而不是 push_health.json：那是 weixin_notifier 的地盘，
    两个写者做「读-改-写」会互相丢更新（M114/M122 同类缺陷）。

    `count=False` 用于**同一份报告的重试**：重试失败不重复累加 streak，
    否则一次 90 秒的网络抖动就能把连续失败推到阈值、把自检刷成 fail。

    **绝不抛异常**：调用点在 B 侧失联状态机里（先发通知、再清去重标记），
    这里抛出去会让「已通知」标记永久卡住 —— 之后真失联也不会再通知。
    """
    try:
        now = time.time()
        with _PUSH_HEALTH_LOCK:
            prev = _read_json(PUSH_HEALTH_OWN) or {}
            if not isinstance(prev, dict):
                prev = {}
            d = {
                "last_ok": _num(prev.get("last_ok")),
                "last_fail": _num(prev.get("last_fail")),
                "consecutive_fail": int(_num(prev.get("consecutive_fail"))),
                "last_attempt_ok": bool(ok),
                "updated": now,
            }
            if ok:
                d["last_ok"] = now
                d["consecutive_fail"] = 0
            else:
                d["last_fail"] = now
                if count:
                    d["consecutive_fail"] = d["consecutive_fail"] + 1
            if not atomic_write_json(PUSH_HEALTH_OWN, d, mode=0o644):
                logger.warning("推送健康记录落盘失败（自检可能读到旧值）")
    except Exception as exc:                       # noqa: BLE001
        logger.warning("推送健康记录异常（不影响发送路径）: %r", exc)


def _wechat_snapshot() -> Dict[str, Any]:
    """企微推送健康：**优先读本模块自己的记录**，读不到才回退 push_health.json。

    为什么必须优先读自己的：`push_health.json` 只由旧的 weixin_notifier（哨兵事件）
    写，而自检报告/告警走本模块的 `_push_wecom`。判据读别人的文件，结果就是
    「用户明明收到了推送，自检报 172 天没成功过」（M127）。

    回退分支仍有价值：升级到本版之前的记录只存在于 push_health.json，
    别让判据直接瞎掉（那时没有 last_attempt_ok，只能按时间窗兜底）。
    """
    key_set = bool(_guard_bot_key())
    own = _read_json(PUSH_HEALTH_OWN)
    if isinstance(own, dict) and (own.get("last_ok") or own.get("last_fail")):
        ok_ts = _num(own.get("last_ok"))
        fail_ts = _num(own.get("last_fail"))
        return {
            "source": "self",
            "bot_key_set": key_set,
            "last_ok_age_s": round(max(0.0, time.time() - ok_ts)) if ok_ts > 0 else None,
            # 单次失败只在近期才算可疑，需要这个时间差
            "last_fail_age_s": round(max(0.0, time.time() - fail_ts)) if fail_ts > 0 else None,
            "consecutive_fail": int(_num(own.get("consecutive_fail"))),
            "last_attempt_ok": bool(own.get("last_attempt_ok")),
            "bots": 1 if key_set else 0,
        }

    data = _read_json(app_config.PUSH_HEALTH_FILE)
    if data is None:
        time.sleep(0.02)
        data = _read_json(app_config.PUSH_HEALTH_FILE)
    out: Dict[str, Any] = {"last_ok_age_s": None, "consecutive_fail": 0, "bots": 0,
                          "source": "legacy", "last_attempt_ok": None,
                          "bot_key_set": key_set, "last_fail_age_s": None}
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


def _vehicle_refresh(cfg: Dict[str, Any]) -> None:
    """拉一次 TeslaMate /msg 的 raw_data 写入缓存。配额克制：按 states_interval_s 调用。

    **为什么不是 /states**：那个端点返回的是「服务配置状态」
    （auto_del / buttons / schedule / mqtt_relay / share_config …共 20 个键），
    与车辆毫无关系。真正的车辆字段在 /msg 的 raw_data 里，是**扁平 95 键**：
    locked / sentry_mode / is_user_present / state / shift_state / speed /
    ui_park_time_str …（没有 vehicle_state 那一层，别套 Tesla 官方嵌套结构）。
    """
    try:
        scfg = _read_json(app_config.SENTRY_CONFIG_FILE) or {}
        base_url = str(scfg.get("teslamate_url") or "").strip()
        password = str(scfg.get("teslamate_password") or "")
        if not base_url:
            _VEHICLE_CACHE.update(at=time.monotonic(), data=None, err="no_url")
            return

        from location_detector import TeslaMateCustomAPI
        api = TeslaMateCustomAPI(
            base_url=base_url,
            password=password,
            token_cache_path=os.path.join(app_config.DATA_DIR, ".teslamate_token.json"),
        )
        raw = api.get_vehicle_data()      # 返回 /msg 的 raw_data
        _VEHICLE_CACHE.update(
            at=time.monotonic(),
            data=raw if isinstance(raw, dict) else None,
            err=None if isinstance(raw, dict) else "empty",
        )
    except Exception as e:
        _VEHICLE_CACHE.update(at=time.monotonic(), data=None, err=str(e)[:120])
        logger.debug("拉取 /msg 失败: %s", e)


def _vehicle_refresh_guarded(cfg: Dict[str, Any]) -> None:
    """带在途闸的 /msg 拉取：上一轮没结束就跳过，防线程堆积。"""
    if _VEHICLE_INFLIGHT.is_set():
        return
    _VEHICLE_INFLIGHT.set()
    try:
        _vehicle_refresh(cfg)
    finally:
        _VEHICLE_INFLIGHT.clear()


def _clip_name_ts(name: str) -> Optional[float]:
    """从 Tesla 文件名取时间。字典序即时间序，比 stat 的 mtime 稳（M52/M58）。"""
    if len(name) < 19:
        return None
    try:
        return time.mktime(time.strptime(name[:19], "%Y-%m-%d_%H-%M-%S"))
    except (ValueError, OverflowError):
        return None


def _latest_clip(dir_path: str) -> Optional[Dict[str, Any]]:
    """取该目录里最新一个 mp4（不排序、不 stat）。

    两种布局都必须支持（`video_service.py:185-187` 写明了）：
      · RecentClips —— **平铺**：RecentClips/2026-11-09_09-37-02-front.mp4
      · SentryClips / SavedClips —— **事件子目录**：
            SentryClips/2026-05-09_17-47-03/2026-05-09_17-47-03-front.mp4
    只扫顶层的话，哨兵目录永远扫不到 mp4（顶层全是目录名），
    于是 L3 的「已锁车」判定永久失效。
    """
    def _scan(folder: str, fixed_ts: float = 0.0):
        best = None
        try:
            with os.scandir(folder) as it:
                for e in it:
                    n = e.name
                    if not n.endswith(".mp4"):
                        continue
                    ts = fixed_ts or _clip_name_ts(n)
                    if not ts:
                        continue
                    if best is None or ts > best[1]:
                        best = (os.path.join(folder, n), ts)
        except OSError:
            return None
        return best

    newest = _scan(dir_path)                 # 平铺（RecentClips）
    best_dir = None
    try:
        with os.scandir(dir_path) as it:     # 事件子目录（SentryClips/SavedClips）
            for e in it:
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                dts = _clip_name_ts(e.name)  # 目录名本身就是事件时间
                if dts and (best_dir is None or dts > best_dir[1]):
                    best_dir = (e.path, dts)
    except OSError:
        pass
    if best_dir:
        inner = _scan(best_dir[0], fixed_ts=best_dir[1])
        if inner and (newest is None or inner[1] > newest[1]):
            newest = inner

    if newest is None:
        return None
    path, ts = newest
    age = time.time() - ts
    # 文件名时间来自车机 GPS 授时，本机钟未同步时会算成「未来」→ 不可信（M51）
    if age < -300:
        return None
    return {"name": os.path.basename(path), "ts": ts, "path": path,
            "age_s": max(0.0, age)}


def _clips_hint() -> Optional[Dict[str, Any]]:
    """L3 兜底：用行车记录仪文件名判断车态（60s 缓存，纯本地，零网络）。

    依据是 2026-06-01 实测结论：
      · SentryClips **只在锁车后**由哨兵录制 —— 有新文件 ≈ 已锁车（最强判据）
      · RecentClips 在行驶中持续写入 —— 有新文件 ≈ 车还在活动
      · SentryClips 里**永远没有 SEI**（哨兵片段不含遥测），所以别指望用它读档位
    """
    now = time.monotonic()
    if _CLIPS_CACHE["data"] is not None and now - _CLIPS_CACHE["at"] < _CLIPS_TTL_S:
        return _CLIPS_CACHE["data"]

    sen = _latest_clip(_TESLA_CLIP_DIRS["SentryClips"])
    rec = _latest_clip(_TESLA_CLIP_DIRS["RecentClips"])
    out: Dict[str, Any] = {
        "sentry_age_s": round(sen["age_s"]) if sen else None,
        "recent_age_s": round(rec["age_s"]) if rec else None,
    }
    # RecentClips 新鲜 = 还在录；两个都新鲜时按「在动」处理（防御，正常不会同时成立）
    if rec and rec["age_s"] <= 120:
        out.update(locked=False, why="recent_clip_writing")
    elif sen and sen["age_s"] <= 600:
        out.update(locked=True, why="sentry_clip_written")
    else:
        out.update(locked=None, why="no_recent_clip")
    _CLIPS_CACHE.update(at=now, data=out)
    return out


def _sei_hint() -> Optional[Dict[str, Any]]:
    """L2 兜底：解析最新**已写完**的 mp4 的 SEI，读档位与车速。

    Tesla 每分钟写一组（同时间戳前缀）4 个摄像头文件。要跳过的是**最新的一组**，
    不是「一个文件」—— 按 `name[:19]` 分组后整体跳过，否则解析的仍是正在写入的那组。
    SentryClips 始终没有 SEI（哨兵片段不含遥测），所以只扫 RecentClips / SavedClips。
    """
    cands: List[str] = []
    for key in ("RecentClips", "SavedClips"):
        d = _TESLA_CLIP_DIRS[key]
        groups: Dict[str, List[str]] = {}
        try:
            with os.scandir(d) as it:
                for e in it:
                    n = e.name
                    if n.endswith(".mp4") and len(n) >= 19:
                        groups.setdefault(n[:19], []).append(n)
        except OSError:
            continue
        for g in sorted(groups, reverse=True)[1:4]:      # 跳过最新一组（可能正在写）
            for n in sorted(groups[g], reverse=True)[:1]:  # 每组取一个摄像头即可
                cands.append(os.path.join(d, n))
    if not cands:
        return None

    now = time.monotonic()
    for path in cands:
        if _SEI_CACHE["path"] == path and _SEI_CACHE["data"] is not None:
            return _SEI_CACHE["data"]
        # 负缓存：解析失败的文件 5 分钟内不再重读（否则稳态每拍重读 26MB）
        bad_at = _SEI_BAD.get(path)
        if bad_at is not None and now - bad_at < _SEI_BAD_TTL_S:
            continue
        try:
            if os.path.getsize(path) > _SEI_MAX_BYTES:
                continue
            from utils.sei_parser import _extract_sei_direct
            rows = _extract_sei_direct(path)
        except Exception as e:
            logger.debug("SEI 解析失败 %s: %s", os.path.basename(path), e)
            _SEI_BAD[path] = now
            continue
        if not rows:
            _SEI_BAD[path] = now
            continue
        last = rows[-1]                   # 最后一条 = 该片段结束时的车态
        out = {"gear": last.get("gear_state"), "speed": last.get("speed_kmh"),
               "file": os.path.basename(path)}
        _SEI_CACHE.update(path=path, at=now, data=out)
        return out
    return None


def _vehicle_snapshot(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """车辆状态（只读缓存，不在这里发网络请求，避免拖慢心跳节拍）。

    三层数据源，按可靠性降级：
      L1 TeslaMate `/msg` 的 raw_data —— locked/shift_state/speed 最权威
      L2 SEI 解析最新**已写完**的 mp4 —— 能读档位/车速；但 SentryClips 无 SEI
      L3 行车记录仪文件名 —— SentryClips 有新文件 ≈ 已锁车（零网络，最抗故障）

    缓存过旧时必须降级为 unknown：否则车已开走，B 仍按旧快照的 locked=True
    判「该在线」→ 断电后误报。
    """
    data = _VEHICLE_CACHE["data"]
    age = None if not _VEHICLE_CACHE["at"] else time.monotonic() - _VEHICLE_CACHE["at"]
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
        "shift_state": None,
        "speed": None,
        "battery": None,          # 电量%（v0.3.1.84：模板里带一行，便于看停车后掉电）
        "age_s": round(age) if age is not None else None,
        "source": "unavailable",
        "detect": "none",        # 锁车态来自哪一层：teslamate / clips / sei / none
    }
    if isinstance(data, dict) and not stale:
        out["source"] = "teslamate_msg"
        # /msg 的 raw_data 是**扁平**结构：字段直接在顶层，没有 vehicle_state 那一层。
        # 早期版本按 Tesla 官方嵌套结构去取 data["vehicle_state"]["locked"]，
        # 结果永远拿不到值（M133）。
        out["locked"] = _tri(data.get("locked"))
        out["sentry_mode"] = _tri(data.get("sentry_mode"))
        out["user_present"] = _tri(data.get("is_user_present"))
        st = data.get("state")
        out["state"] = str(st).lower() if isinstance(st, str) else None
        pt = data.get("ui_park_time_str")
        out["park_time"] = pt if isinstance(pt, str) else None
        ss = data.get("shift_state")
        out["shift_state"] = (str(ss).upper()
                              if isinstance(ss, str) and ss.strip() else None)
        # /msg 里的 speed 是**字符串**（实测 '0'）。只认 int/float 会让它恒为 None，
        # 于是「还在开就别报」的车速闸形同虚设（M134）
        sp = data.get("speed")
        try:
            out["speed"] = float(sp) if sp is not None and str(sp).strip() else None
        except (TypeError, ValueError):
            out["speed"] = None
        # 电量（v0.3.1.84）：停车自检里带一行，方便看"停车后有没有异常掉电"
        bp = data.get("battery_level")
        try:
            out["battery"] = int(float(bp)) if bp is not None and str(bp).strip() else None
        except (TypeError, ValueError):
            out["battery"] = None
        if out["locked"] is not None:
            out["detect"] = "teslamate"
        # TeslaMate 的 locked 会陈旧/漂移：实测「车已锁定 + 哨兵开启」时它返回
        # locked="false"（2026-11-09 实证）。特斯拉语义里**哨兵只在锁车后开启**，
        # 所以这个组合必然是 locked 错。以哨兵为准，否则：
        #   ① 锁车推送永远触发不了（看不到 true）
        #   ② 错误的 false 还会被当成「解锁」，反复给 B 开静默窗口（M134）
        # 归一化必须加「确定停着」这个前提：行驶中/刚解锁时 TeslaMate 的字段
        # 可能不同步（正是 M134 的成因），强行判「已锁」会让 _lock_reported
        # 闩锁一直卡在 True，反而把**真正锁车**那条推送吞掉（审查 P1-4）
        parked = (out["state"] != "driving"
                  and out["shift_state"] not in ("D", "R")
                  and (out["speed"] is None or out["speed"] <= 3))
        if out["sentry_mode"] is True and out["locked"] is False and parked:
            out["locked"] = True
            out["locked_note"] = "sentry_conflict"
            _warn_lock_conflict()
    elif isinstance(data, dict):
        out["source"] = "stale"

    # ── 兜底：L1 拿不到 locked 才启用（车休眠时 /msg 常返回空值）──
    if out["locked"] is None and (cfg or {}).get("lock_detect_fallback", True):
        h = _clips_hint()
        if isinstance(h, dict):
            out["clips_age_s"] = h.get("sentry_age_s")
            out["clips_why"] = h.get("why")
            if h.get("locked") is not None:
                out["locked"] = h["locked"]
                out["detect"] = "clips"
        if out["locked"] is None:
            s = _sei_hint()
            if isinstance(s, dict):
                if out["shift_state"] is None:
                    out["shift_state"] = s.get("gear")
                if out["speed"] is None:
                    out["speed"] = s.get("speed")
                out["detect"] = "sei"
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
    elif (thumb.get("last_scan_age_s") is None
          or thumb["last_scan_age_s"] > THUMB_SCAN_STALE_S):
        # 扫描心跳缺失/过旧：服务 active 但扫描循环已卡住或刚升级完还没跑第一轮。
        # 判 warn 而不是 fail —— 不能因为一个统计文件就报硬故障。
        items["thumb"] = "warn"
    else:
        items["thumb"] = "pass"

    # 判据：连续失败 → fail；能拿到「最近一次尝试」的结果就按它判（通道可用即 pass）。
    #
    # 不再要求「24h 内有过成功」：没消息要发的时候成功记录本来就不会更新，
    # 停着不动的设备会永久挂黄 —— 那正是自检最怕的"狼来了"。
    # 只有回退到旧 push_health.json（拿不到 last_attempt_ok）时才用时间窗兜底。
    streak = int(wechat.get("consecutive_fail") or 0)
    last_attempt_ok = wechat.get("last_attempt_ok")
    age = wechat.get("last_ok_age_s")
    fail_age = wechat.get("last_fail_age_s")
    if wechat.get("bot_key_set") is False:
        # 没配机器人 = 通知能力为零。这是配置问题不是通道抖动，判 warn 提醒去配；
        # 判 fail 会让人以为企微挂了，反而找错方向。
        items["wechat"] = "warn"
    elif streak >= WECHAT_FAIL_STREAK:
        items["wechat"] = "fail"
    elif last_attempt_ok is True:
        items["wechat"] = "pass"
    elif last_attempt_ok is False:
        # 单次失败不能粘住一整天：只有近 30 分钟内才判可疑，否则按通道可用处理
        items["wechat"] = ("warn" if (fail_age is not None
                                      and fail_age <= WECHAT_FAIL_WARN_S) else "pass")
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


def _queue_push(body: Dict[str, Any], limit: int) -> int:
    """入队，返回入队后的长度（-1 = 未写入，调用方保留旧计数）。"""
    if limit <= 0:                     # items[-0:] 等于全表 → 队列永不淘汰，会写满分区
        return -1
    with _QUEUE_LOCK:                  # 读-改-写必须串行（可能有两个 sender 短暂并存）
        items = _queue_load()
        items.append(body)
        if len(items) > limit:
            items = items[-limit:]
        atomic_write_json(QUEUE_PATH, items, mode=0o600)
        return len(items)


# ═══════════════════════════════════════════════════════════
# A 端直连企微（自检报告 / B 侧失联通知）
#   为什么不复用 weixin_notifier：它的推送点全部绑定「系统通知」bot，
#   而守护必须有独立机器人（混在系统通知里一眼看不出来），
#   且「B 挂了」这条恰恰不能经由 B 转发——必须是 A 自己的直连能力。
# ═══════════════════════════════════════════════════════════

_REPORT_RANK = {"pass": 0, "warn": 1, "fail": 2}
_TIER_CN = {"pass": "正常", "warn": "注意", "fail": "异常"}


def _disp_width(text: str) -> int:
    """按显示列数算宽度（中文=2，ASCII=1）——企微里对齐全靠这个。"""
    n = 0
    for ch in str(text or ""):
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _pad(text: str, width: int = 8) -> str:
    """右侧补空格到固定显示宽度（对齐「网络/USB链路/缩略图」这种不等长标签）。

    至少留 1 个空格：`企微推送` 本身就占满 8 列，否则会和状态字粘成「企微推送正常」。
    """
    return str(text or "") + " " * max(1, width - _disp_width(text))
_ITEM_CN = {"net": "网络", "gadget": "USB链路", "thumb": "缩略图", "wechat": "企微推送"}
_B_KIND_CN = {
    "timeout": "连接超时",
    "unreachable": "无法连接",
    "auth": "令牌不匹配",
    "b_5xx": "B 端服务异常",
    "config": "通道配置错误",
}


def _guard_bot_key() -> str:
    """读 A7Z 守护机器人的 webhook key。

    只取主字段：key 被挪到 `_disabled` 说明用户主动禁用了该机器人，
    此时应当真的不发（与 PushHealth 那套禁用语义保持一致）。
    """
    cfg = _read_json(SENTRY_CONFIG)
    if not isinstance(cfg, dict):
        return ""
    return str(cfg.get(GUARD_BOT_CONFIG_KEY) or "").strip()


def _scrub(text: str, key: str = "") -> str:
    """把 key 从错误文本里抹掉——requests 的异常 message 会带完整 URL。

    优先用调用方手里的那把 key：如果靠"重新读一次 sentry.json"，
    配置刚改过就会拿到新 key，旧 key 反而原样留在字符串里。
    """
    if not key:
        try:
            key = _guard_bot_key()
        except Exception:
            return text
    return text.replace(key, "***") if key else text


def _push_wecom(text: str) -> tuple:
    """A 端直连企微。返回 (ok, err)；key 不进日志、不进返回值。"""
    key = _guard_bot_key()
    if not key:
        return False, "no_guard_bot_key"
    try:
        import requests
        r = requests.post(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=8,
        )
        if r.status_code != 200:
            return False, "http_%s" % r.status_code
        try:
            body = r.json()
        except ValueError:
            return True, ""            # 200 但非 JSON：按成功处理，避免重复轰炸
        code = body.get("errcode") if isinstance(body, dict) else 0
        if code not in (0, None):
            return False, "errcode_%s" % code
        return True, ""
    except Exception as e:
        # 先脱敏再截断：反过来的话 key 被切成两半，替换就失效了
        return False, _scrub("%s: %s" % (type(e).__name__, str(e)), key)[:120]


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
        # ── 报告（跃迁 / 恢复 / 日报）──
        self._last_result = ""                    # 上一拍的 selfcheck.result
        self._report_at: Dict[str, float] = {}    # 各档位最近推送时刻（单调钟）
        self._daily_done = ""                     # 已推日报的日期 YYYY-MM-DD
        # ── 锁车事件报告（与档位跃迁是两条独立通道）──
        self._prev_locked: Optional[bool] = None  # 上一拍的 locked（边沿判定）
        self._locked_primed = False               # 首拍只建基准，不补推
        self._lock_report_at: Optional[float] = None   # 上次锁车报告时刻（None=从未）
        self._lock_reported = False               # 这一把锁已经报过（等真实解锁才复位）
        # 「每次重启都补一条」的挂起标记：首拍时车已锁 → 先挂起，
        # 等预热结束再发（预热期自检是半成品，报出去容易看到假 warn）
        self._lock_pending_report = False
        self._offline_at: Dict[str, float] = {}   # reason → 上次握手时刻
        # 待重发的报告用**小列表**而不是单槽：单槽会让「B 侧失联」这类安全通知
        # 被随后的一条自检报告覆盖掉（谁后写谁活），那是最不该丢的一条。
        # v0.3.1.83：队列**落盘**——只在内存时，离家无网锁车那条「停车自检」
        # 重试 3 档后就被丢弃，车一睡断电/进程重启更是直接消失（M136）。
        self._pending_reports: List[Dict[str, Any]] = _load_report_queue()
        self._no_key_warned = False
        self._report_ok = 0
        self._report_fail = 0
        # ── B 侧健康 ──
        self._ping_kind = ""                      # 最近一次 ping 失败的类别
        self._b_down_notified = False             # 已就「B 失联」通知过（去重）
        # 开机/升级后前 5 分钟只跟踪状态、不推报告：这段时间 bgpreview 还没写扫描心跳、
        # WiFi 还在连、systemctl 刚拉起，自检必然短暂 warn。不设这道闸，
        # 每次升级都会收到一条「注意 → 正常」的噪音，把真正的异常淹掉。
        self._warmup_until = time.monotonic() + 300

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
        if self._stop.is_set():
            # 已被 apply_config 停用：绝不能再跑一轮。
            # 否则旧线程可能在新 sender 起来之后还往同一个队列/状态文件写一次，
            # 破坏「队列只有一个写者」的前提。
            return
        now_mono = time.monotonic()

        # TeslaMate /msg 独立节奏（states_interval_s），不进主节拍。
        # 端点必须是 /msg：/states 返回的是服务配置状态，与车辆无关（M133）。
        if now_mono - self._last_states >= self.cfg["states_interval_s"]:
            self._last_states = now_mono
            threading.Thread(target=_vehicle_refresh_guarded, args=(self.cfg,),
                             daemon=True, name="guard-vehicle").start()

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
            queued = _queue_push(snap, self.cfg["queue_max"])
            if queued >= 0:
                self._queue_count = queued   # 用入队返回值，不再额外读一次盘

        # 低频 HTTPS 双发探针 + 反向 ping：让 B 在正常态也知道两条通道的健康
        eps = self.cfg["endpoints"]
        if now_mono - self._last_probe >= self.cfg["probe_every_s"]:
            self._last_probe = now_mono
            if eps.get("https") and eps.get("ts"):
                probe = dict(snap)
                probe["probe"] = True
                self._post(eps["https"], probe, "https", self.cfg["https_timeout_s"])
            self._ping()

        # 报告放在最后：它要发一个可能耗时 8s 的企微请求，绝不能挡在心跳前面
        self._maybe_report(snap)

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
            self._ping_failed("config")
            return
        if url.startswith("https://"):
            ca = (self.cfg.get("ca_path") or "").strip()
            if not ca or not os.path.isfile(ca):
                self._ping_failed("config")
                return
            verify = ca

        try:
            import requests
            r = requests.get(url, headers={"Authorization": "Bearer " + self.cfg["token"]},
                             timeout=5, verify=verify)
        except Exception as e:
            name = type(e).__name__.lower()
            # **必须和 _send 一样做 failover**：TS 断了但 HTTPS 还通时，心跳能发出去，
            # 若 ping 只走 TS 就会误报「B 侧失联」——这是最典型的假告警。
            alt = (eps.get("https") or "").strip()
            if use_ts and alt:
                if self._ping_try(alt, self.cfg.get("ca_path") or ""):
                    self._ping_ok()
                    return
            self._ping_failed("timeout" if "timeout" in name else "unreachable")
            return

        if 200 <= r.status_code < 300:
            self._ping_ok()
        elif r.status_code in (401, 403):
            # 401 与超时必须分开：令牌错是「B 活着但不认我」，超时才是「B 没了」。
            # 混成一句话会把排查方向指错（该改 token 却去查 VPS）。
            self._ping_failed("auth")
        elif r.status_code >= 500:
            alt = (eps.get("https") or "").strip()
            if use_ts and alt and self._ping_try(alt, self.cfg.get("ca_path") or ""):
                self._ping_ok()
                return
            self._ping_failed("b_5xx")
        else:
            self._ping_failed("http_%s" % r.status_code)

    def _ping_try(self, base: str, ca_path: str) -> bool:
        """备用通道单发一次 ping。返回是否 2xx（任何异常都算失败）。"""
        url = base.rstrip("/") + "/guard/ping"
        verify: Any = True
        if url.startswith("https://"):
            if not ca_path or not os.path.isfile(ca_path):
                return False
            verify = ca_path
        else:
            # 备用通道只可能是 https（调用方传的就是 endpoints.https）。
            # 一旦被手改成 http://，绝不能把 Bearer 明文发出去 ——
            # 与 `_post` 的 https_scheme_required 同一条防线。
            return False
        try:
            import requests
            r = requests.get(url, headers={"Authorization": "Bearer " + self.cfg["token"]},
                             timeout=5, verify=verify)
            return 200 <= r.status_code < 300
        except Exception:
            return False

    def _ping_ok(self) -> None:
        if self._b_down_notified:
            logger.info("B 端 ping 已恢复（此前连续失败 %d 次）", self._ping_fail)
            self._push_report(self._fmt_b_recovered(), critical=True)
        self._b_down_notified = False
        self._ping_fail = 0
        self._ping_kind = ""

    def _ping_failed(self, kind: str) -> None:
        self._ping_fail += 1
        self._ping_kind = kind

        # 升级窗口内抑制失联告警：B 一停机重启，ping 必然连续失败——
        # 这是**我们自己制造的**失败，不是它挂了。若照常推送，每次升级都会收到一条
        # 假「B 侧失联」，久了就没人信这条告警（M85：后台操作逆转的状态要落成持久抑制）。
        #
        # 但**只抑制传输层失败**（timeout / unreachable / b_5xx）。
        # auth（令牌错）和 config（通道配错）跟"B 在重启"无关，它们在窗口内
        # 依然要报——尤其是 auth：升级后恰好看不出"令牌被换掉了"是最糟的时机。
        if kind in ("timeout", "unreachable", "b_5xx"):
            left = in_upgrade_window()
            if left > 0:
                logger.info("升级窗口内（剩余 %ds）ping 失败 %s，暂不判失联", left, kind)
                return

        threshold = int(self.cfg.get("ping_fail_threshold") or 3)
        if self._ping_fail < threshold or self._b_down_notified:
            return                       # 去重：一次失联只通知一条，抖回去也不重复
        self._b_down_notified = True
        logger.warning("B 端连续 %d 次 ping 不通（%s）——守护链路半边不可用",
                       self._ping_fail, _B_KIND_CN.get(kind, kind))
        self._push_report(self._fmt_b_down(kind), critical=True)

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
            self._queue_count = 0          # 队列已空，状态页不该还显示旧值（P1-5）
            return

        sent_items = []
        for body in items[:3]:
            payload = dict(body)
            payload["replay"] = True
            ok, _ = self._send(payload)
            if not ok:
                break
            sent_items.append(body)

        if sent_items:
            # 发送在网络里耗时，**不能持锁**；落盘时重新读一次，只删确实发出去的那几条，
            # 避免把这期间新入队的心跳一起覆盖掉（M114 的同一类问题）。
            with _QUEUE_LOCK:
                current = _queue_load()
                remaining = [b for b in current if b not in sent_items]
                capped = remaining[-self.cfg["queue_max"]:]
                atomic_write_json(QUEUE_PATH, capped, mode=0o600)
                self._queue_count = len(capped)
            logger.info("补报 %d 条历史心跳（剩余 %d）", len(sent_items), self._queue_count)

    # ── 预期离线握手 ──
    _OFFLINE_WINDOW = {"driving": None, "unlocked": 900, "user_present": 600}
    # 同类跃迁的握手冷却（秒）。driving 为 0 = **不冷却**：一天多次出车是正常
    # 事件，冷却会把第二趟的静默窗口吞掉，B 在锁车断电后照样误报（审查 P1-2）。
    # unlocked / user_present 才是 TeslaMate 数据漂移的重灾区。
    _OFFLINE_COOLDOWN = {"driving": 0, "unlocked": 1800, "user_present": 1800}
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
        # 只有 TeslaMate 的 locked 才参与「解锁」握手。clips/sei 推出来的是**推断值**
        # （RecentClips 在写 ≠ 车没锁），拿它去给 B 开 900s 静默窗口，
        # 恰好会在「车已锁、设备即将断电」这个最需要告警的窗口把告警关掉。
        # 默认可信，只排除**明确来自兜底**的值：缺字段的老路径不能因此失效，
        # 而 clips/sei 是推断值，绝不能拿来给 B 开静默窗口。
        trusted = str(v.get("detect") or "") not in ("clips", "sei")
        cur = {
            "locked": v.get("locked") if trusted else None,
            "state": v.get("state"),
            "user_present": v.get("user_present"),
            "sentry_mode": v.get("sentry_mode") if trusted else None,
        }
        prev = self._prev_vehicle

        reason = ""
        if prev:
            if prev.get("state") != "driving" and cur.get("state") == "driving":
                reason = "driving"
            elif (prev.get("locked") is True and cur.get("locked") is False
                  and cur.get("sentry_mode") is not True):
                # 哨兵还开着就不可能是真解锁。TeslaMate 的 locked 会漂移，
                # 单看它会给 B 反复开 900s 静默窗口，恰好把真失联告警关掉（M134）
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

        # 同类跃迁冷却：数据漂移会让同一事件反复触发，每次都给 B 开静默窗口
        # = 反复关掉真正的失联告警（M134）。
        # ★ 默认值必须是 None 而不是 0.0：time.monotonic() 返回的是**开机秒数**，
        #   开机不足 1800s 时 `now - 0.0` 会被判成「冷却中」，把首次握手直接吞掉
        #   ——那正好是「每次用车」和「刚升级重启」这两个最关键的时刻（审查 P0）。
        #   冷却时刻在**送达成功**时才记账（见 _try_send_offline），失败不占用。
        last = self._offline_at.get(reason)
        if last is not None:
            cooldown = self._OFFLINE_COOLDOWN.get(reason, 0)
            if cooldown > 0 and time.monotonic() - last < cooldown:
                logger.info("预期离线握手冷却中 reason=%s 剩余 %ds",
                            reason, int(cooldown - (time.monotonic() - last)))
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
            # 冷却只在送达后记账：握手最容易失败的时刻正是「车刚启动、网络在重连」，
            # 失败就占冷却会让这次跃迁在重试期内被永久吞掉（审查 P1-3）
            rs = str(body.get("reason") or "")
            if rs:
                self._offline_at[rs] = time.monotonic()
            self._pending_offline = None
        else:
            logger.warning("预期离线握手失败（第 %d/%d 次），下轮重试: %s",
                           self._pending_tries, self._OFFLINE_MAX_TRIES, err)

    def _retry_pending_offline(self) -> None:
        if self._pending_offline and time.monotonic() - self._pending_last >= 30:
            self._try_send_offline()

    # ── 报告（跃迁 / 恢复 / 日报 / B 失联）──
    # 普通报告：30s / 120s / 600s 三档后丢弃；安全项：永不丢弃，间隔逐步拉长。
    _REPORT_BACKOFF = (30, 120, 600)
    _CRIT_BACKOFF = (30, 60, 300, 900)

    def _device_line(self) -> str:
        eps = self.cfg["endpoints"]
        base = (eps.get("ts") or eps.get("https") or "").strip()
        host = base.split("//")[-1].strip("/")
        return "v%s → %s" % (app_config.APP_VERSION, host or "未配置")

    def _fmt_evidence(self, key: str, net: Dict[str, Any], gad: Dict[str, Any],
                      th: Dict[str, Any], wc: Dict[str, Any],
                      tier: str = "") -> str:
        """每项后面附一句「凭什么这么判」，否则报告只能说『异常』没法排查。"""
        try:
            if key == "net":
                bits = []
                if net.get("wifi_ssid"):
                    bits.append(str(net["wifi_ssid"]))
                if net.get("rssi") is not None:
                    bits.append("%s%%" % net["rssi"])
                bits.append("出网%s" % ("正常" if net.get("internet_ok") else "不通"))
                if net.get("tailscale"):
                    bits.append("TS %s" % net["tailscale"])
                return "（%s）" % " · ".join(bits)
            if key == "gadget":
                bits = ["mode=%s" % (gad.get("mode") or "?")]
                if gad.get("udc_connected"):
                    bits.append("已连接")
                elif gad.get("cable") is False:
                    bits.append("线离位")
                else:
                    bits.append("state=%s" % (gad.get("state") or "?"))
                age = gad.get("age_s")
                if isinstance(age, (int, float)):
                    bits.append("状态%d秒前" % int(age))
                return "（%s）" % " · ".join(bits)
            if key == "thumb":
                bits = ["服务%s" % ("active" if th.get("service") else "非active")]
                bits.append("积压%s" % (th.get("queue") if th.get("queue") is not None else "?"))
                age = th.get("last_scan_age_s")
                bits.append("扫描%s" % (("%d秒前" % int(age))
                                        if isinstance(age, (int, float)) else "无记录"))
                return "（%s）" % " · ".join(bits)
            if key == "wechat":
                age = wc.get("last_ok_age_s")
                if isinstance(age, (int, float)):
                    bits = ["最近成功%d小时前" % int(age // 3600)]
                elif tier == "pass":
                    # 判 pass 却没有成功记录 = 只是"没有待发消息"，不是异常。
                    # 直接写"最近无成功记录"会让人以为通道坏了（自相矛盾）。
                    bits = ["无待发消息"]
                else:
                    bits = ["最近无成功记录"]
                if wc.get("consecutive_fail"):
                    bits.append("连续失败%s" % wc["consecutive_fail"])
                return "（%s）" % " · ".join(bits)
        except Exception:
            pass
        return ""

    def _fmt_items(self, snap: Dict[str, Any]) -> str:
        sc = snap.get("selfcheck") or {}
        items = sc.get("items") or {}
        net = snap.get("net") or {}
        gad = snap.get("gadget") or {}
        th = snap.get("thumb") or {}
        wc = snap.get("wechat") or {}
        lines = []
        for k in ("net", "gadget", "thumb", "wechat"):
            st = str(items.get(k, "unknown"))
            lines.append("· %s：%s%s" % (_ITEM_CN.get(k, k), _TIER_CN.get(st, st),
                                        self._fmt_evidence(k, net, gad, th, wc)))
        return "\n".join(lines)

    def _fmt_transition(self, prev: str, cur: str, snap: Dict[str, Any]) -> str:
        return "\n".join([
            "【A7Z 自检】%s → %s" % (_TIER_CN.get(prev, prev), _TIER_CN.get(cur, cur)),
            self._device_line(),
            self._fmt_items(snap),
        ])

    def _fmt_recovery(self, prev: str, snap: Dict[str, Any]) -> str:
        return "\n".join([
            "【A7Z 自检恢复】%s → 正常" % _TIER_CN.get(prev, prev),
            self._device_line(),
            self._fmt_items(snap),
        ])

    def _fmt_daily(self, snap: Dict[str, Any]) -> str:
        sc = snap.get("selfcheck") or {}
        result = str(sc.get("result") or "unknown")
        eps = self.cfg["endpoints"]
        ch = [c for c in ("TS", "HTTPS") if eps.get("ts" if c == "TS" else "https")]
        b_line = ("正常" if not self._b_down_notified
                  else "失联（%s）" % _B_KIND_CN.get(self._ping_kind, self._ping_kind))
        return "\n".join([
            "【A7Z 自检日报】%s" % _TIER_CN.get(result, result),
            self._device_line(),
            self._fmt_items(snap),
            "通道：%s · 成功 %d / 失败 %d · 队列 %d" % (
                "+".join(ch) or "未配置", self._tx_ok, self._tx_fail, self._queue_count),
            "僚机：%s" % b_line,
        ])

    # ── 停车自检模板（v0.3.1.84，样式 2：结论置顶 + 异常置顶 + 分项对齐）──
    _ITEM_ORDER = ("net", "gadget", "thumb", "wechat")
    _ADVICE = {
        "net": "WiFi/热点与 Tailscale 是否可用",
        "gadget": "USB 线是否在位、车机口是否有电",
        "thumb": "预览积压可在设置页手动清理",
        "wechat": "企微机器人 key 是否还有效",
    }

    def _vehicle_line(self, v: Dict[str, Any]) -> str:
        bits = []
        sh = str(v.get("shift_state") or "").strip()
        if sh:
            bits.append("%s 档" % sh)
        pt = str(v.get("park_time") or "").strip()
        if pt:
            # 00:24:42 → 00:24：秒对"停了多久"没有信息量
            parts = pt.split(":")
            if len(parts) == 3:
                pt = "%s:%s" % (parts[0], parts[1])
            bits.append("停车 %s" % pt)
        bat = v.get("battery")
        if isinstance(bat, int):
            bits.append("电量 %d%%" % bat)
        return "%s%s" % (_pad("车辆"), " · ".join(bits) or "—")

    def _fmt_items_aligned(self, snap: Dict[str, Any]) -> List[str]:
        """分项：全绿时逐行列出；有异常时**异常项置顶**、正常项压成一行。

        只报「注意/异常」是不够的——用户还要知道"凭什么这么判"，所以每行都带证据
        （`_fmt_evidence`），异常行前置 ⚠。
        """
        sc = snap.get("selfcheck") or {}
        items = sc.get("items") or {}
        net = snap.get("net") or {}
        gad = snap.get("gadget") or {}
        th = snap.get("thumb") or {}
        wc = snap.get("wechat") or {}
        bad, good = [], []
        for k in self._ITEM_ORDER:
            st = str(items.get(k, "unknown"))
            line = "%s%s%s" % (_pad(_ITEM_CN.get(k, k)), _TIER_CN.get(st, st),
                               self._fmt_evidence(k, net, gad, th, wc, tier=st))
            if st == "pass":
                good.append((k, line))
            else:
                bad.append((k, line))
        out = ["⚠ " + ln for _k, ln in bad]
        if bad and good:
            out.append("%s%s 正常" % (_pad("其余"), " · ".join(_ITEM_CN[k] for k, _l in good)))
        elif good:
            out.extend(ln for _k, ln in good)
        return out

    def _advice_line(self, snap: Dict[str, Any]) -> str:
        items = (snap.get("selfcheck") or {}).get("items") or {}
        tips = [self._ADVICE[k] for k in self._ITEM_ORDER
                if items.get(k) not in (None, "pass")]
        return ("建议：" + "；".join(tips[:2])) if tips else ""

    def _fmt_locked(self, snap: Dict[str, Any], restarted: bool = False) -> str:
        sc = snap.get("selfcheck") or {}
        result = str(sc.get("result") or "unknown")
        v = snap.get("vehicle") or {}
        # 来源与补报合并进**一个**括号：拆成两个会出现
        # 「（据行车记录仪）（重启后补报）」这种背靠背括号，微信里像两条修饰语打架
        bits = []
        src_cn = {"clips": "据行车记录仪", "sei": "据视频遥测"}.get(
            str(v.get("detect") or ""), "")
        if src_cn:
            bits.append(src_cn)
        if str(v.get("locked_note") or "") == "sentry_conflict":
            bits.append("据哨兵推断")
        if restarted:
            bits.append("重启后补报")
        suffix = ("（%s）" % "·".join(bits)) if bits else ""
        icon = {"pass": "✅", "warn": "⚠️", "fail": "❗"}.get(result, "•")
        tier = _TIER_CN.get(result, "未知")
        head = "【停车自检】%s %s · %s%s" % (
            icon, tier,
            "车辆已锁" if v.get("locked") is True else "车辆已停止（锁车状态未知）",
            suffix)
        lines = [head, self._vehicle_line(v)]
        lines.extend(self._fmt_items_aligned(snap))
        advice = self._advice_line(snap)
        if advice:
            lines.append(advice)
        lines.append("%s · %s" % (self._device_line(), time.strftime("%H:%M")))
        return "\n".join(lines)

    def _fmt_b_down(self, kind: str) -> str:
        if kind == "auth":
            head = "【护卫告警】B 侧在线，但令牌不匹配"
            tail = "→ B 端进程是活的，是两边 token 不一致。到设置页重新保存守护配置即可。"
        elif kind == "config":
            head = "【护卫告警】守护通道配置错误"
            tail = "→ 检查 endpoints（HTTPS 通道必须是 https://，且 ca_path 存在）。"
        else:
            head = "【护卫告警】B 侧失联（%s）" % _B_KIND_CN.get(kind, kind)
            tail = "→ 设备本身在线，是云端僚机连不上。设备若此刻掉线将无人告警。"
        return "\n".join([
            head,
            self._device_line(),
            "连续失败：%d 次（探针每 %ds 一次）" % (self._ping_fail, self.cfg["probe_every_s"]),
            tail,
        ])

    def _fmt_b_recovered(self) -> str:
        # 措辞按原因区分：令牌错/配置错从来没"失联"过，说"已恢复连通"会误导
        kind = self._ping_kind
        if kind == "auth":
            head = "【护卫恢复】令牌已匹配，B 侧重新接受心跳"
        elif kind == "config":
            head = "【护卫恢复】守护通道配置已修正"
        else:
            head = "【护卫恢复】B 侧已恢复连通"
        return "\n".join([
            head,
            self._device_line(),
            "此前连续失败 %d 次（%s）" % (self._ping_fail,
                                        _B_KIND_CN.get(kind, kind)),
        ])

    def _push_report(self, text: str, critical: bool = False) -> None:
        """推一条报告。失败留待重试。

        `critical=True` 用于「B 侧失联 / 已恢复」这类安全通知：它们**永不丢弃**，
        也不会被随后的自检报告挤掉——这条消息本身就是告警体系的一部分。
        """
        ok, err = _push_wecom(text)
        if ok:
            self._report_ok += 1
            _note_push(True)
            logger.info("自检报告已推送（%d 字）", len(text))
            return
        self._report_fail += 1
        if err == "no_guard_bot_key":
            # 没配机器人是配置问题，重试一万次也不会成功——只提示一次，别刷日志
            if not self._no_key_warned:
                self._no_key_warned = True
                logger.warning("自检报告未推送：未配置「A7Z 守护」机器人（设置页 → 健康与通知）")
            return          # 配置缺失不算通道故障，不记入推送健康

        _note_push(False)   # 真正的投递失败才计入连续失败
        # `wall` 必须落盘：补发时要在正文里告诉用户「这条是什么时候的事」
        self._pending_reports.append({
            "text": text,
            "critical": bool(critical),
            "tries": 0,
            "at": time.monotonic(),
            "wall": time.time(),
        })
        self._trim_report_queue()
        _save_report_queue(self._pending_reports)
        logger.warning("自检报告推送失败（留待重试，待发 %d 条）: %s",
                       len(self._pending_reports), err)

    def _trim_report_queue(self) -> None:
        """只裁剪**普通**报告（保留最新 3 条），安全项一律保留。

        安全项（critical）是告警体系的一部分：B 侧失联/停车自检被丢一条，
        用户就完全不知道发生过什么。
        """
        if len(self._pending_reports) <= REPORT_QUEUE_MAX:
            return
        crit = [i for i in self._pending_reports if i.get("critical")]
        normal = [i for i in self._pending_reports if not i.get("critical")]
        self._pending_reports = crit + normal[-3:]

    def _retry_pending_report(self) -> None:
        if not self._pending_reports:
            return
        # 安全项优先：B 失联不能排在自检报告后面，更不能被它挤掉
        item = next((i for i in self._pending_reports if i.get("critical")),
                    self._pending_reports[0])
        tries = int(item.get("tries") or 0)
        backoff = self._CRIT_BACKOFF if item.get("critical") else self._REPORT_BACKOFF
        wait = backoff[min(tries, len(backoff) - 1)]
        # `at` 用 None 表示「刚从磁盘读出来」：单调钟跨重启无意义，
        # 重启后必须**立刻**补发一次，而不是等满一个退避窗（M134 的 0.0 陷阱）
        at = item.get("at")
        if at is not None and (time.monotonic() - float(at)) < wait:
            return

        item["tries"] = tries + 1
        item["at"] = time.monotonic()
        _save_report_queue(self._pending_reports)   # 「第几次」也要跨重启可续
        ok, err = _push_wecom(_report_text(item))
        if ok:
            self._pending_reports.remove(item)
            _save_report_queue(self._pending_reports)
            self._report_ok += 1
            _note_push(True)
            logger.info("自检报告补发成功（待发 %d 条）", len(self._pending_reports))
            return

        _note_push(False, count=False)   # 同一份报告的重试不重复累加 streak
        if not item.get("critical") and item["tries"] >= len(self._REPORT_BACKOFF):
            self._pending_reports.remove(item)
            _save_report_queue(self._pending_reports)
            logger.warning("自检报告重试耗尽，丢弃 1 条（待发 %d 条）",
                           len(self._pending_reports))
        else:
            logger.warning("自检报告补发失败（第 %d 次，%s）: %s",
                           item["tries"], "安全项" if item.get("critical") else "普通", err)

    def _maybe_report(self, snap: Dict[str, Any]) -> None:
        self._retry_pending_report()

        sc = snap.get("selfcheck") or {}
        result = str(sc.get("result") or "unknown")
        prev = self._last_result
        # 状态**永远**先更新：预热期只是不推送，不能连"上一拍是什么"都不记，
        # 否则预热结束后第一次比较会拿不到基准（或拿到很旧的值）。
        self._last_result = result

        # 预热期内不出任何报告（含日报）：这段时间 bgpreview 还没写扫描心跳，
        # 日报会把「注意」当成常态发出去，反而让人以为设备一直有问题。
        if time.monotonic() < self._warmup_until:
            # 预热期只抑制推送，**仍要跟踪锁车边沿**：否则重启后 5 分钟内锁车的
            # 事件会永久丢失（预热结束后第一拍只会「建基准」）。
            self._maybe_lock_report(snap, dry_run=True)
            return

        self._maybe_daily_report(snap)

        # 锁车是事件型通知，优先于档位跃迁：这一拍刚锁车就只发停车自检。
        # 否则「锁车 → gadget 判 warn」会同时触发跃迁报告，用户收到两条重复消息。
        if self._maybe_lock_report(snap):
            return

        # 跃迁/恢复：只在两个**已知档位**之间判定。
        # unknown（探针集体失效、刚开机）不参与，否则每次重启都会误报一次跃迁。
        if (self.cfg.get("report_transition") and prev in _REPORT_RANK
                and result in _REPORT_RANK and prev != result):
            now_mono = time.monotonic()
            if now_mono - self._report_at.get(result, 0.0) >= self.cfg["report_cooldown_s"]:
                self._report_at[result] = now_mono
                if result == "pass":
                    self._push_report(self._fmt_recovery(prev, snap))
                else:
                    self._push_report(self._fmt_transition(prev, result, snap))

    def _maybe_lock_report(self, snap: Dict[str, Any], dry_run: bool = False) -> bool:
        """锁车 → 推一份停车自检（无论档位）。返回是否已推送。

        用户明确要求「车辆锁车后无论正常与否都要推送」。这与 report_transition
        （只在档位变化时报）互不重叠：那条是状态型，这条是事件型。

        用「这一把锁是否已报过」的**闩锁**，而不是只看时间窗：车休眠时 /msg 常返回
        nil，`locked` 会在 True→None→True 之间抖动，光靠时间窗一晚上能推六条。
        """
        cfg = self.cfg
        v = snap.get("vehicle") or {}
        cur = v.get("locked")

        # 首拍：重启时车已锁**不等于**这条锁车事件已经报过。
        # 旧实现写 `_lock_reported = (cur is True)`，等于把「重启那一刻车锁着」
        # 判成「这一把锁已通知」——10:37 锁车、10:48 重启，那条事件就被永久吞掉，
        # 之后除非再解锁一次永远不会推（2026-11-09 实证，M134）。
        # 现在按用户要求「每次重启都补一条」：先挂起，预热结束后补发。
        if not self._locked_primed:
            self._locked_primed = True
            self._lock_reported = False
            self._lock_pending_report = (cur is True)
            if self._lock_pending_report:
                logger.info("重启时车已锁 → 挂起一条停车自检，待预热结束后补发")
            return False

        # 基准只在拿到已知值时更新：None 不得覆盖已知状态，否则抖动会重新「上膛」
        if cur is not None:
            self._prev_locked = cur

        if cur is False:
            self._lock_reported = False          # 只有真实解锁才复位
            self._lock_pending_report = False    # 已解锁，补报没有意义
        if cur is not True:
            return False
        if self._lock_reported and not self._lock_pending_report:
            return False
        if dry_run:
            return False                         # 预热期：只跟踪，不推送

        if not cfg.get("report_on_lock", True):
            # 用户明确不想收 → 这条锁车事件直接视为「已处理」，挂起作废。
            # 只清 pending 不够：首拍时 _lock_reported 是 False，重开开关后
            # 立刻会补推一条标着「重启后补报」的旧事件（审查 P1-1）
            self._lock_pending_report = False
            self._lock_reported = True
            return False

        # 还在开就别报：等红灯挂 P 也会让 locked 抖动，加一道车速/档位闸
        gear = str(v.get("shift_state") or "").upper()
        sp = v.get("speed")
        if gear in ("D", "R") or (isinstance(sp, (int, float)) and sp > 3):
            # 车在动就不该补一条「停车自检」；挂起标记一并作废，
            # 否则它会一直挂着，等几小时后挂 P 档时突然报一条（审查 P2-4）
            self._lock_pending_report = False
            return False

        # 同 P0：0.0 当哨兵在 monotonic（开机秒数）下会误判成「冷却中」，
        # 冷启动 10 分钟内锁车时补报会被拖住。None = 从未报过
        now_mono = time.monotonic()
        gap = cfg.get("lock_report_min_gap_s", 600)
        if self._lock_report_at is not None and now_mono - self._lock_report_at < gap:
            return False                    # 下车拿东西又锁一次，不重复打扰
        pending = self._lock_pending_report
        self._lock_report_at = now_mono
        self._lock_reported = True
        self._lock_pending_report = False
        # critical=True：这条是用户点名要的「锁车后无论正常与否都要推」，
        # 不能因为当时没网就被丢弃/挤出队列（普通报告 3 档后退避耗尽即丢）。
        self._push_report(self._fmt_locked(snap, restarted=pending), critical=True)
        logger.info("停车自检已发出（车态来源=%s, 档位=%s, 补报=%s）",
                    v.get("detect") or "none", gear or "-", pending)
        return True

    def _maybe_daily_report(self, snap: Dict[str, Any]) -> None:
        at = str(self.cfg.get("report_daily_at") or "").strip()
        if not at:
            return
        try:
            hh, mm = at.split(":")
            target = int(hh) * 60 + int(mm)
        except (ValueError, IndexError):
            return
        lt = time.localtime()
        today = time.strftime("%Y-%m-%d", lt)
        if self._daily_done == today:
            return
        cur = lt.tm_hour * 60 + lt.tm_min
        # 用取模而不是直接相减：target=00:05 时 23:59 → 1434 ≥10 会整天不触发
        if (cur - target) % 1440 < 10:     # 10 分钟宽限：错过整分也能补上
            self._daily_done = today
            self._push_report(self._fmt_daily(snap))

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
            # 年龄在 A 端算好再给前端：浏览器用自己的墙钟减 A 的时间戳，
            # 手机时钟差 10 分钟就会显示「10m前」，而实际刚发过。
            "last_ok_age_s": (round(time.time() - self._last_ok_at)
                              if self._last_ok_at else None),
            "last_attempt_at": self._last_attempt_at,
            "queue": self._queue_count,           # 缓存计数，避免每次轮询全量读盘
            "ping_fail": self._ping_fail,
            "ping_kind": self._ping_kind,         # timeout/unreachable/auth/b_5xx/config
            "b_down": self._b_down_notified,      # 已就「B 失联」通知过（去重标记）
            "report_ok": self._report_ok,
            "report_fail": self._report_fail,
            "report_pending": len(self._pending_reports),
            "last_result": self._last_result,
            "pending_offline": bool(self._pending_offline),
            # 锁车报告挂起中（重启时车已锁、等预热结束）：用户说「又没收到」时
            # 能一眼区分「挂起中」和「根本没触发」
            "lock_pending": self._lock_pending_report,
            # 升级窗口剩余秒数：不显示出来的话，用户看到"心跳正常但没告警"
            # 会以为是守护坏了（其实是我们主动压着）
            "upgrade_window": in_upgrade_window(),
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


def apply_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """热更新：把新配置套到运行中的心跳线程上（**不重启 web**）。

    「配了没反应」是这类功能最常见的投诉源：sender 是启动时创建的，
    若不就地替换 `cfg`，用户改完设置必须手动重启服务才生效。
    返回 {started, stopped, updated}，供接口回给前端。
    """
    global _sender
    cfg = cfg or load_config()
    out = {"started": False, "stopped": False, "updated": False}

    runnable = bool(cfg.get("enabled") and cfg.get("token") and _has_channel(cfg))

    with _started_lock:
        if not runnable:
            if _sender is not None:
                old, _sender = _sender, None
                old.stop()
                # 必须等它真的退出再放开：否则就地新建 sender 会有**两个心跳线程并存**，
                # 各自带一份 cfg 往同一个队列/计数/报告里写（T63 之前踩过同类竞态）。
                try:
                    if old._thread is not None:
                        old._thread.join(timeout=2.0)
                except RuntimeError:
                    pass
                out["stopped"] = True
                logger.info("自检守护已停用（配置变更）")
            return out
        if _sender is None:
            _sender = GuardSender(cfg)
            _sender.start()
            out["started"] = True
            return out
        # 就地换 cfg：线程每轮都从 self.cfg 读参数，下一轮即生效
        _sender.cfg = cfg
        out["updated"] = True
    return out


def public_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给前端的配置视图：**绝不包含明文 token**。"""
    cfg = cfg or load_config()
    token = str(cfg.get("token") or "")
    out = {k: v for k, v in cfg.items() if k != "token"}
    out["token_set"] = bool(token)
    out["token_tail"] = ("…" + token[-4:]) if len(token) >= 8 else ("…" if token else "")
    out["bot_key_set"] = bool(_guard_bot_key())
    return out


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


def is_running() -> bool:
    s = _sender            # 先取局部：另一个线程可能把 _sender 置 None
    if s is None:
        return False
    t = s._thread
    return bool(t and t.is_alive())


def push_bot_to_b(timeout: int = 5) -> tuple:
    """把 A 端的守护机器人 key 同步给 B（「保存即双写」的第二步）。

    返回 (ok, err)。**B 不通不影响本地保存**：自检报告与「B 侧失联」都由 A 直连企微，
    只有「设备失联」那条仍需要 B 自己持有 key —— 所以同步失败只提示，不回滚本地配置。
    """
    key = _guard_bot_key()
    if not key:
        return False, "no_guard_bot_key"

    cfg = load_config()
    eps = cfg.get("endpoints") or {}
    candidates = []
    if (eps.get("ts") or "").strip():
        candidates.append(("ts", eps["ts"].strip()))
    if (eps.get("https") or "").strip():
        candidates.append(("https", eps["https"].strip()))
    if not candidates:
        return False, "no_endpoint"

    headers = {"Authorization": "Bearer " + cfg["token"],
               "Content-Type": "application/json"}
    ca = (cfg.get("ca_path") or "").strip()
    errors = []

    # 与 _send 一样走 failover：TS 段断掉时仍能从 HTTPS 把 key 送达
    for ch, base in candidates:
        url = base.rstrip("/") + "/guard/config"
        verify: Any = True
        if ch == "https" and not url.startswith("https://"):
            errors.append("https=https_scheme_required")   # 别把 key 明文送公网
            continue
        if url.startswith("https://"):
            if not ca or not os.path.isfile(ca):
                errors.append("%s=https_without_ca" % ch)
                continue
            verify = ca
        try:
            import requests
            r = requests.post(url, json={"wecom_key": key}, headers=headers,
                              timeout=timeout, verify=verify)
            if 200 <= r.status_code < 300:
                return True, ""
            errors.append("%s=http_%s" % (ch, r.status_code))
        except Exception as e:
            errors.append("%s=%s" % (ch, _scrub("%s: %s" % (type(e).__name__, str(e)), key)[:80]))

    return False, "; ".join(errors)


def send_test_report() -> tuple:
    """设置页「发送测试」：立刻用当前快照推一条自检报告。

    刻意由 **A 自己**发（直连企微），不经 B —— 因为要验证的正是
    「A → 企微」这条独立链路；若经 B 转发，B 挂了就永远测不出来。
    """
    cfg = load_config()
    # 优先用落盘的新鲜快照：现场采集要走 systemctl/nmcli/tailscale，最坏十几秒，
    # 而这是**用户在页面上点按钮**触发的 Web 请求，不能让它等十几秒。
    snap = _read_json(STATUS_PATH)
    fresh = False
    if isinstance(snap, dict):
        ts = snap.get("ts")
        ttl = max(3 * int(cfg.get("interval_s") or 60), 180)
        if isinstance(ts, (int, float)) and 0 <= time.time() - float(ts) <= ttl:
            fresh = True
    if not fresh:
        snap = build_snapshot(cfg)
    sc = snap.get("selfcheck") or {}
    items = sc.get("items") or {}
    eps = cfg.get("endpoints") or {}
    lines = ["【A7Z 自检 · 测试消息】",
             "版本 v%s" % app_config.APP_VERSION]
    for k in ("net", "gadget", "thumb", "wechat"):
        st = str(items.get(k, "unknown"))
        lines.append("· %s：%s" % (_ITEM_CN.get(k, k), _TIER_CN.get(st, st)))
    lines.append("通道：TS %s · HTTPS %s" % (
        "已配" if (eps.get("ts") or "").strip() else "未配",
        "已配" if (eps.get("https") or "").strip() else "未配"))
    lines.append("心跳线程：%s" % ("运行中" if is_running() else "未运行"))
    ok, err = _push_wecom("\n".join(lines))
    # 测试即验证：用户点这一下就是为了确认「A → 企微」通不通，
    # 结果必须计入推送健康，否则点成功自检仍显示 warn，没人会信。
    _note_push(ok)
    return ok, err


# ═══════════════════════════════════════════════════════════
# 僚机版本同步（v0.3.1.78）
#
# 定位：A 端只是**分发者 + 发起者**，不是签发者。
#   · 包的 Ed25519 私钥在离线开发机（A 上只有公钥，只做预验签）
#   · B 端会独立再验一次签，并且**要人工输确认码**才切换
# 这样即使 A 被完全拿下，攻击者最多让 B 下载一个「真实发布过的包」，无法让它生效。
# ═══════════════════════════════════════════════════════════

def in_upgrade_window() -> int:
    """升级窗口剩余秒数（0 = 不在窗口内）。

    落盘而不是只放内存：窗口跨进程重启仍要有效，
    否则"升级中重启了一次 web"就立刻冒出一条假「B 侧失联」。
    """
    d = _read_json(UPGRADE_WINDOW_PATH)
    if not isinstance(d, dict):
        return 0
    until = d.get("until")
    if not isinstance(until, (int, float)):
        return 0
    left = int(float(until) - time.time())
    return left if left > 0 else 0


def open_upgrade_window(seconds: int = 900, note: str = "") -> None:
    body = {"until": int(time.time()) + int(seconds),
            "note": note,
            "at": int(time.time())}
    atomic_write_json(UPGRADE_WINDOW_PATH, body, mode=0o600)


def close_upgrade_window() -> None:
    try:
        os.remove(UPGRADE_WINDOW_PATH)
    except OSError:
        pass


def _pkg_pubkey(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or load_config()
    pk = str(cfg.get("pkg_pubkey") or "").strip()
    if pk.startswith(PKG_SIGNER_IDENTITY + " "):
        pk = pk[len(PKG_SIGNER_IDENTITY) + 1:]
    return pk


def _extract_from_tar(raw: bytes, wanted: str) -> Optional[bytes]:
    """从 tar.gz 里只取指定文件（不落盘、不解压其它内容）。"""
    import io as _io
    import tarfile as _tarfile
    try:
        with _tarfile.open(fileobj=_io.BytesIO(raw), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                if posixpath.basename(posixpath.normpath(m.name)) == wanted:
                    f = tf.extractfile(m)
                    if f is None:
                        return None
                    return f.read(PKG_MAX_BYTES)
    except (_tarfile.TarError, OSError):
        return None
    return None


def verify_package(raw: bytes, cfg: Optional[Dict[str, Any]] = None) -> tuple:
    """A 端预验签：**只证明「manifest 是这把私钥签的」**，不做逐文件 hash 复核。

    返回值 (ok, manifest_dict, err_code)。

    边界必须说清楚（UI 文案也据此写）：
      · 它拦得住"不是我签的包"和"清单被改过的包"；
      · 它拦不住"清单没动、载荷文件被换了"——那由 B 端逐文件核 hash 兜住
        （`guardian-b/upgrade.py` 的 stage_package，以及落地前的二次复核）。
    所以 A 端这层的价值是 **fail fast**：坏包别上传、别占 B 的带宽和磁盘，
    而不是"我已经验证过内容了"。
    """
    cfg = cfg or load_config()
    if len(raw) > PKG_MAX_BYTES:
        return False, None, "pkg_too_large"
    pk = _pkg_pubkey(cfg)
    if not pk:
        return False, None, "no_pkg_pubkey"

    man_raw = _extract_from_tar(raw, "manifest.json")
    sig_raw = _extract_from_tar(raw, "manifest.json.sig")
    if not man_raw:
        return False, None, "no_manifest"
    if not sig_raw:
        return False, None, "no_signature"

    import tempfile as _tempfile
    tmpdir = _tempfile.mkdtemp(prefix="a7zpkg-")
    try:
        allowed = os.path.join(tmpdir, "allowed")
        with open(allowed, "w", encoding="utf-8") as f:
            f.write("%s %s\n" % (PKG_SIGNER_IDENTITY, pk))
        os.chmod(allowed, 0o600)
        sigf = os.path.join(tmpdir, "m.sig")
        with open(sigf, "wb") as f:
            f.write(sig_raw)
        try:
            r = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", allowed,
                 "-I", PKG_SIGNER_IDENTITY, "-n", PKG_SIGN_NAMESPACE, "-s", sigf],
                input=man_raw, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False, None, "ssh_keygen_failed"
        if r.returncode != 0:
            return False, None, "bad_signature"
    finally:
        try:
            for n in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, n))
            os.rmdir(tmpdir)
        except OSError:
            pass

    try:
        man = json.loads(man_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False, None, "bad_manifest"
    if not isinstance(man, dict) or man.get("kind") != "guardian-b":
        return False, None, "bad_manifest"
    ver = str(man.get("version") or "")
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", ver):
        return False, None, "bad_version"
    return True, man, ""


def save_local_package(raw: bytes, cfg: Optional[Dict[str, Any]] = None) -> tuple:
    """验签通过后落地到 data/guard_pkg/guardian-b-v<ver>.tar.gz。"""
    ok, man, err = verify_package(raw, cfg)
    if not ok:
        return False, err, None
    ver = str(man.get("version"))
    os.makedirs(PKG_DIR, exist_ok=True)
    path = os.path.join(PKG_DIR, "guardian-b-v%s.tar.gz" % ver)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)

    # 只保留最近 2 个版本：A7Z 是 959MB 内存的小机器，别让包堆满分区
    items = list_local_packages()
    for it in items[2:]:
        try:
            os.remove(it["path"])
        except OSError:
            pass
    return True, "", {"version": ver, "path": path, "bytes": len(raw),
                      "files": len(man.get("files") or [])}


def list_local_packages() -> List[Dict[str, Any]]:
    """本地已签名的 B 包（按版本倒序）。"""
    out: List[Dict[str, Any]] = []
    try:
        names = os.listdir(PKG_DIR)
    except OSError:
        return out
    for n in names:
        if not (n.startswith("guardian-b-v") and n.endswith(".tar.gz")):
            continue
        p = os.path.join(PKG_DIR, n)
        ver = n[len("guardian-b-v"):-len(".tar.gz")]
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"version": ver, "path": p, "bytes": st.st_size,
                    "at": int(st.st_mtime)})
    out.sort(key=lambda x: _ver_tuple(x["version"]), reverse=True)
    return out


def _ver_tuple(s: str) -> tuple:
    try:
        return tuple(int(x) for x in str(s).split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)


def _peer_candidates(cfg: Dict[str, Any]) -> List[tuple]:
    """TS 优先、HTTPS 兜底（与 _send 同一顺序）。"""
    eps = cfg.get("endpoints") or {}
    out = []
    if (eps.get("ts") or "").strip():
        out.append(("ts", eps["ts"].strip(), cfg.get("ts_timeout_s") or 8))
    if (eps.get("https") or "").strip():
        out.append(("https", eps["https"].strip(), cfg.get("https_timeout_s") or 10))
    return out


def _peer_kind_from_status(code: int) -> str:
    if code in (401, 403):
        return "auth"
    if code >= 500:
        return "b_5xx"
    return "http_%s" % code


def peer_call(cfg, method: str, path: str, json_body: Any = None,
              files: Any = None, timeout: int = 20) -> tuple:
    """向 B 发一个请求（TS→HTTPS failover）。

    返回 (ok, payload, err)。err 是**稳定错误码**，前端按码翻译文案——
    「升级失败」这种笼统提示等于没提示，会把排查方向带错。
    """
    candidates = _peer_candidates(cfg)
    if not candidates:
        return False, None, "no_endpoint"
    token = str(cfg.get("token") or "")
    if not token:
        return False, None, "no_token"
    ca = (cfg.get("ca_path") or "").strip()
    errors = []

    for ch, base, tmo in candidates:
        url = base.rstrip("/") + path
        verify: Any = True
        if ch == "https" and not url.startswith("https://"):
            errors.append("https=https_scheme_required")
            continue
        if url.startswith("https://"):
            if not ca or not os.path.isfile(ca):
                errors.append("%s=https_without_ca" % ch)
                continue
            verify = ca
        headers = {"Authorization": "Bearer " + token}
        try:
            import requests
            r = requests.request(method, url, json=json_body, files=files,
                                 headers=headers, timeout=max(timeout, tmo),
                                 verify=verify)
        except Exception as e:
            name = type(e).__name__.lower()
            kind = "timeout" if "timeout" in name else "unreachable"
            errors.append("%s=%s" % (ch, kind))
            continue

        try:
            payload = r.json()
        except ValueError:
            payload = None
        if 200 <= r.status_code < 300:
            return True, payload, ""
        # B 的拒绝理由原样带回（upgrade_disabled / bad_signature / ...），
        # 方便直接显示，不必在 A 端再维护一份错误码表
        code = ""
        if isinstance(payload, dict):
            code = str(payload.get("error") or "")
        errors.append("%s=%s" % (ch, code or _peer_kind_from_status(r.status_code)))
        # 403/409/400 是 B 的明确答复，换通道也是同样结果 → 不再试
        if r.status_code in (400, 403, 409, 413):
            return False, payload, code or _peer_kind_from_status(r.status_code)

    return False, None, "; ".join(errors) or "unreachable"


def get_peer_status(cfg: Optional[Dict[str, Any]] = None) -> tuple:
    cfg = cfg or load_config()
    return peer_call(cfg, "GET", "/guard/status", timeout=8)


def peer_prepare(cfg: Optional[Dict[str, Any]] = None, version: str = "") -> tuple:
    """把本地包上传给 B 预置（不生效）。成功时打开升级窗口，抑制假失联告警。"""
    cfg = cfg or load_config()
    pkgs = list_local_packages()
    if not pkgs:
        return False, None, "no_local_package"
    if version:
        pkgs = [p for p in pkgs if p["version"] == version]
        if not pkgs:
            return False, None, "package_not_found"
    pkg = pkgs[0]
    try:
        with open(pkg["path"], "rb") as f:
            raw = f.read()
    except OSError:
        return False, None, "package_unreadable"

    ok, man, err = verify_package(raw, cfg)
    if not ok:
        return False, None, err

    # 先开窗口再上传：上传过程中 B 会短暂忙，ping 可能超时
    open_upgrade_window(900, note="prepare %s" % pkg["version"])
    try:
        ok2, payload, err2 = peer_call(
            cfg, "POST", "/guard/upgrade/prepare",
            files={"pkg": ("guardian-b-v%s.tar.gz" % pkg["version"],
                           raw, "application/gzip")},
            timeout=90)
    except Exception:
        close_upgrade_window()
        raise
    if not ok2:
        close_upgrade_window()
        return False, payload, err2
    return True, payload, ""


def peer_confirm(cfg: Optional[Dict[str, Any]] = None, job: str = "",
                 code: str = "") -> tuple:
    cfg = cfg or load_config()
    if not job:
        return False, None, "no_job"
    # B 要重启，ping 会失败一段时间：窗口给足（apply + 健康检查最长 10 分钟）
    open_upgrade_window(900, note="confirm %s" % job)
    ok, payload, err = peer_call(cfg, "POST", "/guard/upgrade/confirm",
                                 json_body={"job": job, "code": code}, timeout=30)
    if not ok:
        # 确认失败（码错了 / 过期 / 任务不匹配）→ B **不会**重启，
        # 此时若把窗口留着，等于白白让它 15 分钟不报失联——
        # 而"码打错"恰恰是最常见的情形（审查实证）。
        close_upgrade_window()
    return ok, payload, err


def peer_upgrade_state(cfg: Optional[Dict[str, Any]] = None, job: str = "") -> tuple:
    cfg = cfg or load_config()
    q = ("?job=" + job) if job else ""
    return peer_call(cfg, "GET", "/guard/upgrade/status" + q, timeout=10)
