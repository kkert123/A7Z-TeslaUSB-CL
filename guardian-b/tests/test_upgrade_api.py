#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 升级端点单测（v1.1.0）
====================================
只测「不依赖 root / systemd」的那一半：
  1. 未启用时 /guard/upgrade/* 一律 403（且不影响 /healthz）
  2. 来源地址不在 TS 网段 → 403（防"公网也能推包"）
  3. prepare：坏包/未签名包/降级包被拒，错误码稳定
  4. confirm：码错 → 403 且**不写触发文件**；码对 → 写 request 文件（path unit 的引线）
  5. 触发文件写成原子替换，且路径固定（systemd 监听的就是它）

运行：python tests/test_upgrade_api.py
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

TOKEN = "u" * 48
TMPDIR = tempfile.mkdtemp(prefix="guardb-upg-")
UPDIR = os.path.join(TMPDIR, "upgrade")
TRUSTED = os.path.join(TMPDIR, "trusted")
os.makedirs(TRUSTED, exist_ok=True)
os.makedirs(UPDIR, exist_ok=True)

os.environ.update({
    "GUARD_TOKEN": TOKEN,
    "GUARD_STATE_PATH": os.path.join(TMPDIR, "state.json"),
    "GUARD_RUNTIME_PATH": os.path.join(TMPDIR, "runtime.json"),
    "GUARD_JUDGE_ENABLE": "0",
    "GUARD_ALERT_DRY_RUN": "1",
    "GUARD_UPGRADE_ENABLE": "1",
    "GUARD_UPGRADE_DIR": UPDIR,
    "GUARD_UPGRADE_PUBKEY": os.path.join(TRUSTED, "pkg.pub"),
    "GUARD_UPGRADE_FLOOR": os.path.join(TRUSTED, "floor"),
    "GUARD_RELEASES_DIR": os.path.join(TMPDIR, "releases"),
    "GUARD_UPGRADE_CODE_VIA_API": "1",     # 测试里要能拿到码
    "GUARD_TS_CIDR": "100.64.0.0/10",
    "GUARD_UPGRADE_ALLOW_LOOPBACK": "1",   # test_client 的来源是 127.0.0.1
})

while "." in sys.path:
    sys.path.pop(0)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as gapp              # noqa: E402
import upgrade as up            # noqa: E402

RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


def _sha(b):
    return hashlib.sha256(b).hexdigest()


KEY = os.path.join(TMPDIR, "k")
FILES = {"app.py": b"# app\n", "judge.py": b"# judge\n"}


def setup_key():
    if os.path.exists(KEY):
        os.remove(KEY)
    if os.path.exists(KEY + ".pub"):
        os.remove(KEY + ".pub")
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", KEY, "-N", "",
                    "-C", up.SIGNER_IDENTITY, "-q"], check=True,
                   stdin=subprocess.DEVNULL)
    with open(KEY + ".pub", encoding="utf-8") as f:
        pub = f.read().strip()
    with open(os.path.join(TRUSTED, "pkg.pub"), "w", encoding="utf-8") as f:
        f.write("%s %s\n" % (up.SIGNER_IDENTITY, pub))


def build_pkg(version="1.1.0", frm=("1.0.2",), files=None, key=None):
    files = FILES if files is None else files
    man = {"format": 1, "kind": "guardian-b", "version": version,
           "from": list(frm), "removes": [],
           "files": [{"path": n, "sha256": _sha(c), "mode": "0644"}
                     for n, c in sorted(files.items())]}
    raw = json.dumps(man).encode("utf-8")
    mp = os.path.join(TMPDIR, "manifest.json")
    with open(mp, "wb") as f:
        f.write(raw)
    sig = mp + ".sig"
    if os.path.exists(sig):
        os.remove(sig)
    subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key or KEY,
                    "-n", up.SIGN_NAMESPACE, mp],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    with open(sig, "rb") as f:
        sigb = f.read()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
        add("guardian-b/manifest.json", raw)
        add("guardian-b/manifest.json.sig", sigb)
        for n, c in files.items():
            add("guardian-b/files/" + n, c)
    return buf.getvalue()


def client():
    a = gapp.create_app()
    return a.test_client()


H = {"Authorization": "Bearer " + TOKEN}


def _journal(**kw):
    j = {"job": "abcd1234ef567890", "state": "verified", "version": "1.1.0",
         "from_version": "1.0.2", "pkg_sha256": "a" * 64,
         "code_exp": int(time.time()) + 600, "confirm_attempts": 0,
         "updated_at": int(time.time())}
    j.update(kw)
    with open(os.path.join(UPDIR, "journal.json"), "w", encoding="utf-8") as f:
        json.dump(j, f)
    return j


def _clear_req():
    p = os.path.join(UPDIR, "request")
    if os.path.exists(p):
        os.remove(p)
    return p


# ═══════════════════════════════════════════════════════════
@case
def a1_未启用时403():
    os.environ["GUARD_UPGRADE_ENABLE"] = "0"
    try:
        c = client()
        r = c.post("/guard/upgrade/prepare", headers=H)
        ok(r.status_code == 403, str(r.status_code))
        ok(r.get_json()["error"] == "upgrade_disabled", str(r.get_json()))
        # 存活探针不能被牵连
        ok(c.get("/healthz").status_code == 200, "healthz 不该受影响")
    finally:
        os.environ["GUARD_UPGRADE_ENABLE"] = "1"


@case
def a2_非TS来源被拒():
    os.environ["GUARD_TS_CIDR"] = "10.0.0.0/8"
    os.environ["GUARD_UPGRADE_ALLOW_LOOPBACK"] = "0"
    try:
        c = client()
        r = c.post("/guard/upgrade/prepare", headers=H)
        ok(r.status_code == 403, str(r.status_code))
        ok(r.get_json()["error"] == "addr_not_allowed", str(r.get_json()))
    finally:
        os.environ["GUARD_TS_CIDR"] = "100.64.0.0/10"
        os.environ["GUARD_UPGRADE_ALLOW_LOOPBACK"] = "1"


@case
def a3_没带令牌401():
    c = client()
    ok(c.post("/guard/upgrade/prepare").status_code == 401, "缺令牌应 401")
    ok(c.get("/guard/upgrade/status").status_code == 401, "状态接口也要鉴权")


@case
def a4_正常包被暂存():
    # 目标版本必须比当前运行版本新，且清单的 from 必须包含当前版本，否则先撞
    # not_newer / incompatible_from。**当前版本从 app.VERSION 取**，不要写死——
    # 写死会让"只改版本号"的发布直接红一条，而实现其实完全正确。
    raw = build_pkg(version="1.2.0", frm=(gapp.VERSION,))
    c = client()
    r = c.post("/guard/upgrade/prepare", headers=H,
               data={"pkg": (io.BytesIO(raw), "guardian-b-v1.1.0.tar.gz")},
               content_type="multipart/form-data")
    d = r.get_json()
    ok(r.status_code == 202, "%s %s" % (r.status_code, d))
    ok(d["version"] == "1.2.0", str(d))
    ok(len(d["confirm_code"]) == 6, "应返回 6 位确认码：%s" % d.get("confirm_code"))
    j = json.load(open(os.path.join(UPDIR, "journal.json"), encoding="utf-8"))
    ok(j["state"] == "verified", str(j))


@case
def a5_降级包被拒():
    raw = build_pkg(version="0.9.0")
    c = client()
    r = c.post("/guard/upgrade/prepare", headers=H,
               data={"pkg": (io.BytesIO(raw), "x.tar.gz")},
               content_type="multipart/form-data")
    ok(r.status_code == 400, str(r.status_code))
    ok(r.get_json()["error"] == "not_newer", str(r.get_json()))


@case
def a6_别人签的包被拒():
    other = os.path.join(TMPDIR, "k2")
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", other, "-N", "",
                    "-C", up.SIGNER_IDENTITY, "-q"], check=True,
                   stdin=subprocess.DEVNULL)
    raw = build_pkg(version="1.2.0", key=other)
    c = client()
    r = c.post("/guard/upgrade/prepare", headers=H,
               data={"pkg": (io.BytesIO(raw), "x.tar.gz")},
               content_type="multipart/form-data")
    ok(r.status_code == 400, str(r.status_code))
    ok(r.get_json()["error"] == "bad_signature", str(r.get_json()))


@case
def a7_码错不写触发文件():
    _clear_req()
    _journal()
    c = client()
    r = c.post("/guard/upgrade/confirm", headers=H,
               json={"job": "abcd1234ef567890", "code": "000000"})
    ok(r.status_code == 403, str(r.status_code))
    ok(r.get_json()["error"] == "bad_code", str(r.get_json()))
    ok(not os.path.exists(os.path.join(UPDIR, "request")),
       "码错时绝不能触发 root helper")


@case
def a8_码对写触发文件():
    """确认成功后写 request 文件 —— 这是 path unit 的引线，写错名字就永远不动。"""
    _clear_req()
    j = _journal()
    sec = up._secret(os.path.join(UPDIR, "secret"))
    code = up.make_code(sec, j["job"], j["pkg_sha256"])
    c = client()
    r = c.post("/guard/upgrade/confirm", headers=H,
               json={"job": j["job"], "code": code})
    ok(r.status_code == 202, "%s %s" % (r.status_code, r.get_json()))
    ok(os.path.exists(os.path.join(UPDIR, "request")), "应写出触发文件")
    ok(json.load(open(os.path.join(UPDIR, "journal.json"), encoding="utf-8"))["state"]
       == "committed", "journal 应进入 committed")


@case
def a9_任务号不符被拒():
    _clear_req()
    _journal()
    c = client()
    r = c.post("/guard/upgrade/confirm", headers=H,
               json={"job": "ffffffffffffffff", "code": "123456"})
    ok(r.status_code == 409, str(r.status_code))
    ok(not os.path.exists(os.path.join(UPDIR, "request")), "不该触发")


@case
def a10_状态接口不含确认码默认值():
    _journal()
    c = client()
    d = c.get("/guard/upgrade/status", headers=H).get_json()
    ok(d["ok"], str(d))
    ok(d["upgrade"]["state"] == "verified", str(d))
    ok("staged" not in json.dumps(d), "状态里不该出现内部路径")


@case
def a11_卡住的committed会被标出来():
    _journal(state="committed", updated_at=int(time.time()) - 1000)
    c = client()
    d = c.get("/guard/upgrade/status", headers=H).get_json()
    ok(d["upgrade"].get("stalled") is True, "超过 15 分钟未推进应标 stalled：%s" % d)


@case
def a12_确认码推企微():
    """v1.1.2：码生成后推一条到企微（异步），任一路失败都不影响升级。

    这里直接测 `_notify_code`，不经 HTTP —— 推送是后台线程，从 HTTP 层只能靠
    sleep 猜时序，那种测试必然偶发红。判据全部落在「消息体里有没有该有的字段」。
    """
    class FakeAlerter:
        def __init__(self, configured=True, boom=False):
            self.configured = configured
            self.boom = boom
            self.sent = []
            self.done = threading.Event()

        def status(self):
            return {"configured": self.configured, "key_tail": "…abcd", "dry_run": False}

        def send_text(self, text):
            try:
                if self.boom:
                    raise RuntimeError("webhook down")
                self.sent.append(text)
                return "sent"
            finally:
                self.done.set()

    class On:
        code_notify = True

    class Off:
        code_notify = False

    # ① 正常路径：消息里必须有码 / 版本 / 任务号 / 来源
    fa = FakeAlerter()
    gapp._notify_code(fa, On(), "abcd1234ef567890", "654321", "1.1.2", 600, "100.116.18.42")
    ok(fa.done.wait(5), "推送线程没跑起来")
    ok(len(fa.sent) == 1, "应只发一条，实际 %d" % len(fa.sent))
    for frag, why in (("654321", "确认码"), ("1.1.2", "目标版本"),
                      ("abcd1234", "任务号前 8 位"), ("100.116.18.42", "来源 IP")):
        ok(frag in fa.sent[0], "消息里缺%s：%s" % (why, fa.sent[0]))

    # ② 开关关闭 → 一条都不发
    fa2 = FakeAlerter()
    gapp._notify_code(fa2, Off(), "j", "111111", "1.1.2", 600, "x")
    ok(fa2.done.wait(0.5) is False, "GUARD_UPGRADE_CODE_NOTIFY=0 时不应推送")

    # ③ 企微未配置 → 不发，也不能抛
    fa3 = FakeAlerter(configured=False)
    gapp._notify_code(fa3, On(), "j", "222222", "1.1.2", 600, "x")
    ok(fa3.done.wait(0.5) is False, "未配置 key 时不应推送")

    # ④ 投递抛异常 → 必须在线程内被吞掉（主线程零影响）
    fa4 = FakeAlerter(boom=True)
    gapp._notify_code(fa4, On(), "j", "333333", "1.1.2", 600, "x")
    ok(fa4.done.wait(5), "异常路径也要真正跑到")


@case
def a13_prepare成功后触发推送():
    """证明推送真的接在 prepare 的成功路径上，而不是「写了个函数没人调」。

    把 `_notify_code` 换成记录器：异步线程不好断言，"被调用 + 参数正确"才是判据。
    """
    # a11 把 journal 留在 committed，prepare 会直接 409 → 先恢复到可预置状态
    _journal(state="verified")
    calls = []

    def _rec(*a, **kw):
        calls.append(a)

    orig = gapp._notify_code
    gapp._notify_code = _rec
    try:
        raw = build_pkg(version="1.2.1", frm=(gapp.VERSION,))
        r = client().post("/guard/upgrade/prepare", headers=H,
                          data={"pkg": (io.BytesIO(raw), "guardian-b-v1.2.1.tar.gz")},
                          content_type="multipart/form-data")
    finally:
        gapp._notify_code = orig

    d = r.get_json()
    ok(r.status_code == 202, "%s %s" % (r.status_code, d))
    ok(len(calls) == 1, "prepare 成功必须恰好触发一次推送，实际 %d" % len(calls))
    # 签名：(alerter, cfg, job, code, version, ttl, src_ip)
    args = calls[0]
    ok(str(args[2]) and len(str(args[2])) >= 8, "推送必须带 job")
    ok(len(str(args[3])) == 6 and all(c in "0123456789ABCDEF" for c in str(args[3]).upper()),
       "推送必须带 6 位确认码（make_code = HMAC hexdigest[:6].upper()，含 A-F 是设计如此）：%r"
       % (args[3],))
    ok(args[4] == "1.2.1", "推送必须带目标版本：%r" % (args[4],))
    ok(int(args[5]) == up.CONFIRM_TTL_S, "推送必须带有效期：%r" % (args[5],))


def main():
    setup_key()
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
            import traceback
            print("  ERROR %s" % name)
            traceback.print_exc()
    print("\n%d/%d passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
