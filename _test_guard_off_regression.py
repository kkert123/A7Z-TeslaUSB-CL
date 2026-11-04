#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""未启用状态下旧接口逐项回归（M116 / F1+F2 验收）

背景：v0.3.1.68 首版守卫把 CSRF 校验无条件放在「是否启用」之前，
而 csrf 只在 3 个位置产生 → 升级后即使不启用登录保护，前端 65 处写操作
全部 403。真机影子验证 35/35 没抓到，因为只测了鉴权链路本身。

本测试的判据是「开关关闭 = 与上一版行为等价」：
从真实前端源码静态扫描出所有写请求路径，逐条验证四种状态下的行为。

  G1 路径清单（扫描结果是否可信）
  G2 未启用 + 无 CSRF      → 全部 200（核心：旧功能零变化）
  G3 未启用 + 匿名入口      → 仍要求 CSRF（防轰炸）
  G4 已启用 + 无会话        → 401/4001
  G5 已启用 + 有会话无 CSRF → 403/4010
  G6 已启用 + 有会话有 CSRF → 200（F2 的必要性）
  G7 已启用 + 只读会话      → 403/4015
  G8 未启用 + GET 页面      → 不被踢到登录页
  G9 context_processor 注入 → token 存在、两次一致、空时自生成
  G10 模板注入点             → base/event_player 含 __CSRF__ 且在 app.js 之前
"""
import ast
import os
import json
import re
import sys
import tempfile
import types
from pathlib import Path

# 可用 TESLAUSB_ROOT 指向影子副本（真机验证：/tmp/v1NN-shadow）
ROOT = Path(os.environ.get("TESLAUSB_ROOT") or r"D:/teslausb/a7z")
TMP = Path(tempfile.mkdtemp(prefix="reg_guard_"))

sys.path.insert(0, str(ROOT))
try:
    import bs4  # noqa: F401
except Exception:
    _m = types.ModuleType("bs4")
    _m.BeautifulSoup = object
    sys.modules["bs4"] = _m

import config as cfg  # noqa: E402
cfg.DATA_DIR = str(TMP)

from utils import auth_service as auth  # noqa: E402
auth.AUTH_FILE = TMP / "web_auth.json"
auth.RUNTIME_FILE = TMP / "auth_runtime.json"
auth.AUDIT_FILE = TMP / "auth_audit.log"
auth.LOCK_FILE = TMP / ".auth.lock"
auth.DATA_DIR = TMP

import utils.auth_guard as guard  # noqa: E402
import routes.auth_routes as auth_routes  # noqa: E402
from flask import Flask, session  # noqa: E402

PASS = FAIL = 0
FAILS = []


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS", name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("FAIL", name, str(extra)[:200])


# ── G1 静态扫描前端写请求路径 ────────────────────────────────────────
CALL_RE = re.compile(r"""(?:fetch|App\.post)\(\s*['"]([^'"]+)['"]""")
METH_RE = re.compile(r"""method\s*:\s*['"](POST|PUT|PATCH|DELETE)['"]""")


def scan_write_paths():
    found = {}
    files = list((ROOT / "static").rglob("*.js")) + list((ROOT / "templates").rglob("*.html"))
    for f in files:
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in CALL_RE.finditer(t):
            url = m.group(1)
            tail = t[m.end(): m.end() + 400]
            if not METH_RE.search(tail):
                continue
            if url.startswith(("http://", "https://", "//")):
                continue
            # 动态段替换成占位，便于注册 stub
            url = re.sub(r"\$\{[^}]*\}", "1", url)
            url = re.sub(r"\{\{[^}]*\}\}", "1", url)
            if not url.startswith("/"):
                continue
            if "?" in url:
                url = url.split("?")[0]
            if not url.startswith("/api/"):
                continue
            found.setdefault(url, f.name)
    return found


paths = scan_write_paths()
print(f"\n扫描到前端写请求路径 {len(paths)} 条（来源 {len(set(paths.values()))} 个文件）\n")

# 关键接口必须被扫到（防止正则失效导致"空集通过"）
MUST = [
    "/api/mode/switch",
    "/api/cleanup/execute",
]
for mu in MUST:
    ck(f"G1 扫描命中关键接口 {mu}", mu in paths, sorted(paths)[:8])
ck("G1 路径数量 >= 20", len(paths) >= 20, len(paths))

app = Flask(__name__)
app.secret_key = "regression-secret"
guard.register(app)
app.register_blueprint(auth_routes.auth_bp)

existing = {str(r.rule) for r in app.url_map.iter_rules()}
registered, skipped = [], []
for i, p in enumerate(sorted(paths)):
    if p in existing:
        skipped.append(p)
        continue

    def _mk(pp):
        def _h():
            return {"ok": True, "stub": pp}
        return _h

    app.add_url_rule(p, endpoint=f"stub{i}", view_func=_mk(p),
                     methods=["POST", "PUT", "PATCH", "DELETE"])
    registered.append(p)

print(f"\n注册 stub {len(registered)} 条；跳过已存在路由 {len(skipped)} 条 {skipped[:5]}\n")

# 一条公开的 GET 页面用于 G8
app.add_url_rule("/videos", endpoint="page_videos", view_func=lambda: "OK")


def write_cfg(**kw):
    base = {"enabled": False, "lan_cidrs": ["192.168.42.0/24"]}
    base.update(kw)
    auth.AUTH_FILE.write_text(json.dumps(base), encoding="utf-8")


def call(client, path, headers=None, method="POST"):
    return client.open(path, method=method, headers=headers or {})


# ── G2 未启用 + 无 CSRF → 全部 200 ──────────────────────────────────
write_cfg(enabled=False)
ck("G2.0 前置：is_enabled() == False", auth.is_enabled() is False)
bad = []
with app.test_client() as c:
    for p in registered:
        r = call(c, p)
        if r.status_code != 200:
            bad.append((p, r.status_code, r.get_data(as_text=True)[:60]))
ck("G2.1 未启用时全部写接口放行（旧功能零变化）", not bad,
   f"{len(bad)}/{len(registered)} 被拦，样例 {bad[:3]}")

# ── G3 未启用 + 匿名登录入口 → 仍要求 CSRF ──────────────────────────
with app.test_client() as c:
    r = call(c, "/api/auth/code/request", {"Content-Type": "application/json"})
ck("G3.1 未启用时发码接口仍校验 CSRF", r.status_code == 403 and "4010" in r.get_data(as_text=True),
   r.status_code)
with app.test_client() as c:
    # 注意：/videos 是 stub、不渲染模板，context_processor 不会触发，
    # 所以这里必须手工注入 csrf（真实页面由 base.html 渲染时自动产生）
    import secrets as _secrets
    tok = _secrets.token_hex(16)
    with c.session_transaction() as s:
        s["csrf"] = tok
    r = call(c, "/api/auth/code/request",
             {"X-CSRF-Token": tok, "Content-Type": "application/json"})
ck("G3.2 带正确 CSRF 时不再被 4010 拦（可进入业务校验）",
   not (r.status_code == 403 and "4010" in r.get_data(as_text=True)), r.status_code)

# ── G4~G7 已启用 ────────────────────────────────────────────────────
write_cfg(enabled=True)
ck("G4.0 前置：is_enabled() == True", auth.is_enabled() is True)

with app.test_client() as c:
    bad = []
    for p in registered:
        r = call(c, p)
        if not (r.status_code == 401 and "4001" in r.get_data(as_text=True)):
            bad.append((p, r.status_code, r.get_data(as_text=True)[:60]))
ck("G4.1 已启用 + 未登录 → 全部 401/4001", not bad, f"{len(bad)} 条不符，样例 {bad[:3]}")

with app.test_client() as c:
    with c.session_transaction() as s:
        s.update(auth.build_session_payload(readonly=False))
    bad = []
    for p in registered:
        r = call(c, p)
        if not (r.status_code == 403 and "4010" in r.get_data(as_text=True)):
            bad.append((p, r.status_code, r.get_data(as_text=True)[:60]))
ck("G5.1 已启用 + 已登录 + 无 CSRF → 全部 403/4010", not bad,
   f"{len(bad)} 条不符，样例 {bad[:3]}")

with app.test_client() as c:
    with c.session_transaction() as s:
        s.update(auth.build_session_payload(readonly=False))
        tok = s["csrf"]
    bad = []
    for p in registered:
        r = call(c, p, {"X-CSRF-Token": tok})
        if r.status_code != 200:
            bad.append((p, r.status_code, r.get_data(as_text=True)[:60]))
ck("G6.1 已启用 + 已登录 + 带 CSRF → 全部 200（F2 生效时前端行为）",
   not bad, f"{len(bad)} 条不符，样例 {bad[:3]}")

with app.test_client() as c:
    with c.session_transaction() as s:
        s.update(auth.build_session_payload(readonly=True))
        tok = s["csrf"]
    bad = []
    for p in registered:
        if p == "/api/auth/logout":
            continue
        r = call(c, p, {"X-CSRF-Token": tok})
        if not (r.status_code == 403 and "4015" in r.get_data(as_text=True)):
            bad.append((p, r.status_code))
ck("G7.1 已启用 + 只读会话 → 写接口全部 403/4015", not bad,
   f"{len(bad)} 条不符，样例 {bad[:3]}")

# ── G8 未启用时 GET 页面不被踢 ──────────────────────────────────────
write_cfg(enabled=False)
with app.test_client() as c:
    r = c.get("/videos")
ck("G8.1 未启用时页面直接 200（不 302 到登录页）", r.status_code == 200, r.status_code)

# ── G9 context_processor 真实语义（源码取自 app.py，非重写）─────────
APP_PY = ROOT / "app.py"
src = APP_PY.read_text(encoding="utf-8")
tree = ast.parse(src)
node = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_inject_csrf"), None)
ck("G9.0 app.py 存在 _inject_csrf", node is not None)

if node is not None:
    # 源码片段取自模块级函数，缩进本身正确，直接拼装饰器即可
    # （原实现统一加 4 空格 + strip，把 if 体内层缩进抹平 → 语法错误）
    seg = "@app.context_processor\n" + ast.get_source_segment(src, node)
    app2 = Flask(__name__)
    app2.secret_key = "g9"
    ns = {"app": app2}
    try:
        exec(seg, ns)
        ok_exec = True
    except Exception as e:
        ok_exec = False
        ck("G9.1 提取的 _inject_csrf 可执行", False, e)

    if ok_exec:
        ck("G9.1 提取的 _inject_csrf 可执行", True)
        # 必须用 render_template_string：它会调用 app.update_template_context，
        # 才会触发 context_processor。jinja_env.from_string().render() 不触发
        # （首版写成后者，导致 token 恒为空、两次"相等"其实是假通过）。
        from flask import render_template_string
        with app2.test_request_context("/videos"):
            t1 = render_template_string("{{ csrf }}")
            t2 = render_template_string("{{ csrf }}")
            ck("G9.2 首次渲染生成非空 token", bool(t1) and len(t1) == 32, t1)
            ck("G9.3 二次渲染 token 不变（同会话稳定）", t1 == t2, (t1, t2))
            ck("G9.4 session 内已写入 csrf", session.get("csrf") == t1, session.get("csrf"))

# ── G10 模板注入点 ──────────────────────────────────────────────────
base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
ck("G10.1 base.html 注入 __CSRF__", 'window.__CSRF__="{{ csrf }}"' in base)
i_csrf = base.find("__CSRF__")
i_appjs = base.find("app.js")
ck("G10.2 base.html 中 __CSRF__ 在 app.js 之前", 0 <= i_csrf < i_appjs, (i_csrf, i_appjs))

ep = (ROOT / "templates" / "event_player.html").read_text(encoding="utf-8")
ck("G10.3 event_player.html 注入 __CSRF__（不继承 base，需单独注入）",
   'window.__CSRF__="{{ csrf }}"' in ep)

appjs = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
ck("G10.4 app.js 有 fetch 拦截器", "window.fetch = function" in appjs)
ck("G10.5 拦截器限制同源（防 token 外泄）", "sameOrigin" in appjs and "origin ===" in appjs)
ck("G10.6 拦截器覆盖四种写方法",
   all(m in appjs for m in ("POST", "PUT", "PATCH", "DELETE")))

print("\n" + "=" * 60)
print(f"结果: {PASS} passed, {FAIL} failed")
if FAILS:
    print("失败项:")
    for f in FAILS:
        print("  -", f)
sys.exit(1 if FAIL else 0)
