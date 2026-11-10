#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级/回退的数据搬迁安全性测试（v0.3.1.84 / M139）

事故背景（2026-11-10）：升级到 v0.3.1.83 期间，
  · 页面「僚机安全登录信息」变空、守护线程起不来 —— 因为 `config/` 被
    `rmtree` + `copytree` 非原子替换，中间有个"目录不存在"的窗口；
  · `sqlite3.OperationalError: no such table: delete_queue` —— 在线复制 WAL 模式的
    `.db` 与 `-wal` 顺序不定，表只存在于 WAL 里就跟着丢；
  · 回退救不回来 —— 备份是 `except: pass` 的"尽力而为"，还出现过 9 KB 的空包
    （`teslausb-vteslausb.tar.gz`）。

本测试钉死四件事：
  A. 替换是原子的：复制期间旧目录**一直存在**，切完旧目录仍保留一份
  B. 保存失败必须抛错（不再静默产半份），且临时目录被清理
  C. 备份必须自检（版本目录 / 体积 / 必含 app.py + config/）
  D. 软链切换遇到"真实目录布局"必须拒绝，绝不能把整个部署删掉

跑法：python _test_upgrade_atomic.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import upgrade_service as up   # noqa: E402

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


SKIPPED = []


def skip(name, why):
    SKIPPED.append(name)
    print("SKIP %s （%s）" % (name, why))


def can_symlink(d):
    t = os.path.join(d, ".symtest")
    try:
        os.symlink(d, t)
        os.unlink(t)
        return True
    except (OSError, NotImplementedError):
        return False


def mk(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    root = tempfile.mkdtemp(prefix="upa-")
    saved = {"SYMLINK": up.SYMLINK, "BAK_DIR": up.BAK_DIR,
             "DEPLOY_BASE": up.DEPLOY_BASE}

    # ── A. 原子替换 ────────────────────────────────────────
    src = os.path.join(root, "src_cfg")
    mk(os.path.join(src, "sentry.json"), '{"k":"v"}')
    mk(os.path.join(src, "guard.json"), '{"enabled":true}')
    dest = os.path.join(root, "live", "config")
    mk(os.path.join(dest, "old.json"), "old")

    seen_alive = []
    orig_copy = shutil.copytree

    def spy(s, d, **kw):
        if os.path.abspath(d).startswith(os.path.abspath(dest) + "."):
            seen_alive.append(os.path.isdir(dest))   # 复制期间旧目录必须还在
        return orig_copy(s, d, **kw)

    shutil.copytree = spy
    try:
        old_dir = up._copy_dir_atomic(src, dest, "config")
    finally:
        shutil.copytree = orig_copy

    ok("A1 复制期间旧目录一直存在（无空窗）", seen_alive and all(seen_alive), seen_alive)
    ok("A2 新目录已生效", os.path.isfile(os.path.join(dest, "guard.json")))
    ok("A3 旧目录被保留而不是删除", bool(old_dir) and os.path.isdir(old_dir), old_dir)
    ok("A4 旧目录内容还在（可人工回捞）",
       os.path.isfile(os.path.join(old_dir, "old.json")), old_dir)
    ok("A5 只保留一份 .old",
       len([d for d in os.listdir(os.path.dirname(dest)) if d.startswith("config.old-")]) == 1)

    # ── A6. 复制失败时不得破坏 dest ─────────────────────────
    src_bad = os.path.join(root, "src_bad")
    os.makedirs(src_bad, exist_ok=True)
    real_copy = shutil.copytree

    def boom(s, d, **kw):
        raise OSError("disk full")
    shutil.copytree = boom
    raised = False
    try:
        up._copy_dir_atomic(src_bad, dest, "config")
    except Exception:
        raised = True
    finally:
        shutil.copytree = real_copy
    ok("A6 复制失败会抛错", raised)
    ok("A7 失败后现有目录完好", os.path.isfile(os.path.join(dest, "guard.json")))

    # ── B. _save_user_data 失败必须抛错 ────────────────────
    dep = os.path.join(root, "deploy")
    mk(os.path.join(dep, "config", "sentry.json"), '{"wecom":"k"}')
    mk(os.path.join(dep, "data", "guard_status.json"), '{"seq":1}')

    tmpd, cfg, data, thumbs = up._save_user_data(dep)
    ok("B1 正常保存返回路径", os.path.isdir(cfg) and os.path.isdir(data))
    ok("B2 配置内容一致",
       open(os.path.join(cfg, "sentry.json")).read() == '{"wecom":"k"}')
    up._release_keep_tmp({"keep_tmpd": tmpd})

    shutil.copytree = boom
    raised = False
    try:
        up._save_user_data(dep)
    except up.UserDataError:
        raised = True
    finally:
        shutil.copytree = real_copy
    ok("B3 保存失败抛 UserDataError（不再静默）", raised)

    # ── B4. SQLite：WAL 里的表必须跟着走 ────────────────────
    db_dir = os.path.join(root, "db", "data")
    os.makedirs(db_dir, exist_ok=True)
    dbp = os.path.join(db_dir, "file_index.db")
    conn = sqlite3.connect(dbp)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")     # 别让 sqlite 自动并回去
    conn.execute("CREATE TABLE delete_queue (id INTEGER PRIMARY KEY, p TEXT)")
    conn.execute("INSERT INTO delete_queue (p) VALUES ('a')")
    conn.commit()
    ok("B4 构造出带 -wal 的现场", os.path.exists(dbp + "-wal"))
    # 我们的保存流程会先 checkpoint：把 WAL 回填进主库。
    # 判据不看 -wal 体积（FULL 不截断文件），而是"只拿 .db 这一个文件还完整吗"——
    # 这正是事故形态：只搬了 .db、丢掉 -wal，于是表消失。
    up._checkpoint_sqlite(db_dir)
    db_only = os.path.join(root, "only.db")
    shutil.copy2(dbp, db_only)
    try:
        c4 = sqlite3.connect(db_only)
        n4 = c4.execute("SELECT count(*) FROM delete_queue").fetchone()[0]
        c4.close()
        ok("B4b checkpoint 后只拿 .db 也完整（表没落在 WAL 里）", n4 == 1, n4)
    except Exception as exc:
        ok("B4b checkpoint 后只拿 .db 也完整（表没落在 WAL 里）", False, repr(exc))
    conn.close()

    copied = os.path.join(root, "db_copy")
    shutil.copytree(db_dir, copied, symlinks=True)
    up._verify_sqlite_copies(db_dir, copied)
    c2 = sqlite3.connect(os.path.join(copied, "file_index.db"))
    rows = c2.execute("SELECT count(*) FROM delete_queue").fetchone()[0]
    c2.close()
    eq("B5 复制件里 delete_queue 表还在", rows, 1)

    # B6：复制件坏了 → 用 VACUUM INTO 从源库重建
    bad_dir = os.path.join(root, "db_bad")
    shutil.copytree(db_dir, bad_dir, symlinks=True)
    open(os.path.join(bad_dir, "file_index.db"), "wb").write(b"garbage")
    up._verify_sqlite_copies(db_dir, bad_dir)
    c3 = sqlite3.connect(os.path.join(bad_dir, "file_index.db"))
    rows3 = c3.execute("SELECT count(*) FROM delete_queue").fetchone()[0]
    c3.close()
    eq("B6 坏副本被 VACUUM INTO 重建", rows3, 1)

    # ── C. 备份自检 ────────────────────────────────────────
    if not can_symlink(root):
        for n in ("C1 备份过小被拒绝", "C2 非版本目录被拒绝",
                  "D1 真实目录布局拒绝切换", "D2 拒绝后目录内容完好（没被 rmtree）",
                  "D3 软链已切到新目标"):
            skip(n, "本机没有创建符号链接的权限，需在 Linux/真机跑")
        for k, v in saved.items():
            setattr(up, k, v)
        shutil.rmtree(root, ignore_errors=True)
        print("\n%d/%d passed（跳过 %d 项）" % (PASS, PASS + FAIL, len(SKIPPED)))
        return 0 if FAIL == 0 else 1

    up.BAK_DIR = os.path.join(root, "bak")
    fake_link = os.path.join(root, "teslausb")
    ver_dir = os.path.join(root, "teslausb-v0.3.1.82")
    mk(os.path.join(ver_dir, "app.py"), "# app")
    mk(os.path.join(ver_dir, "config", "sentry.json"), "{}")
    os.symlink(ver_dir, fake_link)
    up.SYMLINK = fake_link
    up.DEPLOY_BASE = root

    # C1: 小版本目录 → 备份包过小，必须拒绝
    okf, msg = up._backup_current()
    ok("C1 备份过小被拒绝", okf is False and "过小" in msg, msg)

    # C2: 非版本目录（软链指向普通目录）→ 拒绝
    plain = os.path.join(root, "plain")
    mk(os.path.join(plain, "app.py"), "# a")
    os.unlink(fake_link)
    os.symlink(plain, fake_link)
    okf, msg = up._backup_current()
    ok("C2 非版本目录被拒绝", okf is False and "不是版本目录" in msg, msg)

    # ── D. 软链切换：真实目录布局必须拒绝且不删 ────────────
    real_root = os.path.join(root, "deploy_root")
    mk(os.path.join(real_root, "app.py"), "# keep me")
    up.SYMLINK = real_root
    raised = False
    try:
        up._switch_symlink(os.path.join(root, "teslausb-v0.3.1.84"))
    except up.UserDataError:
        raised = True
    ok("D1 真实目录布局拒绝切换", raised)
    ok("D2 拒绝后目录内容完好（没被 rmtree）",
       os.path.isfile(os.path.join(real_root, "app.py")))

    # D3: 正常软链切换
    link2 = os.path.join(root, "link2")
    os.symlink(os.path.join(root, "old_target"), link2)
    up.SYMLINK = link2
    target = os.path.join(root, "teslausb-v0.3.1.84")
    os.makedirs(target, exist_ok=True)
    up._switch_symlink(target)
    eq("D3 软链已切到新目标", os.path.realpath(link2), os.path.realpath(target))

    for k, v in saved.items():
        setattr(up, k, v)
    shutil.rmtree(root, ignore_errors=True)

    print("\n%d/%d passed" % (PASS, PASS + FAIL))
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
