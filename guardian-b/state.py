#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 状态持久化
========================
单 JSON 文件，**原子写**（tmp + os.replace）。对应设计 §7。

关键约定：
- `last_seen[channel]` 记录的是 **B 自己的接收时刻**（B 侧单调坐标），
  **绝不使用 A 传来的 `ts` 做超时判断**（M51：跨机时钟不可比）。
  A 的 `ts` 仅存于 `last_body` 供展示/诊断。
- 进程重启后状态不丢；`offline_until` 窗口跨重启依然有效。
"""

import json
import os
import tempfile
import threading
import time


def _empty_state() -> dict:
    return {
        "ver": 1,
        "last_seen": {"ts": 0.0, "https": 0.0},
        "last_seq": {"ts": 0, "https": 0},
        "last_body": None,
        "last_event": None,
        "offline_until": 0.0,
        "offline_reason": "",
        "alerts": {},
        "boot": 0.0,
        "updated": 0.0,
    }


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data = self._load()

    # ── 读写 ────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = _empty_state()
            # 浅合并，容忍旧版本缺字段
            for k, v in data.items():
                base[k] = v
            for k in ("last_seen", "last_seq", "alerts"):
                if not isinstance(base.get(k), dict):
                    base[k] = _empty_state()[k]
            base["last_seen"].setdefault("ts", 0.0)
            base["last_seen"].setdefault("https", 0.0)
            base["last_seq"].setdefault("ts", 0)
            base["last_seq"].setdefault("https", 0)
            return base
        except FileNotFoundError:
            return _empty_state()
        except Exception:
            # 损坏 → 从空状态起（不因单文件损坏让服务起不来）
            return _empty_state()

    def save(self) -> None:
        with self._lock:
            self.data["updated"] = time.time()
            d = os.path.dirname(self.path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # ── 业务写入 ────────────────────────────────────────────
    def touch(self, channel: str, seq: int, body: dict, now: float = None,
              store_body: bool = True) -> None:
        """记录一次心跳（channel ∈ {ts, https}）。

        store_body=False：只更新 last_seen / last_event（用于 going_offline 之类
        **不含 vehicle 快照**的事件包，避免覆盖上一份有效心跳的车辆状态）。
        """
        now = time.time() if now is None else now
        with self._lock:
            self.data["last_seen"][channel] = now
            if seq is not None:
                self.data["last_seq"][channel] = seq
            if isinstance(body, dict):
                self.data["last_event"] = body.get("event")
            if body is not None and store_body:
                self.data["last_body"] = body
                self.data["last_body"]["_recv_channel"] = channel
                self.data["last_body"]["_recv_at"] = now
            self.save()

    def set_offline(self, expected_until: float, reason: str) -> None:
        with self._lock:
            self.data["offline_until"] = float(expected_until or 0)
            self.data["offline_reason"] = reason or ""
            self.save()

    def alert_slot(self, key: str) -> dict:
        """取（或建）某故障键的告警记录。调用方决定是否推送与更新。"""
        with self._lock:
            rec = self.data["alerts"].get(key)
            if not isinstance(rec, dict):
                rec = {"last_sent": 0.0, "active": False}
                self.data["alerts"][key] = rec
            rec.setdefault("last_sent", 0.0)
            rec.setdefault("active", False)
            return rec

    def mark_boot(self) -> None:
        with self._lock:
            self.data["boot"] = time.time()
            self.save()

    # ── 只读视图 ────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.data, ensure_ascii=False))
