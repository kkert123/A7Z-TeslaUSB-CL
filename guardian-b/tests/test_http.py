#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · HTTP 层冒烟测试（Flask test_client，不起真实端口）
==============================================================
覆盖：鉴权、心跳写入、反向心跳、状态页、healthz。

运行：  <venv>/python tests/test_http.py     （需 Flask，见 requirements.txt）
"""

import json
import os
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from config import Config       # noqa: E402
from app import create_app      # noqa: E402


RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


def build_app():
    cfg = Config()
    cfg.token = "test-token-test-token-test-token"   # 32 chars
    cfg.state_path = os.path.join(tempfile.mkdtemp(prefix="guardb-http-"), "state.json")
    cfg.log_path = os.path.join(os.path.dirname(cfg.state_path), "guard-b.log")
    cfg.judge_enable = False          # 冒烟不跑后台线程
    cfg.board_enable = False          # 车态板线程同理：别在测试里去抓 4000
    cfg.deadman_url = ""
    cfg.public_status = False
    cfg.wecom_key = ""
    cfg.alert_dry_run = True
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app, cfg


AUTH = {"Authorization": "Bearer test-token-test-token-test-token"}


@case
def t_healthz_no_auth():
    app, cfg = build_app()
    c = app.test_client()
    r = c.get("/healthz")
    assert r.status_code == 200 and r.get_json()["ok"] is True, r.data


@case
def t_heartbeat_requires_auth():
    app, cfg = build_app()
    c = app.test_client()
    r = c.post("/guard/heartbeat", json={})
    assert r.status_code == 401, r.status_code


@case
def t_heartbeat_wrong_token():
    app, cfg = build_app()
    c = app.test_client()
    r = c.post("/guard/heartbeat", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401, r.status_code


@case
def t_heartbeat_ok_and_state():
    app, cfg = build_app()
    c = app.test_client()
    body = {"seq": 5, "ts": 111, "version": "0.3.1.6x",
            "vehicle": {"locked": True, "user_present": False, "sentry_mode": True}}
    r = c.post("/guard/heartbeat", json=body,
               headers={**AUTH, "X-Guard-Channel": "ts", "X-Guard-Seq": "5"})
    assert r.status_code == 200, r.data
    j = r.get_json()
    assert j["ok"] is True and j["channel"] == "ts", j
    store = app.config["GUARD"]["store"]
    assert store.data["last_seq"]["ts"] == 5, store.data["last_seq"]
    assert store.data["last_body"]["vehicle"]["locked"] is True


@case
def t_going_offline_sets_window():
    """预期离线窗口：优先用相对量 offline_s（跨机安全），绝对量必须被夹取。

    2026-11-09 修正：原断言是 `offline_until == 123456.0`（1970 年），那是**旧实现**
    的语义。实现早已按 M51 改成「相对量优先 + 绝对量夹到 [now+60, now+6h]」——
    A 的墙钟可能未同步（冷启动可慢 87 分钟），直接采信 expected_until 会算错窗口：
    快了漏报、慢了当场误报。测试跟着实现走，不能留一个"实现正确反而红"的红点。
    """
    app, cfg = build_app()
    c = app.test_client()
    now = time.time()
    r = c.post("/guard/heartbeat",
               json={"event": "going_offline", "offline_s": 1800, "reason": "driving"},
               headers={**AUTH, "X-Guard-Channel": "https", "X-Guard-Seq": "6"})
    assert r.status_code == 200, r.data
    store = app.config["GUARD"]["store"]
    assert abs(store.data["offline_until"] - (now + 1800)) < 5, store.data["offline_until"]
    assert store.data["offline_reason"] == "driving"

    # 坏钟保护：给一个已经过去的绝对时刻，不能被采信成"窗口已过期"
    r = c.post("/guard/heartbeat",
               json={"event": "going_offline", "expected_until": 123456, "reason": "driving"},
               headers={**AUTH, "X-Guard-Channel": "https", "X-Guard-Seq": "7"})
    assert r.status_code == 200, r.data
    assert store.data["offline_until"] >= time.time() + 55, store.data["offline_until"]


@case
def t_ping_ok():
    app, cfg = build_app()
    c = app.test_client()
    r = c.get("/guard/ping", headers=AUTH)
    assert r.status_code == 200 and r.get_json()["ok"] is True, r.data


@case
def t_status_requires_auth_by_default():
    app, cfg = build_app()
    c = app.test_client()
    assert c.get("/guard/status").status_code == 401
    r = c.get("/guard/status", headers=AUTH)
    assert r.status_code == 200, r.data
    j = r.get_json()
    assert "classify" in j and "state" in j


@case
def t_status_public_toggle():
    app, cfg = build_app()
    cfg.public_status = True
    c = app.test_client()
    assert c.get("/guard/status").status_code == 200


@case
def t_invalid_json_body_tolerated():
    app, cfg = build_app()
    c = app.test_client()
    r = c.post("/guard/heartbeat", data="not-json",
               headers={**AUTH, "Content-Type": "application/json",
                        "X-Guard-Channel": "https"})
    assert r.status_code == 200, r.data


@case
def t_alert_test_requires_auth():
    """链路自证端点不得裸奔：能发企微就必须带 token。"""
    app, cfg = build_app()
    c = app.test_client()
    r = c.post("/guard/alert/test")
    assert r.status_code == 401, r.data


@case
def t_alert_test_reports_delivery():
    """`mode` 必须是三态之一，让「没收到」能区分「没发」和「发了没到」。"""
    app, cfg = build_app()
    c = app.test_client()
    r = c.post("/guard/alert/test", headers=AUTH)
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True, d
    assert d["mode"] in ("sent", "dryrun", "failed"), d
    assert d["mode"] == "dryrun", "测试环境应是 dry_run，实际 %r" % (d["mode"],)


def main():
    passed, total = 0, len(RESULTS)
    for fn in RESULTS:
        try:
            fn()
            passed += 1
            print("  PASS  %s" % fn.__name__)
        except AssertionError as exc:
            print("  FAIL  %s  -> %s" % (fn.__name__, exc))
        except Exception:
            print("  ERROR %s" % fn.__name__)
            traceback.print_exc()
    print("\n%d/%d passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
