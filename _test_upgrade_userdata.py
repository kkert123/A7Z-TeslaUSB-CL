#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级时「根 config.json」搬运的回归用例（M149）

背景（2026-11-16 实证）：`_save_user_data` 一直只搬 `config/` 目录，**根 config.json 不在范围**。
历史上包内就是用户真实配置，覆盖后"看起来没变"，所以这个洞长期不可见。
v0.3.1.91 打包开始脱敏该文件（M148）→ 覆盖第一次产生后果：`auth.users` 被清空、
用户那次"改密码"没有落脚点。

本脚本钉住：
  1. 旧目录有 config.json → 升级后必须恢复成旧内容（含 auth.users）
  2. 旧目录没有 config.json → 不得改动目标文件（不制造空配置）
  3. 备份文件缺失 → 必须抛 UserDataError（不能静默跳过，否则又回到"配置被覆盖但零报错"）

运行： python _test_upgrade_userdata.py
输出： <通过>/<总数> passed
"""

import json
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import upgrade_service as us      # noqa: E402

TMP = tempfile.mkdtemp(prefix="updata-", dir=HERE)
RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


def _mk_tree(root, cfg_json=None, with_config_dir=True, cfg_name="config.json"):
    os.makedirs(root, exist_ok=True)
    if with_config_dir:
        os.makedirs(os.path.join(root, "config"), exist_ok=True)
        with open(os.path.join(root, "config", "sentry.json"), "w", encoding="utf-8") as f:
            json.dump({"wecom_guard_webhook_key": "abc"}, f)
    if cfg_json is not None:
        with open(os.path.join(root, cfg_name), "w", encoding="utf-8") as f:
            json.dump(cfg_json, f, ensure_ascii=False)
    return root


REAL = {"auth_enabled": False,
        "auth": {"users": {"admin": "scrypt:32768:8:1$AAAA$BBBB"}},
        "cleanup": {"disk_thresholds": {"warning": 85}}}
SANITIZED = {"auth_enabled": False,
             "auth": {"users": {}, "_sanitized": "打包时已清空 auth.users（真实哈希不入公开包）"},
             "cleanup": {"disk_thresholds": {"warning": 85}}}


@case
def t_root_config_preserved_across_upgrade():
    """旧配置有 auth.users → 升级恢复后必须还在（否则登录凭据被包内脱敏版覆盖）。"""
    old = _mk_tree(os.path.join(TMP, "old_a"), REAL)
    new = _mk_tree(os.path.join(TMP, "new_a"), SANITIZED)

    tmpd, cfg, data, thumbs, root = us._save_user_data(old)
    try:
        assert root and os.path.isfile(root), "根 config.json 没有被备份"
        us._restore_user_data(new, cfg, data, thumbs, root)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    with open(os.path.join(new, "config.json"), encoding="utf-8") as f:
        got = json.load(f)
    assert got["auth"]["users"]["admin"].startswith("scrypt:"), got
    assert "_sanitized" not in got["auth"], got
    assert got["cleanup"]["disk_thresholds"]["warning"] == 85, got


@case
def t_missing_old_config_keeps_target():
    """旧目录没有 config.json（全新安装/异常）→ 不得改动目标文件，更不能造空配置。"""
    old = _mk_tree(os.path.join(TMP, "old_b"), None)
    new = _mk_tree(os.path.join(TMP, "new_b"), SANITIZED)

    tmpd, cfg, data, thumbs, root = us._save_user_data(old)
    try:
        assert root is None, ("没有源文件却产生了备份路径", root)
        us._restore_user_data(new, cfg, data, thumbs, root)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    with open(os.path.join(new, "config.json"), encoding="utf-8") as f:
        got = json.load(f)
    assert "_sanitized" in got["auth"], ("无备份时不该动目标文件", got)


@case
def t_missing_backup_raises():
    """备份路径失效 → 必须抛 UserDataError（静默跳过会让"被覆盖"重回零报错）。"""
    new = _mk_tree(os.path.join(TMP, "new_c"), SANITIZED)
    ghost = os.path.join(TMP, "ghost.json")
    try:
        us._restore_user_data(new, None, None, None, ghost)
    except us.UserDataError as exc:
        assert "config.json" in str(exc), exc
        return
    raise AssertionError("备份文件不存在时必须抛 UserDataError")


@case
def t_restore_is_atomic_no_partial():
    """恢复后目标目录不得残留 .restore-* 临时文件（半截写入的痕迹）。"""
    old = _mk_tree(os.path.join(TMP, "old_d"), REAL)
    new = _mk_tree(os.path.join(TMP, "new_d"), SANITIZED)

    tmpd, cfg, data, thumbs, root = us._save_user_data(old)
    try:
        us._restore_user_data(new, cfg, data, thumbs, root)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    leftovers = [n for n in os.listdir(new) if ".restore-" in n]
    assert not leftovers, leftovers


def main():
    passed = 0
    total = len(RESULTS)
    print("TMP=%s" % TMP)
    for fn in RESULTS:
        name = fn.__name__
        try:
            fn()
            passed += 1
            print("  PASS  %s" % name)
        except AssertionError as exc:
            print("  FAIL  %s  -> %s" % (name, exc))
        except Exception:
            print("  ERROR %s" % name)
            traceback.print_exc()
    print("\n%d/%d passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
