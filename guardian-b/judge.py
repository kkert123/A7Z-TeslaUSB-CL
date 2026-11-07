#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 判死 / 告警状态机
===============================
对齐设计 §2（通道判别矩阵）+ §4（判死伪代码）。

核心不变式：
  1. `last_seen[channel]` 一律是 **B 自己的接收时刻**（B 侧坐标），不采信 A 的 ts（M51）。
  2. 超时判定与阈值比较全部用 **B 的墙钟**；`time.time()` 单调性由 VPS NTP 保证。
  3. 一切超时都以「静默优先」为默认：预期离线窗口 / 车在开 → 直接静默，防误报（M90 对应面）。

与 §4 伪代码的**一处有意偏离**（已同步文档 §4 注释）：
  伪代码里 TS 故障判据写作 `age_ts >= ALARM and age_https < WARN`。
  这会让 https 年龄落在 [WARN, ALARM) 时**两个分支都不命中而静默**，
  但此时 https 尚在「未告警」区、设备显然可达 → 应判 **TS 故障**。
  故本实现改用 `age_https < ALARM`，与 §2 矩阵口径一致。
"""

import logging
import time

logger = logging.getLogger("guard-b.judge")

FAULT_TS = "ts_channel_fault"     # Tailscale 通道故障（设备仍在线）
FAULT_DEV = "device_offline"      # 设备失联（断电/崩溃/断网不可区分）

FAULT_TITLE = {
    FAULT_TS: "Tailscale 通道故障（设备在线）",
    FAULT_DEV: "设备失联",
}


def _classify_vehicle(body):
    """返回 (online_expected, reason)：
      online_expected: True=该在线, False=不该在线(在开/已知离线), None=未知
    """
    if not isinstance(body, dict):
        return None, "no_body"
    v = body.get("vehicle")
    if not isinstance(v, dict):
        return None, "no_vehicle_field"
    locked = v.get("locked")
    present = v.get("user_present")
    sentry = v.get("sentry_mode")
    if locked is True and present is False and sentry is True:
        return True, "sentry_on_parked_locked"
    if present is True:
        return False, "user_present"
    if sentry is False:
        return False, "sentry_off"
    if locked is False:
        return False, "unlocked"
    return None, "partial"


class Judge:
    def __init__(self, cfg, store, alerter, now_fn=None) -> None:
        self.cfg = cfg
        self.store = store
        self.alerter = alerter
        self._now = now_fn or time.time

    # ── 入口：收到心跳 ───────────────────────────────────────
    def on_heartbeat(self, channel: str, seq, body: dict, now: float = None) -> dict:
        now = self._now() if now is None else now
        ev = body.get("event") if isinstance(body, dict) else None
        if ev == "going_offline":
            # 优先用相对量 offline_s（跨机安全）：A 的墙钟可能未同步（冷启动可慢 87 分钟），
            # 直接采信它的 expected_until 会算错窗口——快了就漏报、慢了当场误报（M51）。
            # 兼容旧 A：退化为 expected_until，但必须夹到 [now+60, now+6h]，
            # 否则一个坏钟就能把窗口设成负数（立刻告警）或几年（永不告警）。
            exp = None
            rel = body.get("offline_s")
            if rel is not None:
                try:
                    exp = now + float(rel)
                except (TypeError, ValueError):
                    exp = None
            if exp is None:
                try:
                    exp = float(body.get("expected_until") or 0)
                except (TypeError, ValueError):
                    exp = 0
                if exp <= 0:
                    exp = now + self.cfg.offline_default_s
                exp = min(max(exp, now + 60), now + 21600)
            self.store.set_offline(exp, body.get("reason", ""))
            self.store.touch(channel, seq, body, now=now, store_body=False)
            logger.info("预期离线握手下达 channel=%s reason=%s until=%.0f",
                        channel, body.get("reason", ""), exp)
            return {"event": "going_offline", "expected_until": exp}
        self.store.touch(channel, seq, body, now=now, store_body=True)
        return {"event": "heartbeat", "channel": channel}

    # ── 入口：周期 tick ─────────────────────────────────────
    def tick(self, now: float = None) -> list:
        """返回本次 tick 产生的事件列表（供测试断言 / 日志）。"""
        now = self._now() if now is None else now
        data = self.store.data
        ls = data.get("last_seen", {})
        age_ts = now - float(ls.get("ts", 0.0))
        age_https = now - float(ls.get("https", 0.0))

        # 1) 预期离线窗口 → 静默
        if now < float(data.get("offline_until", 0.0)):
            return [{"type": "silent", "reason": "expected_offline"}]

        # 2) 车在开 / 已知离线 → 静默（防误报核心）
        online_expected, why = _classify_vehicle(data.get("last_body"))
        if online_expected is False:
            return [{"type": "silent", "reason": "not_expected_online", "why": why}]
        if online_expected is None and self.cfg.unknown_vehicle == "silent":
            return [{"type": "silent", "reason": "unknown_vehicle", "why": why}]

        events = []
        warn = self.cfg.warn_s
        alarm = self.cfg.alarm_s

        # 只统计「曾经收到过心跳」的通道：单通道部署（只配 https）时
        # last_seen.ts 恒为 0 → age_ts 是个巨大值，旧判据会永远报「Tailscale 通道故障」。
        ts_seen = float(ls.get("ts", 0.0)) > 0
        https_seen = float(ls.get("https", 0.0)) > 0
        if not ts_seen and not https_seen:
            # 从未收到任何心跳：没有基准可比，不判死（首次部署 / 刚重启）
            return [{"type": "ok", "reason": "no_heartbeat_yet"}]
        ages = ([age_ts] if ts_seen else []) + ([age_https] if https_seen else [])
        age_min = min(ages)
        # 主通道：有 TS 就看 TS（TS 新鲜即设备在线，哪怕 https 冷却中）；
        # 没有 TS（单通道部署）才退化看 https。
        primary_age = age_ts if ts_seen else age_https

        # 3) 健康区：正常 + 检查恢复
        if primary_age < warn:
            events.extend(self._maybe_recover(now, age_ts, age_https))
            if not events:
                events.append({"type": "ok", "age_ts": round(age_ts, 1),
                               "age_https": round(age_https, 1)})
            return events

        # 4) 告警区
        fault = None
        if ts_seen and https_seen and age_ts >= alarm and age_https < alarm:
            fault = FAULT_TS
        elif age_min >= alarm:
            fault = FAULT_DEV

        if fault is None:
            # WARN..ALARM 过渡带：不告警，也不恢复（保持既有状态）
            return [{"type": "warn", "age_ts": round(age_ts, 1),
                     "age_https": round(age_https, 1)}]

        events.append(self._raise(fault, now, age_ts, age_https))

        # 另一类故障若已恢复，一并收敛
        other = FAULT_DEV if fault == FAULT_TS else FAULT_TS
        events.extend(self._recover_one(other, now, age_ts, age_https))
        return events

    # ── 告警 / 恢复 内部 ────────────────────────────────────
    def _raise(self, fault: str, now: float, age_ts: float, age_https: float) -> dict:
        rec = self.store.alert_slot(fault)
        last_sent = float(rec.get("last_sent", 0.0))
        # 去重：同故障 GUARD_DEDUP_S 内只推一次（**不依赖 active**——恢复后
        # 紧接的复发同样应被抑制，避免「恢复+告警」抖成两条）。
        # 注意 last_sent>0 的前提：从未推送过（=0）绝不能判成重复。
        if last_sent > 0.0 and (now - last_sent) < self.cfg.dedup_s:
            rec["active"] = True          # 故障持续存在，仅抑制重复推送
            self.store.save()
            return {"type": "dedup", "fault": fault}
        msg = self._format_fault(fault, age_ts, age_https)
        ok = self.alerter.send_text(msg)
        rec["active"] = True
        rec["last_sent"] = now
        self.store.save()
        return {"type": "alert", "fault": fault, "delivered": bool(ok)}

    def _maybe_recover(self, now: float, age_ts: float, age_https: float) -> list:
        return (self._recover_one(FAULT_TS, now, age_ts, age_https)
                + self._recover_one(FAULT_DEV, now, age_ts, age_https))

    def _recover_one(self, fault: str, now: float, age_ts: float, age_https: float) -> list:
        rec = self.store.alert_slot(fault)
        if not rec.get("active"):
            return []
        cleared = (age_ts < self.cfg.warn_s) if fault == FAULT_TS \
            else (min(age_ts, age_https) < self.cfg.warn_s)
        if not cleared:
            return []
        self.alerter.send_text("【恢复】%s —— 心跳已恢复" % FAULT_TITLE.get(fault, fault))
        rec["active"] = False
        rec["last_sent"] = now
        self.store.save()
        return [{"type": "recover", "fault": fault}]

    def _format_fault(self, fault: str, age_ts: float, age_https: float) -> str:
        body = self.store.data.get("last_body") or {}
        last_ts = body.get("ts")
        ver = body.get("version", "?")
        tail = ""
        if fault == FAULT_TS:
            tail = "\n（设备仍在心跳，仅 Tailscale 通道不通；请检查 tailnet / A 的 TS key）"
        else:
            tail = "\n（无法区分断电/崩溃/断网——如实报失联）"
        return (
            "【停车自检·告警】%s\n"
            "last_seen(ts)=%.0fs前  last_seen(https)=%.0fs前\n"
            "A 版本=%s  A 上报 ts=%s%s"
            % (FAULT_TITLE.get(fault, fault), age_ts, age_https, ver, last_ts, tail)
        )

    # ── 状态页用 ────────────────────────────────────────────
    def classify(self, now: float = None) -> str:
        now = self._now() if now is None else now
        data = self.store.data
        if now < float(data.get("offline_until", 0.0)):
            return "expected_offline"
        online_expected, _ = _classify_vehicle(data.get("last_body"))
        if online_expected is False:
            return "not_expected_online"
        ls = data.get("last_seen", {})
        age_ts = now - float(ls.get("ts", 0.0))
        age_https = now - float(ls.get("https", 0.0))
        ts_seen = float(ls.get("ts", 0.0)) > 0
        https_seen = float(ls.get("https", 0.0)) > 0
        if not ts_seen and not https_seen:
            return "no_heartbeat_yet"
        ages = ([age_ts] if ts_seen else []) + ([age_https] if https_seen else [])
        if ts_seen and https_seen and age_ts >= self.cfg.alarm_s and age_https < self.cfg.alarm_s:
            return "ts_channel_fault"
        if min(ages) >= self.cfg.alarm_s:
            return "device_offline"
        if (age_ts if ts_seen else age_https) >= self.cfg.warn_s:
            return "warn"
        return "ok"
