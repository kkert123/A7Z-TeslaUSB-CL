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
        # v1.1.4：TeslaMate 车态板（免 token 抓首页 SSR）+ 锁车代发闩锁
        "board": None,
        "lock_check": {},
        "boot": 0.0,
        "last_test": {},
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
            for k in ("last_seen", "last_seq", "alerts", "last_test"):
                if not isinstance(base.get(k), dict):
                    base[k] = _empty_state()[k]
            for k in ("offline_until", "boot", "updated"):
                if not isinstance(base.get(k), (int, float)):
                    base[k] = 0.0
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

    def note_test(self, body: dict, now: float = None) -> None:
        """只记录「收到过测试心跳」，**不动 last_seen / last_body**。

        测试心跳若走 `touch()` 会做两件坏事：
          ① 把 last_seen 刷成「刚刚」，掩盖真正的心跳中断；
          ② 把 body 里的 vehicle 存成「该在线」，于是 ALARM 一到必然报「设备失联」。
        2026-11-08 现场就是这样被一条 `version=manual-test` 的 curl 弄出整夜误报。
        """
        now = time.time() if now is None else now
        with self._lock:
            body = body if isinstance(body, dict) else {}
            self.data["last_test"] = {
                "at": now,
                "version": str(body.get("version") or ""),
                "channel": str(body.get("_recv_channel") or ""),
            }
            self.save()

    def set_offline(self, expected_until: float, reason: str) -> None:
        with self._lock:
            self.data["offline_until"] = float(expected_until or 0)
            self.data["offline_reason"] = reason or ""
            self.save()

    def set_board(self, sample: dict, now: float = None) -> dict:
        """存一次车态板采样，返回**上一次**采样（调用方据此判「未锁→已锁」边沿）。

        边沿判定必须落在持久状态里：进程重启后还能知道上一拍是未锁，
        否则重启后的第一拍会被当成「刚锁车」，代发一条重复的自检。
        """
        now = time.time() if now is None else now
        with self._lock:
            prev = self.data.get("board")
            prev = dict(prev) if isinstance(prev, dict) else {}
            cur = {"at": now}
            for k in ("locked", "sentry", "state", "err"):
                cur[k] = (sample or {}).get(k)
            self.data["board"] = cur
            self.save()
            return prev

    def arm_lock_check(self, now: float = None) -> None:
        """记一次锁车事件，等宽限期过后由 tick 判定要不要代发。"""
        now = time.time() if now is None else now
        with self._lock:
            self.data["lock_check"] = {"at": now, "done": False, "why": ""}
            self.save()

    def clear_lock_check(self) -> None:
        """开锁 → 闩锁复位（下一把锁还要再报一次）。"""
        with self._lock:
            if self.data.get("lock_check"):
                self.data["lock_check"] = {}
                self.save()

    def mark_lock_check_done(self, why: str = "") -> None:
        with self._lock:
            lc = self.data.get("lock_check")
            if not isinstance(lc, dict):
                lc = {}
            lc["done"] = True
            lc["why"] = why
            self.data["lock_check"] = lc
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
