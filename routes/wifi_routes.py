# routes/wifi_routes.py
from flask import Blueprint, render_template, request, jsonify, current_app
from app_state import state
import wifi_service

wifi_bp = Blueprint('wifi', __name__, url_prefix='')

# ─────────────────────────────────────────────
# 页面路由
# ─────────────────────────────────────────────

@wifi_bp.route('/wifi')
def wifi_page():
    from routes.misc_routes import get_template_context
    ctx = get_template_context()
    ctx['current'] = wifi_service.get_current_wifi()
    ctx['connections'] = wifi_service.get_wifi_connections()
    ctx['wifi_status'] = wifi_service.get_wifi_status()
    return render_template('wifi.html', **ctx)

# ─────────────────────────────────────────────
# WiFi 管理 API 路由
# ─────────────────────────────────────────────

@wifi_bp.route('/api/wifi/scan')
def wifi_scan():
    """扫描周边可用 WiFi"""
    try:
        networks = wifi_service.get_available_networks(rescan=True)
        return jsonify({"success": True, "networks": networks})
    except Exception as e:
        return jsonify({"success": False, "networks": [], "error": str(e)})


@wifi_bp.route('/api/wifi/switch', methods=['POST'])
def wifi_switch():
    """切换到指定 WiFi（含自动回档 + 5GHz优先）"""
    data = request.get_json(silent=True) or request.form
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()
    prefer_5ghz = data.get("prefer_5ghz", True)
    if isinstance(prefer_5ghz, str):
        prefer_5ghz = prefer_5ghz.lower() in ("true", "1", "yes", "on")
    if not ssid:
        return jsonify({"success": False, "message": "SSID 不能为空"})
    try:
        result = wifi_service.switch_wifi(ssid, password, prefer_5ghz=prefer_5ghz)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@wifi_bp.route('/api/wifi/priority', methods=['POST'])
def wifi_priority():
    """修改连接优先级"""
    data = request.get_json(silent=True) or request.form
    con_name = (data.get("con_name") or "").strip()
    try:
        priority = int(data.get("priority", 5))
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "优先级必须为整数"})
    if not con_name:
        return jsonify({"success": False, "message": "连接名不能为空"})
    result = wifi_service.update_wifi_priority(con_name, priority)
    return jsonify(result)


@wifi_bp.route('/api/wifi/autoconnect', methods=['POST'])
def wifi_autoconnect():
    """切换连接的自动连接开关"""
    data = request.get_json(silent=True) or request.form
    con_name = (data.get("con_name") or "").strip()
    autoconnect = data.get("autoconnect")
    if not con_name:
        return jsonify({"success": False, "message": "连接名不能为空"})
    if isinstance(autoconnect, str):
        autoconnect = autoconnect.lower() in ("true", "1", "yes", "on")
    elif not isinstance(autoconnect, bool):
        return jsonify({"success": False, "message": "autoconnect 参数必须为布尔值"})
    result = wifi_service.update_connection_autoconnect(con_name, autoconnect)
    return jsonify(result)


@wifi_bp.route('/api/wifi/rename', methods=['POST'])
def wifi_rename():
    """修改连接名称"""
    data = request.get_json(silent=True) or request.form
    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    if not old_name:
        return jsonify({"success": False, "message": "原连接名不能为空"})
    if not new_name:
        return jsonify({"success": False, "message": "新连接名不能为空"})
    result = wifi_service.update_connection_name(old_name, new_name)
    return jsonify(result)


@wifi_bp.route('/api/wifi/delete', methods=['POST'])
def wifi_delete():
    """删除 WiFi 连接配置"""
    data = request.get_json(silent=True) or request.form
    con_name = (data.get("con_name") or "").strip()
    if not con_name:
        return jsonify({"success": False, "message": "连接名不能为空"})
    result = wifi_service.delete_wifi_connection(con_name)
    return jsonify(result)


@wifi_bp.route('/api/wifi/status/dismiss', methods=['POST'])
def wifi_status_dismiss():
    """清除 WiFi 切换状态提示"""
    wifi_service.clear_wifi_status()
    return jsonify({"success": True})


@wifi_bp.route('/api/wifi/details')
def wifi_connection_details():
    """API: 获取 WiFi 连接详情"""
    details = wifi_service.get_connection_details()
    return jsonify({"success": True, "data": details})


@wifi_bp.route('/api/wifi/speedtest', methods=['POST'])
def wifi_speedtest():
    """API: 执行网络下载速度测试，支持预设服务器和自定义URL"""
    try:
        data = request.get_json(silent=True) or {}
        server = data.get("server")       # 预设 key 或 "__custom__"
        custom_url = data.get("custom_url")  # 自定义服务器地址
        result = wifi_service.run_speed_test(server=server, custom_url=custom_url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@wifi_bp.route('/api/wifi/speedtest/upload', methods=['POST'])
def wifi_speedtest_upload():
    """API: 执行网络上传速度测试"""
    try:
        result = wifi_service.run_upload_speed_test()
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── AP 管理 API ──

@wifi_bp.route('/api/ap/status')
def ap_status_api():
    """API: 获取 AP 状态"""
    status = wifi_service.get_ap_status()
    return jsonify({"success": True, **status})


@wifi_bp.route('/api/ap/config', methods=['GET', 'POST'])
def ap_config_api():
    """API: 获取/设置 AP 配置"""
    if request.method == 'GET':
        config = wifi_service.get_ap_config()
        return jsonify({"success": True, "ssid": config.get("ssid"), "enabled": config.get("enabled", True)})
    
    data = request.get_json() or {}
    ssid = data.get("ssid", "").strip()
    passphrase = data.get("passphrase", "")
    result = wifi_service.set_ap_config(ssid, passphrase)
    return jsonify(result)


@wifi_bp.route('/api/ap/mode', methods=['GET', 'POST'])
def ap_mode_api():
    """API: 获取/设置 AP 强制模式"""
    if request.method == 'GET':
        mode = wifi_service.get_ap_force_mode()
        return jsonify({"success": True, "mode": mode})
    
    data = request.get_json() or {}
    mode = data.get("mode", "auto")
    result = wifi_service.set_ap_force_mode(mode)
    return jsonify(result)


@wifi_bp.route('/api/ap/control', methods=['POST'])
def ap_control_api():
    """API: 手动控制 AP 启停"""
    data = request.get_json() or {}
    action = data.get("action", "")
    
    if action == "start":
        result = wifi_service.start_ap()
    elif action == "stop":
        result = wifi_service.stop_ap()
    else:
        return jsonify({"success": False, "message": "无效的操作"}), 400
    
    return jsonify(result)


# ── 位置检测配置 API ──

import os
import json as _json

SENTRY_CONFIG_PATH = "/opt/radxa_data/teslausb/config/sentry.json"
LOCATION_DEFAULTS = {
    "teslamate_url": "http://100.64.0.11:7777",
    "teslamate_password": "",
    "home_location": "家",
    "home_wifi_ssids": [],
    "hotspot_ssids": [],
}

def _read_sentry_config():
    """读取 sentry.json 配置文件"""
    try:
        if os.path.exists(SENTRY_CONFIG_PATH):
            with open(SENTRY_CONFIG_PATH, "r", encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return {}

def _write_sentry_config(cfg):
    """写入 sentry.json 配置文件（保留原有字段）"""
    os.makedirs(os.path.dirname(SENTRY_CONFIG_PATH), exist_ok=True)
    with open(SENTRY_CONFIG_PATH, "w", encoding="utf-8") as f:
        _json.dump(cfg, f, indent=2, ensure_ascii=False)

@wifi_bp.route('/api/wifi/location-config', methods=['GET', 'POST'])
def location_config_api():
    """API: 获取/设置位置检测配置"""
    if request.method == 'GET':
        cfg = _read_sentry_config()
        result = {}
        for key, default in LOCATION_DEFAULTS.items():
            val = cfg.get(key, default)
            if isinstance(default, list) and isinstance(val, str):
                val = [s.strip() for s in val.split(",") if s.strip()]
            result[key] = val
        return jsonify({"success": True, "config": result})

    # POST: 保存配置
    data = request.get_json(silent=True) or request.form
    cfg = _read_sentry_config()

    for key, default in LOCATION_DEFAULTS.items():
        if key in data:
            val = data[key]
            if isinstance(default, list) and isinstance(val, str):
                val = [s.strip() for s in val.split(",") if s.strip()]
            elif isinstance(default, list) and isinstance(val, list):
                val = val
            cfg[key] = val

    try:
        _write_sentry_config(cfg)
        return jsonify({"success": True, "message": "位置检测配置已保存"})
    except Exception as e:
        return jsonify({"success": False, "message": f"保存失败: {str(e)}"}), 500
