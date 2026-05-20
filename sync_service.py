"""
TeslaUSB A7Z — 视频同步与归档服务
基于 rsync + SMB/CIFS 将 TeslaCam 视频同步到 NAS

触发方式：
  - NetworkManager dispatcher (WiFi 连接匹配家庭 SSID)
  - systemd timer 每 30min 兜底
  - Web 手动触发 (POST /api/sync/trigger)
"""

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 常量 ──
CONFIG_FILE = "/opt/radxa_data/teslausb/config/sync.json"
STATUS_FILE = "/opt/radxa_data/sync_status.json"
CREDENTIALS_FILE = "/root/.smbcred"
MOUNT_POINT = "/tmp/nas_mount"
SOURCE_DIR = "/mnt/teslacam"
MODE_FILE = "/tmp/teslausb_mode"  # Present Mode 标识
SYNC_COOLDOWN = 300  # 5 分钟冷却期
MAX_RETRIES = 3
RETRY_DELAY = 60  # 秒

# ── 默认配置 ──
DEFAULT_CONFIG = {
    "enabled": False,  # 需要用户配置后手动开启
    "nas_protocol": "cifs",
    "nas_ip": "",
    "nas_share": "teslacam",
    "nas_user": "",
    "nas_domain": "WORKGROUP",
    "home_ssid": "",
    "retention_days": 7,
    "delete_after_sync": True,
    "notify_wechat": True,
}


# ═════════════════════════════════════════════════
# 配置管理
# ═════════════════════════════════════════════════

def load_config() -> dict:
    """加载同步配置"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                # 合并默认值
                merged = DEFAULT_CONFIG.copy()
                merged.update(cfg)
                return merged
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> dict:
    """保存同步配置"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return {"success": True, "message": "配置已保存"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ═════════════════════════════════════════════════
# 状态追踪
# ═════════════════════════════════════════════════

def _load_status() -> dict:
    """读取同步状态文件"""
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return _empty_status()


def _empty_status() -> dict:
    return {
        "last_run": None,
        "last_success": None,
        "status": "never",
        "files_synced": 0,
        "bytes_transferred": 0,
        "duration_sec": 0,
        "errors": [],
        "history": [],
    }


def _save_status(st: dict):
    """保存同步状态"""
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def get_sync_status() -> dict:
    """获取当前同步状态（供 API 使用）"""
    st = _load_status()
    cfg = load_config()
    st["config"] = {
        "enabled": cfg["enabled"],
        "nas_ip": cfg["nas_ip"],
        "home_ssid": cfg["home_ssid"],
        "retention_days": cfg["retention_days"],
    }
    return st


def get_sync_history() -> list[dict]:
    """获取同步历史（最多 50 条）"""
    st = _load_status()
    return st.get("history", [])[-50:]


# ═════════════════════════════════════════════════
# 前置检查
# ═════════════════════════════════════════════════

def _is_present_mode() -> bool:
    """检查是否为 Present Mode（连接 Tesla）"""
    try:
        if os.path.exists(MODE_FILE):
            with open(MODE_FILE, "r") as f:
                return f.read().strip() == "present"
    except Exception:
        pass
    return False


def _get_current_ssid() -> Optional[str]:
    """获取当前连接的 WiFi SSID"""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[0] == "yes":
                    return parts[1].strip()
    except Exception:
        pass
    return None


def _is_nas_reachable(nas_ip: str) -> bool:
    """检查 NAS 是否可达（ping + SMB 端口检查）"""
    if not nas_ip:
        return False
    try:
        # ping 检查
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", nas_ip],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return False
        
        # SMB 端口检查 (445)
        r = subprocess.run(
            ["timeout", "2", "bash", "-c", f"echo > /dev/tcp/{nas_ip}/445 2>/dev/null"],
            capture_output=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def check_prerequisites(cfg: dict = None) -> dict:
    """
    检查同步前置条件
    返回: {"ok": bool, "reason": str}
    """
    if cfg is None:
        cfg = load_config()
    
    if not cfg.get("enabled"):
        return {"ok": False, "reason": "同步未启用（sync.json 中 enabled: false）"}
    
    if not cfg.get("nas_ip"):
        return {"ok": False, "reason": "NAS IP 未配置"}
    
    if _is_present_mode():
        return {"ok": False, "reason": "Present Mode — 不干扰 Tesla 写入"}
    
    # 冷却期检查
    st = _load_status()
    if st.get("last_run"):
        try:
            last = datetime.strptime(st["last_run"], "%Y-%m-%d %H:%M:%S")
            elapsed = (datetime.now() - last).total_seconds()
            if elapsed < SYNC_COOLDOWN:
                return {"ok": False, "reason": f"冷却中（距上次 {int(elapsed)}s，需 {SYNC_COOLDOWN}s）"}
        except Exception:
            pass
    
    if not _is_nas_reachable(cfg["nas_ip"]):
        return {"ok": False, "reason": f"NAS ({cfg['nas_ip']}) 不可达"}
    
    return {"ok": True, "reason": ""}


# ═════════════════════════════════════════════════
# SMB 挂载管理
# ═════════════════════════════════════════════════

def _write_credentials_file(user: str, password: str):
    """写入 SMB 凭据文件"""
    try:
        content = f"username={user}\npassword={password}\ndomain=WORKGROUP\n"
        # 用 sudo tee 写入 root-only 文件
        result = subprocess.run(
            ["sudo", "-n", "tee", CREDENTIALS_FILE],
            input=content, capture_output=True, text=True, timeout=5,
        )
        subprocess.run(["sudo", "-n", "chmod", "600", CREDENTIALS_FILE],
                       capture_output=True, timeout=3)
        return result.returncode == 0
    except Exception:
        return False


def mount_nas(cfg: dict = None) -> dict:
    """挂载 SMB 共享到 /tmp/nas_mount"""
    if cfg is None:
        cfg = load_config()
    
    nas_ip = cfg.get("nas_ip", "")
    share = cfg.get("nas_share", "teslacam")
    user = cfg.get("nas_user", "")
    domain = cfg.get("nas_domain", "WORKGROUP")
    
    if not nas_ip or not share:
        return {"success": False, "message": "NAS 配置不完整"}
    
    try:
        os.makedirs(MOUNT_POINT, exist_ok=True)
        
        # 写入凭据文件
        _write_credentials_file(user, cfg.get("_nas_pass", ""))
        
        # 尝试挂载
        cmd = [
            "sudo", "-n", "mount.cifs",
            f"//{nas_ip}/{share}",
            MOUNT_POINT,
            "-o", f"credentials={CREDENTIALS_FILE},vers=3.0,iocharset=utf8,uid=0,gid=0,forceuid,forcegid"
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return {"success": True, "message": "NAS 已挂载"}
        
        # 回退尝试 vers=2.0
        cmd[-1] = f"credentials={CREDENTIALS_FILE},vers=2.0,iocharset=utf8"
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return {"success": True, "message": "NAS 已挂载（SMB 2.0）"}
        
        return {"success": False, "message": f"挂载失败: {result.stderr.strip()}"}
    
    except Exception as e:
        return {"success": False, "message": str(e)}


def umount_nas():
    """卸载 NAS 共享"""
    try:
        subprocess.run(
            ["sudo", "-n", "umount", MOUNT_POINT],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            ["sudo", "-n", "umount", "-l", MOUNT_POINT],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ═════════════════════════════════════════════════
# 主同步逻辑
# ═════════════════════════════════════════════════

def run_sync(cfg: dict = None) -> dict:
    """
    执行一次同步（由 NM dispatcher / timer / Web 触发）
    返回: {"success": bool, "message": str, "files": int, "bytes": int, ...}
    """
    if cfg is None:
        cfg = load_config()
    
    # 1. 前置检查
    pre = check_prerequisites(cfg)
    if not pre["ok"]:
        return {"success": False, "message": pre["reason"], "files": 0, "bytes": 0, "skipped": True}
    
    # 2. 加载状态
    st = _load_status()
    st["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["status"] = "running"
    st["errors"] = []
    _save_status(st)
    
    start_time = time.time()
    total_files = 0
    total_bytes = 0
    
    try:
        # 3. 挂载 NAS
        mount_result = mount_nas(cfg)
        if not mount_result["success"]:
            st["status"] = "error"
            st["errors"].append(f"NAS 挂载失败: {mount_result['message']}")
            _save_status(st)
            return {"success": False, "message": mount_result["message"], "files": 0, "bytes": 0}
        
        # 4. 执行 rsync（含重试）
        retention_days = cfg.get("retention_days", 7)
        delete_after = cfg.get("delete_after_sync", True)
        
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                files, bytez = _do_rsync(retention_days, delete_after)
                if files >= 0:  # rsync 返回码 0 或部分成功
                    total_files = files
                    total_bytes = bytez
                    break
                else:
                    st["errors"].append(f"rsync 第 {attempt}/{MAX_RETRIES} 次失败")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)
            except Exception as e:
                st["errors"].append(f"rsync 第 {attempt}/{MAX_RETRIES} 次异常: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
        
        # 5. 清理空目录
        _cleanup_empty_dirs()
        
        # 6. 卸载 NAS
        umount_nas()
        
        # 7. 更新状态
        duration = int(time.time() - start_time)
        success = total_files >= 0
        
        st["status"] = "success" if success else "error"
        st["last_success"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if success else st.get("last_success")
        st["files_synced"] = total_files
        st["bytes_transferred"] = total_bytes
        st["duration_sec"] = duration
        
        # 添加历史记录
        st["history"].append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": total_files,
            "bytes": total_bytes,
            "duration_sec": duration,
            "status": "success" if success else "error",
            "errors": st["errors"][-3:] if st["errors"] else [],
        })
        # 保留最近 50 条
        st["history"] = st["history"][-50:]
        
        _save_status(st)
        
        # 8. 企业微信通知
        if cfg.get("notify_wechat", True):
            _send_wechat_notify(st["status"], total_files, total_bytes, duration, st["errors"])
        
        return {
            "success": success,
            "message": f"同步{'完成' if success else '失败'}：{total_files} 文件，{_fmt_bytes(total_bytes)}",
            "files": total_files,
            "bytes": total_bytes,
            "duration_sec": duration,
        }
    
    except Exception as e:
        umount_nas()
        st["status"] = "error"
        st["errors"].append(str(e))
        _save_status(st)
        return {"success": False, "message": str(e), "files": 0, "bytes": 0}


def _do_rsync(retention_days: int, delete_after: bool) -> tuple:
    """
    执行 rsync 同步
    返回: (files_count, bytes_transferred) 或 (-1, 0) 表示失败
    """
    # 构建 rsync 参数
    cmd = [
        "sudo", "-n", "rsync",
        "-av",                      # archive + verbose
        "--progress",
        "--stats",                  # 输出统计信息
        "--no-owner", "--no-group", # 忽略 UID/GID
        "--modify-window=1",        # exFAT 时间精度低
    ]
    
    if delete_after:
        cmd.append("--remove-source-files")  # 同步后删除源文件
    
    # 只同步 N 天前的文件
    if retention_days > 0:
        cmd.extend(["--min-age", f"{retention_days}d"])
    
    # 源和目标
    cmd.extend([
        f"{SOURCE_DIR}/",
        f"{MOUNT_POINT}/"
    ])
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    
    files = 0
    bytez = 0
    
    if result.returncode in (0, 24):  # 0=success, 24=partial transfer (some files vanished)
        # 解析 rsync 输出获取统计
        for line in result.stdout.splitlines():
            if "Number of files:" in line:
                try:
                    files = int(line.split(":")[1].strip().replace(",", ""))
                except ValueError:
                    pass
            if "Total file size:" in line:
                try:
                    size_str = line.split(":")[1].strip().split()[0].replace(",", "")
                    bytez = int(size_str)
                except ValueError:
                    pass
    
    if result.returncode > 24:
        return -1, 0
    
    return files, bytez


def _cleanup_empty_dirs():
    """清理源目录中的空文件夹"""
    try:
        subprocess.run(
            ["find", SOURCE_DIR, "-type", "d", "-empty", "-delete"],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


# ═════════════════════════════════════════════════
# 企业微信通知
# ═════════════════════════════════════════════════

def _send_wechat_notify(status: str, files: int, bytez: int, duration: int, errors: list[str]):
    """通过企业微信机器人发送同步通知"""
    try:
        # 尝试导入 weixin_notifier
        sys.path.insert(0, "/opt/radxa_data/teslausb")
        from weixin_notifier import WeixinNotifier
        notifier = WeixinNotifier("status")
        
        if status == "success":
            content = (
                f"📤 视频同步完成\n"
                f"━━━━━━━━━━━━\n"
                f"✅ 同步成功\n"
                f"📁 文件数: {files}\n"
                f"💾 数据量: {_fmt_bytes(bytez)}\n"
                f"⏱ 耗时: {duration}s\n"
                f"🕐 时间: {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            err_str = "\n".join(errors[-3:]) if errors else "未知错误"
            content = (
                f"⚠️ 视频同步异常\n"
                f"━━━━━━━━━━━━\n"
                f"❌ 同步失败\n"
                f"📁 文件数: {files}\n"
                f"🕐 时间: {datetime.now().strftime('%H:%M:%S')}\n"
                f"📝 {err_str}"
            )
        
        notifier.send_text(content)
    except Exception:
        pass  # 通知非关键路径


# ═════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════

import sys  # 放这里避免循环导入问题

def _fmt_bytes(b: int) -> str:
    """格式化字节数"""
    if b == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(b)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if i > 0 else f"{int(f)} {units[i]}"
