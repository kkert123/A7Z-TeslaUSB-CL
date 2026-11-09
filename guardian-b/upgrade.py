#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 升级包校验与应用
==============================
设计依据：`docs/explanation/守护-B端版本同步-设计评审.md` §4 / §6

职责边界（重要）：
  · 本模块只做**校验 + 暂存 + 记账**，不执行任何特权操作。
  · 真正切换版本 / 重启服务由 root-owned 的 `deploy/apply.py` 完成
    （Flask 进程被 systemd 沙箱限制为对代码目录零写权限）。
  · 本模块本身**不在升级包可覆盖范围内**（它在 releases/ 之外，
    路径由 unit 的 ExecStart 决定）。

信任链：
    manifest.json  ← 被 Ed25519 签名（离线开发机，namespace=a7z-guard-pkg）
      └─ files[]   ← 每项 {path, sha256, mode}
           └─ tar 内 guardian-b/files/<path> 逐字节比对

任何一步失败 → 整包拒绝（不做"部分安装"）。
"""

import hashlib
import hmac
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time

# ── 硬限制（防止 zip bomb / 塞满磁盘）───────────────────────────
MAX_PKG_BYTES = 8 * 1024 * 1024        # 单个上传包上限
MAX_UNPACKED_BYTES = 32 * 1024 * 1024  # 解包后总量上限
MAX_ENTRIES = 240                      # 条目数上限
MAX_FILES = 200                        # manifest 文件数上限

VER_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# 包内允许的扩展名：只有 Python 源码。加类型必须改这个文件（人工升级），
# 这正是"锁死"的意图——远程升级不能扩大自己的能力范围。
ALLOWED_SUFFIX = (".py",)

# manifest 里绝对不允许出现的路径（R2：升级不得触碰告警配置与状态）
FORBIDDEN_PATHS = {
    "guard-b.env", "guard-b.env.example",
    "runtime.json", "state.json", "guard-b.log",
    # 验签器与提权 helper 自己绝不能被升级包覆盖 —— 否则"校验"这一层
    # 就可以被一次合法签名顺手换掉，锁死形同虚设
    "upgrade.py", "apply.py",
}
FORBIDDEN_PREFIX = ("data/", ".trusted/", ".upgrade", "releases/", "venv/", "deploy/", "tls/")

# 签名身份与命名空间（namespace 与 A 自身升级包的 `file` 隔离，
# 防止把发给 A 的签名挪用作 B 包签名）
SIGNER_IDENTITY = "a7z-guard"
SIGN_NAMESPACE = "a7z-guard-pkg"

CONFIRM_TTL_S = 600
STAGED_KEEP = 1


class UpgradeError(Exception):
    """带稳定错误码的校验失败。A 端按 code 翻译文案，不解析 message。"""

    def __init__(self, code, detail=""):
        Exception.__init__(self, code + (": " + detail if detail else ""))
        self.code = code
        self.detail = detail


# ── 小工具 ────────────────────────────────────────────────────
def vtuple(s):
    """版本号 → 可比较元组。字符串比较会把 1.0.10 判成小于 1.0.9。"""
    try:
        return tuple(int(x) for x in str(s).strip().split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)


def atomic_write(path, data, mode=0o600):
    """原子写（mkstemp + fsync + os.replace，tmp 名不带 pid）。"""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def read_text(path, limit=65536):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(limit)
    except (OSError, ValueError):
        return ""


def sha256_file(path, chunk=65536):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def has_ssh_keygen():
    return shutil.which("ssh-keygen") is not None


# ── 签名校验 ──────────────────────────────────────────────────
def _has_principal(allowed_signers_text, identity):
    """allowed_signers 里是否有**以 identity 开头**的一行。

    不能写成 `identity in text.split()`：公钥行的 comment 字段常写成同一个名字
    （`ssh-keygen -C a7z-guard`），那样换掉 principal 仍能骗过检查
    （2026-11-08 单测 h1 实证）。必须逐行看第一个字段。
    """
    for line in allowed_signers_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(None, 1)
        if parts and parts[0] == identity:
            return True
    return False


def verify_manifest_sig(manifest_bytes, sig_path, allowed_signers):
    """用 ssh-keygen -Y verify 验 manifest 签名。

    与 A 端 `upgrade_service._verify_ed25519` 同一套机制，但有两点不同：
      1. allowed_signers 用 mkstemp（A 端写在 tempfile.gettempdir() 的可预测路径）；
      2. namespace 独立（`a7z-guard-pkg`），与 A 自身升级包的 `file` 隔离。
    """
    if not has_ssh_keygen():
        raise UpgradeError("ssh_keygen_missing",
                           "宿主机缺少 openssh-client，无法验签")
    if not os.path.isfile(allowed_signers):
        raise UpgradeError("no_pubkey", "未安装签名公钥 %s" % allowed_signers)
    if not os.path.isfile(sig_path):
        raise UpgradeError("no_signature", "包内缺 manifest.json.sig")

    allowed_txt = read_text(allowed_signers, 8192).strip()
    if not _has_principal(allowed_txt, SIGNER_IDENTITY):
        # 公钥文件被替换成别的身份 → 明确报出来，而不是"验签失败"含糊过去
        raise UpgradeError("pubkey_identity_mismatch",
                           "allowed_signers 中无 %s 身份" % SIGNER_IDENTITY)

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".allowed-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(allowed_txt + "\n")
        os.chmod(tmp, 0o600)
        r = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", tmp,
             "-I", SIGNER_IDENTITY, "-n", SIGN_NAMESPACE, "-s", sig_path],
            input=manifest_bytes, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise UpgradeError("sig_verify_timeout")
    except OSError as exc:
        raise UpgradeError("sig_verify_failed", repr(exc))
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)

    if r.returncode != 0:
        msg = (r.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise UpgradeError("bad_signature", msg or "signature invalid")
    return True


# ── manifest 解析 ─────────────────────────────────────────────
def parse_manifest(raw):
    """解析并做结构校验。返回规范化的 dict。"""
    try:
        m = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise UpgradeError("manifest_not_json")
    if not isinstance(m, dict):
        raise UpgradeError("manifest_not_object")
    if m.get("format") != 1:
        raise UpgradeError("manifest_format_unsupported", str(m.get("format")))
    if m.get("kind") != "guardian-b":
        raise UpgradeError("manifest_kind_mismatch", str(m.get("kind")))

    version = str(m.get("version") or "").strip()
    if not VER_RE.match(version):
        # 版本号只用于展示与比较，绝不参与路径拼接 —— 这里再挡一次
        raise UpgradeError("bad_version", version)

    files = m.get("files")
    if not isinstance(files, list) or not files:
        raise UpgradeError("manifest_no_files")
    if len(files) > MAX_FILES:
        raise UpgradeError("too_many_files", str(len(files)))

    seen = set()
    norm = []
    for it in files:
        if not isinstance(it, dict):
            raise UpgradeError("bad_file_entry")
        p = str(it.get("path") or "").replace("\\", "/")
        h = str(it.get("sha256") or "").lower()
        mode = str(it.get("mode") or "0644")
        if not p or p.startswith("/") or ".." in p.split("/"):
            raise UpgradeError("bad_path", p)
        np = posixpath.normpath(p)
        if np != p or np.startswith("..") or "/" in p:
            # 只允许 releases/<ver>/ 下的**平铺文件**：B 端的模块都是单层，
            # 多一层就说明包结构变了，需要人工改 verifier（锁死的一部分）
            raise UpgradeError("bad_path_layout", p)
        if np in FORBIDDEN_PATHS or np.startswith(FORBIDDEN_PREFIX):
            raise UpgradeError("forbidden_path", np)
        if not np.endswith(ALLOWED_SUFFIX):
            raise UpgradeError("file_type_not_allowed", np)
        if not SHA_RE.match(h):
            raise UpgradeError("bad_sha256", np)
        if mode not in ("0644", "0755"):
            raise UpgradeError("bad_mode", "%s=%s" % (np, mode))
        if np in seen:
            raise UpgradeError("duplicate_path", np)
        seen.add(np)
        norm.append({"path": np, "sha256": h, "mode": mode})

    froms = m.get("from")
    if not isinstance(froms, list) or not froms:
        raise UpgradeError("manifest_no_from")
    froms = [str(x).strip() for x in froms]
    for x in froms:
        if not VER_RE.match(x):
            raise UpgradeError("bad_from_version", x)

    removes = m.get("removes") or []
    if not isinstance(removes, list):
        raise UpgradeError("bad_removes")
    norm_removes = []
    for x in removes:
        r = posixpath.normpath(str(x).replace("\\", "/"))
        if ("/" in r) or r.startswith("..") or not r.endswith(ALLOWED_SUFFIX):
            raise UpgradeError("bad_removes_entry", r)
        if r in seen:
            # 同一个文件既"新增"又"删除" → manifest 自相矛盾，不做猜测
            raise UpgradeError("remove_conflicts_add", r)
        norm_removes.append(r)

    return {
        "format": 1,
        "kind": "guardian-b",
        "version": version,
        "from": froms,
        "files": norm,
        "removes": norm_removes,
    }


# ── 安全解包 ──────────────────────────────────────────────────
def _member_rel_path(name):
    """tar 条目名 → 相对 releases/<ver>/ 的路径；不合法直接抛。"""
    n = str(name).replace("\\", "/")
    if n.startswith("/") or n.startswith("./"):
        raise UpgradeError("absolute_path", n)
    np = posixpath.normpath(n)
    if np.startswith("../") or np == ".." or "/../" in np:
        raise UpgradeError("path_traversal", n)
    parts = np.split("/")
    if len(parts) != 3 or parts[0] != "guardian-b" or parts[1] != "files":
        # 允许 guardian-b/manifest.json 与 .sig（在别处单独取）
        if len(parts) == 2 and parts[0] == "guardian-b" and parts[1] in (
                "manifest.json", "manifest.json.sig"):
            return None
        raise UpgradeError("unexpected_entry", np)
    return parts[2]


def inspect_tar(tf, expected_paths):
    """第一遍：只体检不动盘。返回 {rel_path: member}。"""
    found = {}
    total = 0
    count = 0
    for m in tf.getmembers():
        count += 1
        if count > MAX_ENTRIES:
            raise UpgradeError("too_many_entries", str(count))
        if m.isdir():
            continue
        rel = _member_rel_path(m.name)
        if rel is None:
            continue
        # 符号链接/硬链接/设备/管道一律拒：我们不需要任何链接，
        # 而链接是"解包写穿目录"最经典的载体
        if not m.isfile():
            raise UpgradeError("not_regular_file", m.name)
        if m.mode & 0o7000:
            raise UpgradeError("setuid_bit", "%s mode=%o" % (m.name, m.mode))
        if rel not in expected_paths:
            raise UpgradeError("unexpected_file", rel)
        if rel in found:
            raise UpgradeError("duplicate_entry", rel)
        total += m.size
        if total > MAX_UNPACKED_BYTES:
            raise UpgradeError("too_large", str(total))
        found[rel] = m
    missing = expected_paths - set(found)
    if missing:
        raise UpgradeError("missing_file", ",".join(sorted(missing)))
    return found


def extract_selected(tf, members, dest, modes):
    """第二遍：只解体检通过的条目，且写前再核一次路径。"""
    for rel, m in members.items():
        target = os.path.join(dest, rel)
        real_dest = os.path.realpath(dest)
        if not os.path.realpath(target).startswith(real_dest + os.sep):
            raise UpgradeError("escape_dest", rel)
        src = tf.extractfile(m)
        if src is None:
            raise UpgradeError("unreadable_entry", rel)
        os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
        # O_NOFOLLOW：即便上面漏了什么，也不会顺着链接写出去
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags, 0o644)
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out, 65536)
                out.flush()
                os.fsync(out.fileno())
        finally:
            src.close()
        os.chmod(target, int(modes.get(rel, "0644"), 8))


# ── 主流程 ────────────────────────────────────────────────────
class Journal(object):
    """升级账本。任意时刻断电都要能从这里恢复（§6.1）。"""

    def __init__(self, path):
        self.path = path
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except FileNotFoundError:
            return {}
        except PermissionError:
            # 静默吞掉会让「读不到」伪装成「没有任务」（state=idle），
            # 现场表现就是 A 端永远等不到 healthy、卡片停在「正在切换」（M138）
            logging.getLogger("guard-b.upgrade").warning(
                "journal 无读权限（%s）：多半是 root helper 写成了 0600，"
                "状态将显示为 idle；执行 chown guardb:guardb 该文件即可恢复", self.path)
        except (OSError, ValueError) as exc:
            logging.getLogger("guard-b.upgrade").warning(
                "journal 读取失败（%s）: %r", self.path, exc)
        return {}

    def save(self):
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False).encode("utf-8"), 0o600)

    def set(self, **kw):
        self.data.update(kw)
        self.data["updated_at"] = int(time.time())
        self.save()


def read_floor(path):
    t = read_text(path, 64).strip()
    return t if VER_RE.match(t) else ""


def _secret(path):
    """确认码用的一次性密钥。缺失则生成（0600）。"""
    s = read_text(path, 128).strip()
    if len(s) >= 32:
        return s
    s = secrets.token_hex(32)
    atomic_write(path, s.encode("utf-8"), 0o600)
    return s


def make_code(secret, job, pkg_sha):
    msg = ("%s|%s" % (job, pkg_sha)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:6].upper()


def _cleanup_staged(staged_root, keep):
    try:
        names = [n for n in os.listdir(staged_root) if n.startswith("v-")]
    except OSError:
        return
    names.sort(key=lambda n: os.path.getmtime(os.path.join(staged_root, n)) if
               os.path.exists(os.path.join(staged_root, n)) else 0)
    while len(names) > keep:
        n = names.pop(0)
        shutil.rmtree(os.path.join(staged_root, n), ignore_errors=True)


def stage_package(tar_path, paths, current_version):
    """校验整包并暂存 → (job, manifest, staged_dir, pkg_sha256)。失败抛 UpgradeError。"""
    if not os.path.isfile(tar_path):
        raise UpgradeError("pkg_missing")
    if os.path.getsize(tar_path) > MAX_PKG_BYTES:
        raise UpgradeError("pkg_too_large")

    staging_root = paths["staging"]
    os.makedirs(staging_root, exist_ok=True)

    work = tempfile.mkdtemp(prefix="chk-", dir=staging_root)
    try:
        # ① 先取出 manifest 与 sig（不解其它文件）
        # 只认顶层 `guardian-b/manifest.json(.sig)`，且**先到者胜**：
        # 早先版本对 sig 是后写覆盖先写，与 manifest 的取法不一致
        # （tar 里塞两个同名条目时行为不可预测）。
        manifest = None
        sig_bytes = None
        sig_path = os.path.join(work, "manifest.json.sig")
        try:
            with tarfile.open(tar_path, "r:gz") as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    np = posixpath.normpath(m.name.replace("\\", "/"))
                    if np == "guardian-b/manifest.json" and manifest is None:
                        f = tf.extractfile(m)
                        manifest = f.read(256 * 1024) if f else None
                    elif np == "guardian-b/manifest.json.sig" and sig_bytes is None:
                        f = tf.extractfile(m)
                        if f:
                            sig_bytes = f.read(16 * 1024)
        except (tarfile.TarError, OSError) as exc:
            raise UpgradeError("bad_tar", str(exc)[:120])

        if not manifest:
            raise UpgradeError("no_manifest")
        if sig_bytes:
            with open(sig_path, "wb") as out:
                out.write(sig_bytes)

        # ② 验签（对 manifest 原始字节，不是对 tar）
        verify_manifest_sig(manifest, sig_path, paths["pubkey"])

        # ③ 结构 + 版本策略
        man = parse_manifest(manifest)
        version = man["version"]
        if vtuple(version) <= vtuple(current_version):
            raise UpgradeError("not_newer", "%s <= %s" % (version, current_version))
        floor = read_floor(paths["floor"])
        if floor and vtuple(version) <= vtuple(floor):
            raise UpgradeError("below_floor", "%s <= %s" % (version, floor))
        if man["from"] and current_version not in man["from"]:
            raise UpgradeError("incompatible_from", current_version)

        # ④ 解包（体检 → 落地 → 逐文件核 hash）
        expected = set(f["path"] for f in man["files"])
        modes = {f["path"]: f["mode"] for f in man["files"]}
        dest = os.path.join(work, "files")
        os.makedirs(dest, exist_ok=True)
        try:
            with tarfile.open(tar_path, "r:gz") as tf:
                members = inspect_tar(tf, expected)
                extract_selected(tf, members, dest, modes)
        except (tarfile.TarError, OSError) as exc:
            raise UpgradeError("bad_tar", str(exc)[:120])

        for f in man["files"]:
            got = sha256_file(os.path.join(dest, f["path"]))
            if not hmac.compare_digest(got, f["sha256"]):
                raise UpgradeError("hash_mismatch",
                                   "%s 期望 %s 实际 %s" % (f["path"], f["sha256"][:8], got[:8]))

        # ⑤ 落位到 staged/v-<version>-<job>
        job = secrets.token_hex(8)
        pkg_sha = sha256_file(tar_path)
        staged = os.path.join(staging_root, "v-%s-%s" % (version, job))
        os.makedirs(staged, exist_ok=True)
        for name in os.listdir(dest):
            shutil.move(os.path.join(dest, name), os.path.join(staged, name))
        with open(os.path.join(staged, "manifest.json"), "wb") as f:
            f.write(manifest)
        # **签名必须一起放到 staged**：否则 apply.py 拿到的那份 manifest
        # 只是一个"自称"的清单——它把文件和清单一起改掉，重新核 hash 依然全过，
        # 等于这层校验根本不存在（2026-11-08 审查实证）。
        if not sig_bytes:
            raise UpgradeError("no_signature")
        with open(os.path.join(staged, "manifest.json.sig"), "wb") as f:
            f.write(sig_bytes)

        _cleanup_staged(staging_root, STAGED_KEEP)
        return job, man, staged, pkg_sha
    finally:
        shutil.rmtree(work, ignore_errors=True)


def confirm(journal, code, secret_path):
    """校验确认码并推进到 committed。返回 (ok, code_or_reason)。"""
    d = journal.data
    if not d or d.get("state") != "verified":
        return False, "no_pending"
    exp = int(d.get("code_exp") or 0)
    if time.time() > exp:
        journal.set(state="failed", error="confirm_expired")
        return False, "confirm_expired"
    want = make_code(_secret(secret_path), d.get("job", ""), d.get("pkg_sha256", ""))
    if not hmac.compare_digest(str(code or "").strip().upper(), want):
        d["confirm_attempts"] = int(d.get("confirm_attempts") or 0) + 1
        if d["confirm_attempts"] >= 5:
            journal.set(state="failed", error="too_many_attempts")
        else:
            journal.save()
        return False, "bad_code"
    journal.set(state="committed", error="")
    return True, ""


def public_status(journal):
    """给 A 端看的状态（不回确认码、不回内部路径）。"""
    d = journal.data or {}
    return {
        "job": d.get("job", ""),
        "state": d.get("state", "idle"),
        "version": d.get("version", ""),
        "from_version": d.get("from_version", ""),
        "error": d.get("error", ""),
        "updated_at": int(d.get("updated_at") or 0),
        "confirm_expires_in": max(0, int(d.get("code_exp") or 0) - int(time.time())),
    }


def resume_state(journal):
    """进程启动时：committed 却一直没 healthy，说明 apply 半途失败 → 交给 helper 判定。

    这里只做标记，不猜结果（猜错会把正常升级判成回滚）。
    """
    d = journal.data or {}
    if d.get("state") == "committed":
        d["resume_checked_at"] = int(time.time())
        journal.save()
        return "committed"
    return d.get("state", "idle")
