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
    s._lock_report_at = 0.0
    s._lock_reported = False
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
        ok("C4 文案含停车自检", "【停车自检】车辆已锁车" in s.pushed[0], s.pushed[0][:40])
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
        ok("C17 标注来源", "据行车记录仪" in s.pushed[0], s.pushed[0][:50])

        # C18：True→None→True（车休眠 /msg 返回 nil）不能重复推
        s = make_sender(gc)
        s._maybe_lock_report(snap(True))          # 首拍建基准（已锁 → 视为已报）
        s._lock_report_at = time.monotonic() - 9999
        s._maybe_lock_report(snap(None))
        ok("C18 True→None→True 不重复", s._maybe_lock_report(snap(True)) is False)
        eq("C19 无推送", len(s.pushed), 0)

        # C20：真实解锁后重新上膛，能再推一次
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
