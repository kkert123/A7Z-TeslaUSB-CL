#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录鉴权守卫（Flask before_request）

从 app.py 抽出来，理由：
  1) app.py 只保留"启动 + 注册"，守卫逻辑可被集成测试直接覆盖；
  2) 守卫是安全边界，单独成文件便于审查（一眼看清放行规则）。

放行规则（顺序即优先级）：
  1 所有写方法校验 CSRF（含匿名登录接口）
  2 公开路径直接放行（登录页、登录接口、静态资源、favicon）
  3 尚未启用登录保护 → 一律放行（避免升级后把用户锁在门外）
  4 会话无效 / epoch 不匹配 → 页面跳登录（原路径存服务端会话）、API 返回 401
  5 只读模式（L4）只允许读方法 + 主动登出
"""
import hmac
from typing import Any

from flask import jsonify, redirect, request, session

from utils import auth_service as auth

PUBLIC_EXACT = ("/login", "/favicon.ico")
PUBLIC_PREFIX = ("/static/", "/api/auth/")

CSRF_HEADER = "X-CSRF-Token"
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
READONLY_ALLOW = ("GET", "HEAD", "OPTIONS")
READONLY_ALLOW_POST = ("/api/auth/logout",)


def is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIX)


def csrf_ok() -> bool:
    expect = session.get("csrf") or ""
    got = request.headers.get(CSRF_HEADER) or ""
    if not expect or not got:
        return False
    return hmac.compare_digest(str(expect), str(got))


def wants_html() -> bool:
    return "text/html" in (request.headers.get("Accept") or "")


def guard() -> Any:
    path = request.path

    # 1) CSRF：所有写方法（含匿名登录接口）都必须带会话内 token。
    #    注意顺序——先查 CSRF 再判放行，否则 /api/auth/* 就成了 CSRF 缺口。
    if request.method in MUTATING and not csrf_ok():
        if path.startswith("/api/"):
            return jsonify({"success": False, "error_code": 4010,
                            "message": "CSRF 校验失败，请刷新页面重试"}), 403
        return "CSRF 校验失败，请刷新页面重试", 403

    # 2) 公开路径
    if is_public(path):
        return None

    # 3) 尚未启用：不拦截（首次配置阶段）
    if not auth.is_enabled():
        return None

    # 4) 会话
    ok, reason = auth.check_session(session)
    if not ok:
        # 按路径前缀区分，而不是 Accept：curl/脚本可能不发 Accept，
        # 但浏览器打开页面必须拿到 302 而不是一段 JSON。
        if path.startswith("/api/"):
            return jsonify({"success": False, "error_code": 4001,
                            "message": "未登录或会话已失效"}), 401
        if request.method == "GET":
            # 主路径：原始路径存服务端会话，URL 里不出现跳转目标（防钓鱼）
            session["intended_path"] = auth.safe_relative_path(request.full_path.rstrip("?"))
        target = "/login?reason=kicked" if reason == "kicked" else "/login"
        return redirect(target, code=302)

    # 5) 只读模式
    if session.get("readonly"):
        if request.method not in READONLY_ALLOW and path not in READONLY_ALLOW_POST:
            if path.startswith("/api/"):
                return jsonify({"success": False, "error_code": 4015,
                                "message": "当前离线只读模式：危险操作需用本地密钥登录"}), 403
            return "当前离线只读模式", 403

    return None


def register(app) -> None:
    app.before_request(guard)
