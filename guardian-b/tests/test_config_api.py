#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 运行期配置与告警三态 单测（v1.0.2）
=================================================
覆盖：
  1. `Alerter.send_text` 返回三态 sent/dryrun/failed（不再是 bool）
  2. `POST /guard/config` 原子写 runtime.json（0600）、热生效、响应不含明文 key
  3. `GET /guard/config` / `/guard/status` 只回 key_tail
  4. runtime.json 覆盖 env（B 重启后仍是 UI 保存的值）

运行：python tests/test_config_api.py
"""

import json
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "t" * 48
TMPDIR = tempfile.mkdtemp(prefix="guardb-test-")
STATE = os.path.join(TMPDIR, "state.json")
RUNTIME = os.path.join(TMPDIR, "runtime.json")

os.environ.update({
    "GUARD_TOKEN": TOKEN,
    "GUARD_STATE_PATH": STATE,
    "GUARD_RUNTIME_PATH": RUNTIME,
    "GUARD_JUDGE_ENABLE": "0",          # 不起后台线程，测试才可控
    "GUARD_PUBLIC_STATUS": "0",
    "GUARD_WECOM_KEY": "",
    "GUARD_ALERT_DRY_RUN": "1",
})

while "." in sys.path:                   # 避免把项目根的同名模块抢先导入
    sys.path.pop(0)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as gcfg            # noqa: E402
import alerts as galerts         # noqa: E402
import app as gapp               # noqa: E402

PASS = FAIL = 0
FAILS = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL  %s  %s" % (name, detail))


def auth(client, method, path, **kw):
    kw.setdefault("headers", {})
    kw["headers"]["Authorization"] = "Bearer " + TOKEN
    return getattr(client, method)(path, **kw)


print("=" * 64)
print("guardian-b 运行期配置测试")
print("=" * 64)

# ── A. 告警三态 ──────────────────────────────────────────────
print("\n[A] Alerter 三态")

a_dry = galerts.Alerter(key="k" * 40, dry_run=True)
ok("A1 dry_run → 'dryrun'", a_dry.send_text("x") == "dryrun", a_dry.send_text("x"))

a_none = galerts.Alerter(key="", url="", dry_run=False)
ok("A2 无通道 → 'failed'", a_none.send_text("x") == "failed", a_none.send_text("x"))

a_real = galerts.Alerter(key="", url="", dry_run=False)
ok("A3 未配置时不抛异常", isinstance(a_real.send_text("x"), str))

st = a_dry.status()
ok("A4 status 不回明文 key", st.get("key_tail") == "…kkkk" and "configured" in st, str(st))

# ── B. 配置接口 ──────────────────────────────────────────────
print("\n[B] /guard/config 接口")

cfg = gcfg.load_config()
application = gapp.create_app(cfg)
client = application.test_client()

r = client.get("/guard/config")
ok("B1 无 token → 401", r.status_code == 401, r.status_code)

r = auth(client, "get", "/guard/config")
body = r.get_json() or {}
ok("B2 带 token → 200", r.status_code == 200, r.status_code)
ok("B3 版本号存在", body.get("version") == gapp.VERSION, body.get("version"))
ok("B4 GET 不含明文 key", "wecom_key" not in json.dumps(body), json.dumps(body)[:120])

NEWKEY = "11111111-2222-3333-4444-555555555555"
r = auth(client, "post", "/guard/config", json={"wecom_key": NEWKEY})
rb = r.get_json() or {}
ok("B5 POST 200", r.status_code == 200, r.status_code)
ok("B6 changed 含 wecom_key", "wecom_key" in (rb.get("changed") or []), str(rb.get("changed")))
ok("B7 响应不含明文 key", NEWKEY not in json.dumps(rb), "泄露！")

ok("B8 runtime.json 已创建", os.path.exists(RUNTIME))
mode = stat.S_IMODE(os.stat(RUNTIME).st_mode) if os.path.exists(RUNTIME) else 0
if os.name == "posix":
    ok("B9 runtime.json 权限 0600", mode == 0o600, oct(mode))
else:
    # Windows 的 chmod 只有只读位，0600 无法表达（真机是 Linux）。
    # 这里只记录，不当失败——把跨平台差异伪装成"全绿"比失败更坏。
    print("  INFO  B9 权限检查跳过（Windows 无 POSIX 权限位，实测 %s）" % oct(mode))

disk = {}
if os.path.exists(RUNTIME):
    try:
        with open(RUNTIME, "r", encoding="utf-8") as f:
            disk = json.load(f)
    except Exception:
        disk = {}
ok("B10 落盘内容正确", disk.get("wecom_key") == NEWKEY, str(disk)[:80])

ok("B11 内存已热生效", application.config["GUARD"]["alerter"].key == NEWKEY)

# 换一个新 Config：模拟 B 重启后仍读 UI 保存的值
fresh = gcfg.load_config()
ok("B12 runtime 覆盖 env（重启后仍生效）", fresh.wecom_key == NEWKEY, fresh.wecom_key[:8])

r = auth(client, "post", "/guard/config", json={"wecom_key": "short"})
ok("B13 过短 key → 400", r.status_code == 400, r.status_code)

r = auth(client, "post", "/guard/config", json={"warn_s": 1})
ok("B14 不可编辑字段 → 400", r.status_code == 400, r.status_code)

r = auth(client, "post", "/guard/config", json={"alert_dry_run": False})
rb = r.get_json() or {}
ok("B15 关闭 dry_run 生效", rb.get("alert", {}).get("dry_run") is False, str(rb.get("alert")))
r = auth(client, "post", "/guard/config", json={"alert_dry_run": True})
ok("B16 重新开启 dry_run", (r.get_json() or {}).get("alert", {}).get("dry_run") is True)

# ── C. 状态页不外泄 ──────────────────────────────────────────
print("\n[C] /guard/status")

r = auth(client, "get", "/guard/status")
txt = r.get_data(as_text=True)
ok("C1 状态页 200", r.status_code == 200, r.status_code)
ok("C2 状态页不含明文 key", NEWKEY not in txt)
ok("C3 状态页含 alert 摘要", '"alert"' in txt and "key_tail" in txt)
ok("C4 状态页不含 token", TOKEN not in txt)

print("\n" + "=" * 64)
print("结果: %d 通过, %d 失败" % (PASS, FAIL))
if FAILS:
    for f in FAILS:
        print("  - " + f)
print("=" * 64)
sys.exit(0 if FAIL == 0 else 1)
