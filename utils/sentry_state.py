"""哨兵事件状态文件读写 —— 供 /sentry 页面流程使用（纯逻辑层，可单测）。

状态文件：config.SENTRY_STATE_FILE（默认 /opt/radxa_data/data/sentry_events.json）
结构：{"updated_at": "...", "events": [ {id, status, confirmation_code, ...}, ... ]}
事件 status 取值：detected / pending_confirm / confirmed / uploading / completed / expired
"""
import json
import os
import tempfile
from typing import Optional, Dict, Any

try:
    from config import SENTRY_STATE_FILE  # type: ignore
except Exception:  # 测试/离线环境兜底
    SENTRY_STATE_FILE = "/opt/radxa_data/data/sentry_events.json"

# 预览图实际落盘目录（与 sentry_watchdog.py 的 PREVIEW_DIR 一致）
PREVIEW_DIR = "/opt/teslausb-web/data/previews"


def load_state() -> Dict[str, Any]:
    """读取哨兵状态文件，返回 {'updated_at':..., 'events':[...]}；缺失/损坏返回空结构。"""
    try:
        with open(SENTRY_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"updated_at": None, "events": []}
        data.setdefault("events", [])
        return data
    except FileNotFoundError:
        return {"updated_at": None, "events": []}
    except (json.JSONDecodeError, OSError):
        return {"updated_at": None, "events": []}


def find_event_by_code(code: str) -> Optional[Dict[str, Any]]:
    """按 confirmation_code 反查事件；code 为空或不存在返回 None。"""
    if not code:
        return None
    code = code.strip()
    state = load_state()
    for ev in state.get("events", []):
        if ev.get("confirmation_code") == code:
            return ev
    return None


def find_event_by_id(event_id: str) -> Optional[Dict[str, Any]]:
    """按事件 id 反查事件。"""
    if not event_id:
        return None
    state = load_state()
    for ev in state.get("events", []):
        if ev.get("id") == event_id:
            return ev
    return None


def save_state(state: Dict[str, Any]) -> bool:
    """原子写回状态文件（tmp + os.replace）。成功返回 True。"""
    try:
        d = os.path.dirname(SENTRY_STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d or None, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SENTRY_STATE_FILE)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return True
    except OSError:
        return False


def set_event_status(event_id: str, status: str,
                      extra: Optional[Dict[str, Any]] = None) -> bool:
    """将指定事件状态置为 status（可附带额外字段），原子写回。成功返回 True。"""
    if not event_id:
        return False
    state = load_state()
    for ev in state.get("events", []):
        if ev.get("id") == event_id:
            ev["status"] = status
            if extra:
                ev.update(extra)
            return save_state(state)
    return False


def build_preview_url(preview_path: Optional[str]) -> Optional[str]:
    """将预览图绝对路径转为 HTTP 服务 URL；文件不存在返回 None。"""
    if not preview_path:
        return None
    if not os.path.exists(preview_path):
        return None
    return "/api/sentry/preview/" + os.path.basename(preview_path)
