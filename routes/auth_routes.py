#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录鉴权 HTTP 路由（仅验证码登录 v2）

职责：只做 HTTP 编排与权限校验，业务状态全部委托给 utils/auth_service.py。
响应统一：{"success": bool, "error_code": int, "message": str, "data": {...}}
"""
import secrets
import time
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from utils import auth_service as auth

auth_bp = Blueprint("auth", __name__, url_prefix="")

REAUTH_WINDOW = 300  # 二次认证有效期（秒）


# ══════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════
def _client_ip() -> str:
    # 不信任 X-Forwarded-For：当前部署无反向代理，信任 header 等于自废
    return request.remote_addr or ""


def _ua() -> str:
    return request.headers.get("User-Agent", "")


def _ok(data: Dict[str, Any] = None, message: str = "ok", http: int = 200):
    body = {"success": True, "error_code": 0, "message": message}
    if data:
        body["data"] = data
    return jsonify(body), http


def _err(code: int, message: str, http: int = 400, data: Dict[str, Any] = None):
    body = {"success": False, "error_code": code, "message": message}
    if data:
        body["data"] = data
    return jsonify(body), http


def _json() -> Dict[str, Any]:
    try:
        return request.get_json(silent=True) or {}
    except Exception:
        return {}


def _safe_next(path: str) -> str:
    """登录后跳转校验（实现见 auth_service.safe_relative_path，保持单一口径）。"""
    return auth.safe_relative_path(path)

def _is_xhr() -> bool:
    accept = request.headers.get("Accept", "")
    return "text/html" not in accept


def _need_reauth() -> bool:
    return (time.time() - float(session.get("auth_at") or 0)) > REAUTH_WINDOW


def _require_admin() -> Tuple[bool, Any]:
    """安全配置的统一权限门。

    未启用（首次配置阶段）：仅本地网络可操作，无会话也能配。
    已启用：必须已登录 + 本地网络 + 5 分钟内二次认证。
    """
    ip = _client_ip()
    cfg = auth.load_config()
    local = auth.is_local_network(ip, cfg.get("lan_cidrs"))

    if not auth.is_enabled():
        if not local:
            auth.audit("security_denied", ip=ip, ua=_ua(), reason="not_local_uninitialized")
            return False, _err(4007, "安全配置仅限本地网络", 403)
        return True, None

    ok, reason = auth.check_session(session)
    if not ok:
        return False, _err(4001, "请先登录", 401)
    if not local:
        auth.audit("security_denied", ip=ip, ua=_ua(), reason="not_local")
        return False, _err(4007, "安全配置仅限本地网络修改（tailnet 远程不可改）", 403)
    if session.get("readonly"):
        return False, _err(4015, "当前为只读模式，请用本地密钥登录后操作", 403)
    if _need_reauth():
        return False, _err(4008, "请先完成二次认证", 403)
    return True, None


# ══════════════════════════════════════════════════════════════════════
# 登录
# ══════════════════════════════════════════════════════════════════════
@auth_bp.route("/login", methods=["GET"])
def login_page():
    ip = _client_ip()
    ua = _ua()
    cfg = auth.load_config()

    ok, _reason = auth.check_session(session)
    if ok and auth.is_enabled():
        return redirect(_safe_next(session.get("intended_path") or "/"))

    # 主路径：跳转意图存服务端会话，URL 里不出现目标地址（防钓鱼）
    intended = request.args.get("next")
    if intended and not session.get("intended_path"):
        session["intended_path"] = _safe_next(intended)

    csrf = session.get("csrf") or secrets.token_hex(16)
    session["csrf"] = csrf
    challenge = auth.issue_challenge(ip, ua)

    return render_template(
        "login.html",
        challenge=challenge,
        csrf=csrf,
        enabled=auth.is_enabled(),
        security_email=auth.mask_email(cfg.get("security_email", "")),
        local_mode=auth.is_local_network(ip, cfg.get("lan_cidrs")),
        has_local_key=auth.has_local_key(),
        reason=request.args.get("reason", ""),
    )


@auth_bp.route("/api/auth/status", methods=["GET"])
def status():
    ok, reason = auth.check_session(session)
    return _ok({
        "enabled": auth.is_enabled(),
        "logged_in": bool(ok),
        "readonly": bool(session.get("readonly")),
        "reason": reason if not ok else "",
    })


@auth_bp.route("/api/auth/code/request", methods=["POST"])
def code_request():
    if not auth.is_enabled():
        return _err(4011, "双因子登录未启用，请先在设置页完成安全配置", 400)
    body = _json()
    result = auth.request_code(body.get("challenge", ""), _client_ip(), _ua())
    if result.get("ok"):
        return _ok({k: v for k, v in result.items() if k not in ("ok", "error_code", "message")},
                   result.get("message", "已发送"))
    return _err(result.get("error_code", 4006), result.get("message", "发送失败"), 400,
                {"error_type": result.get("error_type", "")})


@auth_bp.route("/api/auth/code/verify", methods=["POST"])
def code_verify():
    if not auth.is_enabled():
        return _err(4011, "双因子登录未启用", 400)
    body = _json()
    result = auth.verify_code(body.get("challenge", ""), body.get("code", ""), _client_ip(), _ua())
    if not result.get("ok"):
        return _err(result.get("error_code", 4003), result.get("message", "验证失败"), 401)

    payload = auth.build_session_payload(readonly=False)
    session.clear()
    session.update(payload)
    session["auth_at"] = time.time()
    target = _safe_next(session.get("intended_path") or "/")
    session.pop("intended_path", None)
    session["csrf"] = secrets.token_hex(16)
    return _ok({"redirect": target, "csrf": session["csrf"]}, "登录成功")


@auth_bp.route("/api/auth/local", methods=["POST"])
def local_login():
    """L2 完整登录（需本地密钥）/ L4 只读（不提供密钥）。"""
    if not auth.is_enabled():
        return _err(4011, "双因子登录未启用", 400)
    ip = _client_ip()
    cfg = auth.load_config()
    if not auth.is_local_network(ip, cfg.get("lan_cidrs")):
        auth.audit("local_login_denied", ip=ip, ua=_ua(), reason="not_local")
        return _err(4007, "离线登录仅限本地网络", 403)

    body = _json()
    key = (body.get("local_key") or "").strip()
    readonly = True

    if key:
        if not auth.verify_local_key(key):
            auth.audit("local_login_failed", ip=ip, ua=_ua(), has_key=True)
            return _err(4013, "本地密钥不正确", 401)
        readonly = False
    elif not body.get("readonly"):
        return _err(4013, "请输入本地密钥，或选择只读浏览", 401)

    payload = auth.build_session_payload(readonly=readonly)
    session.clear()
    session.update(payload)
    session["auth_at"] = time.time()
    session["csrf"] = secrets.token_hex(16)
    auth.audit("local_login", ip=ip, ua=_ua(), readonly=readonly)
    return _ok({"readonly": readonly, "redirect": _safe_next(session.get("intended_path") or "/"),
                "csrf": session["csrf"]},
               "已进入只读模式" if readonly else "登录成功（本地密钥）")


@auth_bp.route("/api/auth/reauth", methods=["POST"])
def reauth():
    ip = _client_ip()
    cfg = auth.load_config()
    ok, _reason = auth.check_session(session)
    if not ok:
        return _err(4001, "请先登录", 401)
    if not auth.is_local_network(ip, cfg.get("lan_cidrs")):
        return _err(4007, "二次认证仅限本地网络", 403)
    body = _json()
    if not auth.verify_local_key((body.get("local_key") or "").strip()):
        auth.audit("reauth_failed", ip=ip, ua=_ua())
        return _err(4013, "本地密钥不正确", 401)
    session["auth_at"] = time.time()
    auth.audit("reauth_ok", ip=ip, ua=_ua())
    return _ok({"reauth_until": time.time() + REAUTH_WINDOW}, "二次认证通过")


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    ip = _client_ip()
    session.clear()
    auth.audit("logout", ip=ip, ua=_ua())
    return _ok({"redirect": "/login"}, "已退出")


# ══════════════════════════════════════════════════════════════════════
# 安全配置
# ══════════════════════════════════════════════════════════════════════
@auth_bp.route("/api/security/config", methods=["GET"])
def security_config():
    ip = _client_ip()
    cfg = auth.load_config()
    data = auth.public_config()
    data["is_local_network"] = auth.is_local_network(ip, cfg.get("lan_cidrs"))
    # 首次配置：把设备自身所在网段回给前端预填，避免用户漏填导致启用后自锁
    data["suggested_cidrs"] = auth.direct_iface_cidrs()
    data["reauth_ok"] = (time.time() - float(session.get("auth_at") or 0)) <= REAUTH_WINDOW
    data["smtp_preset"] = {"host": "smtp.exmail.qq.com", "port": 465, "tls_mode": "ssl"}
    # 前端需要 CSRF token；未启用（首次配置）阶段 session 里还没有，这里补发一个
    csrf = session.get("csrf")
    if not csrf:
        csrf = secrets.token_hex(16)
        session["csrf"] = csrf
    data["csrf"] = csrf
    return _ok(data)


@auth_bp.route("/api/security/smtp/test", methods=["POST"])
def smtp_test():
    ok, resp = _require_admin()
    if not ok:
        return resp
    from utils.mailer import test_smtp

    body = _json()
    cfg = auth.load_config()
    candidate = dict(cfg)
    cand_smtp = dict(cfg.get("smtp") or {})
    for k in ("host", "port", "tls_mode", "user", "from_addr"):
        if body.get(k) not in (None, ""):
            cand_smtp[k] = body[k]
    if body.get("password"):
        cand_smtp["password"] = body["password"]
    candidate["smtp"] = cand_smtp
    if body.get("security_email"):
        candidate["security_email"] = body["security_email"]

    result = test_smtp(candidate)
    auth.audit("smtp_test", ip=_client_ip(), ua=_ua(), ok=bool(result.get("ok")),
               error_type=result.get("error_type", ""))
    if result.get("ok"):
        return _ok({"error_type": "", "detail": result.get("detail", "")}, result.get("message", "测试成功"))
    # 三类错误用不同错误码，前端据此给出不同引导
    etype = result.get("error_type")
    code = {"connect": 4016, "auth": 4017, "rejected": 4018, "config": 4012}.get(etype, 4006)
    return _err(code, result.get("message", "测试失败"), 400, {"error_type": etype})


@auth_bp.route("/api/security/smtp/save", methods=["POST"])
def smtp_save():
    ok, resp = _require_admin()
    if not ok:
        return resp
    body = _json()
    cfg = auth.load_config()
    smtp = dict(cfg.get("smtp") or {})

    def _pick(key, default=""):
        """F4：字段出现在请求里就用它的值（含空串），只有完全没提交才回退到已保存值。

        旧写法 `body.get(k) or smtp.get(k)` 会让"清空输入框后保存"变成
        "静默沿用旧值"——用户以为清空了，其实没清。
        """
        if key in body and body.get(key) is not None:
            return body.get(key)
        v = smtp.get(key)
        return default if v is None else v

    from utils.mailer import is_valid_email

    host = str(_pick("host", "")).strip()
    try:
        port = int(_pick("port", 465) or 465)
    except (TypeError, ValueError):
        return _err(4012, "端口必须是数字", 400)
    if not host or len(host) > 253:
        return _err(4012, "请填写 SMTP 服务器地址（腾讯企业邮：smtp.exmail.qq.com）", 400)
    if not (1 <= port <= 65535):
        return _err(4012, "端口超出范围（465 或 587）", 400)
    tls_mode = str(_pick("tls_mode", "ssl")).lower()
    if tls_mode not in ("ssl", "starttls", "none"):
        return _err(4012, "TLS 模式不合法", 400)
    user = str(_pick("user", "")).strip()
    if not user:
        return _err(4012, "请填写发送邮箱（完整地址，例如 someone@example.com）", 400)
    if not is_valid_email(user):
        return _err(4012, "发送邮箱格式不正确：需要 local@domain.tld（例如 someone@example.com）", 400)
    from_addr = str(_pick("from_addr", "")).strip() or user
    if not is_valid_email(from_addr):
        return _err(4012, "发件人地址格式不正确（需为完整邮箱）", 400)
    if user.lower() == (cfg.get("security_email") or "").strip().lower():
        return _err(4012, "发送邮箱不能与安全邮箱相同", 400)
    password = str(_pick("password", "") or "")
    if not password:
        return _err(4012, "请填写发送邮箱的客户端专用密码（企业邮为 16 位，不是登录密码）", 400)

    smtp.update({"host": host, "port": port, "tls_mode": tls_mode,
                 "user": user, "from_addr": from_addr, "password": password})
    cfg["smtp"] = smtp
    auth.save_config(cfg)
    auth.audit("smtp_saved", ip=_client_ip(), ua=_ua(), host=host, port=port)
    return _ok({"host": host, "port": port, "tls_mode": tls_mode}, "SMTP 配置已保存")


@auth_bp.route("/api/security/email/start", methods=["POST"])
def email_start():
    ok, resp = _require_admin()
    if not ok:
        return resp
    body = _json()
    # F1：把设置页当前的 SMTP 表单值一起带进来（首次配置时 config 里还没有它）
    override = body.get("smtp") if isinstance(body.get("smtp"), dict) else None
    result = auth.start_email_change(body.get("email", ""), _client_ip(), _ua(), override)
    if result.get("ok"):
        return _ok({k: v for k, v in result.items() if k not in ("ok", "error_code", "message")},
                   result.get("message", "已发送"))
    return _err(result.get("error_code", 4006), result.get("message", "发送失败"), 400,
                {"error_type": result.get("error_type", "")})


@auth_bp.route("/api/security/email/confirm", methods=["POST"])
def email_confirm():
    ok, resp = _require_admin()
    if not ok:
        return resp
    body = _json()
    result = auth.confirm_email_change(body.get("code", ""), _client_ip(), _ua())
    if result.get("ok"):
        # 变更安全邮箱后强制注销其它会话（epoch+1）
        auth.bump_epoch()
        session["epoch"] = auth.current_epoch()
        return _ok({k: v for k, v in result.items() if k not in ("ok", "error_code", "message")},
                   result.get("message", "已更新"))
    return _err(result.get("error_code", 4003), result.get("message", "验证失败"), 400)


@auth_bp.route("/api/security/lan/save", methods=["POST"])
def lan_save():
    """口径 C：软校验 + 二次确认。loopback 永久豁免（安全网 1）。"""
    ok, resp = _require_admin()
    if not ok:
        return resp
    body = _json()
    raw = body.get("lan_cidrs", "")
    if isinstance(raw, list):
        raw = ",".join(str(x) for x in raw)
    good, cidrs, err = auth.parse_cidrs(raw)
    if not good:
        return _err(4012, err, 400)

    ip = _client_ip()
    enabled = auth.is_enabled()
    # 口径 C 的判据必须用「启用后」的严格口径（M118）：
    # 未启用阶段直连网段临时算本地（解引导死锁），但启用后只认 lan_cidrs，
    # 所以"保存后会不会失去权限"要排除直连网段，否则二次确认会被静默跳过。
    after_enable = auth.is_local_network(ip, cidrs, include_direct=False)
    if not after_enable and not body.get("confirm"):
        msg = ("当前访问地址不在新网段内，保存后将立即失去设置权限"
               if enabled else
               "当前访问地址不在新网段内，启用登录保护后将失去设置权限")
        return _err(4014, msg, 400,
                    {"requires_confirm": True, "current_ip": ip, "new_cidrs": cidrs})

    cfg = auth.load_config()
    cfg["lan_cidrs"] = cidrs
    auth.save_config(cfg)
    now_local = auth.is_local_network(ip, cidrs)
    auth.audit("lan_saved", ip=ip, ua=_ua(), cidrs=cidrs,
               still_local=now_local, local_after_enable=after_enable)
    return _ok({"lan_cidrs": cidrs, "has_local_access": now_local,
                "local_after_enable": after_enable, "current_ip": ip},
               "网段已保存")


@auth_bp.route("/api/security/local-key/set", methods=["POST"])
def local_key_set():
    """生成或自定义本地密钥。明文只返回这一次。"""
    ok, resp = _require_admin()
    if not ok:
        return resp
    body = _json()
    custom = (body.get("custom_key") or "").strip() or None
    good, plain, message = auth.set_local_key(custom)
    if not good:
        return _err(4012, message, 400)
    return _ok({"local_key": plain, "is_custom": bool(custom),
                "notice": "本地密钥只显示这一次，请立即保存到密码管理器或打印留存"},
               message)


@auth_bp.route("/api/security/enable", methods=["POST"])
def security_enable():
    ok, resp = _require_admin()
    if not ok:
        return resp
    cfg = auth.load_config()
    # 网段留空 → 启用后只剩 loopback + AP 段算本地，家里局域网会被判非本地，
    # 用户再也进不来改配置（M118）。所以启用前必须先把日常访问网段填好。
    if not (cfg.get("lan_cidrs") or []):
        return _err(4012, "请先填写你日常访问 A7Z 的局域网段（例如 192.168.1.0/24）；"
                          "留空会导致启用后无法再修改安全配置", 400)
    if not cfg.get("security_email_verified"):
        return _err(4009, "请先完成安全邮箱的「发送测试码 → 回填」闭环", 400)
    if not (cfg.get("smtp") or {}).get("password"):
        return _err(4012, "请先配置发送邮箱与密码", 400)

    generated = None
    if not auth.has_local_key():
        good, plain, msg = auth.set_local_key(None)
        if not good:
            return _err(4012, msg, 400)
        generated = plain

    cfg = auth.load_config()
    cfg["enabled"] = True
    auth.save_config(cfg)
    auth.audit("security_enabled", ip=_client_ip(), ua=_ua(),
               generated_local_key=bool(generated))
    data = {"enabled": True}
    if generated:
        data["local_key"] = generated
        data["notice"] = "已自动生成本地密钥，只显示这一次，请立即保存"
    return _ok(data, "双因子登录已启用")


@auth_bp.route("/api/security/disable", methods=["POST"])
def security_disable():
    """关闭保护：需要本地网络 + 已登录 + 二次认证。"""
    ok, resp = _require_admin()
    if not ok:
        return resp
    cfg = auth.load_config()
    cfg["enabled"] = False
    auth.save_config(cfg)
    auth.audit("security_disabled", ip=_client_ip(), ua=_ua())
    return _ok({"enabled": False}, "已关闭登录保护（建议尽快重新启用）")
