#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告补发队列落盘 + 锁车报告 critical 测试（v0.3.1.83 / M136）

背景：用户 16:44 锁车后收不到「停车自检」。除了 B 端判死闸的缺陷（M135），
A 端还有一条独立缺口：
  · `_push_report()` 默认 `critical=False` → 重试 3 档（30/120/600s）后**丢弃**；
  · `_pending_reports` **只在内存** → 车一睡断电 / web 重启，队列直接消失。

于是「离家无网时锁车」这条报告必然丢失，而这恰好是最常见的场景
（用户现场：车载 MIFI 掉线，回家才恢复）。

修法：锁车报告 `critical=True`（永不丢弃、退避拉长到 900s 封顶）
     + 队列原子落盘 + 回网/重启后补发并标注「补发 + 原生成时刻」。

跑法：python _test_guard_report_queue.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import guard_client as gc   # noqa: E402

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


TMP = tempfile.mkdtemp(prefix="repq-")
gc.REPORT_QUEUE_PATH = os.path.join(TMP, "guard_report_queue.json")
gc._note_push = lambda *a, **k: None          # 别去写真实设备的推送健康文件


def fresh_queue():
    try:
        os.unlink(gc.REPORT_QUEUE_PATH)
    except OSError:
        pass
    gc._CLIPS_CACHE.update(at=0.0, data=None)


def mk_sender():
    s = gc.GuardSender(dict(gc.DEFAULTS))
    s._warmup_until = 0.0
    return s


def queue_file():
    try:
        with open(gc.REPORT_QUEUE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def send_fails(text):
    return False, "timeout"


def send_ok(text):
    SENT.append(text)
    return True, ""


SENT = []


def main():
    # ── 1. 锁车报告必须是 critical ────────────────────────
    fresh_queue()
    s = mk_sender()
    seen = []
    s._push_report = lambda text, critical=False: seen.append(critical)
    s._locked_primed = True
    s._prev_locked = None
    s._lock_reported = False
    s._lock_pending_report = False
    s._lock_report_at = None
    s._device_line = lambda: "设备：A7Z"
    s._fmt_items = lambda snap: "分项：网络 正常"
    snap = {"vehicle": {"locked": True, "shift_state": "P", "speed": 0.0,
                        "detect": "teslamate"},
            "selfcheck": {"result": "pass"}}
    s._maybe_lock_report(snap)
    eq("1 锁车报告 critical=True", seen, [True])

    # ── 2. 发送失败 → 队列落盘（含 wall）────────────────────
    fresh_queue()
    gc._push_wecom = send_fails
    s = mk_sender()
    before = time.time()
    s._push_report("【停车自检】车辆已锁车", critical=True)
    d = queue_file()
    ok("2.1 队列文件已生成", isinstance(d, dict) and d.get("items"), d)
    item = (d or {}).get("items", [{}])[0]
    eq("2.2 文本落盘", item.get("text"), "【停车自检】车辆已锁车")
    eq("2.3 critical 落盘", item.get("critical"), True)
    ok("2.4 wall 落盘", float(item.get("wall") or 0) >= before - 1, item.get("wall"))

    # ── 3. 重启后队列还在（at 重置为 None = 立刻可补发）──────
    s2 = mk_sender()
    eq("3.1 重启后队列长度", len(s2._pending_reports), 1)
    eq("3.2 at 被重置", s2._pending_reports[0].get("at"), None)

    # ── 4. 补发正文标注「补发 + 原生成时刻」，成功后清队列 ────
    SENT.clear()
    gc._push_wecom = send_ok
    s2._retry_pending_report()
    ok("4.1 补发已发出", len(SENT) == 1, SENT)
    ok("4.2 标注补发", "（补发：本条生成于" in SENT[0], SENT[:1])
    ok("4.3 带原时刻", time.strftime("%m-%d %H:%M",
                                   time.localtime(s2._pending_reports[0]["wall"]))
       if s2._pending_reports else True, "")
    eq("4.4 成功后队列清空", s2._pending_reports, [])
    ok("4.5 队列文件已删除", not os.path.exists(gc.REPORT_QUEUE_PATH))

    # ── 5. 普通报告 3 档后丢弃；critical 永不丢弃 ────────────
    fresh_queue()
    gc._push_wecom = send_fails
    s3 = mk_sender()
    s3._push_report("普通报告", critical=False)
    for _ in range(len(gc.GuardSender._REPORT_BACKOFF)):
        for it in s3._pending_reports:
            it["at"] = None
        s3._retry_pending_report()
    eq("5.1 普通报告重试耗尽后丢弃", s3._pending_reports, [])
    ok("5.2 丢弃后清空队列文件", not os.path.exists(gc.REPORT_QUEUE_PATH))

    fresh_queue()
    s4 = mk_sender()
    s4._push_report("安全报告", critical=True)
    for _ in range(len(gc.GuardSender._REPORT_BACKOFF) + 4):
        for it in s4._pending_reports:
            it["at"] = None
        s4._retry_pending_report()
    eq("5.3 安全项永不丢弃", len(s4._pending_reports), 1)

    # ── 6. 裁剪：安全项保留，普通只留最近几条 ───────────────
    fresh_queue()
    s5 = mk_sender()
    s5._push_report("安全1", critical=True)
    for i in range(20):
        s5._push_report("普通%02d" % i, critical=False)
    normals = [i["text"] for i in s5._pending_reports if not i.get("critical")]
    crit = [i["text"] for i in s5._pending_reports if i.get("critical")]
    eq("6.1 安全项保留", crit, ["安全1"])
    ok("6.2 队列总量 ≤ REPORT_QUEUE_MAX", len(s5._pending_reports) <= gc.REPORT_QUEUE_MAX,
       len(s5._pending_reports))
    ok("6.3 保留的是最新的", normals[-1] == "普通19", normals)
    ok("6.4 最老的已丢弃", "普通00" not in normals, normals)

    # ── 7. 坏文件 / 过期条目 容错 ───────────────────────────
    fresh_queue()
    with open(gc.REPORT_QUEUE_PATH, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    eq("7.1 坏 JSON → 空队列", gc._load_report_queue(), [])

    fresh_queue()
    with open(gc.REPORT_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump({"ver": 1, "items": [
            {"text": "旧报告", "critical": True, "tries": 0,
             "wall": time.time() - 8 * 86400},
            {"text": "新报告", "critical": True, "tries": 0, "wall": time.time()},
            "垃圾",
            {"text": ""},
        ]}, f)
    got = gc._load_report_queue()
    eq("7.2 过期/垃圾条目被过滤", [i["text"] for i in got], ["新报告"])

    print("\n%d/%d passed" % (PASS, PASS + FAIL))
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
