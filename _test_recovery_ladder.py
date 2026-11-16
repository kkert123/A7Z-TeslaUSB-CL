#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络自愈阶梯 + 看门狗网络判据 · 离线回归

对应 2026-11-15 实证：A7Z 断网 2h12m（10:44:41→12:57:56），
「nmcli 重连 + 强制刷 DHCP」重复 61 次全部无效，看门狗 717 行全是「健康检查通过」，
最终靠整车下电（A7Z 断电重启）恢复。

本脚本钉住四件事：
  1. watchdog：网络连续不可达 NET_FATAL_STREAK 次且护栏通过 → healthy=False
  2. watchdog：车在行驶时护栏必须否决（否则「开出地库没网」就重启）
  3. watchdog：4h 冷却戳生效（防 MIFI 本身没网导致重启风暴）
  4. wifi_service：5/10/15 次分别触发 网络栈重建 / 驱动重载 / 重启请求，且每档只一次

运行： python _test_recovery_ladder.py
输出： <通过>/<总数> passed
"""

import json
import logging
import os
import sys
import tempfile
import time
import traceback
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hardware_watchdog as hw      # noqa: E402
import wifi_service as ws           # noqa: E402

TMP = tempfile.mkdtemp(prefix="netfix-", dir=HERE)
RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


# ── 辅助 ─────────────────────────────────────────────────────

def _guard_snapshot(vehicle, name="guard_status.json"):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"vehicle": vehicle}, f)
    hw.GUARD_STATUS_FILE = p
    return p


def _mk_wd():
    """构造一个只测网络判据的看门狗（其余指标全部 mock 成健康）。"""
    wd = hw.HardwareWatchdog()
    wd.get_cpu_load = lambda: 10.0
    wd.get_memory_usage = lambda: {"percent": 40.0, "total_mb": 900,
                                   "used_mb": 360, "available_mb": 540}
    wd.get_disk_usage = lambda path="/": {"percent": 30, "total_gb": 20,
                                          "used_gb": 6, "free_gb": 14}
    wd.get_temperature = lambda: 45.0
    wd.check_service_status = lambda name: {"active": True, "status": "active",
                                           "details": {}}
    wd.check_web_service = lambda port=5000: True
    wd._save_status = lambda: None
    wd.check_network_connectivity = lambda: False
    return wd


class _FakeRun:
    """替代 subprocess.run，记录命令并一律成功。"""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()


# ── 看门狗：网络判据 ─────────────────────────────────────────

@case
def t_watchdog_net_fatal_after_streak():
    """连续不可达 30 次 + 护栏通过 → 判致命（旧实现永远 healthy）。"""
    _guard_snapshot({"state": "online", "speed": 0, "age_s": 5})
    hw.NET_REBOOT_STAMP_FILE = os.path.join(TMP, "stamp_a")
    if os.path.exists(hw.NET_REBOOT_STAMP_FILE):
        os.remove(hw.NET_REBOOT_STAMP_FILE)

    wd = _mk_wd()
    for i in range(hw.NET_FATAL_STREAK - 1):
        st = wd.run_health_check()
        assert st["healthy"] is True, ("第 %d 次不该判死" % (i + 1), st["issues"])

    st = wd.run_health_check()
    assert st["healthy"] is False, ("第 %d 次应判致命" % hw.NET_FATAL_STREAK,
                                    st["issues"])
    assert st["metrics"].get("net_fatal") is True, st["metrics"]
    assert st["metrics"].get("net_fail_streak") == hw.NET_FATAL_STREAK, st["metrics"]


@case
def t_watchdog_net_recovered_resets_streak():
    """ping 通一次必须归零，否则「断续抖动」会被累计成故障。"""
    _guard_snapshot({"state": "online", "speed": 0, "age_s": 5})
    hw.NET_REBOOT_STAMP_FILE = os.path.join(TMP, "stamp_b")
    wd = _mk_wd()
    for _ in range(hw.NET_FATAL_STREAK - 1):
        wd.run_health_check()
    wd.check_network_connectivity = lambda: True
    st = wd.run_health_check()
    assert st["metrics"]["net_fail_streak"] == 0, st["metrics"]
    assert st["healthy"] is True, st["issues"]


@case
def t_watchdog_net_fatal_blocked_while_driving():
    """车在行驶 → 护栏否决，永不因网络重启（车开出地库是常态）。"""
    _guard_snapshot({"state": "driving", "speed": 45, "age_s": 5})
    hw.NET_REBOOT_STAMP_FILE = os.path.join(TMP, "stamp_c")
    wd = _mk_wd()
    st = None
    for _ in range(hw.NET_FATAL_STREAK + 5):
        st = wd.run_health_check()
    assert st["healthy"] is True, st["issues"]
    assert "net_fatal_blocked" in st["metrics"], st["metrics"]
    assert "driving" in str(st["metrics"]["net_fatal_blocked"]), st["metrics"]


@case
def t_watchdog_net_fatal_blocked_by_stale_guard():
    """guard 快照过期（本地守护也停了）→ 无证据，不得判死。"""
    _guard_snapshot({"state": "online", "speed": 0, "age_s": 1200})
    hw.NET_REBOOT_STAMP_FILE = os.path.join(TMP, "stamp_d")
    wd = _mk_wd()
    st = None
    for _ in range(hw.NET_FATAL_STREAK + 2):
        st = wd.run_health_check()
    assert st["healthy"] is True, st["issues"]
    assert "过期" in str(st["metrics"].get("net_fatal_blocked", "")), st["metrics"]


@case
def t_watchdog_net_reboot_cooldown():
    """重启后 30min 防抖：刚因网络重启过 → 再断网也不重启。

    同时验证**旧格式兼容**：v0.3.1.90 初版写的是裸浮点时间戳（没有 ineffective 字段）。
    """
    _guard_snapshot({"state": "online", "speed": 0, "age_s": 5})
    stamp = os.path.join(TMP, "stamp_e")
    hw.NET_REBOOT_STAMP_FILE = stamp
    with open(stamp, "w") as f:
        f.write(str(time.time()))

    wd = _mk_wd()
    st = None
    for _ in range(hw.NET_FATAL_STREAK + 2):
        st = wd.run_health_check()
    assert st["healthy"] is True, st["issues"]
    assert "冷却" in str(st["metrics"].get("net_fatal_blocked", "")), st["metrics"]


# ── 重启冷却分级（方案 B：按「上次重启是否有效」）──────────────

@case
def t_net_gap_ladder():
    """冷却阶梯：0 次无效 30min / 1→60 / 2→120 / 3→24h 熔断（封顶）。"""
    hw.NET_REBOOT_STAMP_FILE = os.path.join(TMP, "rec_gap.json")
    wd = hw.HardwareWatchdog()
    expect = {0: 1800, 1: 3600, 2: 7200, 3: 86400, 5: 86400, 9: 86400}
    for ineff, want in expect.items():
        got = wd._net_reboot_gap({"at": time.time(), "ineffective": ineff})
        assert got == want, (ineff, got, want)


@case
def t_verify_reboot_effective_clears_counter():
    """重启后网络恢复 → ineffective 清零，但 at 保留（30min 防抖仍在）。"""
    p = os.path.join(TMP, "rec_ok.json")
    hw.NET_REBOOT_STAMP_FILE = p
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"at": time.time() - hw.NET_REBOOT_VERIFY_S - 10,
                   "ineffective": 2}, f)

    wd = hw.HardwareWatchdog()
    wd._verify_last_reboot(True)
    rec = wd._net_reboot_record()
    assert rec["ineffective"] == 0, rec
    assert rec["at"] > 0, rec
    assert wd._net_reboot_gap(rec) == hw.NET_REBOOT_MIN_GAP, rec


@case
def t_verify_reboot_ineffective_escalates():
    """重启后 10min 仍无网 → ineffective+1，冷却 30min → 60min。"""
    p = os.path.join(TMP, "rec_bad.json")
    hw.NET_REBOOT_STAMP_FILE = p
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"at": time.time() - hw.NET_REBOOT_VERIFY_S - 10,
                   "ineffective": 0}, f)

    wd = hw.HardwareWatchdog()
    wd._verify_last_reboot(False)
    rec = wd._net_reboot_record()
    assert rec["ineffective"] == 1, rec
    assert wd._net_reboot_gap(rec) == 3600, rec


@case
def t_verify_not_run_before_window():
    """重启后未满 10min → 不判定（避免把「还在启动」误记成无效重启）。"""
    p = os.path.join(TMP, "rec_early.json")
    hw.NET_REBOOT_STAMP_FILE = p
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"at": time.time() - 30, "ineffective": 1}, f)

    wd = hw.HardwareWatchdog()
    wd._verify_last_reboot(False)
    rec = wd._net_reboot_record()
    assert rec["ineffective"] == 1, rec          # 未判定，保持原值
    assert wd._reboot_verified is False, "判定窗口未到不得标记已判"


@case
def t_net_breaker_blocks_reboot():
    """连续 3 次无效 → 24h 熔断：即使再断网也不重启，只报「熔断」。"""
    _guard_snapshot({"state": "online", "speed": 0, "age_s": 5})
    p = os.path.join(TMP, "rec_break.json")
    hw.NET_REBOOT_STAMP_FILE = p
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"at": time.time() - 100,
                   "ineffective": hw.NET_REBOOT_MAX_INVALID}, f)

    wd = _mk_wd()
    st = None
    for _ in range(hw.NET_FATAL_STREAK + 2):
        st = wd.run_health_check()
    assert st["healthy"] is True, st["issues"]
    assert "熔断" in str(st["metrics"].get("net_fatal_blocked", "")), st["metrics"]


@case
def t_net_recovery_clears_invalid():
    """熔断期内网络自己恢复 → ineffective 清零，下次断网可正常重启。

    否则「一次误判罚一天」：熔断 24h 里网络恢复了，计数仍停在上限。
    """
    _guard_snapshot({"state": "online", "speed": 0, "age_s": 5})
    p = os.path.join(TMP, "rec_recover.json")
    hw.NET_REBOOT_STAMP_FILE = p
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"at": time.time() - 100,
                   "ineffective": hw.NET_REBOOT_MAX_INVALID}, f)

    wd = _mk_wd()                                   # 默认 check_network = False
    wd.run_health_check()                           # streak 断网累计
    rec_before = wd._net_reboot_record()
    assert rec_before["ineffective"] == hw.NET_REBOOT_MAX_INVALID, rec_before

    wd.check_network_connectivity = lambda: True    # 网络自己回来了
    wd.run_health_check()
    rec_after = wd._net_reboot_record()
    assert rec_after["ineffective"] == 0, rec_after
    assert rec_after["at"] > 0, rec_after           # at 保留 → 30min 防抖仍在


# ── WiFi 自愈阶梯 ────────────────────────────────────────────

def _fresh_ws():
    ws.RECOVERY_STAGE_FILE = os.path.join(TMP, "stage")
    ws.RECOVERY_REBOOT_STAMP = os.path.join(TMP, "reboot_at")
    ws.WIFI_REBOOT_REQUEST_FILE = os.path.join(TMP, "reboot_req.json")
    for p in (ws.RECOVERY_STAGE_FILE, ws.RECOVERY_REBOOT_STAMP,
              ws.WIFI_REBOOT_REQUEST_FILE):
        if os.path.exists(p):
            os.remove(p)
    obj = ws.WifiSmartSwitch.__new__(ws.WifiSmartSwitch)
    obj.log = logging.getLogger("t-ladder")
    obj.log.addHandler(logging.NullHandler())
    return obj


@case
def t_wifi_ladder_escalates_once_per_stage():
    """5→网络栈重建、10→驱动重载、15→重启请求；每档只执行一次。"""
    obj = _fresh_ws()
    fake = _FakeRun()
    with mock.patch.object(ws.subprocess, "run", fake), \
            mock.patch.object(ws.time, "sleep", lambda s: None):
        for c in range(1, 17):
            obj._escalate_recovery(c)

    cmds = [" ".join(c) for c in fake.calls]
    net = [c for c in cmds if "networking" in c]
    mod = [c for c in cmds if "modprobe" in c]

    assert len(net) == 2, ("网络栈只应重建一次（off+on），实际：%s" % net)
    assert any("networking" in c and "off" in c for c in net), net
    assert any("networking" in c and "on" in c for c in net), net
    assert any("modprobe" in c and "-r" in c for c in mod), mod
    assert any(ws.WIFI_DRIVER_MODULE in c and "-r" not in c.split("modprobe")[-1]
               for c in mod), mod
    assert os.path.exists(ws.WIFI_REBOOT_REQUEST_FILE), "未写重启请求文件"
    with open(ws.WIFI_REBOOT_REQUEST_FILE, encoding="utf-8") as f:
        req = json.load(f)
    assert "阶梯已用尽" in req.get("reason", ""), req
    assert obj._recovery_stage() == 3, obj._recovery_stage()


@case
def t_wifi_ladder_respects_cooldown():
    """2h 内第二次请求重启 → 必须被限流拒绝，且不改写请求文件。"""
    obj = _fresh_ws()
    fake = _FakeRun()
    with mock.patch.object(ws.subprocess, "run", fake), \
            mock.patch.object(ws.time, "sleep", lambda s: None):
        assert obj._request_reboot(15) is True
        first = os.path.getmtime(ws.WIFI_REBOOT_REQUEST_FILE)
        time.sleep(0.05)
        assert obj._request_reboot(16) is False, "冷却期内不应再次请求"
        assert os.path.getmtime(ws.WIFI_REBOOT_REQUEST_FILE) == first


@case
def t_wifi_driver_reload_failure_keeps_stage_low():
    """驱动加载失败 → 档位停在第 1 档（下轮重试），不得冒充成功。"""
    obj = _fresh_ws()

    class _Fail:
        def __call__(self, cmd, **kw):
            class _R:
                returncode = 1
                stdout = ""
                stderr = "module is in use"
            return _R()

    with mock.patch.object(ws.subprocess, "run", _Fail()), \
            mock.patch.object(ws.time, "sleep", lambda s: None):
        obj._escalate_recovery(10)
    assert obj._recovery_stage() == 1, obj._recovery_stage()


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
