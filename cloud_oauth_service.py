#!/usr/bin/env python3
"""
TeslaUSB A7Z — Cloud OAuth 认证服务
====================================

Plan A 最小化实现：仅支持 Google Drive OAuth 2.0。
Token 以 JSON 格式存储（chmod 0600），非 Fernet 加密 —
嵌入式设备上密钥存储问题暂无更好方案，后续可扩展。

OAuth 流程：
  1. 用户在 Web UI 点击"授权"
  2. 生成 Google OAuth 授权 URL
  3. 用户在浏览器中授权，获取授权码
  4. 在 Web UI 中输入授权码
  5. 服务端换取 access_token + refresh_token
  6. Token 加密存储，自动刷新

架构：
  Google OAuth 2.0 → Fernet 加密存储 → rclone 使用

作者：TeslaUSB A7Z 项目
版本：1.0.0
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────

# Token 存储路径
TOKEN_FILE = "/opt/radxa_data/teslausb/data/cloud_tokens.json"

# Google OAuth 端点
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# 默认 Google OAuth 范围（Google Drive 读写）
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Token 提前刷新阈值（在过期前 N 秒刷新）
REFRESH_THRESHOLD = 300  # 5 分钟


# ── Token 管理 ──────────────────────────────────────────


def _load_tokens() -> dict:
    """加载加密存储的 OAuth token（明文 JSON）"""
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'r') as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"加载 token 文件失败: {e}")
    return {}


def _save_tokens(tokens: dict):
    """保存 OAuth token 到文件（JSON 明文，权限 0600）"""
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'w') as f:
            json.dump(tokens, f, indent=2)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError as e:
        logger.error(f"保存 token 文件失败: {e}")


def get_stored_token(provider: str = "google") -> Optional[dict]:
    """
    获取已存储的 OAuth token。

    Args:
        provider: 云服务提供商名称

    Returns:
        Token 字典，如果不存在或已过期返回 None
    """
    tokens = _load_tokens()
    token_data = tokens.get(provider, {})

    if not token_data:
        return None

    # 检查是否需要刷新
    expires_at = token_data.get('expires_at', 0)
    if expires_at > 0:
        if time.time() > expires_at - REFRESH_THRESHOLD:
            # Token 即将过期，尝试刷新
            if token_data.get('refresh_token'):
                refreshed = refresh_token(provider)
                if refreshed:
                    return refreshed

    return token_data if token_data.get('access_token') else None


def store_token(provider: str, token_data: dict):
    """
    存储 OAuth token（含过期时间计算）。

    Args:
        provider: 云服务提供商名称
        token_data: OAuth 响应字典，含 access_token, refresh_token, expires_in
    """
    tokens = _load_tokens()

    expires_in = token_data.get('expires_in', 3600)
    token_data['expires_at'] = int(time.time()) + expires_in
    token_data['stored_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    tokens[provider] = token_data
    _save_tokens(tokens)
    logger.info(f"OAuth token 已存储 ({provider}), 过期时间: {expires_in}s")


def delete_token(provider: str = "google"):
    """删除指定提供商的 OAuth token（撤销授权）"""
    tokens = _load_tokens()
    if provider in tokens:
        # 尝试撤销 refresh_token
        refresh_token = tokens[provider].get('refresh_token', '')
        if refresh_token:
            _revoke_google_token(refresh_token)

        del tokens[provider]
        _save_tokens(tokens)
        logger.info(f"OAuth token 已删除 ({provider})")


# ── OAuth 认证流程 ──────────────────────────────────────


def get_auth_url(client_id: str, redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
                 scopes: Optional[list] = None) -> str:
    """
    生成 Google OAuth 授权 URL。

    使用 OOB (out-of-band) 模式，用户在浏览器授权后复制授权码到 Web UI。

    Args:
        client_id: Google Cloud Console 中的 OAuth 2.0 Client ID
        redirect_uri: 重定向 URI（默认使用 OOB 模式获取授权码）
        scopes: OAuth 权限范围列表

    Returns:
        完整的 Google OAuth 授权 URL
    """
    if scopes is None:
        scopes = DEFAULT_SCOPES

    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ' '.join(scopes),
        'access_type': 'offline',   # 获取 refresh_token
        'prompt': 'consent',        # 每次都显示同意画面（确保获取 refresh_token）
    }

    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, auth_code: str,
                  redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob") -> Tuple[bool, str, Optional[dict]]:
    """
    用授权码换取 access_token 和 refresh_token。

    Args:
        client_id: OAuth Client ID
        client_secret: OAuth Client Secret
        auth_code: 用户粘贴的授权码
        redirect_uri: 重定向 URI

    Returns:
        (success, message, token_data)
    """
    import urllib.request

    post_data = urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'code': auth_code,
        'grant_type': 'authorization_code',
        'redirect_uri': redirect_uri,
    }).encode('utf-8')

    try:
        req = urllib.request.Request(GOOGLE_TOKEN_URL, data=post_data)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        with urllib.request.urlopen(req, timeout=30) as resp:
            token_data = json.loads(resp.read().decode('utf-8'))

        if 'access_token' not in token_data:
            error = token_data.get('error_description', token_data.get('error', '未知错误'))
            return False, f"换取 token 失败: {error}", None

        # 存储 token
        store_token('google', token_data)

        return True, "授权成功", token_data

    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode('utf-8'))
            err_msg = err_body.get('error_description', err_body.get('error', str(e)))
        except Exception:
            err_msg = str(e)
        return False, f"HTTP 错误: {err_msg}", None
    except Exception as e:
        return False, f"网络错误: {e}", None


# ── Token 刷新 ──────────────────────────────────────────


def refresh_token(provider: str = "google") -> Optional[dict]:
    """
    使用 refresh_token 刷新过期的 access_token。

    Google 的 refresh_token 长期有效（除非用户撤销授权）。

    Args:
        provider: 云服务提供商名称

    Returns:
        更新后的 token 字典，失败返回 None
    """
    import urllib.request

    tokens = _load_tokens()
    token_data = tokens.get(provider, {})

    refresh_tok = token_data.get('refresh_token', '')
    if not refresh_tok:
        logger.warning(f"没有 refresh_token，无法刷新 ({provider})")
        return None

    # 从配置中获取 client credentials
    client_id, client_secret = _get_cloud_credentials()

    post_data = urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_tok,
        'grant_type': 'refresh_token',
    }).encode('utf-8')

    try:
        req = urllib.request.Request(GOOGLE_TOKEN_URL, data=post_data)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        with urllib.request.urlopen(req, timeout=30) as resp:
            new_token = json.loads(resp.read().decode('utf-8'))

        if 'access_token' not in new_token:
            logger.error(f"刷新 token 失败: {new_token}")
            return None

        # 合并：保留旧的 refresh_token，更新 access_token
        token_data['access_token'] = new_token['access_token']
        token_data['expires_in'] = new_token.get('expires_in', 3600)
        token_data['expires_at'] = int(time.time()) + new_token.get('expires_in', 3600)
        token_data['stored_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 如果 Google 返回了新的 refresh_token，也更新
        if 'refresh_token' in new_token:
            token_data['refresh_token'] = new_token['refresh_token']

        _save_tokens(tokens)
        logger.info(f"Token 已刷新 ({provider})")
        return token_data

    except Exception as e:
        logger.error(f"刷新 token 时网络错误 ({provider}): {e}")
        return None


def _revoke_google_token(token: str):
    """撤销 Google token（取消授权时调用）"""
    import urllib.request

    try:
        revoke_url = "https://oauth2.googleapis.com/revoke"
        post_data = urlencode({'token': token}).encode('utf-8')
        req = urllib.request.Request(revoke_url, data=post_data)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        urllib.request.urlopen(req, timeout=10)
        logger.info("Google token 已撤销")
    except Exception as e:
        logger.warning(f"撤销 token 时出错（可忽略）: {e}")


# ── 配置读取 ────────────────────────────────────────────


def _get_cloud_credentials() -> Tuple[str, str]:
    """
    从 config_manager 读取 Google Cloud OAuth 凭据。

    Returns:
        (client_id, client_secret)
    """
    client_id = ""
    client_secret = ""

    try:
        # 尝试从 config.json 读取
        config_path = "/opt/radxa_data/teslausb/config/cloud.json"
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                cfg = json.load(f)
            client_id = cfg.get('google_client_id', '')
            client_secret = cfg.get('google_client_secret', '')
    except Exception:
        pass

    # 回退到 config_manager
    if not client_id:
        try:
            from config_manager import get_config_manager
            mgr = get_config_manager()
            cfg = mgr.get_config()
            # 使用 NasConfig 中的字段（复用 NAS 的 host/share 作为可选字段，
            # 实际 cloud credentials 存储在单独配置中）
        except ImportError:
            pass

    return client_id, client_secret


def get_oauth_status(provider: str = "google") -> dict:
    """
    获取 OAuth 授权状态（供 Web UI 使用）。

    Args:
        provider: 云服务提供商名称

    Returns:
        {
            "authorized": bool,
            "provider": str,
            "expires_at": str (ISO 格式),
            "expires_in_sec": int (剩余秒数),
            "scopes": list,
        }
    """
    token = get_stored_token(provider)

    if not token:
        return {
            "authorized": False,
            "provider": provider,
            "expires_at": None,
            "expires_in_sec": 0,
            "scopes": [],
        }

    expires_at = token.get('expires_at', 0)
    expires_in = max(0, int(expires_at - time.time())) if expires_at else 0

    return {
        "authorized": True,
        "provider": provider,
        "expires_at": datetime.fromtimestamp(expires_at).isoformat() if expires_at else None,
        "expires_in_sec": expires_in,
        "scopes": token.get('scope', '').split(' ') if token.get('scope') else [],
    }
