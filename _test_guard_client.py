#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_test_guard_client.py — guard_client 回归测试（本机可跑，不碰设备）

覆盖：
  A 配置与工具函数（默认关闭、损坏回落、三态归一化、时间解析）
  B 发送链路（零请求判据、Bearer/通道头、失败入队、恢复补报、HTTPS 无 CA 拒绝）
  C 预期离线握手（locked True→False 触发一次）
  D 原子写并发（8 写 × 30 轮，读端零异常）
  E 自检判据（服务停/积压/推送连续失败）

用法：
    python _test_guard_client.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import http.server

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import guard_client as gc  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL  %s %s" % (name, extra))


# ── 假 B 端 ───────────────────────────────────────────────
class Recorder:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.lock = threading.Lock()

    def add(self, path, headers, body):
        with self.lock:
            self.requests.append({"path": path, "headers": dict(headers), "body": body})

    def reset(self):
        with self.lock:
            self.requests = []

    def count(self):
        with self.lock:
            return len(self.requests)


REC = Recorder()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {"_raw": raw[:200].decode("utf-8", "replace")}
        REC.add(self.path, self.headers, body)
        self.send_response(REC.status)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):   # 静音
        pass


def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


# ── 环境隔离 ─────────────────────────────────────────────
TMP = os.path.join(HERE, ".workbuddy", "_tmp", "guardtest-%d" % os.getpid())
os.makedirs(TMP, exist_ok=True)

gc.CONFIG_PATH = os.path.join(TMP, "guard.json")
gc.STATUS_PATH = os.path.join(TMP, "guard_status.json")
gc.QUEUE_PATH = os.path.join(TMP, "guard_queue.json")
gc.USB_GUARD_STATUS = os.path.join(TMP, "usb_guard_status.json")
gc.PREVIEW_QUEUE = os.path.join(TMP, "preview_queue.json")
gc.PREVIEW_BLOCKED = os.path.join(TMP, "preview_blocked.json")
gc.PREVIEW_SCAN = os.path.join(TMP, "preview_scan.json")
gc.SENTRY_CONFIG = os.path.join(TMP, "sentry.json")
# 升级窗口也必须指到临时目录：它会让 `_ping_failed` 抑制失联告警，
# 若读到上一次运行/别的测试残留的窗口文件，本套件会出现"复跑就过"的假失败。
gc.PKG_DIR = os.path.join(TMP, "guard_pkg")
gc.UPGRADE_WINDOW_PATH = os.path.join(TMP, "upgrade_window.json")
# PUSH_HEALTH_OWN 在 import 时就绑定了 app_config.DATA_DIR，必须显式改成临时路径：
# 否则 G 系列每跑一次都会写真实数据目录（真机上等于用合成数据覆盖生产健康记录）。
gc.PUSH_HEALTH_OWN = os.path.join(TMP, "guard_push_health.json")

import config as app_config  # noqa: E402
app_config.PUSH_HEALTH_FILE = os.path.join(TMP, "push_health.json")
app_config.SENTRY_CONFIG_FILE = os.path.join(TMP, "sentry.json")

# 探针打桩：测试不读真机 sysfs / 不发起真实网络探测
gc._wifi_snapshot = lambda: {"wifi_ssid": "C12345", "rssi": -52, "connected": True}
gc.tailscale_state = lambda force=False: {"backend": "Running", "exit_node": None, "ok": True}
gc._internet_ok = lambda: True
gc._teslamate_ok = lambda: True
gc._systemctl_active = lambda unit: True
gc._vehicle_refresh = lambda cfg: None


def fresh_cfg(**over):
    cfg = json.loads(json.dumps(gc.DEFAULTS))
    cfg.update({
        "enabled": True,
        "token": "t" * 40,
        "interval_s": 60,
        "states_interval_s": 99999,   # 测试里不拉 /states
        "probe_every_s": 99999,
    })
    cfg.update(over)
    return cfg


# ═══════════════════════════════════════════════════════════
print("\n[A] 配置与工具函数")

# A1 无配置文件 → 默认关闭
if os.path.exists(gc.CONFIG_PATH):
    os.unlink(gc.CONFIG_PATH)
cfg = gc.load_config()
ok("A1 无配置文件 → enabled=False", cfg["enabled"] is False)

# A2 损坏 JSON → 回落默认，不抛异常
with open(gc.CONFIG_PATH, "w", encoding="utf-8") as f:
    f.write('{"enabled": true, "token": "abc"')   # 故意截断
cfg = gc.load_config()
ok("A2 损坏 JSON → enabled=False 且不抛", cfg["enabled"] is False)

# A3 interval 夹取
gc.atomic_write_json(gc.CONFIG_PATH, {"enabled": True, "token": "x", "interval_s": 1})
cfg = gc.load_config()
ok("A3 interval_s=1 被夹到 15", cfg["interval_s"] == 15, "实际 %s" % cfg["interval_s"])

# A4 三态归一化（M62：nil 会被序列化成字符串）
ok("A4a _tri('nil') → None", gc._tri("nil") is None)
ok("A4b _tri('true') → True", gc._tri("true") is True)
ok("A4c _tri(1) → True", gc._tri(1) is True)
ok("A4d _tri('weird') → None", gc._tri("weird") is None)

# A5 时间解析
ok("A5a 未来时间 → None（时钟回跳不信）", gc._iso_age("2999-01-01T00:00:00") is None)
ok("A5b 非法串 → None", gc._iso_age("not-a-time") is None)
age = gc._iso_age(time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60)))
ok("A5c 60s 前 → 约 60", age is not None and 55 <= age <= 70, "实际 %s" % age)


# ═══════════════════════════════════════════════════════════
print("\n[B] 发送链路")

srv, port = start_server()
base = "http://127.0.0.1:%d" % port

# B1 未启用 → 零请求
REC.reset()
gc.start_background(fresh_cfg(enabled=False, endpoints={"ts": base, "https": ""}))
time.sleep(0.2)
ok("B1 enabled=false → 零请求", REC.count() == 0, "实际 %d" % REC.count())

# B2 启用但无 token → 不启动
ok("B2 无 token → 不启动", gc.start_background(fresh_cfg(token="", endpoints={"ts": base, "https": ""})) is False)

# B3 token 缺失但 endpoints 全空 → 不启动
ok("B3 无通道 → 不启动", gc.start_background(fresh_cfg(endpoints={"ts": "", "https": ""})) is False)

# B4 正常单轮：直接驱动 _tick，避免 20s 首轮延迟
REC.reset()
cfg = fresh_cfg(endpoints={"ts": base, "https": ""})
sender = gc.GuardSender(cfg)
sender._tick()
ok("B4a 收到 1 次心跳", REC.count() == 1, "实际 %d" % REC.count())
req = REC.requests[0] if REC.requests else {"headers": {}, "body": {}, "path": ""}
ok("B4b 路径 /guard/heartbeat", req["path"] == "/guard/heartbeat", req["path"])
ok("B4c Bearer 头正确", req["headers"].get("Authorization") == "Bearer " + cfg["token"])
ok("B4d 通道头 ts", req["headers"].get("X-Guard-Channel") == "ts")
body = req["body"] or {}
ok("B4e body 含 version", body.get("version") == app_config.APP_VERSION, str(body.get("version")))
ok("B4f body 六段齐全",
   all(k in body for k in ("net", "gadget", "thumb", "wechat", "vehicle", "selfcheck")))
ok("B4g seq 递增非零", isinstance(body.get("seq"), int) and body["seq"] > 0)

# B5 状态文件落盘 + 可读
st = gc.get_last_status()
ok("B5a guard_status.json 可读", st.get("available") is True)
ok("B5b 状态文件含 selfcheck", isinstance(st.get("selfcheck"), dict))

# B6 失败入队
REC.status = 500
sender._tick()
q = gc._queue_load()
ok("B6 服务器 500 → 入队 1 条", len(q) == 1, "实际 %d" % len(q))

# B7 恢复后补报（本轮失败 → 下一轮成功才补；这是有意设计，避免通道挂着时白吃超时）
REC.status = 200
REC.reset()
sender._tick()          # 本轮：先发新鲜心跳（补报闸门未开，因为上轮失败）
sender._tick()          # 下一轮：上一轮成功 → 开闸补报
replays = [r for r in REC.requests if (r["body"] or {}).get("replay")]
ok("B7a 补报请求带 replay 标记", len(replays) >= 1, "replays=%d" % len(replays))
ok("B7b 队列已清空", len(gc._queue_load()) == 0, "剩余 %d" % len(gc._queue_load()))

# B8 HTTPS 无 CA → 拒绝（绝不明文信任系统根）
REC.reset()
cfg_https = fresh_cfg(endpoints={"ts": "", "https": "https://127.0.0.1:9"})
s2 = gc.GuardSender(cfg_https)
ok8, err8 = s2._send({"seq": 1})
ok("B8a HTTPS 无 CA 判失败", ok8 is False)
ok("B8b 错误原因 https_without_ca", "https_without_ca" in err8, err8)
ok("B8c 未发出任何请求", REC.count() == 0)

# B9 通道全失败 → 入队
s2._tick()
ok("B9 无可用通道 → 入队", len(gc._queue_load()) >= 1)


# ═══════════════════════════════════════════════════════════
print("\n[C] 预期离线握手")

REC.reset()
gc.atomic_write_json(gc.QUEUE_PATH, [])
gc._VEHICLE_CACHE.update(at=time.monotonic(), data=None, err=None)
cfg3 = fresh_cfg(endpoints={"ts": base, "https": ""})
s3 = gc.GuardSender(cfg3)
s3._prev_vehicle = {"locked": True, "state": "asleep", "user_present": False}
_orig_vehicle = gc._vehicle_snapshot
# 注意：sentry_mode 必须为 False。v0.3.1.82 起「哨兵还开着」时 locked=False
# 不算真解锁（哨兵只在锁车后开启，TeslaMate 的 locked 会漂移），
# 那条 false 会反复给 B 开静默窗口、把真失联告警关掉（M134）。
gc._vehicle_snapshot = lambda cfg=None: {"locked": False, "sentry_mode": False,
                                         "user_present": False, "state": "online",
                                         "park_time": None, "source": "test"}
s3._tick()
evs = [r for r in REC.requests if (r["body"] or {}).get("event") == "going_offline"]
ok("C1 locked True→False 触发握手", len(evs) == 1, "实际 %d" % len(evs))
ok("C1b 原因是 unlocked", evs and evs[0]["body"].get("reason") == "unlocked")

# C1c：哨兵仍开启时，locked 漂移成 False 不能握手（否则反复关掉失联告警）
REC.reset()
s3b = gc.GuardSender(cfg3)
s3b._prev_vehicle = {"locked": True, "state": "asleep", "user_present": False,
                     "sentry_mode": True}
gc._vehicle_snapshot = lambda cfg=None: {"locked": False, "sentry_mode": True,
                                         "user_present": False, "state": "online",
                                         "park_time": None, "source": "test"}
s3b._tick()
evs = [r for r in REC.requests if (r["body"] or {}).get("event") == "going_offline"]
ok("C1c 哨兵开启时不判解锁", len(evs) == 0, "实际 %d" % len(evs))
# 控制组：证明这一拍真的发过请求。否则在缺 requests / 无网环境下
# C1c 会「平凡通过」——它只是没跑起来，而不是闸门生效（审查 P2-6）
ok("C1c-ctl 心跳仍发出", len(REC.requests) >= 1, "实际 %d" % len(REC.requests))
if evs:
    b = evs[0]["body"]
    ok("C2 握手带相对量 offline_s（跨机安全）", isinstance(b.get("offline_s"), int), str(b.get("offline_s")))
    ok("C3 reason=unlocked", b.get("reason") == "unlocked", str(b.get("reason")))
    ok("C3b unlocked 用短窗口 900s（不是 1h）", b.get("offline_s") == 900, str(b.get("offline_s")))

REC.reset()
s3._tick()      # 状态未再变化 → 不得重复握手
evs2 = [r for r in REC.requests if (r["body"] or {}).get("event") == "going_offline"]
ok("C4 状态未变不重复握手", len(evs2) == 0, "实际 %d" % len(evs2))

# C5 握手失败要重试（fire-and-forget 会丢掉这趟出车的防误报闸门）
gc._vehicle_snapshot = _orig_vehicle
REC.status = 500
REC.reset()
s4 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}))
s4._maybe_going_offline({"vehicle": {"locked": False, "state": "online", "user_present": False}})
ok("C5 无前态时不误触发", s4._pending_offline is None)
s4._prev_vehicle = {"locked": True, "state": "asleep", "user_present": False}
s4._maybe_going_offline({"vehicle": {"locked": False, "state": "online", "user_present": False}})
ok("C6 失败后保留 pending", s4._pending_offline is not None)
ok("C7 已重试 1 次", s4._pending_tries == 1, str(s4._pending_tries))
REC.status = 200
s4._pending_last = 0.0                      # 绕过 30s 间隔，模拟下轮
s4._retry_pending_offline()
ok("C8 恢复后重试成功并清空 pending", s4._pending_offline is None)
REC.status = 200


# ═══════════════════════════════════════════════════════════
print("\n[D] 原子写并发")

target = os.path.join(TMP, "concurrent.json")
errors = []
bad_writes = []
stop = threading.Event()


def writer(i):
    for k in range(30):
        try:
            if not gc.atomic_write_json(target, {"w": i, "k": k, "pad": "x" * 400}):
                bad_writes.append((i, k))
        except Exception as e:            # 写端也不许抛
            errors.append("w:%s" % e)


def reader():
    while not stop.is_set():
        try:
            with open(target, "r", encoding="utf-8") as f:
                json.load(f)              # 读到半个文件就会 ValueError
        except ValueError:
            errors.append("r: JSONDecodeError")
        except OSError:
            pass
        time.sleep(0.002)


threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
rt = threading.Thread(target=reader, daemon=True)
rt.start()
for t in threads:
    t.start()
for t in threads:
    t.join()
stop.set()
time.sleep(0.05)
ok("D1 8 写线程零异常（失败必须用返回值上报，不得抛）", errors == [], str(errors[:3]))
if os.name == "posix":
    ok("D1b 240 次写全部成功（Linux rename 原子）", bad_writes == [],
       "失败 %d 次" % len(bad_writes))
else:
    # Windows 下「8 线程同时 replace 同一文件 + 读线程每 2ms 打开」是病态场景，
    # 会周期性撞 WinError 5（文件被占用）。这不是设计缺陷（生产只有心跳线程一个写者），
    # 也不该用一个会抖动的阈值来假装全绿——这里只做记录，判据交给 D4。
    print("  INFO  Windows 病态并发下失败 %d/240 次（仅记录，不作判据）" % len(bad_writes))

# D4 单写者（= 生产真实形态）：无论哪个平台都必须 100% 成功
single_bad = 0
for k in range(60):
    if not gc.atomic_write_json(target, {"k": k, "pad": "y" * 300}):
        single_bad += 1
ok("D4 单写者 60 次全部成功（跨平台确定性判据）", single_bad == 0, "失败 %d 次" % single_bad)
try:
    with open(target, "r", encoding="utf-8") as f:
        final = json.load(f)
    ok("D2 最终文件合法", isinstance(final, dict) and ("w" in final or "k" in final))
except Exception as e:
    ok("D2 最终文件合法", False, str(e))

leftover = [n for n in os.listdir(TMP) if n.startswith(".atomic-")]
ok("D3 无残留 .atomic- 临时文件", leftover == [], str(leftover))


# ═══════════════════════════════════════════════════════════
print("\n[E] 自检判据")

base_net = {"connected": True, "internet_ok": True}
sc = gc._selfcheck(base_net, {"mode": "present", "udc_connected": True, "cable": True,
                              "age_s": 10},
                   {"service": True, "queue": 0, "last_scan_age_s": 10},
                   {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("E1 全正常 → pass", sc["result"] == "pass", sc["result"])

sc = gc._selfcheck(base_net, {"mode": "edit", "udc_connected": False, "cable": True},
                   {"service": True, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("E2 Edit 模式解绑不算故障（M103）", sc["items"]["gadget"] == "pass", sc["items"]["gadget"])

sc = gc._selfcheck(base_net, {"mode": "present", "udc_connected": False, "cable": False},
                   {"service": True, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("E3 线离位只 warn 不 fail", sc["items"]["gadget"] == "warn", sc["items"]["gadget"])

sc = gc._selfcheck(base_net, {"mode": "present", "udc_connected": True, "cable": True},
                   {"service": False, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("E4 缩略图服务停 → fail", sc["result"] == "fail")

sc = gc._selfcheck(base_net, {"mode": "present", "udc_connected": True, "cable": True},
                   {"service": True, "queue": gc.THUMB_PENDING_WARN + 5},
                   {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("E5 队列积压 → warn", sc["items"]["thumb"] == "warn", sc["items"]["thumb"])

sc = gc._selfcheck(base_net, {"mode": "present", "udc_connected": True, "cable": True},
                   {"service": True, "queue": 0},
                   {"last_ok_age_s": 60, "consecutive_fail": gc.WECHAT_FAIL_STREAK})
ok("E6 推送连续失败 → fail", sc["items"]["wechat"] == "fail", sc["items"]["wechat"])

sc = gc._selfcheck({"connected": False, "internet_ok": False},
                   {"mode": "present", "udc_connected": True, "cable": True},
                   {"service": True, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("E7 WiFi 未连接 → net fail", sc["items"]["net"] == "fail", sc["items"]["net"])


# ═══════════════════════════════════════════════════════════
print("\n[F] 交叉复核发现项（P0/P1）")

# F1 enabled 写字符串 "false" 不能被当成开启（P0-1）
gc.atomic_write_json(gc.CONFIG_PATH, {"enabled": "false", "token": "x",
                                      "endpoints": {"ts": base, "https": ""}})
ok("F1 enabled='false' → 关闭", gc.load_config()["enabled"] is False)
gc.atomic_write_json(gc.CONFIG_PATH, {"enabled": "true", "token": "x",
                                      "endpoints": {"ts": base, "https": ""}})
ok("F2 enabled='true' → 开启", gc.load_config()["enabled"] is True)
gc.atomic_write_json(gc.CONFIG_PATH, {"enabled": "0"})
ok("F2b enabled='0' → 关闭", gc.load_config()["enabled"] is False)

# F3 数值参数夹取（P1-1 / P1-2 / P0-4）
gc.atomic_write_json(gc.CONFIG_PATH, {"probe_every_s": 600, "queue_max": 0,
                                      "interval_s": 300, "ts_timeout_s": 0,
                                      "states_interval_s": 0})
c = gc.load_config()
ok("F3a probe 600 → 150（必须 < B 的 ALARM 300s）", c["probe_every_s"] == 150, str(c["probe_every_s"]))
ok("F3b queue_max 0 → 1", c["queue_max"] == 1, str(c["queue_max"]))
ok("F3c interval 300 → 60", c["interval_s"] == 60, str(c["interval_s"]))
ok("F3d timeout 0 → 2", c["ts_timeout_s"] == 2, str(c["ts_timeout_s"]))
ok("F3e states 0 → 120", c["states_interval_s"] == 120, str(c["states_interval_s"]))
ok("F3f queue_max<=0 时不写入（不无限增长）",
   gc._queue_push({"x": 1}, 0) == -1 and len(gc._queue_load()) == 0)

# F4 HTTPS 通道配成 http:// → 拒绝发送（P0-2，token 不得明文过公网）
REC.reset()
cfg_plain = fresh_cfg(endpoints={"ts": "", "https": "http://127.0.0.1:%d" % port})
s5 = gc.GuardSender(cfg_plain)
ok5, err5 = s5._send({"seq": 1})
ok("F4a http 冒充 https → 失败", ok5 is False)
ok("F4b 原因 https_scheme_required", "https_scheme_required" in err5, err5)
ok("F4c 未发出任何请求", REC.count() == 0)

# F5 探针未知（None）不能判 pass（P1-4）
sc = gc._selfcheck(base_net, {"mode": "present", "udc_connected": True, "cable": True, "age_s": 10},
                   {"service": None, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("F5a systemctl 未知 → warn", sc["items"]["thumb"] == "warn", sc["items"]["thumb"])
sc = gc._selfcheck({"connected": None, "internet_ok": None},
                   {"mode": "present", "udc_connected": True, "cable": True, "age_s": 10},
                   {"service": True, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("F5b net 未知 → warn 不当 pass", sc["items"]["net"] == "warn", sc["items"]["net"])

# F6 usb_guard 状态陈旧（守护停摆）→ warn（P1-5）
sc = gc._selfcheck(base_net,
                   {"mode": "present", "udc_connected": True, "cable": True,
                    "age_s": gc.GADGET_STALE_S + 60},
                   {"service": True, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("F6 状态文件陈旧 → gadget warn", sc["items"]["gadget"] == "warn", sc["items"]["gadget"])

# F7 状态接口必须区分「活着」与「停摆」（P1-6）
gc.atomic_write_json(gc.CONFIG_PATH, {"enabled": True, "token": "t" * 40,
                                      "endpoints": {"ts": base, "https": ""}})
gc.atomic_write_json(gc.STATUS_PATH, {"ts": int(time.time()) - 3600, "selfcheck": {}})
st = gc.get_last_status()
ok("F7a 一小时前的快照 → available=false", st.get("available") is False)
ok("F7b 标记 stale", st.get("stale") is True)
ok("F7c 带 age_s", isinstance(st.get("age_s"), int) and st["age_s"] >= 3500, str(st.get("age_s")))
gc.atomic_write_json(gc.STATUS_PATH, {"ts": int(time.time()), "selfcheck": {}})
ok("F7d 新鲜快照 → available=true", gc.get_last_status().get("available") is True)

# F8 vehicle 缓存过期必须降级为 unknown（P1-10）
gc._VEHICLE_CACHE.update(at=time.monotonic() - 4000,
                        data={"locked": True, "sentry_mode": True, "is_user_present": False},
                        err=None)
# 关掉兜底再断言：新设计允许 stale 之后由 L3（行车记录仪文件）给出 locked，
# 那样 F8b 就失去意义。这条只验证「不采信过期的 L1」。
v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
ok("F8a 过期缓存 → source=stale", v.get("source") == "stale", str(v.get("source")))
ok("F8b 过期缓存 → 三态置 None", v.get("locked") is None and v.get("sentry_mode") is None)
gc._VEHICLE_CACHE.update(at=time.monotonic(),
                        data={"locked": True, "sentry_mode": True, "is_user_present": False},
                        err=None)
v = gc._vehicle_snapshot()
ok("F8c 新鲜缓存 → 正常解析", v.get("locked") is True and v.get("source") == "teslamate_msg")

# F9 补报必须在新鲜心跳之前（否则 B 的 last_body 停在最旧快照）（P2-1）
REC.reset()
gc.atomic_write_json(gc.QUEUE_PATH, [{"seq": 1, "ts": 1, "old": True},
                                     {"seq": 2, "ts": 2, "old": True}])
s6 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}))
s6._tx_ok = 1
s6._fail_streak = 0
s6._tick()
first = REC.requests[0]["body"] if REC.requests else {}
ok("F9a 首个请求是补报（旧快照先补）", first.get("replay") is True, str(first.get("replay")))
ok("F9b 之后才是新鲜心跳", (REC.requests[-1]["body"] or {}).get("replay") is not True)
ok("F9c 队列已清空", len(gc._queue_load()) == 0, "剩余 %d" % len(gc._queue_load()))

# F10 失败计数与「最后成功时刻」分离（P1-7）
REC.status = 500
s6._tick()
bad_ok_at = s6._last_ok_at
s6._tick()
ok("F10a 失败不刷新 last_ok_at", s6._last_ok_at == bad_ok_at)
ok("F10b fail_streak 累加", s6._fail_streak >= 2, str(s6._fail_streak))
ok("F10c 有 last_attempt_at", s6._last_attempt_at > 0)
REC.status = 200
s6._tick()
ok("F10d 成功后 fail_streak 归零", s6._fail_streak == 0)


# F11 反向 ping 也必须守 https 语义（不能成为 P0-2 的绕过路径）
REC.reset()
cfg_ping = fresh_cfg(endpoints={"ts": "", "https": "http://127.0.0.1:%d" % port})
s7 = gc.GuardSender(cfg_ping)
s7._ping()
ok("F11a https 端点写成 http → ping 不发请求", REC.count() == 0, "实际 %d" % REC.count())
ok("F11b ping_fail 计数 +1", s7._ping_fail == 1, str(s7._ping_fail))

# F12 车态全 unknown 时不得覆盖 prev（否则真实跃迁会被数据空窗吃掉）
s8 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}))
s8._prev_vehicle = {"locked": True, "state": "asleep", "user_present": False}
s8._maybe_going_offline({"vehicle": {"locked": None, "state": None, "user_present": None}})
ok("F12a unknown 不覆盖 prev", s8._prev_vehicle.get("locked") is True, str(s8._prev_vehicle))
ok("F12b unknown 不触发握手", s8._pending_offline is None)
REC.reset()
s8._maybe_going_offline({"vehicle": {"locked": False, "state": "online", "user_present": False}})
ev12 = [r for r in REC.requests if (r["body"] or {}).get("event") == "going_offline"]
ok("F12c 数据恢复后仍能识别真实跃迁", len(ev12) == 1, "实际 %d" % len(ev12))

# F12d 部分字段缺失（locked=None 但这拍有 state）不得抹掉已知道的 locked
REC.reset()
s9 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}))
s9._prev_vehicle = {"locked": True, "state": "asleep", "user_present": False}
s9._maybe_going_offline({"vehicle": {"locked": None, "state": "online", "user_present": None}})
ok("F12d 部分缺失不抹 locked", s9._prev_vehicle.get("locked") is True, str(s9._prev_vehicle))
s9._maybe_going_offline({"vehicle": {"locked": False, "state": None, "user_present": None}})
ev12d = [r for r in REC.requests if (r["body"] or {}).get("event") == "going_offline"]
ok("F12e 下一拍拿到 locked=False 仍能触发解锁跃迁", len(ev12d) == 1, "实际 %d" % len(ev12d))

# F13 vehicle stale 阈值必须跟随配置（不是写死 900s）
cfg_long = fresh_cfg(endpoints={"ts": base, "https": ""}, states_interval_s=1800)
gc._VEHICLE_CACHE.update(at=time.monotonic() - 2000,
                        data={"locked": True, "sentry_mode": True, "is_user_present": False},
                        err=None)
v = gc._vehicle_snapshot(cfg_long)
ok("F13a states_interval=1800 时 2000s 不算 stale", v.get("source") == "teslamate_msg",
   str(v.get("source")))
v = gc._vehicle_snapshot(fresh_cfg(endpoints={"ts": base, "https": ""}, states_interval_s=300))
ok("F13b 300s 配置下 2000s 判 stale", v.get("source") == "stale", str(v.get("source")))


# ═══════════════════════════════════════════════════════════
print("\n[G] 配置归一化 / 自检报告 / 僚机失联（v0.3.1.77）")
# ═══════════════════════════════════════════════════════════

# ── G1~G3 新参数 ──
c = gc.normalize_config({"report_cooldown_s": 10, "ping_fail_threshold": 99})
ok("G1a report_cooldown_s 夹到下限 300", c["report_cooldown_s"] == 300, c["report_cooldown_s"])
ok("G1b ping_fail_threshold 夹到上限 6", c["ping_fail_threshold"] == 6, c["ping_fail_threshold"])
c = gc.normalize_config({"report_transition": "false"})
ok("G2 字符串 'false' 判成关闭", c["report_transition"] is False, c["report_transition"])
ok("G3a '8:5' → '08:05'", gc._norm_hhmm("8:5") == "08:05", gc._norm_hhmm("8:5"))
ok("G3b 空串 = 关闭日报", gc._norm_hhmm("") == "")
ok("G3c 越界时刻回落默认", gc._norm_hhmm("25:99") == gc.DEFAULTS["report_daily_at"])
ok("G3d 非法格式回落默认", gc._norm_hhmm("abc") == gc.DEFAULTS["report_daily_at"])

# ── G4 配置视图不含明文 token ──
pub = gc.public_config(fresh_cfg(token="SECRET-TOKEN-1234567890"))
ok("G4a 视图不含明文 token", "SECRET-TOKEN" not in json.dumps(pub))
ok("G4b 有 token_set + 末 4 位", pub.get("token_set") is True and pub.get("token_tail") == "…7890",
   str(pub.get("token_tail")))

# ── G5~G6 自检报告：跃迁 / 恢复 / 去重 ──
PUSHED = []
gc._guard_bot_key = lambda: "fake-key-for-test"
gc._push_wecom = lambda text: (PUSHED.append(text), (True, ""))[1]

snap_pass = {"selfcheck": {"result": "pass", "items": {"net": "pass", "gadget": "pass",
                                                       "thumb": "pass", "wechat": "pass"}},
             "net": {"connected": True, "internet_ok": True, "wifi_ssid": "C12345"},
             "gadget": {"mode": "present", "udc_connected": True, "age_s": 3},
             "thumb": {"service": True, "queue": 0, "last_scan_age_s": 12.0},
             "wechat": {"last_ok_age_s": 60, "consecutive_fail": 0}}
snap_fail = {"selfcheck": {"result": "fail", "items": {"net": "fail", "gadget": "pass",
                                                       "thumb": "pass", "wechat": "pass"}},
             "net": {"connected": False, "internet_ok": False},
             "gadget": {"mode": "present", "udc_connected": True, "age_s": 3},
             "thumb": {"service": True, "queue": 0, "last_scan_age_s": 12.0},
             "wechat": {"last_ok_age_s": 60, "consecutive_fail": 0}}

s = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""},
                             report_cooldown_s=300, report_daily_at=""))
s._warmup_until = 0          # 关掉预热闸门，否则前 5 分钟一律不推
PUSHED.clear()
s._maybe_report(snap_pass)
ok("G5a 首轮（无历史）不推跃迁", len(PUSHED) == 0, str(len(PUSHED)))
s._maybe_report(snap_fail)
ok("G5b pass→fail 推一条", len(PUSHED) == 1, "实际 %d" % len(PUSHED))
ok("G5c 文案含「异常」", bool(PUSHED) and "异常" in PUSHED[0], PUSHED[0][:24] if PUSHED else "")
s._maybe_report(snap_pass)
ok("G6a fail→pass 推恢复", len(PUSHED) == 2 and "恢复" in PUSHED[1],
   str(len(PUSHED)) + " " + (PUSHED[1][:20] if len(PUSHED) > 1 else ""))
s._maybe_report(snap_fail)
ok("G6b 冷却窗口内不重复（同档 300s）", len(PUSHED) == 2, "实际 %d" % len(PUSHED))

# G6c~G6e 预热期（升级/重启后 300s）只跟踪不推送
s_w = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}, report_daily_at=""))
PUSHED.clear()
s_w._maybe_report(snap_pass)
s_w._maybe_report(snap_fail)
ok("G6c 预热期内不推跃迁", len(PUSHED) == 0, "实际 %d" % len(PUSHED))
ok("G6d 预热期仍跟踪状态", s_w._last_result == "fail", s_w._last_result)
s_w._warmup_until = 0
s_w._maybe_report(snap_pass)
ok("G6e 预热结束后正常推送", len(PUSHED) == 1 and "恢复" in PUSHED[0], str(len(PUSHED)))

# ── G7 日报（用假时钟把「现在」拨到 20:03）──
class _FakeTime:
    def localtime(self, *a):
        return time.struct_time((2026, 11, 8, 20, 3, 0, 6, 312, -1))

    def __getattr__(self, name):
        return getattr(time, name)


s2 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}, report_daily_at="20:00"))
s2._warmup_until = 0
PUSHED.clear()
_real_time = gc.time
gc.time = _FakeTime()
try:
    s2._maybe_report(snap_pass)
    ok("G7a 20:03 触发日报", len(PUSHED) == 1 and "日报" in PUSHED[0],
       str(len(PUSHED)) + " " + (PUSHED[0][:18] if PUSHED else ""))
    s2._maybe_report(snap_pass)
    ok("G7b 同一天不重复日报", len(PUSHED) == 1, "实际 %d" % len(PUSHED))
finally:
    gc.time = _real_time

# ── G8 僚机失联：阈值 / 去重 / 恢复 ──
PUSHED.clear()
s3 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}, ping_fail_threshold=3))
s3._ping_failed("timeout")
s3._ping_failed("timeout")
ok("G8a 未达阈值不推", len(PUSHED) == 0, str(len(PUSHED)))
s3._ping_failed("timeout")
ok("G8b 达阈值推一条", len(PUSHED) == 1, str(len(PUSHED)))
ok("G8c 文案是「僚机失联」", bool(PUSHED) and "僚机" in PUSHED[0] and "失联" in PUSHED[0],
   PUSHED[0][:26] if PUSHED else "")
s3._ping_failed("timeout")
s3._ping_failed("timeout")
ok("G8d 持续失联不重复推", len(PUSHED) == 1, "实际 %d" % len(PUSHED))
s3._ping_ok()
ok("G8e 恢复推一条", len(PUSHED) == 2 and "恢复" in PUSHED[1], "实际 %d" % len(PUSHED))
ok("G8f 计数归零 + 解除标记", s3._ping_fail == 0 and s3._b_down_notified is False,
   "%d %s" % (s3._ping_fail, s3._b_down_notified))

# ── G9 401 不能报成「失联」 ──
PUSHED.clear()
s4 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}, ping_fail_threshold=2))
s4._ping_failed("auth")
s4._ping_failed("auth")
ok("G9 令牌错 → 文案是「令牌不匹配」且不说失联",
   bool(PUSHED) and "令牌" in PUSHED[0] and "失联" not in PUSHED[0],
   PUSHED[0][:30] if PUSHED else "")
st = s4.stats()
ok("G10 stats 含 b_down/ping_kind/report_ok",
   all(k in st for k in ("b_down", "ping_kind", "last_result", "report_ok", "report_pending")))

# ── G11 缩略图扫描年龄落盘读取 ──
gc.atomic_write_json(gc.PREVIEW_SCAN, {"ts": time.time() - 42, "total": 5, "pending": 2},
                     mode=0o644)
th = gc._thumb_snapshot()
ok("G11a last_scan_age_s 有值", isinstance(th.get("last_scan_age_s"), float),
   str(th.get("last_scan_age_s")))
ok("G11b 年龄≈42s", abs(float(th.get("last_scan_age_s") or 0) - 42) < 3,
   str(th.get("last_scan_age_s")))
ok("G11c 队列积压值来自队列文件", th.get("queue") == 0, str(th.get("queue")))

sc = gc._selfcheck({"connected": True, "internet_ok": True},
                   {"mode": "present", "udc_connected": True, "cable": True, "age_s": 5},
                   {"service": True, "queue": 0, "last_scan_age_s": 999},
                   {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("G11d 扫描陈旧(999s) → thumb warn", sc["items"]["thumb"] == "warn", sc["items"]["thumb"])
sc = gc._selfcheck({"connected": True, "internet_ok": True},
                   {"mode": "present", "udc_connected": True, "cable": True, "age_s": 5},
                   {"service": True, "queue": 0, "last_scan_age_s": None},
                   {"last_ok_age_s": 60, "consecutive_fail": 0})
ok("G11e 无扫描记录 → thumb warn（不假绿）", sc["items"]["thumb"] == "warn", sc["items"]["thumb"])

# ── G13 待发报告：安全项不得被覆盖、不得被丢弃 ──
gc._push_wecom = lambda text: (PUSHED.append(text), (False, "http_500"))[1]
s5 = gc.GuardSender(fresh_cfg(endpoints={"ts": base, "https": ""}, ping_fail_threshold=2))
s5._warmup_until = 0
s5._last_result = "pass"
s5._ping_failed("timeout")
s5._ping_failed("timeout")          # → B 失联入队
s5._maybe_report(snap_fail)         # → 自检异常再入一条
ok("G13a 两条都留在待发队列", len(s5._pending_reports) == 2, str(len(s5._pending_reports)))
ok("G13b 队头是 B 失联（未被覆盖）",
   bool(s5._pending_reports) and "失联" in s5._pending_reports[0]["text"],
   str(s5._pending_reports[:1]))
ok("G13c B 失联被标为安全项", bool(s5._pending_reports)
   and s5._pending_reports[0]["critical"] is True)

for _ in range(8):                              # 安全项重试很多次也不丢
    if s5._pending_reports:
        s5._pending_reports[0]["at"] = 0
        s5._retry_pending_report()
ok("G13d 安全项重试 8 次后仍在", len(s5._pending_reports) == 2, str(len(s5._pending_reports)))

s5._pending_reports = [i for i in s5._pending_reports if not i.get("critical")]
for _ in range(4):                              # 普通项 3 档后丢弃
    if s5._pending_reports:
        s5._pending_reports[0]["at"] = 0
        s5._retry_pending_report()
ok("G13e 普通项重试耗尽后丢弃", len(s5._pending_reports) == 0, str(len(s5._pending_reports)))

gc._push_wecom = lambda text: (PUSHED.append(text), (True, ""))[1]
s5._pending_reports = [
    {"text": "critical-1", "critical": True, "tries": 0, "at": 0},
    {"text": "normal-1", "critical": False, "tries": 0, "at": 0},
]
s5._retry_pending_report()
ok("G13f 补发优先安全项", len(s5._pending_reports) == 1
   and s5._pending_reports[0]["text"] == "normal-1", str(s5._pending_reports))
s5._pending_reports[0]["at"] = 0
s5._retry_pending_report()
ok("G13g 全部补发完", len(s5._pending_reports) == 0, str(len(s5._pending_reports)))




# ── G14 推送健康：自检必须看**自己的**发送结果（M127）──────
import json as _json
import threading as _th

PH = gc.PUSH_HEALTH_OWN


def _ph_read():
    try:
        return _json.load(open(PH, encoding="utf-8"))
    except Exception:
        return {}


def _ph_reset():
    try:
        os.remove(PH)
    except OSError:
        pass


def _sc(wechat):
    return gc._selfcheck({"connected": True, "internet_ok": True},
                         {"mode": "present", "udc_connected": True, "age_s": 1},
                         {"service": True, "queue": 0, "last_scan_age_s": 1},
                         wechat)


_ph_reset()
gc._note_push(True)
d = _ph_read()
ok("G14a 成功写入 last_attempt_ok",
   d.get("last_attempt_ok") is True and int(d.get("consecutive_fail") or 0) == 0, str(d))

_ph_reset()
gc._note_push(False)
gc._note_push(False)
d = _ph_read()
ok("G14b 连续失败累加到 2", int(d.get("consecutive_fail") or 0) == 2, str(d))

gc._note_push(True)
d = _ph_read()
ok("G14c 成功清零 streak 且保留 last_fail",
   int(d.get("consecutive_fail") or 0) == 0 and float(d.get("last_fail") or 0) > 0, str(d))

_ph_reset()
gc._note_push(False)
gc._note_push(False, count=False)
gc._note_push(False, count=False)
d = _ph_read()
ok("G14d 重试不重复累加 streak", int(d.get("consecutive_fail") or 0) == 1, str(d))

_ph_reset()
open(PH, "w", encoding="utf-8").write('{"last_ok":"abc","consecutive_fail":"x"}')
try:
    gc._note_push(True)
    ok("G14e 脏数据不抛异常（会污染 B 失联状态机）", True)
except Exception as exc:                        # noqa: BLE001
    ok("G14e 脏数据不抛异常（会污染 B 失联状态机）", False, repr(exc))

_ph_reset()
gc._note_push(True)
w = gc._wechat_snapshot()
ok("G14f snapshot 优先走 self 分支", w.get("source") == "self", str(w))
ok("G14g snapshot 带 bot_key_set", "bot_key_set" in w, str(w))

ok("G14h 单次成功即 pass（不再卡 24h 窗口）",
   _sc({"source": "self", "bot_key_set": True, "last_attempt_ok": True,
        "last_ok_age_s": 99 * 86400, "last_fail_age_s": None,
        "consecutive_fail": 0})["items"]["wechat"] == "pass")

ok("G14i 近期单次失败 → warn",
   _sc({"source": "self", "bot_key_set": True, "last_attempt_ok": False,
        "last_ok_age_s": 100, "last_fail_age_s": 300,
        "consecutive_fail": 1})["items"]["wechat"] == "warn")

ok("G14j 陈旧失败不粘住（30 分钟外判 pass）",
   _sc({"source": "self", "bot_key_set": True, "last_attempt_ok": False,
        "last_ok_age_s": None, "last_fail_age_s": 4000,
        "consecutive_fail": 1})["items"]["wechat"] == "pass")

ok("G14k 连续 3 次失败 → fail",
   _sc({"source": "self", "bot_key_set": True, "last_attempt_ok": False,
        "last_ok_age_s": None, "last_fail_age_s": 10,
        "consecutive_fail": 3})["items"]["wechat"] == "fail")

ok("G14l 未配机器人 → warn（配置问题，不误判通道故障）",
   _sc({"source": "self", "bot_key_set": False, "last_attempt_ok": True,
        "last_ok_age_s": 10, "last_fail_age_s": None,
        "consecutive_fail": 0})["items"]["wechat"] == "warn")

_ph_reset()
_ts = [_th.Thread(target=gc._note_push, args=(False,)) for _ in range(16)]
for _t in _ts:
    _t.start()
for _t in _ts:
    _t.join()
d = _ph_read()
ok("G14m 并发 16 次写入无丢更新", int(d.get("consecutive_fail") or 0) == 16, str(d))

# ═══════════════════════════════════════════════════════════
srv.shutdown()
print("\n" + "=" * 46)
print("结果: %d PASS / %d FAIL" % (PASS, FAIL))
if FAILURES:
    print("失败项:", ", ".join(FAILURES))
print("临时目录（默认保留，便于排查）:", TMP)
sys.exit(0 if FAIL == 0 else 1)
