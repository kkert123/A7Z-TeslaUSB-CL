"""version_service.py — GitHub 版本检测

用法:
    from version_service import check_latest_release, get_current_version
    info = check_latest_release()
    # info = {'current': '0.1.0', 'latest': '0.2.0', 'has_update': True, ...}
"""

import json
import threading
import time
import urllib.request
import config

GITHUB_REPO = "kkert123/A7Z-TeslaUSB-CL"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# M63：匿名配额 60 次/小时按公网 IP 计，且与电脑等设备共享。降频 + 留余量：
CHECK_INTERVAL = 21600          # 成功缓存 6h（4 次/天，占配额 0.3%）
FAILURE_CACHE_TTL = 1800        # 失败也缓存 30min——防 30s 轮询轰炸（本次事故放大器）
MIN_REQUEST_INTERVAL = 600      # 距上次实际请求 <10min 一律用缓存
RATE_GUARD_REMAINING = 10       # 匿名余量低于此值 → 静默到配额重置时刻

_lock = threading.Lock()
_last_request_ts = 0.0
_failure_cache = None
_failure_ts = 0.0
_quota_guard_until = 0.0


def _read_github_token():
    """从 config/sentry.json 读取 GitHub 版本检测令牌"""
    try:
        cfg_path = config.SENTRY_CONFIG_FILE
        if not cfg_path:
            cfg_path = '/opt/radxa_data/teslausb/config/sentry.json'
        with open(cfg_path, 'r') as f:
            return json.load(f).get('github_version_token', '')
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return ''


def get_current_version():
    return config.APP_VERSION


def _compare_versions(v1, v2):
    """比较语义版本字符串。>0 v1更新, <0 v2更新, 0 相同"""
    try:
        v1 = v1.split('-')[0]
        v2 = v2.split('-')[0]
        p1 = [int(x) for x in v1.split('.')]
        p2 = [int(x) for x in v2.split('.')]
        for i in range(max(len(p1), len(p2))):
            a = p1[i] if i < len(p1) else 0
            b = p2[i] if i < len(p2) else 0
            if a > b:
                return 1
            if a < b:
                return -1
        return 0
    except (ValueError, AttributeError):
        return 0


def result_placeholder():
    return {
        'current': get_current_version(),
        'latest': None,
        'has_update': False,
        'changelog': '',
        'html_url': '',
        'asset_url': None,
        'error': None,
    }


def check_latest_release(force=False):
    """查询 GitHub 最新 release。

    缓存与限流策略（M63，匿名设计降频留余量）：
    - 成功缓存 6h；失败也缓存 30min（防轮询轰炸）
    - 距上次实际请求 <10min 一律返回缓存
    - 匿名余量 <10 时静默到配额重置时刻（force 也不例外——硬限）
    - force 仅绕过时间类缓存

    返回 dict:
        current:    当前运行版本号
        latest:     远端最新版本号 (tag_name 去 v 前缀)
        has_update: 是否有更新可用
        changelog:  更新说明正文
        html_url:   release 页面地址
        asset_url:  下载链接 (首个 asset)
        error:      错误信息 (仅失败时有值)
    """
    global _last_request_ts, _failure_cache, _failure_ts

    from app_state import state

    now = time.time()
    with _lock:
        # 配额护栏：硬限，force 也不例外（匿名余量不足时绝不发请求）
        if now < _quota_guard_until:
            cached = _failure_cache or (dict(state._version_cache) if state._version_cache else None)
            if cached is None:
                cached = result_placeholder()
                cached['error'] = 'GitHub 匿名配额保护中，稍后自动恢复'
            return dict(cached)

        if not force:
            with state.version_cache_lock:
                if state._version_last_check and (now - state._version_last_check) < CHECK_INTERVAL:
                    return dict(state._version_cache)
            if _failure_cache and (now - _failure_ts) < FAILURE_CACHE_TTL:
                return dict(_failure_cache)
            if now - _last_request_ts < MIN_REQUEST_INTERVAL:
                cached = _failure_cache or (dict(state._version_cache) if state._version_cache else None)
                if cached is not None:
                    return dict(cached)

        result = {
            'current': get_current_version(),
            'latest': None,
            'has_update': False,
            'changelog': '',
            'html_url': '',
            'asset_url': None,
            'error': None,
        }

        token = _read_github_token()

        def _do_request(auth_token=None):
            global _quota_guard_until
            req = urllib.request.Request(GITHUB_API)
            if auth_token:
                req.add_header('Authorization', f'token {auth_token}')
            req.add_header('Accept', 'application/vnd.github+json')
            req.add_header('User-Agent', 'A7Z-TeslaUSB-VersionCheck/1.0')
            with urllib.request.urlopen(req, timeout=15) as resp:
                remaining = resp.headers.get('X-RateLimit-Remaining')
                reset = resp.headers.get('X-RateLimit-Reset')
                try:
                    if remaining is not None and int(remaining) < RATE_GUARD_REMAINING and reset:
                        _quota_guard_until = max(_quota_guard_until, int(reset))
                except (TypeError, ValueError):
                    pass
                return json.loads(resp.read().decode('utf-8'))

        data = None
        last_error = None
        error_detail = ''

        # 优先匿名（公开仓库），失败再用 token 重试（私有仓库）
        for use_token in (False, True):
            try:
                data = _do_request(token if use_token else None)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404 and not use_token and token:
                    continue  # 公开接口 404 → 用 token 重试
                if e.code == 404:
                    error_detail = '未找到 Release（仓库尚无发布版本或为私有）'
                elif e.code == 401:
                    error_detail = '检测令牌已失效，请在系统页更新版本检测令牌'
                elif e.code == 403:
                    error_detail = 'API 限流（60次/小时），请稍后再试'
                else:
                    error_detail = f'HTTP {e.code}'
                last_error = error_detail
            except urllib.error.URLError as e:
                error_detail = f'网络不可达 ({e.reason})'
                last_error = error_detail
            except Exception as e:
                error_detail = str(e)
                last_error = error_detail

        _last_request_ts = time.time()

        if data is None:
            result['error'] = last_error or '未知错误'
            # 失败也缓存（TTL 30min），防轮询轰炸（M63）
            _failure_cache = dict(result)
            _failure_ts = time.time()
            state._version_cache = {}
            state._version_last_check = 0
            return result

        tag = data.get('tag_name', '')
        result['latest'] = tag.lstrip('v') if tag else None
        result['changelog'] = data.get('body', '')
        result['html_url'] = data.get('html_url', '')

        assets = data.get('assets', [])
        result['asset_url'] = ''
        result['sig_url'] = ''
        result['sha256'] = ''

        for a in assets:
            url = a.get('browser_download_url', '')
            name = a.get('name', '')
            if name.endswith('.sig'):
                result['sig_url'] = url
            elif not result['asset_url']:
                result['asset_url'] = url

        # 从 release body 提取 SHA-256
        import re
        body = data.get('body', '')
        m = re.search(r'(?:SHA-?256|sha256)[:\s]+([a-fA-F0-9]{64})', body)
        if m:
            result['sha256'] = m.group(1)

        if result['latest'] and result['current']:
            result['has_update'] = _compare_versions(
                result['latest'], result['current']
            ) > 0

        _failure_cache = None
        _failure_ts = 0.0
        _update_cache(state, result)
        return result


def _update_cache(s, result):
    s._version_cache = result
    s._version_last_check = time.time()
