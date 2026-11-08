#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_test_guard_api.py — 守护配置接口回归（走真实 Flask 路由，不碰设备）

覆盖：
  A 读取：默认值、token 掩码、不含明文
  B 保存校验：缺 token / HTTPS 非 https:// / 配了 HTTPS 缺 ca_path / 不可编辑字段
  C 保存生效：越界夹取、归一化、热启动与停止、落盘一致
  D 状态接口：顶层 sender / stale / channels 形状

用法：python _test_guard_api.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import guard_client as gc  # noqa: E402

TMP = os.path.join(HERE, ".workbuddy", "_tmp", "guardapi-%d" % os.getpid())
os.makedirs(TMP, exist_ok=True)
gc.CONFIG_PATH = os.path.join(TMP, "guard.json")
gc.STATUS_PATH = os.path.join(TMP, "guard_status.json")
if os.path.exists(gc.CONFIG_PATH):
    os.remove(gc.CONFIG_PATH)

import app as webapp  # noqa: E402

PASS = FAIL = 0
FAILS = []


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append("%s %s" % (name, extra))
        print("  FAIL  %s  %s" % (name, extra))


C = webapp.app.test_client()
TOKEN = "b" * 64
BASE = "http://127.0.0.1:1"

print("=" * 60)
print("守护配置接口测试")
print("=" * 60)

# ── A 读取 ──
print("\n[A] GET /api/guard/config")
r = C.get("/api/guard/config")
d = r.get_json() or {}
ok("A1 200", r.status_code == 200, r.status_code)
ok("A2 默认未启用", d["config"]["enabled"] is False)
ok("A3 默认日报 20:00", d["config"]["report_daily_at"] == "20:00")
ok("A4 有 limits 表", isinstance(d.get("limits"), dict) and "probe_every_s" in d["limits"])
ok("A5 视图无 token 明文字段", "token" not in d["config"]
   and "token_set" in d["config"], str(list(d["config"].keys())))

# ── B 保存校验 ──
print("\n[B] 保存校验")
ok("B1 缺 token → 400", C.post("/api/guard/config", json={"enabled": True}).status_code == 400)
r = C.post("/api/guard/config", json={"enabled": True, "token": TOKEN,
                                      "endpoints": {"ts": "", "https": "http://1.2.3.4:8899"}})
ok("B2 https 写成 http → 400", r.status_code == 400, r.status_code)
r = C.post("/api/guard/config", json={"enabled": True, "token": TOKEN,
                                      "endpoints": {"ts": "", "https": "https://1.2.3.4:8899"}})
ok("B3 配了 HTTPS 但无 ca_path → 400", r.status_code == 400, r.status_code)
ok("B4 400 时未落盘", not os.path.exists(gc.CONFIG_PATH))
r = C.post("/api/guard/config", json={"totally_unknown": 1})
ok("B5 无已知字段 → 400", r.status_code == 400, r.status_code)

# ── C 保存生效 ──
print("\n[C] 保存与热生效")
r = C.post("/api/guard/config", json={
    "enabled": True, "token": TOKEN,
    "endpoints": {"ts": BASE, "https": ""},
    "probe_every_s": 600,
    "ping_fail_threshold": 1,
    "report_daily_at": "8:5",
    "interval_s": 60,
})
d = r.get_json() or {}
ok("C1 200", r.status_code == 200, r.status_code)
ok("C2 越界探针夹到 150", d["config"]["probe_every_s"] == 150, d["config"].get("probe_every_s"))
ok("C3 阈值夹到 2", d["config"]["ping_fail_threshold"] == 2)
ok("C4 时刻归一化 08:05", d["config"]["report_daily_at"] == "08:05")
ok("C5 响应不含明文", TOKEN not in json.dumps(d))
ok("C6 掩码末 4 位", d["config"]["token_tail"] == "…bbbb", str(d["config"].get("token_tail")))
ok("C7 心跳线程已启动", d["applied"]["started"] is True and d["running"] is True)
ok("C8 落盘存在", os.path.exists(gc.CONFIG_PATH))

with open(gc.CONFIG_PATH, "r", encoding="utf-8") as f:
    disk = json.load(f)
ok("C9 落盘 token 一致", disk.get("token") == TOKEN)
ok("C10 落盘值已是夹取后", disk.get("probe_every_s") == 150, disk.get("probe_every_s"))

r = C.post("/api/guard/config", json={"interval_s": 30})     # 不传 token
d = r.get_json() or {}
ok("C11 不传 token 不清空", (d["config"].get("token_set") is True
                        and disk.get("token") == TOKEN))
ok("C12 interval 更新", d["config"]["interval_s"] == 30)

r = C.post("/api/guard/config", json={"enabled": False})
d = r.get_json() or {}
ok("C13 关闭 → stopped", d["applied"]["stopped"] is True, str(d.get("applied")))
ok("C14 关闭 → running False", d["running"] is False)

# ── D 状态接口形状 ──
print("\n[D] GET /api/guard/status")
r = C.get("/api/guard/status")
d = r.get_json() or {}
ok("D1 200", r.status_code == 200)
ok("D2 顶层有 sender", isinstance(d.get("sender"), dict), str(type(d.get("sender"))))
ok("D3 顶层有 stale", "stale" in d)
ok("D4 channels 是对象", isinstance(d.get("channels"), dict))
ok("D5 关闭时 enabled=False", d.get("enabled") is False)
ok("D6 状态不含 token", TOKEN not in json.dumps(d))
ok("D7 未启用时 snapshot 为 null", d.get("snapshot") is None)

print("\n" + "=" * 60)
print("结果: %d PASS / %d FAIL" % (PASS, FAIL))
if FAILS:
    for x in FAILS:
        print("  - " + x)
print("临时目录（保留便于排查）:", TMP)
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
