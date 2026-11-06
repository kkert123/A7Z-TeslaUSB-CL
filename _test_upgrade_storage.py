#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.3.1.74 存储治理回归测试

覆盖：版本目录清理（安全护栏）、缩略图共享迁移（幂等）、
临时目录 finally 清理（失败路径）、遗留临时目录清理、备份保留 5 个。
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import upgrade_service as us  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        print("  FAIL  " + name + (("  " + str(detail)) if detail else ""))


BASE = ROOT / ".workbuddy" / "_tmp" / "storetest"
if BASE.exists():
    shutil.rmtree(BASE)
(BASE / "data").mkdir(parents=True)

# ── 隔离：把模块里的部署路径全部指向沙箱 ────────────────────────────
us.DEPLOY_BASE = str(BASE / "deploy")
us.SYMLINK = os.path.join(us.DEPLOY_BASE, "teslausb")
us.BAK_DIR = os.path.join(us.DEPLOY_BASE, "teslausb-bak")
us.SHARED_DIR = os.path.join(us.DEPLOY_BASE, "shared")
us.SHARED_THUMBNAILS = os.path.join(us.SHARED_DIR, "thumbnails")
us.VERSION_FILE = str(BASE / "data" / "version_history.json")
os.makedirs(us.DEPLOY_BASE, exist_ok=True)
os.makedirs(us.BAK_DIR, exist_ok=True)

# 临时目录也隔离（_save_user_data 用 mkdtemp）
TMPROOT = BASE / "tmp"
TMPROOT.mkdir(exist_ok=True)
tempfile.tempdir = str(TMPROOT)


def mkver(v, nfiles=3):
    d = os.path.join(us.DEPLOY_BASE, "teslausb-v%s" % v)
    os.makedirs(os.path.join(d, "static", "thumbnails"), exist_ok=True)
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    os.makedirs(os.path.join(d, "config"), exist_ok=True)
    Path(d, "config", "x.json").write_text("{}", encoding="utf-8")
    for i in range(nfiles):
        Path(d, "static", "thumbnails", "t%d.jpg" % i).write_text("x" * 100, encoding="utf-8")
    return d


print("[T1] 版本目录清理：保留最近 5 个")
vers = ["0.3.1.60", "0.3.1.61", "0.3.1.62", "0.3.1.63", "0.3.1.64", "0.3.1.65", "0.3.1.66"]
dirs = {v: mkver(v) for v in vers}
cur = dirs["0.3.1.66"]
if os.path.islink(us.SYMLINK) or os.path.exists(us.SYMLINK):
    os.unlink(us.SYMLINK) if os.path.islink(us.SYMLINK) else shutil.rmtree(us.SYMLINK)
os.symlink(cur, us.SYMLINK)

# 干扰项：名字像但不是版本目录 / 特殊目录
for decoy in ("teslausb-web", "tim", "teslausb-vmanual-upload", "shared"):
    os.makedirs(os.path.join(us.DEPLOY_BASE, decoy), exist_ok=True)
    Path(us.DEPLOY_BASE, decoy, "keep.txt").write_text("keep", encoding="utf-8")
os.makedirs(os.path.join(us.DEPLOY_BASE, "teslausb-bak"), exist_ok=True)

removed, freed, skipped = us.prune_version_dirs(keep=5)
check("T1.1 删掉 2 个更早的版本（60/61）", sorted(removed) == ["teslausb-v0.3.1.60", "teslausb-v0.3.1.61"], removed)
check("T1.2 最近的 5 个都还在",
      all(os.path.isdir(dirs[v]) for v in vers[2:]), [v for v in vers[2:] if not os.path.isdir(dirs[v])])
check("T1.3 释放字节数为正", freed > 0, freed)
check("T1.4 非版本目录一个没动",
      all(os.path.exists(os.path.join(us.DEPLOY_BASE, d, "keep.txt"))
          for d in ("teslausb-web", "tim", "teslausb-vmanual-upload", "shared")))
check("T1.5 当前运行目录被保护（即使它最旧）", os.path.isdir(cur))

print("[T2] 版本目录清理：安全护栏")
old = mkver("0.3.1.10")
os.unlink(us.SYMLINK)
os.symlink(old, us.SYMLINK)          # 当前指向最旧的版本
removed2, _f2, skipped2 = us.prune_version_dirs(keep=1)
check("T2.1 当前运行目录永不被删", os.path.isdir(old), skipped2)
check("T2.2 其余旧版本被清掉", len(removed2) >= 4, removed2)
check("T2.3 keep<=0 时不动任何东西",
      us.prune_version_dirs(keep=0) == ([], 0, []) and os.path.isdir(old))

# 符号链接目录不参与清理
linkdir = os.path.join(us.DEPLOY_BASE, "teslausb-v0.3.1.5")
os.symlink(old, linkdir)
us.prune_version_dirs(keep=1)
check("T2.4 名字匹配但本身是 symlink 的条目被跳过", os.path.islink(linkdir))
os.unlink(linkdir)

print("[T3] 缩略图共享迁移")
d = mkver("0.3.1.70", nfiles=4)
ok, msg = us.ensure_shared_thumbnails(d)
link = os.path.join(d, "static", "thumbnails")
check("T3.1 迁移成功", ok, msg)
check("T3.2 变成指向共享目录的 symlink",
      os.path.islink(link) and os.path.realpath(link) == os.path.realpath(us.SHARED_THUMBNAILS), link)
check("T3.3 文件已进共享目录", len(os.listdir(us.SHARED_THUMBNAILS)) >= 4, os.listdir(us.SHARED_THUMBNAILS))
ok2, msg2 = us.ensure_shared_thumbnails(d)
check("T3.4 幂等：重复调用不改动", ok2 and msg2 == "已指向共享目录", msg2)

# 已有真实目录里混有共享目录已存在的同名文件 → 不覆盖、去重
d2 = mkver("0.3.1.71", nfiles=0)
Path(d2, "static", "thumbnails", "t0.jpg").write_text("DUPLICATE", encoding="utf-8")
Path(d2, "static", "thumbnails", "new.jpg").write_text("NEW", encoding="utf-8")
ok3, msg3 = us.ensure_shared_thumbnails(d2)
shared_t0 = Path(us.SHARED_THUMBNAILS, "t0.jpg").read_text(encoding="utf-8")
check("T3.5 同名文件不被覆盖（共享目录内容保持）", ok3 and shared_t0 != "DUPLICATE", shared_t0)
check("T3.6 新文件已并入", os.path.exists(os.path.join(us.SHARED_THUMBNAILS, "new.jpg")))

print("[T4] _save_user_data / _restore_user_data 保留 symlink（不再复制上万文件）")
d3 = mkver("0.3.1.72")
us.ensure_shared_thumbnails(d3)
tmpd, sc, sd, st = us._save_user_data(d3)
check("T4.1 返回 4 元组且 tmpd 指向 upgrade-keep-*",
      os.path.isdir(tmpd) and os.path.basename(tmpd).startswith("upgrade-keep-"), tmpd)
check("T4.2 缩略图备份是 symlink（不是 1.8G 拷贝）", st and os.path.islink(st), st)
target = os.path.join(us.DEPLOY_BASE, "teslausb-v0.3.1.99")
os.makedirs(os.path.join(target, "static"), exist_ok=True)
us._restore_user_data(target, sc, sd, st)
restored = os.path.join(target, "static", "thumbnails")
check("T4.3 恢复后仍是 symlink 且指向共享目录",
      os.path.islink(restored)
      and os.path.realpath(restored) == os.path.realpath(us.SHARED_THUMBNAILS), restored)
shutil.rmtree(tmpd, ignore_errors=True)

print("[T5] 遗留临时目录清理")
stale = Path(TMPROOT, "upgrade-keep-stale")
fresh = Path(TMPROOT, "upgrade-keep-fresh")
stale.mkdir(exist_ok=True)
fresh.mkdir(exist_ok=True)
old_t = time.time() - 3600
os.utime(stale, (old_t, old_t))
removed3 = us.cleanup_stale_temp(min_age=300)
check("T5.1 超龄目录被清掉", "upgrade-keep-stale" in removed3 and not stale.exists(), removed3)
check("T5.2 新目录保留（可能正在用）", fresh.exists())

print("[T6] 失败路径也不留临时目录（finally 生效）")
d4 = mkver("0.3.1.73")
if os.path.islink(us.SYMLINK):
    os.unlink(us.SYMLINK)
os.symlink(d4, us.SYMLINK)
bad = Path(TMPROOT, "broken.tar.gz")
bad.write_bytes(b"this is not a tar.gz")
before = {p.name for p in TMPROOT.iterdir() if p.name.startswith("upgrade-keep-")}
ok4, msg4 = us.do_upgrade_from_tarball(str(bad), "0.3.1.74")
after = {p.name for p in TMPROOT.iterdir() if p.name.startswith("upgrade-keep-")}
check("T6.1 损坏包升级失败", ok4 is False, msg4)
check("T6.2 失败后没有新增 upgrade-keep-*", after <= before, (before, after))
check("T6.3 版本目录未被创建/残留", not os.path.exists(os.path.join(us.DEPLOY_BASE, "teslausb-v0.3.1.74")))

print("[T7] 备份压缩包保留 5 个")
# T6 走了真实升级流程，_backup_current 会往 BAK_DIR 写一份当前版本备份；这里重建目录隔离
if os.path.isdir(us.BAK_DIR):
    shutil.rmtree(us.BAK_DIR)
os.makedirs(us.BAK_DIR, exist_ok=True)
for v in ["0.3.1.60", "0.3.1.61", "0.3.1.62", "0.3.1.63", "0.3.1.64", "0.3.1.65", "0.3.1.66"]:
    Path(us.BAK_DIR, "teslausb-v%s.tar.gz" % v).write_text("x", encoding="utf-8")
us._prune_bak()
left = sorted(f for f in os.listdir(us.BAK_DIR) if f.endswith(".tar.gz"))
check("T7.1 只保留最近 5 个压缩包", len(left) == 5, left)
check("T7.2 保留的是最新的 5 个",
      left == ["teslausb-v0.3.1.62.tar.gz", "teslausb-v0.3.1.63.tar.gz",
               "teslausb-v0.3.1.64.tar.gz", "teslausb-v0.3.1.65.tar.gz",
               "teslausb-v0.3.1.66.tar.gz"], left)

print("\n结果: %d 通过, %d 失败" % (PASS, FAIL))
print("沙箱（未清理）: %s" % BASE)
sys.exit(0 if FAIL == 0 else 1)
