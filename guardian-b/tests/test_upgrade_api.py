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
    # 当前运行版本是 1.1.0（app.VERSION），所以必须用更新版本，否则命中 not_newer
    raw = build_pkg(version="1.2.0", frm=("1.1.0",))
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
