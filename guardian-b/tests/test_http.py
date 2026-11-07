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
    app, cfg = build_app()
    c = app.test_client()
    r = c.post("/guard/heartbeat",
               json={"event": "going_offline", "expected_until": 123456, "reason": "driving"},
               headers={**AUTH, "X-Guard-Channel": "https", "X-Guard-Seq": "6"})
    assert r.status_code == 200, r.data
    store = app.config["GUARD"]["store"]
    assert store.data["offline_until"] == 123456.0, store.data["offline_until"]
    assert store.data["offline_reason"] == "driving"


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
