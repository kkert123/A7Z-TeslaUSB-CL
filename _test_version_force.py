#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本检查缓存行为测试（v0.3.1.88 / M144）

现场问题：用户在页面上点「检查版本」看不到新版本，必须先「重启服务」再点才行。
根因是 `check_latest_release` 的成功缓存 6 小时（CHECK_INTERVAL=21600），
而「检查版本」按钮不带 force → 命中缓存直接返回旧结果；重启把内存里的
`_version_last_check` 清零后才真正发出请求。

本测试钉死三条：
  A. 非 force：缓存期内**不发请求**
  B. force：绕过 6 小时缓存，真的发请求
  C. force 也不能连点：30 秒内第二次复用缓存（保护 GitHub 匿名配额）

跑法：python _test_version_force.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import version_service as vs   # noqa: E402
from app_state import state    # noqa: E402

PASS = 0
FAIL = 0
FAILED = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s" % name)
    else:
        FAIL += 1
        FAILED.append(name)
        print("FAIL %s %s" % (name, detail))


class _Resp(object):
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")
        self.headers = {}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


CALLS = []


def fake_urlopen(req, timeout=None):
    CALLS.append(getattr(req, "full_url", "?"))
    return _Resp({"tag_name": "v9.9.9", "body": "test body",
                  "html_url": "https://example/r", "assets": []})


def reset():
    CALLS[:] = []
    vs._last_request_ts = 0.0
    vs._failure_cache = None
    vs._failure_ts = 0.0
    vs._quota_guard_until = 0.0
    state._version_cache = {}
    state._version_last_check = 0
    vs.urllib.request.urlopen = fake_urlopen


def main():
    reset()

    # ── A. 首次请求会真的发出（建立缓存）──
    r1 = vs.check_latest_release(force=True)
    ok("A1 首次 force 会发请求", len(CALLS) == 1, len(CALLS))
    ok("A2 解析出 latest", r1.get("latest") == "9.9.9", r1.get("latest"))
    ok("A3 has_update", r1.get("has_update") is True, r1)

    # ── B. 缓存期内非 force 不再发请求 ──
    reset()
    vs.check_latest_release(force=False)                    # 建立缓存
    n1 = len(CALLS)
    for _ in range(3):
        vs.check_latest_release(force=False)
    ok("B1 缓存期内连续调 3 次不再发请求", len(CALLS) == n1, (n1, len(CALLS)))

    # ── C. force 绕过 6 小时缓存 ──
    # 造一个「刚检查过、但缓存里是旧版本」的现场，这正是用户遇到的形态
    state._version_cache = {"current": "0.0.1", "latest": "0.0.1",
                            "has_update": False, "changelog": "", "error": None}
    state._version_last_check = time.time()
    vs._last_request_ts = 0.0          # 但距上次真实请求已经很久
    CALLS[:] = []
    r2 = vs.check_latest_release(force=False)
    ok("C1 非 force 命中 6h 缓存（返回旧结果）", r2.get("latest") == "0.0.1", r2.get("latest"))
    ok("C2 非 force 不发请求", len(CALLS) == 0, len(CALLS))

    r3 = vs.check_latest_release(force=True)
    ok("C3 force 绕过 6h 缓存，真的发请求", len(CALLS) == 1, len(CALLS))
    ok("C4 force 拿到新版本", r3.get("latest") == "9.9.9", r3.get("latest"))

    # ── D. force 也防连点（30s 内第二次用缓存）──
    CALLS[:] = []
    vs._last_request_ts = time.time()        # 刚刚请求过
    r4 = vs.check_latest_release(force=True)
    ok("D1 30s 内第二次 force 不再发请求", len(CALLS) == 0, len(CALLS))
    ok("D2 仍然返回缓存内容", r4.get("latest") == "9.9.9", r4.get("latest"))

    # 30s 之后放行
    vs._last_request_ts = time.time() - vs.FORCE_MIN_INTERVAL - 1
    r5 = vs.check_latest_release(force=True)
    ok("D3 超过 30s 后 force 放行", len(CALLS) == 1, len(CALLS))
    ok("D4 常量如预期", (vs.CHECK_INTERVAL, vs.FORCE_MIN_INTERVAL) == (21600, 30),
       (vs.CHECK_INTERVAL, vs.FORCE_MIN_INTERVAL))

    # ── E. 路由层把 force 透传下去 ──
    # 静态断言：路由必须读 query 里的 force 并传给 check_latest_release
    src = open(os.path.join(HERE, "routes", "system_routes.py"), encoding="utf-8").read()
    seg = src[src.index("def api_version_check"):]
    seg = seg[:seg.index("\n@system_bp.route")] if "\n@system_bp.route" in seg else seg
    ok("E1 路由读取 force 参数", "request.args.get('force')" in seg, seg[:200])
    ok("E2 路由把 force 传给 check_latest_release", "check_latest_release(force=force)" in seg)

    # ── F. 前端按钮必须带 force=1 ──
    html = open(os.path.join(HERE, "templates", "system.html"), encoding="utf-8").read()
    ok("F1 「检查版本」请求带 ?force=1", "/api/version/check?force=1" in html)

    print("\n%d/%d passed" % (PASS, PASS + FAIL))
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
