#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 重启/关机接口的回归用例（M151）

背景（2026-11-16 实证）：`/api/system/reboot` 原来是
    subprocess.Popen(['sudo','shutdown','-r','+1'], stdout=DEVNULL, stderr=DEVNULL)
既不检查返回值、又把输出丢掉。sudo 因为 `shutdown` 不在 NOPASSWD 白名单里直接失败
（exit 1），前端却收到 `success: True 系统将在1分钟后重启` —— 用户等不到开机通知，
最后只能去车里断电。

本脚本不 import Flask（本机没有），而是用 AST 把 `_run_shutdown` /
`_request_reboot_via_watchdog` 的函数体提取出来，在注入 mock 的命名空间里**真实执行**，
从而验证：
  1. sudo 成功 → (True, '')，且命令行是 `sudo -n shutdown -r +1`
  2. sudo 被拒（exit 1）→ 必须返回 False，并把 stderr 带出来（不许静默）
  3. 超时 → 必须返回 False，不能挂住
  4. 兜底通道能写出合法的重启请求文件

运行： python _test_system_reboot_api.py
输出： <通过>/<总数> passed
"""

import ast
import json
import os
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "routes", "system_routes.py")
TMP = tempfile.mkdtemp(prefix="rebootapi-", dir=HERE)
RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


def _extract(name):
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise SystemExit("!! 在 %s 里找不到函数 %s" % (SRC, name))


class _FakeRun:
    """替代 subprocess.run，可指定返回码/输出/是否超时。"""

    def __init__(self, returncode=0, stderr="", stdout="", timeout=False):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        self.timeout = timeout
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if self.timeout:
            raise TimeoutExpired(cmd)
        outer = self

        class _R:
            returncode = outer.returncode
            stderr = outer.stderr
            stdout = outer.stdout
        return _R()


class TimeoutExpired(Exception):
    pass


class _FakeSubprocess:
    TimeoutExpired = TimeoutExpired

    def __init__(self, runner):
        self.run = runner


def _load_run_shutdown(runner):
    """把 _run_shutdown 放进注入了 mock 的命名空间里执行，返回可调用对象。"""
    ns = {"subprocess": _FakeSubprocess(runner), "time": time}
    exec(_extract("_run_shutdown"), ns)          # noqa: S102 —— 受控源码
    return ns["_run_shutdown"]


def _load_watchdog_writer(target_file):
    ns = {"os": os, "json": json, "time": time,
          "_REBOOT_REQUEST_FILE": target_file}
    exec(_extract("_request_reboot_via_watchdog"), ns)   # noqa: S102
    return ns["_request_reboot_via_watchdog"]


# ── 用例 ─────────────────────────────────────────────────────

@case
def t_sudo_success():
    """成功路径：返回 True，且命令行必须是 `sudo -n shutdown -r +1`。"""
    runner = _FakeRun(returncode=0)
    ok, detail = _load_run_shutdown(runner)("-r")
    assert ok is True and detail == "", (ok, detail)
    assert runner.calls == [["sudo", "-n", "shutdown", "-r", "+1"]], runner.calls


@case
def t_sudo_denied_is_reported():
    """sudo 要密码（就被拒）→ 必须返回 False 并带出原因，绝不能假成功。"""
    runner = _FakeRun(returncode=1, stderr="sudo: a password is required")
    ok, detail = _load_run_shutdown(runner)("-r")
    assert ok is False, "被拒却返回成功 —— 这正是 M151 的老 bug"
    assert "password" in detail.lower(), detail


@case
def t_timeout_does_not_hang():
    """命令超时 → 返回 False（进程不能挂住，否则前端永远转圈）。"""
    runner = _FakeRun(timeout=True)
    ok, detail = _load_run_shutdown(runner)("-h")
    assert ok is False and "超时" in detail, (ok, detail)


@case
def t_shutdown_flag_passed_through():
    """关机用 -h，且同样带 -n（快速失败而不是等密码）。"""
    runner = _FakeRun(returncode=0)
    _load_run_shutdown(runner)("-h")
    assert runner.calls == [["sudo", "-n", "shutdown", "-h", "+1"]], runner.calls


@case
def t_watchdog_fallback_writes_request():
    """兜底通道：写出的必须是被 watchdog 认可的结构（requested_at + reason）。"""
    target = os.path.join(TMP, "reboot_request.json")
    writer = _load_watchdog_writer(target)
    assert writer("web: sudo shutdown 失败，降级硬复位") is True
    assert os.path.isfile(target)
    d = json.load(open(target, encoding="utf-8"))
    assert isinstance(d.get("requested_at"), (int, float)), d
    assert "降级硬复位" in d.get("reason", ""), d
    assert not os.path.exists(target + ".web.tmp"), "临时文件未清理"


@case
def t_source_has_no_silent_popen():
    """静态守卫：真实调用里不得再有 `Popen(...sudo...)`，且必须存在 `run(...sudo...)`。

    只看 AST 的 Call 节点 —— 注释/docstring 里引用旧代码不该误伤。
    """
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)

    def calls(fn_names, needle):
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in fn_names:
                seg = ast.get_source_segment(src, node) or ""
                if needle in seg:
                    out.append(seg[:100].replace("\n", " "))
        return out

    bad = calls({"Popen"}, "sudo")
    assert not bad, "仍有不检查返回值的 Popen+sudo：\n" + "\n".join(bad)
    good = calls({"run"}, "sudo")
    assert good, "没有找到经过结果检查的 subprocess.run(['sudo', ...])"


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
