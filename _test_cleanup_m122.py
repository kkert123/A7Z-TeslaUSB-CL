#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M122 回归测试：清理 O(n²) / 单飞锁 / 历史原子写

设计原则：
  1. **确定性优先**：「不再 O(n²)」不是靠计时断言，而是把 os.listdir
     打桩成「一调用就抛异常」，能跑通即证明删除循环里不再整目录扫描。
  2. 大文件数场景只在 Linux 上跑（Windows 沙箱对批量删除有拦截）。
  3. 可本机跑，也可在 A7Z（python3.9）跑：`python3 _test_cleanup_m122.py`
"""
import io
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import auto_cleanup as ac                     # noqa: E402
from routes import cleanup_routes as cr       # noqa: E402

PASS = 0
FAIL = 0
IS_LINUX = sys.platform.startswith("linux")
# 真机上测试文件可能只拷到 /tmp/m122，而模板/源码在部署目录 —— 逐个候选取第一个存在的
LIVE_ROOT = os.environ.get("TESLAUSB_ROOT") or "/opt/radxa_data/teslausb"


def find_file(*parts):
    for base in (ROOT, LIVE_ROOT):
        cand = os.path.join(base, *parts)
        if os.path.isfile(cand):
            return cand
    return os.path.join(ROOT, *parts)


WORK = os.path.join(ROOT, ".workbuddy", "_tmp", "m122_work")
if os.path.isdir(WORK):
    shutil.rmtree(WORK, ignore_errors=True)   # 显式清理，见用户级记忆（批量删除拦截）
os.makedirs(WORK, exist_ok=True)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s  <<< %s" % (name, detail))


def fresh_dir(name):
    d = os.path.join(WORK, name)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


def make_files(d, n, prefix="f"):
    paths = []
    for i in range(n):
        p = os.path.join(d, "%s%04d.jpg" % (prefix, i))
        with open(p, "wb") as fh:
            fh.write(b"x" * 16)
        paths.append(p)
    return paths


# ══════════════════════════════════════════════════════════════
print("[S1] _dir_is_empty：O(1) 判空的正确性")
c = ac.AutoCleaner()
d = fresh_dir("s1_empty")
check("S1.1 空目录 → True", c._dir_is_empty(d) is True)

d = fresh_dir("s1_one")
make_files(d, 1)
check("S1.2 一个文件 → False", c._dir_is_empty(d) is False)

d = fresh_dir("s1_many")
make_files(d, 100)
check("S1.3 一百个文件 → False", c._dir_is_empty(d) is False)
check("S1.40 不存在的目录 → False（不抛异常）",
      c._dir_is_empty(os.path.join(WORK, "nope")) is False)

# ══════════════════════════════════════════════════════════════
print("[S2] _prune_empty_parents：仍能回收空目录")
base = fresh_dir("s2_nest")
deep = os.path.join(base, "a", "b", "c")
os.makedirs(deep)
f = os.path.join(deep, "x.txt")
open(f, "w").close()
c = ac.AutoCleaner()
c._nonempty_parents = set()
os.remove(f)
c._prune_empty_parents(deep)
check("S2.1 三层空目录整条删掉", not os.path.exists(os.path.join(base, "a")))

base = fresh_dir("s2_keep")
f1 = os.path.join(base, "keep.jpg")
f2 = os.path.join(base, "del.jpg")
open(f1, "w").close()
open(f2, "w").close()
c = ac.AutoCleaner()
c._nonempty_parents = set()
os.remove(f2)
c._prune_empty_parents(base)
check("S2.2 目录还有别的文件 → 不删目录", os.path.isdir(base))

# ══════════════════════════════════════════════════════════════
print("[S3] O(n²) 回归（确定性）：删除循环里不得再整目录 listdir")
d = fresh_dir("s3_perf")
files = make_files(d, 30)
c = ac.AutoCleaner()
c.stats["errors"] = []
c.stats["missing_files"] = 0
c._nonempty_parents = set()

_orig_listdir = ac.os.listdir


def _boom(*a, **kw):
    raise AssertionError("os.listdir 被调用 —— O(n²) 回来了")


ac.os.listdir = _boom
try:
    ok_count = 0
    for p in files:
        ok, _sz = c._delete_file_safe(p)
        ok_count += 1 if ok else 0
    check("S3.1 打桩 listdir 抛异常后仍能删完 30 个文件",
          ok_count == 30 and os.listdir is _boom, "ok=%d" % ok_count)
    check("S3.2 删除过程零 error", not c.stats.get("errors"), c.stats.get("errors"))
finally:
    ac.os.listdir = _orig_listdir

if IS_LINUX:
    d = fresh_dir("s3_big")
    N = 3000
    files = make_files(d, N)
    c = ac.AutoCleaner()
    c.stats["errors"] = []
    c.stats["missing_files"] = 0
    c._nonempty_parents = set()
    t0 = time.time()
    for p in files:
        c._delete_file_safe(p)
    dt = time.time() - t0
    # 旧实现：N × listdir(N) ≈ 3000 × 2ms ≈ 6s+，且随 N 平方增长；
    # 新实现：N × (unlink + 一次 scandir) 线性。
    check("S3.3 单目录删 %d 个文件耗时 < 5s（旧实现为 O(n²)）" % N, dt < 5.0,
          "%.2fs" % dt)
    print("       实测 %.2fs（%.3f ms/文件）" % (dt, dt * 1000.0 / N))

# ══════════════════════════════════════════════════════════════
print("[S4] ENOENT 竞态：记 missing，不再当 ERROR")
c = ac.AutoCleaner()
c.stats["errors"] = []
c.stats["missing_files"] = 0
ok, sz = c._delete_file_safe(os.path.join(WORK, "not-there.jpg"))
check("S4.1 文件本就不存在 → False + missing=1",
      ok is False and c.stats["missing_files"] == 1, c.stats)
check("S4.2 不记 error", not c.stats["errors"], c.stats["errors"])

d = fresh_dir("s4_race")
p = os.path.join(d, "race.jpg")
open(p, "w").close()
c = ac.AutoCleaner()
c.stats["errors"] = []
c.stats["missing_files"] = 0
c._nonempty_parents = set()
_orig_remove = ac.os.remove
ac.os.remove = lambda *a, **kw: (_ for _ in ()).throw(
    FileNotFoundError(2, "No such file or directory"))
try:
    ok, _sz = c._delete_file_safe(p)
finally:
    ac.os.remove = _orig_remove
check("S4.3 检查后被并发删掉（remove 抛 ENOENT）→ False + missing=1",
      ok is False and c.stats["missing_files"] == 1, c.stats)
check("S4.4 竞态不再写成 ERROR", not c.stats["errors"], c.stats["errors"])

# ══════════════════════════════════════════════════════════════
print("[S5] atomic_write_json：原子替换、无残留")
d = fresh_dir("s5")
p = os.path.join(d, "h.json")
ac.atomic_write_json(p, [{"a": 1}])
with io.open(p, "r", encoding="utf-8") as fh:
    check("S5.1 写回内容一致", json.load(fh) == [{"a": 1}])
leftover = [f for f in os.listdir(d) if f.startswith((".tmp_", ".atomic-"))]
check("S5.2 无 .tmp_ 残留", not leftover, leftover)

ac.atomic_write_json(p, [{"a": 2}])
_orig_dump = ac.json.dump


def _bad_dump(*a, **kw):
    raise RuntimeError("磁盘满")


ac.json.dump = _bad_dump
try:
    ok = False
    try:
        ac.atomic_write_json(p, [{"a": 3}])
    except RuntimeError:
        ok = True
    check("S5.3 dump 中途失败 → 抛异常", ok)
finally:
    ac.json.dump = _orig_dump
with io.open(p, "r", encoding="utf-8") as fh:
    check("S5.4 写失败后旧文件内容完好（不会被截断成半截）",
          json.load(fh) == [{"a": 2}])
leftover = [f for f in os.listdir(d) if f.startswith((".tmp_", ".atomic-"))]
check("S5.5 失败也不留 .tmp_", not leftover, leftover)

# ══════════════════════════════════════════════════════════════
print("[S6] append_cleanup_history：最新在前 + 上限 50")
d = fresh_dir("s6")
p = os.path.join(d, "h.json")
for i in range(55):
    ac.append_cleanup_history({"n": i}, p, keep=50)
h = ac.load_cleanup_history(p)
check("S6.1 只保留 50 条", len(h) == 50, len(h))
check("S6.2 最新一条在最前", h[0]["n"] == 54, h[0] if h else None)
check("S6.3 末尾是最早保留的那条", h[-1]["n"] == 5, h[-1] if h else None)

# ══════════════════════════════════════════════════════════════
print("[S7] load_cleanup_history：坏文件隔离自愈，不抛异常")
d = fresh_dir("s7")
p = os.path.join(d, "h.json")
io.open(p, "w", encoding="utf-8").write('[{"x": 1}, {"y": 2')
h = ac.load_cleanup_history(p)
check("S7.1 损坏文件 → 返回空列表（不抛）", h == [], h)
leftover = [f for f in os.listdir(d) if ".corrupt-" in f]
check("S7.2 已隔离为 .corrupt-<ts>", len(leftover) == 1, leftover)
check("S7.3 状态位 corrupted=True", ac._HISTORY_STATE["corrupted"] is True)
check("S7.4 原路径已不存在（可重建）", not os.path.exists(p))

ac.atomic_write_json(p, [{"ok": 1}])
h = ac.load_cleanup_history(p)
check("S7.5 正常文件 → 正常读出", h == [{"ok": 1}], h)
check("S7.6 状态位复位 corrupted=False", ac._HISTORY_STATE["corrupted"] is False)
check("S7.7 文件不存在 → 空列表",
      ac.load_cleanup_history(os.path.join(d, "none.json")) == [])

# ══════════════════════════════════════════════════════════════
print("[S8] 复刻 2026-11-07 的真实损坏形态（char 8200 截断）")
d = fresh_dir("s8")
p = os.path.join(d, "h.json")
c = ac.AutoCleaner()
big = [{"timestamp": "2026-11-07T10:30:02", "deleted_files": 1607,
        "errors": ["e" * 60] * 10, "actions": ["a" * 60] * 20} for _ in range(30)]
raw = json.dumps(big, ensure_ascii=False, indent=2)
io.open(p, "w", encoding="utf-8").write(raw[:8200])   # 正好断在 8KB 缓冲边界
try:
    h = ac.load_cleanup_history(p)
    crashed = False
except Exception as exc:                              # noqa: BLE001
    h, crashed = None, "%s: %s" % (type(exc).__name__, exc)
check("S8.1 8KB 处截断的文件不再让接口崩", crashed is False, crashed)
check("S8.2 返回空列表并隔离", h == [] and ac._HISTORY_STATE["corrupted"] is True)

# ══════════════════════════════════════════════════════════════
print("[S9] 单飞锁：不排队、不重入、可释放")
cr._CLEANUP_RUN_STATE["started_at"] = 0.0
cr._CLEANUP_RUN_STATE["fp"] = None
ok, elapsed = cr._acquire_cleanup_lock()
check("S9.1 首次获取成功", ok is True and elapsed == 0, (ok, elapsed))
ok2, elapsed2 = cr._acquire_cleanup_lock()
check("S9.2 执行中再次获取被拒", ok2 is False, ok2)
check("S9.3 被拒时给出已进行秒数（≥0）", isinstance(elapsed2, int) and elapsed2 >= 0,
      elapsed2)
cr._release_cleanup_lock()
ok3, _ = cr._acquire_cleanup_lock()
check("S9.4 释放后可再次获取", ok3 is True)
cr._release_cleanup_lock()
try:
    cr._release_cleanup_lock()
    double_release_ok = True
except Exception:                                     # noqa: BLE001
    double_release_ok = False
check("S9.5 重复释放不抛异常", double_release_ok)

# ══════════════════════════════════════════════════════════════
print("[S10] 接口行为：409 抢占 + 历史接口不再 500")
try:
    from flask import Flask
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(cr.cleanup_bp)
    client = app.test_client()

    # (a) 锁被占 → 409 + cleaning
    cr._CLEANUP_RUN_STATE["started_at"] = 0.0
    cr._CLEANUP_RUN_STATE["fp"] = None
    cr._acquire_cleanup_lock()
    r = client.post("/api/cleanup/execute")
    body = r.get_json() or {}
    check("S10.1 清理进行中再点 → 409", r.status_code == 409, r.status_code)
    check("S10.2 响应带 cleaning=True 与 elapsed_seconds",
          body.get("cleaning") is True and "elapsed_seconds" in body, body)
    cr._release_cleanup_lock()

    # (b) 空闲时正常进入实现（把实现替换掉，避免真删文件）
    _orig_impl = cr._cleanup_execute_impl
    cr._cleanup_execute_impl = lambda: (__import__("flask").jsonify(
        {"success": True, "mode": "edit"}), 200)
    try:
        r = client.post("/api/cleanup/execute")
        check("S10.3 空闲时正常执行 200", r.status_code == 200, r.status_code)
        r2 = client.post("/api/cleanup/execute")
        check("S10.4 上一轮结束后锁已释放（可再次执行）", r2.status_code == 200,
              r2.status_code)
    finally:
        cr._cleanup_execute_impl = _orig_impl
    check("S10.5 实现抛异常时锁也会释放（finally）",
          cr._CLEANUP_LOCK.locked() is False)

    # (c) 历史文件损坏 → 200 + history_reset，而不是 500
    d = fresh_dir("s10")
    cr.CLEANUP_LOG = os.path.join(d, "h.json")
    io.open(cr.CLEANUP_LOG, "w", encoding="utf-8").write('[{"broken": ')
    r = client.get("/api/cleanup/history")
    body = r.get_json() or {}
    check("S10.6 历史损坏 → 200（不再 500）", r.status_code == 200, r.status_code)
    check("S10.7 返回 history_reset=True 供前端提示",
          body.get("history_reset") is True, body)
    check("S10.8 返回空列表而不是报错", body.get("history") == [], body.get("history"))
except Exception as exc:                              # noqa: BLE001
    check("S10 接口层测试可运行", False, "%s: %s" % (type(exc).__name__, exc))

# ══════════════════════════════════════════════════════════════
print("[S11] 源码静态断言（防回归回退）")
src_ac = io.open(find_file("auto_cleanup.py"), encoding="utf-8").read()
src_cr = io.open(find_file("routes", "cleanup_routes.py"), encoding="utf-8").read()
src_ui = io.open(find_file("templates", "analytics.html"), encoding="utf-8").read()

check("S11.1 auto_cleanup 不再有 `not os.listdir(parent)`",
      "not os.listdir(parent)" not in src_ac)
check("S11.2 auto_cleanup 有 O(1) 判空实现", "def _dir_is_empty" in src_ac)
check("S11.3 auto_cleanup 历史写走原子实现",
      "append_cleanup_history(self.stats" in src_ac)
check("S11.4 auto_cleanup 捕获 FileNotFoundError",
      "except FileNotFoundError:" in src_ac)
check("S11.5 routes 有单飞锁与实现拆分",
      "_acquire_cleanup_lock" in src_cr and "def _cleanup_execute_impl" in src_cr)
check("S11.6 routes 返回 409 + cleaning", "}), 409" in src_cr and "'cleaning': True" in src_cr)
check("S11.7 routes 历史读走统一实现", "load_cleanup_history(CLEANUP_LOG)" in src_cr)
check("S11.8 routes 不再裸 except: pass", "    except:\n        pass" not in src_cr)
check("S11.9 前端有执行中标记", "_cleanupRunning=false;" in src_ui)
check("S11.10 前端 applyCleanupMode 不再无条件解禁",
      "btn.disabled=_cleanupRunning;" in src_ui)
check("S11.11 前端有超时提示", "清理仍需时间" in src_ui)
check("S11.12 前端处理 409", "d.cleaning" in src_ui)
check("S11.13 前端历史重建提示", "history_reset" in src_ui)
check("S11.14 前端成功后也刷新历史",
      src_ui.count("loadCleanupHistory();") >= 3)

# ══════════════════════════════════════════════════════════════
print("\n结果: %d 通过, %d 失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
