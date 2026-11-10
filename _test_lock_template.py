#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""停车自检推送模板测试（v0.3.1.84，样式 2：结论置顶 + 异常置顶 + 分项对齐）

模板是用户唯一会读的东西，所以它必须满足三件事：
  ① 全绿时能扫读（每项一行、对齐、带证据）；
  ② 有异常时**第一屏就看到问题**（异常项置顶 + ⚠ + 建议），正常项压成一行；
  ③ 没有的数据不要编（没电量就不出现「电量」）。

跑法：python _test_lock_template.py
"""
import os
import sys

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


def sender():
    s = gc.GuardSender.__new__(gc.GuardSender)
    s.cfg = dict(gc.DEFAULTS)
    s._device_line = lambda: "v0.3.1.84 → 100.111.252.121:8899"
    return s


def snap(items, result="pass", battery=67, locked=True, park="00:24:42"):
    return {
        "vehicle": {"locked": locked, "detect": "teslamate", "shift_state": "P",
                    "park_time": park, "battery": battery,
                    "locked_note": "sentry_conflict"},
        "selfcheck": {"result": result, "items": items},
        "net": {"wifi_ssid": "C12345", "internet_ok": True, "tailscale": "Running"},
        "gadget": {"mode": "present", "udc_connected": True, "age_s": 2.9},
        "thumb": {"service": True, "queue": 0, "last_scan_age_s": 1},
        "wechat": {},
    }


ALL = {"net": "pass", "gadget": "pass", "thumb": "pass", "wechat": "pass"}


def main():
    s = sender()

    # ── 全绿 ──
    txt = s._fmt_locked(snap(ALL))
    lines = txt.split("\n")
    ok("A1 标题带 ✅ 与档位", lines[0].startswith("【停车自检】✅ 正常 · 车辆已锁"), lines[0])
    ok("A2 车辆行含档位/停车/电量",
       "P 档" in lines[1] and "停车 00:24" in lines[1] and "电量 67%" in lines[1], lines[1])
    ok("A3 停车时长去掉了秒", "00:24:42" not in txt)
    ok("A4 四项各一行", sum(1 for l in lines if l.startswith(("网络", "USB链路", "缩略图", "企微推送"))) == 4, lines)
    ok("A5 每行都带证据（括号）", all("（" in l for l in lines[2:6]), lines[2:6])
    ok("A6 全绿不追加建议行", "建议：" not in txt)
    ok("A7 末行是设备+时间", lines[-1].startswith("v0.3.1.84 → ") and " · " in lines[-1], lines[-1])
    ok("A8 对齐：分项标签后至少一个空格",
       all(l[0] != "" for l in lines[2:6]) and all(" " in l[:9] for l in lines[2:6]), lines[2:6])

    # ── 有异常 ──
    bad_items = dict(ALL, thumb="warn")
    sn = snap(bad_items, result="warn")
    sn["thumb"] = {"service": True, "queue": 128, "last_scan_age_s": 480}
    txt = s._fmt_locked(sn)
    lines = txt.split("\n")
    ok("B1 标题带 ⚠️ 与「注意」", lines[0].startswith("【停车自检】⚠️ 注意"), lines[0])
    idx_bad = next(i for i, l in enumerate(lines) if "缩略图" in l)
    idx_net = next(i for i, l in enumerate(lines) if "网络" in l)
    ok("B2 异常项排在正常项之前", idx_bad < idx_net, (idx_bad, idx_net))
    ok("B3 异常行带 ⚠ 与积压证据", lines[idx_bad].startswith("⚠") and "积压128" in lines[idx_bad], lines[idx_bad])
    ok("B4 正常项合并成一行「其余」", any(l.startswith("其余") and "正常" in l for l in lines), lines)
    ok("B5 附建议", any(l.startswith("建议：") and "预览积压" in l for l in lines), lines)

    # ── 边界 ──
    t2 = s._fmt_locked(snap(ALL, battery=None))
    ok("C1 没有电量就不出现「电量」", "电量" not in t2, t2.split("\n")[1])

    t3 = s._fmt_locked(snap(ALL, locked=False))
    ok("C2 未锁时标题说明锁车状态未知", "锁车状态未知" in t3.split("\n")[0], t3.split("\n")[0])

    t4 = s._fmt_locked(snap(ALL), restarted=True)
    ok("C3 重启补报标记", "重启后补报" in t4.split("\n")[0], t4.split("\n")[0])

    # pass 态没有成功记录 = "无待发消息"，不能说"最近无成功记录"（自相矛盾）
    ok("C4 pass 的企微项不说「最近无成功记录」",
       "最近无成功记录" not in s._fmt_locked(snap(ALL)) and "无待发消息" in s._fmt_locked(snap(ALL)))
    # 真的 warn 了就要如实说（否则等于把异常藏起来）
    t5 = s._fmt_locked(snap({"net": "pass", "gadget": "pass", "thumb": "pass",
                             "wechat": "warn"}, result="warn"))
    ok("C5 warn 的企微项如实报「最近无成功记录」", "最近无成功记录" in t5, t5)

    # ── 对齐宽度工具 ──
    ok("D1 中文按 2 列算", gc._disp_width("网络") == 4, gc._disp_width("网络"))
    ok("D2 USB 按 3 列算", gc._disp_width("USB") == 3, gc._disp_width("USB"))
    ok("D3 pad 至少留一个空格", gc._pad("企微推送") == "企微推送 ", repr(gc._pad("企微推送")))

    print("\n%d/%d passed" % (PASS, PASS + FAIL))
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
