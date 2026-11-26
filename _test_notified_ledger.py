#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.3.1.94 回归测试：已通知账本真 FIFO / 回读校验 / 并发安全

覆盖 2026-11-26「同一事件被反复补发」的四个修复点：
  1. 真 FIFO —— 超限淘汰**最老**的，而不是 set 里随机一条
  2. 上限 20000（不再是 2000 饱和）
  3. mark 后回读校验 + 扩窗重试
  4. 排他锁 —— 多进程同时 mark 不丢更新

本机（Windows）与 A7Z（Linux/py3.9）都能跑：
    python _test_notified_ledger.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from utils.notified_ledger import NotifiedLedger, event_time  # noqa: E402

PASS = 0
FAIL = 0
IS_LINUX = sys.platform.startswith("linux")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))


def new_dir():
    return tempfile.mkdtemp(prefix="ledger-test-")


def write_raw(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
# 子进程 worker（并发测试用；Windows 无 fcntl 时主流程会跳过）
# ══════════════════════════════════════════════════════════════

def worker(path, prefix, count):
    led = NotifiedLedger(path, max_keep=50000)
    for i in range(count):
        led.add("%s_%03d" % (prefix, i), source="worker")
    return 0


# ══════════════════════════════════════════════════════════════
# 用例
# ══════════════════════════════════════════════════════════════

def t_v1_compat():
    print("[1] v1 旧格式兼容读取")
    d = new_dir()
    p = os.path.join(d, "n.json")
    write_raw(p, ["2026-11-24_20-20-33", "2026-11-01_08-00-00"])
    led = NotifiedLedger(p, max_keep=20000)
    check("v1 读到 2 条", led.size() == 2, led.size())
    check("v1 格式标记", "format=v1" in led.describe(), led.describe())
    from datetime import datetime as _dt
    expect = _dt(2026, 11, 24, 20, 20, 33).timestamp()
    check("event_time 解析", abs(event_time("2026-11-24_20-20-33") - expect) < 2,
          event_time("2026-11-24_20-20-33"))
    shutil.rmtree(d, ignore_errors=True)


def t_upgrade_to_v2():
    print("[2] 写入后升级为 v2 且保留 event_ids")
    d = new_dir()
    p = os.path.join(d, "n.json")
    write_raw(p, ["2026-11-24_20-20-33"])
    led = NotifiedLedger(p, max_keep=20000)
    led.add("2026-11-25_10-00-00")
    obj = json.load(open(p, encoding="utf-8"))
    check("version=2", obj.get("version") == 2, obj.get("version"))
    check("event_ids 仍在（老版本可读）", len(obj.get("event_ids") or []) == 2, obj.get("event_ids"))
    check("timestamps 存在", isinstance(obj.get("timestamps"), dict))
    shutil.rmtree(d, ignore_errors=True)


def t_true_fifo():
    print("[3] 真 FIFO：超限淘汰最老的（旧实现会随机丢）")
    d = new_dir()
    p = os.path.join(d, "n.json")
    led = NotifiedLedger(p, max_keep=3)
    # 故意乱序加：最老的在最后加
    for eid in ["2026-11-20_10-00-00", "2026-11-22_10-00-00",
                "2026-11-21_10-00-00", "2026-11-23_10-00-00"]:
        led.add(eid)
    obj = json.load(open(p, encoding="utf-8"))
    ids = obj.get("event_ids") or []
    check("保留 3 条", len(ids) == 3, ids)
    check("淘汰最老的 11-20", "2026-11-20_10-00-00" not in ids, ids)
    check("保留 21/22/23", set(ids) == {"2026-11-21_10-00-00", "2026-11-22_10-00-00",
                                       "2026-11-23_10-00-00"}, ids)
    check("文件内升序", ids == sorted(ids), ids)
    shutil.rmtree(d, ignore_errors=True)


def t_no_timestamp_id():
    print("[4] 无时间戳 ID 不炸（用当前时间，不参与误淘汰）")
    d = new_dir()
    p = os.path.join(d, "n.json")
    led = NotifiedLedger(p, max_keep=2)
    led.add("manual_upload_x")            # 解析不出时间 → ts=now
    led.add("2026-11-20_10-00-00")
    led.add("2026-11-21_10-00-00")        # 超限：manual（now 最大）不会被淘汰
    ids = json.load(open(p, encoding="utf-8")).get("event_ids") or []
    check("保留 2 条", len(ids) == 2, ids)
    check("最老的事件被淘汰", "2026-11-20_10-00-00" not in ids, ids)
    shutil.rmtree(d, ignore_errors=True)


def t_readback_widen():
    print("[5] 回读校验 + 扩窗重试（模拟首次写入把标记裁掉）")
    d = new_dir()
    p = os.path.join(d, "n.json")
    led = NotifiedLedger(p, max_keep=2)
    target = "2026-11-26_20-20-33"
    orig = led._write_raw
    state = {"n": 0}

    def flaky(entries, keep):
        state["n"] += 1
        if state["n"] == 1:                 # 第一次：模拟"被裁剪"
            trimmed = {k: v for k, v in entries.items() if k != target}
            return orig(trimmed, keep)
        return orig(entries, keep)

    led._write_raw = flaky
    ok = led.add(target)
    check("扩窗后确认落盘", ok is True)
    check("确实走了两轮写入", state["n"] == 2, state["n"])
    check("目标在账本中", led.contains(target))
    shutil.rmtree(d, ignore_errors=True)


def t_readback_fail():
    print("[6] 回读始终失败 → 返回 False 并留日志（不静默）")
    d = new_dir()
    p = os.path.join(d, "n.json")
    led = NotifiedLedger(p, max_keep=2)
    target = "2026-11-26_21-00-00"
    orig = led._write_raw
    led._write_raw = lambda entries, keep: orig(
        {k: v for k, v in entries.items() if k != target}, keep
    )
    ok = led.add(target)
    check("返回 False", ok is False)
    check("目标确实不在", not led.contains(target))
    shutil.rmtree(d, ignore_errors=True)


def t_lock_degrade():
    print("[12] 锁不可用时降级，且不掩盖临界区内的业务异常")
    d = new_dir()
    led = NotifiedLedger(os.path.join(d, "n.json"), max_keep=20000)
    led.lock_path = "/proc/nonexistent/x.lock"      # 必然创建失败 → 走降级分支
    raised = None
    try:
        with led._locked():
            raise OSError("业务内的 OSError")
    except OSError as exc:
        raised = exc
    except RuntimeError as exc:                     # 二次 yield 的典型症状
        raised = exc
    check("业务异常原样传播（未被 contextmanager 掩盖）",
          isinstance(raised, OSError) and "业务内" in str(raised), repr(raised))
    shutil.rmtree(d, ignore_errors=True)


def t_idempotent():
    print("[7] 重复 mark 幂等（第二次不写盘）")
    d = new_dir()
    p = os.path.join(d, "n.json")
    led = NotifiedLedger(p, max_keep=20000)
    eid = "2026-11-25_12-00-00"
    led.add(eid)
    m1 = os.stat(p).st_mtime_ns
    time.sleep(0.05)
    led.add(eid)
    m2 = os.stat(p).st_mtime_ns
    check("mtime 未变（未重复写）", m1 == m2, "%s → %s" % (m1, m2))
    check("只有 1 条", led.size() == 1, led.size())
    shutil.rmtree(d, ignore_errors=True)


def t_concurrent():
    print("[8] 多进程并发 mark 不丢更新")
    if not IS_LINUX:
        print("  SKIP  仅 Linux 有 fcntl（Windows 无锁，跳过）")
        return
    d = new_dir()
    p = os.path.join(d, "n.json")
    procs = []
    for i in range(4):
        procs.append(subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker", p, "W%d" % i, "25"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for pr in procs:
        pr.wait(timeout=120)
    led = NotifiedLedger(p, max_keep=50000)
    n = led.size()
    check("100 条全部落盘（无覆盖丢失）", n == 100, "实际 %d 条" % n)
    shutil.rmtree(d, ignore_errors=True)


def t_compact():
    print("[11] compact：v1 无序账本重排为 v2 有序")
    d = new_dir()
    p = os.path.join(d, "n.json")
    ids = ["2026-11-22_10-00-00", "2026-11-20_10-00-00", "2026-11-21_10-00-00"]
    write_raw(p, ids)                      # 旧实现写出来的无序 v1
    led = NotifiedLedger(p, max_keep=20000)
    kept, fmt = led.compact()
    obj = json.load(open(p, encoding="utf-8"))
    got = obj.get("event_ids") or []
    check("compact 保留 3 条", kept == 3 and len(got) == 3, (kept, got))
    check("compact 后升序", got == sorted(got), got)
    check("compact 后为 v2", obj.get("version") == 2, obj.get("version"))
    check("原格式为 v1", fmt == "v1", fmt)
    shutil.rmtree(d, ignore_errors=True)


def t_integration():
    print("[9] 与 sentry_notify_queue 集成")
    import sentry_notify_queue as sq
    check("NOTIFIED_MAX_KEEP=20000", sq.NOTIFIED_MAX_KEEP == 20000, sq.NOTIFIED_MAX_KEEP)
    d = new_dir()
    q = sq.SentryNotifyQueue(queue_file=Path(os.path.join(d, "q.json")))
    q.notified_file = os.path.join(d, "n.json")
    eid = "2026-11-24_20-20-33"
    q.mark_notified(eid)
    check("mark 后账本含该事件", eid in q._load_notified())
    check("describe 可读", "oldest=" in q._ledger().describe(), q._ledger().describe())
    shutil.rmtree(d, ignore_errors=True)


def t_legacy_reader():
    print("[10] 老版本读法（data['event_ids']）仍能读到全部")
    d = new_dir()
    p = os.path.join(d, "n.json")
    led = NotifiedLedger(p, max_keep=20000)
    for eid in ["2026-11-20_10-00-00", "2026-11-21_10-00-00"]:
        led.add(eid)
    data = json.load(open(p, encoding="utf-8"))
    legacy = set(data.get("event_ids", [])) if isinstance(data, dict) else set(data)
    check("老读法拿到 2 条", len(legacy) == 2, legacy)
    shutil.rmtree(d, ignore_errors=True)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        sys.exit(worker(sys.argv[2], sys.argv[3], int(sys.argv[4])))

    print("=" * 60)
    print("账本回归测试（v0.3.1.94）")
    print("=" * 60)
    t_v1_compat()
    t_upgrade_to_v2()
    t_true_fifo()
    t_no_timestamp_id()
    t_readback_widen()
    t_readback_fail()
    t_idempotent()
    t_concurrent()
    t_compact()
    t_lock_degrade()
    t_integration()
    t_legacy_reader()
    print("-" * 60)
    print("PASS=%d FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
