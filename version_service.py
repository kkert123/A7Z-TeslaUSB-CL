"""version_service.py — GitHub 版本检测

用法:
    from version_service import check_latest_release, get_current_version
    info = check_latest_release()
    # info = {'current': '0.1.0', 'latest': '0.2.0', 'has_update': True, ...}
"""

import json
import time
import urllib.request
import config

GITHUB_REPO = "kkert123/A7Z-TeslaUSB-CL"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_INTERVAL = 3600


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


def check_latest_release(force=False):
    """查询 GitHub 最新 release。1h 缓存，force=True 绕过缓存。

    返回 dict:
        current:    当前运行版本号
        latest:     远端最新版本号 (tag_name 去 v 前缀)
        has_update:  是否有更新可用
        changelog:   更新说明正文
        html_url:    release 页面地址
        asset_url:   下载链接 (首个 asset)
        error:       错误信息 (仅失败时有值)
    """
    from app_state import state

    now = time.time()
    if not force:
        with state.version_cache_lock:
            if state._version_last_check and (now - state._version_last_check) < CHECK_INTERVAL:
                return dict(state._version_cache)

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
        req = urllib.request.Request(GITHUB_API)
        if auth_token:
            req.add_header('Authorization', f'token {auth_token}')
        req.add_header('Accept', 'application/vnd.github+json')
        req.add_header('User-Agent', 'A7Z-TeslaUSB-VersionCheck/1.0')
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))

    data = None
    last_error = None

    # 优先匿名（公开仓库），失败再用 token 重试（私有仓库）
    for use_token in (False, True):
        try:
            data = _do_request(token if use_token else None)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404 and not use_token and token:
                continue  # 公开接口 404 → 用 token 重试
            last_error = f'GitHub API 错误: HTTP {e.code}'
        except urllib.error.URLError as e:
            last_error = f'网络错误: {e.reason}'
        except Exception as e:
            last_error = f'检查失败: {e}'

    if data is None:
        result['error'] = last_error or '未知错误'
        _update_cache(state, result)
        return result

    tag = data.get('tag_name', '')
    result['latest'] = tag.lstrip('v') if tag else None
    result['changelog'] = data.get('body', '')
    result['html_url'] = data.get('html_url', '')
    assets = data.get('assets', [])
    if assets:
        result['asset_url'] = assets[0].get('browser_download_url', '')

    if result['latest'] and result['current']:
        result['has_update'] = _compare_versions(
            result['latest'], result['current']
        ) > 0

    _update_cache(state, result)
    return result


def _update_cache(s, result):
    s._version_cache = result
    s._version_last_check = time.time()
