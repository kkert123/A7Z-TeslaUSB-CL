#!/usr/bin/env python3
"""
A7Z TeslaUSB 部署管理器 v1.0
=============================
版本化部署工具：备份 → 部署 → 回滚 → 版本追踪 → 完整性校验

设计原则：
  1. 任何部署前必须先备份（本地 + 远程双重备份）
  2. 每次部署生成唯一版本号，记录文件清单和 SHA256
  3. 一键回滚到任意历史版本
  4. 校验远程文件完整性
  5. 所有操作可追溯

目录结构：
  _deploy/                       ← 本地版本库
  ├── versions.json             ← 版本索引
  └── backups/
      ├── v20260529_001200/     ← 版本备份
      │   ├── manifest.json     ← 文件清单+哈希
      │   └── *.py              ← 备份文件
      └── ...

用法：
  python deploy_manager.py status          # 查看当前状态
  python deploy_manager.py list            # 列出所有版本
  python deploy_manager.py deploy          # 交互式部署
  python deploy_manager.py deploy --files auto_cleanup.py,app.py -m "cleanup v2"
  python deploy_manager.py rollback        # 回滚到上一个版本
  python deploy_manager.py rollback v3     # 回滚到指定版本
  python deploy_manager.py verify          # 校验远程文件完整性

作者: TeslaUSB-Neo 项目
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Windows 编码兼容：强制 UTF-8 输出，避免 emoji 在 GBK 控制台报错 ──
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass  # 某些环境下 reconfigure 不可用

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

class Config:
    """全局配置"""

    # A7Z 连接信息
    A7Z_HOST = os.environ.get("A7Z_HOST", "100.116.18.42")
    A7Z_HOST_FALLBACK = os.environ.get("A7Z_HOST_FALLBACK", "192.168.0.102")
    A7Z_PORT = int(os.environ.get("A7Z_PORT", "22"))
    A7Z_USER = os.environ.get("A7Z_USER", "radxa")
    A7Z_PASSWORD = os.environ.get("A7Z_PASSWORD", "radxa")

    # 远程路径
    REMOTE_BASE = "/opt/radxa_data/teslausb"

    # 本地版本库路径（相对于脚本所在目录）
    VERSION_DB_DIR = "_deploy"
    BACKUP_DIR = os.path.join(VERSION_DB_DIR, "backups")
    VERSIONS_FILE = os.path.join(VERSION_DB_DIR, "versions.json")

    # 需要追踪和管理的项目文件（白名单之外的拒绝部署）
    MANAGED_FILES = [
        "app.py",
        "app_state.py",
        # utils/
        "utils/__init__.py",
        "utils/app_helpers.py",
        "utils/sei_parser.py",
        "utils/nvme_monitor.py",
        "utils/thumbnail_utils.py",
        "utils/thumbnail_decision.py",
        "utils/log_rotator.py",
        "utils/mvhd_timestamp.py",
        "utils/cache_coherency.py",
        "utils/sentry_state.py",
        "utils/video_trim.py",
        "utils/hardware_stats.py",
        "utils/system_info.py",
        # routes/
        "routes/__init__.py",
        "routes/lockchime_routes.py",
        "routes/wifi_routes.py",
        "routes/cleanup_routes.py",
        "routes/analytics_routes.py",
        "routes/cloud_routes.py",
        "routes/system_routes.py",
        "routes/video_routes.py",
        "routes/media_routes.py",
        "routes/misc_routes.py",
        "auto_cleanup.py",
        "config.json",
        "config/sentry.json",
        "config.py",
        "config_manager.py",
        "weixin_notifier.py",
        "sentry_watchdog.py",
        "sentry_service.py",
        "location_detector.py",
        "sync_service.py",
        "wifi_service.py",
        "media_service.py",
        "video_preview.py",
        "hardware_watchdog.py",
        "system_monitor.py",
        "boot_notify.py",
        "disk_image_manager.py",
        "dashcam_pb2.py",
        "dashcam.proto",
        # video-management + cloud-archive (2026-05-30)
        "video_service.py",
        "file_index.py",
        "cloud_oauth_service.py",
        "cloud_rclone_service.py",
        "cloud_archive_service.py",
        "bg_preview_generator.py",
        "templates/cloud_archive.html",
        "templates/base.html",
        "templates/videos.html",
        "templates/event_player.html",
        "templates/analytics.html",
        "templates/dashboard.html",
        "templates/system.html",
        # templates (2026-07-07 log system)
        "templates/logs.html",
        # templates (2026-07-08 wifi fix)
        "templates/wifi.html",
        # static CSS (2026-07-08)
        "static/style.css",
        "static/js/dashcam-mp4.js",
        "static/js/protobuf.min.js",
        "static/js/dashcam.proto",
        "sei_service.py",
        "preview_generator.py",
        "upload_scheduler.py",
        "sentry_notify_queue.py",
        # filesystem check (2026-06-26)
        "fsck_check.py",
        # staging service (2026-06-29)
        "staging_service.py",
        # USB gadget script (2026-07-08)
        "usb_gadget_init.sh",
        # auto present service (2026-07-08 v92)
        "auto_present_service.py",
        # camera routes + GIF service (2026-07-11 台风场景分析)
        "routes/camera_routes.py",
        "gif_service.py",
        "templates/recent_clips.html",
        "templates/sentry.html",
        # gadget health monitor (2026-07-11 UDC 解绑自动恢复)
        "gadget_health.py",
    ]

    # 部署后需要重启的服务
    RESTART_SERVICES = ["teslausb-web", "teslausb-bgpreview", "teslausb-sentry"]

    # 部署超时（秒）
    CONNECT_TIMEOUT = 15
    DEPLOY_TIMEOUT = 120


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def sha256_file(filepath: str) -> str:
    """计算文件的 SHA256 哈希"""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """计算字节的 SHA256 哈希"""
    return hashlib.sha256(data).hexdigest()


def fmt_size(size: int) -> str:
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def timestamp_id() -> str:
    """生成时间戳版本号: v20260529_143022"""
    return "v" + datetime.now().strftime("%Y%m%d_%H%M%S")


# ═══════════════════════════════════════════════════════════
# 连接管理（支持双 IP 自动切换）
# ═══════════════════════════════════════════════════════════

class A7ZConnection:
    """A7Z 连接管理，支持 Tailscale + WiFi 双 IP 自动切换"""

    def __init__(self, host: str = None):
        self.host = host or Config.A7Z_HOST
        self._sftp = None

    def connect(self, timeout: int = None):
        """
        建立连接，返回 sftp 对象。
        命令执行使用 self.exec_command()（基于 Transport.open_session）。
        """
        import paramiko
        import socket

        timeout = timeout or Config.CONNECT_TIMEOUT
        hosts_to_try = [self.host]
        if self.host != Config.A7Z_HOST_FALLBACK:
            hosts_to_try.append(Config.A7Z_HOST_FALLBACK)

        last_error = None
        for host in hosts_to_try:
            try:
                sock = socket.create_connection((host, Config.A7Z_PORT), timeout=timeout)
                self._transport = paramiko.Transport(sock)
                self._transport.connect(
                    username=Config.A7Z_USER,
                    password=Config.A7Z_PASSWORD,
                )
                self._sftp = paramiko.SFTPClient.from_transport(self._transport)

                # 验证连接可用
                out, _ = self.exec_command("echo ok", timeout=10)
                if out.strip() != "ok":
                    raise RuntimeError(f"连接验证失败: {out}")

                if host != self.host:
                    print(f"  ⚠ 主 IP 不可达，使用备用 IP: {host}")

                return self._sftp

            except Exception as e:
                last_error = e
                self._cleanup()
                continue

        raise ConnectionError(
            f"无法连接到 A7Z (尝试了 {hosts_to_try}: {last_error}"
        )

    def exec_command(self, cmd: str, timeout: int = 30) -> Tuple[str, str]:
        """
        通过 transport 执行远程命令。
        返回 (stdout_str, stderr_str)
        """
        ch = self._transport.open_session()
        ch.settimeout(timeout)
        ch.exec_command(cmd)
        stdout = ch.recv(4096).decode("utf-8", errors="replace")
        stderr = ""
        while ch.recv_stderr_ready():
            stderr += ch.recv_stderr(4096).decode("utf-8", errors="replace")
        ch.close()
        return stdout.strip(), stderr.strip()

    def _cleanup(self):
        """清理当前已建立的连接"""
        for attr in ("_sftp", "_transport"):
            obj = getattr(self, attr, None)
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def close(self):
        """关闭所有连接"""
        self._cleanup()

    def __enter__(self):
        self.connect()
        return self._sftp

    def __exit__(self, *args):
        self.close()


# ═══════════════════════════════════════════════════════════
# 版本数据库
# ═══════════════════════════════════════════════════════════

class VersionDB:
    """管理本地版本索引和备份文件"""

    def __init__(self, base_dir: str = None):
        base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.db_dir = os.path.join(base_dir, Config.VERSION_DB_DIR)
        self.backup_dir = os.path.join(base_dir, Config.BACKUP_DIR)
        self.versions_file = os.path.join(base_dir, Config.VERSIONS_FILE)
        self._init_dirs()

    def _init_dirs(self):
        """确保目录结构存在"""
        os.makedirs(self.db_dir, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)
        if not os.path.exists(self.versions_file):
            self._write_versions({"next_id": 1, "current": None, "versions": []})

    def _read_versions(self) -> dict:
        """读取版本索引"""
        try:
            with open(self.versions_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {"next_id": 1, "current": None, "versions": []}

    def _write_versions(self, data: dict):
        """写入版本索引（原子写入）"""
        tmp = self.versions_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.versions_file)

    def create_version(
        self, message: str, files: List[str], manifest: dict
    ) -> dict:
        """创建新版本记录"""
        data = self._read_versions()
        version_id = data["next_id"]
        version_name = timestamp_id()

        version = {
            "id": version_id,
            "name": version_name,
            "timestamp": datetime.now().isoformat(),
            "message": message,
            "files": files,
            "manifest": manifest,
        }

        data["versions"].insert(0, version)  # 最新排最前
        data["next_id"] = version_id + 1
        data["current"] = version_id

        self._write_versions(data)

        # 创建备份目录
        backup_path = os.path.join(self.backup_dir, version_name)
        os.makedirs(backup_path, exist_ok=True)
        return version

    def get_version(self, version_spec: str = None) -> Optional[dict]:
        """
        获取版本。支持:
          None  → 当前版本
          "latest" → 最新版本
          "v1" / "1" → 指定 id
          "v20260529_143022" → 指定 name
        """
        data = self._read_versions()
        versions = data["versions"]
        if not versions:
            return None

        if version_spec is None:
            target_id = data.get("current")
        elif version_spec.lower() == "latest":
            return versions[0]
        else:
            spec = version_spec.lstrip("v")
            for v in versions:
                if v["id"] == int(spec) if spec.isdigit() else v["name"] == version_spec:
                    target_id = v["id"]
                    break
            else:
                return None

        for v in versions:
            if v["id"] == target_id:
                return v
        return None

    def set_current(self, version_id: int):
        """设置当前版本"""
        data = self._read_versions()
        data["current"] = version_id
        self._write_versions(data)

    def list_versions(self) -> List[dict]:
        """列出所有版本（最新在前）"""
        data = self._read_versions()
        current_id = data.get("current")
        result = []
        for v in data["versions"]:
            v["is_current"] = v["id"] == current_id
            result.append(v)
        return result

    def save_backup(
        self, version_name: str, files_data: Dict[str, bytes], manifest: dict
    ):
        """保存备份文件到版本目录"""
        backup_path = os.path.join(self.backup_dir, version_name)
        os.makedirs(backup_path, exist_ok=True)

        for fname, content in files_data.items():
            fpath = os.path.join(backup_path, fname)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "wb") as f:
                f.write(content)

        # 保存 manifest
        manifest_path = os.path.join(backup_path, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    def load_backup(self, version_name: str) -> Tuple[dict, Dict[str, bytes]]:
        """加载备份文件。返回 (manifest, {filename: content_bytes})"""
        backup_path = os.path.join(self.backup_dir, version_name)
        manifest_path = os.path.join(backup_path, "manifest.json")

        if not os.path.exists(manifest_path):
            raise FileNotFoundError(f"备份 manifest 不存在: {manifest_path}")

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        files_data = {}
        for fname in manifest.get("files", {}):
            fpath = os.path.join(backup_path, fname)
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    files_data[fname] = f.read()
            else:
                print(f"  ⚠ 备份文件缺失: {fname}")

        return manifest, files_data


# ═══════════════════════════════════════════════════════════
# 部署管理器
# ═══════════════════════════════════════════════════════════

class DeployManager:
    """版本化部署管理器"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.vdb = VersionDB()
        self.conn = A7ZConnection()

    # ── 备份 ──────────────────────────────────────────────

    def backup_files(
        self, files: List[str], version_name: str
    ) -> Tuple[dict, Dict[str, bytes]]:
        """
        从远程下载文件作为备份。
        返回 (manifest, {filename: content_bytes})
        """
        print("📦 远程备份中...")
        manifest = {"version": version_name, "files": {}, "timestamp": datetime.now().isoformat()}
        files_data = {}
        sftp = self.conn.connect()

        try:
            for fname in files:
                remote_path = f"{Config.REMOTE_BASE}/{fname}"
                try:
                    with sftp.open(remote_path, "rb") as rf:
                        content = rf.read()
                    h = sha256_bytes(content)
                    size = len(content)
                    files_data[fname] = content
                    manifest["files"][fname] = {"sha256": h, "size": size}
                    print(f"  ✅ {fname} ({fmt_size(size)})")
                except FileNotFoundError:
                    print(f"  ⚠ {fname} 远程文件不存在，跳过")
                    manifest["files"][fname] = {"sha256": None, "size": 0, "missing": True}
                except Exception as e:
                    print(f"  ❌ {fname} 备份失败: {e}")
                    manifest["files"][fname] = {"sha256": None, "size": 0, "error": str(e)}
        finally:
            self.conn.close()

        # 保存到本地备份目录
        self.vdb.save_backup(version_name, files_data, manifest)

        # 同时在远程创建备份副本（双重保险）
        self._remote_backup(files)

        return manifest, files_data

    def _remote_backup(self, files: List[str]):
        """在 A7Z 上创建远程备份副本（双重保险，失败不影响主流程）"""
        try:
            sftp = self.conn.connect()
            backup_dir = f"{Config.REMOTE_BASE}/_backup_deploy"
            self.conn.exec_command(
                f"echo '{Config.A7Z_PASSWORD}' | "
            + f"sudo -S mkdir -p {backup_dir} 2>/dev/null"
            )

            for fname in files:
                src = f"{Config.REMOTE_BASE}/{fname}"
                dst = f"{backup_dir}/{fname}"
                self.conn.exec_command(
                    f"echo '{Config.A7Z_PASSWORD}' | "
                + f"sudo -S cp '{src}' '{dst}'"
                )

            self.conn.close()
        except Exception as e:
            if self.verbose:
                print(f"  ⚠ 远程备份失败（不影响主流程）: {e}")

    # ── 部署 ──────────────────────────────────────────────

    def deploy_files(self, files: List[str], version_name: str) -> bool:
        """上传文件到 A7Z（二进制模式）"""
        print(f"\n📤 部署中 (版本 {version_name})...")
        sftp = self.conn.connect()
        success_count = 0
        failed = []

        try:
            for fname in files:
                local_path = fname
                remote_path = f"{Config.REMOTE_BASE}/{fname}"

                if not os.path.exists(local_path):
                    print(f"  ❌ {fname} 本地文件不存在!")
                    failed.append(fname)
                    continue

                try:
                    # 二进制上传
                    with open(local_path, "rb") as lf:
                        data = lf.read()
                        remote_dir = os.path.dirname(remote_path)
                        # 确保远程目录存在
                        self.conn.exec_command(
                            f"echo '{Config.A7Z_PASSWORD}' | sudo -S mkdir -p '{remote_dir}' 2>/dev/null"
                        )

                        with sftp.open(remote_path, "w") as rf:
                            rf.write(data)

                    # 验证上传
                    remote_stat = sftp.stat(remote_path)
                    local_hash = sha256_file(local_path)

                    # 读取远程文件验证哈希
                    with sftp.open(remote_path, "rb") as rf:
                        remote_content = rf.read()
                    remote_hash = sha256_bytes(remote_content)

                    if local_hash == remote_hash:
                        print(f"  ✅ {fname} ({fmt_size(len(data))}, 校验通过)")
                        success_count += 1
                    else:
                        print(f"  ⚠ {fname} 上传成功但校验失败 (local={local_hash[:8]}.. remote={remote_hash[:8]}..)")
                        failed.append(fname)

                except Exception as e:
                    print(f"  ❌ {fname} 部署失败: {e}")
                    failed.append(fname)

        finally:
            self.conn.close()

        print(f"\n部署完成: {success_count}/{len(files)} 成功\n")
        if failed:
            print(f"失败文件 ({len(failed)}): {', '.join(failed)}\n")
            return False
        return True

    # ── 重启服务 ──────────────────────────────────────────

    def restart_services(self) -> bool:
        """重启关联的 systemd 服务"""
        self.conn.connect()

        try:
            for svc in Config.RESTART_SERVICES:
                _, err = self.conn.exec_command(
                    f"echo '{Config.A7Z_PASSWORD}' | "
                + f"sudo -S systemctl restart {svc} 2>&1"
                )
                # 过滤掉 sudo 密码提示（这是正常的）
                if err and "password" not in err.lower():
                    print(f"  ❌ {svc} 重启失败: {err}")
                    return False

                # 验证服务状态
                status, _ = self.conn.exec_command(
                    f"systemctl is-active {svc} 2>/dev/null"
                )
                icon = "✅" if status == "active" else "⚠️"
                print(f"  {icon} {svc}: {status}")
        except Exception as e:
            print(f"  ❌ 重启服务失败: {e}")
            return False
        finally:
            self.conn.close()

        return True

    # ── 回滚 ──────────────────────────────────────────────

    def rollback(self, version_spec: str = None) -> bool:
        """回滚到指定版本"""

        # 确定回滚目标
        if version_spec is None:
            # 默认回滚到上一个版本
            versions = self.vdb.list_versions()
            current = next((v for v in versions if v.get("is_current")), None)
            if not current:
                print("❌ 没有当前版本，无法确定回滚目标")
                return False
            # 找到当前版本的前一个版本
            for i, v in enumerate(versions):
                if v["id"] == current["id"]:
                    if i + 1 < len(versions):
                        target = versions[i + 1]
                        break
                    else:
                        print("❌ 当前已是最早版本，无更早版本可回滚")
                        return False
        else:
            target = self.vdb.get_version(version_spec)
            if not target:
                print(f"❌ 版本不存在: {version_spec}")
                return False

        print(f"\n⏪ 回滚到版本 {target['name']} ({target['message']})")
        print(f"   涉及文件: {', '.join(target['files'])}")

        # 确认
        confirm = input("\n⚠️ 确认回滚？这将覆盖远程文件！[y/N]: ")
        if confirm.lower() != "y":
            print("已取消")
            return False

        # 加载备份文件
        try:
            manifest, files_data = self.vdb.load_backup(target["name"])
        except FileNotFoundError as e:
            print(f"❌ {e}")
            return False

        # 上传备份文件
        print("📤 回滚部署中...")
        sftp = self.conn.connect()
        success = True

        try:
            for fname in target["files"]:
                if fname not in files_data:
                    print(f"  ⚠ {fname} 缺失备份，跳过")
                    continue

                remote_path = f"{Config.REMOTE_BASE}/{fname}"
                try:
                    with sftp.open(remote_path, "w") as rf:
                        rf.write(files_data[fname])

                    # 校验
                    local_hash = sha256_bytes(files_data[fname])
                    with sftp.open(remote_path, "rb") as rf:
                        remote_hash = sha256_bytes(rf.read())

                    if local_hash == remote_hash:
                        print(f"  ✅ {fname} 回滚成功")
                    else:
                        print(f"  ⚠ {fname} 校验不匹配")
                        success = False
                except Exception as e:
                    print(f"  ❌ {fname} 回滚失败: {e}")
                    success = False
        finally:
            self.conn.close()

        if success:
            # 更新当前版本
            self.vdb.set_current(target["id"])
            print(f"\n✅ 已回滚到 {target['name']}: {target['message']}")
            # 重启服务
            self.restart_services()
        else:
            print("\n⚠️ 部分文件回滚失败，请手动检查")

        return success

    # ── 部署主流程 ────────────────────────────────────────

    def deploy(self, files: List[str], message: str, skip_confirm: bool = False):
        """
        完整部署流程: 备份 → 上传 → 记录版本 → 重启
        """
        # 验证文件列表非空
        if not files:
            print("❌ 文件列表为空，至少指定一个要部署的文件")
            return False

        # 验证文件白名单
        invalid = [f for f in files if f not in Config.MANAGED_FILES]
        if invalid:
            print(f"❌ 以下文件不在管理列表中: {', '.join(invalid)}")
            print(f"   管理列表: {', '.join(Config.MANAGED_FILES)}")
            return False

        # 验证本地文件存在
        missing = [f for f in files if not os.path.exists(f)]
        if missing:
            print(f"❌ 以下文件本地不存在: {', '.join(missing)}")
            return False

        # 显示部署计划
        print("=" * 60)
        print("📋 部署计划")
        print("=" * 60)
        print(f"消息: {message}")
        print(f"文件 ({len(files)}): {', '.join(files)}")
        print(f"目标: {Config.A7Z_USER}@{Config.A7Z_HOST}:{Config.REMOTE_BASE}")
        print(f"服务重启: {', '.join(Config.RESTART_SERVICES)}")
        print("=" * 60)

        if not skip_confirm:
            confirm = input("\n⚠️ 确认部署？[y/N]: ")
            if confirm.lower() != "y":
                print("已取消")
                return False

        # 创建版本
        version = self.vdb.create_version(message, files, {})

        # 步骤1: 备份
        print(f"\n[1/4] 备份远程文件 → 版本 {version['name']}")
        try:
            manifest, files_data = self.backup_files(files, version["name"])
            version["manifest"] = manifest
        except ConnectionError as e:
            print(f"❌ 备份失败 - 连接错误: {e}")
            print("   请检查 A7Z 是否在线")
            return False

        # 步骤2: 上传
        print(f"\n[2/4] 上传新文件")
        try:
            ok = self.deploy_files(files, version["name"])
            if not ok:
                print("⚠️ 部分文件部署失败，请检查")
        except ConnectionError as e:
            print(f"❌ 部署失败 - 连接错误: {e}")
            print(f"   备份已保存到 _deploy/backups/{version['name']}/")
            print("   可以稍后手动重试或回滚")
            return False

        # 步骤3: 记录版本
        print(f"\n[3/4] 记录版本")
        data = self.vdb._read_versions()
        for v in data["versions"]:
            if v["id"] == version["id"]:
                v["manifest"] = {
                    "files": {
                        f: {
                            "sha256": sha256_file(f) if os.path.exists(f) else None,
                            "size": os.path.getsize(f) if os.path.exists(f) else 0,
                        }
                        for f in files
                    }
                }
        self.vdb._write_versions(data)
        print(f"  ✅ 版本 {version['name']} 已记录")

        # 步骤4: 重启服务
        print(f"\n[4/4] 重启服务")
        try:
            self.restart_services()
        except Exception as e:
            print(f"  ⚠ 重启失败: {e}")

        print(f"\n{'=' * 60}")
        print(f"✅ 部署完成 - 版本 {version['name']}")
        print(f"   回滚命令: python deploy_manager.py rollback v{version['id']}")
        print(f"{'=' * 60}")

        return True

    # ── 状态查询 ──────────────────────────────────────────

    def show_status(self):
        """显示当前状态"""
        print("=" * 60)
        print("📊 部署状态")
        print("=" * 60)

        # 版本信息
        current = self.vdb.get_version()
        if current:
            print(f"\n当前版本: v{current['id']} ({current['name']})")
            print(f"消息: {current['message']}")
            print(f"时间: {current['timestamp']}")
            print(f"文件: {', '.join(current['files'])}")
        else:
            print("\n当前版本: 无 (从未部署过)")

        # 远程连接测试
        print("\n远程连接:")
        try:
            sftp = self.conn.connect()
            uptime, _ = self.conn.exec_command("uptime")
            print(f"  ✅ {Config.A7Z_USER}@{Config.A7Z_HOST}")
            print(f"     {uptime}")

            # 检查服务状态
            svc, _ = self.conn.exec_command(
                "systemctl is-active teslausb-web 2>/dev/null"
            )
            print(f"     teslausb-web: {svc}")

            self.conn.close()
        except Exception as e:
            print(f"  ❌ 无法连接: {e}")

        # 版本历史摘要
        versions = self.vdb.list_versions()
        print(f"\n版本历史: {len(versions)} 个版本")
        for i, v in enumerate(versions[:5]):
            marker = " ← 当前" if v.get("is_current") else ""
            print(f"  v{v['id']} {v['name']} - {v['message']}{marker}")

    # ── 版本列表 ──────────────────────────────────────────

    def list_versions(self):
        """列出所有版本"""
        versions = self.vdb.list_versions()
        if not versions:
            print("暂无版本记录")
            return

        print("=" * 80)
        print(f"{'ID':>4}  {'版本名':<18} {'消息':<30} {'文件数':<6} {'时间'}")
        print("=" * 80)
        for v in versions:
            marker = " *" if v.get("is_current") else "  "
            dt = v["timestamp"][:19].replace("T", " ")
            print(f"{v['id']:>4}{marker} {v['name']:<18} {v['message'][:28]:<30} {len(v.get('files',[])):<6} {dt}")

    # ── 远程校验 ──────────────────────────────────────────

    def verify(self) -> bool:
        """校验远程文件完整性（对比当前版本 manifest）"""
        current = self.vdb.get_version()
        if not current:
            print("❌ 没有当前版本记录，无法校验")
            return False

        manifest = current.get("manifest", {}).get("files", {})
        if not manifest:
            print("❌ 当前版本无 manifest，无法校验")
            return False

        print("=" * 60)
        print("🔍 远程文件完整性校验")
        print(f"   对比版本: v{current['id']} ({current['name']})")
        print("=" * 60)

        sftp = self.conn.connect()
        all_ok = True

        try:
            for fname, info in manifest.items():
                if info.get("missing"):
                    print(f"  ⚪ {fname}: 部署时就不存在，跳过")
                    continue

                remote_path = f"{Config.REMOTE_BASE}/{fname}"
                expected_hash = info.get("sha256")

                try:
                    with sftp.open(remote_path, "rb") as rf:
                        content = rf.read()
                    actual_hash = sha256_bytes(content)
                    actual_size = len(content)

                    if expected_hash and actual_hash == expected_hash:
                        print(f"  ✅ {fname} ({fmt_size(actual_size)}): 匹配")
                    else:
                        print(f"  ❌ {fname}: 哈希不匹配!")
                        print(f"       期望: {expected_hash[:16]}...")
                        print(f"       实际: {actual_hash[:16]}...")
                        all_ok = False
                except FileNotFoundError:
                    print(f"  ❌ {fname}: 文件不存在!")
                    all_ok = False
                except Exception as e:
                    print(f"  ❌ {fname}: 读取失败 ({e})")
                    all_ok = False
        finally:
            self.conn.close()

        if all_ok:
            print("\n✅ 所有文件完整，与当前版本一致")
        else:
            print("\n⚠️ 发现不一致，建议重新部署或回滚")

        return all_ok


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="A7Z TeslaUSB 部署管理器 v1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python deploy_manager.py status
  python deploy_manager.py list
  python deploy_manager.py deploy --files auto_cleanup.py,app.py -m "cleanup v2 upgrade"
  python deploy_manager.py deploy -f auto_cleanup.py,config.json -m "quick fix" --yes
  python deploy_manager.py rollback
  python deploy_manager.py rollback v3
  python deploy_manager.py verify
        """,
    )

    sub = parser.add_subparsers(dest="command", help="命令")

    # deploy
    p_deploy = sub.add_parser("deploy", help="部署文件")
    p_deploy.add_argument(
        "-f", "--files", required=True,
        help="要部署的文件（逗号分隔），例如: auto_cleanup.py,app.py"
    )
    p_deploy.add_argument(
        "-m", "--message", required=True,
        help="本次部署的说明信息"
    )
    p_deploy.add_argument(
        "-y", "--yes", action="store_true",
        help="跳过确认提示"
    )

    # rollback
    p_rollback = sub.add_parser("rollback", help="回滚到指定版本")
    p_rollback.add_argument(
        "version", nargs="?", default=None,
        help="目标版本（v1 / 1 / v20260529_143022），默认上一个版本"
    )

    # status
    sub.add_parser("status", help="查看当前部署状态")

    # list
    sub.add_parser("list", help="列出所有版本")

    # verify
    sub.add_parser("verify", help="校验远程文件完整性")

    args = parser.parse_args()
    dm = DeployManager(verbose=True)

    if args.command == "status":
        dm.show_status()

    elif args.command == "list":
        dm.list_versions()

    elif args.command == "deploy":
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        dm.deploy(files, args.message, skip_confirm=args.yes)

    elif args.command == "rollback":
        dm.rollback(args.version)

    elif args.command == "verify":
        dm.verify()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
