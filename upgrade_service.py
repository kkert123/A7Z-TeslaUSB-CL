"""upgrade_service.py — 一键升级与回退

用法:
    from upgrade_service import do_upgrade, do_rollback, restart_service
    ok, msg = do_upgrade(version, asset_url, sha256, sig_url)
    ok, msg = do_rollback()
"""

import os
import json
import os
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

# 升级进度文件（v0.3.1.34：前端轮询真实进度，替代模拟进度）
UPGRADE_PROGRESS_FILE = "/var/run/upgrade_progress.json"


def _write_progress(step: str, pct: int):
    """写入升级进度（前端每 1s 轮询展示真实步骤）"""
    try:
        with open(UPGRADE_PROGRESS_FILE, "w") as f:
            json.dump({"step": step, "progress": pct}, f)
    except Exception:
        pass


def _clear_progress():
    """升级结束（成功/失败）清除进度文件"""
    try:
        if os.path.exists(UPGRADE_PROGRESS_FILE):
            os.remove(UPGRADE_PROGRESS_FILE)
    except Exception:
        pass


def get_upgrade_progress():
    """读取当前升级进度（供 GET /api/version/upgrade/progress）"""
    try:
        with open(UPGRADE_PROGRESS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


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

def _save_user_data(base_dir):
    """保存 config/、data/ 和 static/thumbnails/ 目录（升级时保留用户配置和缩略图）

    永久保护：无论调用方是否理解 3 元组返回值，static/thumbnails/ 始终会被保存。
    这样即使升级前运行的是旧版（2 元组版），缩略图也不会丢——因为新代码接管后
    _restore_user_data 会自动从 saved_thumbs 恢复。
    """
    import tempfile
    tmpd = tempfile.mkdtemp(prefix='upgrade-keep-')
    saved_cfg = None
    saved_data = None
    saved_thumbs = None
    try:
        old_cfg = os.path.join(base_dir, 'config')
        old_data = os.path.join(base_dir, 'data')
        old_thumbs = os.path.join(base_dir, 'static', 'thumbnails')
        if os.path.isdir(old_cfg):
            saved_cfg = os.path.join(tmpd, 'config')
            shutil.copytree(old_cfg, saved_cfg, symlinks=True)
        if os.path.isdir(old_data):
            saved_data = os.path.join(tmpd, 'data')
            shutil.copytree(old_data, saved_data, symlinks=True)
        # 关键：缩略图保存是"路径驱动"的，不依赖调用者是否解包第三元素
        if os.path.isdir(old_thumbs):
            saved_thumbs = os.path.join(tmpd, 'thumbnails')
            shutil.copytree(old_thumbs, saved_thumbs, symlinks=True)
    except Exception:
        pass
    return saved_cfg, saved_data, saved_thumbs


def _restore_user_data(target_dir, saved_cfg, saved_data, saved_thumbs=None):
    """恢复之前保存的 config/、data/ 和 static/thumbnails/ 目录"""
    if saved_cfg and os.path.isdir(saved_cfg):
        dest = os.path.join(target_dir, 'config')
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(saved_cfg, dest, symlinks=True)
    if saved_data and os.path.isdir(saved_data):
        dest = os.path.join(target_dir, 'data')
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(saved_data, dest, symlinks=True)
    if saved_thumbs and os.path.isdir(saved_thumbs):
        dest = os.path.join(target_dir, 'static', 'thumbnails')
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(saved_thumbs, dest, symlinks=True)

def do_upgrade(new_version, asset_url, sha256_expected, sig_url=None):
    """一键升级。返回 (success, message)

    步骤: 备份 → 下载 → SHA-256 → Ed25519验签 → 解压 → venv → 切symlink → 记录版本
    """
    steps = []

    # ── 0. 前置检查 ──
    # 版本号白名单校验：仅允许 数字.数字.数字(.数字) 格式，防目录穿越/注入
    import re as _re
    if not _re.fullmatch(r'\d+\.\d+\.\d+(\.\d+)?', str(new_version)):
        return False, f"非法版本号: {new_version!r}"

    current_dir = get_current_version_dir()
    if not current_dir:
        return False, "当前部署目录不存在，无法升级"

    new_dir = os.path.join(DEPLOY_BASE, f"teslausb-v{new_version}")
    if os.path.realpath(new_dir) == os.path.realpath(current_dir):
        return False, f"已是最新版本 v{new_version}，无需升级"
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir)

    # ── 1. 下载 + SHA-256 校验（一体化：镜像/直连任一源校验失败自动换源）──
    tarball = os.path.join(tempfile.gettempdir(), f"upgrade-v{new_version}.tar.gz")
    sig_file = None
    try:
        _write_progress("下载升级包并校验 SHA-256", 12)
        steps.append("下载并校验 SHA-256...")
        _download(asset_url, tarball, sha256_expected)
        steps[-1] = "下载完成，SHA-256 校验通过"
        _write_progress("SHA-256 校验通过", 35)

        if sig_url:
            sig_file = tarball + ".sig"
            _download(sig_url, sig_file)
    except Exception as e:
        _clear_progress()
        _cleanup(tarball, sig_file)
        return False, f"下载或校验失败: {e}"

    # ── 2. 双保险：显式 SHA-256 校验（sha256_expected 为空时跳过）──
    if sha256_expected:
        _write_progress("SHA-256 复核", 45)
        ok, msg = _verify_sha256(tarball, sha256_expected)
        if not ok:
            _clear_progress()
            _cleanup(tarball, sig_file)
            return False, f"SHA-256 校验失败: {msg}"
        steps.append(f"SHA-256 复核通过 ({msg[:12]}...)")

    # ── 3. Ed25519 签名验证 ──
    if sig_url and sig_file and os.path.exists(sig_file):
        _write_progress("Ed25519 签名验证", 52)
        steps.append("签名验证...")
        ok, msg = _verify_ed25519(tarball, sig_file)
        if not ok:
            _clear_progress()
            _cleanup(tarball, sig_file)
            return False, f"签名验证失败: {msg}"
        steps[-1] = "签名验证通过"
    elif not sig_url:
        steps.append("(无签名文件，跳过验签)")

    # ── 4. 备份当前版本 ──
    _write_progress("备份当前版本", 60)
    steps.append("备份当前版本...")
    ok, msg = _backup_current()
    if not ok:
        steps[-1] = f"备份警告: {msg}（继续升级）"
    else:
        steps[-1] = f"已备份到 {msg}"

    # ── 5. 保存旧版本数据（升级后恢复，避免 config/wecom.json 等丢失）──
    _write_progress("保存配置", 66)
    steps.append("保存配置...")
    saved_cfg, saved_data, saved_thumbs = _save_user_data(current_dir)

    # ── 6. 解压并安装 ──
    _write_progress("解压安装", 72)
    steps.append("解压安装...")
    # 直接内联 tar + 系统 pip3，不调用 _extract_and_setup
    # 原因：运行中的进程可能使用旧版 upgrade_service，_extract_and_setup 可能还走 venv 路径
    os.makedirs(new_dir, exist_ok=True)
    # 安全解压：校验每个成员路径，拒绝 ../ 或绝对路径（zip-slip 防护）
    try:
        import tarfile as _tf
        with _tf.open(tarball, 'r:*') as _t:
            for _m in _t.getmembers():
                _m_path = _m.name.replace('\\', '/')
                if _m_path.startswith('/') or '..' in _m_path.split('/'):
                    _clear_progress()
                    _cleanup(tarball, sig_file)
                    shutil.rmtree(new_dir, ignore_errors=True)
                    return False, f"升级包包含非法路径: {_m_path!r}，已中止"
            _t.extractall(new_dir)
    except Exception as _e:
        _clear_progress()
        _cleanup(tarball, sig_file)
        shutil.rmtree(new_dir, ignore_errors=True)
        return False, f"解压失败: {_e}"
    # Windows tar 打包丢失 Unix 执行位 → 解压后统一 chmod +x
    # 根因: usb_gadget_init.sh 无 +x → present_usb.sh 报"不存在或不可执行" → mode 服务 failed
    try:
        for root, _, files in os.walk(new_dir):
            for f in files:
                if f.endswith('.sh'):
                    os.chmod(os.path.join(root, f), 0o755)
    except Exception:
        pass
    # 清除 tarball 中可能残留的 venv 目录（旧版本打包遗留）
    _legacy_venv = os.path.join(new_dir, "venv")
    if os.path.isdir(_legacy_venv):
        shutil.rmtree(_legacy_venv, ignore_errors=True)
    # 用系统 pip3 安装依赖（失败不阻塞——Flask 已在系统 python3 预装）
    _write_progress("安装依赖", 84)
    _req = os.path.join(new_dir, "requirements.txt")
    if os.path.exists(_req):
        _pip = shutil.which("pip3") or shutil.which("pip") or "python3 -m pip"
        if " " in _pip:
            _r, _, _e = _run(["python3", "-m", "pip", "install", "-r", _req], timeout=300)
        else:
            _r, _, _e = _run([_pip, "install", "-r", _req], timeout=300)
        if _r != 0:
            steps.append(f"pip 警告: {_e[:120] if _e else 'unknown'}（系统 python3 已预装核心依赖）")
    steps[-1] = "安装完成"
    _write_progress("安装完成", 90)

    # ── 恢复旧版本配置 ──
    _restore_user_data(new_dir, saved_cfg, saved_data, saved_thumbs)

    # ── 6. 切换 symlink ──
    _write_progress("切换版本", 94)
    steps.append("切换版本...")
    if os.path.islink(SYMLINK):
        os.unlink(SYMLINK)
    elif os.path.isdir(SYMLINK):
        shutil.rmtree(SYMLINK)
    os.symlink(new_dir, SYMLINK)

    # ── 7. 记录版本 ──
    _record_version(new_version, sha256_expected, "upgrade")

    _cleanup(tarball, sig_file)

    # 清理旧备份（保留最近 2 个）
    _prune_backups(keep=2)

    _write_progress("等待重启生效", 98)
    steps.append("等待重启生效")  # 重启由 API 层异步执行，避免杀死 HTTP 响应
    # S4：成功返回前清除进度文件（防残留 98% 脏状态导致 progress API 误报 running）
    _clear_progress()
    return True, "\n".join(steps)


def do_upgrade_from_tarball(tarball_path, new_version):
    """从本地 tar.gz 升级（跳过下载+校验，调用方已做）。返回 (success, message)"""
    steps = []
    import re as _re
    if not _re.fullmatch(r'\d+\.\d+\.\d+(\.\d+)?', str(new_version)):
        return False, f"非法版本号: {new_version!r}"
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

    # 保存旧版本 config/data
    saved_cfg, saved_data, saved_thumbs = _save_user_data(current_dir)

    # 解压安装（内联，不调用 _extract_and_setup——运行中进程可能用旧版）
    steps.append("解压安装...")
    os.makedirs(new_dir, exist_ok=True)
    # 安全解压：校验每个成员路径，拒绝 ../ 或绝对路径（zip-slip 防护）
    try:
        import tarfile as _tf
        with _tf.open(tarball_path, 'r:*') as _t:
            for _m in _t.getmembers():
                _m_path = _m.name.replace('\\', '/')
                if _m_path.startswith('/') or '..' in _m_path.split('/'):
                    shutil.rmtree(new_dir, ignore_errors=True)
                    return False, f"升级包包含非法路径: {_m_path!r}，已中止"
            _t.extractall(new_dir)
    except Exception as _e:
        shutil.rmtree(new_dir, ignore_errors=True)
        return False, f"解压失败: {_e}"
    # Windows tar 打包丢失 Unix 执行位 → 解压后统一 chmod +x
    try:
        for root, _, files in os.walk(new_dir):
            for f in files:
                if f.endswith('.sh'):
                    os.chmod(os.path.join(root, f), 0o755)
    except Exception:
        pass
    _lv = os.path.join(new_dir, "venv")
    if os.path.isdir(_lv):
        shutil.rmtree(_lv, ignore_errors=True)
    _req = os.path.join(new_dir, "requirements.txt")
    if os.path.exists(_req):
        _pip = shutil.which("pip3") or shutil.which("pip") or "python3 -m pip"
        if " " in _pip:
            _r, _, _e = _run(["python3", "-m", "pip", "install", "-r", _req], timeout=300)
        else:
            _r, _, _e = _run([_pip, "install", "-r", _req], timeout=300)
        if _r != 0:
            steps.append(f"pip 警告(不影响): {_e[:120] if _e else '?'}")
    steps[-1] = "安装完成"

    # 恢复用户数据
    _restore_user_data(new_dir, saved_cfg, saved_data, saved_thumbs)

    # 切 symlink
    if os.path.islink(SYMLINK):
        os.unlink(SYMLINK)
    elif os.path.isdir(SYMLINK):
        shutil.rmtree(SYMLINK)
    os.symlink(new_dir, SYMLINK)

    _record_version(new_version, "", "manual-upload")
    _prune_backups(keep=2)

    steps.append("等待重启生效")
    return True, "\n".join(steps)


# ═══════════════════════════════════════════════════════════════
# 回退流程
# ═══════════════════════════════════════════════════════════════

def do_rollback(version=None):
    """回退到指定版本（v0.3.1.34：支持任意本地保留版本）。

    Args:
        version: 目标版本号（如 "0.3.1.30"）；None 时回退到上一版本（兼容旧调用）

    Returns:
        (success, message)
    """
    import re as _re

    if version:
        # ── 指定版本路径 ──
        if not _re.fullmatch(r'\d+\.\d+\.\d+(\.\d+)?', str(version)):
            return False, f"非法版本号: {version!r}"
        target_dir = os.path.join(DEPLOY_BASE, f"teslausb-v{version}")
        # 主目录不存在 → 尝试备份目录
        if not os.path.isdir(target_dir):
            backup_dir = os.path.join(BACKUP_DIR, f"teslausb-v{version}")
            if os.path.isdir(backup_dir):
                target_dir = backup_dir
            else:
                return False, f"版本目录不存在: v{version}（该版本可能已被清理）"
        current = get_current_version_dir()
        if current and os.path.realpath(target_dir) == os.path.realpath(current):
            return False, f"当前已是 v{version}，无需回退"
        # S3：校验目标目录完整性（防回退到解压中断残留的损坏目录 → 服务起不来）
        missing = [f for f in ("app.py", "config.py", "requirements.txt") if not os.path.exists(os.path.join(target_dir, f))]
        if missing:
            return False, f"目标版本 v{version} 目录不完整（缺少 {', '.join(missing)}），拒绝回退"
        record_sha = ""
    else:
        # ── 旧逻辑：回退到上一版本 ──
        history = _read_version_history()
        if len(history) < 2:
            return False, "仅有当前版本，无可回退版本"
        prev = history[-2]
        version = prev["version"]
        target_dir = os.path.join(DEPLOY_BASE, f"teslausb-v{version}")
        if not os.path.isdir(target_dir):
            backup_dir = os.path.join(BACKUP_DIR, f"teslausb-v{version}")
            if os.path.isdir(backup_dir):
                target_dir = backup_dir
            else:
                return False, f"版本目录不存在: {target_dir}"
        record_sha = prev.get("sha256", "")

    # 切 symlink
    if os.path.islink(SYMLINK):
        os.unlink(SYMLINK)
    elif os.path.isdir(SYMLINK):
        shutil.rmtree(SYMLINK)
    os.symlink(target_dir, SYMLINK)

    _record_version(version, record_sha, "rollback")

    return True, f"已回退到 v{version}（重启后生效）"


def get_rollback_options():
    """列出所有本地可回退版本（v0.3.1.34）。

    扫描 DEPLOY_BASE/teslausb-v* + 备份目录，排除当前 symlink 指向的版本，
    按版本号降序返回。回退选项与版本目录保留策略（auto_cleanup keep=5）联动——
    只显示设备上实际存在的版本目录。

    Returns:
        [{"version": "0.3.1.32", "dir": "/opt/radxa_data/teslausb-v0.3.1.32", "from_backup": False}, ...]
    """
    import re as _re
    pattern = _re.compile(r'^teslausb-v(\d+\.\d+\.\d+(?:\.\d+)?)$')
    current = get_current_version_dir()
    current_real = os.path.realpath(current) if current else ""

    options = []
    seen = set()
    for base in (DEPLOY_BASE, BACKUP_DIR):
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                m = pattern.match(name)
                if not m:
                    continue
                ver = m.group(1)
                d = os.path.join(base, name)
                if not os.path.isdir(d) or os.path.islink(d):
                    continue
                if current_real and os.path.realpath(d) == current_real:
                    continue  # 排除当前版本
                if ver in seen:
                    continue
                seen.add(ver)
                options.append({"version": ver, "dir": d, "from_backup": base == BACKUP_DIR})
        except OSError:
            continue

    # 按版本号降序（新 → 旧）
    options.sort(key=lambda x: [int(p) for p in x["version"].split(".")], reverse=True)
    return options


def get_rollback_info():
    """返回可回退的版本信息（含备份检测）"""
    history = _read_version_history()

    # 初始化版本历史（首次安装）
    if not history:
        current_ver = getattr(config, 'APP_VERSION', '0')
        if current_ver and current_ver != '0':
            _record_version(current_ver, '', 'init')
        return None

    # 检查备份目录中是否有可回退版本
    if len(history) < 2 and os.path.isdir(BACKUP_DIR):
        backups = sorted(
            [d for d in os.listdir(BACKUP_DIR) if os.path.isdir(os.path.join(BACKUP_DIR, d))],
            reverse=True
        )
        if backups:
            ver = backups[0].replace('teslausb-v', '')
            return {"version": ver, "installed_at": "", "from_backup": True}

    if len(history) < 2:
        return None
    prev = history[-2]
    return {"version": prev["version"], "installed_at": prev.get("installed_at", "")}


# ═══════════════════════════════════════════════════════════════
# 内部实现
# ═══════════════════════════════════════════════════════════════

def _download(url, dest, sha256_expected=None):
    """下载文件 — 国内优先走镜像，直连做回退。

    若指定 sha256_expected，则下载后立即校验 SHA-256：
    校验失败说明该下载源返回了损坏/缓存污染内容，自动换源重试，
    全部源均不通过才抛异常（防止镜像返回错误文件导致升级失败）。
    """
    if sha256_expected:
        _download_verified(url, dest, sha256_expected)
        return
    _download_raw(url, dest)


def _download_raw(url, dest):
    """基础下载（不做校验），单源失败自动切备源"""
    max_retries = 2
    last_error = None

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
                _http_get(try_url, dest)
                return  # 成功
            except Exception as e:
                last_error = e
                if hasattr(e, 'code') and e.code == 504:
                    break
                if retry < max_retries - 1:
                    import time
                    time.sleep((retry + 1) * 5)
                continue
    raise last_error or Exception("下载失败")


def _download_verified(url, dest, sha256_expected):
    """下载 + SHA-256 校验，失败自动换源（镜像 <-> 直连），全部失败抛异常"""
    mirror_url = url.replace(
        "https://github.com/",
        "https://ghproxy.net/https://github.com/"
    )
    urls_to_try = [mirror_url, url] if mirror_url != url else [url]
    errors = []
    for try_url in urls_to_try:
        try:
            _http_get(try_url, dest)
            ok, msg = _verify_sha256(dest, sha256_expected)
            if ok:
                return
            errors.append(f"{try_url}: 校验失败 {msg[:24]}")
        except Exception as e:
            errors.append(f"{try_url}: {e}")
    raise RuntimeError("下载内容校验失败，已尝试全部下载源： " + "; ".join(errors))


def _http_get(url, dest):
    """单次 HTTP 下载到文件"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "A7Z-TeslaUSB-Upgrade/1.0")
    with urllib.request.urlopen(req, timeout=300) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


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

    # 依赖安装：直接使用系统 python3/pip3
    # 注意：systemd service (teslausb-web) 使用 /usr/bin/python3，不依赖 venv
    req = os.path.join(target_dir, "requirements.txt")
    if os.path.exists(req):
        # 优先用系统 pip3，回退到 python3 -m pip
        system_pip = shutil.which("pip3") or shutil.which("pip")
        if system_pip:
            rc, stdout, stderr = _run([system_pip, "install", "-r", req], timeout=300)
        else:
            rc, stdout, stderr = _run(["python3", "-m", "pip", "install", "-r", req], timeout=300)
        if rc != 0:
            # 不阻塞升级：Flask 等核心依赖已在系统 python3 中预装
            import logging
            logging.getLogger(__name__).warning(f"依赖安装失败（服务使用系统 python3，可能不影响运行）: {stderr[:200]}")

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
