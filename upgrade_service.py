"""upgrade_service.py — 一键升级与回退

用法:
    from upgrade_service import do_upgrade, do_rollback, restart_service
    ok, msg = do_upgrade(version, asset_url, sha256, sig_url)
    ok, msg = do_rollback()
"""

import os
import json
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import datetime
import config

DEPLOY_BASE = "/opt/radxa_data"
SYMLINK = os.path.join(DEPLOY_BASE, "teslausb")
BACKUP_DIR = os.path.join(DEPLOY_BASE, "teslausb-backups")
VERSION_FILE = os.path.join(config.DATA_DIR, "version_history.json")


def _run(cmd_args, timeout=120):
    """执行命令，返回 (returncode, stdout, stderr)"""
    r = subprocess.run(cmd_args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def get_current_version_dir():
    if os.path.islink(SYMLINK):
        target = os.readlink(SYMLINK)
        return target if os.path.isabs(target) else os.path.join(DEPLOY_BASE, target)
    if os.path.isdir(SYMLINK):
        return SYMLINK
    return None


# ═══════════════════════════════════════════════════════════════
# 升级流程
# ═══════════════════════════════════════════════════════════════

def do_upgrade(new_version, asset_url, sha256_expected, sig_url=None):
    """一键升级。返回 (success, message)

    步骤: 备份 → 下载 → SHA-256 → Ed25519验签 → 解压 → venv → 切symlink → 记录版本
    """
    steps = []

    # ── 0. 前置检查 ──
    current_dir = get_current_version_dir()
    if not current_dir:
        return False, "当前部署目录不存在，无法升级"

    new_dir = os.path.join(DEPLOY_BASE, f"teslausb-v{new_version}")
    if os.path.realpath(new_dir) == os.path.realpath(current_dir):
        return False, f"已是最新版本 v{new_version}，无需升级"
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir)

    # ── 1. 下载 ──
    tarball = os.path.join(tempfile.gettempdir(), f"upgrade-v{new_version}.tar.gz")
    sig_file = None
    try:
        steps.append("下载中...")
        _download(asset_url, tarball)
        steps[-1] = "下载完成"

        if sig_url:
            sig_file = tarball + ".sig"
            _download(sig_url, sig_file)
    except Exception as e:
        _cleanup(tarball, sig_file)
        return False, f"下载失败: {e}"

    # ── 2. SHA-256 校验 ──
    steps.append("SHA-256 校验...")
    ok, msg = _verify_sha256(tarball, sha256_expected)
    if not ok:
        _cleanup(tarball, sig_file)
        return False, f"SHA-256 校验失败: {msg}"
    steps[-1] = f"SHA-256 通过 ({msg[:12]}...)"

    # ── 3. Ed25519 签名验证 ──
    if sig_url and sig_file and os.path.exists(sig_file):
        steps.append("签名验证...")
        ok, msg = _verify_ed25519(tarball, sig_file)
        if not ok:
            _cleanup(tarball, sig_file)
            return False, f"签名验证失败: {msg}"
        steps[-1] = "签名验证通过"
    elif not sig_url:
        steps.append("(无签名文件，跳过验签)")

    # ── 4. 备份当前版本 ──
    steps.append("备份当前版本...")
    ok, msg = _backup_current()
    if not ok:
        steps[-1] = f"备份警告: {msg}（继续升级）"
    else:
        steps[-1] = f"已备份到 {msg}"

    # ── 5. 解压并安装 ──
    steps.append("解压安装...")
    ok, msg = _extract_and_setup(tarball, new_dir)
    if not ok:
        _cleanup(tarball, sig_file)
        shutil.rmtree(new_dir, ignore_errors=True)  # 清理残留，便于重试
        return False, f"安装失败: {msg}"
    steps[-1] = "安装完成"

    # ── 6. 切换 symlink ──
    steps.append("切换版本...")
    if os.path.islink(SYMLINK):
        os.unlink(SYMLINK)
    elif os.path.isdir(SYMLINK):
        shutil.rmtree(SYMLINK)
    os.symlink(new_dir, SYMLINK)

    # ── 7. 记录版本 ──
    _record_version(new_version, sha256_expected, "upgrade")

    # ── 8. 重启服务 ──
    steps.append("重启服务...")
    rc, stdout, stderr = _run(["sudo", "systemctl", "restart", "teslausb-web"], timeout=30)
    if rc == 0:
        steps[-1] = "服务已重启"
    else:
        steps[-1] = f"重启警告: {stderr or 'unknown'}"

    _cleanup(tarball, sig_file)

    # 清理旧备份（保留最近 2 个）
    _prune_backups(keep=2)

    return True, "\n".join(steps)


def do_upgrade_from_tarball(tarball_path, new_version):
    """从本地 tar.gz 升级（跳过下载+校验，调用方已做）。返回 (success, message)"""
    steps = []
    current_dir = get_current_version_dir()
    if not current_dir:
        return False, "当前部署目录不存在，无法升级"

    new_dir = os.path.join(DEPLOY_BASE, f"teslausb-v{new_version}")
    if os.path.realpath(new_dir) == os.path.realpath(current_dir):
        return False, f"已是最新版本 v{new_version}，无需升级"
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir)

    # 备份
    steps.append("备份当前版本...")
    ok, msg = _backup_current()
    steps[-1] = f"已备份到 {msg}" if ok else f"备份警告: {msg}（继续升级）"

    # 解压安装
    steps.append("解压安装...")
    ok, msg = _extract_and_setup(tarball_path, new_dir)
    if not ok:
        shutil.rmtree(new_dir, ignore_errors=True)
        return False, f"安装失败: {msg}"
    steps[-1] = "安装完成"

    # 切 symlink
    if os.path.exists(SYMLINK) or os.path.islink(SYMLINK):
        os.unlink(SYMLINK)
    os.symlink(new_dir, SYMLINK)

    _record_version(new_version, "", "manual-upload")
    _prune_backups(keep=2)

    # 重启
    rc, stdout, stderr = _run(["sudo", "systemctl", "restart", "teslausb-web"], timeout=30)
    steps.append("服务已重启" if rc == 0 else f"重启警告: {stderr or 'unknown'}")

    return True, "\n".join(steps)


# ═══════════════════════════════════════════════════════════════
# 回退流程
# ═══════════════════════════════════════════════════════════════

def do_rollback():
    """回退到上一个版本"""
    history = _read_version_history()
    if len(history) < 2:
        return False, "仅有当前版本，无可回退版本"

    prev = history[-2]
    prev_version = prev["version"]
    prev_dir = os.path.join(DEPLOY_BASE, f"teslausb-v{prev_version}")

    # 也检查备份目录
    if not os.path.isdir(prev_dir):
        backup_dir = os.path.join(BACKUP_DIR, f"teslausb-v{prev_version}")
        if os.path.isdir(backup_dir):
            prev_dir = backup_dir
        else:
            return False, f"版本目录不存在: {prev_dir}"

    # 切 symlink
    if os.path.islink(SYMLINK):
        os.unlink(SYMLINK)
    elif os.path.isdir(SYMLINK):
        shutil.rmtree(SYMLINK)
    os.symlink(prev_dir, SYMLINK)

    _record_version(prev_version, prev.get("sha256", ""), "rollback")

    rc, stdout, stderr = _run(["sudo", "systemctl", "restart", "teslausb-web"], timeout=30)
    return True, f"已回退到 v{prev_version}" + ("（服务已重启）" if rc == 0 else "（重启失败，请手动重启）")


def get_rollback_info():
    """返回可回退的版本信息"""
    history = _read_version_history()
    if len(history) < 2:
        return None
    prev = history[-2]
    return {"version": prev["version"], "installed_at": prev.get("installed_at", "")}


# ═══════════════════════════════════════════════════════════════
# 内部实现
# ═══════════════════════════════════════════════════════════════

def _download(url, dest):
    """下载文件 — 国内优先走镜像，直连做回退"""
    max_retries = 2
    last_error = None

    # 国内优先 ghproxy 镜像直连
    mirror_url = url.replace(
        "https://github.com/",
        "https://ghproxy.net/https://github.com/"
    )

    # 镜像优先，直连做 fallback
    urls_to_try = [mirror_url, url]
    if mirror_url == url:
        urls_to_try = [url]

    for try_url in urls_to_try:
        for retry in range(max_retries):
            try:
                req = urllib.request.Request(try_url)
                req.add_header("User-Agent", "A7Z-TeslaUSB-Upgrade/1.0")
                with urllib.request.urlopen(req, timeout=300) as resp:
                    with open(dest, "wb") as f:
                        shutil.copyfileobj(resp, f)
                return  # 成功
            except Exception as e:
                last_error = e
                # GitHub 直连 504 不用重试，直接切镜像
                if hasattr(e, 'code') and e.code == 504:
                    break
                if retry < max_retries - 1:
                    import time
                    time.sleep((retry + 1) * 5)
                continue
    raise last_error or Exception("下载失败")


def _verify_sha256(filepath, expected):
    rc, stdout, stderr = _run(["sha256sum", filepath])
    if rc != 0:
        return False, f"sha256sum 执行失败: {stderr}"
    actual = stdout.split()[0] if stdout else ""
    if actual.lower() != expected.lower():
        return False, f"期望 {expected[:16]}...  实际 {actual[:16]}..."
    return True, actual


def _verify_ed25519(data_file, sig_file):
    pubkey = getattr(config, "UPGRADE_PUBKEY", "")
    if not pubkey:
        return False, "未配置升级公钥"

    # Parse identity from pubkey comment
    identity = pubkey.split()[-1] if pubkey.split() else "a7z-upgrade"

    # Write temporary allowed_signers
    tmp_allowed = os.path.join(tempfile.gettempdir(), "upgrade_allowed")
    with open(tmp_allowed, "w") as f:
        f.write(f"{identity} {pubkey}\n")

    # Pipe file content through ssh-keygen verify
    try:
        with open(data_file, "rb") as fdata:
            r = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", tmp_allowed,
                 "-I", identity, "-n", "file", "-s", sig_file],
                stdin=fdata, capture_output=True, text=True, timeout=30
            )
        return r.returncode == 0, r.stderr.strip() or r.stdout.strip() or "OK"
    finally:
        if os.path.exists(tmp_allowed):
            os.unlink(tmp_allowed)


def _backup_current():
    current = get_current_version_dir()
    if not current or not os.path.isdir(current):
        return False, "当前部署目录不存在"
    dest = os.path.join(BACKUP_DIR, os.path.basename(current))
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(current, dest, symlinks=True)
    return True, dest


def _extract_and_setup(tarball, target_dir):
    os.makedirs(target_dir, exist_ok=True)
    rc, stdout, stderr = _run(
        ["tar", "xzf", tarball, "-C", target_dir],
        timeout=120
    )
    if rc != 0:
        return False, f"解压失败: {stderr}"

    venv_dir = os.path.join(target_dir, "venv")
    rc, stdout, stderr = _run(["python3", "-m", "venv", venv_dir], timeout=120)
    if rc != 0:
        # venv 创建失败（通常是没装 python3-venv 包），尝试复用旧 venv
        # 注意：python3 -m venv 失败前可能已创建部分目录，先清理
        if os.path.exists(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)
        current_dir = get_current_version_dir()
        old_venv = os.path.join(current_dir, "venv") if current_dir else None
        if old_venv and os.path.isdir(old_venv):
            shutil.copytree(old_venv, venv_dir, symlinks=True)
        # 无旧 venv → 跳过，所有 service 用 /usr/bin/python3 直接运行

    pip = os.path.join(venv_dir, "bin", "pip") if os.path.isdir(venv_dir) else None
    req = os.path.join(target_dir, "requirements.txt")
    if pip and os.path.exists(req):
        rc, stdout, stderr = _run([pip, "install", "-r", req], timeout=300)
        if rc != 0:
            return False, f"依赖安装失败: {stderr}"

    return True, target_dir


def _record_version(ver, sha256, source):
    history = _read_version_history()
    history.append({
        "version": ver,
        "installed_at": datetime.now().isoformat(),
        "sha256": sha256,
        "source": source,
    })
    os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
    with open(VERSION_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def _read_version_history():
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _prune_backups(keep=2):
    if not os.path.isdir(BACKUP_DIR):
        return
    dirs = sorted(os.listdir(BACKUP_DIR), reverse=True)
    for d in dirs[keep:]:
        shutil.rmtree(os.path.join(BACKUP_DIR, d), ignore_errors=True)


def _cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass
