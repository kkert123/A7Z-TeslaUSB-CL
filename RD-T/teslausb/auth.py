"""
鉴权模块 - 预留，当前默认关闭
AUTH_ENABLED=True 时启用 HTTP Basic Auth
"""

import functools
from flask import request, Response
from config import AUTH_ENABLED, AUTH_USERNAME, AUTH_PASSWORD


def check_auth(username: str, password: str) -> bool:
    return username == AUTH_USERNAME and password == AUTH_PASSWORD


def authenticate():
    return Response(
        "需要认证", 401,
        {"WWW-Authenticate": 'Basic realm="TeslaUSB Neo Web"'}
    )


def require_auth(f):
    """路由装饰器：当 AUTH_ENABLED=True 时要求 Basic Auth"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not AUTH_ENABLED:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated
