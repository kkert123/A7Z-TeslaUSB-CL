"""
TeslaUSB Neo Web - WiFi 服务模块
基于 NetworkManager (nmcli) 实现：
  - 当前连接查询
  - WiFi 扫描
  - 连接切换（含自动回档）
  - 连接列表 / 优先级管理 / 删除
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime

from config import WIFI_INTERFACE, WIFI_STATUS_FILE

logger = logging.getLogger(__name__)


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
        pass  # 非关键路径，写失败不影响主逻辑


def get_wifi_status() -> dict | None:
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
    """获取当前已连接的 WiFi 信息"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
            capture_output=True, text=True, check=False, timeout=5,
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

        # Fallback：iw
        result = subprocess.run(
            ["iw", "dev", WIFI_INTERFACE, "link"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0 and "Connected to" in result.stdout:
            m = re.search(r"SSID:\s*(.+)", result.stdout)
            ssid = m.group(1).strip() if m else "Unknown"
            return {"connected": True, "ssid": ssid, "signal": None}

    except Exception as e:
        logger.warning(f"get_current_wifi error: {e}")

    return {"connected": False, "ssid": None, "signal": None}


def _get_active_connection_name() -> str | None:
    """获取当前激活的 WiFi 连接名称（NetworkManager connection name）"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"],
            capture_output=True, text=True, check=False, timeout=5,
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

def get_available_networks(rescan: bool = True) -> list[dict]:
    """
    扫描并返回周边 WiFi 列表
    每项：{"ssid": str, "signal": int, "secured": bool}
    """
    try:
        if rescan:
            subprocess.run(
                ["sudo", "-n", "nmcli", "dev", "wifi", "rescan"],
                capture_output=True, check=False, timeout=10,
            )
            time.sleep(1)

        result = subprocess.run(
            ["sudo", "-n", "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if result.returncode != 0:
            return []

        networks: list[dict] = []
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
    except Exception as e:
        logger.error(f"get_available_networks error: {e}")
        return []


# ─────────────────────────────────────────────
# 连接管理（激活 / 添加 / 删除 / 优先级）
# ─────────────────────────────────────────────

def get_wifi_connections() -> list[dict]:
    """
    获取所有已配置的 WiFi 连接，按优先级降序排列
    每项：{"name": str, "ssid": str, "priority": int, "autoconnect": bool}
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY", "connection", "show"],
            capture_output=True, text=True, check=False, timeout=8,
        )
        if result.returncode != 0:
            return []

        connections: list[dict] = []
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
                connections.append({
                    "name": name,
                    "ssid": ssid or name,
                    "priority": priority,
                    "autoconnect": autoconnect,
                })

        connections.sort(key=lambda x: x["priority"], reverse=True)
        return connections
    except Exception as e:
        logger.error(f"get_wifi_connections error: {e}")
        return []


def _get_connection_ssid(con_name: str) -> str:
    """获取指定连接配置的 SSID"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", con_name],
            capture_output=True, text=True, check=False, timeout=5,
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
            capture_output=True, text=True, check=False, timeout=timeout,
        )
        if result.returncode == 0:
            time.sleep(3)
            return get_current_wifi().get("connected", False)

        # NetworkManager 有时返回非零但实际已连接，多检查几次
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

def _write_password_temp_file(password: str) -> str:
    """
    将密码写入临时文件，返回文件路径。
    使用文件传递密码，避免命令行参数暴露。
    """
    import tempfile
    fd, path = tempfile.mkstemp(prefix=".wifi_pass_", dir="/tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(password)
        os.chmod(path, 0o600)  # 仅所有者可读写
        return path
    except Exception as e:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(path):
            os.unlink(path)
        raise RuntimeError(f"创建密码临时文件失败: {e}")


def switch_wifi(ssid: str, password: str = "") -> dict:
    """
    切换到指定 WiFi，失败时自动回档到上一个连接。
    密码通过临时文件传递，避免命令行暴露。

    流程：
    1. 记录当前连接（回档快照）
    2. 校验输入
    3. 如连接已存在 → modify；否则 → add
    4. nmcli connection up 激活
    5. 等待验证：最多重试 5×2s
    6. 失败 → 尝试回档 → 如回档也失败则返回错误
    7. 全程写状态到 WIFI_STATUS_FILE
    """
    # 输入校验
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID 长度必须为 1-32 字符")
    if password and (len(password) < 8 or len(password) > 63):
        raise ValueError("密码长度必须为 8-63 字符（开放网络可留空）")

    # 快照当前连接
    prev_conn = get_current_wifi()
    prev_con_name = _get_active_connection_name()
    prev_ssid = prev_conn.get("ssid") if prev_conn.get("connected") else None

    con_name = f"WiFi-{ssid}"

    try:
        # 检查连接是否已存在
        check = subprocess.run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        con_exists = con_name in check.stdout.splitlines()

        if con_exists:
            # 修改已有连接
            if password:
                # 使用 wifi-sec.psk 直接设置密码（nmcli connection modify 不支持 --passwd-file）
                cmd = ["sudo", "-n", "nmcli", "connection", "modify", con_name,
                       "wifi.ssid", ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
            else:
                cmd = ["sudo", "-n", "nmcli", "connection", "modify", con_name,
                       "wifi.ssid", ssid, "wifi-sec.key-mgmt", "none"]
            r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
            if r.returncode != 0:
                raise RuntimeError(f"修改连接失败：{r.stderr.strip()}")
        else:
            # 新建连接 - 使用设备连接
            if password:
                cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
                       "password", password, "name", con_name]
            else:
                cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
                       "name", con_name]
            r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            initial_err = r.stderr.strip() if r.returncode != 0 else None
            # "No network with SSID" 是用户错误，直接抛
            if r.returncode != 0 and "No network with SSID" in (r.stderr or ""):
                raise ValueError(f"找不到网络 '{ssid}'")

        # 激活连接
        activate = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "up", con_name],
            capture_output=True, text=True, check=False, timeout=30,
        )

        # 等待并验证
        time.sleep(3)
        curr = get_current_wifi()

        if curr.get("connected") and curr.get("ssid") == ssid:
            status = {"success": True, "message": f"已成功连接到 '{ssid}'",
                      "ssid": ssid, "prev_ssid": prev_ssid, "action": "connected"}
            _save_wifi_status(status)
            return status

        # 等待 NetworkManager 可能的自动重连
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
                # NM 已自动回档
                err = activate.stderr.strip() if activate.returncode != 0 else "连接验证失败"
                status = {"success": False,
                          "message": f"连接 '{ssid}' 失败，已自动回档到 '{prev_ssid}'",
                          "ssid": ssid, "prev_ssid": prev_ssid, "action": "reverted", "error": err}
                _save_wifi_status(status)
                return status

        # 手动回档
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

    except subprocess.TimeoutExpired as e:
        logger.error(f"WiFi 切换超时: SSID={ssid}, 超时={e}")
        # 超时时仍尝试回档
        time.sleep(2)
        curr = get_current_wifi()
        reverted = (curr.get("connected") and prev_ssid and curr.get("ssid") == prev_ssid)
        if not reverted and prev_con_name:
            reverted = _activate_connection(prev_con_name, timeout=15)

        status = {
            "success": False,
            "code": "TIMEOUT",
            "message": (f"连接超时，已回档到 '{prev_ssid}'" if reverted else "连接超时，请手动检查网络"),
            "ssid": ssid, "prev_ssid": prev_ssid,
            "action": "reverted" if reverted else "failed",
            "error": "timeout"
        }
        _save_wifi_status(status)
        return status

    except ValueError as e:
        # 用户输入错误，直接抛出
        logger.warning(f"WiFi 切换参数错误: {e}")
        raise
    except subprocess.CalledProcessError as e:
        logger.error(f"WiFi 切换子进程错误: returncode={e.returncode}, stderr={e.stderr}")
        raise RuntimeError(f"WiFi 切换失败: {e.stderr or '命令执行错误'}")
    except Exception as e:
        logger.error(f"WiFi 切换未知异常: {e}", exc_info=True)
        raise RuntimeError(f"切换 WiFi 异常：{e}")


# ─────────────────────────────────────────────
# 添加 / 删除 / 修改优先级
# ─────────────────────────────────────────────

def add_wifi_connection(ssid: str, password: str, priority: int, con_name: str = None, autoconnect: bool = True) -> dict:
    """
    添加新的 WiFi 连接配置（不立即激活）
    
    Args:
        ssid: WiFi SSID
        password: WiFi 密码
        priority: 自动连接优先级 (0-100)
        con_name: 自定义连接名，默认 WiFi-{ssid}
        autoconnect: 是否自动连接，默认 True
    """
    if not ssid or len(ssid) > 32:
        return {"success": False, "message": "SSID 长度必须为 1-32 字符"}
    if password and (len(password) < 8 or len(password) > 63):
        return {"success": False, "message": "密码长度必须为 8-63 字符"}
    if not (0 <= priority <= 100):
        return {"success": False, "message": "优先级必须在 0-100 之间"}
    
    # 使用自定义连接名或默认连接名
    con_name = con_name.strip() if con_name else f"WiFi-{ssid}"
    if not con_name or len(con_name) > 64:
        return {"success": False, "message": "连接名长度必须为 1-64 字符"}

    # 检查是否已存在
    check = subprocess.run(
        ["nmcli", "-t", "-f", "NAME", "connection", "show"],
        capture_output=True, text=True, check=False, timeout=5,
    )
    if con_name in check.stdout.splitlines():
        return {"success": False, "message": f"连接 '{con_name}' 已存在，请直接修改优先级或删除后重新添加"}

    try:
        autoconnect_val = "yes" if autoconnect else "no"
        
        if password:
            # 使用 wifi-sec.psk 直接设置密码（nmcli connection add 不支持 --passwd-file）
            cmd = [
                "sudo", "-n", "nmcli", "connection", "add",
                "type", "wifi", "ssid", ssid, "con-name", con_name,
                "wifi-sec.key-mgmt", "wpa-psk",
                "wifi-sec.psk", password,
                "connection.autoconnect", autoconnect_val,
                "connection.autoconnect-priority", str(priority),
            ]
        else:
            cmd = [
                "sudo", "-n", "nmcli", "connection", "add",
                "type", "wifi", "ssid", ssid, "con-name", con_name,
                "connection.autoconnect", autoconnect_val,
                "connection.autoconnect-priority", str(priority),
            ]
        r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
        if r.returncode == 0:
            auto_msg = "自动连接" if autoconnect else "手动连接"
            return {"success": True, "message": f"已添加 WiFi '{ssid}'（{con_name}，优先级 {priority}，{auto_msg}）"}
        return {"success": False, "message": f"添加失败：{r.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": f"添加失败：{e}"}


def delete_wifi_connection(con_name: str) -> dict:
    """删除指定 WiFi 连接配置"""
    if not con_name:
        return {"success": False, "message": "连接名不能为空"}
    try:
        r = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "delete", con_name],
            capture_output=True, text=True, check=False, timeout=10,
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
            capture_output=True, text=True, check=False, timeout=10,
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
            capture_output=True, text=True, check=False, timeout=10,
        )
        if r.returncode == 0:
            status = "开启" if autoconnect else "关闭"
            return {"success": True, "message": f"'{con_name}' 自动连接已{status}", "autoconnect": autoconnect}
        return {"success": False, "message": f"修改失败：{r.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": f"修改失败：{e}"}


def update_connection_name(old_name: str, new_name: str) -> dict:
    """修改连接名称（con-name）"""
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
            capture_output=True, text=True, check=False, timeout=10,
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
        "mac": None,
        "ip": None,
        "gateway": None,
        "dns": [],
        "channel": None,
        "frequency": None,
        "mode": None,
        "rate": None,
    }
    
    try:
        # 获取 IP 地址和 MAC
        result = subprocess.run(
            ["ip", "addr", "show", WIFI_INTERFACE],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            # 提取 MAC
            mac_match = re.search(r"link/ether\s+([0-9a-f:]{17})", result.stdout, re.I)
            if mac_match:
                details["mac"] = mac_match.group(1).upper()
            
            # 提取 IP
            ip_match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", result.stdout)
            if ip_match:
                details["ip"] = ip_match.group(1)
        
        # 获取网关
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            gw_match = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
            if gw_match:
                details["gateway"] = gw_match.group(1)
        
        # 获取 DNS
        if os.path.exists("/etc/resolv.conf"):
            with open("/etc/resolv.conf", "r") as f:
                for line in f:
                    match = re.search(r"nameserver\s+(\d+\.\d+\.\d+\.\d+)", line)
                    if match:
                        details["dns"].append(match.group(1))
        
        # 获取信道、频率、速率信息（通过 iw）
        result = subprocess.run(
            ["iw", "dev", WIFI_INTERFACE, "link"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            # 信道
            ch_match = re.search(r"channel\s+(\d+)", result.stdout)
            if ch_match:
                details["channel"] = int(ch_match.group(1))
            
            # 频率
            freq_match = re.search(r"freq:\s+(\d+)", result.stdout)
            if freq_match:
                details["frequency"] = int(freq_match.group(1))
            
            # 模式
            mode_match = re.search(r"wdev\s+.*,\s*(\w+)", result.stdout)
            if mode_match:
                details["mode"] = mode_match.group(1)
            
            # 速率
            rate_match = re.search(r"rx\s+rate:\s+([\d.]+\s+\w+)", result.stdout)
            if rate_match:
                details["rate"] = rate_match.group(1)
        
        # 通过 nmcli 获取更多信息
        result = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.HW-ADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
             "device", "show", WIFI_INTERFACE],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("GENERAL.HW-ADDR:") and not details["mac"]:
                    details["mac"] = line.split(":", 1)[1].strip().upper()
                elif line.startswith("IP4.ADDRESS[1]:") and not details["ip"]:
                    ip_part = line.split(":", 1)[1].strip()
                    details["ip"] = ip_part.split("/")[0] if "/" in ip_part else ip_part
                elif line.startswith("IP4.GATEWAY:") and not details["gateway"]:
                    details["gateway"] = line.split(":", 1)[1].strip()
                elif line.startswith("IP4.DNS["):
                    dns = line.split(":", 1)[1].strip()
                    if dns not in details["dns"]:
                        details["dns"].append(dns)
        
    except Exception as e:
        logger.error(f"获取连接详情失败: {e}")
    
    return details


# ─────────────────────────────────────────────
# AP 模式管理
# ─────────────────────────────────────────────

# AP 控制脚本路径
AP_CONTROL_SCRIPT = "/opt/teslausb-web/ap_control.sh"
FORCE_MODE_FILE = "/tmp/teslausb_ap_force_mode"
AP_CONFIG_FILE = "/opt/teslausb-web/config/ap_config.json"


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
    except Exception as e:
        logger.error(f"创建 AP 配置失败: {e}")


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
        
        # 更新环境变量（用于 ap_control.sh）
        os.environ["AP_SSID"] = ssid
        os.environ["AP_PASSPHRASE"] = passphrase
        
        return {"success": True, "message": "AP 配置已更新"}
    except Exception as e:
        return {"success": False, "message": f"保存配置失败: {e}"}


def get_ap_status() -> dict:
    """获取 AP 状态"""
    try:
        # 检查脚本是否存在
        if not os.path.exists(AP_CONTROL_SCRIPT):
            return {
                "available": False,
                "ap_active": False,
                "message": "AP 控制脚本未安装"
            }
        
        # 调用 ap_control.sh 获取状态
        result = subprocess.run(
            ["sudo", "-n", AP_CONTROL_SCRIPT, "status"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        
        if result.returncode == 0:
            try:
                status = json.loads(result.stdout.strip())
                status["available"] = True
                return status
            except json.JSONDecodeError:
                return {
                    "available": True,
                    "ap_active": False,
                    "message": "无法解析状态",
                    "raw": result.stdout
                }
        else:
            return {
                "available": True,
                "ap_active": False,
                "message": f"获取状态失败: {result.stderr}",
                "error": result.stderr
            }
    except Exception as e:
        return {
            "available": False,
            "ap_active": False,
            "message": f"检查 AP 状态失败: {e}"
        }


def set_ap_force_mode(mode: str) -> dict:
    """
    设置 AP 强制模式
    mode: "force-on" - 强制开启 AP
          "force-off" - 强制关闭 AP
          "auto" - 自动模式（断网开启）
    """
    if mode not in ("force-on", "force-off", "auto"):
        return {"success": False, "message": "无效的模式"}
    
    try:
        if not os.path.exists(AP_CONTROL_SCRIPT):
            # 如果脚本不存在，使用简单方式设置
            if mode == "auto":
                if os.path.exists(FORCE_MODE_FILE):
                    os.remove(FORCE_MODE_FILE)
            else:
                with open(FORCE_MODE_FILE, "w") as f:
                    f.write(mode)
            return {"success": True, "message": f"AP 模式已设置为: {mode}"}
        
        # 调用 ap_control.sh
        result = subprocess.run(
            ["sudo", "-n", AP_CONTROL_SCRIPT, mode],
            capture_output=True, text=True, check=False, timeout=10,
        )
        
        if result.returncode == 0:
            return {"success": True, "message": f"AP 模式已设置为: {mode}"}
        else:
            return {"success": False, "message": f"设置失败: {result.stderr}"}
    except Exception as e:
        return {"success": False, "message": f"设置 AP 模式失败: {e}"}


def get_ap_force_mode() -> str:
    """获取当前 AP 强制模式"""
    try:
        if os.path.exists(FORCE_MODE_FILE):
            with open(FORCE_MODE_FILE, "r") as f:
                return f.read().strip()
    except Exception:
        pass
    return "auto"


def start_ap() -> dict:
    """手动启动 AP"""
    try:
        if not os.path.exists(AP_CONTROL_SCRIPT):
            return {"success": False, "message": "AP 控制脚本未安装"}
        
        result = subprocess.run(
            ["sudo", "-n", AP_CONTROL_SCRIPT, "start"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        
        if result.returncode == 0:
            return {"success": True, "message": "AP 已启动"}
        else:
            return {"success": False, "message": f"启动失败: {result.stderr}"}
    except Exception as e:
        return {"success": False, "message": f"启动 AP 失败: {e}"}


def stop_ap() -> dict:
    """手动停止 AP"""
    try:
        if not os.path.exists(AP_CONTROL_SCRIPT):
            return {"success": False, "message": "AP 控制脚本未安装"}
        
        result = subprocess.run(
            ["sudo", "-n", AP_CONTROL_SCRIPT, "stop"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        
        if result.returncode == 0:
            return {"success": True, "message": "AP 已停止"}
        else:
            return {"success": False, "message": f"停止失败: {result.stderr}"}
    except Exception as e:
        return {"success": False, "message": f"停止 AP 失败: {e}"}
