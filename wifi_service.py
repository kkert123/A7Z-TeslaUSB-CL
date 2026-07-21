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
import socket
import subprocess
import time
from datetime import datetime
from ipaddress import ip_address, IPv4Address, IPv6Address
from typing import List, Optional, Tuple
from urllib.parse import urlparse

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

def _pick_best_bssid(ssid: str) -> Optional[str]:
    """
    扫描同名 SSID 的所有 BSSID，5GHz 优先选择。

    策略:
      1. 扫描 nmcli 获取所有同名 SSID 的 AP（含频段和信号）
      2. 分离 5GHz 和 2.4GHz
      3. 如果 5GHz 信号 >= 20%（-80dBm 以上），选 5GHz 中信号最强的
      4. 否则选信号最强的那个（不限频段）
      5. 只有一个 AP 时直接返回其 BSSID

    Returns:
        BSSID (MAC 地址) 或 None（无需指定 BSSID）
    """
    _5GHZ_MIN_SIGNAL = 20  # 5GHz 最低信号阈值

    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,BSSID,FREQ,SIGNAL", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None

        ap_2g: list = []   # [(signal, bssid), ...]
        ap_5g: list = []   # [(signal, bssid), ...]
        # BSSID 格式: XX:XX:XX:XX:XX:XX
        _mac_re = re.compile(r'((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})')

        for line in r.stdout.splitlines():
            # 格式: SSID:BSSID:FREQ:SIGNAL（SSID 中冒号转义为 \:）
            # 从右向左解析: 最后两个数字是 SIGNAL 和 FREQ，再往前是 BSSID(MAC)
            line = line.strip()
            if not line:
                continue

            # 反向分割：取最后 3 段 BSSID:FREQ:SIGNAL
            parts = line.rsplit(":", 3)
            if len(parts) < 4:
                continue

            ap_ssid_raw = parts[0].replace("\\:", ":").strip()
            if ap_ssid_raw != ssid:
                continue

            try:
                freq = int(parts[2].strip())
                signal = int(parts[3].strip())
                bssid = parts[1].strip()
            except (ValueError, IndexError):
                continue

            # 验证 BSSID 格式
            if not _mac_re.fullmatch(bssid):
                continue

            if freq >= 5000:
                ap_5g.append((signal, bssid))
            else:
                ap_2g.append((signal, bssid))

        if not ap_5g and not ap_2g:
            return None  # 没扫到，正常连接

        # 只有一个频段 → 直接选信号最强的
        if ap_5g and not ap_2g:
            best = max(ap_5g, key=lambda x: x[0])
            return best[1]
        if ap_2g and not ap_5g:
            best = max(ap_2g, key=lambda x: x[0])
            return best[1]

        # 两个频段都有 → 5GHz 优先（需信号达标）
        best_5g = max(ap_5g, key=lambda x: x[0])
        best_2g = max(ap_2g, key=lambda x: x[0])

        if best_5g[0] >= _5GHZ_MIN_SIGNAL:
            return best_5g[1]

        # 5GHz 信号太差，用 2.4GHz
        return best_2g[1]

    except Exception:
        return None


def switch_wifi(ssid: str, password: str = "", prefer_5ghz: bool = True) -> dict:
    """切换到指定 WiFi，失败时自动回档到上一个连接
    
    Args:
        ssid: WiFi SSID
        password: WiFi 密码（开放网络留空）
        prefer_5ghz: True 时同一SSID有2.4G和5G则优选5GHz信号
    """
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID 长度必须为 1-32 字符")
    if password and (len(password) < 8 or len(password) > 63):
        raise ValueError("密码长度必须为 8-63 字符（开放网络可留空）")

    # 5GHz 优先: 扫描同名 SSID 的所有 BSSID，选最佳
    target_bssid = _pick_best_bssid(ssid) if prefer_5ghz else None

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
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    raise RuntimeError(f"修改连接失败：{r.stderr.strip()}")
            # 未提供密码 = 使用已保存的凭据，不修改连接 profile
        else:
            if password:
                cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
                       "password", password, "name", con_name]
                if target_bssid:
                    cmd.insert(-2, "bssid")
                    cmd.insert(-2, target_bssid)
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode != 0 and "No network with SSID" in (r.stderr or ""):
                    raise ValueError(f"找不到网络 '{ssid}'")
            else:
                # 开放网络：断开 NM → iw connect → NM 重新接管 DHCP
                subprocess.run(
                    ["sudo", "-n", "nmcli", "device", "disconnect", WIFI_INTERFACE],
                    capture_output=True, text=True, timeout=5,
                )
                time.sleep(1)
                iw_cmd = ["sudo", "-n", "/sbin/iw", "dev", WIFI_INTERFACE, "connect", ssid]
                if target_bssid:
                    iw_cmd.extend([target_bssid])
                subprocess.run(iw_cmd, capture_output=True, text=True, timeout=15)

        # 激活连接:
        # - 有密码或已有保存的连接: 使用 nmcli connection up
        # - 无密码且无保存的连接: 已通过 iw 连接（开放网络），跳过
        if password or con_exists:
            activate = subprocess.run(
                ["sudo", "-n", "nmcli", "connection", "up", con_name],
                capture_output=True, text=True, timeout=30,
            )
        else:
            activate = None

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
                err = (activate.stderr.strip() if activate and activate.returncode != 0 else "连接验证失败")
                status = {"success": False,
                          "message": f"连接 '{ssid}' 失败，已自动回档到 '{prev_ssid}'",
                          "ssid": ssid, "prev_ssid": prev_ssid, "action": "reverted", "error": err}
                _save_wifi_status(status)
                return status

        reverted = False
        if prev_con_name and prev_ssid:
            reverted = _activate_connection(prev_con_name)

        err = (activate.stderr.strip() if activate and activate.returncode != 0 else "连接验证失败")
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
    if not (0 <= priority <= 800):
        return {"success": False, "message": "优先级必须在 0-800 之间"}
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
                "passphrase": "CHANGE_ME_AP_PASSWORD",
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
        return {"ssid": "TeslaUSB-Setup", "passphrase": "CHANGE_ME_AP_PASSWORD", "enabled": True}


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


# ═══════════════════════════════════════════════════════════════
# WifiSmartSwitch — 自动 WiFi 智能切换（从 wifi_smart_switch.sh 迁移）
# ═══════════════════════════════════════════════════════════════

SMART_SWITCH_LOCK_FILE = "/var/run/wifi-smart-switch.lock"
SMART_SWITCH_STATE_FILE = "/var/run/wifi-smart-switch.state"
SMART_SWITCH_FAILURE_COUNT_FILE = "/var/run/wifi-failure-count"
SMART_SWITCH_LOG_FILE = "/var/log/wifi-smart-switch.log"
SMART_SWITCH_PRIORITY_CONFIG = "/opt/radxa_data/teslausb/config/wifi_priority.json"

# 默认优先级（从 shell 脚本继承）
DEFAULT_WIFI_PRIORITY = {
    "CD": 400, "C12345": 300, "HP-00J6O": 200,
    "C123": 100, "189-AP": 50, "YL-MIFI-000500": 150,
}

# 可调参数
SWITCH_COOLDOWN_SEC = 300      # 切换冷却时间（秒）
SIGNAL_THRESHOLD_DBM = 30      # 最低信号强度阈值
CONNECT_WAIT_SEC = 2           # 连接等待间隔（秒）
CONNECTIVITY_TARGETS = ["8.8.8.8", "1.1.1.1", "baidu.com"]
MAX_CONNECT_RETRIES = 3        # 单次切换重试次数


def _load_priority_config() -> dict:
    """加载 WiFi 优先级配置，优先读 JSON 文件，fallback 默认值"""
    try:
        if os.path.exists(SMART_SWITCH_PRIORITY_CONFIG):
            with open(SMART_SWITCH_PRIORITY_CONFIG, "r") as f:
                cfg = json.load(f)
                if isinstance(cfg, dict) and cfg:
                    # 确保值是 int
                    return {k: int(v) for k, v in cfg.items()}
    except Exception:
        pass
    return DEFAULT_WIFI_PRIORITY.copy()


class WifiSmartSwitch:
    """WiFi 智能切换引擎
    - 定期检测网络连通性（quick_check）
    - 按优先级自动切换到最优 WiFi（full_check）
    - 冷却机制、失败计数、锁文件防止并发
    """

    _stream_handler_attached = False

    def __init__(self, log_to_file: bool = True):
        self.priority = _load_priority_config()
        self._setup_logging(log_to_file)

    def _setup_logging(self, to_file: bool):
        import logging
        self.log = logging.getLogger("WifiSmartSwitch")
        self.log.setLevel(logging.INFO)
        self.log.handlers.clear()
        fmt = logging.Formatter("%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        if to_file:
            try:
                fh = logging.FileHandler(SMART_SWITCH_LOG_FILE)
                fh.setFormatter(fmt)
                self.log.addHandler(fh)
            except Exception:
                pass
        # StreamHandler 只附加一次（防止同进程多次实例化时日志翻倍）
        if not WifiSmartSwitch._stream_handler_attached:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            self.log.addHandler(sh)
            WifiSmartSwitch._stream_handler_attached = True

    # ── 锁机制 ──

    def _acquire_lock(self) -> bool:
        """获取 PID 锁文件，防止并发执行"""
        try:
            if os.path.exists(SMART_SWITCH_LOCK_FILE):
                with open(SMART_SWITCH_LOCK_FILE, "r") as f:
                    old_pid = f.read().strip()
                if old_pid:
                    try:
                        os.kill(int(old_pid), 0)
                        self.log.info("脚本已在运行 (PID: %s)，退出", old_pid)
                        return False
                    except (OSError, ValueError):
                        os.remove(SMART_SWITCH_LOCK_FILE)
            with open(SMART_SWITCH_LOCK_FILE, "w") as f:
                f.write(str(os.getpid()))
            return True
        except Exception as e:
            self.log.warning("获取锁失败: %s", e)
            return False

    def _release_lock(self):
        try:
            if os.path.exists(SMART_SWITCH_LOCK_FILE):
                os.remove(SMART_SWITCH_LOCK_FILE)
        except Exception:
            pass

    # ── 优先级 ──

    def get_priority(self, ssid: str) -> int:
        """获取指定 SSID 的优先级（未配置的返回 0）"""
        return self.priority.get(ssid, 0)

    def reload_priority(self):
        """重新加载优先级配置（供 Web UI 修改后调用）"""
        self.priority = _load_priority_config()

    # ── 网络检测 ──

    def _get_current_ssid(self) -> str:
        """获取当前连接的 WiFi SSID（空字符串表示未连接）"""
        try:
            # 方法 1: nmcli device status
            r = subprocess.run(
                ["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if line.startswith("wlan0:connected:"):
                        return line.split(":")[2].strip()

            # 方法 2: nmcli device wifi (当前活跃)
            r2 = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,ACTIVE", "device", "wifi"],
                capture_output=True, text=True, timeout=5,
            )
            if r2.returncode == 0:
                for line in r2.stdout.splitlines():
                    if line.endswith(":yes"):
                        return line.rsplit(":", 1)[0].strip()

            # 方法 3: iwgetid
            r3 = subprocess.run(
                ["iwgetid", "-r"], capture_output=True, text=True, timeout=5,
            )
            if r3.returncode == 0 and r3.stdout.strip():
                return r3.stdout.strip()
        except Exception:
            pass
        return ""

    def _check_connectivity(self) -> bool:
        """并行 ping 3 个目标，任一成功即视为网络正常"""
        from subprocess import Popen, DEVNULL

        processes = []
        for target in CONNECTIVITY_TARGETS:
            try:
                p = Popen(
                    ["ping", "-c", "2", "-W", "3", target],
                    stdout=DEVNULL, stderr=DEVNULL,
                )
                processes.append(p)
            except Exception:
                continue

        if not processes:
            return False

        # 等待任一成功（最多等 6 秒）
        deadline = time.time() + 6
        success = False
        while time.time() < deadline:
            for p in processes[:]:
                if p.poll() is not None:
                    if p.returncode == 0:
                        success = True
                    processes.remove(p)
            if success:
                break
            if not processes:
                break
            time.sleep(0.2)

        # 清理残留进程
        for p in processes:
            try:
                p.kill()
                p.wait()
            except Exception:
                pass

        return success

    # ── 扫描 ──

    def _scan_available(self) -> list:
        """扫描可用 WiFi，仅返回优先级列表中的网络，按优先级降序排列"""
        try:
            # 触发扫描
            r_scan = subprocess.run(
                ["sudo", "-n", "nmcli", "dev", "wifi", "rescan"],
                capture_output=True, text=True, timeout=8,
            )
            if r_scan.returncode != 0:
                self.log.debug("rescan 失败 (可能无 sudo 免密): %s",
                               r_scan.stderr.strip()[:120])
            time.sleep(1)

            r = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return []

            results: list = []
            seen: set = set()
            for line in r.stdout.splitlines():
                # 处理含冒号的 SSID
                parts = line.rsplit(":", 1)
                if len(parts) < 2:
                    continue
                ssid = parts[0].replace("\\:", ":").strip()
                signal_str = parts[1].strip()

                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)

                try:
                    signal = int(signal_str)
                except ValueError:
                    signal = 0

                prio = self.get_priority(ssid)
                if prio > 0:
                    results.append({"ssid": ssid, "signal": signal, "priority": prio})

            # 先按优先级降序，再按信号降序
            results.sort(key=lambda x: (-x["priority"], -x["signal"]))
            return results
        except Exception:
            return []

    # ── 切换 ──

    def _can_switch(self) -> bool:
        """检查是否超过冷却时间"""
        try:
            if os.path.exists(SMART_SWITCH_STATE_FILE):
                with open(SMART_SWITCH_STATE_FILE, "r") as f:
                    last_ts = int(f.read().strip())
                if int(time.time()) - last_ts < SWITCH_COOLDOWN_SEC:
                    return False
        except Exception:
            pass
        return True

    def _save_switch_time(self):
        try:
            with open(SMART_SWITCH_STATE_FILE, "w") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

    def _switch_to(self, ssid: str) -> bool:
        """切换到指定 WiFi（优先 5GHz），返回是否成功"""
        if not self._can_switch():
            self.log.info("切换冷却中，跳过切换到 %s", ssid)
            return False

        # 5GHz 优先：扫描同名 SSID 的最佳 BSSID
        best_bssid = _pick_best_bssid(ssid)
        if best_bssid:
            self.log.info("5GHz优先: %s → BSSID %s", ssid, best_bssid)

        self.log.info("正在切换到WiFi: %s", ssid)

        # 断开当前连接
        try:
            subprocess.run(
                ["sudo", "-n", "nmcli", "device", "disconnect", "wlan0"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        time.sleep(CONNECT_WAIT_SEC)

        # 尝试连接，最多 3 次
        for attempt in range(1, MAX_CONNECT_RETRIES + 1):
            try:
                cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", ssid]
                if best_bssid:
                    cmd.extend(["bssid", best_bssid])
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=20,
                )
                if r.returncode == 0:
                    self.log.info("切换成功: %s", ssid)
                    self._save_switch_time()
                    return True
            except Exception:
                pass
            if attempt < MAX_CONNECT_RETRIES:
                time.sleep(CONNECT_WAIT_SEC)

        self.log.info("切换失败: %s", ssid)
        return False

    # ── 快速检测 ──

    def quick_check(self) -> None:
        """快速网络连通性检测，连续失败 2 次触发完整检查"""
        self.log.info("开始快速检测...")

        if self._check_connectivity():
            # 网络正常，重置失败计数
            try:
                if os.path.exists(SMART_SWITCH_FAILURE_COUNT_FILE):
                    os.remove(SMART_SWITCH_FAILURE_COUNT_FILE)
            except Exception:
                pass
            self.log.info("网络正常")

            # 如果 AP 正在运行，自动关闭
            try:
                force_mode = get_ap_force_mode()
                if force_mode != "force-on":
                    result = subprocess.run(
                        ["systemctl", "is-active", "hostapd"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.stdout.strip() == "active":
                        self.log.info("WiFi 已恢复，关闭 AP 热点...")
                        subprocess.run(
                            ["sudo", "-n", "systemctl", "stop", "hostapd"],
                            capture_output=True, text=True, timeout=30,
                        )
                        subprocess.run(
                            ["sudo", "-n", "systemctl", "stop", "dnsmasq"],
                            capture_output=True, text=True, timeout=30,
                        )
                        self.log.info("AP 热点已关闭")
            except Exception:
                pass

            return

        # 网络不通，累加失败计数
        count = 0
        try:
            if os.path.exists(SMART_SWITCH_FAILURE_COUNT_FILE):
                with open(SMART_SWITCH_FAILURE_COUNT_FILE, "r") as f:
                    count = int(f.read().strip())
        except Exception:
            pass

        count += 1
        try:
            with open(SMART_SWITCH_FAILURE_COUNT_FILE, "w") as f:
                f.write(str(count))
        except Exception:
            pass

        self.log.info("网络不通 (失败 %d 次)", count)

        if count >= 2:
            self.log.info("连续失败，触发完整检测")
            self.full_check()

    # ── 完整检测 ──

    def full_check(self) -> None:
        """完整 WiFi 检测与优化切换"""
        self.log.info("开始完整检测...")

        cur_ssid = self._get_current_ssid()
        is_connected = self._check_connectivity()
        cur_priority = self.get_priority(cur_ssid) if cur_ssid else 0

        if is_connected:
            self.log.info("当前连接: %s (优先级: %d)", cur_ssid or "未知", cur_priority)

            # 扫描可用 WiFi
            available = self._scan_available()
            for net in available:
                ssid = net["ssid"]
                signal = net["signal"]
                net_priority = net["priority"]

                if net_priority > cur_priority and signal >= SIGNAL_THRESHOLD_DBM:
                    self.log.info(
                        "发现更优网络: %s (优先级: %d, 信号: %d%%)",
                        ssid, net_priority, signal,
                    )
                    if self._switch_to(ssid):
                        return  # 切换成功，退出

            self.log.info("未找到更优网络")

        else:
            self.log.info("网络连接异常，尝试重连...")

            available = self._scan_available()
            reconnected = False
            for net in available:
                ssid = net["ssid"]
                signal = net["signal"]

                if signal >= SIGNAL_THRESHOLD_DBM:
                    self.log.info("尝试连接: %s (信号: %d%%)", ssid, signal)
                    if self._switch_to(ssid):
                        reconnected = True
                        break

            # 优先级列表中无可连接网络 → 尝试所有 NetworkManager 已保存的网络
            if not reconnected and not available:
                self.log.info("优先级列表无可连接网络，尝试所有 NM 已保存连接...")
                saved = self._get_saved_connections()
                for ssid in saved:
                    self.log.info("尝试已保存网络: %s", ssid)
                    if self._switch_to(ssid):
                        reconnected = True
                        break

            # 所有 WiFi 都无法连接 → 自动启用 AP 热点
            if not reconnected:
                self.log.info("所有网络均无法连接，启动 AP 热点...")
                self._start_ap_fallback()

        self.log.info("完整检测完成")

    def _get_saved_connections(self) -> list:
        """获取所有 NetworkManager 已保存的 WiFi 连接（排除当前连接）"""
        try:
            cur_ssid = self._get_current_ssid()
            r = subprocess.run(
                ["nmcli", "-t", "-f", "TYPE,NAME", "connection"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return []
            connections = []
            for line in r.stdout.splitlines():
                parts = line.split(":", 1)
                if len(parts) == 2 and parts[0].strip() == "802-11-wireless":
                    name = parts[1].strip()
                    if name and name != cur_ssid:
                        connections.append(name)
            return connections
        except Exception:
            return []

    def _start_ap_fallback(self) -> None:
        """当所有 WiFi 不可用时自动启用 AP 热点"""
        try:
            force_mode = get_ap_force_mode()
            if force_mode == "force-off":
                self.log.info("AP 强制关闭，跳过自动启用")
                return

            # 检查 hostapd 是否已在运行
            result = subprocess.run(
                ["systemctl", "is-active", "hostapd"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "active":
                self.log.info("AP 已运行")
                return

            # 确保 hostapd 配置文件存在
            if not os.path.exists("/etc/hostapd/hostapd.conf"):
                self._generate_hostapd_conf()

            # 启动 hostapd + dnsmasq
            self.log.info("正在启动 AP 热点...")
            subprocess.run(
                ["sudo", "-n", "systemctl", "start", "hostapd"],
                capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                ["sudo", "-n", "systemctl", "start", "dnsmasq"],
                capture_output=True, text=True, timeout=30,
            )
            self.log.info("AP 热点已启动 (SSID: %s)", get_ap_config().get("ssid", "TeslaUSB-Setup"))
        except Exception as e:
            self.log.error("启动 AP 失败: %s", e)

    def _generate_hostapd_conf(self) -> None:
        """生成 hostapd 配置文件"""
        config = get_ap_config()
        ssid = config.get("ssid", "TeslaUSB-Setup")
        passphrase = config.get("passphrase", "CHANGE_ME_AP_PASSWORD")

        conf = f"""interface=wlan0
driver=nl80211
ssid={ssid}
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase={passphrase}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
"""
        with open("/tmp/hostapd.conf.tmp", "w") as f:
            f.write(conf)
        subprocess.run(
            ["sudo", "-n", "cp", "/tmp/hostapd.conf.tmp", "/etc/hostapd/hostapd.conf"],
            capture_output=True, timeout=10,
        )
        os.unlink("/tmp/hostapd.conf.tmp")

    # ── 入口 ──

    def run(self, mode: str) -> int:
        """执行主逻辑，返回退出码"""
        if not self._acquire_lock():
            return 0  # 锁冲突是正常竞争，不算错误

        try:
            if mode == "--quick":
                self.quick_check()
            else:
                self.full_check()
            return 0
        except Exception as e:
            self.log.error("执行异常: %s", e)
            return 1
        finally:
            self._release_lock()


def run_smart_switch(mode: str) -> int:
    """CLI 入口函数"""
    switcher = WifiSmartSwitch()
    return switcher.run(mode)


# ─────────────────────────────────────────────
# 网络测速
# ─────────────────────────────────────────────

# 预设测速服务器（key → 显示名 + 基础URL，支持 ?bytes=N 参数）
_SPEED_TEST_SERVERS = {
    "cloudflare": {
        "name": "Cloudflare（全球）",
        "url": "https://speed.cloudflare.com/__down",
    },
    "__lan__": {
        "name": "局域网（网关测速）",
        "url": None,  # 动态获取
    },
}

_SPEED_TEST_DEFAULT_SERVER = "cloudflare"
_SPEED_TEST_TIMEOUT = 30  # 单次测试最大秒数
# 外网测速递增大小
_SPEED_TEST_SIZES = [
    (1 * 1024 * 1024, "1MB"),    # 预热
    (5 * 1024 * 1024, "5MB"),    # 正式
    (10 * 1024 * 1024, "10MB"),  # 大文件
]
# 局域网测速大小（网关页面通常较小，用多轮下载累加）
_LAN_TEST_SIZES = [
    (1 * 1024 * 1024, "1MB"),
    (5 * 1024 * 1024, "5MB"),
]

# SSRF 防护 — 禁止目标 IP 范围
_SSRF_BLOCKED_NETS = [
    # 回环
    (ip_address("127.0.0.0"),    ip_address("127.255.255.255")),
    (ip_address("::1"),           ip_address("::1")),
    # A/B/C 类私有
    (ip_address("10.0.0.0"),      ip_address("10.255.255.255")),
    (ip_address("172.16.0.0"),    ip_address("172.31.255.255")),
    (ip_address("192.168.0.0"),   ip_address("192.168.255.255")),
    # 链路本地
    (ip_address("169.254.0.0"),   ip_address("169.254.255.255")),
    (ip_address("fe80::"),        ip_address("febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff")),
    # 文档/测试
    (ip_address("0.0.0.0"),       ip_address("0.255.255.255")),
]


def _validate_speed_test_url(raw_url: str) -> str:
    """
    SSRF 安全校验：验证用户提供的测速 URL，返回规范化 URL 或抛出 ValueError。

    规则:
      1. 仅允许 http/https scheme
      2. 解析主机名 → DNS 解析 → 检查所有 IP 不在私有/回环/链路本地范围
      3. 规范化 URL（去除 fragment、多余斜杠）
    """
    raw = raw_url.strip()
    if not raw:
        raise ValueError("测速地址不能为空")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https 协议")
    if not parsed.hostname:
        raise ValueError("无法解析服务器地址")

    hostname = parsed.hostname

    # 尝试将 hostname 解析为 IP（纯 IP 地址场景）
    try:
        addr = ip_address(hostname)
        _check_ip_not_blocked(addr)
    except ValueError:
        # 不是 IP 地址 → 是域名，做 DNS 解析
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            ips = set(info[4][0] for info in infos)
            for ip_str in ips:
                _check_ip_not_blocked(ip_address(ip_str))
        except socket.gaierror:
            pass  # DNS 解析失败由 curl 在实际请求时报错

    # 规范化：去除 fragment，保留完整路径和查询参数
    normalized = parsed._replace(fragment="").geturl()
    return normalized


def _check_ip_not_blocked(addr):
    """检查 IP 是否在 SSRF 黑名单中（自动跳过不同 IP 版本）"""
    for lo, hi in _SSRF_BLOCKED_NETS:
        try:
            if lo <= addr <= hi:
                raise ValueError(f"禁止访问内网地址: {addr}")
        except TypeError:
            # IPv4 vs IPv6 无法比较，跳过
            continue


def _get_gateway_ip() -> Optional[str]:
    """获取当前默认网关 IP"""
    try:
        r = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            m = re.search(r"via\s+(\S+)", r.stdout)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _build_test_url(server_key: str, custom_url: Optional[str]) -> Tuple[str, str, bool]:
    """
    根据选择的服务器构建测速 URL。

    Returns:
        (base_url, display_name, is_lan) — is_lan 表示局域网模式（不使用 ?bytes= 参数）
    """
    if server_key == "__custom__":
        if not custom_url:
            raise ValueError("请提供自定义测速服务器地址")
        validated = _validate_speed_test_url(custom_url)
        return validated, "自定义服务器", False
    if server_key == "__lan__":
        gw = _get_gateway_ip()
        if not gw:
            raise ValueError("无法获取网关IP，请使用自定义地址")
        return f"http://{gw}/", f"局域网 ({gw})", True
    if server_key in _SPEED_TEST_SERVERS:
        srv = _SPEED_TEST_SERVERS[server_key]
        return srv["url"], srv["name"], False
    raise ValueError(f"未知测速服务器: {server_key}")


def run_speed_test(server: Optional[str] = None, custom_url: Optional[str] = None) -> dict:
    """
    执行网络下载速度测试，返回结构化结果。

    使用 curl 从指定测速服务器下载测试文件，通过 curl -w 获取精确的
    传输时间和速度指标。分多轮递增测试（1MB→5MB→10MB），取最大值。

    Args:
        server: 预设服务器 key（默认 "cloudflare"），"__custom__" 使用自定义URL
        custom_url: 自定义测速服务器地址（仅 server="__custom__" 时生效）

    Returns:
        {
            "success": bool,
            "server": str,              # 使用的服务器名称
            "download_mbps": float,
            "latency_ms": float,
            "total_bytes": int,
            "total_time_s": float,
            "stages": [...],
            "connected_wifi": str or None,
            "error": str or None,
        }
    """
    # 默认 server
    if not server:
        server = _SPEED_TEST_DEFAULT_SERVER

    result = {
        "success": False,
        "server": server,
        "download_mbps": 0.0,
        "latency_ms": 0.0,
        "total_bytes": 0,
        "total_time_s": 0.0,
        "stages": [],
        "connected_wifi": None,
        "error": None,
    }

    # 构建测速 URL
    try:
        base_url, display_name, is_lan = _build_test_url(server, custom_url)
        result["server"] = display_name
    except ValueError as e:
        result["error"] = str(e)
        return result

    # 选择测试大小：局域网模式下网关页面通常较小，用更保守的大小
    test_sizes = _LAN_TEST_SIZES if is_lan else _SPEED_TEST_SIZES

    # 记录当前 WiFi
    current = get_current_wifi()
    result["connected_wifi"] = current.get("ssid") if current.get("connected") else None

    if not current.get("connected"):
        result["error"] = "设备未连接 WiFi，无法测速"
        return result

    best_mbps = 0.0

    for test_bytes, label in test_sizes:
        stage = {"label": label, "speed_mbps": 0.0, "time_s": 0.0,
                 "latency_ms": 0.0, "bytes": 0, "success": False}

        try:
            fmt = "%{time_namelookup}|%{time_connect}|%{time_starttransfer}|%{time_total}|%{speed_download}|%{size_download}"

            if is_lan:
                # 局域网模式：直接下载网关页面（不支持 ?bytes=N）
                url = base_url
                # 如果网关页面太小(<50KB)，下载多次累加
                if test_bytes > 50 * 1024:
                    # 用 curl range 请求多次模拟大文件
                    total_downloaded = 0
                    total_time = 0.0
                    loops = max(1, test_bytes // (50 * 1024))
                    t_start = time.time()
                    for _ in range(min(loops, 20)):  # 最多20轮防止过慢
                        p = subprocess.run(
                            ["curl", "-s", "-o", "/dev/null", "--max-time", "10", url],
                            capture_output=True, text=True, timeout=15,
                        )
                        if p.returncode != 0:
                            break
                    t_end = time.time()
                    total_time = t_end - t_start
                    total_downloaded = test_bytes  # 估算
                    if total_time > 0:
                        speed_mbps = (total_downloaded * 8) / total_time / 1_000_000
                        stage["speed_mbps"] = round(speed_mbps, 2)
                        stage["time_s"] = round(total_time, 2)
                        stage["latency_ms"] = 0.0
                        stage["bytes"] = total_downloaded
                        stage["success"] = True
                else:
                    # 单次下载
                    proc = subprocess.run(
                        ["curl", "-s", "-o", "/dev/null", "-w", fmt,
                         "--max-time", "10", url],
                        capture_output=True, text=True, timeout=15,
                    )
                    if proc.returncode == 0:
                        parts = proc.stdout.strip().split("|")
                        if len(parts) == 6:
                            speed_download = float(parts[4])
                            size_download = int(parts[5])
                            time_total = float(parts[3])
                            time_starttransfer = float(parts[2])
                            speed_mbps = (speed_download * 8) / 1_000_000
                            latency_ms = time_starttransfer * 1000
                            stage["speed_mbps"] = round(speed_mbps, 2)
                            stage["time_s"] = round(time_total, 2)
                            stage["latency_ms"] = round(latency_ms, 1)
                            stage["bytes"] = size_download
                            stage["success"] = True
                        else:
                            stage["error"] = "网关响应异常"
                    else:
                        stage["error"] = f"网关不可达 (curl {proc.returncode})"
            else:
                # 外网模式：使用 ?bytes=N 参数
                proc = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", fmt,
                     "--max-time", str(_SPEED_TEST_TIMEOUT),
                     f"{base_url}{'&' if '?' in base_url else '?'}bytes={test_bytes}"],
                    capture_output=True, text=True, timeout=_SPEED_TEST_TIMEOUT + 5,
                )

                if proc.returncode != 0:
                    stage["error"] = f"curl 退出码 {proc.returncode}: {proc.stderr[:200]}"
                    result["stages"].append(stage)
                    continue

                parts = proc.stdout.strip().split("|")
                if len(parts) != 6:
                    stage["error"] = f"curl 输出格式异常: {proc.stdout[:200]}"
                    result["stages"].append(stage)
                    continue

                time_starttransfer = float(parts[2])
                time_total = float(parts[3])
                speed_download = float(parts[4])
                size_download = int(parts[5])

                speed_mbps = (speed_download * 8) / 1_000_000
                latency_ms = time_starttransfer * 1000

                stage["speed_mbps"] = round(speed_mbps, 2)
                stage["time_s"] = round(time_total, 2)
                stage["latency_ms"] = round(latency_ms, 1)
                stage["bytes"] = size_download
                stage["success"] = True

            if stage["success"]:
                if stage["speed_mbps"] > best_mbps:
                    best_mbps = stage["speed_mbps"]
                    result["download_mbps"] = stage["speed_mbps"]
                    result["latency_ms"] = stage.get("latency_ms", 0.0)
                    result["total_bytes"] = stage["bytes"]
                    result["total_time_s"] = stage["time_s"]

        except subprocess.TimeoutExpired:
            stage["error"] = f"测试超时（>{_SPEED_TEST_TIMEOUT}s）"
        except ValueError as e:
            stage["error"] = f"解析失败: {e}"
        except Exception as e:
            stage["error"] = str(e)[:200]

        result["stages"].append(stage)

    if any(s["success"] for s in result["stages"]):
        result["success"] = True
    elif not result["error"]:
        last_err = next((s.get("error") for s in reversed(result["stages"])
                         if s.get("error")), "未知错误")
        result["error"] = last_err

    return result


# ─────────────────────────────────────────────
# 上传测速
# ─────────────────────────────────────────────

_UPLOAD_TEST_URL = "https://file.io"
_UPLOAD_TEST_TIMEOUT = 20
_UPLOAD_TEST_SIZES = [
    (256 * 1024, "256KB"),
    (1 * 1024 * 1024, "1MB"),
]


def run_upload_speed_test() -> dict:
    """
    执行网络上传速度测试。

    创建 tmpfs 临时文件 → curl -T 上传 → 清理。
    用 %{speed_upload} 获取 TCP 上行速率。

    Returns:
        {success, upload_mbps, total_bytes, total_time_s, stages[], connected_wifi, error}
    """
    result = {
        "success": False,
        "upload_mbps": 0.0,
        "total_bytes": 0,
        "total_time_s": 0.0,
        "stages": [],
        "connected_wifi": None,
        "error": None,
    }

    current = get_current_wifi()
    result["connected_wifi"] = current.get("ssid") if current.get("connected") else None
    if not current.get("connected"):
        result["error"] = "设备未连接 WiFi，无法测速"
        return result

    best_mbps = 0.0
    tmpfile = "/tmp/_speedtest_upload.bin"

    for test_bytes, label in _UPLOAD_TEST_SIZES:
        stage = {"label": label, "speed_mbps": 0.0, "time_s": 0.0,
                 "bytes": 0, "success": False}

        try:
            # 创建测试文件（tmpfs 零磁盘 IO）
            subprocess.run(
                ["dd", "if=/dev/zero", f"of={tmpfile}",
                 f"bs={test_bytes}", "count=1"],
                capture_output=True, timeout=5,
            )

            fmt = "%{speed_upload}|%{size_upload}|%{time_total}"
            proc = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", fmt,
                 "-T", tmpfile, "--max-time", str(_UPLOAD_TEST_TIMEOUT),
                 _UPLOAD_TEST_URL],
                capture_output=True, text=True,
                timeout=_UPLOAD_TEST_TIMEOUT + 10,
            )

            # 清理
            try:
                os.remove(tmpfile)
            except Exception:
                pass

            if proc.returncode not in (0, 28, 52):
                # 28=timeout 52=empty reply 都可能是服务器正常行为
                if proc.returncode != 0:
                    continue

            parts = proc.stdout.strip().split("|")
            if len(parts) != 3:
                continue

            speed_upload = float(parts[0])
            size_upload = int(parts[1])
            time_total = float(parts[2])

            if size_upload < 4096:  # 忽略过小的结果
                stage["error"] = f"上传数据太少 ({size_upload}B)"
                result["stages"].append(stage)
                continue

            speed_mbps = (speed_upload * 8) / 1_000_000
            stage["speed_mbps"] = round(speed_mbps, 2)
            stage["time_s"] = round(time_total, 2)
            stage["bytes"] = size_upload
            stage["success"] = True

            if speed_mbps > best_mbps:
                best_mbps = speed_mbps
                result["upload_mbps"] = round(speed_mbps, 2)
                result["total_bytes"] = size_upload
                result["total_time_s"] = round(time_total, 2)

        except subprocess.TimeoutExpired:
            stage["error"] = f"上传超时"
        except Exception as e:
            stage["error"] = str(e)[:200]

        result["stages"].append(stage)

    # 清理可能遗留的文件
    try:
        os.remove(tmpfile)
    except Exception:
        pass

    if any(s["success"] for s in result["stages"]):
        result["success"] = True
    elif not result["error"]:
        result["error"] = next((s.get("error", "") for s in reversed(result["stages"]) if s.get("error")), "未知错误")

    return result


# 命令行直接执行
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2 or sys.argv[1] not in ("--quick", "--full"):
        print("用法: python wifi_service.py [--quick|--full]", file=sys.stderr)
        print("  --quick   快速检测网络连接", file=sys.stderr)
        print("  --full    完整检测并优化WiFi连接", file=sys.stderr)
        sys.exit(1)
    sys.exit(run_smart_switch(sys.argv[1]))
