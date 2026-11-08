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
import tempfile
import threading
import time

from flask import Flask, jsonify, request

from config import as_bool, load_config
from state import StateStore
from alerts import Alerter
from judge import Judge

logger = logging.getLogger("guard-b")

VERSION = "1.0.2"


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
            "version": VERSION,
            "now": int(time.time()),
            "classify": judge.classify(),
            "warn_s": cfg.warn_s,
            "alarm_s": cfg.alarm_s,
            "alert": alerter.status(),      # 只回 configured/key_tail/dry_run，不回明文
            "state": snap,
        })

    # ── 配置（v1.0.2：供 A 端设置页下发机器人 key）───────────
    @app.route("/guard/config", methods=["GET"])
    def get_config():
        if not _authorized():
            return _deny()
        return jsonify({
            "ok": True,
            "version": VERSION,
            "alert": alerter.status(),
            "warn_s": cfg.warn_s,
            "alarm_s": cfg.alarm_s,
            "dedup_s": cfg.dedup_s,
            "offline_default_s": cfg.offline_default_s,
        })

    @app.route("/guard/config", methods=["POST"])
    def set_config():
        """只接受「可在线修改项」：wecom_key / alert_dry_run。

        刻意不允许改 warn_s / alarm_s / 端口 / token —— 那些在 env 里。
        两边都能改就会出现「谁生效说不清」，出问题只能靠猜。
        """
        if not _authorized():
            return _deny()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "body_must_be_object"}), 400

        rt = {}
        if os.path.exists(cfg.runtime_path):
            try:
                with open(cfg.runtime_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    rt = loaded
            except (OSError, ValueError):
                rt = {}

        changed = []
        key = data.get("wecom_key")
        if isinstance(key, str) and key.strip():
            k = key.strip()
            if len(k) < 20:
                return jsonify({"ok": False, "error": "wecom_key_too_short"}), 400
            rt["wecom_key"] = k
            alerter.set_key(k)
            changed.append("wecom_key")
        if "alert_dry_run" in data:
            v = as_bool(data.get("alert_dry_run"), alerter.dry_run)
            rt["alert_dry_run"] = v
            alerter.dry_run = v
            changed.append("alert_dry_run")

        if not changed:
            return jsonify({"ok": False, "error": "no_editable_field"}), 400

        # 原子写 + 0600：这个文件里有 webhook key
        parent = os.path.dirname(cfg.runtime_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".rt-", suffix=".json", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rt, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, cfg.runtime_path)
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

        logger.info("运行期配置已更新: %s", ",".join(changed))
        return jsonify({"ok": True, "version": VERSION,
                        "changed": changed, "alert": alerter.status()})

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
