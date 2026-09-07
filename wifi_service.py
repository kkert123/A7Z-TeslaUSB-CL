"""
TeslaUSB A7Z - WiFi 服务模块
基于 NetworkManager (nmcli) 实现：
  - 当前连接查询 / WiFi 扫描 / 连接切换（含自动回档）
  - 连接列表 / 优先级管理 / 删除 / 重命名
  - AP 热点管理
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
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

# AP 生命周期安全护栏（v0.3.1.31，防 8-28 同类事故）
AP_TRANSITION_FILE = "/var/run/teslausb-ap-transition"   # R6: AP 起停进行中标记（Web/timer 并发互斥）
AP_TRANSITION_TTL = 120                                   # transition 标记最长有效秒数（覆盖 NM restart 60s+轮询 30s 慢路径）
AP_START_TIME_FILE = "/var/run/teslausb-ap-start-time"    # R5: AP 启动时间戳（宽限期防震荡）；内容 JSON {ts, manual}
AP_GRACE_PERIOD_SEC = 900                                 # R5: fallback 自动开启 AP 的宽限期 15min（防震荡）
AP_MANUAL_GRACE_PERIOD_SEC = 180                          # v0.3.1.36: 手动开启 AP 的宽限期 3min（用户主动开，无震荡风险）
AP_BACKOFF_FILE = "/var/run/teslausb-ap-backoff"          # 自愈退避分钟数（持久化，timer 新进程可见）
AP_LAST_TRY_FILE = "/var/run/teslausb-ap-last-try"        # 自愈上次探测时间戳
AP_HAD_CLIENTS_FILE = "/var/run/teslausb-ap-had-clients"  # v0.3.1.36: 上次探测时是否有客户端（断开事件检测）
AP_BACKOFF_INIT = 5                                       # 退避起点 5min（S7：2min 过激进，减少 NM restart 断网窗口）
AP_BACKOFF_MAX = 30                                       # 退避上限 30min
DNSMASQ_AP_CONF = "/etc/dnsmasq.d/ap.conf"                # M46: AP 的 dnsmasq 配置（条件启动保护）

# captive portal（v0.3.1.36）：AP 模式下 dnsmasq 提供 DNS（任意域名 → AP 网关）
# + iptables 将 80 端口重定向到 Web 5000，手机连 AP 开任意网页自动跳 A7Z 配置页。
# DNS 方案（8-30 实测优化）：**不触碰 systemd-resolved**（stop/start 有 D-Bus 竞态
# + Restart=always 触发 start-limit 限流），dnsmasq 用 8053 端口 + iptables
# 53→8053 REDIRECT（仅 wlan0 入站），与 resolved 的 127.0.0.53:53 完全隔离。
AP_WEB_PORT = 5000
AP_DNS_PORT = 8053                                        # dnsmasq DNS 端口（避开 53/5353）
# captive portal 规则集（list-of-lists，每条是完整 iptables 参数）：
AP_PORTAL_IPTABLES = [
    ["iptables", "-t", "nat", "-A", "PREROUTING", "-i", "wlan0", "-p", "tcp",
     "--dport", "80", "-j", "REDIRECT", "--to-ports", "5000"],
]
# DNS 转发规则：手机 → 42.1:53 → dnsmasq 8053（UDP + TCP）
AP_DNS_IPTABLES = [
    ["iptables", "-t", "nat", "-A", "PREROUTING", "-i", "wlan0", "-p", "udp",
     "--dport", "53", "-j", "REDIRECT", "--to-ports", str(AP_DNS_PORT)],
    ["iptables", "-t", "nat", "-A", "PREROUTING", "-i", "wlan0", "-p", "tcp",
     "--dport", "53", "-j", "REDIRECT", "--to-ports", str(AP_DNS_PORT)],
]


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
        # B2：密码留空 = 保持当前密码不变（前端"留空不修改"承诺），
        # 不得覆盖为空（空密码会在 bring_up 时回退默认 teslausb123，且 UI 警告失效）
        if passphrase:
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


# AP 静态 IP 与子网（与 ap_control.sh 保持一致）
AP_STATIC_IP = "192.168.42.1"


def _write_hostapd_conf() -> bool:
    """按当前 AP 配置重写 /etc/hostapd/hostapd.conf，返回是否成功。

    始终重写（而非仅在文件缺失时生成），避免旧配置残留旧 SSID/密码，
    导致用户改过的 AP 名称不生效。
    """
    config = get_ap_config()
    ssid = config.get("ssid", "TeslaUSB-Setup")
    passphrase = config.get("passphrase", "teslausb123")
    # hostapd 要求 WPA 密码 8-63 字符，过短会导致 hostapd 启动失败 → AP 无法广播
    if not passphrase or len(passphrase) < 8:
        passphrase = "teslausb123"

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
rsn_pairwise=CCMP
"""
    try:
        with open("/tmp/hostapd.conf.tmp", "w") as f:
            f.write(conf)
        # 确保目录存在后再拷贝（/etc/hostapd 可能不存在）
        subprocess.run(
            ["sudo", "-n", "mkdir", "-p", "/etc/hostapd"],
            capture_output=True, timeout=10,
        )
        r = subprocess.run(
            ["sudo", "-n", "cp", "/tmp/hostapd.conf.tmp", "/etc/hostapd/hostapd.conf"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.unlink("/tmp/hostapd.conf.tmp")
        except Exception:
            pass


# ── AP 生命周期安全护栏辅助（v0.3.1.31） ──

def _set_ap_transition():
    """标记 AP 起/停进行中（R6：Web 手动操作与 timer quick_check 的并发互斥）。

    quick_check 的 only_cleanup 自愈见到此标记（60s 内）即跳过，
    避免在 Web bring_up 的配置阶段（hostapd 仍 inactive）抢回 wlan0。
    """
    try:
        with open(AP_TRANSITION_FILE, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def _clear_ap_transition():
    try:
        if os.path.exists(AP_TRANSITION_FILE):
            os.remove(AP_TRANSITION_FILE)
    except Exception:
        pass


def _ap_transition_active() -> bool:
    """AP 起/停是否正在进行（60s 内有效，防陈旧标记长期阻塞自愈）"""
    try:
        if os.path.exists(AP_TRANSITION_FILE):
            ts = int(open(AP_TRANSITION_FILE).read().strip())
            return (time.time() - ts) < AP_TRANSITION_TTL
    except Exception:
        pass
    return False


def _record_ap_start_time(manual: bool = False):
    """记录 AP 启动时间戳（R5：宽限期防 fallback 震荡；v0.3.1.36：区分手动/自动）。"""
    try:
        with open(AP_START_TIME_FILE, "w") as f:
            json.dump({"ts": int(time.time()), "manual": bool(manual)}, f)
    except Exception:
        pass


def _ap_grace_period_elapsed() -> bool:
    """AP 启动是否已过宽限期。手动开启 3min / fallback 15min。无记录时视为已过。"""
    try:
        if os.path.exists(AP_START_TIME_FILE):
            with open(AP_START_TIME_FILE) as f:
                data = json.load(f)
            ts = int(data.get("ts", 0))
            manual = bool(data.get("manual", False))
            grace = AP_MANUAL_GRACE_PERIOD_SEC if manual else AP_GRACE_PERIOD_SEC
            return (time.time() - ts) >= grace
    except Exception:
        pass
    return True


def _read_had_clients() -> bool:
    """读取上次探测时的客户端状态（v0.3.1.36 断开事件检测）"""
    try:
        if os.path.exists(AP_HAD_CLIENTS_FILE):
            return open(AP_HAD_CLIENTS_FILE).read().strip() == "1"
    except Exception:
        pass
    return False


def _write_had_clients(has: bool):
    try:
        with open(AP_HAD_CLIENTS_FILE, "w") as f:
            f.write("1" if has else "0")
    except Exception:
        pass


def _ap_add_portal_iptables():
    """添加 captive portal 规则：80→5000（Web 跳转）+ 53→8053（DNS 转发）。幂等。"""
    try:
        for rule in AP_PORTAL_IPTABLES + AP_DNS_IPTABLES:
            # 先删再加（幂等）：[:3] 保留 -t nat，[4:] 跳过 -A
            subprocess.run(rule[:3] + ["-D"] + rule[4:],
                           capture_output=True, text=True, timeout=10)
        for rule in AP_PORTAL_IPTABLES + AP_DNS_IPTABLES:
            subprocess.run(rule, capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def _ap_del_portal_iptables():
    """删除 captive portal 规则（AP 关闭/回滚时调用）"""
    try:
        for rule in AP_PORTAL_IPTABLES + AP_DNS_IPTABLES:
            subprocess.run(rule[:3] + ["-D"] + rule[4:],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def _ap_ensure_resolved() -> bool:
    """保险：确保 systemd-resolved 正常运行（仅 failed 时修复，幂等）。

    v0.3.1.36 DNS 方案 dnsmasq 用 8053 端口 + iptables 53→8053 转发，
    与 resolved 的 127.0.0.53:53 完全隔离——开/关 AP 本不应影响 resolved。
    此函数仅清理**历史遗留**的 failed/start-limit 状态（8-30 调试期实测：
    resolved 单元 Restart=always + 反复 start 失败会触发 systemd 限流，该状态
    持久存在，只有 reset-failed 或成功 start 才能清除）。
    调用点：_ap_bring_up 开头（开 AP 前）+ _ap_bring_down 末尾（NM 稳定后）。
    """
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "systemd-resolved"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "active":
            return True  # 正常运行：零开销跳过
        subprocess.run(
            ["sudo", "-n", "systemctl", "reset-failed", "systemd-resolved"],
            capture_output=True, text=True, timeout=15,
        )
        for _i in range(3):
            r2 = subprocess.run(
                ["sudo", "-n", "systemctl", "start", "systemd-resolved"],
                capture_output=True, text=True, timeout=30,
            )
            if r2.returncode == 0:
                return True
            time.sleep(2)
        return False
    except Exception:
        return False


def _ap_probe_self() -> bool:
    """AP 启动自检：本机 curl AP 网关 Web 端口，确认 Web 监听可用。
    偶发不稳定窗口重试 3 次；全部失败仅告警（不阻塞 AP，captive portal
    全链路「域名→80→REDIRECT→5000」需真机手机验证，此处只探 Web 存活）。
    """
    last_err = ""
    for i in range(3):
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "3",
                 f"http://{AP_STATIC_IP}:{AP_WEB_PORT}/"],
                capture_output=True, text=True, timeout=6,
            )
            if r.stdout.strip() == "200":
                return True
            last_err = f"code={r.stdout.strip()!r} err={r.stderr.strip()[:80]}"
        except Exception as e:
            last_err = str(e)[:80]
        time.sleep(2)
    logging.getLogger("WifiSmartSwitch").warning(
        "AP 自检: %s:%d 未响应（%s）", AP_STATIC_IP, AP_WEB_PORT, last_err)
    return False


def _rollback_wlan0_station():
    """bring_up 失败时的回滚：恢复 wlan0 管控 + 触发重连（R1，8-28 事故同类防护）。

    bring_up 曾执行 nmcli managed no + stop wpa_supplicant，任何后续失败
    都必须恢复 wlan0 到 station 模式，否则设备彻底断网。
    v0.3.1.36：不重启 NetworkManager（8-30 实测 NM restart 打断 resolved 的
    D-Bus → resolved 崩溃 → start-limit 限流），改用 nmcli device connect 局部重连。
    """
    try:
        subprocess.run(
            ["sudo", "-n", "nmcli", "device", "set", "wlan0", "managed", "yes"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            ["sudo", "-n", "nmcli", "device", "connect", "wlan0"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass
    # v0.3.1.36：保险（仅 failed 时修复历史遗留 resolved 状态）+ 清理 portal 规则
    _ap_ensure_resolved()
    _ap_del_portal_iptables()


def _read_backoff() -> int:
    """读取自愈退避分钟数（持久化 /var/run，timer 每次新进程可见）"""
    try:
        if os.path.exists(AP_BACKOFF_FILE):
            return max(AP_BACKOFF_INIT, int(open(AP_BACKOFF_FILE).read().strip()))
    except Exception:
        pass
    return AP_BACKOFF_INIT


def _write_backoff(minutes: int):
    try:
        with open(AP_BACKOFF_FILE, "w") as f:
            f.write(str(min(max(minutes, AP_BACKOFF_INIT), AP_BACKOFF_MAX)))
    except Exception:
        pass


def _ap_last_try_elapsed(backoff_min: int) -> bool:
    """距上次自愈探测是否已过退避间隔"""
    try:
        if os.path.exists(AP_LAST_TRY_FILE):
            ts = int(open(AP_LAST_TRY_FILE).read().strip())
            return (time.time() - ts) >= backoff_min * 60
    except Exception:
        pass
    return True


def _record_ap_try():
    try:
        with open(AP_LAST_TRY_FILE, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def _ap_bring_up(manual: bool = False) -> Tuple[bool, str]:
    """完整启动 AP 热点（对齐 ap_control.sh，补齐原实现缺失的关键步骤）。

    关键：必须先释放 NetworkManager/wpa_supplicant 对 wlan0 的 station 管控，
    否则 hostapd 无法把 wlan0 切换到 AP(master) 模式，导致 AP 不广播、手机搜不到。

    安全护栏（v0.3.1.31，防 8-28 同类事故）：
    - R6: 全程持有 transition 标记，防止 timer quick_check 并发抢回 wlan0
    - Y2: 先校验 hostapd 单元存在且未 mask，避免无谓释放 wlan0
    - R1: 任何失败路径立即回滚（_rollback_wlan0_station），不留"无主"断网窗口
    - R2: _write_hostapd_conf 返回值检查（写失败即失败，不静默用旧配置）
    - R3: dnsmasq 失败即失败，verify 同时校验 hostapd + dnsmasq

    v0.3.1.36（manual 参数 + captive portal）：
    - manual=True：Web 手动开启（退避已由 start_ap 重置、宽限期 3min）
    - captive portal：停 systemd-resolved 释放 53 → dnsmasq 提供 DNS
      （任意域名 → AP 网关）+ iptables 80→5000，手机连 AP 开任意网页跳 A7Z Web
    """
    _set_ap_transition()
    try:
        # v0.3.1.36：开 AP 前确保 resolved 正常（dnsmasq 8053 与 resolved 隔离，
        # 仅修复历史遗留的 failed/start-limit 状态）
        _ap_ensure_resolved()
        # 0) 前置校验：hostapd 单元必须存在且未 mask（Y2）
        r_unit = subprocess.run(
            ["systemctl", "is-enabled", "hostapd"],
            capture_output=True, text=True, timeout=5,
        )
        if "masked" in r_unit.stdout or "masked" in (r_unit.stderr or ""):
            _clear_ap_transition()
            return False, "hostapd 单元被 mask，无法启动 AP"
        if r_unit.returncode != 0 and "not-found" in (r_unit.stderr or "").lower():
            _clear_ap_transition()
            return False, "hostapd 单元不存在，无法启动 AP"

        # 1) 释放 station 管控 + 停止 wpa_supplicant（避免与 hostapd 争抢 wlan0）
        r_nm = subprocess.run(
            ["sudo", "-n", "nmcli", "device", "set", "wlan0", "managed", "no"],
            capture_output=True, text=True, timeout=10,
        )
        if r_nm.returncode != 0:
            _clear_ap_transition()
            return False, f"释放 wlan0 管控失败: {(r_nm.stderr or '').strip()[:120]}"
        subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "wpa_supplicant"],
            capture_output=True, text=True, timeout=10,
        )

        # 2) 重写 hostapd.conf（始终按当前 AP 配置；失败立即回滚）
        if not _write_hostapd_conf():
            _rollback_wlan0_station()
            _clear_ap_transition()
            return False, "写 hostapd.conf 失败（已回滚 wlan0 管控）"

        # 3) 启动 hostapd（把 wlan0 置为 AP 模式并广播 SSID）
        r_hap = subprocess.run(
            ["sudo", "-n", "systemctl", "start", "hostapd"],
            capture_output=True, text=True, timeout=30,
        )
        time.sleep(2)

        # 4) 静态 IP（失败仅告警；dnsmasq 依赖此地址，若失败后续会暴露）
        subprocess.run(
            ["sudo", "-n", "ip", "addr", "add", f"{AP_STATIC_IP}/24", "dev", "wlan0"],
            capture_output=True, text=True, timeout=10,
        )

        # 5) dnsmasq DHCP + captive portal DNS（v0.3.1.36）
        #    DNS 方案：不触碰 systemd-resolved（stop/start 有 D-Bus 竞态 + Restart=always
        #    触发 start-limit 限流，8-30 实测），dnsmasq 用 port=8053 + iptables
        #    53→8053 REDIRECT（仅 wlan0 入站）→ 手机 DNS 查询转发到 dnsmasq。
        _ap_prefix = ".".join(AP_STATIC_IP.split(".")[:3])
        dnsmasq_conf = (
            f"port={AP_DNS_PORT}\n"                        # DNS 端口 8053（避开 53/5353）
            "bind-interfaces\n"                            # Y3：DNS/DHCP 仅绑 wlan0，隔离其他网口
            "interface=wlan0\n"
            f"dhcp-range={_ap_prefix}.10,{_ap_prefix}.100,12h\n"
            f"dhcp-option=3,{AP_STATIC_IP}\n"
            f"dhcp-option=6,{AP_STATIC_IP}\n"              # 手机 DNS = AP 网关（iptables 转 8053）
            f"address=/#/{AP_STATIC_IP}\n"                 # captive portal：任意域名 → AP 网关
        )
        try:
            with open("/tmp/ap-dnsmasq.conf.tmp", "w") as f:
                f.write(dnsmasq_conf)
            subprocess.run(
                ["sudo", "-n", "mkdir", "-p", "/etc/dnsmasq.d"],
                capture_output=True, timeout=10,
            )
            r_cp = subprocess.run(
                ["sudo", "-n", "cp", "/tmp/ap-dnsmasq.conf.tmp", DNSMASQ_AP_CONF],
                capture_output=True, timeout=10,
            )
            if r_cp.returncode != 0:
                # Y1：cp 失败（磁盘满/权限）→ 不能继续用旧/空配置启动 dnsmasq
                raise OSError(f"写入 {DNSMASQ_AP_CONF} 失败: rc={r_cp.returncode}")
        finally:
            try:
                os.unlink("/tmp/ap-dnsmasq.conf.tmp")
            except Exception:
                pass
        r_dns = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "dnsmasq"],
            capture_output=True, text=True, timeout=30,
        )

        # 6) 校验 hostapd + dnsmasq 是否真的起来（R3：dnsmasq 失败不再静默）
        verify_hap = subprocess.run(
            ["systemctl", "is-active", "hostapd"],
            capture_output=True, text=True, timeout=5,
        )
        hap_ok = verify_hap.stdout.strip() == "active"
        dns_ok = r_dns.returncode == 0
        if dns_ok:
            verify_dns = subprocess.run(
                ["systemctl", "is-active", "dnsmasq"],
                capture_output=True, text=True, timeout=5,
            )
            dns_ok = verify_dns.stdout.strip() == "active"

        if hap_ok and dns_ok:
            # captive portal 端口重定向（80→5000 + 53→8053）+ 启动自检（v0.3.1.36）
            _ap_add_portal_iptables()
            _ap_probe_self()
            _record_ap_start_time(manual=manual)  # R5: 记录启动时间，宽限期内不自动关闭
            _clear_ap_transition()
            return True, "AP 已启动"
        # 任一失败 → 回滚（R1：不留无主断网窗口）
        _ap_del_portal_iptables()    # v0.3.1.36: 清理 captive portal 规则
        _rollback_wlan0_station()
        _clear_ap_transition()
        if not hap_ok:
            return False, f"hostapd 未 active: {(r_hap.stderr or '').strip()[:200]}"
        return False, f"dnsmasq 未 active: {(r_dns.stderr or '').strip()[:200]}"
    except Exception as e:
        _ap_del_portal_iptables()
        _rollback_wlan0_station()
        _clear_ap_transition()
        return False, f"启动 AP 失败: {e}"


def _ap_bring_down() -> Tuple[bool, str]:
    """完整关闭 AP 并恢复 wlan0 的 station 模式（对齐 ap_control.sh stop_ap）。

    修复（2026-08-28）：原实现只 restart NetworkManager，未显式恢复 wlan0 的
    NM 管控（bring_up 曾 nmcli managed no 释放）、未校验重启结果、未确认重连，
    曾导致关热点后设备不自动重连 WiFi 而离线。现补齐对称恢复 + 校验 + 轮询 + 兜底。

    v0.3.1.31 增强：R6 transition 标记防并发；Y1 轮询追加 IP 确认（L2 connected
    但 DHCP 未完成不再误报"已重连"）。
    """
    _set_ap_transition()
    try:
        # 1) 停 hostapd / dnsmasq
        subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "hostapd"],
            capture_output=True, text=True, timeout=30,
        )
        subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "dnsmasq"],
            capture_output=True, text=True, timeout=30,
        )
        # 1.5) v0.3.1.36：清理 captive portal 规则 + 重置客户端状态。
        #      （DNS 方案不触碰 systemd-resolved——dnsmasq 8053 + iptables 53→8053，
        #        无需 stop/start resolved，8-30 实测消除 D-Bus 竞态与 start-limit）
        _ap_del_portal_iptables()
        _write_had_clients(False)  # AP 关闭 → 重置客户端状态（断开事件检测基准）
        # 2) 清理 AP 静态 IP（不存在时 ip addr del 会返回非 0，忽略即可）
        subprocess.run(
            ["sudo", "-n", "ip", "addr", "del", f"{AP_STATIC_IP}/24", "dev", "wlan0"],
            capture_output=True, text=True, timeout=10,
        )
        # 3) 删除 AP 的 dnsmasq 配置：避免下次开机 dnsmasq（系统 enabled）自动带起
        #    在 wlan0 上的 DHCP 服务，干扰正常 station 模式的网络。
        subprocess.run(
            ["sudo", "-n", "rm", "-f", DNSMASQ_AP_CONF],
            capture_output=True, text=True, timeout=10,
        )
        # 4) 显式恢复 wlan0 管控（bring_up 曾 nmcli managed no 释放；对称恢复）
        subprocess.run(
            ["sudo", "-n", "nmcli", "device", "set", "wlan0", "managed", "yes"],
            capture_output=True, text=True, timeout=10,
        )
        # 5) v0.3.1.36：不重启 NetworkManager（8-30 实测 NM restart 会打断
        #    systemd-resolved 的 D-Bus 连接 → resolved 崩溃 → Restart=always 风暴
        #    → start-limit 限流 → 设备本机 DNS 损坏）。改用 nmcli device connect
        #    局部触发重连（NM 进程不重启，D-Bus 保持）。
        subprocess.run(
            ["sudo", "-n", "nmcli", "device", "connect", "wlan0"],
            capture_output=True, text=True, timeout=30,
        )
        time.sleep(3)

        # 6) 轮询等待 wlan0 恢复连接 + 获取 IP（最多 30s；Y1：L2 connected + DHCP 完成才算重连）
        for _i in range(15):
            time.sleep(2)
            r = subprocess.run(
                ["nmcli", "-t", "-f", "DEVICE,STATE", "dev", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if "wlan0:connected" in r.stdout:
                r_ip = subprocess.run(
                    ["ip", "-4", "addr", "show", "dev", "wlan0"],
                    capture_output=True, text=True, timeout=5,
                )
                if "inet " in r_ip.stdout:
                    _ap_ensure_resolved()  # v0.3.1.36: 保险（仅 failed 时修复历史遗留）
                    _clear_ap_transition()
                    return True, "AP 已停止，wlan0 已重连并获取 IP"

        # 7) 兜底：显式激活 wlan0（NM 自动连接未触发时拉起已保存连接）
        subprocess.run(
            ["sudo", "-n", "nmcli", "device", "connect", "wlan0"],
            capture_output=True, text=True, timeout=30,
        )
        time.sleep(3)
        r2 = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,STATE", "dev", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if "wlan0:connected" in r2.stdout:
            r_ip2 = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", "wlan0"],
                capture_output=True, text=True, timeout=5,
            )
            if "inet " in r_ip2.stdout:
                _clear_ap_transition()
                return True, "AP 已停止，wlan0 已重连（兜底拉起）"
            _clear_ap_transition()
            return False, "AP 已停止但 wlan0 无 IP（L2 已连、DHCP 未完成，请检查网络）"
        _clear_ap_transition()
        return False, "AP 已停止但 wlan0 未能重连（NM 重启 + 兜底拉起均失败，需人工检查网络）"
    except Exception as e:
        _clear_ap_transition()
        return False, f"停止 AP 失败: {e}"


def _ap_ensure_down(only_cleanup: bool = False) -> Tuple[bool, str]:
    """确保 AP 处于关闭状态（幂等自愈，业务闭环的关键兜底）。

    两种模式：
    - only_cleanup=False（默认）：hostapd 运行中 → 完整 bring-down（停 hostapd/dnsmasq、
      删 ap.conf、恢复 station 管控）；仅配置残留 → 清理。
    - only_cleanup=True：只清理「hostapd 未运行但 ap.conf 残留」的脏状态，
      不触碰运行中的 AP（运行中 AP 由网络正常分支负责关闭），
      用于开机/每次检测前的低风险自愈，避免强制关 AP 造成 fallback 震荡。
    """
    try:
        # R6：AP 起/停进行中（Web 手动操作）→ 跳过，避免并发干扰
        if _ap_transition_active():
            return True, "AP 起停进行中，跳过自愈"

        r = subprocess.run(
            ["systemctl", "is-active", "hostapd"],
            capture_output=True, text=True, timeout=5,
        )
        # activating（启动中）也视为运行中：only_cleanup 时跳过，避免打断并发 bring_up
        hostapd_state = r.stdout.strip()
        if hostapd_state in ("active", "activating"):
            if only_cleanup:
                return True, "AP 运行中/启动中，跳过（由网络正常分支负责关闭）"
            return _ap_bring_down()

        # hostapd 未运行：清理残留配置，防止 dnsmasq 以 AP 配置在
        # station 模式下干扰 DHCP / 开机自动带起
        cleaned = False
        if os.path.exists(DNSMASQ_AP_CONF):
            r_rm = subprocess.run(
                ["sudo", "-n", "rm", "-f", DNSMASQ_AP_CONF],
                capture_output=True, text=True, timeout=10,
            )
            if r_rm.returncode != 0:
                return False, f"删除 {DNSMASQ_AP_CONF} 失败: {(r_rm.stderr or '').strip()[:120]}"
            cleaned = True
        if cleaned:
            r_dns = subprocess.run(
                ["sudo", "-n", "systemctl", "stop", "dnsmasq"],
                capture_output=True, text=True, timeout=30,
            )
            if r_dns.returncode != 0:
                return False, f"停止 dnsmasq 失败: {(r_dns.stderr or '').strip()[:120]}"
            # Y3: 清理 bring_up 残留的 AP 静态 IP（存在才删；不存在时 ip addr del 返回非 0，忽略）
            subprocess.run(
                ["sudo", "-n", "ip", "addr", "del", f"{AP_STATIC_IP}/24", "dev", "wlan0"],
                capture_output=True, text=True, timeout=10,
            )
        # v0.3.1.36：AP 异常退出残留场景 → 恢复 resolved + 清理 captive portal 规则（幂等）
        _ap_del_portal_iptables()
        _write_had_clients(False)
        # 恢复 wlan0 管控（_ap_bring_up 曾 nmcli managed no 释放；幂等，NM 会重新接管）。
        # 无残留时也执行：覆盖 bring_up 中途崩溃导致 wlan0 保持 unmanaged 的残留态。
        r_nm = subprocess.run(
            ["sudo", "-n", "nmcli", "device", "set", "wlan0", "managed", "yes"],
            capture_output=True, text=True, timeout=10,
        )
        if r_nm.returncode != 0:
            return False, f"恢复 wlan0 管控失败: {(r_nm.stderr or '').strip()[:120]}"
        return True, "AP 残留已清理" if cleaned else "AP 已处于关闭状态"
    except Exception as e:
        return False, f"清理 AP 残留失败: {e}"


def _ap_has_clients() -> bool:
    """AP 是否有手机客户端连接（iw station dump；需 root）。

    客户端感知：有手机连着 AP 时不得打断（用户可能正在配置）。
    安全语义（v0.3.1.36 B1 修复）：任何查询失败（sudo 无免密/iw 瞬时错误/
    接口抖动）一律保守返回 True（视为有客户端），绝不误判"无客户端"——
    否则配合断开事件检测会把正在配置的手机踢断。
    """
    try:
        r = subprocess.run(
            ["sudo", "-n", "iw", "dev", WIFI_INTERFACE, "station", "dump"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return True  # B1：查询失败（sudo 无免密等）→ 保守视为有客户端，不打断
        # 输出含 "Station <mac>" 行即表示有已关联客户端
        return "Station " in r.stdout
    except Exception:
        return True  # 查询失败时保守处理：视为有客户端，不打断


def start_ap() -> dict:
    """手动启动 AP（完整 bring-up）。

    v0.3.1.36（2B）：手动开启 = 用户主动操作（无 fallback 震荡风险）→
    重置自愈退避到初始值 + 宽限期缩短（manual 标记，_ap_grace_period_elapsed 读取）。
    否则设备在断网 fallback 期间退避可能已积累到 30min，手动开 AP 后
    手机断开也要等满退避 → "不切回 WiFi"。
    """
    _write_backoff(AP_BACKOFF_INIT)   # 2B：手动开 AP 退避归 5min
    _write_had_clients(False)         # 重置客户端状态（断开事件检测基准）
    try:
        if os.path.exists(AP_LAST_TRY_FILE):
            os.remove(AP_LAST_TRY_FILE)  # 🟢2：清上次探测时间戳，避免宽限期后仍等旧退避
    except Exception:
        pass
    ok, msg = _ap_bring_up(manual=True)
    return {"success": ok, "message": msg}


def stop_ap() -> dict:
    """手动停止 AP（完整 bring-down + 恢复 station）"""
    ok, msg = _ap_bring_down()
    return {"success": ok, "message": msg}


# ═══════════════════════════════════════════════════════════════
# WifiSmartSwitch — 自动 WiFi 智能切换（从 wifi_smart_switch.sh 迁移）
# ═══════════════════════════════════════════════════════════════

SMART_SWITCH_LOCK_FILE = "/var/run/wifi-smart-switch.lock"
SMART_SWITCH_STATE_FILE = "/var/run/wifi-smart-switch.state"
SMART_SWITCH_FAILURE_COUNT_FILE = "/var/run/wifi-failure-count"
SMART_SWITCH_LOG_FILE = "/var/log/wifi-smart-switch.log"
LOCATION_STATE_FILE = "/opt/radxa_data/teslausb/data/location_state.json"  # M69: web 位置判定落盘
SENTRY_CONFIG_FILE_WIFI = "/opt/radxa_data/teslausb/config/sentry.json"
LOCATION_STATE_MAX_AGE_SEC = 900  # 位置状态超过 15 分钟视为失效（按外出处理）
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
CONNECTIVITY_TARGETS = ["12.127.12.8", "12.127.12.245", "baidu.com"]
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
        self.priority = self._load_nm_priorities() or _load_priority_config()
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

    def _load_nm_priorities(self) -> dict:
        """读取 NM 各连接的 autoconnect-priority（/wifi 页面写入的权威值），
        按 SSID 返回。非 WiFi 连接（SSID 为 "--"）跳过。
        失败返回空 dict（调用方回退 wifi_priority.json / 默认表）。"""
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,AUTOCONNECT-PRIORITY", "connection", "show"],
                capture_output=True, text=True, timeout=8,
            )
            if r.returncode != 0:
                return {}
            result: dict = {}
            for line in r.stdout.splitlines():
                if ":" not in line:
                    continue
                name, _, prio = line.rpartition(":")
                if not name or not prio.strip().lstrip("-").isdigit():
                    continue
                r2 = subprocess.run(
                    ["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", name],
                    capture_output=True, text=True, timeout=5,
                )
                ssid = r2.stdout.strip().replace("\\:", ":") if r2.returncode == 0 else ""
                if not ssid or ssid == "--":
                    continue
                # 同一 SSID 多 profile（如 "C12345" 与 "C12345 2"）→ 取最高优先级
                result[ssid] = max(result.get(ssid, 0), int(prio))
            return result
        except Exception:
            return {}

    def get_priority(self, ssid: str) -> int:
        """获取指定 SSID 的优先级（未配置的返回 0）"""
        return self.priority.get(ssid, 0)

    def reload_priority(self):
        """重新加载优先级配置（供 Web UI 修改后调用）"""
        self.priority = self._load_nm_priorities() or _load_priority_config()

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
                        con_name = line.split(":")[2].strip()
                        # M69: NM 连接名 ≠ SSID（如 profile "WiFi-YL-MIFI-000500" 的 SSID 是
                        # "YL-MIFI-000500"），不解析会导致同网跳过失效、优先级比较出 0 vs 150 幻觉，
                        # 每 10 分钟对同一 SSID 断开重连（单日 31 次断连的根因）
                        try:
                            r_ssid = subprocess.run(
                                ["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", con_name],
                                capture_output=True, text=True, timeout=5,
                            )
                            if r_ssid.returncode == 0:
                                ssid = r_ssid.stdout.strip().replace("\\:", ":")
                                if ssid and ssid != "--":
                                    return ssid
                        except Exception:
                            pass
                        return con_name  # 解析失败退回连接名（旧行为）

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

    def _wifi_associated(self) -> bool:
        """检查 wlan0 是否已关联并拿到 IPv4 地址。

        用于避免 Tailscale(tailscale0)/以太网等其它接口仍可通时，误判"WiFi 正常"，
        导致断网后既不重连也不开启 AP。探测失败时返回 True，回退到纯 ping 判定，避免误伤。
        """
        try:
            r = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", "wlan0"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            return re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", r.stdout) is not None
        except Exception:
            return True

    def _check_connectivity(self) -> bool:
        """并行 ping 3 个目标，任一成功即视为网络正常"""
        from subprocess import Popen, DEVNULL

        # WiFi 关联性前置检查：wlan0 无 IP 时直接判为断网（见 _wifi_associated 说明）
        if not self._wifi_associated():
            return False

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

        # 🔧 检查是否已连接目标 SSID，避免不必要的断连
        # 原代码误用未定义的 get_current_wifi_ssid()，会抛 NameError 导致整个
        # 重连链路崩溃（这正是"断网后无法自动重连"的直接根因）。
        current_ssid = self._get_current_ssid()
        if current_ssid and current_ssid == ssid:
            # 已关联到目标 SSID，但若连通性仍异常（典型场景：车机热点网段变更后，
            # wlan0 仍在 L2 关联，却持有旧子网的 IP/默认路由，L3 不通），
            # 直接跳过会导致 DHCP 租约与默认路由永不刷新 → 永远无法真正恢复联网。
            # 此时必须强制重连以刷新 DHCP，不能跳过。
            if self._check_connectivity():
                self.log.info("已在目标网络 %s 且连通正常，跳过切换", ssid)
                self._save_switch_time()
                return True
            self.log.info("已关联 %s 但连通异常（疑似网段变更/旧租约），强制重连刷新 DHCP", ssid)

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

        # AP 残留自愈：只清理「hostapd 未运行 + ap.conf 残留」的脏状态，
        # 不强制关闭运行中的 AP（运行中 AP 由网络正常分支负责关闭，避免 fallback 震荡）
        try:
            if get_ap_force_mode() != "force-on":
                ok, msg = _ap_ensure_down(only_cleanup=True)
                if not ok:
                    self.log.warning("AP 残留自愈失败: %s", msg)
        except Exception:
            pass

        # v0.3.1.31 AP 自愈：AP 运行中按退避周期探测可回连网络
        # （客户端感知：有手机连接时不打断；扫描驱动：确认已知 WiFi 才让出）
        try:
            self._ap_self_heal()
        except Exception:
            pass

        if self._check_connectivity():
            # 网络正常，重置失败计数
            try:
                if os.path.exists(SMART_SWITCH_FAILURE_COUNT_FILE):
                    os.remove(SMART_SWITCH_FAILURE_COUNT_FILE)
            except Exception:
                pass
            self.log.info("网络正常")

            # 确保 AP 处于关闭状态（幂等自愈：hostapd 运行中则完整关闭，
            # 仅配置残留则清理 ap.conf + 停 dnsmasq，防止干扰 station 模式）
            try:
                force_mode = get_ap_force_mode()
                if force_mode != "force-on":
                    ok, msg = _ap_ensure_down()
                    if not ok:
                        self.log.warning("AP 关闭/清理失败: %s", msg)
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

        # v0.3.1.31 AP 模式降频：hostapd 运行中且距启动 <15min → 跳过本轮
        # 全量探测（减少子进程风暴，8-28 僵死叠加因素）。启动 >15min 后允许
        # 周期重试，网络恢复则回连退出 AP，由 quick_check 正常分支负责关闭。
        try:
            r_hap = subprocess.run(
                ["systemctl", "is-active", "hostapd"],
                capture_output=True, text=True, timeout=5,
            )
            if r_hap.stdout.strip() == "active" and not _ap_grace_period_elapsed():
                self.log.info("AP 运行中且距启动 <15min（降频窗口），跳过本轮全量探测")
                return
        except Exception:
            pass

        cur_ssid = self._get_current_ssid()
        is_connected = self._check_connectivity()
        cur_priority = self.get_priority(cur_ssid) if cur_ssid else 0

        # M69: 位置联动——在家时只在家庭白名单内选网（按页面优先级排序），
        # 防止无外网的车载热点在家抢占连接、导致推送反复失败
        loc_state, loc_age = self._read_location_state()
        at_home = loc_state == "home" and loc_age < LOCATION_STATE_MAX_AGE_SEC
        home_allowed = self._get_home_allowed() if at_home else set()
        if at_home:
            self.log.info("位置判定: 在家（%d 秒前），家庭白名单: %s", loc_age, sorted(home_allowed))

        if is_connected:
            self.log.info("当前连接: %s (优先级: %d)", cur_ssid or "未知", cur_priority)

            # 扫描可用 WiFi
            available = self._scan_available()
            for net in available:
                if at_home and net["ssid"] not in home_allowed:
                    self.log.info("在家，跳过非家庭网络: %s", net["ssid"])
                    continue
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

            if at_home and cur_ssid not in home_allowed:
                # 在家但连着非白名单网络 → 切到白名单内最优可见网络。
                # 白名单身份优先于页面优先级数值（YL=600 > C12345=500，
                # 但在家时 YL 无外网必须让位）；页面优先级仅用于白名单内部排序。
                allowed_nets = [n for n in available
                                if n["ssid"] in home_allowed and n["signal"] >= SIGNAL_THRESHOLD_DBM]
                if allowed_nets:
                    best = allowed_nets[0]  # _scan_available 已按 (-优先级, -信号) 排序
                    self.log.info("在家纠正: 非白名单 %s → 白名单最优 %s (优先级 %d)",
                                  cur_ssid, best["ssid"], best["priority"])
                    if self._switch_to(best["ssid"]):
                        return
                else:
                    self.log.info("在家但白名单网络均不可见，保持当前连接: %s（下轮再试）", cur_ssid)
            else:
                self.log.info("未找到更优网络")
                self.log.info("未找到更优网络")

        else:
            self.log.info("网络连接异常，尝试重连...")

            available = self._scan_available()
            reconnected = False

            # M69: 在家时重连只尝试白名单网络
            if at_home:
                allowed = [n for n in available if n["ssid"] in home_allowed]
                if allowed:
                    available = allowed
                else:
                    self.log.info("在家但白名单网络不可见，保持完整重连流程")
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

    def _read_location_state(self):
        """读取 web 位置判定落盘（location_detector 每 30s 刷新）。
        返回 (state, age_seconds)；文件缺失/损坏返回 ("unknown", 极大值)。"""
        try:
            with open(LOCATION_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("state", "unknown")), time.time() - int(data.get("ts", 0))
        except Exception:
            return "unknown", float("inf")

    def _get_home_allowed(self) -> set:
        """家庭白名单 = sentry.json home_wifi_ssids（含 189-AP），兜底 C12345"""
        try:
            with open(SENTRY_CONFIG_FILE_WIFI, "r", encoding="utf-8") as f:
                ssids = json.load(f).get("home_wifi_ssids", []) or []
            return {str(x) for x in ssids} or {"C12345"}
        except Exception:
            return {"C12345"}

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

    def _wait_scan_results(self, max_wait: int = 30, interval: int = 3) -> set:
        """轮询 nmcli wifi list 直到出现非空结果（v0.3.1.39 AP 退出修复）。

        AIC8800 从 AP 模式切回 station 后扫描需 10-30s，固定 sleep(5) 常拿到空
        结果 → 误判"无可回连网络"。轮询等待：每 interval 秒查一次，直到非空
        或超时。返回扫描到的 SSID 集合（可能为空=超时仍未扫到）。

        注意：-f SSID 为单列输出，行内容即 SSID（nmcli -t 将 SSID 内特殊字符
        转义，冒号显示为 \\:，需还原）；不做 rsplit 切分（含冒号 SSID 会被截断）。
        子进程 timeout 可能使单窗口略超 max_wait（约 +10s），无碍（多等即多等）。

        Args:
            max_wait: 最长等待秒数
            interval: 轮询间隔秒数
        """
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
                    capture_output=True, text=True, timeout=10,
                )
                scanned = set()
                for line in r.stdout.splitlines():
                    ssid = line.strip().replace("\\:", ":")
                    if ssid:
                        scanned.add(ssid)
                if scanned:
                    return scanned
            except Exception:
                pass
            time.sleep(interval)
        return set()

    def _start_ap_fallback(self) -> None:
        """当所有 WiFi 不可用时自动启用 AP 热点（复用完整 bring-up 流程）"""
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

            self.log.info("正在启动 AP 热点...")
            ok, msg = _ap_bring_up()
            if ok:
                self.log.info("AP 热点已启动 (SSID: %s)", get_ap_config().get("ssid", "TeslaUSB-Setup"))
            else:
                self.log.error("AP 启动失败: %s", msg)
        except Exception as e:
            self.log.error("启动 AP 失败: %s", e)

    def _ap_self_heal(self) -> None:
        """AP 运行中的自愈探测（v0.3.1.31，解决「AP 开了出不来」的核心痛点）。

        流程（客户端感知 + 扫描驱动探测 + 指数退避）：
          1. force-on / hostapd 未运行 / 起停进行中 / 退避期内 → 跳过
          2. 客户端感知：有手机连着 AP → 跳过（配置中的用户不被踢断）
          3. 让出探测：完整 bring_down（恢复 wlan0）→ nmcli rescan（5s）→
             「已知 WiFi（NM 已保存）∩ 扫描结果」→ 有才尝试连接
               - 连上 → AP 保持关闭 ✅（重置退避）
               - 无已知 WiFi / 连接失败 → 立即重启 AP + 退避加倍（2→30min cap）
        """
        try:
            if get_ap_force_mode() == "force-on":
                return
            r = subprocess.run(
                ["systemctl", "is-active", "hostapd"],
                capture_output=True, text=True, timeout=5,
            )
            if r.stdout.strip() != "active":
                return
            # R6：AP 起停进行中（可能 Web 正在操作）→ 跳过
            if _ap_transition_active():
                return
            # ── 客户端感知（v0.3.1.36 前置：断开事件优先处理）──
            has_clients = _ap_has_clients()
            client_just_left = _read_had_clients() and not has_clients
            _write_had_clients(has_clients)
            if has_clients:
                # 有手机连着 AP → 不打断配置（2C：不记录 try，避免"断开后仍等满退避"）
                self.log.info("AP 有客户端连接，跳过自愈探测（不打断配置）")
                return
            if client_just_left:
                # 2A：客户端「有→无」断开事件 = 用户配置完成/放弃 → 立即让出回连
                # （跳过宽限期/退避/冷却：bring_down 已含 NM restart+轮询确认 wlan0
                #   connected，依赖 NM 自动连接已保存 WiFi，不调 _switch_to 免冷却干扰）
                self.log.info("AP 客户端已断开，立即让出探测回连 WiFi...")
                ok, msg = _ap_bring_down()
                if ok:
                    _write_backoff(AP_BACKOFF_INIT)   # 成功回连：退避重置
                    _record_ap_try()
                    self.log.info("AP 自愈: 客户端断开后已回连，AP 保持关闭")
                else:
                    # 让出失败（wlan0 未恢复）→ 立即重启 AP 兜底，避免断网
                    # Y2：兜底重启带 manual=True（保持手动语义，宽限期 3min 而非 15min，
                    #     避免问题 2"断开不切回"复发窗口）
                    self.log.warning("AP 自愈: 断开后 bring_down 失败(%s)，重启 AP 兜底", msg)
                    _ap_bring_up(manual=True)
                    _write_backoff(min(_read_backoff() * 2, AP_BACKOFF_MAX))
                    _record_ap_try()
                return
            # ── 常规路径（无客户端、非断开事件）：宽限期 → 冷却 → 退避 ──
            # R5/B1：AP 启动宽限期内不自愈让出（手动 3min / fallback 15min）
            if not _ap_grace_period_elapsed():
                return
            # S2：切换冷却期内不让出（避免 _switch_to 被冷却挡住 → 误判失败重启 AP）
            if not self._can_switch():
                return
            # 退避检查：距上次探测不足退避间隔 → 跳过（指数退避 5→30min）
            backoff = _read_backoff()
            if not _ap_last_try_elapsed(backoff):
                return
            # 让出探测：完整关闭 AP（含恢复 wlan0 station 管控）
            self.log.info("AP 无客户端，让出探测已知 WiFi...")
            ok, msg = _ap_bring_down()
            if not ok:
                # bring_down 失败（wlan0 未恢复）→ 立即重启 AP 兜底，避免断网
                self.log.warning("AP 自愈: bring_down 失败(%s)，立即重启 AP 兜底", msg)
                _ap_bring_up()
                _write_backoff(min(backoff * 2, AP_BACKOFF_MAX))
                _record_ap_try()
                return
            # 主动扫描（v0.3.1.39 FIX：AIC8800 从 AP 切回 station 后扫描需 10-30s，
            # 原固定 rescan + sleep(5) 常扫不到已保存 WiFi → 误判"无可回连网络"
            # → 反复重启 AP + 退避加倍（手机断开不切回根因，8-31 日志
            # 12:44:24→35 实锤 11 秒误判）。改为轮询式等待 + 空结果重试。
            # 注：nmcli wifi list 可能先返回切模式前的旧扫描缓存（非空）→ 轮询
            # 提前返回旧 SSID。旧 SSID 是真实存在过的网络，_switch_to 会实际连接
            # 验证，连不上则走下方"无可回连"兜底，行为正确（最多多一次失败尝试）。）
            subprocess.run(
                ["nmcli", "dev", "wifi", "rescan"],
                capture_output=True, text=True, timeout=15,
            )
            time.sleep(3)
            scanned = self._wait_scan_results(max_wait=30, interval=3)
            if not scanned:
                # 空结果重试一次（再 rescan + 再轮询，最长约 +30s）
                self.log.info("AP 自愈: 首次扫描为空（可能仍在切模式），重试 rescan...")
                subprocess.run(
                    ["nmcli", "dev", "wifi", "rescan"],
                    capture_output=True, text=True, timeout=15,
                )
                time.sleep(3)
                scanned = self._wait_scan_results(max_wait=30, interval=3)
            # S1：全量扫描结果 ∩ NM 已保存连接（含未配置优先级的已保存 WiFi，
            # 避免 _scan_available 只返回优先级网络 → 误判"无可回连网络"）
            saved = set(self._get_saved_connections())
            known = [ssid for ssid in saved if ssid in scanned]
            if known:
                self.log.info("AP 自愈: 发现已知 WiFi %s，尝试回连", known)
                for ssid in known:
                    if self._switch_to(ssid):
                        _write_backoff(AP_BACKOFF_INIT)  # 成功：重置退避
                        _record_ap_try()
                        self.log.info("AP 自愈: 已回连 %s，AP 保持关闭", ssid)
                        return
            # 无已知 WiFi 或连接失败 → 立即重启 AP（不等 NM 空等 30s），退避加倍
            # v0.3.1.39：日志记录扫描结果数，便于区分「真无可回连」vs「扫描未完成」
            self.log.info("AP 自愈: 无可回连网络（已保存 %d, 扫描到 %d 个 SSID），重启 AP + 退避加倍(%d→%dmin)",
                          len(saved), len(scanned), backoff, min(backoff * 2, AP_BACKOFF_MAX))
            _ap_bring_up()
            _write_backoff(min(backoff * 2, AP_BACKOFF_MAX))
            _record_ap_try()
        except Exception as e:
            self.log.warning("AP 自愈异常: %s", e)
            try:
                _record_ap_try()
                _write_backoff(min(_read_backoff() * 2, AP_BACKOFF_MAX))
            except Exception:
                pass

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
# WiFi 智能切换 timer 自愈（v0.3.1.27）
# ─────────────────────────────────────────────
# 背景：wifi-quick-check.timer / wifi-full-check.timer 若被禁用或文件丢失，
# 自动重连与 AP 兜底将完全失效（设备曾因此自 2026-07-25 起智能切换从未运行，
# 断网 210 分钟既未切到家里 WiFi 也未开 AP）。
TIMER_SRC_DIR = "/opt/radxa_data/teslausb/services"
TIMER_DST_DIR = "/etc/systemd/system"


def _sudo(cmd: list) -> subprocess.CompletedProcess:
    """统一提权执行（与模块内其它命令一致：sudo -n；root 下等效直接执行）"""
    return subprocess.run(
        ["sudo", "-n"] + cmd,
        capture_output=True, text=True, timeout=30,
    )


def ensure_smart_switch_timers() -> dict:
    """确保 WiFi 智能切换 systemd timer 已部署并启用（幂等自愈）。

    步骤：
      0. 同步 .service 单元（timer 的触发目标；仅复制，不 enable，oneshot 无需 enable）
      1. 从应用目录 services/ 复制 timer 到 /etc/systemd/system/（缺失或内容不一致时）
      2. daemon-reload
      3. enable（未启用时）并 start（未激活时，立即触发一次检查）
    返回 {timer_name: {enabled, started, action}}，任何异常不向上抛出。
    """
    results = {}
    for timer_name in ("wifi-quick-check", "wifi-full-check"):
        entry = {"enabled": False, "started": False, "action": "none"}
        _unit_changed = False
        try:
            # 0) 同步 .service 单元：若缺失或内容不一致则复制（否则 timer 触发会失败）
            svc_src = os.path.join(TIMER_SRC_DIR, f"{timer_name}.service")
            svc_dst = os.path.join(TIMER_DST_DIR, f"{timer_name}.service")
            if os.path.exists(svc_src):
                svc_diff = True
                if os.path.exists(svc_dst):
                    try:
                        with open(svc_src, "rb") as f1, open(svc_dst, "rb") as f2:
                            svc_diff = f1.read() != f2.read()
                    except Exception:
                        svc_diff = True
                if svc_diff:
                    _sudo(["cp", svc_src, svc_dst])
                    entry["action"] = "svc-copied"
                    _unit_changed = True

            src = os.path.join(TIMER_SRC_DIR, f"{timer_name}.timer")
            dst = os.path.join(TIMER_DST_DIR, f"{timer_name}.timer")

            # 1) 源文件不存在则跳过（应用目录未部署，等升级包带上）
            if not os.path.exists(src):
                entry["action"] = "src-missing"
                results[timer_name] = entry
                continue

            # 2) 目标缺失或内容不一致 → 复制
            need_copy = False
            if not os.path.exists(dst):
                need_copy = True
            else:
                try:
                    with open(src, "rb") as f1, open(dst, "rb") as f2:
                        need_copy = f1.read() != f2.read()
                except Exception:
                    need_copy = True
            if need_copy:
                _sudo(["cp", src, dst])
                entry["action"] = "copied"
                _unit_changed = True

            # 3) 仅当单元文件有变化才重载 systemd 定义（避免每次无谓 daemon-reload）
            if _unit_changed:
                _sudo(["systemctl", "daemon-reload"])

            # 4) 启用（is-enabled 为 disabled/static 时执行 enable，并校验返回码）
            r = subprocess.run(
                ["systemctl", "is-enabled", f"{timer_name}.timer"],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip() != "enabled":
                r_en = _sudo(["systemctl", "enable", f"{timer_name}.timer"])
                if r_en.returncode != 0:
                    entry["action"] = f"enable-failed: {(r_en.stderr or '').strip()[:100]}"
                    results[timer_name] = entry
                    continue
                entry["action"] = entry["action"] if entry["action"] != "none" else "enabled"
            entry["enabled"] = True

            # 5) 启动（未激活时 start，立即触发一次检查；校验返回码）
            r = subprocess.run(
                ["systemctl", "is-active", f"{timer_name}.timer"],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip() != "active":
                r_st = _sudo(["systemctl", "start", f"{timer_name}.timer"])
                if r_st.returncode != 0:
                    entry["action"] = f"start-failed: {(r_st.stderr or '').strip()[:100]}"
                    results[timer_name] = entry
                    continue
                entry["action"] = "started"
            entry["started"] = True

        except Exception as e:
            entry["action"] = f"error: {e}"
        results[timer_name] = entry
    return results


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
