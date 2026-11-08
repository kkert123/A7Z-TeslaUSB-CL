#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 端 · 僚机版本同步测试
========================
覆盖：peer 请求层错误分类、A 端预验签、包库存、升级窗口对「B 失联」告警的抑制、
      以及四个新路由的返回形状。

运行：  python _test_guard_peer.py
依赖：  宿主机 ssh-keygen；requests（与线上一致）
"""

import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="peer-")

# 本机可能配了 HTTP_PROXY：会把 127.0.0.1:1 的"拒绝连接"变成代理的 5xx，
# 让"连不上"用例假失败（实测踩过）。测试内一律直连。
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import guard_client as gc          # noqa: E402

# 把全部落盘路径改到临时目录（绝不能碰真实 config/ 与 data/）
gc.CONFIG_PATH = os.path.join(TMP, "guard.json")
gc.STATUS_PATH = os.path.join(TMP, "guard_status.json")
gc.QUEUE_PATH = os.path.join(TMP, "guard_queue.json")
gc.PKG_DIR = os.path.join(TMP, "guard_pkg")
gc.UPGRADE_WINDOW_PATH = os.path.join(TMP, "window.json")

RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


# ── 假 B 端 ───────────────────────────────────────────────────
class _H(http.server.BaseHTTPRequestHandler):
    routes = {}          # path -> (status, payload)

    def do_GET(self):
        self._serve()

    def do_POST(self):
        self._serve()

    def _serve(self):
        path = self.path.split("?")[0]
        st, payload = self.routes.get(path, (404, {"ok": False, "error": "not_found"}))
        body = json.dumps(payload).encode("utf-8")
        self.send_response(st)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _TS(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_fake():
    srv = _TS(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


SRV, BASE = start_fake()


def cfg_for(base=BASE, token="t" * 32):
    return gc.normalize_config({"enabled": True, "token": token,
                                "endpoints": {"ts": base, "https": ""}})


# ═══════════════════════════════════════════════════════════
# A. peer 请求层：错误必须分类，不能一律「失败」
# ═══════════════════════════════════════════════════════════
@case
def a1_未配通道():
    c = gc.normalize_config({"enabled": True, "token": "x" * 32})
    okc, _, err = gc.peer_call(c, "GET", "/guard/status")
    ok(not okc and err == "no_endpoint", err)


@case
def a2_令牌为空():
    c = gc.normalize_config({"enabled": True, "endpoints": {"ts": BASE}})
    okc, _, err = gc.peer_call(c, "GET", "/guard/status")
    ok(not okc and err == "no_token", err)


@case
def a3_成功透传payload():
    _H.routes = {"/guard/status": (200, {"ok": True, "version": "1.1.0"})}
    okc, p, err = gc.peer_call(cfg_for(), "GET", "/guard/status")
    ok(okc and p.get("version") == "1.1.0", "%s %s" % (okc, err))


@case
def a4_403禁用_原样带回错误码():
    _H.routes = {"/guard/upgrade/prepare": (403, {"ok": False, "error": "upgrade_disabled"})}
    okc, _, err = gc.peer_call(cfg_for(), "POST", "/guard/upgrade/prepare")
    ok(not okc and err == "upgrade_disabled", err)


@case
def a5_401归类为auth():
    _H.routes = {"/guard/status": (401, {"ok": False, "error": "unauthorized"})}
    okc, p, err = gc.peer_call(cfg_for(), "GET", "/guard/status")
    # 形如 "ts=unauthorized" —— 通道前缀是有意保留的（诊断时要知道是哪条通道挂了）
    ok(not okc and err.split("=")[-1] in ("auth", "unauthorized"), err)


@case
def a6_500归类为b_5xx():
    _H.routes = {"/guard/status": (500, {"ok": False})}
    okc, _, err = gc.peer_call(cfg_for(), "GET", "/guard/status")
    ok(not okc and err.split("=")[-1] == "b_5xx", err)


@case
def a7_连不上归类unreachable():
    c = cfg_for(base="http://127.0.0.1:1")     # 必然拒绝
    okc, _, err = gc.peer_call(c, "GET", "/guard/status", timeout=2)
    ok(not okc and err.split("=")[-1] == "unreachable", err)


@case
def a8_https端点非https被拦():
    c = gc.normalize_config({"enabled": True, "token": "x" * 32,
                             "endpoints": {"https": "http://127.0.0.1:1"}})
    okc, _, err = gc.peer_call(c, "GET", "/guard/status")
    ok(not okc and "https_scheme_required" in err, err)


# ═══════════════════════════════════════════════════════════
# B. A 端预验签
# ═══════════════════════════════════════════════════════════
KEY = os.path.join(TMP, "k")
PKG = os.path.join(TMP, "guardian-b-v1.1.0.tar.gz")


def _pack():
    """复用官方打包工具产出真包（保证 A 端验签与 B 端一致）。"""
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", KEY, "-N", "",
                    "-C", gc.PKG_SIGNER_IDENTITY, "-q"], check=True,
                   stdin=subprocess.DEVNULL)
    src = os.path.join(HERE, "guardian-b")
    out = os.path.join(TMP, "dist")
    r = subprocess.run([sys.executable, os.path.join(src, "tools", "pack.py"),
                        "--version", "1.1.0", "--from", "1.0.0,1.0.1,1.0.2",
                        "--key", KEY, "--out", out, "--src", src],
                       capture_output=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise AssertionError("pack 失败：%s" % (r.stderr or b"")[:300])
    shutil.copyfile(os.path.join(out, "guardian-b-v1.1.0.tar.gz"), PKG)
    with open(KEY + ".pub", "r", encoding="utf-8") as f:
        return f.read().strip()


PUBKEY = ""


@case
def b1_好包验签通过():
    global PUBKEY
    PUBKEY = _pack()
    c = gc.normalize_config({"pkg_pubkey": PUBKEY})
    with open(PKG, "rb") as f:
        raw = f.read()
    okc, man, err = gc.verify_package(raw, c)
    ok(okc, err)
    ok(man["version"] == "1.1.0", man["version"])
    ok(len(man["files"]) >= 5, str(len(man["files"])))


@case
def b2_未配公钥拒绝():
    c = gc.normalize_config({})
    with open(PKG, "rb") as f:
        okc, _, err = gc.verify_package(f.read(), c)
    ok(not okc and err == "no_pkg_pubkey", err)


@case
def b3_别人的key签的包被拒():
    other = os.path.join(TMP, "other")
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", other, "-N", "",
                    "-C", gc.PKG_SIGNER_IDENTITY, "-q"], check=True,
                   stdin=subprocess.DEVNULL)
    src = os.path.join(HERE, "guardian-b")
    out = os.path.join(TMP, "dist2")
    subprocess.run([sys.executable, os.path.join(src, "tools", "pack.py"),
                    "--version", "1.2.0", "--from", "1.0.0", "--key", other,
                    "--out", out, "--src", src], capture_output=True,
                   stdin=subprocess.DEVNULL)
    c = gc.normalize_config({"pkg_pubkey": PUBKEY})
    with open(os.path.join(out, "guardian-b-v1.2.0.tar.gz"), "rb") as f:
        okc, _, err = gc.verify_package(f.read(), c)
    ok(not okc and err == "bad_signature", err)


@case
def b4_篡改manifest被拒_载荷篡改由B端兜():
    """A 端预验签的**边界**必须写清楚：

    A 只验 manifest 的签名，不逐文件核 hash（那是 B 的职责，也是唯一有意义的
    校验点——A 验过了 B 还要再验一遍，重复没收益）。
    所以：
      · 改 manifest  → A 就该拒（签名对不上）
      · 改载荷文件   → A 可能放过（它没读那个文件），但 B 必然拒绝（hash_mismatch）
    这条用例把第一种钉死，第二种由 guardian-b/tests/test_upgrade.py b4 钉死。
    """
    import io as _io
    import tarfile as _tarfile
    with _tarfile.open(PKG, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            data = tf.extractfile(m).read() if m.isfile() else b""
            if m.name.endswith("manifest.json"):
                # 同长度替换：只改内容不改结构，防止"因为长度变了所以被拒"的假通过
                data = data.replace(b'"1.1.0"', b'"9.9.9"')
            members.append((m.name, data, m.type, m.mode, m.linkname))
    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, typ, mode, link in members:
            ti = _tarfile.TarInfo(name)
            ti.type = typ
            ti.mode = mode
            if typ == _tarfile.REGTYPE:
                ti.size = len(data)
                tf.addfile(ti, _io.BytesIO(data))
            else:
                ti.linkname = link
                tf.addfile(ti)
    c = gc.normalize_config({"pkg_pubkey": PUBKEY})
    okc, _, err = gc.verify_package(buf.getvalue(), c)
    ok(not okc and err == "bad_signature", err)


@case
def b5_超上限拒绝():
    c = gc.normalize_config({"pkg_pubkey": PUBKEY})
    okc, _, err = gc.verify_package(b"\0" * (gc.PKG_MAX_BYTES + 1), c)
    ok(not okc and err == "pkg_too_large", err)


@case
def b6_非gzip拒绝():
    c = gc.normalize_config({"pkg_pubkey": PUBKEY})
    okc, _, err = gc.verify_package(b"not a tar", c)
    ok(not okc and err in ("no_manifest", "bad_manifest"), err)


# ═══════════════════════════════════════════════════════════
# C. 包库存
# ═══════════════════════════════════════════════════════════
@case
def c1_落盘并列出():
    c = gc.normalize_config({"pkg_pubkey": PUBKEY})
    with open(PKG, "rb") as f:
        okc, err, info = gc.save_local_package(f.read(), c)
    ok(okc, err)
    ok(info["version"] == "1.1.0", str(info))
    items = gc.list_local_packages()
    ok(len(items) == 1 and items[0]["version"] == "1.1.0", str(items))


@case
def c2_按版本倒序():
    # 造两个假包（只测排序，不测验签）
    os.makedirs(gc.PKG_DIR, exist_ok=True)
    for v in ("1.0.9", "1.2.10"):
        with open(os.path.join(gc.PKG_DIR, "guardian-b-v%s.tar.gz" % v), "wb") as f:
            f.write(b"x")
    items = gc.list_local_packages()
    ok(items[0]["version"] == "1.2.10", "1.2.10 应排最前：" + str([i["version"] for i in items]))
    ok(items[-1]["version"] == "1.0.9", "1.0.9 应排最后")


# ═══════════════════════════════════════════════════════════
# D. 升级窗口抑制假告警（本次最关键的行为）
# ═══════════════════════════════════════════════════════════
@case
def d1_窗口开合():
    gc.close_upgrade_window()
    ok(gc.in_upgrade_window() == 0, "初始应为 0")
    gc.open_upgrade_window(60)
    ok(gc.in_upgrade_window() > 50, str(gc.in_upgrade_window()))
    gc.close_upgrade_window()
    ok(gc.in_upgrade_window() == 0, "关闭后应为 0")


@case
def d2_窗口内ping失败不判失联():
    gc.close_upgrade_window()
    gc.open_upgrade_window(300)
    s = gc.GuardSender(cfg_for(BASE, "t" * 32))
    s.cfg["ping_fail_threshold"] = 2
    pushed = []
    gc._push_wecom = lambda t: (pushed.append(t), (True, ""))[1]
    for _ in range(6):
        s._ping_failed("timeout")
    ok(not s._b_down_notified, "窗口内不应判「B 侧失联」")
    ok(not pushed, "窗口内不应推送：%s" % pushed)
    ok(s._ping_fail == 6, "计数仍应累计（窗口结束后立刻能判）：%d" % s._ping_fail)
    gc.close_upgrade_window()


@case
def d3_窗口结束后照常判失联():
    gc.close_upgrade_window()
    s = gc.GuardSender(cfg_for(BASE, "t" * 32))
    s.cfg["ping_fail_threshold"] = 2
    s._ping_fail = 5           # 模拟窗口里已经攒了 5 次
    pushed = []
    gc._push_wecom = lambda t: (pushed.append(t), (True, ""))[1]
    s._ping_failed("timeout")
    ok(s._b_down_notified, "窗口外应判失联")
    ok(len(pushed) == 1 and "失联" in pushed[0], str(pushed))


@case
def d2b_窗口内auth仍要报警():
    """窗口只该抑制**传输层**失败。

    「令牌不匹配」跟"B 在重启"没有因果关系：升级后恰好看不出令牌被换掉，
    是最糟的时机（那份配置很可能就是升级前刚改的）。
    """
    gc.close_upgrade_window()
    gc.open_upgrade_window(300)
    s = gc.GuardSender(cfg_for(BASE, "t" * 32))
    s.cfg["ping_fail_threshold"] = 2
    pushed = []
    gc._push_wecom = lambda t: (pushed.append(t), (True, ""))[1]
    s._ping_failed("auth")
    s._ping_failed("auth")
    ok(s._b_down_notified, "令牌错在升级窗口内也必须报")
    ok(len(pushed) == 1, str(pushed))
    gc.close_upgrade_window()


@case
def d5_确认失败必须关窗():
    """确认失败（码错/过期）时 B 不会重启，窗口留着就是白给 15 分钟不告警。"""
    gc.close_upgrade_window()
    _H.routes = {"/guard/upgrade/confirm": (403, {"ok": False, "error": "bad_code"})}
    okc, _, err = gc.peer_confirm(cfg_for(), job="abc12345", code="000000")
    ok(not okc and err == "bad_code", err)
    ok(gc.in_upgrade_window() == 0, "确认失败后不应留下抑制窗口")


@case
def d6_快照带窗口字段供仪表盘显示():
    s = gc.GuardSender(cfg_for(BASE, "t" * 32))
    st = s.stats()
    ok("upgrade_window" in st, "缺 upgrade_window 字段")
    gc.open_upgrade_window(60)
    ok(s.stats()["upgrade_window"] > 0, "窗口内应 > 0")
    gc.close_upgrade_window()


@case
def d4_窗口不会因重启丢失():
    # 落盘 → 重新读模块级路径仍是同一个文件
    gc.open_upgrade_window(120)
    ok(os.path.exists(gc.UPGRADE_WINDOW_PATH), "窗口必须落盘")
    d = json.load(open(gc.UPGRADE_WINDOW_PATH, encoding="utf-8"))
    ok(d["until"] > 0, str(d))
    gc.close_upgrade_window()


# ═══════════════════════════════════════════════════════════
# E. 路由
# ═══════════════════════════════════════════════════════════
def _client():
    import app as webapp
    return webapp.app.test_client()


@case
def e1_未配通道时返回200且给出原因():
    gc.CONFIG_PATH = os.path.join(TMP, "empty.json")
    c = _client()
    r = c.get("/api/guard/peer")
    d = r.get_json()
    ok(r.status_code == 200, str(r.status_code))
    ok(d["configured"] is False, str(d))
    ok("通道" in d["error"], d["error"])


@case
def e2_没有本地包时升级被拒且文案可读():
    gc.CONFIG_PATH = os.path.join(TMP, "g2.json")
    with open(gc.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"enabled": True, "token": "t" * 32,
                   "endpoints": {"ts": BASE}}, f)
    # 包库存也是共享的（c2 造了两个假包），这里必须指到空目录，
    # 否则走不到 no_local_package 分支
    old_dir = gc.PKG_DIR
    gc.PKG_DIR = os.path.join(TMP, "pkgs_empty")
    try:
        c = _client()
        r = c.post("/api/guard/peer/upgrade", json={})
        d = r.get_json()
        ok(r.status_code == 400, str(r.status_code))
        ok("已签名的僚机包" in d["error"], d["error"])
    finally:
        gc.PKG_DIR = old_dir


@case
def e3_确认码缺失被拒():
    c = _client()
    r = c.post("/api/guard/peer/confirm", json={"job": "x"})
    ok(r.status_code == 400, str(r.status_code))
    ok("确认码" in r.get_json()["error"], r.get_json()["error"])


@case
def e4_公钥算法非ed25519被拒():
    gc.CONFIG_PATH = os.path.join(TMP, "g3.json")
    with open(gc.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"enabled": False}, f)
    c = _client()
    r = c.post("/api/guard/config", json={"pkg_pubkey": "ssh-rsa AAAAB3Nza"})
    ok(r.status_code == 400, str(r.status_code))
    ok("ed25519" in r.get_json()["error"], r.get_json()["error"])


@case
def e5_路由清单齐全():
    import app as webapp
    rules = [str(r) for r in webapp.app.url_map.iter_rules()]
    for p in ("/api/guard/peer", "/api/guard/peer/package",
              "/api/guard/peer/upgrade", "/api/guard/peer/confirm",
              "/api/guard/peer/job"):
        ok(p in rules, "缺路由 " + p)


@case
def e6_升级接口不在免鉴权白名单():
    # 这是安全项：这些接口能换掉 B 的代码，绝不能匿名可调
    try:
        from utils import auth_guard
    except Exception:
        return                      # 无该模块时跳过
    public = getattr(auth_guard, "PUBLIC_PREFIX", []) + getattr(auth_guard, "PUBLIC_EXACT", [])
    for p in ("/api/guard/peer/upgrade", "/api/guard/peer/confirm"):
        ok(not any(p.startswith(x) and x != "/" for x in public), p + " 不能被放行")


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
    SRV.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d/%d passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
