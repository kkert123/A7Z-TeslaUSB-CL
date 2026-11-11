#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锁车「停车自检」与三层车态数据源测试（v0.3.1.81 / M133）

背景：用户报「下车锁车后收不到自检」，查下来是两个问题叠加 ——
  ① `_vehicle_snapshot` 一直在读 TeslaMate `/states`，而那个端点返回的是
     **服务配置状态**（auto_del / buttons / schedule …20 键），车辆字段在
     `/msg` 的 raw_data 里（扁平 95 键）→ locked/state 恒为 null；
  ② 代码里根本没有「锁车」这个触发点，只有档位跃迁和每日摘要。

本测试钉死三件事：
  A. 扁平 raw_data 能正确解析（含 "nil" 字符串三态）
  B. 三层兜底按预期降级（SentryClips 新文件 ≈ 已锁车；RecentClips 在写 ≈ 在动）
  C. 锁车边沿只推一次，且被冷却/档位/车速/开关正确拦住

跑法：python _test_guard_lock_report.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

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


def eq(name, got, want):
    ok(name, got == want, "got=%r want=%r" % (got, want))


def mk_clip(dir_path, ts, suffix="front"):
    """按 Tesla 命名造一个空 mp4：2026-11-09_09-37-02-front.mp4"""
    os.makedirs(dir_path, exist_ok=True)
    name = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(ts)) + "-%s.mp4" % suffix
    p = os.path.join(dir_path, name)
    with open(p, "wb") as f:
        f.write(b"\x00" * 32)
    return p


def make_sender(gc):
    """不启动线程的 GuardClient：只测判定逻辑，_push_report 打桩收集文本。"""
    s = gc.GuardSender.__new__(gc.GuardSender)
    s.cfg = dict(gc.DEFAULTS)
    s._prev_locked = None
    s._locked_primed = False
    # 冷却初值必须是 None（=从未报过）。0.0 在 monotonic（开机秒数）下
    # 会让「开机不足 gap 秒」被误判成冷却中（审查 P0）
    s._lock_report_at = None
    s._lock_reported = False
    s._lock_pending_report = False
    s._offline_at = {}
    # _maybe_report 的依赖字段：留空才能在不启动线程的情况下测接线（审查 P1-2）
    s._warmup_until = 0.0
    s._last_result = ""
    s._daily_done = "1970-01-01"
    s._pending_reports = []
    s._report_at = {}
    s._seq = 0
    s._pending_offline = None
    s._pending_tries = 0
    s._pending_last = 0.0
    s._report_ok = 0
    s._report_fail = 0
    s._b_down_notified = False
    s._ping_kind = ""
    s._tx_ok = 0
    s._tx_fail = 0
    s._queue_count = 0
    s._device_line = lambda: "设备：A7Z · v0.3.1.81"
    s._fmt_items = lambda snap: "分项：网络 正常｜USB链路 正常"
    s.pushed = []
    s._push_report = lambda text, critical=False: s.pushed.append(text)
    return s


def main():
    tmp = tempfile.mkdtemp(prefix="lockrep_")
    import guard_client as gc

    saved_dirs = dict(gc._TESLA_CLIP_DIRS)
    saved_vehicle = dict(gc._VEHICLE_CACHE)
    try:
        sen_dir = os.path.join(tmp, "SentryClips")
        rec_dir = os.path.join(tmp, "RecentClips")
        sav_dir = os.path.join(tmp, "SavedClips")
        gc._TESLA_CLIP_DIRS = {"SentryClips": sen_dir, "SavedClips": sav_dir,
                               "RecentClips": rec_dir}

        def reset_clips():
            gc._CLIPS_CACHE.update(at=0.0, data=None)

        def reset_vehicle(data=None, at_offset=0.0):
            gc._VEHICLE_CACHE.update(
                at=(time.monotonic() - at_offset) if data is not None else 0.0,
                data=data, err=None)

        now = time.time()

        # ══ A. 扁平 raw_data 解析 ══
        reset_vehicle({
            "locked": True, "sentry_mode": True, "is_user_present": False,
            "state": "online", "shift_state": "P", "speed": 0.0,
            "ui_park_time_str": "00:24:42",
        })
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("A1 locked", v["locked"], True)
        eq("A2 sentry_mode", v["sentry_mode"], True)
        eq("A3 user_present", v["user_present"], False)
        eq("A4 state", v["state"], "online")
        eq("A5 shift_state", v["shift_state"], "P")
        eq("A6 speed", v["speed"], 0.0)
        eq("A7 park_time", v["park_time"], "00:24:42")
        eq("A8 source", v["source"], "teslamate_msg")
        eq("A9 detect", v["detect"], "teslamate")

        # 三态归一：服务端会把 nil 序列化成字符串 "nil"（M62）
        reset_vehicle({"locked": "nil", "state": "asleep"})
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("A10 \"nil\" → None", v["locked"], None)
        reset_vehicle({"locked": "true"})
        eq("A11 \"true\" → True",
           gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))["locked"], True)

        # 空字典不能当成「全 null 的合法车态」，要降级
        reset_vehicle({})
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("A12 空 raw_data → locked None", v["locked"], None)
        eq("A13 空 raw_data → detect none", v["detect"], "none")

        # ══ B. 三层兜底 ══
        # B1：SentryClips 刚写入 → 已锁车（哨兵只在锁车后启用）
        shutil.rmtree(sen_dir, ignore_errors=True)
        shutil.rmtree(rec_dir, ignore_errors=True)
        mk_clip(sen_dir, now - 30)
        mk_clip(rec_dir, now - 3600)
        reset_clips()
        h = gc._clips_hint()
        eq("B1 SentryClips 新鲜 → locked", h.get("locked"), True)
        eq("B2 判定原因", h.get("why"), "sentry_clip_written")

        # B3：RecentClips 正在写 → 在动
        mk_clip(rec_dir, now - 20)
        reset_clips()
        h = gc._clips_hint()
        eq("B3 RecentClips 新鲜 → locked", h.get("locked"), False)
        eq("B4 判定原因", h.get("why"), "recent_clip_writing")

        # B5：两边都旧 → 未知（不能猜）
        shutil.rmtree(sen_dir, ignore_errors=True)
        shutil.rmtree(rec_dir, ignore_errors=True)
        mk_clip(sen_dir, now - 86400)
        mk_clip(rec_dir, now - 86400)
        reset_clips()
        eq("B5 都旧 → 未知", gc._clips_hint().get("locked"), None)

        # B6：L1 缺失时自动走 L3
        reset_vehicle(None)
        shutil.rmtree(sen_dir, ignore_errors=True)
        mk_clip(sen_dir, now - 30)
        mk_clip(rec_dir, now - 7200)
        reset_clips()
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=True))
        eq("B6 L1 缺失 → 用 L3 判 locked", v["locked"], True)
        eq("B7 detect=clips", v["detect"], "clips")

        # B8：关掉兜底就只剩 L1
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("B8 关闭兜底 → 不再猜", v["locked"], None)

        # ══ C. 锁车报告 ══
        def snap(locked=True, gear="P", speed=0.0):
            return {
                "selfcheck": {"result": "pass",
                              "items": {"net": "pass", "gadget": "pass",
                                        "thumb": "pass", "wechat": "pass"}},
                "vehicle": {"locked": locked, "shift_state": gear,
                            "speed": speed, "detect": "teslamate"},
            }

        s = make_sender(gc)
        ok("C1 首拍只建基准，不补推", s._maybe_lock_report(snap(True)) is False)
        eq("C1 首拍未推送", len(s.pushed), 0)

        s = make_sender(gc)
        s._maybe_lock_report(snap(False))                 # 基准：未锁
        ok("C2 未锁→已锁 触发", s._maybe_lock_report(snap(True)) is True)
        eq("C3 推送条数", len(s.pushed), 1)
        ok("C4 文案含停车自检且标明来源 A7Z",
           "【A7Z】停车自检" in s.pushed[0] and "已锁" in s.pushed[0],
           s.pushed[0][:60])
        ok("C5 推送后仍保持 locked 基准", s._prev_locked is True)
        ok("C6 已锁→已锁 不重复", s._maybe_lock_report(snap(True)) is False)
        eq("C7 仍只有 1 条", len(s.pushed), 1)

        # C8：冷却期内再次 未锁→已锁 不推
        s._maybe_lock_report(snap(False))
        ok("C8 冷却内不推", s._maybe_lock_report(snap(True)) is False)
        eq("C9 仍只有 1 条", len(s.pushed), 1)

        # C10：冷却过了再锁 → 推
        s._lock_report_at = time.monotonic() - 9999
        s._maybe_lock_report(snap(False))
        ok("C10 冷却过后可再推", s._maybe_lock_report(snap(True)) is True)
        eq("C11 共 2 条", len(s.pushed), 2)

        # C12：还在 D 档（等红灯挂 P→D 抖动）不推
        s = make_sender(gc)
        s._maybe_lock_report(snap(False, gear="D"))
        ok("C12 D 档不推", s._maybe_lock_report(snap(True, gear="D")) is False)

        # C13：有车速不推
        s = make_sender(gc)
        s._maybe_lock_report(snap(False, speed=10.0))
        ok("C13 有车速不推", s._maybe_lock_report(snap(True, speed=10.0)) is False)

        # C14：开关关掉就完全不推
        s = make_sender(gc)
        s.cfg["report_on_lock"] = False
        s._maybe_lock_report(snap(False))
        ok("C14 关闭开关不推", s._maybe_lock_report(snap(True)) is False)
        eq("C15 无推送", len(s.pushed), 0)

        # C16：锁车态未知（None→True，例如 L3 刚判定出来）也要推
        s = make_sender(gc)
        s._maybe_lock_report(snap(None))
        ok("C16 None→True 触发", s._maybe_lock_report(snap(True)) is True)

        # C17：文案标注数据来源
        s = make_sender(gc)
        sn = snap(True)
        sn["vehicle"]["detect"] = "clips"
        s._maybe_lock_report(snap(False))
        s._maybe_lock_report(sn)
        ok("C17 标注来源", "据记录仪" in s.pushed[0], s.pushed[0][:50])

        # C18：**已报过之后** True→None→True（车休眠 /msg 返回 nil）不能重复推。
        # 注意 v0.3.1.82 起「首拍时车已锁」不再视为已报（那是被推翻的旧语义，
        # 它会把重启前的锁车事件永久吞掉），所以这里先走一次正常的未锁→已锁边沿。
        s = make_sender(gc)
        s._maybe_lock_report(snap(False))         # 基准：未锁
        s._maybe_lock_report(snap(True))          # 正常边沿 → 推 1 条
        s._lock_report_at = time.monotonic() - 9999
        s._maybe_lock_report(snap(None))
        ok("C18 已报后再抖动不重复", s._maybe_lock_report(snap(True)) is False)
        eq("C19 仍只有 1 条", len(s.pushed), 1)

        # C20：真实解锁后重新上膛，能再推一次
        s._lock_report_at = time.monotonic() - 9999
        s._maybe_lock_report(snap(False))
        ok("C20 解锁后重新上膛", s._maybe_lock_report(snap(True)) is True)

        # ══ D. 事件子目录结构（真机 SentryClips 就是这种）══
        shutil.rmtree(sen_dir, ignore_errors=True)
        ev = os.path.join(sen_dir, time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now - 120)))
        os.makedirs(ev, exist_ok=True)
        with open(os.path.join(ev, os.path.basename(ev) + "-front.mp4"), "wb") as f:
            f.write(b"\x00" * 32)
        got = gc._latest_clip(sen_dir)
        ok("D1 哨兵事件子目录能取到 mp4", got is not None and got["age_s"] <= 200,
           str(got))
        reset_clips()
        shutil.rmtree(rec_dir, ignore_errors=True)
        mk_clip(rec_dir, now - 7200)
        eq("D2 子目录结构 → 判为已锁车", gc._clips_hint().get("locked"), True)

        # ══ E. 握手不能采信 clips 的推断值 ══
        s = make_sender(gc)
        sent = []
        s._pending_offline = None
        s._pending_tries = 0
        s._pending_last = 0.0
        s._send = lambda body, channel=None: (sent.append(body), (True, ""))[1]
        s._prev_vehicle = {"locked": True, "state": "online", "user_present": False}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                           "user_present": False, "detect": "clips"}})
        eq("E1 clips 推断的未锁 → 不发解锁握手", len(sent), 0)
        s._prev_vehicle = {"locked": True, "state": "online", "user_present": False}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                           "user_present": False, "detect": "teslamate"}})
        eq("E2 teslamate 的未锁 → 正常握手", len(sent), 1)

        # ══ F. 车态矛盾：哨兵开着就一定是已锁（M134）══
        # 实测：车已锁定 + 哨兵开启，TeslaMate 却返回 locked="false"
        reset_vehicle({"locked": "false", "sentry_mode": "true", "state": "online",
                       "shift_state": "P", "speed": "0"})
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("F1 locked=false + 哨兵=true → 按已锁", v["locked"], True)
        eq("F2 标注矛盾来源", v.get("locked_note"), "sentry_conflict")

        reset_vehicle({"locked": "false", "sentry_mode": "false", "state": "online"})
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("F3 哨兵也关 → 保持未锁", v["locked"], False)
        eq("F4 无矛盾标记", v.get("locked_note"), None)

        reset_vehicle({"locked": "true", "sentry_mode": "true", "state": "online"})
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("F5 两者一致 → 已锁", v["locked"], True)

        # ══ G. 重启时车已锁 → 预热后补报一条（用户要求「每次重启都补」）══
        def snapv(locked=True, gear="P", speed=0.0):
            return {
                "selfcheck": {"result": "pass",
                              "items": {"net": "pass", "gadget": "pass",
                                        "thumb": "pass", "wechat": "pass"}},
                "vehicle": {"locked": locked, "shift_state": gear,
                            "speed": speed, "detect": "teslamate"},
            }

        s = make_sender(gc)
        ok("G1 首拍车已锁：不立即推", s._maybe_lock_report(snapv(True)) is False)
        eq("G2 但标记挂起", s._lock_pending_report, True)

        ok("G3 预热期内仍不推",
           s._maybe_lock_report(snapv(True), dry_run=True) is False)
        eq("G4 仍只有 0 条", len(s.pushed), 0)

        ok("G5 预热结束后补推", s._maybe_lock_report(snapv(True)) is True)
        eq("G6 共 1 条", len(s.pushed), 1)
        ok("G7 文案标注重启后补报", "补报" in s.pushed[0], s.pushed[0][:50])
        ok("G8 补报后不重复", s._maybe_lock_report(snapv(True)) is False)

        # G9：首拍已锁但随后解锁 → 补报作废（事件已过期）
        s = make_sender(gc)
        s._maybe_lock_report(snapv(True))
        s._maybe_lock_report(snapv(False))
        eq("G9 解锁后清挂起", s._lock_pending_report, False)
        eq("G10 无推送", len(s.pushed), 0)

        # G11：首拍未锁 → 不挂起
        s = make_sender(gc)
        s._maybe_lock_report(snapv(False))
        eq("G11 首拍未锁不挂起", s._lock_pending_report, False)

        # ══ H. 握手：哨兵开着不能判解锁 + 同类冷却（M134）══
        sent[:] = []
        s = make_sender(gc)
        s._send = lambda body, channel=None: (sent.append(body), (True, ""))[1]
        s._prev_vehicle = {"locked": True, "state": "online",
                           "user_present": False, "sentry_mode": True}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                           "user_present": False,
                                           "sentry_mode": True,
                                           "detect": "teslamate"}})
        eq("H1 哨兵仍开启 → 不判解锁", len(sent), 0)

        s._prev_vehicle = {"locked": True, "state": "online",
                           "user_present": False, "sentry_mode": True}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                           "user_present": False,
                                           "sentry_mode": False,
                                           "detect": "teslamate"}})
        eq("H2 哨兵关闭 + 未锁 → 真解锁握手", len(sent), 1)

        s._prev_vehicle = {"locked": True, "state": "online",
                           "user_present": False, "sentry_mode": False}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                           "user_present": False,
                                           "sentry_mode": False,
                                           "detect": "teslamate"}})
        eq("H3 30 分钟内同类跃迁 → 冷却", len(sent), 1)

        s._offline_at["unlocked"] = time.monotonic() - 9999
        s._prev_vehicle = {"locked": True, "state": "online",
                           "user_present": False, "sentry_mode": False}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                           "user_present": False,
                                           "sentry_mode": False,
                                           "detect": "teslamate"}})
        eq("H4 冷却过后可再握手", len(sent), 2)

        # ══ I. _maybe_report 接线：预热期不推、预热后补推（审查 P1-2）══
        s = make_sender(gc)
        s.cfg["report_daily_at"] = ""
        s._maybe_lock_report(snapv(True))          # 首拍：挂起
        s._warmup_until = time.monotonic() + 999
        s._maybe_report(snapv(True))
        eq("I1 预热期内 _maybe_report 不推", len(s.pushed), 0)
        eq("I2 预热期挂起仍在", s._lock_pending_report, True)
        s._warmup_until = time.monotonic() - 1
        s._maybe_report(snapv(True))
        eq("I3 预热结束后补推 1 条", len(s.pushed), 1)
        ok("I4 标注重启后补报", "补报" in s.pushed[0], s.pushed[0][:60])

        # ══ J. 冷却基准不能是 0.0（monotonic 是开机秒数，审查 P0）══
        sent[:] = []
        s = make_sender(gc)
        s._send = lambda body, channel=None: (sent.append(body), (True, ""))[1]
        s._offline_at = {}
        s._prev_vehicle = {"locked": True, "state": "online",
                           "user_present": False, "sentry_mode": False}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                           "user_present": False,
                                           "sentry_mode": False,
                                           "detect": "teslamate"}})
        eq("J1 从未握手时首次必发（不受开机秒数影响）", len(sent), 1)
        eq("J2 送达后才记录冷却", "unlocked" in s._offline_at, True)

        # J3：发送失败不占用冷却
        sent[:] = []
        s2 = make_sender(gc)
        s2._send = lambda body, channel=None: (False, "timeout")
        s2._prev_vehicle = {"locked": True, "state": "online",
                            "user_present": False, "sentry_mode": False}
        s2._maybe_going_offline({"vehicle": {"locked": False, "state": "online",
                                            "user_present": False,
                                            "sentry_mode": False,
                                            "detect": "teslamate"}})
        eq("J3 发送失败不占冷却", "unlocked" in s2._offline_at, False)

        # ══ K. driving 是真实可重复事件，不冷却（审查 P1-2）══
        sent[:] = []
        s = make_sender(gc)
        s._send = lambda body, channel=None: (sent.append(body), (True, ""))[1]
        s._offline_at = {"driving": time.monotonic()}      # 刚出过一趟车
        s._prev_vehicle = {"locked": False, "state": "asleep",
                           "user_present": False, "sentry_mode": False}
        s._maybe_going_offline({"vehicle": {"locked": False, "state": "driving",
                                           "user_present": False,
                                           "sentry_mode": False,
                                           "detect": "teslamate"}})
        eq("K1 driving 不冷却（第二趟仍握手）", len(sent), 1)

        # ══ L. 关闭开关：挂起作废且不补推（审查 P1-1）══
        s = make_sender(gc)
        s._maybe_lock_report(snapv(True))                  # 首拍挂起
        s.cfg["report_on_lock"] = False
        s._maybe_lock_report(snapv(True))
        eq("L1 关开关后挂起作废", s._lock_pending_report, False)
        s.cfg["report_on_lock"] = True
        ok("L2 重开不补推旧事件", s._maybe_lock_report(snapv(True)) is False)
        eq("L3 无推送", len(s.pushed), 0)

        # ══ M. 挂起期间车辆在动 → 作废（审查 P2-4）══
        s = make_sender(gc)
        s._maybe_lock_report(snapv(True))
        s._maybe_lock_report(snapv(True, gear="D"))
        eq("M1 挂 D 档后挂起作废", s._lock_pending_report, False)

        # ══ N. speed 是字符串（实测 /msg 返回 '0'）══
        reset_vehicle({"locked": "true", "speed": "0", "state": "online",
                       "shift_state": "P", "sentry_mode": "false"})
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("N1 speed='0' → 0.0", v["speed"], 0.0)
        reset_vehicle({"locked": "true", "speed": "", "state": "online"})
        eq("N2 speed='' → None",
           gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))["speed"], None)

        # ══ O. 行驶中不做哨兵归一（审查 P1-4）══
        reset_vehicle({"locked": "false", "sentry_mode": "true", "state": "driving",
                       "shift_state": "D", "speed": "30"})
        v = gc._vehicle_snapshot(dict(gc.DEFAULTS, lock_detect_fallback=False))
        eq("O1 行驶中不归一（保持未锁）", v["locked"], False)
        eq("O2 无矛盾标记", v.get("locked_note"), None)

        # ══ P. 闩锁复位：车开走必须松开，否则下一次锁车被吞 ══
        # 2026-11-10 现场：14:06 推过之后开门上车开走，TeslaMate 的 locked 一路仍报
        # true → 旧实现只在 `locked is False` 时复位 → 闩锁卡死 → 16:31 / 19:21
        # 两次真实锁车被当成「这一把已报过」直接 return。这一组就是钉这个。
        def snap_mv(locked=True, gear="P", speed=0.0, state="online"):
            d = snap(locked, gear, speed)
            d["vehicle"]["state"] = state
            return d

        s = make_sender(gc)
        s._maybe_lock_report(snap_mv(False))                       # 首拍建基准
        ok("P1 锁车推一次", s._maybe_lock_report(snap_mv(True)) is True)
        eq("P2 条数", len(s.pushed), 1)

        # TeslaMate 的 locked 在开走后仍报 true —— 这正是现场形态
        ok("P3 车开走（locked 仍 true + 43km/h）不推",
           s._maybe_lock_report(snap_mv(True, gear="D", speed=43.0, state="driving")) is False)
        eq("P3b 但闩锁必须已松开", s._lock_reported, False)

        s._lock_report_at -= 200                                   # 模拟 200 秒后回来
        ok("P4 回来后锁车要再推一条", s._maybe_lock_report(snap_mv(True)) is True)
        eq("P5 共 2 条", len(s.pushed), 2)
        ok("P6 同一把锁重复上报不重复", s._maybe_lock_report(snap_mv(True)) is False)
        eq("P7 仍 2 条", len(s.pushed), 2)

        # state=driving 单独也算在动（gear/speed 在 TeslaMate 里可能滞后）
        s2 = make_sender(gc)
        s2._maybe_lock_report(snap_mv(False))
        s2._maybe_lock_report(snap_mv(True))
        s2._maybe_lock_report(snap_mv(True, "0.0", 0.0, "driving"))   # 档位给了个无效值，只剩 state 可用
        eq("P8 state=driving 也松闩锁", s2._lock_reported, False)

        # 只有车速（档位未知）同样要松
        s3 = make_sender(gc)
        s3._maybe_lock_report(snap_mv(False))
        s3._maybe_lock_report(snap_mv(True))
        s3._maybe_lock_report(snap_mv(True, None, 12.0))
        eq("P9 只靠车速也能松闩锁", s3._lock_reported, False)

        # ══ Q. 采样节奏自适应 ══
        s4 = make_sender(gc)
        s4.cfg = dict(gc.DEFAULTS, states_interval_s=300, states_fast_s=60)
        s4._lock_reported = False
        eq("Q1 未确认锁好 → 快档 60s", s4._states_interval(), 60)
        s4._lock_reported = True
        eq("Q2 已锁已推 → 慢档 300s", s4._states_interval(), 300)
        s4.cfg = dict(s4.cfg, states_fast_s=600)
        eq("Q3 fast>slow 退化慢档（配错不能把采样打快）", s4._states_interval(), 300)
        s4.cfg = dict(s4.cfg, states_fast_s=0)
        eq("Q4 fast=0 退化慢档", s4._states_interval(), 300)
        eq("Q5 默认配置下快档是 60s", gc.DEFAULTS["states_fast_s"], 60)

        # ══ R. 冷却从 600s 放宽到 120s（短途二次锁车不再漏） ══
        eq("R0 默认冷却已改 120s", gc.DEFAULTS["lock_report_min_gap_s"], 120)
        s5 = make_sender(gc)
        s5._maybe_lock_report(snap_mv(False))
        s5._maybe_lock_report(snap_mv(True))
        eq("R1 首次推送", len(s5.pushed), 1)
        s5._maybe_lock_report(snap_mv(True, "D", 20.0))            # 开走
        s5._lock_report_at -= 130
        ok("R2 130 秒后二次锁车要推", s5._maybe_lock_report(snap_mv(True)) is True)
        eq("R3 共 2 条", len(s5.pushed), 2)

        s6 = make_sender(gc)
        s6._maybe_lock_report(snap_mv(False))
        s6._maybe_lock_report(snap_mv(True))
        s6._maybe_lock_report(snap_mv(True, "D", 20.0))
        s6._lock_report_at -= 50
        ok("R4 50 秒内二次锁车仍防抖（不连着发）",
           s6._maybe_lock_report(snap_mv(True)) is False)
        eq("R4b 防抖期间不丢闩锁（下拍还能推）", s6._lock_reported, False)

    finally:
        gc._TESLA_CLIP_DIRS = saved_dirs
        gc._VEHICLE_CACHE.update(saved_vehicle)
        gc._CLIPS_CACHE.update(at=0.0, data=None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILED:
        print("失败项: %s" % ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
