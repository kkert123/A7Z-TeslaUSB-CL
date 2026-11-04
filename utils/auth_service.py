#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 访问鉴权服务 —— 仅验证码登录（v2）

设计依据：docs/explanation/登录鉴权设计说明v2-仅验证码登录.md

本模块只负责"状态与判定"，不做 HTTP、不发邮件（发信见 utils/mailer.py）。
分层边界（与设计文档 §3 一致）：
    challenge 签发/校验 → 本模块
    验证码生成/存储/消费 → 本模块
    会话与 epoch 踢人     → 本模块
    限流                  → 本模块（第二道防线）
    审计                  → 本模块
    SMTP 发送             → utils/mailer.py
    HTTP 路由             → routes/auth_routes.py

配置存放：DATA_DIR/web_auth.json（独立 0600 文件）
    为什么不写进 /data/teslausb-web.json：那份文件由 config_manager 的 dataclass
    整体重写，任何不在 dataclass 里的字段都会被下一次 save_config() 抹掉。
"""
import hashlib
import hmac
import ipaddress
import logging
import json
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR

logger = logging.getLogger(__name__)

# ── 文件路径 ────────────────────────────────────────────────────────────
AUTH_FILE = os.path.join(DATA_DIR, "web_auth.json")
RUNTIME_FILE = os.path.join(DATA_DIR, "auth_runtime.json")
AUDIT_FILE = os.path.join(DATA_DIR, "auth_audit.log")
LOCK_FILE = os.path.join(DATA_DIR, ".auth.lock")
SECRET_FILE = "/data/.secret_key"

# ── 参数（与设计文档 §6 一致）─────────────────────────────────────────────
CHALLENGE_TTL = 300          # challenge 票据有效期（秒）
DEFAULT_CODE_TTL = 300       # 邮箱验证码有效期（秒）
CODE_TTL_MIN = 180
CODE_TTL_MAX = 600
MAX_CODE_TRIES = 3           # 单个验证码最多尝试次数
RESEND_COOLDOWN = 60         # 重发冷却（秒）
MAX_RESEND_PER_CHALLENGE = 3 # 同一 challenge 最多签发次数
IP_WINDOW = 600
IP_LIMIT = 5                 # 单 IP 10 分钟最多签发
GLOBAL_WINDOW = 600
GLOBAL_LIMIT = 10            # 全局 10 分钟最多签发
DAILY_LIMIT = 20             # 全局每日最多签发（对齐服务商日限）
LOCK_IP_FAILS = 5            # 密码/验证码连续失败
LOCK_IP_SECONDS = 900
LOCK_GLOBAL_FAILS = 10
LOCK_GLOBAL_SECONDS = 1800
LOCAL_KEY_MIN = 10
LOCAL_KEY_MAX = 64
SESSION_EPOCH_KEY = "epoch"

AP_CIDR = "192.168.42.0/24"  # 设备自带 AP 段，内置不可删
LOOPBACK_CIDR = "127.0.0.0/8"

# ── fcntl（Linux 有；Windows 无，退化无锁，仅影响本地开发并发）──────────
try:
    import fcntl  # type: ignore
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - 开发环境
    fcntl = None  # type: ignore
    _HAS_FCNTL = False


# ══════════════════════════════════════════════════════════════════════
# 基础工具：原子写 / 锁 / 密钥
# ══════════════════════════════════════════════════════════════════════
def _atomic_write_text(path: str, text: str, mode: int = 0o600) -> None:
    """同目录 mkstemp + os.replace 原子替换（M114 修法）。"""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


@contextmanager
def _locked():
    """跨进程互斥（写运行态时用）。读改写必须在锁内完成，否则并发会互相覆盖。"""
    fd = None
    try:
        os.makedirs(os.path.dirname(LOCK_FILE) or ".", exist_ok=True)
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        if _HAS_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fd is not None:
            try:
                if _HAS_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


_cache_secret = None  # type: Optional[bytes]


def _secret_candidates() -> List[str]:
    """密钥候选路径。

    第一项是历史路径 /data/.secret_key（root 600，只有 web 进程读得到）；
    第二项落在应用数据目录，作为无法读取/写入历史路径时的落点。
    **必须多候选**：单路径读不到时旧实现会"每次返回新随机密钥"，
    结果是 challenge 签名与校验的密钥不一致 → 所有票据立刻失效，
    且因为异常被 pass 吞掉，现场看不到任何报错（2026-11-05 真机验证暴露）。
    """
    out = [SECRET_FILE]
    alt = os.path.join(DATA_DIR, ".secret_key")
    if alt not in out:
        out.append(alt)
    return [p for p in out if p]


def get_secret_key() -> bytes:
    """会话/签名密钥：多候选读取，进程内缓存，落盘保证重启后会话不失效。"""
    global _cache_secret
    if _cache_secret:
        return _cache_secret

    for p in _secret_candidates():
        try:
            if os.path.exists(p):
                with open(p, "rb") as f:
                    raw = f.read().strip()
                if len(raw) >= 32:
                    _cache_secret = raw[:64]
                    return _cache_secret
                logger.warning("会话密钥过短（%d 字节），忽略: %s", len(raw), p)
        except OSError as e:
            logger.warning("读取会话密钥失败 %s: %s", p, e)

    raw = secrets.token_hex(32).encode()
    saved = None
    for p in _secret_candidates():
        try:
            _atomic_write_text(p, raw.decode(), mode=0o600)
            saved = p
            logger.info("已生成会话密钥: %s", p)
            break
        except OSError as e:
            logger.warning("写入会话密钥失败 %s: %s", p, e)
    if saved is None:
        logger.warning("会话密钥无法持久化 —— 重启后所有登录会话将失效")
    _cache_secret = raw
    return _cache_secret


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _ua_hash(ua: str) -> str:
    return hashlib.sha256((ua or "").encode("utf-8")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════
# 本地网络判定（D4/D6/D7 共用）
# ══════════════════════════════════════════════════════════════════════
def parse_cidrs(raw: str) -> Tuple[bool, List[str], str]:
    """校验用户手填的局域网段（逗号分隔）。返回 (ok, cidrs, err)。"""
    items = [x.strip() for x in (raw or "").split(",") if x.strip()]
    if len(items) > 4:
        return False, [], "最多 4 个网段"
    out = []
    for it in items:
        try:
            net = ipaddress.ip_network(it, strict=False)
        except ValueError:
            return False, [], "网段格式非法：%s（示例 192.168.1.0/24）" % it
        if net.version != 4:
            return False, [], "仅支持 IPv4 网段"
        if net.prefixlen < 8:
            return False, [], "网段过大：%s（前缀需 ≥8）" % it
        if net.prefixlen == 0:
            return False, [], "禁止 0.0.0.0/0"
        if net.overlaps(ipaddress.ip_network("100.64.0.0/10")):
            return False, [], "不允许把 tailnet 段（100.64.0.0/10）填为局域网"
        out.append(str(net))
    return True, out, ""


def _ip_obj(ip: str):
    try:
        return ipaddress.ip_address(ip)
    except ValueError:
        return None


def is_loopback(ip: str) -> bool:
    """127.0.0.0/8 —— 永久本地，且永久豁免网段校验（安全网 1）。"""
    obj = _ip_obj(ip)
    return bool(obj and obj.is_loopback)


def is_local_network(ip: str, lan_cidrs: Optional[List[str]] = None) -> bool:
    """本地网络判定：loopback ∪ 手填局域网 ∪ AP 段。tailnet 不算本地。"""
    if is_loopback(ip):
        return True
    obj = _ip_obj(ip)
    if obj is None:
        return False
    nets = [AP_CIDR]
    for c in (lan_cidrs or []):
        nets.append(c)
    for c in nets:
        try:
            if obj in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


# ══════════════════════════════════════════════════════════════════════
# 配置读写
# ══════════════════════════════════════════════════════════════════════
def _default_config() -> Dict[str, Any]:
    return {
        "enabled": False,
        "username": "admin",
        "security_email": "",
        "security_email_verified": False,
        "smtp": {
            "host": "smtp.exmail.qq.com",
            "port": 465,
            "tls_mode": "ssl",
            "user": "",
            "from_addr": "",
            "password": "",
        },
        "email_code_ttl": DEFAULT_CODE_TTL,
        "lan_cidrs": [],
        "local_key_hash": "",
        "local_key_salt": "",
        "local_key_is_custom": False,
        "session_epoch": 0,
        "last_mail_ok_at": 0,
        "created_at": 0,
        "updated_at": 0,
    }


def load_config() -> Dict[str, Any]:
    cfg = _read_json(AUTH_FILE, None)
    if not isinstance(cfg, dict):
        return _default_config()
    base = _default_config()
    for k, v in base.items():
        if k not in cfg:
            cfg[k] = v
    if not isinstance(cfg.get("smtp"), dict):
        cfg["smtp"] = base["smtp"]
    else:
        for k, v in base["smtp"].items():
            cfg["smtp"].setdefault(k, v)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    cfg["updated_at"] = time.time()
    _atomic_write_text(AUTH_FILE, json.dumps(cfg, indent=2, ensure_ascii=False), 0o600)


def is_enabled() -> bool:
    """未完成首次启用时**不拦截**任何请求。

    否则升级后立刻把用户锁在门外（配置还没做，验证码也发不出）。
    """
    try:
        return bool(load_config().get("enabled"))
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
# 运行态（挑战 / 验证码 / 限流 / 锁定）
# ══════════════════════════════════════════════════════════════════════
def _default_runtime() -> Dict[str, Any]:
    return {
        "boot_id": _boot_id(),
        "challenges": {},   # nonce -> {issued_wall, issued_mono, ip, ua_hash, code, resend, used}
        "issued": [],       # [wall_ts] 全局签发时间戳（限流用）
        "ip_issued": {},    # ip -> [wall_ts]
        "fails": {"ip": {}, "global": {"count": 0, "until": 0}},
    }


def _load_runtime() -> Dict[str, Any]:
    rt = _read_json(RUNTIME_FILE, None)
    if not isinstance(rt, dict):
        return _default_runtime()
    base = _default_runtime()
    for k, v in base.items():
        rt.setdefault(k, v)
    # 跨重启：monotonic 归零 → 标记为不可用于 TTL，回退 wall 时间
    if rt.get("boot_id") != _boot_id():
        rt["boot_id"] = _boot_id()
        rt["_mono_valid"] = False
    else:
        rt["_mono_valid"] = True
    return rt


def _save_runtime(rt: Dict[str, Any]) -> None:
    rt.pop("_mono_valid", None)
    _atomic_write_text(RUNTIME_FILE, json.dumps(rt, ensure_ascii=False), 0o600)


def _elapsed(entry: Dict[str, Any], now_mono: float) -> float:
    """距签发过去了多久。同一 boot 用单调时钟（不受 NTP 校时影响）。"""
    if entry.get("_mono_ok"):
        return max(0.0, now_mono - float(entry.get("issued_mono") or now_mono))
    return max(0.0, time.time() - float(entry.get("issued_wall") or time.time()))


# ══════════════════════════════════════════════════════════════════════
# Challenge 票据
# ══════════════════════════════════════════════════════════════════════
def _sign(payload: str) -> str:
    return hmac.new(get_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def issue_challenge(ip: str, ua: str) -> str:
    """签发 challenge（页面每次 GET /login 一个）。"""
    nonce = secrets.token_hex(12)
    issued = int(time.time())
    payload = "%s.%d.%s" % (nonce, issued, _ua_hash(ua))
    token = "%s.%s" % (payload, _sign(payload))
    with _locked():
        rt = _load_runtime()
        rt["challenges"][nonce] = {
            "issued_wall": issued,
            "issued_mono": time.monotonic(),
            "ip": ip,
            "ua_hash": _ua_hash(ua),
            "code": None,
            "resend": 0,
            "used": False,
        }
        _prune_challenges(rt)
        _save_runtime(rt)
    return token


def _prune_challenges(rt: Dict[str, Any]) -> None:
    now = time.time()
    dead = [k for k, v in rt["challenges"].items()
            if now - float(v.get("issued_wall") or 0) > CHALLENGE_TTL * 4]
    for k in dead:
        rt["challenges"].pop(k, None)


def parse_challenge(token: str) -> Optional[Dict[str, Any]]:
    """校验签名与格式，返回 {nonce, issued}；失败 None。"""
    try:
        body, sig = (token or "").rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        nonce, issued, ua_h = body.split(".")
        return {"nonce": nonce, "issued": int(issued), "ua_hash": ua_h}
    except (ValueError, TypeError):
        return None


def check_challenge(token: str, ip: str, ua: str, need_unused: bool = True) -> Tuple[bool, str, Optional[Dict]]:
    """返回 (ok, reason, entry)。reason 用于审计，不返回给前端。"""
    parsed = parse_challenge(token)
    if not parsed:
        return False, "bad_signature", None
    if time.time() - parsed["issued"] > CHALLENGE_TTL:
        return False, "expired", None
    if parsed["ua_hash"] != _ua_hash(ua):
        return False, "ua_mismatch", None
    with _locked():
        rt = _load_runtime()
        entry = rt["challenges"].get(parsed["nonce"])
        if not entry:
            return False, "unknown", None
        if float(entry.get("issued_wall") or 0) != parsed["issued"]:
            return False, "stale", None
        if need_unused and entry.get("used"):
            return False, "used", None
    return True, "", {"nonce": parsed["nonce"], "entry": entry}


# ══════════════════════════════════════════════════════════════════════
# 验证码
# ══════════════════════════════════════════════════════════════════════
def _hash_code(code: str, nonce: str) -> str:
    return hmac.new(get_secret_key(), ("%s:%s" % (nonce, code)).encode(), hashlib.sha256).hexdigest()


def _rate_limited(rt: Dict[str, Any], ip: str) -> Optional[str]:
    """第二道防线：返回 None 表示放行，否则返回限流原因。"""
    now = time.time()
    issued = [t for t in rt.get("issued", []) if now - t < GLOBAL_WINDOW]
    if len(issued) >= GLOBAL_LIMIT:
        return "global_10m"
    if len([t for t in issued if now - t < 86400]) >= DAILY_LIMIT:
        return "daily"
    ip_list = [t for t in rt.get("ip_issued", {}).get(ip, []) if now - t < IP_WINDOW]
    if len(ip_list) >= IP_LIMIT:
        return "ip_10m"
    return None


def _check_lock(rt: Dict[str, Any], ip: str) -> Optional[int]:
    now = time.time()
    g = rt["fails"]["global"]
    if g.get("until", 0) > now:
        return int(g["until"] - now)
    ipf = rt["fails"]["ip"].get(ip) or {}
    if ipf.get("until", 0) > now:
        return int(ipf["until"] - now)
    return None


def request_code(challenge: str, ip: str, ua: str) -> Dict[str, Any]:
    """发码。返回 {ok, error_code, message, sent_to, cooldown, expires_in}。

    四道闸（设计 §2.2）：
      1 challenge 有效  2 UA 一致（IP 变化只记录）  3 无未过期有效码  4 生成并落盘成功
    限流是第五层，在发信前判断。
    """
    ok, reason, ctx = check_challenge(challenge, ip, ua)
    if not ok:
        audit("code_request_reject", ip=ip, ua=ua, reason=reason)
        return {"ok": False, "error_code": 4002, "message": "请求已失效，请刷新页面重试"}

    cfg = load_config()
    nonce = ctx["nonce"]
    ttl = int(cfg.get("email_code_ttl") or DEFAULT_CODE_TTL)
    ttl = max(CODE_TTL_MIN, min(CODE_TTL_MAX, ttl))
    now_mono = time.monotonic()

    with _locked():
        rt = _load_runtime()
        entry = rt["challenges"].get(nonce) or {}

        # 闸 3：已有未过期有效码 → 不重发（连点只收一封）
        c = entry.get("code") or None
        if c and not c.get("used") and _elapsed(c, now_mono) < ttl and c.get("tries", 0) < MAX_CODE_TRIES:
            left = int(ttl - _elapsed(c, now_mono))
            return {"ok": True, "reused": True, "error_code": 0,
                    "message": "验证码已发送，请查收邮箱",
                    "sent_to": mask_email(cfg.get("security_email", "")),
                    "cooldown": RESEND_COOLDOWN, "expires_in": left}

        if int(entry.get("resend") or 0) >= MAX_RESEND_PER_CHALLENGE:
            return {"ok": False, "error_code": 4004, "message": "重发次数过多，请刷新页面重试"}

        lock = _check_lock(rt, ip)
        if lock:
            return {"ok": False, "error_code": 4005, "message": "尝试过于频繁，请 %d 秒后再试" % lock}

        limited = _rate_limited(rt, ip)
        if limited:
            audit("code_request_limited", ip=ip, ua=ua, reason=limited)
            return {"ok": False, "error_code": 4004, "message": "请求过于频繁，请稍后再试"}

        # 闸 4：先生成并落盘，再发信
        code = "%06d" % secrets.randbelow(1000000)
        entry["code"] = {
            "hash": _hash_code(code, nonce),
            "issued_wall": time.time(),
            "issued_mono": now_mono,
            "_mono_ok": rt.get("_mono_valid", True),
            "tries": 0,
            "used": False,
        }
        entry["resend"] = int(entry.get("resend") or 0) + 1
        entry["ip"] = ip
        rt["challenges"][nonce] = entry
        rt.setdefault("issued", []).append(time.time())
        rt.setdefault("ip_issued", {}).setdefault(ip, []).append(time.time())
        # 清理老化计数
        rt["issued"] = [t for t in rt["issued"] if time.time() - t < 86400]
        rt["ip_issued"][ip] = [t for t in rt["ip_issued"][ip] if time.time() - t < IP_WINDOW]
        _save_runtime(rt)

    # 发信（在锁外，避免 SMTP 超时阻塞其它请求）
    from utils.mailer import send_email_code
    result = send_email_code(cfg, code, ttl)

    if not result.get("ok"):
        audit("code_send_failed", ip=ip, ua=ua, error_type=result.get("error_type"), detail=result.get("detail", "")[:200])
        return {"ok": False, "error_code": 4006,
                "message": result.get("message") or "验证码发送失败，请稍后重试",
                "error_type": result.get("error_type")}

    cfg["last_mail_ok_at"] = time.time()
    save_config(cfg)
    audit("code_sent", ip=ip, ua=ua, sent_to=mask_email(cfg.get("security_email", "")))
    return {"ok": True, "reused": False, "error_code": 0, "message": "验证码已发送，请查收邮箱",
            "sent_to": mask_email(cfg.get("security_email", "")),
            "cooldown": RESEND_COOLDOWN, "expires_in": ttl}


def verify_code(challenge: str, code: str, ip: str, ua: str) -> Dict[str, Any]:
    """验证并**原子消费**。成功返回 {ok, error_code, message}。"""
    # need_unused=False：登录成功后 challenge 会被标记 used，
    # 此时再提交同一验证码必须命中"重放"分支（4003 + 告警），
    # 而不是被误判成票据失效（那样看不见重放信号）。
    ok, reason, ctx = check_challenge(challenge, ip, ua, need_unused=False)
    if not ok:
        return {"ok": False, "error_code": 4002, "message": "请求已失效，请刷新页面重试"}

    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return {"ok": False, "error_code": 4003, "message": "请输入 6 位数字验证码"}

    cfg = load_config()
    nonce = ctx["nonce"]
    ttl = max(CODE_TTL_MIN, min(CODE_TTL_MAX, int(cfg.get("email_code_ttl") or DEFAULT_CODE_TTL)))
    now_mono = time.monotonic()

    with _locked():
        rt = _load_runtime()
        entry = rt["challenges"].get(nonce) or {}
        c = entry.get("code")
        if not c:
            return {"ok": False, "error_code": 4003, "message": "验证码不存在，请重新获取"}
        if c.get("used"):
            audit("code_replay", ip=ip, ua=ua)
            return {"ok": False, "error_code": 4003, "message": "验证码已使用，请重新获取"}
        if _elapsed(c, now_mono) > ttl:
            return {"ok": False, "error_code": 4003, "message": "验证码已过期，请重新获取"}
        if int(c.get("tries") or 0) >= MAX_CODE_TRIES:
            return {"ok": False, "error_code": 4003, "message": "尝试次数过多，请重新获取验证码"}

        if not hmac.compare_digest(str(c.get("hash") or ""), _hash_code(code, nonce)):
            c["tries"] = int(c.get("tries") or 0) + 1
            entry["code"] = c
            rt["challenges"][nonce] = entry
            _bump_fail(rt, ip)
            _save_runtime(rt)
            left = MAX_CODE_TRIES - c["tries"]
            audit("code_wrong", ip=ip, ua=ua, tries=c["tries"])
            return {"ok": False, "error_code": 4003,
                    "message": "验证码不正确，还可尝试 %d 次" % max(0, left)}

        # 消费
        c["used"] = True
        entry["code"] = c
        entry["used"] = True
        rt["challenges"][nonce] = entry
        _reset_fails(rt, ip)
        _save_runtime(rt)

    bump_epoch()
    audit("login_success", ip=ip, ua=ua, method="email_code",
          is_local=is_local_network(ip, cfg.get("lan_cidrs")))
    return {"ok": True, "error_code": 0, "message": "登录成功"}


def _bump_fail(rt: Dict[str, Any], ip: str) -> None:
    now = time.time()
    rec = rt["fails"]["ip"].setdefault(ip, {"count": 0, "until": 0})
    rec["count"] = int(rec.get("count") or 0) + 1
    if rec["count"] >= LOCK_IP_FAILS:
        rec["until"] = now + LOCK_IP_SECONDS
        rec["count"] = 0
    g = rt["fails"]["global"]
    g["count"] = int(g.get("count") or 0) + 1
    if g["count"] >= LOCK_GLOBAL_FAILS:
        g["until"] = now + LOCK_GLOBAL_SECONDS
        g["count"] = 0


def _reset_fails(rt: Dict[str, Any], ip: str) -> None:
    rt["fails"]["ip"][ip] = {"count": 0, "until": 0}
    rt["fails"]["global"]["count"] = 0


# ══════════════════════════════════════════════════════════════════════
# 会话与 epoch（踢出其它会话）
# ══════════════════════════════════════════════════════════════════════
def bump_epoch() -> int:
    with _locked():
        cfg = load_config()
        cfg["session_epoch"] = int(cfg.get("session_epoch") or 0) + 1
        save_config(cfg)
        return cfg["session_epoch"]


def current_epoch() -> int:
    return int(load_config().get("session_epoch") or 0)


def build_session_payload(readonly: bool = False) -> Dict[str, Any]:
    return {
        "logged_in": True,
        "epoch": current_epoch(),
        "readonly": bool(readonly),
        "csrf": secrets.token_hex(16),
    }


def check_session(sess: Dict[str, Any]) -> Tuple[bool, str]:
    if not sess.get("logged_in"):
        return False, "not_logged_in"
    # 注意：不能用 `x or -1` —— epoch 合法值包含 0，会被 or 吞成 -1 导致永远"被踢"
    raw_epoch = sess.get("epoch")
    if raw_epoch is None:
        return False, "kicked"
    if int(raw_epoch) != current_epoch():
        return False, "kicked"
    return True, ""


# ══════════════════════════════════════════════════════════════════════
# 本地密钥（L2）
# ══════════════════════════════════════════════════════════════════════
def _hash_local_key(key: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt.encode("utf-8"), 120000).hex()


def set_local_key(custom: Optional[str] = None) -> Tuple[bool, str, str]:
    """设置本地密钥。custom 为空则生成 10 位。

    返回 (ok, plain_key_or_err, message)。**明文只在本次返回**，之后不可取回。
    """
    if custom:
        key = custom.strip()
        if len(key) < LOCAL_KEY_MIN or len(key) > LOCAL_KEY_MAX:
            return False, "", "本地密钥长度需 %d-%d 位" % (LOCAL_KEY_MIN, LOCAL_KEY_MAX)
        if not all(33 <= ord(ch) <= 126 for ch in key):
            return False, "", "本地密钥只能使用可见 ASCII 字符（不含空格）"
        is_custom = True
    else:
        key = secrets.token_hex(5).upper()   # 10 位
        is_custom = False

    salt = secrets.token_hex(16)
    cfg = load_config()
    cfg["local_key_salt"] = salt
    cfg["local_key_hash"] = _hash_local_key(key, salt)
    cfg["local_key_is_custom"] = is_custom
    save_config(cfg)
    audit("local_key_set", custom=is_custom)
    return True, key, "本地密钥已设置（只显示这一次，请立即保存）"


def verify_local_key(key: str) -> bool:
    cfg = load_config()
    salt = cfg.get("local_key_salt") or ""
    h = cfg.get("local_key_hash") or ""
    if not salt or not h:
        return False
    return hmac.compare_digest(h, _hash_local_key((key or "").strip(), salt))


def has_local_key() -> bool:
    return bool(load_config().get("local_key_hash"))


# ══════════════════════════════════════════════════════════════════════
# 审计
# ══════════════════════════════════════════════════════════════════════
AUDIT_MAX_BYTES = 1024 * 1024   # 1MB
AUDIT_KEEP = 3                  # 保留 3 份历史


def _rotate_audit_if_needed() -> None:
    """审计日志轮转（DATA_DIR 不在 utils/log_rotator 的 /var/log 清单内，就地处理）。"""
    try:
        if os.path.getsize(AUDIT_FILE) < AUDIT_MAX_BYTES:
            return
    except OSError:
        return
    try:
        for i in range(AUDIT_KEEP - 1, 0, -1):
            src = "%s.%d" % (AUDIT_FILE, i)
            dst = "%s.%d" % (AUDIT_FILE, i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(AUDIT_FILE, AUDIT_FILE + ".1")
    except OSError:
        pass


def audit(event: str, **fields: Any) -> None:
    """JSONL 审计。**禁止**记录验证码/密码/SMTP 密码。"""
    try:
        _rotate_audit_if_needed()
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()), "event": event}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        os.makedirs(os.path.dirname(AUDIT_FILE) or ".", exist_ok=True)
        fd = os.open(AUDIT_FILE, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def recent_audit(limit: int = 10) -> List[Dict[str, Any]]:
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return list(reversed(out))


# ══════════════════════════════════════════════════════════════════════
# 展示辅助
# ══════════════════════════════════════════════════════════════════════
def mask_email(addr: str) -> str:
    addr = (addr or "").strip()
    if "@" not in addr:
        return ""
    name, domain = addr.split("@", 1)
    if len(name) <= 3:
        shown = name[:1] + "***"
    else:
        shown = name[:3] + "***"
    return "%s@%s" % (shown, domain)


def public_config() -> Dict[str, Any]:
    """给前端的脱敏配置（永不返回密码/hash）。"""
    cfg = load_config()
    smtp = cfg.get("smtp") or {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "username": cfg.get("username") or "admin",
        "security_email": mask_email(cfg.get("security_email", "")),
        "security_email_verified": bool(cfg.get("security_email_verified")),
        "smtp": {
            "host": smtp.get("host", ""),
            "port": smtp.get("port", 465),
            "tls_mode": smtp.get("tls_mode", "ssl"),
            "user": mask_email(smtp.get("user", "")),
            "from_addr": mask_email(smtp.get("from_addr", "")),
            "has_password": bool(smtp.get("password")),
        },
        "email_code_ttl": int(cfg.get("email_code_ttl") or DEFAULT_CODE_TTL),
        "lan_cidrs": cfg.get("lan_cidrs") or [],
        "has_local_key": has_local_key(),
        "local_key_is_custom": bool(cfg.get("local_key_is_custom")),
        "last_mail_ok_at": float(cfg.get("last_mail_ok_at") or 0),
        "recent_audit": recent_audit(10),
    }


# ══════════════════════════════════════════════════════════════════════
# 安全邮箱变更（两阶段：向新邮箱发码 → 回填确认）
# ══════════════════════════════════════════════════════════════════════
PENDING_EMAIL_TTL = 600
PENDING_EMAIL_TRIES = 3


def start_email_change(new_email: str, ip: str, ua: str) -> Dict[str, Any]:
    """向**新邮箱**发测试码。要求：调用方已校验本地网络 + 已登录 + reauth。"""
    new_email = (new_email or "").strip()
    if not new_email or "@" not in new_email or len(new_email) > 254:
        return {"ok": False, "error_code": 4012, "message": "邮箱格式不正确"}
    cfg = load_config()
    smtp_user = ((cfg.get("smtp") or {}).get("user") or "").strip()
    if smtp_user and smtp_user.lower() == new_email.lower():
        return {"ok": False, "error_code": 4012,
                "message": "安全邮箱不能与发送邮箱相同（否则邮箱失守即等于双通道同时失守）"}

    code = "%06d" % secrets.randbelow(1000000)
    with _locked():
        rt = _load_runtime()
        rt["pending_email"] = {
            "addr": new_email,
            "hash": _hash_code(code, "email-change"),
            "issued_wall": time.time(),
            "issued_mono": time.monotonic(),
            "_mono_ok": rt.get("_mono_valid", True),
            "tries": 0,
        }
        _save_runtime(rt)

    from utils.mailer import _send_test
    probe_cfg = dict(cfg)
    probe_cfg["security_email"] = new_email
    result = _send_test(probe_cfg, new_email)
    if not result.get("ok"):
        audit("email_change_send_failed", ip=ip, ua=ua, to=mask_email(new_email),
              error_type=result.get("error_type"))
        return {"ok": False, "error_code": 4006, "message": result.get("message"),
                "error_type": result.get("error_type")}
    audit("email_change_request", ip=ip, ua=ua, to=mask_email(new_email))
    return {"ok": True, "error_code": 0, "message": "测试码已发送至新邮箱，请查收并回填",
            "sent_to": mask_email(new_email), "expires_in": PENDING_EMAIL_TTL}


def confirm_email_change(code: str, ip: str, ua: str) -> Dict[str, Any]:
    """回填测试码 → 保存新邮箱为 verified。"""
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return {"ok": False, "error_code": 4003, "message": "请输入 6 位数字验证码"}
    now_mono = time.monotonic()
    with _locked():
        rt = _load_runtime()
        p = rt.get("pending_email")
        if not p:
            return {"ok": False, "error_code": 4009, "message": "没有待确认的邮箱变更，请重新发起"}
        if _elapsed(p, now_mono) > PENDING_EMAIL_TTL:
            rt["pending_email"] = None
            _save_runtime(rt)
            return {"ok": False, "error_code": 4003, "message": "测试码已过期，请重新发起"}
        if int(p.get("tries") or 0) >= PENDING_EMAIL_TRIES:
            rt["pending_email"] = None
            _save_runtime(rt)
            return {"ok": False, "error_code": 4003, "message": "尝试次数过多，请重新发起"}
        if not hmac.compare_digest(str(p.get("hash") or ""), _hash_code(code, "email-change")):
            p["tries"] = int(p.get("tries") or 0) + 1
            rt["pending_email"] = p
            _save_runtime(rt)
            return {"ok": False, "error_code": 4003,
                    "message": "测试码不正确，还可尝试 %d 次" % max(0, PENDING_EMAIL_TRIES - p["tries"])}
        new_addr = p.get("addr") or ""
        rt["pending_email"] = None
        _save_runtime(rt)

    cfg = load_config()
    old_addr = cfg.get("security_email") or ""
    cfg["security_email"] = new_addr
    cfg["security_email_verified"] = True
    save_config(cfg)
    audit("email_changed", ip=ip, ua=ua, old=mask_email(old_addr), new=mask_email(new_addr))
    return {"ok": True, "error_code": 0, "message": "安全邮箱已更新",
            "old": mask_email(old_addr), "new": mask_email(new_addr),
            "notify_targets": [mask_email(old_addr), mask_email(new_addr)]}


def set_security_email_initial(email: str, code: str) -> Dict[str, Any]:
    """首次配置时的闭环校验（由 /api/security/email/confirm 复用 confirm_email_change）。"""
    return confirm_email_change(code, "", "")


# ══════════════════════════════════════════════════════════════════════
# 登录后跳转目标校验（唯一权威实现）
# ══════════════════════════════════════════════════════════════════════
def safe_relative_path(path: str) -> str:
    """只接受站内相对路径，其余一律回落到 "/"。

    规则（白名单式，不是黑名单）：
      1 只解码一次（防双重编码绕过）
      2 拒绝空/超长/控制字符/反斜杠
      3 必须以单个 "/" 开头（拒绝 //host 协议相对）
      4 urlparse 后 scheme 与 netloc 必须为空
      5 只保留 path + query，丢弃 fragment
    """
    from urllib.parse import unquote, urlparse

    p = unquote(path or "", errors="ignore")
    if not p or len(p) > 512:
        return "/"
    if any(ord(ch) < 0x20 for ch in p):
        return "/"
    if "\\" in p:
        return "/"
    if not p.startswith("/") or p.startswith("//"):
        return "/"
    try:
        parsed = urlparse(p)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc:
        return "/"
    out = parsed.path or "/"
    if parsed.query:
        out += "?" + parsed.query
    return out
