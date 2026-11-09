#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微机器人开关切换回归测试（M132）

复现的 bug（2026-11-09 用户实测）：设置页「健康与通知 → A7Z 守护」勾选「已启用」，
勾选立刻弹回「已禁用」，永远启用不了，进而自检 wechat 长期 warn。

根因有三段，缺一不成闭环：
  ① 前端 onWecomToggle 在「输入框为空」时直接 loadWecomConfig() 返回，一个请求都不发
     —— 把「输入框有没有内容」当成了「切换开关是否合法」的判据；
  ② 后端要求 effective_key 非空，用户没 key 时切换一定 400；
  ③ saveWecomKey 取 toggle.checked 作为 enabled，而它刚被①回滚成 false
     → 填 key 保存反而存进 _disabled，下次勾选又被①回滚。

本测试钉死三件事：
  A. 后端「只切开关」能搬动 key（T3/T4），且历史非法 key 不阻塞开关（T10）；
  B. 入参布尔语义明确（T9），新 key 才校验格式（T10），首尾空格归一（T11）；
  C. 前端源码里不再出现①那段早退（T14）。

跑法：python _test_wecom_toggle.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = 0
FAIL = 0
FAILED = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s" % name)
    else:
        FAIL += 1
        FAILED.append(name)
        print("FAIL %s %s" % (name, detail))


def eq(name, got, want):
    ok(name, got == want, "got=%r want=%r" % (got, want))


def main():
    tmpdir = tempfile.mkdtemp(prefix="wecom_toggle_")
    cfg_path = os.path.join(tmpdir, "sentry.json")

    # guard 分支会 import guard_client 并调 push_bot_to_b()。这里**显式**替换而不是
    # setdefault：若该模块已被导入，setdefault 什么都不做，测试会调用真实实现
    # （读真实配置、可能真的发 HTTP），而且失败时路由仍返回 200 —— 假绿灯。
    _saved_gc = sys.modules.get("guard_client")
    _calls = []
    sys.modules["guard_client"] = types.SimpleNamespace(
        push_bot_to_b=lambda: (_calls.append(1), (True, ""))[1])

    _saved_path = None
    try:
        from flask import Flask
        import routes.system_routes as sr

        # 路由里的 WECOM_CONFIG_PATH 是从 utils.app_helpers 导入的模块级常量，
        # 改本模块的名字即可（路由函数按全局名查找）。原值最后必须还原，
        # 否则同进程后续测试会指向一个已被删除的临时路径。
        _saved_path = sr.WECOM_CONFIG_PATH
        sr.WECOM_CONFIG_PATH = cfg_path
        ok("T0 桩生效：写的是临时目录",
           cfg_path.startswith(tempfile.gettempdir()), cfg_path)

        app = Flask(__name__)
        app.register_blueprint(sr.system_bp)
        c = app.test_client()

        def post(payload):
            r = c.post("/api/system/wecom-config", json=payload)
            try:
                body = r.get_json() or {}
            except Exception:
                body = {}
            return r.status_code, body

        def read_cfg():
            if not os.path.exists(cfg_path):
                return {}
            with io.open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)

        def get_bot(bot_key):
            d = c.get("/api/system/wecom-config").get_json() or {}
            return {b["bot_key"]: b for b in (d.get("bots") or [])}.get(bot_key) or {}

        # ── T1 无 key 时切开关 → 400 + code=need_key（前端据此引导去填 key）──
        st, body = post({"bot": "guard", "enabled": True})
        eq("T1 无 key 切换: 状态码", st, 400)
        eq("T1 无 key 切换: code", body.get("code"), "need_key")
        ok("T1 无 key 切换: 未落盘", not os.path.exists(cfg_path) or read_cfg() == {})

        # ── T2 首次保存（key + enabled=true）→ 直接进主字段 ──
        KEY = "11111111-2222-3333-4444-555555555555"
        st, body = post({"bot": "guard", "key": KEY, "enabled": True})
        eq("T2 首次保存: 状态码", st, 200)
        cfg = read_cfg()
        eq("T2 首次保存: 主字段", cfg.get("wecom_guard_webhook_key"), KEY)
        ok("T2 首次保存: 无 _disabled 残留",
           "wecom_guard_webhook_key_disabled" not in cfg)
        ok("T2 首次保存: guard 触发了 B 同步桩", len(_calls) == 1, _calls)
        ok("T2 首次保存: 回执带 b_sync", isinstance(body.get("b_sync"), dict), body)

        # ── T3 已启用 → 只发 enabled=false（不带 key）→ 搬进 _disabled ──
        st, body = post({"bot": "guard", "enabled": False})
        eq("T3 关闭: 状态码", st, 200)
        cfg = read_cfg()
        ok("T3 关闭: 主字段已移除", "wecom_guard_webhook_key" not in cfg)
        eq("T3 关闭: 进 _disabled", cfg.get("wecom_guard_webhook_key_disabled"), KEY)

        # ── T4 已禁用 → 只发 enabled=true（不带 key）→ 搬回主字段 ★核心回归点 ──
        st, body = post({"bot": "guard", "enabled": True})
        eq("T4 重新启用: 状态码", st, 200)
        cfg = read_cfg()
        eq("T4 重新启用: 搬回主字段", cfg.get("wecom_guard_webhook_key"), KEY)
        ok("T4 重新启用: _disabled 已清",
           "wecom_guard_webhook_key_disabled" not in cfg)

        # ── T5 GET 状态自洽：enabled=True / has_key=True / 预览尾号正确 ──
        g = get_bot("guard")
        eq("T5 GET: enabled", g.get("enabled"), True)
        eq("T5 GET: has_key", g.get("has_key"), True)
        eq("T5 GET: 预览尾号", g.get("key_preview"), "***" + KEY[-6:])

        # ── T6 其它 bot 不受影响（guard 的 key 不能被写到 status 上）──
        ok("T6 隔离: status 仍是空的",
           not (read_cfg().get("wecom_status_webhook_key") or ""))

        # ── T7 禁用态 GET：enabled=False 但 has_key 仍为 True（三态判定依赖它）──
        post({"bot": "guard", "enabled": False})
        g = get_bot("guard")
        eq("T7 禁用态: enabled", g.get("enabled"), False)
        eq("T7 禁用态: has_key", g.get("has_key"), True)
        eq("T7 禁用态: 预览仍有尾号", g.get("key_preview"), "***" + KEY[-6:])
        post({"bot": "guard", "enabled": True})

        # ── T8 同时换 key + 禁用 → 新 key 进 _disabled ──
        KEY2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        st, body = post({"bot": "status", "key": KEY2, "enabled": False})
        eq("T8 换 key 同时禁用: 状态码", st, 200)
        cfg = read_cfg()
        eq("T8 换 key 同时禁用: _disabled 是新 key",
           cfg.get("wecom_status_webhook_key_disabled"), KEY2)
        ok("T8 换 key 同时禁用: 主字段已移除",
           "wecom_status_webhook_key" not in cfg)

        # ── T9 enabled 必须是布尔：字符串 "false" 不能当 True（bool("false")==True）──
        st, body = post({"bot": "boot", "key": KEY2, "enabled": "false"})
        eq("T9 字符串 enabled: 状态码", st, 400)
        ok("T9 字符串 enabled: 未落盘",
           not (read_cfg().get("wecom_boot_webhook_key") or ""))

        # ── T10 历史非法 key（带空格）只切开关 → 必须成功，不能 400 ──
        legacy = "legacy key with spaces 0000"
        cfg = read_cfg()
        cfg["wecom_boot_webhook_key_disabled"] = legacy
        cfg.pop("wecom_boot_webhook_key", None)
        with io.open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        st, body = post({"bot": "boot", "enabled": True})
        eq("T10 历史非法 key 启用: 状态码", st, 200)
        eq("T10 历史非法 key 启用: 已搬回主字段",
           read_cfg().get("wecom_boot_webhook_key"), legacy)
        # 而用户**新输入**同样的非法值时必须被拒
        st, body = post({"bot": "boot", "key": "not-a-valid-key", "enabled": True})
        eq("T10 新输入非法 key: 状态码", st, 400)

        # ── T11 新 key 首尾空格归一化 ──
        st, body = post({"bot": "sentry", "key": "  " + KEY + "  ", "enabled": True})
        eq("T11 空格归一: 状态码", st, 200)
        eq("T11 空格归一: 落库无空格",
           read_cfg().get("wecom_sentry_webhook_key"), KEY)

        # ── T12 无效机器人 ──
        st, body = post({"bot": "nope", "key": KEY, "enabled": True})
        eq("T12 无效机器人: 状态码", st, 400)

        # ── T13 写入后权限不被收窄（mkstemp 默认 0600）──
        if os.name == "posix":
            mode = os.stat(cfg_path).st_mode & 0o777
            ok("T13 权限保持可读", mode & 0o044 == 0o044, oct(mode))

        # ── T14 前端静态断言（先剥注释，避免注释里的复述造成假绿灯）──
        html = io.open(os.path.join(HERE, "templates", "system.html"),
                       encoding="utf-8").read()
        code = re.sub(r"/\*.*?\*/|(?<!:)//[^\n]*", "", html, flags=re.S)
        ok("T14 前端: 无「输入框空就重载」早退",
           not re.search(r"if\s*\(\s*!\s*input\.value\.trim\(\)\s*\)\s*\{[^{}]*"
                         r"loadWecomConfig\s*\(\s*\)\s*;\s*return", code))
        ok("T14 前端: 切换只发 enabled",
           "JSON.stringify({ bot: botKey, enabled: want })" in code)
        ok("T14 前端: 有未配置三态", "hasKey ? '已禁用' : '未配置'" in code)
        ok("T14 前端: 首次保存默认启用", "toggle.checked || firstTime" in code)
        ok("T14 前端: firstTime 只认 '0'", "toggle.dataset.hasKey === '0'" in code)
        ok("T14 前端: 处理 need_key", "d.code === 'need_key'" in code)
        ok("T14 前端: 轮询有守卫", "if (wecomBusy > 0) return;" in code)
        ok("T14 前端: 未配置有可见指引",
           "还没有 KEY：请在下方粘贴 webhook key" in code)

    finally:
        if _saved_path is not None:
            try:
                sr.WECOM_CONFIG_PATH = _saved_path
            except Exception:
                pass
        if _saved_gc is None:
            sys.modules.pop("guard_client", None)
        else:
            sys.modules["guard_client"] = _saved_gc
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILED:
        print("失败项: %s" % ", ".join(FAILED))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
