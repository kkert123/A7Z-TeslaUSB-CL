#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · HTTP 服务（B 端僚机主进程）
========================================
实现设计 §3 接口契约：

  POST /guard/heartbeat   心跳 + 预期离线握手（event=going_offline）
  GET  /guard/ping        A 侧反向心跳探活（判 B 是否活着）
  GET  /guard/status      只读状态页（默认需 token）
  GET  /healthz           无鉴权存活探针（供 OpenResty / 外部监控）

B 只做三件事：**收心跳 → 判死 → 发告警**（§1）。不做远程控制、不回调 A。

运行形态：单进程 Flask（judge / dead-man 以守护线程在进程内跑）。
公网 TLS 由 OpenResty 终结（自签证书 + A 侧 pinning，见 tls/gen_selfsigned.sh），
本服务默认只绑 127.0.0.1。
"""

import hmac
import json
import logging
import os
import threading
import time

from flask import Flask, jsonify, request

from config import load_config
from state import StateStore
from alerts import Alerter
from judge import Judge

logger = logging.getLogger("guard-b")


# ── 日志 ────────────────────────────────────────────────────
def setup_logging(cfg) -> None:
    handlers = [logging.StreamHandler()]
    try:
        d = os.path.dirname(cfg.log_path)
        if d:
            os.makedirs(d, exist_ok=True)
        handlers.append(logging.FileHandler(cfg.log_path, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


# ── 应用工厂 ────────────────────────────────────────────────
def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask("guard-b")
    store = StateStore(cfg.state_path)
    alerter = Alerter(cfg.wecom_key, cfg.wecom_url, cfg.alert_dry_run)
    judge = Judge(cfg, store, alerter)
    app.config["GUARD"] = {"cfg": cfg, "store": store, "alerter": alerter, "judge": judge}

    # ── 鉴权 ────────────────────────────────────────────────
    def _authorized() -> bool:
        hdr = request.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return False
        got = hdr[7:].strip()
        return bool(cfg.token) and hmac.compare_digest(got, cfg.token)

    def _deny():
        logger.warning("鉴权失败 来自 %s 路径 %s", request.remote_addr, request.path)
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # ── 心跳 ────────────────────────────────────────────────
    @app.route("/guard/heartbeat", methods=["POST"])
    def heartbeat():
        if not _authorized():
            return _deny()
        channel = (request.headers.get("X-Guard-Channel") or "https").strip().lower()
        if channel not in ("ts", "https"):
            channel = "https"
        seq_raw = request.headers.get("X-Guard-Seq", "")
        try:
            seq = int(seq_raw) if seq_raw != "" else None
        except ValueError:
            seq = None
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        res = judge.on_heartbeat(channel, seq, body)
        return jsonify({"ok": True, "channel": channel, "result": res})

    # ── 反向心跳（A 探活 B）──────────────────────────────────
    @app.route("/guard/ping", methods=["GET"])
    def ping():
        if not _authorized():
            return _deny()
        return jsonify({"ok": True, "ts": int(time.time())})

    # ── 存活（无鉴权）────────────────────────────────────────
    @app.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify({"ok": True, "ts": int(time.time())})

    # ── 状态页 ──────────────────────────────────────────────
    @app.route("/guard/status", methods=["GET"])
    def status():
        if not cfg.public_status and not _authorized():
            return _deny()
        snap = store.snapshot()
        # 不把 token 之类敏感物带出（状态里本就没有）
        return jsonify({
            "ok": True,
            "now": int(time.time()),
            "classify": judge.classify(),
            "warn_s": cfg.warn_s,
            "alarm_s": cfg.alarm_s,
            "state": snap,
        })

    # ── 后台线程：judge + dead-man ──────────────────────────
    if cfg.judge_enable:
        _start_judge_thread(cfg, judge)
    if cfg.deadman_url:
        _start_deadman_thread(cfg)

    return app


def _start_judge_thread(cfg, judge) -> None:
    def loop():
        logger.info("judge 线程启动 interval=%ss T=%ss warn=%ss alarm=%ss",
                    cfg.judge_interval, cfg.heartbeat_interval, cfg.warn_s, cfg.alarm_s)
        while True:
            try:
                events = judge.tick()
                for e in events:
                    if e.get("type") not in ("ok", "silent", "warn"):
                        logger.info("judge 事件: %s", e)
            except Exception:
                logger.exception("judge tick 异常（已吞，线程继续）")
            time.sleep(cfg.judge_interval)

    t = threading.Thread(target=loop, name="guard-judge", daemon=True)
    t.start()


def _start_deadman_thread(cfg) -> None:
    import requests

    def loop():
        logger.info("dead-man 线程启动 interval=%ss", cfg.deadman_interval)
        while True:
            try:
                requests.get(cfg.deadman_url, timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dead-man 打点失败: %r", exc)
            time.sleep(cfg.deadman_interval)

    t = threading.Thread(target=loop, name="guard-deadman", daemon=True)
    t.start()


def main() -> int:
    cfg = load_config()
    setup_logging(cfg)
    problems = cfg.validate()
    for p in problems:
        logger.warning("配置告警: %s", p)
    logger.info("guardian-b 启动 bind=%s:%s judge=%s",
                cfg.bind_host, cfg.bind_port, cfg.judge_enable)
    if cfg.state_path and os.path.exists(cfg.state_path):
        logger.info("已加载状态文件 %s", cfg.state_path)
    app = create_app(cfg)
    app.config["GUARD"]["store"].mark_boot()
    app.run(host=cfg.bind_host, port=cfg.bind_port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
