#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮件发送模块 —— 只负责"把邮件交给腾讯企业邮并拿到结果"

职责边界（见设计文档 §3）：本模块不生成验证码、不做业务判断、不写审计。
错误必须分成三类，因为设置页要给出可区分的提示：
    connect  连接失败（网络/端口/超时/TLS 握手）
    auth     认证失败（账号或客户端专用密码）
    rejected 被拒信（发件人/收件人被拒、额度或频率限制）
"""
import smtplib
import socket
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Any, Dict, Optional, Tuple

DEFAULT_HOST = "smtp.exmail.qq.com"
# 465 + SSL（首选）→ 587 + STARTTLS（备用）；25 端口常被运营商封禁，不尝试
FALLBACK_PROFILES = [(465, "ssl"), (587, "starttls")]

CONNECT_ERRORS = (
    socket.timeout,
    socket.gaierror,
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    ssl.SSLError,
    smtplib.SMTPConnectError,
    OSError,
)


def _profiles(cfg: Dict[str, Any]) -> list:
    """把配置里的 (port, tls_mode) 排在最前，其余作为回退。"""
    smtp = cfg.get("smtp") or cfg
    first = (int(smtp.get("port") or 465), (smtp.get("tls_mode") or "ssl").lower())
    out = [first]
    for prof in FALLBACK_PROFILES:
        if prof != first:
            out.append(prof)
    return out


def _connect(host: str, port: int, mode: str, timeout: int = 12):
    if mode == "ssl":
        ctx = ssl.create_default_context()
        srv = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    else:
        srv = smtplib.SMTP(host, port, timeout=timeout)
        if mode == "starttls":
            srv.ehlo()
            ctx = ssl.create_default_context()
            srv.starttls(context=ctx)
            srv.ehlo()
    return srv


def _classify(exc: BaseException) -> Tuple[str, str]:
    """把异常归到三类之一，返回 (error_type, 人类可读说明)。"""
    msg = str(exc) or exc.__class__.__name__
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth", "认证失败：账号或客户端专用密码不正确（若开启了安全登录，必须用 16 位客户端专用密码）"
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return "rejected", "被拒信：收件人或发件人地址被服务器拒绝（%s）" % msg
    if isinstance(exc, smtplib.SMTPDataError):
        code = getattr(exc, "smtp_code", 0)
        if code in (550, 551, 553, 554):
            return "rejected", "被拒信：内容或地址不被接受（%s）" % msg
        return "rejected", "被拒信：可能触发频率或额度限制（%s）" % msg
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "connect", "连接被服务器断开（网络不稳定或端口不对）"
    if isinstance(exc, CONNECT_ERRORS):
        return "connect", "连接失败：无法连上 %s（网络/端口/超时）" % DEFAULT_HOST
    return "connect", "发送失败：%s" % msg[:200]


def _build_message(from_addr: str, to_addr: str, code: str, ttl: int) -> MIMEText:
    minutes = max(1, int(ttl // 60))
    body = (
        "你的登录验证码是：\n\n"
        "    %s\n\n"
        "该验证码 %d 分钟内有效，且只能使用一次。\n"
        "如果这不是你本人的操作，请忽略本邮件；你的设备不会被登录。\n\n"
        "—— TeslaUSB CL 自动发送，请勿直接回复\n"
    ) % (code, minutes)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header("登录验证码 %s" % code, "utf-8")
    msg["From"] = formataddr((str(Header("TeslaUSB CL", "utf-8")), from_addr))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    return msg


def _send_once(cfg: Dict[str, Any], to_addr: str, msg: MIMEText) -> Tuple[bool, str, str]:
    smtp = cfg.get("smtp") or cfg
    host = (smtp.get("host") or DEFAULT_HOST).strip()
    user = (smtp.get("user") or "").strip()
    password = smtp.get("password") or ""
    from_addr = (smtp.get("from_addr") or user).strip()

    last: Optional[BaseException] = None
    for port, mode in _profiles(cfg):
        srv = None
        try:
            srv = _connect(host, port, mode)
            if user:
                srv.login(user, password)
            srv.sendmail(from_addr, [to_addr], msg.as_string())
            return True, "", "%s:%d/%s" % (host, port, mode)
        except BaseException as exc:  # noqa: BLE001 - 需要按类型归类后返回
            last = exc
            # 认证失败不换端口重试：换端口也是同一个账号错
            if isinstance(exc, smtplib.SMTPAuthenticationError):
                break
            continue
        finally:
            if srv is not None:
                try:
                    srv.quit()
                except Exception:
                    try:
                        srv.close()
                    except Exception:
                        pass

    if last is None:
        return False, "connect", "连接失败：没有可用的发送通道"
    etype, detail = _classify(last)
    return False, etype, detail


def send_email_code(cfg: Dict[str, Any], code: str, ttl: int) -> Dict[str, Any]:
    """发送登录验证码。返回 {ok, error_type, message, detail}。"""
    to_addr = (cfg.get("security_email") or "").strip()
    smtp = cfg.get("smtp") or {}
    if not to_addr:
        return {"ok": False, "error_type": "config",
                "message": "未配置安全邮箱，无法发送验证码", "detail": ""}
    if not (smtp.get("user") or "").strip() or not smtp.get("password"):
        return {"ok": False, "error_type": "config",
                "message": "未配置发送邮箱或密码，无法发送验证码", "detail": ""}

    try:
        msg = _build_message((smtp.get("from_addr") or smtp.get("user") or "").strip(), to_addr, code, ttl)
    except Exception as e:  # 构造失败属于配置问题
        return {"ok": False, "error_type": "config", "message": "邮件构造失败", "detail": str(e)[:200]}

    ok, etype, detail = _send_once(cfg, to_addr, msg)
    if ok:
        return {"ok": True, "error_type": "", "message": "已发送", "detail": detail}
    return {"ok": False, "error_type": etype, "message": detail, "detail": detail}


def test_smtp(cfg: Dict[str, Any], to_addr: Optional[str] = None) -> Dict[str, Any]:
    """设置页「测试发信」：真的发一封，验证整条链路（含认证与投递接受）。"""
    to_addr = (to_addr or cfg.get("security_email") or "").strip()
    if not to_addr:
        return {"ok": False, "error_type": "config", "message": "请先填写安全邮箱", "detail": ""}
    return _send_test(cfg, to_addr)


def _send_test(cfg: Dict[str, Any], to_addr: str) -> Dict[str, Any]:
    smtp = cfg.get("smtp") or {}
    from_addr = (smtp.get("from_addr") or smtp.get("user") or "").strip()
    if not (smtp.get("user") or "").strip() or not smtp.get("password"):
        return {"ok": False, "error_type": "config", "message": "请先填写发送邮箱与密码", "detail": ""}
    body = (
        "这是一封测试邮件，用于验证 TeslaUSB CL 的邮件通道是否可用。\n\n"
        "收到本邮件说明：SMTP 连接、认证、投递均正常。\n\n"
        "—— TeslaUSB CL 自动发送，请勿直接回复\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header("TeslaUSB CL 邮件通道测试", "utf-8")
    msg["From"] = formataddr((str(Header("TeslaUSB CL", "utf-8")), from_addr))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    ok, etype, detail = _send_once(cfg, to_addr, msg)
    if ok:
        return {"ok": True, "error_type": "", "message": "测试邮件已发送，请到收件箱查收", "detail": detail}
    return {"ok": False, "error_type": etype, "message": detail, "detail": detail}


def probe_connect(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """只做连接探活（不认证、不发信），用于 D1 的在线判定。"""
    smtp = cfg.get("smtp") or cfg
    host = (smtp.get("host") or DEFAULT_HOST).strip()
    last = ""
    for port, mode in _profiles(cfg):
        srv = None
        try:
            srv = _connect(host, port, mode, timeout=3)
            return {"ok": True, "message": "可达", "detail": "%s:%d/%s" % (host, port, mode)}
        except BaseException as exc:  # noqa: BLE001
            last = str(exc)[:120]
            continue
        finally:
            if srv is not None:
                try:
                    srv.quit()
                except Exception:
                    pass
    return {"ok": False, "message": "不可达", "detail": last}
