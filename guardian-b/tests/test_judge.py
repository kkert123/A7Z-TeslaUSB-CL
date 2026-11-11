#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 判死状态机单测（离线可跑，无第三方依赖）
=====================================================
覆盖设计 §2 通道判别矩阵 + §3.3 预期离线 + §4 去重/恢复 + 车态抑制。

运行：  python3 tests/test_judge.py
输出：  "<通过>/<总数> passed"
"""

import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from config import Config          # noqa: E402
from state import StateStore       # noqa: E402
from judge import Judge            # noqa: E402

T = 60
WARN = 3 * T      # 180
ALARM = 5 * T     # 300


class FakeAlerter:
    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)
        return "sent"          # v1.0.2 起是三态字符串，不再是 bool

    def send_markdown(self, content):
        self.sent.append(content)
        return "sent"


def make_cfg(**over):
    cfg = Config()
    cfg.heartbeat_interval = T
    cfg.warn_mult = 3
    cfg.alarm_mult = 5
    cfg.judge_interval = 30
    cfg.offline_default_s = 3600
    cfg.dedup_s = 1800
    cfg.unknown_vehicle = "judge"
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def make_judge(cfg=None, tmpdir=None):
    cfg = cfg or make_cfg()
    tmpdir = tmpdir or tempfile.mkdtemp(prefix="guardb-test-")
    store = StateStore(os.path.join(tmpdir, "state.json"))
    alerter = FakeAlerter()
    return cfg, store, alerter, Judge(cfg, store, alerter)


PARKED = {"locked": True, "user_present": False, "sentry_mode": True}
DRIVING = {"locked": False, "user_present": True, "sentry_mode": False}


def hb_body(vehicle=None):
    return {"seq": 1, "ts": 1000, "version": "0.3.1.6x",
            "vehicle": vehicle if vehicle is not None else dict(PARKED)}


# ── 测试用例 ─────────────────────────────────────────────────
RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


@case
def t_fresh_ts_ok():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    ev = j.tick(now=1000 + 30)          # age_ts=30 < WARN
    assert ev[0]["type"] == "ok", ev
    assert al.sent == [], al.sent


@case
def t_ts_stale_https_fresh_is_ts_fault():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    # ts 静默 301s（>ALARM），https 刚更新
    j.on_heartbeat("https", 2, hb_body(), now=1301)
    ev = j.tick(now=1301)
    assert ev[0]["type"] == "alert" and ev[0]["fault"] == "ts_channel_fault", ev
    assert len(al.sent) == 1


@case
def t_both_stale_is_device_offline():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    ev = j.tick(now=1000 + ALARM + 5)
    assert ev[0]["type"] == "alert" and ev[0]["fault"] == "device_offline", ev


@case
def t_ts_fresh_https_stale_is_ok():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    j.on_heartbeat("ts", 2, hb_body(), now=1500)   # ts 新，https 老
    ev = j.tick(now=1500)
    assert ev[0]["type"] == "ok", ev
    assert al.sent == []


@case
def t_warn_band_no_alert():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    ev = j.tick(now=1000 + WARN + 10)     # 落在 [WARN, ALARM)
    assert ev[0]["type"] == "warn", ev
    assert al.sent == []


@case
def t_going_offline_window_silent():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, {"event": "going_offline",
                                "expected_until": 1000 + 3600,
                                "reason": "driving"}, now=1000)
    ev = j.tick(now=1000 + 600)           # 窗口内 → 静默
    assert ev[0]["type"] == "silent" and ev[0]["reason"] == "expected_offline", ev
    assert al.sent == []


@case
def t_going_offline_window_expired_judges():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, {"event": "going_offline",
                                "expected_until": 1000 + 100, "reason": "driving"}, now=1000)
    ev = j.tick(now=1000 + 100 + ALARM + 5)   # 窗口过期且心跳停 → 判死
    assert ev[0]["type"] == "alert", ev


@case
def t_going_offline_relative_window():
    """相对量 offline_s 优先：A 的墙钟未同步也不会算错窗口（P0-3 / M51）。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    # 故意给一个错的墙钟绝对时刻（快 2 小时），但带正确的相对量
    j.on_heartbeat("https", 1, {"event": "going_offline", "offline_s": 900,
                                "expected_until": 1000 + 7200,
                                "reason": "unlocked"}, now=1000)
    assert abs(float(store.data["offline_until"]) - (1000 + 900)) < 1, store.data["offline_until"]
    ev = j.tick(now=1000 + 800)
    assert ev[0]["type"] == "silent", ev


@case
def t_going_offline_absolute_clamped():
    """旧 A 只发 expected_until：坏钟不得把窗口设成过去或几年后。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, {"event": "going_offline",
                                "expected_until": 1, "reason": "driving"}, now=1000)
    lu = float(store.data["offline_until"])
    assert 1000 + 60 <= lu <= 1000 + 21600, lu

    j.on_heartbeat("https", 1, {"event": "going_offline",
                                "expected_until": 99999999999, "reason": "driving"}, now=1000)
    lu = float(store.data["offline_until"])
    assert lu <= 1000 + 21600, lu


@case
def t_single_channel_no_false_ts_fault():
    """单通道部署（只配 https）不得报「Tailscale 通道故障」。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    st = j.classify(now=1000 + ALARM + 5)
    assert st in ("device_offline", "warn", "ok"), st
    ev = j.tick(now=1000 + ALARM + 5)
    assert not any(e.get("type") == "alert" and e.get("fault") == "ts_channel_fault"
                   for e in ev), ev


@case
def t_going_offline_does_not_clobber_vehicle():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(PARKED), now=1000)
    j.on_heartbeat("ts", 2, {"event": "going_offline", "expected_until": 1000 + 60},
                   now=1001)
    assert store.data["last_body"]["vehicle"]["locked"] is True
    assert store.data["last_event"] == "going_offline"


@case
def t_user_present_suppresses_alert():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(DRIVING), now=1000)
    ev = j.tick(now=1000 + ALARM + 5)     # 车在开 → 不该在线 → 静默
    assert ev[0]["type"] == "silent" and ev[0]["reason"] == "not_expected_online", ev
    assert al.sent == []


@case
def t_unknown_vehicle_judge_vs_silent():
    # judge：未知车态仍判死
    cfg, store, al, j = make_judge(make_cfg(unknown_vehicle="judge"))
    j.on_heartbeat("ts", 1, {"seq": 1, "ts": 1}, now=1000)   # 无 vehicle
    ev = j.tick(now=1000 + ALARM + 5)
    assert ev[0]["type"] == "alert", ev
    # silent：未知车态静默
    cfg2, store2, al2, j2 = make_judge(make_cfg(unknown_vehicle="silent"))
    j2.on_heartbeat("ts", 1, {"seq": 1, "ts": 1}, now=1000)
    ev2 = j2.tick(now=1000 + ALARM + 5)
    assert ev2[0]["type"] == "silent" and ev2[0]["reason"] == "unknown_vehicle", ev2


@case
def t_dedup_within_window():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    t0 = 1000 + ALARM + 5
    ev1 = j.tick(now=t0)
    ev2 = j.tick(now=t0 + 30)             # 30s 后仍在去重窗内
    assert ev1[0]["type"] == "alert", ev1
    assert ev2[0]["type"] == "dedup", ev2
    assert len(al.sent) == 1


@case
def t_dedup_expired_realert():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    t0 = 1000 + ALARM + 5
    j.tick(now=t0)
    ev = j.tick(now=t0 + cfg.dedup_s + 1)   # 超去重窗 → 再推
    assert ev[0]["type"] == "alert", ev
    assert len(al.sent) == 2


@case
def t_recovery_after_ts_fault():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 2, hb_body(), now=1301)
    ev_first = j.tick(now=1301)
    assert ev_first[0]["fault"] == "ts_channel_fault"
    # ts 恢复
    j.on_heartbeat("ts", 3, hb_body(), now=1400)
    ev = j.tick(now=1400)
    types = [e["type"] for e in ev]
    assert "recover" in types, ev
    assert any("恢复" in m for m in al.sent), al.sent


@case
def t_recovery_then_relapse_no_spam():
    """同一故障恢复后，去重窗内复发不得再推（「恢复+告警」不抖成两条）。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 2, hb_body(), now=1301)
    j.tick(now=1301)                       # alert #1（ts 故障）
    j.on_heartbeat("ts", 3, hb_body(), now=1400)
    ev_rec = j.tick(now=1400)              # 恢复
    assert any(e["type"] == "recover" for e in ev_rec), ev_rec
    n = len(al.sent)
    j.on_heartbeat("https", 4, hb_body(), now=1700)   # https 保持新鲜
    ev = j.tick(now=1700)                  # ts 又静默 300s → 同故障复发，仍在该去重窗内
    assert ev[0]["type"] == "dedup", ev
    assert len(al.sent) == n, al.sent


@case
def t_fault_escalates_ts_to_device_alerts():
    """TS 故障持续恶化到双通道皆死 → 属「新故障」，应升级告警（不是去重）。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 2, hb_body(), now=1301)
    j.tick(now=1301)                       # ts 故障
    ev = j.tick(now=1301 + ALARM + 5)      # https 也死 → device_offline
    assert ev[0]["type"] == "alert" and ev[0]["fault"] == "device_offline", ev
    assert len(al.sent) == 2


@case
def t_last_seen_uses_b_clock_not_a_ts():
    """A 的 ts 乱填（未来/过去）不得影响判死——B 只认自己的接收时刻。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, {"seq": 1, "ts": 9999999999, "vehicle": dict(PARKED)},
                   now=5000)
    ev = j.tick(now=5000 + 10)             # 距 B 接收仅 10s → ok
    assert ev[0]["type"] == "ok", ev


@case
def t_state_persists_across_restart():
    tmp = tempfile.mkdtemp(prefix="guardb-persist-")
    cfg = make_cfg()
    store = StateStore(os.path.join(tmp, "state.json"))
    al = FakeAlerter()
    j = Judge(cfg, store, al)
    j.on_heartbeat("ts", 7, hb_body(), now=1000)
    # 模拟重启：新 Store 读同一文件
    store2 = StateStore(os.path.join(tmp, "state.json"))
    assert store2.data["last_seen"]["ts"] == 1000, store2.data["last_seen"]
    assert store2.data["last_seq"]["ts"] == 7


@case
def t_classify_matrix():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1000)
    assert j.classify(now=1010) == "ok"
    assert j.classify(now=1000 + WARN + 10) == "warn"
    # ts 死、https 新
    j.on_heartbeat("https", 2, hb_body(), now=1400)
    assert j.classify(now=1400) == "ts_channel_fault"
    # 双死
    assert j.classify(now=1400 + ALARM + 5) == "device_offline"


@case
def t_test_heartbeat_does_not_pollute():
    """手工/联调心跳不得计入在线判定（2026-11-08 现场：manual-test 造成整夜误报）"""
    cfg, store, al, j = make_judge()
    body = {"seq": 1, "version": "manual-test", "vehicle": dict(PARKED)}  # 故意带「该在线」
    res = j.on_heartbeat("ts", 1, body, now=1000)
    assert res.get("ignored") is True, res
    assert store.data["last_seen"]["ts"] == 0.0, store.data["last_seen"]
    assert store.data["last_body"] is None, store.data["last_body"]
    assert store.data["last_test"]["version"] == "manual-test", store.data.get("last_test")
    ev = j.tick(now=1000 + ALARM + 60)
    assert all(e["type"] != "alert" for e in ev), ev
    assert al.sent == [], al.sent


@case
def t_test_flag_true_also_ignored():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, {"seq": 1, "test": True, "version": "0.3.1.77"}, now=1000)
    assert store.data["last_seen"]["ts"] == 0.0, store.data["last_seen"]
    assert j.tick(now=1000 + ALARM + 60)[0]["type"] == "ok"


@case
def t_normal_version_still_counts():
    """别把正常的 0.3.1.77 误判成测试心跳"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    assert store.data["last_seen"]["ts"] == 1000.0, store.data["last_seen"]
    assert store.data["last_body"]["version"] == "0.3.1.6x"


@case
def t_alert_text_is_human_readable():
    """告警文案：人话时长、未启用通道不印 epoch、带版本、带建议"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.tick(now=1000 + 16 * 3600)
    assert al.sent, "没有发出告警"
    txt = al.sent[0]
    assert "1794124" not in txt, "把 epoch 当年龄印出来了：%s" % txt
    assert "小时前" in txt, txt
    assert "0.3.1.6x" in txt, txt
    assert "建议" in txt, txt


@case
def t_ts_fault_text_says_device_alive():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_heartbeat("https", 1, hb_body(), now=1300)
    j.tick(now=1330)                    # ts 330s 老，https 30s 新 → TS 通道故障
    assert al.sent, "没有发出告警"
    txt = al.sent[0]
    assert "TS" in txt and "设备本体在线" in txt, txt
    # 标题必须标明发送方是僚机（A 端发的是【A7Z】…，两边共用同一个企微机器人）
    assert txt.startswith("【僚机】"), txt


@case
def t_human_age_units():
    from judge import _human_age
    assert _human_age(30) == "30 秒前", _human_age(30)
    assert _human_age(600) == "10 分钟前", _human_age(600)
    assert "小时前" in _human_age(58118), _human_age(58118)
    assert "天前" in _human_age(3 * 86400), _human_age(3 * 86400)
    assert _human_age(None) == "未知", _human_age(None)


DRIVING_STATE = {"locked": True, "user_present": True, "sentry_mode": False,
                 "state": "driving", "speed": 43.0, "shift_state": "D"}
SLEEPING = {"locked": True, "user_present": False, "sentry_mode": False,
            "state": "asleep", "shift_state": "P"}


@case
def t_driving_evidence_expires_to_alert():
    """M135 · 2026-11-09 现场：A 在 43km/h 上断掉，B 必须最终判死。

    旧实现把「A 死前最后一帧：车在开」当永久现状 → 无限期静默，
    1h38m 后 `alerts.device_offline.last_sent` 仍是 0.0。
    """
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(DRIVING_STATE), now=1000)
    # 证据还新鲜（< offline_default_s）→ 仍静默，别把短时网络抖动报成失联
    ev = j.tick(now=1000 + ALARM + 5)
    assert ev[0]["type"] == "silent" and ev[0]["why"] == "driving", ev
    assert al.sent == []
    # 超过证据保质期 → 必须告警
    ev = j.tick(now=1000 + cfg.offline_default_s + 10)
    assert ev[0]["type"] == "alert" and ev[0]["fault"] == "device_offline", ev
    assert len(al.sent) == 1


@case
def t_driving_why_beats_user_present():
    """车在开时 `why` 必须是 driving（不能是 user_present），否则 TTL 永远不生效。"""
    from judge import _classify_vehicle
    assert _classify_vehicle({"vehicle": DRIVING_STATE}) == (False, "driving")
    # 速度够大、state 缺失也算在开
    assert _classify_vehicle({"vehicle": {"speed": 20.0}}) == (False, "driving")
    # 停着人不走 → 仍是 user_present（长期静默是合法的）
    assert _classify_vehicle(
        {"vehicle": {"locked": False, "user_present": True,
                     "sentry_mode": False}}) == (False, "user_present")


@case
def t_sleeping_car_stays_silent_forever():
    """车睡了、A 随车机断电 → 正常现象，不得因加了保质期就每晚误报。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(SLEEPING), now=1000)
    for now in (1000 + ALARM + 5,
                1000 + cfg.offline_default_s + 10,
                1000 + 20 * 3600):
        ev = j.tick(now=now)
        assert ev[0]["type"] == "silent", (now, ev)
    assert al.sent == []


@case
def t_classify_driving_expired_is_device_offline():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(DRIVING_STATE), now=1000)
    assert j.classify(now=1000 + ALARM + 5) == "not_expected_online"
    assert j.classify(now=1000 + cfg.offline_default_s + 10) == "device_offline"


@case
def t_alert_backoff_escalates():
    """重复告警间隔递增：30min → 2h → 6h，恢复后归零。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    t0 = 1000 + ALARM + 5
    assert j.tick(now=t0)[0]["type"] == "alert"
    assert j.tick(now=t0 + 30)[0]["type"] == "dedup"
    assert j.tick(now=t0 + 1801)[0]["type"] == "alert"      # 第 2 条：+30min
    ev = j.tick(now=t0 + 1801 + 1801)                        # 第 2 条之后 30min 还不够
    assert ev[0]["type"] == "dedup", ev
    assert j.tick(now=t0 + 1801 + 7201)[0]["type"] == "alert"  # 第 3 条：再 +2h
    assert len(al.sent) == 3


@case
def t_alert_text_shows_last_vehicle():
    """告警要回答「车到底锁了没」——只写设备失联等于没说。"""
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(DRIVING_STATE), now=1000)
    j.tick(now=1000 + cfg.offline_default_s + 10)
    txt = al.sent[0]
    assert "车态" in txt and "在开 43km/h" in txt and "已锁" in txt, txt


@case
def t_alert_text_no_overlong_lines():
    """所有推送文案每行 ≤ LINE_MAX。

    企微手机端一行约 26~28 列就强行折行；2026-11-10 现场一份 8 行的停车自检
    被折成 14 行碎块。行宽是"渲染末端"的责任，必须由测试钉住，不能靠每处自己数。
    """
    import judge as jm
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(DRIVING_STATE), now=1000)
    j.tick(now=1000 + cfg.offline_default_s + 10)      # ① 设备失联
    _c2, _s2, al2, j2 = make_judge()
    j2.on_heartbeat("ts", 1, hb_body(), now=1000)
    j2.on_heartbeat("https", 2, hb_body(), now=1000 + 3600)
    j2.tick(now=1000 + 4000)                           # ② TS 通道故障
    j2.tick(now=1000 + 4000 + cfg.warn_s + 1)          # ③ 恢复
    bad = []
    for m in list(al.sent) + list(al2.sent):
        for ln in m.split("\n"):
            if jm._disp_width(ln) > jm.LINE_MAX:
                bad.append("%d|%s" % (jm._disp_width(ln), ln))
    assert not bad, "存在超宽行（会在手机上折行）：\n" + "\n".join(bad)


def main():
    passed = 0
    total = len(RESULTS)
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
