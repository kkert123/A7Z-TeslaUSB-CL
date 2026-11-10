#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 车态板解析 + 锁车代发测试（v1.1.4）
================================================
背景：A7Z 离家后没网，B 端拿不到车态 → 无法「代发停车自检」。
TeslaMate 的 `/api/*` 全要 token（token 缓存在 A7Z 上），但**首页 HTML 是
LiveView 服务端渲染、免鉴权**，里面直接带 locked / sentry / status。

本测试钉死两件事：
  A. 解析只认图标 class（文案会随语言变），解析不出就是 None，绝不猜；
  B. 锁车是**事件**：只在「未锁→已锁」边沿记一次，宽限期后设备仍失联才代发，
     一把锁只代发一次，开锁复位。

跑法：  python3 tests/test_board.py
"""
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from board import parse_board        # noqa: E402
from config import Config            # noqa: E402
from judge import Judge              # noqa: E402
from state import StateStore         # noqa: E402

T = 60
WARN = 3 * T      # 180
ALARM = 5 * T     # 300
GRACE = 300

# 真实页面片段（2026-11-09 从 http://100.111.252.121:4000/ 抓下来的结构）
LOCKED_ON = ('<span class="icon" data-tooltip="Sentry Mode">'
             '<span class="mdi mdi-shield-check"></span></span>'
             '<span class="icon" data-tooltip="Locked">'
             '<span class="mdi mdi-lock"></span></span>')
UNLOCKED = ('<span class="icon" data-tooltip="Sentry Mode">'
            '<span class="mdi mdi-shield-off"></span></span>'
            '<span class="icon" data-tooltip="Unlocked">'
            '<span class="mdi mdi-lock-open-variant"></span></span>')
STATUS = ('<tr><td class="has-text-weight-medium">Status</td>'
          '<td> %s for 13 h<span class="duration-extended">, 26 min</span> </td></tr>')


def page(lock_block=LOCKED_ON, state="online"):
    return "<html><body><table>%s</table>%s</body></html>" % (lock_block, STATUS % state)


class FakeAlerter:
    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)
        return "sent"

    def send_markdown(self, content):
        self.sent.append(content)
        return "sent"


RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


def make_judge(**over):
    cfg = Config()
    cfg.heartbeat_interval = T
    cfg.warn_mult = 3
    cfg.alarm_mult = 5
    cfg.judge_interval = 30
    cfg.offline_default_s = 3600
    cfg.dedup_s = 1800
    cfg.unknown_vehicle = "judge"
    cfg.lock_grace_s = GRACE
    for k, v in over.items():
        setattr(cfg, k, v)
    tmp = tempfile.mkdtemp(prefix="guardb-board-")
    store = StateStore(os.path.join(tmp, "state.json"))
    al = FakeAlerter()
    return cfg, store, al, Judge(cfg, store, al)


PARKED = {"locked": True, "user_present": False, "sentry_mode": True}


def hb_body(vehicle=None):
    return {"seq": 1, "ts": 1000, "version": "0.3.1.83",
            "vehicle": vehicle if vehicle is not None else dict(PARKED)}


# ── A. 解析 ──────────────────────────────────────────────────
@case
def t_parse_locked_sentry_on():
    d = parse_board(page())
    assert d == {"locked": True, "sentry": True, "state": "online", "err": ""}, d


@case
def t_parse_unlocked_sentry_off():
    d = parse_board(page(UNLOCKED))
    assert d["locked"] is False and d["sentry"] is False, d


@case
def t_parse_driving_state():
    d = parse_board(page(LOCKED_ON, "driving"))
    assert d["state"] == "driving", d


@case
def t_parse_garbage_is_unknown():
    """模板变了 / 换语言 → 全 None，不能猜（猜错就是一条假告警）。"""
    for h in ("", "<html><body>Service Unavailable</body></html>", None):
        d = parse_board(h)
        assert d["locked"] is None and d["sentry"] is None, d
        assert d["err"], d


@case
def t_parse_lang_agnostic():
    """只认图标 class：把 tooltip 文案换成中文仍应解析出 locked。"""
    html = ('<span class="icon" data-tooltip="已锁定">'
            '<span class="mdi mdi-lock"></span></span>')
    # 中文文案匹配不到 Locked 正则 → 退回「锁图标扫描」不可得时就给 None，
    # 但绝不能把 locked 判成 False（那是「未锁」的确定结论）
    d = parse_board(html)
    assert d["locked"] is not False, d


# ── B. 锁车代发 ──────────────────────────────────────────────
@case
def t_lock_edge_arms_and_waits():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_board(parse_board(page(UNLOCKED)), now=1000)
    j.on_board(parse_board(page()), now=1060)          # 已锁
    assert store.data["lock_check"].get("at") == 1060, store.data["lock_check"]
    j.tick(now=1060 + GRACE - 10)                      # 宽限期内
    assert not [m for m in al.sent if "僚机代发" in m], al.sent


@case
def t_lock_relay_when_device_silent():
    cfg, store, al, j = make_judge()
    j.on_heartbeat("ts", 1, hb_body(), now=1000)
    j.on_board(parse_board(page(UNLOCKED)), now=1000)
    j.on_board(parse_board(page()), now=1060)
    j.tick(now=1060 + GRACE + 1)                       # 设备已静默 >> WARN
    relay = [m for m in al.sent if "僚机代发" in m]
    assert len(relay) == 1, al.sent
    # 代发已把「设备失联」一并告知，同一拍不得再推一条通用失联
    assert not [m for m in al.sent if "设备失联" in m.split("\n")[0]], al.sent
    txt = relay[0]
    assert "僚机代发" in txt and "已锁" in txt and "心跳" in txt, txt
    # 行宽：企微手机端约 26~28 列折行，本模板必须每行 ≤ LINE_MAX
    import judge as jm
    over = ["%d|%s" % (jm._disp_width(l), l) for l in txt.split("\n")
            if jm._disp_width(l) > jm.LINE_MAX]
    assert not over, "代发文案存在超宽行：%s" % over
    assert store.data["lock_check"]["done"] is True
    j.tick(now=1060 + GRACE + 100)                     # 同一把锁不重复
    assert len([m for m in al.sent if "僚机代发" in m]) == 1, al.sent


@case
def t_lock_relay_skipped_when_device_alive():
    cfg, store, al, j = make_judge()
    j.on_board(parse_board(page(UNLOCKED)), now=1000)
    j.on_board(parse_board(page()), now=1060)
    j.on_heartbeat("ts", 1, hb_body(), now=1060 + GRACE)     # 设备还活着
    j.tick(now=1060 + GRACE + 5)
    assert al.sent == [], al.sent
    assert store.data["lock_check"]["done"] is True
    assert store.data["lock_check"]["why"] == "device_alive"


@case
def t_unlock_resets_latch():
    """第二把锁要再报一次。"""
    cfg, store, al, j = make_judge()
    j.on_board(parse_board(page(UNLOCKED)), now=1000)
    j.on_board(parse_board(page()), now=1060)
    j.tick(now=1060 + GRACE + 1)
    assert len(al.sent) == 1
    j.on_board(parse_board(page(UNLOCKED)), now=2000)  # 开锁
    assert store.data["lock_check"] == {}
    j.on_board(parse_board(page()), now=2100)           # 再锁
    j.tick(now=2100 + GRACE + 1)
    assert len(al.sent) == 2, al.sent


@case
def t_stale_board_skips_relay():
    """车态板过期 → 不能拿旧「已锁」去编造一条自检。"""
    cfg, store, al, j = make_judge()
    j.on_board(parse_board(page(UNLOCKED)), now=1000)
    j.on_board(parse_board(page()), now=1060)
    j.tick(now=1060 + 1000)                            # 板已 >900s
    assert al.sent == [], al.sent
    assert store.data["lock_check"]["why"] == "board_stale"


@case
def t_source_unreachable_never_alarms():
    """抓不到页面（B 侧网络/模板问题）不得错误触发代发。"""
    cfg, store, al, j = make_judge()
    j.on_board({"locked": None, "sentry": None, "state": None, "err": "http_500"},
               now=1000)
    j.tick(now=1000 + GRACE + 100)
    assert al.sent == [], al.sent


def main():
    passed, total = 0, len(RESULTS)
    for fn in RESULTS:
        try:
            fn()
            passed += 1
            print("  PASS  %s" % fn.__name__)
        except AssertionError as exc:
            print("  FAIL  %s  -> %s" % (fn.__name__, exc))
        except Exception:
            print("  ERROR %s" % fn.__name__)
            traceback.print_exc()
    print("\n%d/%d passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
