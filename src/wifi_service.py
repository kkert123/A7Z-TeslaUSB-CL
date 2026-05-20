"""
TeslaUSB A7Z - WiFi 服务模块
基于 NetworkManager (nmcli) 实现：
  - 当前连接查询 / WiFi 扫描 / 连接切换（含自动回档）
  - 连接列表 / 优先级管理 / 删除 / 重命名
  - AP 热点管理
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime
from typing import List, Optional

# ── 常量 ──
WIFI_INTERFACE = "wlan0"
WIFI_STATUS_FILE = "/tmp/teslausb_wifi_status.json"
AP_CONFIG_FILE = "/opt/radxa_data/teslausb/config/ap_config.json"
FORCE_MODE_FILE = "/tmp/teslausb_ap_force_mode"
AP_CONTROL_SCRIPT = "/opt/radxa_data/teslausb/ap_control.sh"


# ─────────────────────────────────────────────
# WiFi 状态文件（切换结果持久化）
# ─────────────────────────────────────────────

def _save_wifi_status(status: dict):
    """保存 WiFi 切换结果到临时文件，供页面展示"""
    try:
        status["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(WIFI_STATUS_FILE, "w") as f:
            json.dump(status, f, ensure_ascii=False)
    except Exception:
        pass


def get_wifi_status() -> Optional[dict]:
    """读取上次 WiFi 切换的结果"""
    try:
        if os.path.exists(WIFI_STATUS_FILE):
            with open(WIFI_STATUS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def clear_wifi_status():
    """清除 WiFi 切换状态"""
    try:
        if os.path.exists(WIFI_STATUS_FILE):
            os.remove(WIFI_STATUS_FILE)
    except Exception:
        pass


# ─────────────────────────────────────────────
# 当前连接信息
# ─────────────────────────────────────────────

def get_current_wifi() -> dict:
    """获取当前已连接的 WiFi 信息（基于 nmcli）"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[0] == "yes":
                    return {
                        "connected": True,
                        "ssid": parts[1] if len(parts) > 1 else "Unknown",
                        "signal": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None,
                    }
    except Exception:
        pass

    # Fallback: iw dev wlan0 link
    try:
        result = subprocess.run(
            ["iw", "dev", WIFI_INTERFACE, "link"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            if "Connected to" in result.stdout or "SSID:" in result.stdout:
                m = re.search(r"SSID:\s*(.+)", result.stdout)
                ssid = m.group(1).strip() if m else "Unknown"
                return {"connected": True, "ssid": ssid, "signal": None}
    except Exception:
        pass

    return {"connected": False, "ssid": None, "signal": None}


def _get_active_connection_name() -> Optional[str]:
    """获取当前激活的 WiFi 连接名称"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and "wireless" in parts[1].lower() and "activated" in parts[2].lower():
                    return parts[0]
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
# 扫描可用 WiFi
# ─────────────────────────────────────────────

def get_available_networks(rescan: bool = True) -> List[dict]:
    """扫描并返回周边 WiFi 列表"""
    try:
        if rescan:
            subprocess.run(
                ["sudo", "-n", "nmcli", "dev", "wifi", "rescan"],
                capture_output=True, timeout=10,
            )
            time.sleep(1)

        result = subprocess.run(
            ["sudo", "-n", "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []

        networks: List[dict] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            ssid = parts[0].strip()
            signal_str = parts[1].strip() if len(parts) > 1 else "0"
            security = parts[2].strip() if len(parts) > 2 else ""
            if ssid and ssid not in seen:
                seen.add(ssid)
                networks.append({
                    "ssid": ssid,
                    "signal": int(signal_str) if signal_str.isdigit() else 0,
                    "secured": bool(security),
                })

        networks.sort(key=lambda x: x["signal"], reverse=True)
        return networks
    except Exception:
        return []


# ─────────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────────

def get_wifi_connections() -> List[dict]:
    """获取所有已配置的 WiFi 连接，按优先级降序排列"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY", "connection", "show"],
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0:
            return []

        connections: List[dict] = []
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and "wireless" in parts[1].lower():
                name = parts[0].strip()
                autoconnect = parts[2].strip().lower() == "yes"
                try:
                    priority = int(parts[3].strip())
                except ValueError:
                    priority = 0
                ssid = _get_connection_ssid(name)
                # Check if this connection is currently active
                active_name = _get_active_connection_name()
                connections.append({
                    "name": name,
                    "ssid": ssid or name,
                    "priority": priority,
                    "autoconnect": autoconnect,
                    "active": (name == active_name),
                })

        # 按 SSID 去重：多个 profile 指向同一 SSID 时保留优先级最高的
        seen_ssids = {}
        unique = []
        for c in connections:
            ssid_key = c.get("ssid", "")
            if ssid_key in seen_ssids:
                # 保留优先级更高的
                existing = seen_ssids[ssid_key]
                if c["priority"] > existing["priority"]:
                    unique.remove(existing)
                    unique.append(c)
                    seen_ssids[ssid_key] = c
            else:
                seen_ssids[ssid_key] = c
                unique.append(c)
        connections = unique

        connections.sort(key=lambda x: x["priority"], reverse=True)
        return connections
    except Exception:
        return []


def _get_connection_ssid(con_name: str) -> str:
    """获取指定连接配置的 SSID"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", con_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "802-11-wireless.ssid:" in line:
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def _activate_connection(con_name: str, timeout: int = 30) -> bool:
    """激活指定 NetworkManager 连接，返回是否成功"""
    try:
        result = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "up", con_name],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            time.sleep(3)
            return get_current_wifi().get("connected", False)

        for _ in range(5):
            time.sleep(2)
            if get_current_wifi().get("connected", False):
                return True
        return False
    except Exception:
        return False


# ─────────────────────────────────────────────
# 切换 WiFi（核心：含自动回档）
# ─────────────────────────────────────────────

def switch_wifi(ssid: str, password: str = "") -> dict:
    """切换到指定 WiFi，失败时自动回档到上一个连接"""
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID 长度必须为 1-32 字符")
    if password and (len(password) < 8 or len(password) > 63):
        raise ValueError("密码长度必须为 8-63 字符（开放网络可留空）")

    prev_conn = get_current_wifi()
    prev_con_name = _get_active_connection_name()
    prev_ssid = prev_conn.get("ssid") if prev_conn.get("connected") else None
    con_name = f"WiFi-{ssid}"

    try:
        # 查找已有连接中是否有相同 SSID 的 profile（避免重复创建）
        check = subprocess.run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show"],
            capture_output=True, text=True, timeout=5,
        )
        existing_names = [n.strip() for n in check.stdout.splitlines()
                         if n.strip() and n.strip() not in ('lo', 'tailscale0')]
        # 查找已存在于此 SSID 的连接名（可能不带 "WiFi-" 前缀）
        for ename in existing_names:
            try:
                r_ssid = subprocess.run(
                    ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", ename],
                    capture_output=True, text=True, timeout=5,
                )
                raw_ssid = r_ssid.stdout.strip()
                if ':' in raw_ssid:
                    raw_ssid = raw_ssid.split(':', 1)[1]
                if raw_ssid == ssid:
                    con_name = ename
                    con_exists = True
                    break
            except Exception:
                continue
        else:
            con_exists = con_name in check.stdout.splitlines() if not any(
                n for n in existing_names
            ) else False

        if con_exists:
            if password:
                cmd = ["sudo", "-n", "nmcli", "connection", "modify", con_name,
                       "wifi.ssid", ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
            else:
                cmd = ["sudo", "-n", "nmcli", "connection", "modify", con_name,
                       "wifi.ssid", ssid, "wifi-sec.key-mgmt", "none"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise RuntimeError(f"修改连接失败：{r.stderr.strip()}")
        else:
            if password:
                cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
                       "password", password, "name", con_name]
            else:
                cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
                       "name", con_name]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0 and "No network with SSID" in (r.stderr or ""):
                raise ValueError(f"找不到网络 '{ssid}'")

        activate = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "up", con_name],
            capture_output=True, text=True, timeout=30,
        )

        time.sleep(3)
        curr = get_current_wifi()

        if curr.get("connected") and curr.get("ssid") == ssid:
            status = {"success": True, "message": f"已成功连接到 '{ssid}'",
                      "ssid": ssid, "prev_ssid": prev_ssid, "action": "connected"}
            _save_wifi_status(status)
            return status

        for _ in range(5):
            time.sleep(2)
            curr = get_current_wifi()
            curr_ssid = curr.get("ssid") if curr.get("connected") else None

            if curr.get("connected") and curr_ssid == ssid:
                status = {"success": True, "message": f"已成功连接到 '{ssid}'",
                          "ssid": ssid, "prev_ssid": prev_ssid, "action": "connected"}
                _save_wifi_status(status)
                return status

            if curr.get("connected") and prev_ssid and curr_ssid == prev_ssid:
                err = activate.stderr.strip() if activate.returncode != 0 else "连接验证失败"
                status = {"success": False,
                          "message": f"连接 '{ssid}' 失败，已自动回档到 '{prev_ssid}'",
                          "ssid": ssid, "prev_ssid": prev_ssid, "action": "reverted", "error": err}
                _save_wifi_status(status)
                return status

        reverted = False
        if prev_con_name and prev_ssid:
            reverted = _activate_connection(prev_con_name)

        err = activate.stderr.strip() if activate.returncode != 0 else "连接验证失败"
        if reverted:
            status = {"success": False,
                      "message": f"连接 '{ssid}' 失败，已回档到 '{prev_ssid}'",
                      "ssid": ssid, "prev_ssid": prev_ssid, "action": "reverted", "error": err}
        else:
            status = {"success": False,
                      "message": f"连接 '{ssid}' 失败，且回档失败，请手动检查网络",
                      "ssid": ssid, "prev_ssid": prev_ssid, "action": "failed", "error": err}
        _save_wifi_status(status)
        return status

    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"切换 WiFi 异常：{e}")


# ─────────────────────────────────────────────
# 删除 / 优先级 / 重命名 / 自动连接
# ─────────────────────────────────────────────

def delete_wifi_connection(con_name: str) -> dict:
    """删除指定 WiFi 连接配置"""
    if not con_name:
        return {"success": False, "message": "连接名不能为空"}
    try:
        r = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "delete", con_name],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return {"success": True, "message": f"已删除连接 '{con_name}'"}
        return {"success": False, "message": f"删除失败：{r.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": f"删除失败：{e}"}


def update_wifi_priority(con_name: str, priority: int) -> dict:
    """修改连接的自动连接优先级"""
    if not con_name:
        return {"success": False, "message": "连接名不能为空"}
    if not (0 <= priority <= 100):
        return {"success": False, "message": "优先级必须在 0-100 之间"}
    try:
        r = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "modify", con_name,
             "connection.autoconnect-priority", str(priority)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return {"success": True, "message": f"'{con_name}' 优先级已更新为 {priority}"}
        return {"success": False, "message": f"修改失败：{r.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": f"修改失败：{e}"}


def update_connection_autoconnect(con_name: str, autoconnect: bool) -> dict:
    """修改连接的自动连接开关"""
    if not con_name:
        return {"success": False, "message": "连接名不能为空"}
    try:
        value = "yes" if autoconnect else "no"
        r = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "modify", con_name,
             "connection.autoconnect", value],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            status = "开启" if autoconnect else "关闭"
            return {"success": True, "message": f"'{con_name}' 自动连接已{status}", "autoconnect": autoconnect}
        return {"success": False, "message": f"修改失败：{r.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": f"修改失败：{e}"}


def update_connection_name(old_name: str, new_name: str) -> dict:
    """修改连接名称"""
    if not old_name:
        return {"success": False, "message": "原连接名不能为空"}
    if not new_name:
        return {"success": False, "message": "新连接名不能为空"}
    if old_name == new_name:
        return {"success": True, "message": "连接名未变更"}
    try:
        r = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "modify", old_name,
             "connection.id", new_name],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return {"success": True, "message": f"连接名已更新为 '{new_name}'"}
        return {"success": False, "message": f"修改失败：{r.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": f"修改失败：{e}"}


# ─────────────────────────────────────────────
# WiFi 连接详情
# ─────────────────────────────────────────────

def get_connection_details() -> dict:
    """获取当前 WiFi 连接的详细信息：MAC、IP、网关、DNS、信道等"""
    details = {
        "mac": None, "ip": None, "gateway": None, "dns": [],
        "channel": None, "frequency": None, "mode": None, "rate": None,
    }
    try:
        # IP 地址和 MAC
        result = subprocess.run(
            ["ip", "addr", "show", WIFI_INTERFACE],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            mac_match = re.search(r"link/ether\s+([0-9a-f:]{17})", result.stdout, re.I)
            if mac_match:
                details["mac"] = mac_match.group(1).upper()
            ip_match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", result.stdout)
            if ip_match:
                details["ip"] = ip_match.group(1)

        # 网关
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            gw_match = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
            if gw_match:
                details["gateway"] = gw_match.group(1)

        # DNS
        if os.path.exists("/etc/resolv.conf"):
            with open("/etc/resolv.conf", "r") as f:
                for line in f:
                    match = re.search(r"nameserver\s+(\d+\.\d+\.\d+\.\d+)", line)
                    if match:
                        details["dns"].append(match.group(1))

        # 信道、频率、速率（优先 iw，fallback nmcli）
        result = subprocess.run(
            ["iw", "dev", WIFI_INTERFACE, "link"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            ch_match = re.search(r"channel\s+(\d+)", result.stdout)
            if ch_match:
                details["channel"] = int(ch_match.group(1))
            freq_match = re.search(r"freq:\s+(\d+)", result.stdout)
            if freq_match:
                details["frequency"] = int(freq_match.group(1))
            rate_match = re.search(r"rx\s+rate:\s+([\d.]+\s+\w+)", result.stdout)
            if rate_match:
                details["rate"] = rate_match.group(1)

        # Fallback: nmcli dev wifi (iw not installed on some boards)
        if not details["channel"] or not details["rate"]:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,CHAN,RATE", "dev", "wifi"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split(":")
                    if len(parts) >= 5 and parts[0] == "*":
                        try:
                            if not details["channel"]:
                                details["channel"] = int(parts[3]) if parts[3].isdigit() else None
                            if not details["rate"]:
                                details["rate"] = parts[4].strip()
                            # Guess frequency from channel
                            if not details["frequency"] and parts[3].isdigit():
                                ch = int(parts[3])
                                if 1 <= ch <= 14:
                                    details["frequency"] = 2412 + (ch - 1) * 5
                                elif 36 <= ch <= 165:
                                    details["frequency"] = 5180 + (ch - 36) * 5
                        except (ValueError, IndexError):
                            pass
                        break

    except Exception:
        pass

    return details


# ─────────────────────────────────────────────
# AP 模式管理
# ─────────────────────────────────────────────

def _ensure_ap_config_exists():
    """确保 AP 配置文件存在"""
    try:
        os.makedirs(os.path.dirname(AP_CONFIG_FILE), exist_ok=True)
        if not os.path.exists(AP_CONFIG_FILE):
            default_config = {
                "ssid": "TeslaUSB-Setup",
                "passphrase": "teslausb123",
                "enabled": True
            }
            with open(AP_CONFIG_FILE, "w") as f:
                json.dump(default_config, f, indent=2)
    except Exception:
        pass


def get_ap_config() -> dict:
    """获取 AP 配置"""
    _ensure_ap_config_exists()
    try:
        with open(AP_CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"ssid": "TeslaUSB-Setup", "passphrase": "teslausb123", "enabled": True}


def set_ap_config(ssid: str, passphrase: str) -> dict:
    """设置 AP 配置"""
    if not ssid or len(ssid) < 1 or len(ssid) > 32:
        return {"success": False, "message": "SSID 必须为 1-32 字符"}
    if passphrase and (len(passphrase) < 8 or len(passphrase) > 63):
        return {"success": False, "message": "密码必须为 8-63 字符"}
    try:
        _ensure_ap_config_exists()
        config = get_ap_config()
        config["ssid"] = ssid
        config["passphrase"] = passphrase
        with open(AP_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        return {"success": True, "message": "AP 配置已更新"}
    except Exception as e:
        return {"success": False, "message": f"保存配置失败: {e}"}


def get_ap_status() -> dict:
    """获取 AP 状态（检查 hostapd 是否运行）"""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "hostapd"],
            capture_output=True, text=True, timeout=5,
        )
        ap_active = result.stdout.strip() == "active"
        return {
            "available": True,
            "ap_active": ap_active,
            "active": ap_active,
            "message": "AP 已开启" if ap_active else "AP 已关闭"
        }
    except Exception:
        return {"available": False, "ap_active": False, "active": False, "message": "检查失败"}


def get_ap_force_mode() -> str:
    """获取当前 AP 强制模式"""
    try:
        if os.path.exists(FORCE_MODE_FILE):
            with open(FORCE_MODE_FILE, "r") as f:
                return f.read().strip()
    except Exception:
        pass
    return "auto"


def set_ap_force_mode(mode: str) -> dict:
    """设置 AP 强制模式: force-on / force-off / auto"""
    if mode not in ("force-on", "force-off", "auto"):
        return {"success": False, "message": "无效的模式"}
    try:
        if mode == "auto":
            if os.path.exists(FORCE_MODE_FILE):
                os.remove(FORCE_MODE_FILE)
        else:
            with open(FORCE_MODE_FILE, "w") as f:
                f.write(mode)
        return {"success": True, "message": f"AP 模式已设置为: {mode}"}
    except Exception as e:
        return {"success": False, "message": f"设置 AP 模式失败: {e}"}


def start_ap() -> dict:
    """手动启动 AP"""
    try:
        subprocess.run(
            ["sudo", "-n", "systemctl", "start", "hostapd"],
            capture_output=True, text=True, timeout=30,
        )
        return {"success": True, "message": "AP 已启动"}
    except Exception as e:
        return {"success": False, "message": f"启动 AP 失败: {e}"}


def stop_ap() -> dict:
    """手动停止 AP"""
    try:
        subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "hostapd"],
            capture_output=True, text=True, timeout=30,
        )
        return {"success": True, "message": "AP 已停止"}
    except Exception as e:
        return {"success": False, "message": f"停止 AP 失败: {e}"}
