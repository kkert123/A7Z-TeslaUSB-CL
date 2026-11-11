#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply.py 主动回退分支的结构测试（guardian-b v1.1.6 / M145）

只测「不碰真 systemd」的那几层：DEST 指向临时目录，service unit 故意用一个
不存在的名字，`restart_unit` 必然失败、`health_check` 必然超时——这恰好把
**最坏情况**造出来了：

  A. 目标版本非法 / 目录不存在 → 标 failed，current 一动不动
  B. 目标合法但起不来 → **自动切回原版本**（这是本功能最关键的兜底：
     回退过去跑不起来等于守护没了，比不回退严重得多）
  C. 非 rollback 状态（healthy/committed…）→ 一行都不执行

跑法（需要 Linux，Windows 建不了符号链接）：
    cd guardian-b && python3 tests/test_apply_rollback.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GB = os.path.dirname(HERE)
APPLY = os.path.join(GB, "deploy", "apply.py")

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


def mk_dest():
    """造一个「current 指向 1.1.5、本地还留着 1.1.4」的部署目录。"""
    d = tempfile.mkdtemp(prefix="gbrb-")
    for v in ("1.1.4", "1.1.5"):
        p = os.path.join(d, "releases", v)
        os.makedirs(p)
        for f in ("app.py", "config.py"):
            with open(os.path.join(p, f), "w", encoding="utf-8") as fh:
                fh.write("# %s\n" % f)
    os.makedirs(os.path.join(d, "data", "upgrade"), exist_ok=True)
    os.symlink(os.path.join(d, "releases", "1.1.5"), os.path.join(d, "current"))
    return d


def run_apply(dest, journal):
    upd = os.path.join(dest, "data", "upgrade")
    jp = os.path.join(upd, "journal.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(journal, f)
    with open(os.path.join(upd, "request"), "w", encoding="utf-8") as f:
        f.write("1")
    env = dict(os.environ,
               GUARD_HOME=dest,
               GUARD_BIND_HOST="127.0.0.1",   # 连接立即被拒 → health_check 快速失败
               GUARD_BIND_PORT="1",
               GUARD_UNIT="guard-b-nonexistent-test",
               GUARD_TOKEN="test-token")
    r = subprocess.run([sys.executable, APPLY], env=env,
                       capture_output=True, timeout=240)
    with open(jp, encoding="utf-8") as f:
        return r, json.load(f)


def cur_ver(dest):
    p = os.path.join(dest, "current")
    return os.path.basename(os.readlink(p)) if os.path.islink(p) else ""


def _can_symlink():
    """`hasattr(os, "symlink")` 不够：Windows 有这个属性但没特权，建链接会抛 OSError。"""
    t = tempfile.mkdtemp(prefix="gbrb-probe-")
    try:
        p = os.path.join(t, "x")
        os.symlink(t, p)
        os.unlink(p)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(t, ignore_errors=True)


def main():
    if not _can_symlink():
        # 本机建不了软链 → 回退功能的核心前提（releases + current）造不出来。
        # 这不是失败，是环境缺能力；Linux/真机上跑满。
        print("SKIP 本机没有创建符号链接的权限（需在 Linux/真机跑）")
        return 0

    # ── A. 非法目标：拒绝且不动 current ──
    d = mk_dest()
    try:
        r, j = run_apply(d, {"state": "rollback", "version": "9.9.9",
                             "from_version": "1.1.5"})
        ok("A1 未知版本被拒（state=failed）", j.get("state") == "failed", j)
        ok("A2 错误码可读", "target_missing" in str(j.get("error")), j.get("error"))
        ok("A3 current 未被动过", cur_ver(d) == "1.1.5", cur_ver(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ── B. 目标合法但跑不起来 → 自动切回原版本（最坏情况兜底）──
    d = mk_dest()
    try:
        r, j = run_apply(d, {"state": "rollback", "version": "1.1.4",
                             "from_version": "1.1.5"})
        ok("B1 记录为 rollback_failed（如实反映失败）",
           j.get("state") == "rollback_failed", j)
        ok("B2 错误里带健康检查原因", "healthcheck_failed" in str(j.get("error")), j.get("error"))
        ok("B3 current 已自动切回 1.1.5（不能把僚机留在起不来的版本上）",
           cur_ver(d) == "1.1.5", cur_ver(d))
        ok("B4 标了 back_ok 字段", "back_ok" in j, j)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ── C. 非 rollback 状态：必须一行都不执行 ──
    d = mk_dest()
    try:
        r, j = run_apply(d, {"state": "healthy", "version": "1.1.5"})
        ok("C1 healthy 状态不动作", cur_ver(d) == "1.1.5", cur_ver(d))
        ok("C2 journal 未被改写", j.get("state") == "healthy", j)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ── D. 目标就是当前版本：幂等返回，不做无谓重启 ──
    d = mk_dest()
    try:
        r, j = run_apply(d, {"state": "rollback", "version": "1.1.5",
                             "from_version": "1.1.5"})
        ok("D1 已在目标版本 → rolled_back_ok", j.get("state") == "rolled_back_ok", j)
        ok("D2 备注 already_there", j.get("note") == "already_there", j.get("note"))
        ok("D3 current 仍是 1.1.5", cur_ver(d) == "1.1.5", cur_ver(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("\n%d/%d passed" % (PASS, PASS + FAIL))
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
