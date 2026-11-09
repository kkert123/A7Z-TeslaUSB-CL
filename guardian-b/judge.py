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
    FAULT_TS: "Tailscale 通道故障",
    FAULT_DEV: "设备失联",
}


def _human_age(sec) -> str:
    """把秒数换成人话。告警里出现 `1794124165s前` 这种数字等于没写。"""
    try:
        s = max(0.0, float(sec))
    except (TypeError, ValueError):
        return "未知"
    if s < 90:
        return "%.0f 秒前" % s
    if s < 5400:
        return "%.0f 分钟前" % (s / 60.0)
    if s < 172800:
        return "%.1f 小时前" % (s / 3600.0)
    return "%.1f 天前" % (s / 86400.0)


def _is_test_body(body) -> bool:
    """识别手工/联调心跳。

    这类 body 若走正常路径会做两件坏事：把 `last_seen` 刷成「刚刚」掩盖真实中断，
    且把 `vehicle` 存成「该在线」→ ALARM 一到必报「设备失联」。
    2026-11-08 现场就是被一条 `version=manual-test` 的 curl 弄出整夜误报。
    """
    if not isinstance(body, dict):
        return False
    if body.get("test") is True:
        return True
    return str(body.get("version") or "").strip().lower().startswith(
        ("manual", "test", "curl", "selftest"))


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
    # 「车在动」**必须最先判**：它在语义上覆盖 user_present（司机就在车上），
    # 而 `why` 决定这条静默证据有没有保质期（见 Judge._evidence_fresh）——
    # 若让 user_present 先命中，出车途中失联会被当成「人在车边、不必告警」（M135）。
    st = str(v.get("state") or "").strip().lower()
    sp = v.get("speed")
    if st == "driving" or (isinstance(sp, (int, float)) and sp > 3):
        return False, "driving"
    if locked is True and present is False and sentry is True:
        return True, "sentry_on_parked_locked"
    if present is True:
        return False, "user_present"
    if sentry is False:
        return False, "sentry_off"
    if locked is False:
        return False, "unlocked"
    return None, "partial"


def _vehicle_line(v) -> str:
    """把最后一次车态说成人话。

    用户点开告警第一反应是「车到底锁了没」——只写「设备失联」等于没说。
    这些字段全是 A 最后一帧的**快照**，可能已过期，所以文案里带「最后」。
    """
    if not isinstance(v, dict) or not v:
        return "未知"
    bits = []
    st = str(v.get("state") or "").strip().lower()
    if st == "driving":
        sp = v.get("speed")
        bits.append("在开 %gkm/h" % sp if isinstance(sp, (int, float)) else "在开")
    elif st:
        bits.append({"online": "车在线", "asleep": "车休眠", "offline": "车离线"}
                    .get(st, "状态=%s" % st))
    lk = v.get("locked")
    if lk is True:
        bits.append("已锁")
    elif lk is False:
        bits.append("未锁")
    if v.get("sentry_mode") is True:
        bits.append("哨兵开")
    if v.get("user_present") is True and st != "driving":
        bits.append("人在车上")
    sh = str(v.get("shift_state") or "").strip().upper()
    if sh:
        bits.append("%s 档" % sh)
    return " · ".join(bits) or "未知"


class Judge:
    def __init__(self, cfg, store, alerter, now_fn=None) -> None:
        self.cfg = cfg
        self.store = store
        self.alerter = alerter
        self._now = now_fn or time.time

    # ── 入口：收到心跳 ───────────────────────────────────────
    def on_heartbeat(self, channel: str, seq, body: dict, now: float = None) -> dict:
        now = self._now() if now is None else now
        if _is_test_body(body):
            # 测试心跳只记「收到过」，绝不参与在线判定
            self.store.note_test(dict(body or {}, _recv_channel=channel), now=now)
            logger.info("收到测试心跳（不计入在线判定）version=%s",
                        (body or {}).get("version"))
            return {"event": "test", "ignored": True}
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
        # 0) 锁车代发：独立事件通道，**不受下面任何静默闸影响**——
        #    它要解决的正是「A 已经哑了，静默闸会把告警吞掉」那种场景。
        self._maybe_relay_lock(now)
        ls = data.get("last_seen", {})
        age_ts = now - float(ls.get("ts", 0.0))
        age_https = now - float(ls.get("https", 0.0))

        # 1) 预期离线窗口 → 静默
        if now < float(data.get("offline_until", 0.0)):
            return [{"type": "silent", "reason": "expected_offline"}]

        # 2) 车在开 / 已知离线 → 静默（防误报核心）
        online_expected, why = _classify_vehicle(data.get("last_body"))
        if online_expected is False and self._could_be_silent(why, now):
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

    def _could_be_silent(self, why: str, now: float) -> bool:
        """车态证据能不能作为「现在不必告警」的理由。

        分两类，**不能一刀切**：
          · `driving`：车在动 → A7Z 一定带电、一定该在线。它不说话只有两种解释
            「网络断了」或「设备死了」，两种都该告警 → 只给有限保质期。
          · `sentry_off` / `unlocked` / `user_present`：车停着、人可能就在旁边，
            A7Z 随车机 USB 断电是**正常现象**（每晚都发生）→ 长期静默是对的，
            给它们加保质期会让每次锁车都误报。

        2026-11-09 实证：15:36:57 A 在 43km/h 上断掉，B 的出车窗口 16:29:55 一过期，
        这一帧「车在开」被当成永久现状 → 一路静默 1h38m，`last_sent` 始终 0.0。
        """
        if why != "driving":
            return True
        return self._evidence_fresh(now)

    def _evidence_fresh(self, now: float) -> bool:
        """`last_body` 是「A 死前最后一句话」，只能替它顶 `offline_default_s`（默认 1h）。

        超过这个时长仍无新心跳，就说明 A 已经不说话了，再拿这帧判「车在开、
        不必告警」= 无限期静默。时长与出车静默窗口同源，运维只需理解一个旋钮。
        """
        lb = self.store.data.get("last_body") or {}
        try:
            recv_at = float(lb.get("_recv_at") or 0.0)
        except (TypeError, ValueError):
            recv_at = 0.0
        if recv_at <= 0.0:
            # 老状态文件没有 `_recv_at`：退回「最后一次收到心跳的时刻」，
            # 绝不能用 0.0 直接比 now（那会让升级后第一拍立刻误报）。
            ls = self.store.data.get("last_seen") or {}
            try:
                recv_at = max(float(ls.get("ts") or 0.0), float(ls.get("https") or 0.0))
            except (TypeError, ValueError):
                recv_at = 0.0
        return recv_at > 0.0 and (now - recv_at) < float(self.cfg.offline_default_s)

    # ── 锁车代发（v1.1.4）────────────────────────────────────
    def on_board(self, sample: dict, now: float = None) -> dict:
        """接收一次车态板采样（TeslaMate 首页 SSR），维护锁车闩锁。

        锁车是**事件**，必须存进持久状态：重启后仍要知道「上一拍是未锁」，
        否则重启第一拍会被当成「刚锁车」，代发一条重复自检。
        """
        now = self._now() if now is None else now
        prev = self.store.set_board(sample, now=now)
        locked = (sample or {}).get("locked")
        if locked is False:
            self.store.clear_lock_check()
            return prev
        if locked is True and prev.get("locked") is not True:
            self.store.arm_lock_check(now)
            logger.info("观测到锁车（TeslaMate 车态板）：%ds 后判定是否代发",
                        self.cfg.lock_grace_s)
        return prev

    def _device_alive(self, now: float) -> bool:
        """设备最近一次心跳是否还在健康区（= A 自己能推）。"""
        ls = self.store.data.get("last_seen") or {}
        seen = [float(ls.get(k) or 0.0) for k in ("ts", "https")]
        seen = [t for t in seen if t > 0]
        return bool(seen) and (now - max(seen)) <= self.cfg.warn_s

    def _maybe_relay_lock(self, now: float) -> None:
        lc = self.store.data.get("lock_check") or {}
        if not lc.get("at") or lc.get("done"):
            return
        if (now - float(lc["at"])) < self.cfg.lock_grace_s:
            return
        b = self.store.data.get("board") or {}
        # 车态板本身必须新鲜：拿一小时前的「已锁」去代发，等于编造
        if (now - float(b.get("at") or 0.0)) > 900:
            self.store.mark_lock_check_done("board_stale")
            logger.info("锁车代发跳过：车态板已过期")
            return
        if self._device_alive(now):
            self.store.mark_lock_check_done("device_alive")
            logger.info("锁车代发跳过：设备在线，A 端会自己推自检")
            return
        mode = self.alerter.send_text(self._fmt_lock_relay(now))
        self.store.mark_lock_check_done("sent:%s" % mode)
        # 这条代发本身就是一次「设备失联」告知，把通用失联槽一并记账，
        # 否则同一拍还会再推一条「设备失联」，两条消息说同一件事。
        rec = self.store.alert_slot(FAULT_DEV)
        rec["active"] = True
        rec["last_sent"] = now
        self.store.save()
        logger.warning("锁车代发已发送（设备失联中）mode=%s", mode)

    def _fmt_lock_relay(self, now: float) -> str:
        b = self.store.data.get("board") or {}
        ls = self.store.data.get("last_seen") or {}
        body = self.store.data.get("last_body") or {}
        ages = [now - float(ls[k]) for k in ("ts", "https") if float(ls.get(k) or 0.0) > 0]
        car = []
        if b.get("locked") is True:
            car.append("已锁")
        if b.get("sentry") is True:
            car.append("哨兵开")
        if b.get("state"):
            car.append("车辆状态 %s" % b["state"])
        return "\n".join([
            "【停车自检·僚机代发】车辆已锁车 · 设备失联",
            "· 车态（TeslaMate）：%s" % (" · ".join(car) or "未知"),
            "· A7Z：最后心跳 %s（%s）" % (_human_age(min(ages)) if ages else "从未收到",
                                         str(body.get("version") or "未知")),
            "",
            "本条由 B 端代发：设备此刻没有心跳，它自己的停车自检（USB/网络/存储）生成不了。",
            "建议：确认设备指示灯，以及它当时连的热点（车载 MIFI / 家宽）是否可用。",
        ])

    # ── 告警 / 恢复 内部 ────────────────────────────────────
    def _raise(self, fault: str, now: float, age_ts: float, age_https: float) -> dict:
        rec = self.store.alert_slot(fault)
        last_sent = float(rec.get("last_sent", 0.0))
        n = int(rec.get("n", 0) or 0)
        gap = self._backoff_gap(n)
        # 去重：同故障按**递增间隔**推送（**不依赖 active**——恢复后紧接的复发
        # 同样应被抑制，避免「恢复+告警」抖成两条）。
        # 注意 last_sent>0 的前提：从未推送过（=0）绝不能判成重复。
        if last_sent > 0.0 and (now - last_sent) < gap:
            rec["active"] = True          # 故障持续存在，仅抑制重复推送
            self.store.save()
            return {"type": "dedup", "fault": fault,
                    "next_in_s": int(gap - (now - last_sent))}
        msg = self._format_fault(fault, age_ts, age_https)
        mode = self.alerter.send_text(msg)      # "sent" / "dryrun" / "failed"
        rec["active"] = True
        rec["last_sent"] = now
        rec["n"] = n + 1
        self.store.save()
        return {"type": "alert", "fault": fault, "mode": mode,
                "delivered": mode in ("sent", "dryrun"), "seq": n + 1}

    def _backoff_gap(self, n: int) -> float:
        """同一故障重复推送的间隔递增：30min → 2h → 6h → 24h 封顶，恢复后归零。

        旧实现固定 `dedup_s`(30min)：设备离线一整晚 = 20 条企微消息，用户会给整个群
        静音，等于把告警废掉（M89「限流限频率」的同一个反面）。
        """
        try:
            seq = tuple(int(x) for x in (self.cfg.alert_backoff or ()))
        except (AttributeError, TypeError, ValueError):
            seq = ()
        if not seq:
            return float(self.cfg.dedup_s)
        # n = 已推送条数。第 1 条之后的**第一次重推**用 seq[0]，
        # 也就是说间隔序列是按「第几条重推」排的，不是按「第几条推送」。
        return float(seq[min(max(n - 1, 0), len(seq) - 1)])

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
        ls = self.store.data.get("last_seen") or {}
        ts_seen = float(ls.get("ts") or 0) > 0
        https_seen = float(ls.get("https") or 0) > 0
        msg = "\n".join([
            "【A7Z 自检·恢复】%s" % FAULT_TITLE.get(fault, fault),
            "· Tailscale：%s" % (_human_age(age_ts) if ts_seen else "未启用"),
            "· HTTPS：%s" % (_human_age(age_https) if https_seen else "未启用"),
            "心跳已恢复正常，无需处理。",
        ])
        mode = self.alerter.send_text(msg)
        rec["active"] = False
        rec["last_sent"] = now
        rec["n"] = 0            # 恢复即归零：下一次故障重新从 30min 开始
        self.store.save()
        return [{"type": "recover", "fault": fault, "mode": mode}]

    def _format_fault(self, fault: str, age_ts: float, age_https: float) -> str:
        ls = self.store.data.get("last_seen") or {}
        ts_seen = float(ls.get("ts") or 0) > 0
        https_seen = float(ls.get("https") or 0) > 0
        body = self.store.data.get("last_body") or {}
        ver = str(body.get("version") or "未知")

        if fault == FAULT_TS:
            return "\n".join([
                "【A7Z 自检】Tailscale 通道故障（设备本体在线）",
                "· Tailscale：%s ← 这条断了" % _human_age(age_ts),
                "· HTTPS：%s ← 仍在收心跳" % (_human_age(age_https) if https_seen else "未启用"),
                "",
                "结论：设备活着，只是走 Tailscale 的那条路不通。",
                "建议检查：tailnet 状态 / A7Z 的 Tailscale key 是否过期",
            ])

        ages = []
        if ts_seen:
            ages.append(age_ts)
        if https_seen:
            ages.append(age_https)
        v = body.get("vehicle") if isinstance(body.get("vehicle"), dict) else {}
        was_driving = str(v.get("state") or "").strip().lower() == "driving"
        lines = [
            "【A7Z 自检】设备失联",
            "最后心跳：%s（%s）" % (_human_age(min(ages)) if ages else "从未收到", ver),
            "· Tailscale：%s" % (_human_age(age_ts) if ts_seen else "未启用"),
            "· HTTPS：%s" % (_human_age(age_https) if https_seen else "未启用"),
            "· 最后车态：%s" % _vehicle_line(v),
            "",
            "可能原因：断电 / 系统崩溃 / 网络中断（云端无法区分）",
            # 行驶中失联 ≠ 停车后失联：前者最常见的是「离开家宽/车载热点后没网」，
            # 让用户去「确认车还停着」等于答非所问（2026-11-09 现场就是这种）。
            ("建议：车最后一次上报是在行驶中。若车已停妥，先看设备指示灯，"
             "再确认它当时连的热点（车载 MIFI / 家宽）是否可用"
             if was_driving else
             "建议：确认车还停着，再检查设备电源与网络指示灯"),
        ]
        return "\n".join(lines)

    # ── 状态页用 ────────────────────────────────────────────
    def classify(self, now: float = None) -> str:
        now = self._now() if now is None else now
        data = self.store.data
        if now < float(data.get("offline_until", 0.0)):
            return "expected_offline"
        online_expected, why = _classify_vehicle(data.get("last_body"))
        if online_expected is False and self._could_be_silent(why, now):
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
