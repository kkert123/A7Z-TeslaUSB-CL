#!/usr/bin/env python3
"""
TeslaUSB A7Z — Cloud rclone 服务封装
======================================

Plan A 最小化实现：封装 rclone 命令行，提供云存储操作。

支持的云服务（通过 rclone）：
  - Google Drive
  - (后续可扩展 OneDrive, Dropbox 等)

功能：
  - rclone 安装检测
  - 动态配置 rclone remote
  - 文件列表查询
  - 上传文件/目录（手动触发）
  - 同步状态检查

依赖：
  - rclone（需在 A7Z 上安装: sudo apt install rclone）
  - cloud_oauth_service（提供 OAuth token）

作者：TeslaUSB A7Z 项目
版本：1.0.0
"""

import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── 同步取消标志（跨模块共享）─────────────────────────
# cloud_archive_service 设置此标志，upload_directory 检查并终止 rclone 进程
_sync_cancel_requested = False

def request_sync_cancel():
    """请求取消正在进行的同步"""
    global _sync_cancel_requested
    _sync_cancel_requested = True

def reset_sync_cancel():
    """重置取消标志（同步完成后调用）"""
    global _sync_cancel_requested
    _sync_cancel_requested = False

def is_sync_cancelled():
    """检查是否已请求取消"""
    return _sync_cancel_requested

# ── 常量 ─────────────────────────────────────────────────

# rclone 配置文件路径
RCLONE_CONFIG_DIR = "/opt/radxa_data/teslausb/config/rclone"
RCLONE_CONFIG_FILE = os.path.join(RCLONE_CONFIG_DIR, "rclone.conf")

# 默认 remote 名称
DEFAULT_REMOTE = "gdrive"


# ── rclone 检测与配置 ──────────────────────────────────


def check_rclone_installed() -> Tuple[bool, str]:
    """
    检查 rclone 是否已安装。

    Returns:
        (installed, version_string)
    """
    try:
        result = subprocess.run(
            ["rclone", "version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            version = result.stdout.split('\n')[0] if result.stdout else "unknown"
            return True, version.strip()
        return False, "rclone 返回错误"
    except FileNotFoundError:
        return False, "rclone 未安装"
    except Exception as e:
        return False, str(e)


def configure_rclone(client_id: str, client_secret: str, access_token: str,
                     refresh_token: str, expires_at: int,
                     remote_name: str = DEFAULT_REMOTE) -> Tuple[bool, str]:
    """
    动态配置 rclone Google Drive remote。

    使用已有的 OAuth token（不需要交互式浏览器授权）。
    配置写入 rclone.conf 文件。

    Args:
        client_id: Google OAuth Client ID
        client_secret: Google OAuth Client Secret
        access_token: 当前有效的 access_token
        refresh_token: refresh_token（用于自动刷新）
        expires_at: access_token 过期时间（Unix timestamp）
        remote_name: rclone remote 名称

    Returns:
        (success, message)
    """
    try:
        os.makedirs(RCLONE_CONFIG_DIR, exist_ok=True)

        # 构建 rclone 配置
        config = f"""[{remote_name}]
type = drive
client_id = {client_id}
client_secret = {client_secret}
token = {{{{"access_token":"{access_token}","token_type":"Bearer","refresh_token":"{refresh_token}","expiry":"{datetime.fromtimestamp(expires_at).isoformat()}"}}}}
"""

        with open(RCLONE_CONFIG_FILE, 'w') as f:
            f.write(config)
        os.chmod(RCLONE_CONFIG_FILE, 0o600)

        # 验证配置
        result = subprocess.run(
            ["rclone", f"--config={RCLONE_CONFIG_FILE}", "lsf", f"{remote_name}:"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "RCLONE_CONFIG": RCLONE_CONFIG_FILE},
        )

        if result.returncode == 0:
            logger.info(f"rclone 配置成功 ({remote_name})")
            return True, "rclone 配置成功"
        else:
            err = result.stderr.strip() if result.stderr else "未知错误"
            logger.error(f"rclone 配置验证失败: {err}")
            return False, f"rclone 验证失败: {err[:200]}"

    except Exception as e:
        logger.error(f"rclone 配置失败: {e}")
        return False, str(e)


def get_rclone_env() -> dict:
    """获取 rclone 运行环境变量"""
    return {
        **os.environ,
        "RCLONE_CONFIG": RCLONE_CONFIG_FILE,
    }


# ── 多 Provider 配置 ─────────────────────────────────────

RCLONE_PROVIDERS = {
    "gdrive":    {"label": "Google Drive",     "type": "drive",     "desc": "15GB 免费存储"},
    "onedrive":  {"label": "OneDrive",         "type": "onedrive",  "desc": "Microsoft 账号自带"},
    "dropbox":   {"label": "Dropbox",          "type": "dropbox",   "desc": "2GB 免费存储"},
    "s3":        {"label": "Amazon S3",        "type": "s3",        "desc": "AWS 对象存储"},
    "b2":        {"label": "Backblaze B2",     "type": "b2",        "desc": "低成本云存储"},
    "wasabi":    {"label": "Wasabi",           "type": "s3",        "desc": "S3 兼容热存储"},
    "minio":     {"label": "MinIO",            "type": "s3",        "desc": "自建 S3 兼容"},
    "s3compat":  {"label": "S3 兼容 (通用)",    "type": "s3",        "desc": "MinIO/Ceph/华为OBS/科技云等"},
    "sftp":      {"label": "SFTP",             "type": "sftp",      "desc": "SSH 文件传输"},
    "webdav":    {"label": "WebDAV",           "type": "webdav",    "desc": "HTTP 文件服务"},
    "smb":       {"label": "SMB / CIFS",       "type": "smb",       "desc": "Windows 共享"},
    "ftp":       {"label": "FTP",              "type": "ftp",       "desc": "传统文件传输"},
    "azureblob": {"label": "Azure Blob",       "type": "azureblob", "desc": "微软云对象存储"},
    "swift":     {"label": "OpenStack Swift",  "type": "swift",     "desc": "开源对象存储"},
    "custom":    {"label": "自定义 rclone",     "type": "custom",    "desc": "粘贴 rclone.conf"},
}


def configure_provider(provider_id: str, config_data: dict) -> Tuple[bool, str]:
    """配置任意 rclone provider。
    
    Args:
        provider_id: rclone provider ID (gdrive/onedrive/s3/...)
        config_data: provider-specific configuration dict
    
    Returns:
        (success, message)
    """
    os.makedirs(RCLONE_CONFIG_DIR, exist_ok=True)
    remote_name = config_data.get("remote_name", provider_id)
    provider = RCLONE_PROVIDERS.get(provider_id)
    if not provider:
        return False, f"未知 Provider: {provider_id}"

    rclone_type = provider["type"]

    try:
        if provider_id == "custom":
            # 用户粘贴完整 rclone.conf 块
            block = config_data.get("config_block", "")
            if not block.strip():
                return False, "rclone.conf 配置块为空"
            with open(RCLONE_CONFIG_FILE, "w") as f:
                f.write(block.strip() + "\n")
            os.chmod(RCLONE_CONFIG_FILE, 0o600)
            return _verify_config(remote_name)

        elif rclone_type in ("drive", "onedrive", "dropbox"):
            # OAuth providers — token-based
            token_json = config_data.get("token_json", "").strip()
            client_id = config_data.get("client_id", "")
            client_secret = config_data.get("client_secret", "")
            if not token_json:
                return False, "缺少 access token（请先完成 OAuth 授权）"

            lines = [f"[{remote_name}]", f"type = {rclone_type}"]
            if client_id:
                lines.append(f"client_id = {client_id}")
            if client_secret:
                lines.append(f"client_secret = {client_secret}")
            lines.append(f"token = {token_json}")

            with open(RCLONE_CONFIG_FILE, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(RCLONE_CONFIG_FILE, 0o600)
            return _verify_config(remote_name)

        elif rclone_type == "s3":
            # S3-compatible
            access_key = config_data.get("access_key_id", "")
            secret_key = config_data.get("secret_access_key", "")
            region = config_data.get("region", "").strip()
            bucket = config_data.get("bucket", "")
            endpoint = config_data.get("endpoint", "").strip()
            use_v2 = config_data.get("v2_auth", False)
            if not access_key or not secret_key:
                return False, "缺少 Access Key 或 Secret Key"

            # 自动补全 https:// 前缀（用户可能只输入域名）
            if endpoint and not endpoint.startswith("http"):
                endpoint = f"https://{endpoint}"

            # s3compat 默认无 region（很多国产 S3 不需要或不认 us-east-1）
            if provider_id == "s3compat" and region in ("", "us-east-1"):
                region = ""

            lines = [f"[{remote_name}]", f"type = s3",
                     f"provider = {_s3_provider(provider_id)}",
                     f"access_key_id = {access_key}",
                     f"secret_access_key = {secret_key}"]
            if region:
                lines.append(f"region = {region}")
            if endpoint:
                lines.append(f"endpoint = {endpoint}")
            # 通用 S3 兼容：Path-Style + 跳过桶检查 + 跳过上传后 HEAD 验证
            # 注意：s3compat 不把 bucket 写入 rclone.conf —— 否则与
            # force_path_style 叠加会造成双重路径前缀 (bucket/bucket/key)。
            # bucket 仅保留在 cloud.json remote_path 中。
            if provider_id in ("s3compat",):
                lines.append("force_path_style = true")
                lines.append("no_check_bucket = true")
                lines.append("no_head = true")
                if use_v2:
                    lines.append("v2_auth = true")
            else:
                if bucket:
                    lines.append(f"bucket = {bucket}")
            lines.append("acl = private")

            with open(RCLONE_CONFIG_FILE, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(RCLONE_CONFIG_FILE, 0o600)
            # s3compat: 用 bucket 作为子路径验证（bucket 不在 rclone.conf 中）
            verify_subpath = bucket if provider_id in ("s3compat",) else ""
            return _verify_config(remote_name, verify_subpath)

        else:
            # NAS / generic backends (SFTP, WebDAV, SMB, FTP, Azure, Swift)
            config_block = config_data.get("config_block", "")
            if config_block.strip():
                with open(RCLONE_CONFIG_FILE, "w") as f:
                    f.write(config_block.strip() + "\n")
                os.chmod(RCLONE_CONFIG_FILE, 0o600)
                return _verify_config(remote_name)

            # Form-based config
            host = config_data.get("host", "").strip()
            # 清理用户可能误输入的 UNC 路径格式 (\\192.168.x.x → 192.168.x.x)
            host = host.removeprefix("\\\\").removeprefix("//")
            user = config_data.get("user", "")
            password = config_data.get("password", "")
            port = config_data.get("port", "")
            path = config_data.get("path", "/")
            if not host:
                return False, "缺少服务器地址 (host)"
            # SMB 必须有共享名
            if rclone_type == "smb" and (not path or path in ("/", "")):
                return False, "SMB 需要填写路径 (共享名)"

            # 如果没有提供新密码，从现有 rclone.conf 中保留旧密码
            existing_pass = None
            if not password and os.path.exists(RCLONE_CONFIG_FILE):
                try:
                    with open(RCLONE_CONFIG_FILE, 'r') as f:
                        in_section = False
                        for line in f:
                            line = line.strip()
                            if line == f"[{remote_name}]":
                                in_section = True
                                continue
                            if in_section and line.startswith('['):
                                break  # 进入下一个 section
                            if in_section and (line.startswith('pass ') or line.startswith('pass=')):
                                existing_pass = line.split('=', 1)[1].strip() if '=' in line else line.split(' ', 1)[1].strip()
                                break
                except Exception:
                    pass

            lines = [f"[{remote_name}]", f"type = {rclone_type}",
                     f"host = {host}"]
            if user: lines.append(f"user = {user}")
            # SMB: rclone 不支持 share 参数，共享名需通过路径指定
            # share 名保存到 cloud.json 的 remote_path 中
            if password:
                # Try to obscure password
                try:
                    r = subprocess.run(["rclone", "obscure", password],
                                       capture_output=True, text=True, timeout=5)
                    if r.returncode == 0:
                        lines.append(f"pass = {r.stdout.strip()}")
                    else:
                        lines.append(f"pass = {password}")
                except:
                    lines.append(f"pass = {password}")
            elif existing_pass:
                lines.append(f"pass = {existing_pass}")
            if port: lines.append(f"port = {port}")

            with open(RCLONE_CONFIG_FILE, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(RCLONE_CONFIG_FILE, 0o600)
            # SMB: 验证用户填写的实际路径而非根目录
            _vpath = path.strip("/") if rclone_type == "smb" else ""
            ok, msg = _verify_config(remote_name, _vpath)
            if not ok:
                return True, f"配置已保存，但连接测试失败: {msg}"
            return True, msg

    except Exception as e:
        logger.error(f"配置 Provider 失败: {e}")
        return False, str(e)


def _s3_provider(provider_id: str) -> str:
    """Map provider_id to rclone s3 provider value"""
    return {
        "s3": "AWS", "b2": "B2", 
        "wasabi": "Wasabi", "minio": "Minio",
        "s3compat": "Other",
    }.get(provider_id, "Other")


def _verify_config(remote_name: str, subpath: str = "") -> Tuple[bool, str]:
    """验证 rclone 配置是否有效
    
    Args:
        remote_name: rclone remote 名称
        subpath: 可选子路径，SMB 需传入共享名+路径来验证实际目录
    """
    try:
        target = f"{remote_name}:{subpath}" if subpath else f"{remote_name}:"
        # S3 兼容存储用更长超时（国产 S3 服务响应较慢）
        _timeout = 60 if remote_name == "s3compat" else 15
        r = subprocess.run(
            ["rclone", f"--config={RCLONE_CONFIG_FILE}", "lsf", target],
            capture_output=True, text=True, timeout=_timeout,
            env={**os.environ, "RCLONE_CONFIG": RCLONE_CONFIG_FILE},
        )
        if r.returncode == 0:
            logger.info(f"rclone 配置验证成功 ({remote_name})")
            return True, "连接成功"
        err = r.stderr.strip()
        if "didn't find backend" in err:
            backend = err.split('"')[1] if '"' in err else 'unknown'
            return False, f"rclone 不支持 {backend} 后端（A7Z rclone v1.53 缺少部分后端，请联系管理员升级 rclone 或使用 SFTP/WebDAV 替代）"
        if "unauthorized" in err.lower() or "auth" in err.lower():
            return False, "认证失败，请检查凭据"
        if "couldn't" in err.lower() or "resolve" in err.lower():
            return False, "无法连接服务器，请检查地址"
        return False, err[:200] if err else "连接失败"
    except subprocess.TimeoutExpired:
        return False, "连接超时"
    except Exception as e:
        return False, str(e)


def delete_rclone_config():
    """删除 rclone 配置文件"""
    try:
        if os.path.exists(RCLONE_CONFIG_FILE):
            os.remove(RCLONE_CONFIG_FILE)
        return True, "配置已删除"
    except Exception as e:
        return False, str(e)


def get_configured_provider() -> dict:
    """检测当前已配置的云服务商，并返回可用于表单回显的配置参数。
    
    解析 rclone.conf 读取 remote 名称、类型、以及 NAS 表单关键字段。
    密码字段仅返回 has_password 标志（密码由 rclone obscure 加密，不可逆）。
    
    Returns:
        {
            "provider_id": str or None, "provider_label": str, "type": str,
            "remote_name": str,
            "config": {  # NAS 表单回显用（仅对 smb/sftp/ftp/webdav 有效）
                "host": str, "user": str, "port": str, "path": str,
                "has_password": bool
            }
        }
        provider_id 为 None 表示未配置
    """
    config_keys = {"host", "user", "port", "path", "pass", "endpoint", "bucket", "region", "access_key_id", "secret_access_key"}
    result = {
        "provider_id": None, "provider_label": "", "type": "", "remote_name": "",
        "config": {"host": "", "user": "", "port": "", "path": "", "has_password": False,
                   "endpoint": "", "bucket": "", "region": ""}
    }
    
    if not os.path.exists(RCLONE_CONFIG_FILE):
        return result
    
    try:
        remote_name = None
        rclone_type = None
        parsed = {}  # 存储 section 内所有 key=value
        
        with open(RCLONE_CONFIG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                # 跳过注释和空行
                if not line or line.startswith('#') or line.startswith(';'):
                    continue
                
                # 检测 [section]
                if line.startswith('[') and line.endswith(']'):
                    section = line[1:-1].strip()
                    if section and not remote_name:
                        remote_name = section
                    continue
                
                # 解析 key=value 或 key = value
                if remote_name and ('=' in line):
                    key, _, val = line.partition('=')
                    key = key.strip().lower()
                    val = val.strip()
                    if key in config_keys:
                        parsed[key] = val
                    if key == 'type':
                        rclone_type = val
        
        if remote_name and rclone_type:
            result["remote_name"] = remote_name
            result["type"] = rclone_type
            
            # 尝试直接匹配 provider_id
            if remote_name in RCLONE_PROVIDERS:
                result["provider_id"] = remote_name
                result["provider_label"] = RCLONE_PROVIDERS[remote_name]["label"]
            else:
                for pid, pdef in RCLONE_PROVIDERS.items():
                    if pdef["type"] == rclone_type:
                        result["provider_id"] = pid
                        result["provider_label"] = pdef["label"]
                        break
                else:
                    result["provider_id"] = remote_name
                    result["provider_label"] = remote_name
            
            # 填充 NAS/S3 表单回显参数
            result["config"] = {
                "host": parsed.get("host", ""),
                "user": parsed.get("user", ""),
                "port": parsed.get("port", ""),
                "path": parsed.get("path", ""),
                "has_password": "pass" in parsed,
                "endpoint": parsed.get("endpoint", ""),
                "bucket": parsed.get("bucket", ""),
                "region": parsed.get("region", ""),
            }
                    
    except Exception as e:
        logger.warning(f"解析 rclone.conf 失败: {e}")
    
    return result


# ── 文件操作 ────────────────────────────────────────────


def list_remote_files(remote_name: str = DEFAULT_REMOTE,
                      remote_path: str = "") -> Tuple[bool, list]:
    """
    列出云存储中的文件。

    Args:
        remote_name: rclone remote 名称
        remote_path: 远程路径（相对于 remote 根目录）

    Returns:
        (success, file_list)
        file_list 每个元素: {name, size, modified, is_dir}
    """
    try:
        target = f"{remote_name}:{remote_path}" if remote_path else f"{remote_name}:"

        result = subprocess.run(
            ["rclone", f"--config={RCLONE_CONFIG_FILE}", "--contimeout=10s", "lsjson", target],
            capture_output=True, text=True, timeout=45,
            env=get_rclone_env(),
        )

        if result.returncode != 0:
            err = result.stderr.strip()
            logger.error(f"rclone lsjson 失败: {err}")
            return False, []

        files = json.loads(result.stdout)
        file_list = []
        for f in files:
            file_list.append({
                'name': f.get('Name', ''),
                'size': f.get('Size', 0),
                'size_fmt': _fmt_size(f.get('Size', 0)),
                'modified': f.get('ModTime', ''),
                'is_dir': f.get('IsDir', False),
                'mime_type': f.get('MimeType', ''),
            })

        return True, file_list

    except json.JSONDecodeError as e:
        logger.error(f"rclone JSON 解析失败: {e}")
        return False, []
    except Exception as e:
        logger.error(f"rclone lsjson 异常: {e}")
        return False, []


def upload_file(local_path: str, remote_name: str = DEFAULT_REMOTE,
                remote_path: str = "TeslaUSB/") -> Tuple[bool, str, dict]:
    """
    上传单个文件到云存储。

    Args:
        local_path: 本地文件路径
        remote_name: rclone remote 名称
        remote_path: 远程目标路径（目录）

    Returns:
        (success, message, stats)
        stats: {filename, size_bytes, duration_sec}
    """
    if not os.path.exists(local_path):
        return False, f"文件不存在: {local_path}", {}

    target = f"{remote_name}:{remote_path}"
    filename = os.path.basename(local_path)
    start_time = datetime.now()

    try:
        result = subprocess.run(
            [
                "rclone", f"--config={RCLONE_CONFIG_FILE}",
                "copyto", local_path, f"{target}/{filename}",
                "--stats-one-line",
                "--progress",
            ],
            capture_output=True, text=True, timeout=600,  # 10 分钟超时
            env=get_rclone_env(),
        )

        duration = (datetime.now() - start_time).total_seconds()
        fsize = os.path.getsize(local_path)

        if result.returncode == 0:
            stats = {
                'filename': filename,
                'size_bytes': fsize,
                'size_fmt': _fmt_size(fsize),
                'duration_sec': round(duration, 1),
                'speed_mbps': round(fsize / duration / 1024 / 1024, 2) if duration > 0 else 0,
            }
            logger.info(f"上传成功: {filename} ({_fmt_size(fsize)}, {duration:.1f}s)")
            return True, "上传成功", stats
        else:
            err = result.stderr.strip()[-300:] if result.stderr else "未知错误"
            return False, f"上传失败: {err}", {}

    except subprocess.TimeoutExpired:
        return False, "上传超时（超过 10 分钟）", {}
    except Exception as e:
        return False, str(e), {}


def upload_directory(local_dir: str, remote_name: str = DEFAULT_REMOTE,
                     remote_path: str = "TeslaUSB/", bwlimit: int = 0,
                     progress_callback=None) -> Tuple[bool, str, dict]:
    """
    上传整个目录到云存储（使用 rclone copy）。

    使用 rclone copy（非 sync），只上传新文件，不删除远程已有文件。
    安全策略：云端是备份，永远不因本地缺失而删除云端数据。

    Args:
        local_dir: 本地目录路径
        remote_name: rclone remote 名称
        remote_path: 远程目标路径
        bwlimit: 带宽限制 (KB/s)，0 = 无限制
        progress_callback: 可选，进度回调 callback(pct: int)，pct 范围 0-100

    Returns:
        (success, message, stats)
    """
    if not os.path.isdir(local_dir):
        return False, f"目录不存在: {local_dir}", {}

    # 确保远程路径以 / 结尾，rclone 才能正确识别为目录
    if not remote_path.endswith('/'):
        remote_path += '/'
    target = f"{remote_name}:{remote_path}"
    start_time = datetime.now()

    try:
        # 构建命令：nice + ionice 降低 CPU/IO 优先级
        cmd = ["nice", "-n", "19", "ionice", "-c", "3",
               "rclone", f"--config={RCLONE_CONFIG_FILE}",
               "copy", local_dir, target,
               "--progress",           # 输出实时进度到 stderr（含百分比）
               "--stats", "5s",        # 每 5 秒输出统计
               "--transfers", "1", "--checkers", "1",  # A7Z 单线程防资源竞争
               "--buffer-size", "16M",   # 16MB 缓冲区，减少磁盘 IOPS
               "--retries", "3",
               "--low-level-retries", "3",
               "--timeout", "30m",
               "--contimeout", "30s",
               "--no-traverse",        # 不遍历远程目录（慢S3端点），用文件级HEAD检查
               "--ignore-checksum",    # S3 兼容端点 ETag≠MD5，跳过上传后校验
              ]
        if bwlimit > 0:
            cmd.append(f"--bwlimit={bwlimit}K")

        # 使用 Popen，捕获 stderr 用于实时进度解析
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # 行缓冲，确保实时读取
            env=get_rclone_env(),
        )

        # 后台线程读取 stderr 实时进度
        import re
        import threading
        all_lines = []
        _stop_reader = [False]

        def _read_stderr():
            try:
                for line in proc.stderr:
                    if _stop_reader[0]:
                        break
                    all_lines.append(line)
                    # 检查取消请求
                    if is_sync_cancelled() and proc.poll() is None:
                        try:
                            proc.kill()
                            if progress_callback:
                                progress_callback(-1)
                        except Exception:
                            pass
                        break
                    # 解析百分比: rclone --progress 输出含 "42%, ..."
                    m = re.search(r'(\d+)%', line)
                    if m:
                        pct = int(m.group(1))
                        if progress_callback:
                            progress_callback(pct)
            except Exception:
                pass

        reader_thread = threading.Thread(target=_read_stderr, daemon=True)
        reader_thread.start()

        # 等待 rclone 完成（带超时 + 取消检查）
        try:
            while proc.poll() is None:
                try:
                    proc.wait(timeout=2)  # 每 2 秒检查取消
                except subprocess.TimeoutExpired:
                    if is_sync_cancelled():
                        proc.kill()
                        if progress_callback:
                            progress_callback(-1)
                        break
                    continue
                break
        except Exception:
            proc.kill()

        _stop_reader[0] = True
        reader_thread.join(timeout=5)

        # 检查是否被取消
        if is_sync_cancelled():
            return False, "同步已被用户取消", {}

        # 解析 stderr 获取最终统计
        all_stderr = ''.join(all_lines)
        duration = (datetime.now() - start_time).total_seconds()
        files_synced = 0
        bytes_transferred = 0

        for line in all_stderr.split('\n'):
            if 'Transferred:' in line:
                m = re.search(r'Transferred:\s+(\d+)', line)
                if m:
                    files_synced = max(files_synced, int(m.group(1)))
                m = re.search(r'Transferred:\s+([\d.]+)\s*(\w?i?B)', line)
                if m:
                    bytes_transferred = _parse_size(m.group(1) + m.group(2).replace('iB', ''))

        if proc.returncode in (0,):
            if progress_callback:
                progress_callback(100)
            stats = {
                'duration_sec': round(duration, 1),
                'files_synced': files_synced,
                'bytes_transferred': bytes_transferred,
                'bytes_fmt': _fmt_size(bytes_transferred),
            }
            return True, f"同步完成 ({duration:.0f}s, {files_synced} 文件)", stats
        else:
            err = all_stderr.strip()[-300:] if all_stderr else "未知错误"
            if progress_callback:
                progress_callback(-1)
            return False, f"同步失败: {err}", {}

    except Exception as e:
        if progress_callback:
            progress_callback(-1)
        return False, str(e), {}


def get_remote_usage(remote_name: str = DEFAULT_REMOTE) -> Tuple[bool, dict]:
    """
    获取云存储空间使用情况。

    Args:
        remote_name: rclone remote 名称

    Returns:
        (success, {total_bytes, used_bytes, free_bytes, total_fmt, used_fmt, free_fmt})
    """
    try:
        result = subprocess.run(
            ["rclone", f"--config={RCLONE_CONFIG_FILE}", "--contimeout=5s", "--timeout=15s", "about", f"{remote_name}:"],
            capture_output=True, text=True, timeout=20,
            env=get_rclone_env(),
        )

        if result.returncode != 0:
            return False, {}

        usage = {}
        for line in result.stdout.split('\n'):
            line = line.strip()
            if ':' in line:
                key, val = line.split(':', 1)
                key = key.strip().lower().replace(' ', '_')
                val = val.strip()

                if key == 'total':
                    usage['total_bytes'] = _parse_size(val)
                elif key == 'used':
                    usage['used_bytes'] = _parse_size(val)
                elif key == 'free':
                    usage['free_bytes'] = _parse_size(val)

        if usage:
            usage['total_fmt'] = _fmt_size(usage.get('total_bytes', 0))
            usage['used_fmt'] = _fmt_size(usage.get('used_bytes', 0))
            usage['free_fmt'] = _fmt_size(usage.get('free_bytes', 0))

            total = usage.get('total_bytes', 1)
            used = usage.get('used_bytes', 0)
            usage['percent'] = round(used / total * 100, 1) if total > 0 else 0

        return True, usage

    except Exception as e:
        logger.error(f"rclone about 失败: {e}")
        return False, {}


# ── 工具函数 ────────────────────────────────────────────


def _fmt_size(b: int) -> str:
    """格式化字节数"""
    if b == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(b)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if i > 0 else f"{int(f)} {units[i]}"


def _parse_size(s: str) -> int:
    """解析 rclone 输出的文件大小字符串为字节数"""
    s = s.strip().upper()
    try:
        if s.endswith('T'):
            return int(float(s[:-1]) * 1024 ** 4)
        elif s.endswith('G'):
            return int(float(s[:-1]) * 1024 ** 3)
        elif s.endswith('M'):
            return int(float(s[:-1]) * 1024 ** 2)
        elif s.endswith('K'):
            return int(float(s[:-1]) * 1024)
        else:
            return int(float(s))
    except (ValueError, IndexError):
        return 0
