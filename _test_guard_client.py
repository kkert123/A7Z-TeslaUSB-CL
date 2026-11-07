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

import config as app_config  # noqa: E402
app_config.PUSH_HEALTH_FILE = os.path.join(TMP, "push_health.json")
app_config.SENTRY_CONFIG_FILE = os.path.join(TMP, "sentry.json")

# 探针打桩：测试不读真机 sysfs / 不发起真实网络探测
gc._wifi_snapshot = lambda: {"wifi_ssid": "C12345", "rssi": -52, "connected": True}
gc.tailscale_state = lambda force=False: {"backend": "Running", "exit_node": None, "ok": True}
gc._internet_ok = lambda: True
gc._teslamate_ok = lambda: True
gc._systemctl_active = lambda unit: True
gc._states_refresh = lambda cfg: None


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
gc._STATES_CACHE.update(at=time.monotonic(), data=None, err=None)
cfg3 = fresh_cfg(endpoints={"ts": base, "https": ""})
s3 = gc.GuardSender(cfg3)
s3._prev_vehicle = {"locked": True, "state": "asleep", "user_present": False}
_orig_vehicle = gc._vehicle_snapshot
gc._vehicle_snapshot = lambda cfg=None: {"locked": False, "sentry_mode": True,
                                         "user_present": False, "state": "online",
                                         "park_time": None, "source": "test"}
s3._tick()
evs = [r for r in REC.requests if (r["body"] or {}).get("event") == "going_offline"]
ok("C1 locked True→False 触发握手", len(evs) == 1, "实际 %d" % len(evs))
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
                   {"service": True, "queue": 0}, {"last_ok_age_s": 60, "consecutive_fail": 0})
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
   (gc._queue_push({"x": 1}, 0) or True) and len(gc._queue_load()) == 0)

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
gc._STATES_CACHE.update(at=time.monotonic() - 4000,
                        data={"locked": True, "sentry_mode": True, "is_user_present": False},
                        err=None)
v = gc._vehicle_snapshot()
ok("F8a 过期缓存 → source=stale", v.get("source") == "stale", str(v.get("source")))
ok("F8b 过期缓存 → 三态置 None", v.get("locked") is None and v.get("sentry_mode") is None)
gc._STATES_CACHE.update(at=time.monotonic(),
                        data={"locked": True, "sentry_mode": True, "is_user_present": False},
                        err=None)
v = gc._vehicle_snapshot()
ok("F8c 新鲜缓存 → 正常解析", v.get("locked") is True and v.get("source") == "teslamate_states")

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
gc._STATES_CACHE.update(at=time.monotonic() - 2000,
                        data={"locked": True, "sentry_mode": True, "is_user_present": False},
                        err=None)
v = gc._vehicle_snapshot(cfg_long)
ok("F13a states_interval=1800 时 2000s 不算 stale", v.get("source") == "teslamate_states",
   str(v.get("source")))
v = gc._vehicle_snapshot(fresh_cfg(endpoints={"ts": base, "https": ""}, states_interval_s=300))
ok("F13b 300s 配置下 2000s 判 stale", v.get("source") == "stale", str(v.get("source")))


# ═══════════════════════════════════════════════════════════
srv.shutdown()
print("\n" + "=" * 46)
print("结果: %d PASS / %d FAIL" % (PASS, FAIL))
if FAILURES:
    print("失败项:", ", ".join(FAILURES))
print("临时目录（默认保留，便于排查）:", TMP)
sys.exit(0 if FAIL == 0 else 1)
