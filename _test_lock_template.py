#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""停车自检推送模板测试（v0.3.1.85，样式 2 窄排版）

模板是用户唯一会读的东西，所以它必须满足四件事：
  ① **每行不超过 `_LINE_MAX`（22 显示列）**——企微手机端一行约 26~28 列就折行，
     2026-11-10 现场一份 8 行的报告被折成 14 行碎块（这是本轮改版的直接动因）；
  ② 全绿时能扫读（每项一行、标签对齐、带短证据）；
  ③ 有异常时**第一屏就看到问题**（异常项置顶 + ⚠ + 建议），正常项压成一行；
  ④ 没有的数据不要编（没电量就不出现「电量」）。

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
    return s


def snap(items, result="pass", battery=67, locked=True, park="00:24:42",
         ssid="C12345", note="sentry_conflict", shift="P"):
    return {
        "vehicle": {"locked": locked, "detect": "teslamate", "shift_state": shift,
                    "park_time": park, "battery": battery, "locked_note": note},
        "selfcheck": {"result": result, "items": items},
        "net": {"wifi_ssid": ssid, "internet_ok": True, "tailscale": "Running"},
        "gadget": {"mode": "present", "udc_connected": True, "age_s": 2.9},
        "thumb": {"service": True, "queue": 0, "last_scan_age_s": 1},
        "wechat": {"last_ok_age_s": 3600},
    }


ALL = {"net": "pass", "gadget": "pass", "thumb": "pass", "wechat": "pass"}


def widths(txt):
    return [(gc._disp_width(l), l) for l in txt.split("\n")]


def main():
    s = sender()

    # ── A. 全绿结构 ────────────────────────────────────────
    txt = s._fmt_locked(snap(ALL))
    lines = txt.split("\n")
    ok("A1 标题只有「图标 + 档位」（不加「· 车辆已锁」）",
       lines[0] == "【停车自检】✅ 正常", lines[0])
    ok("A2 第二行是锁车态 + 推断来源", lines[1].startswith("已锁") and "据哨兵" in lines[1], lines[1])
    ok("A3 第三行是停车时长 + 电量",
       "停车 00:24" in lines[2] and "电量 67%" in lines[2], lines[2])
    ok("A4 停车时长去掉了秒", "00:24:42" not in txt)
    ok("A5 P 档不占位（停车常态、无信息量）", "P 档" not in txt, txt)
    ok("A6 四项各一行", sum(1 for l in lines if l.startswith(("网络", "USB", "缩略图", "推送"))) == 4, lines)
    ok("A7 每项都带短证据（不含括号——括号是长行主因）",
       not any("（" in l for l in lines[3:7]), lines[3:7])
    ok("A8 全绿不追加建议行", "建议：" not in txt)
    ok("A9 末行是版本 + 时间（不再带僚机地址）",
       lines[-1].startswith("v") and " · " in lines[-1] and "100.111" not in lines[-1], lines[-1])
    ok("A10 标签列对齐（「正常」从第 8 列开始，即前面占满 7 列）",
       all(gc._disp_width(l[:l.index("正常")]) == gc._TAG_W for l in lines[3:7]), lines[3:7])

    # ── B. 有异常：异常置顶 + 其余合并 + 建议 ────────────────
    sn = snap(dict(ALL, thumb="warn"), result="warn")
    sn["thumb"] = {"service": True, "queue": 128, "last_scan_age_s": 480}
    txt = s._fmt_locked(sn)
    lines = txt.split("\n")
    ok("B1 标题带 ⚠️ 与「注意」", lines[0] == "【停车自检】⚠️ 注意", lines[0])
    idx_bad = next(i for i, l in enumerate(lines) if "缩略图" in l)
    ok("B2 异常项置顶（紧跟车辆信息之后）", idx_bad == 3, (idx_bad, lines))
    ok("B3 异常行带 ⚠ 与积压+时长证据",
       lines[idx_bad].startswith("⚠") and "积压128" in lines[idx_bad] and "8分" in lines[idx_bad],
       lines[idx_bad])
    ok("B4 正常项合并成「其余 N 项正常」", "其余 3 项正常" in lines, lines)
    ok("B5 附建议", any(l.startswith("建议：") and "清理" in l for l in lines), lines)
    ok("B6 异常时不再逐项列出正常项", not any(l.startswith("网络") for l in lines), lines)

    # fail 用 ❗ 而不是 ⚠（一眼区分「注意」和「异常」）
    sf = snap(dict(ALL, net="fail"), result="fail")
    sf["net"] = {"wifi_ssid": "C12345", "internet_ok": False}
    tf = s._fmt_locked(sf)
    ok("B7 fail 项用 ❗ 且写明「出网不通」",
       any(l.startswith("❗") and "出网不通" in l for l in tf.split("\n")), tf)

    # ── C. 边界：没有的数据不编 ─────────────────────────────
    t2 = s._fmt_locked(snap(ALL, battery=None))
    ok("C1 没有电量就不出现「电量」", "电量" not in t2, t2)

    t3 = s._fmt_locked(snap(ALL, locked=False))
    ok("C2 未锁时写明「锁车未知」", "锁车未知" in t3.split("\n")[1], t3)

    t4 = s._fmt_locked(snap(ALL), restarted=True)
    ok("C3 重启补报标记并进同一括号（不出现背靠背括号）",
       "补报" in t4.split("\n")[1] and "）（" not in t4, t4.split("\n")[1])

    ok("C4 无档位/停车/电量时不输出空行", len(s._fmt_locked(
        {"vehicle": {"locked": True}, "selfcheck": {"result": "pass", "items": dict(ALL)}}).split("\n"))
       == 7)

    # D 档（非 P）才值得说
    t5 = s._fmt_locked(snap(ALL, shift="D", park="--"))
    ok("C5 非 P 档会显示出来", "D 档" in t5, t5)
    ok("C6 park_time 为 '--' 时不显示「停车 --」", "停车 --" not in t5, t5)

    # 长 SSID 必须被截断，不能让整行超宽
    t6 = s._fmt_locked(snap(ALL, ssid="WiFi-YL-MIFI-000500"))
    net_line = next(l for l in t6.split("\n") if l.startswith("网络"))
    ok("C7 超长 SSID 被截断（不撑破行宽）", "…" in net_line, net_line)

    # 全字段缺失也不许崩、不许超宽
    t7 = s._fmt_locked({"vehicle": {}, "selfcheck": {"result": "unknown"}})
    ok("C8 字段全缺仍能渲染", "【停车自检】" in t7 and len(t7.split("\n")) >= 3, t7)

    # ── D. 工具函数 ────────────────────────────────────────
    ok("D1 中文按 2 列算", gc._disp_width("网络") == 4, gc._disp_width("网络"))
    ok("D2 USB 按 3 列算", gc._disp_width("USB") == 3, gc._disp_width("USB"))
    ok("D3 pad 至少留一个空格", gc._pad("缩略图", 7) == "缩略图 ", repr(gc._pad("缩略图", 7)))
    ok("D4 _clip 按显示宽度截断", gc._disp_width(gc._clip("WiFi-YL-MIFI-000500", 9)) == 9,
       gc._clip("WiFi-YL-MIFI-000500", 9))
    ok("D5 _fit 先丢尾段（丢到放得下为止）",
       gc._fit("甲 · 乙 · 丙丁戊己庚辛壬癸", 12) == "甲 · 乙",
       gc._fit("甲 · 乙 · 丙丁戊己庚辛壬癸", 12))
    ok("D6 _dur_short", (gc._dur_short(21), gc._dur_short(480), gc._dur_short(7200))
       == ("21秒", "8分", "2时"), (gc._dur_short(21), gc._dur_short(480)))

    # ── E. 核心回归：任何场景下每行都不许超宽 ───────────────
    cases = {
        "全绿": s._fmt_locked(snap(ALL)),
        "全绿+补报": s._fmt_locked(snap(ALL), restarted=True),
        "缩略图积压": s._fmt_locked(sn),
        "网络异常+推送失败": tf,
        "长 SSID": t6,
        "字段全缺": t7,
        "非 P 档无电量": t5,
    }
    over = []
    for name, t in cases.items():
        for w, l in widths(t):
            if w > gc._LINE_MAX:
                over.append("%s: %d|%s" % (name, w, l))
    ok("E1 所有场景每行 ≤ %d 列" % gc._LINE_MAX, not over, "\n" + "\n".join(over))

    print("\n%d/%d passed" % (PASS, PASS + FAIL))
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
