#!/usr/bin/env python3
"""
TeslaUSB Neo - 文件系统检查模块
================================
功能：
1. 检查 exFAT 分区健康状态
2. 检测 df/du 差异（幽灵空间 / exFAT 元数据损坏）
3. 安全离线 fsck 修复（Edit Mode 下卸载→修复→重新挂载）
4. 监控分区挂载状态
5. 生成文件系统健康报告

注意：
- exFAT 分区（cam/boombox/music/lightshow）由 Tesla 格式化
- fsck.exfat 需要分区卸载后才能执行
- 仅在 Edit Mode 且分区未被使用时自动修复
- A7Z 有足够内存运行 fsck（不需要 swap）

作者: TeslaUSB-Neo 项目
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import PARTITIONS

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

# 需要监控的分区及其期望的文件系统类型
EXPECTED_PARTITIONS = {
    "cam": {"fs_type": "exfat", "required": True},
    "music": {"fs_type": "exfat", "required": False},
    "lightshow": {"fs_type": "exfat", "required": False},
    "boombox": {"fs_type": "exfat", "required": False},
}

# 健康检查状态文件
FS_HEALTH_FILE = "/opt/teslausb-web/data/fs_health.json"

# 日志
LOG_FILE = "/var/log/teslausb-fsck.log"


class FileSystemChecker:
    """文件系统健康检查器"""

    def __init__(self):
        self.results = {
            "timestamp": None,
            "partitions": {},
            "issues": [],
            "warnings": [],
            "healthy": True,
        }

    def get_mount_info(self, path: str) -> Optional[Dict]:
        """获取分区挂载信息"""
        try:
            result = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "--target", path],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split()
                return {
                    "device": parts[0] if len(parts) > 0 else "unknown",
                    "fs_type": parts[1] if len(parts) > 1 else "unknown",
                    "options": parts[2] if len(parts) > 2 else "",
                    "mounted": True,
                }
            return {"mounted": False, "device": None, "fs_type": None, "options": None}
        except FileNotFoundError:
            return self._get_mount_info_fallback(path)
        except Exception as e:
            logger.error(f"获取挂载信息失败 ({path}): {e}")
            return {"mounted": False, "device": None, "fs_type": None, "options": None}

    def _get_mount_info_fallback(self, path: str) -> Optional[Dict]:
        """从 /proc/mounts 获取挂载信息"""
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4 and parts[1] == path:
                        return {
                            "device": parts[0],
                            "fs_type": parts[2],
                            "options": parts[3],
                            "mounted": True,
                        }
            return {"mounted": False, "device": None, "fs_type": None, "options": None}
        except Exception:
            return {"mounted": False, "device": None, "fs_type": None, "options": None}

    def get_disk_usage(self, path: str) -> Optional[Dict]:
        """获取磁盘使用情况"""
        try:
            stat = os.statvfs(path)
            total = stat.f_blocks * stat.f_frsize
            used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            return {
                "total": total,
                "used": used,
                "free": free,
                "percent": int((stat.f_blocks - stat.f_bfree) * 100 / stat.f_blocks) if stat.f_blocks else 0,
            }
        except Exception:
            return None

    def check_partition_integrity(self, path: str) -> Dict:
        """检查分区完整性（非破坏性）"""
        result = {
            "readable": False,
            "dir_count": 0,
            "file_count": 0,
            "total_size": 0,
            "errors": [],
            "warnings": [],
        }

        if not os.path.isdir(path):
            result["errors"].append(f"目录不存在: {path}")
            return result

        try:
            os.listdir(path)
            result["readable"] = True
        except PermissionError:
            result["errors"].append(f"无读取权限: {path}")
            return result
        except OSError as e:
            result["errors"].append(f"读取失败: {e}")
            return result

        try:
            for root, dirs, files in os.walk(path):
                result["dir_count"] += len(dirs)
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        stat = os.stat(fpath)
                        result["file_count"] += 1
                        result["total_size"] += stat.st_size
                        if stat.st_size > 4 * 1024 * 1024 * 1024:
                            result["warnings"].append(f"异常大文件: {fpath}")
                        if self._has_invalid_chars(fname):
                            result["warnings"].append(f"问题文件名: {fname}")
                    except OSError as e:
                        result["errors"].append(f"无法读取: {fpath}: {e}")
        except Exception as e:
            result["errors"].append(f"扫描中断: {e}")

        return result

    def _has_invalid_chars(self, filename: str) -> bool:
        """检查文件名是否包含 exFAT 不支持的字符"""
        invalid_chars = r'[\\/:*?"<>|]'
        return bool(re.search(invalid_chars, filename))

    def check_dmesg_errors(self) -> List[str]:
        """检查 dmesg 中与存储相关的错误"""
        errors = []
        try:
            result = subprocess.run(
                ["dmesg", "--level=err,warn"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    line_lower = line.lower()
                    keywords = ["error", "fail", "corrupt", "i/o error", "extfat", "fat"]
                    if any(kw in line_lower for kw in keywords):
                        errors.append(line.strip())
        except Exception as e:
            logger.warning(f"检查 dmesg 失败: {e}")
        return errors[-20:]

    def check_sd_health(self) -> Dict:
        """检查 SD 卡健康状态"""
        result = {"name": "unknown", "health_status": "unknown"}
        mmc_path = "/sys/block/mmcblk0/device"
        if os.path.exists(mmc_path):
            try:
                name_file = os.path.join(mmc_path, "name")
                if os.path.exists(name_file):
                    with open(name_file, "r") as f:
                        result["name"] = f.read().strip()
                result["health_status"] = "ok"
            except Exception as e:
                result["errors"] = str(e)
        return result

    def run_full_check(self) -> Dict:
        """执行完整的文件系统健康检查"""
        logger.info("=== 文件系统健康检查开始 ===")
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "partitions": {},
            "issues": [],
            "warnings": [],
            "healthy": True,
        }

        for name, path in PARTITIONS.items():
            if name == "data":
                continue

            partition_info = {
                "path": path,
                "expected_fs": EXPECTED_PARTITIONS.get(name, {}).get("fs_type"),
                "required": EXPECTED_PARTITIONS.get(name, {}).get("required", False),
            }

            mount = self.get_mount_info(path)
            partition_info["mount"] = mount

            if not mount.get("mounted"):
                msg = f"分区 {name} ({path}) 未挂载"
                if partition_info["required"]:
                    self.results["issues"].append(msg)
                    self.results["healthy"] = False
                else:
                    self.results["warnings"].append(msg)
                partition_info["integrity"] = {"readable": False, "errors": [msg]}
                self.results["partitions"][name] = partition_info
                continue

            disk = self.get_disk_usage(path)
            partition_info["disk_usage"] = disk

            integrity = self.check_partition_integrity(path)
            partition_info["integrity"] = integrity

            if integrity.get("errors"):
                self.results["issues"].extend([f"[{name}] {e}" for e in integrity["errors"]])
            if integrity.get("warnings"):
                self.results["warnings"].extend([f"[{name}] {w}" for w in integrity["warnings"]])

            # 幽灵空间检测（仅 cam 分区，该分区最容易出现 exFAT 损坏）
            if name == "cam" and mount.get("fs_type") == "exfat":
                ghost = self.detect_du_df_discrepancy(path)
                if ghost:
                    partition_info["ghost_space"] = ghost
                    if ghost["suspicious"]:
                        self.results["issues"].append(
                            f"[{name}] ⚠️ 检测到幽灵空间: {ghost['ghost_fmt']} "
                            f"({ghost['ghost_percent']}%) — 建议执行 fsck 修复"
                        )
                        self.results["healthy"] = False

            self.results["partitions"][name] = partition_info

        dmesg_errors = self.check_dmesg_errors()
        if dmesg_errors:
            self.results["dmesg_errors"] = dmesg_errors

        sd_health = self.check_sd_health()
        self.results["sd_card"] = sd_health

        self._save_report()

        status = "健康" if self.results["healthy"] else "异常"
        logger.info(f"=== 文件系统检查完成: {status} ===")

        return self.results

    def _save_report(self):
        """保存检查报告"""
        try:
            os.makedirs(os.path.dirname(FS_HEALTH_FILE), exist_ok=True)
            with open(FS_HEALTH_FILE, "w") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存检查报告失败: {e}")

    # ═══════════════════════════════════════════════════════════
    # 幽灵空间检测与修复
    # ═══════════════════════════════════════════════════════════

    # 幽灵空间判定阈值
    GHOST_MIN_BYTES = 1 * 1024 * 1024 * 1024   # 差异超过 1GB 才报告
    GHOST_MIN_PERCENT = 5.0                     # 差异超过分区 5% 才报告

    def detect_du_df_discrepancy(self, path: str) -> Optional[Dict]:
        """检测文件系统已用空间与目录树统计大小的差异（幽灵空间）。

        幽灵空间 = df_used - du_total，通常由 exFAT 元数据损坏
        （FAT 表标记了已分配的簇但目录树无对应文件）导致。

        Args:
            path: 挂载点路径，如 "/mnt/teslacam"

        Returns:
            {
                "du_bytes": int,        # 目录遍历累计大小
                "df_used": int,         # 文件系统报告已用
                "ghost_bytes": int,     # 幽灵空间（df_used - du_bytes）
                "ghost_percent": float, # 幽灵空间占分区百分比
                "suspicious": bool,     # 是否超过阈值
            }
            出错时返回 None。
        """
        try:
            # df: 文件系统级别已用空间
            stat = os.statvfs(path)
            df_total = stat.f_blocks * stat.f_frsize
            df_used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
        except OSError as e:
            logger.warning(f"无法获取 {path} 的 df 数据: {e}")
            return None

        # du: 遍历目录树累计所有文件大小（30 秒超时保护）
        du_total = 0
        file_count = 0
        walk_start = time.time()
        walk_timeout = 30  # 秒
        try:
            for root, dirs, files in os.walk(path):
                for fname in files:
                    try:
                        du_total += os.path.getsize(os.path.join(root, fname))
                        file_count += 1
                    except OSError:
                        continue
                # 超时保护：大分区遍历可能很慢
                if time.time() - walk_start > walk_timeout:
                    logger.warning(
                        f"du 遍历超时 ({walk_timeout}s)，已扫描 {file_count} 个文件 "
                        f"({self._fmt_bytes(du_total)})，提前终止"
                    )
                    break
        except Exception as e:
            logger.warning(f"遍历 {path} 计算 du 时出错: {e}")

        ghost_bytes = max(0, df_used - du_total)
        ghost_percent = (ghost_bytes / df_total * 100) if df_total else 0

        suspicious = (
            ghost_bytes >= self.GHOST_MIN_BYTES
            and ghost_percent >= self.GHOST_MIN_PERCENT
        )

        if suspicious:
            logger.warning(
                f"⚠️ 检测到幽灵空间: {path} | df_used={self._fmt_bytes(df_used)} | "
                f"du_total={self._fmt_bytes(du_total)} | "
                f"ghost={self._fmt_bytes(ghost_bytes)} ({ghost_percent:.1f}%) | "
                f"文件数={file_count}"
            )

        return {
            "du_bytes": du_total,
            "df_total": df_total,
            "df_used": df_used,
            "ghost_bytes": ghost_bytes,
            "ghost_percent": round(ghost_percent, 1),
            "suspicious": suspicious,
            "file_count": file_count,
            "du_size_fmt": self._fmt_bytes(du_total),
            "df_used_fmt": self._fmt_bytes(df_used),
            "ghost_fmt": self._fmt_bytes(ghost_bytes),
        }

    def run_fsck_repair(self, name: str) -> Dict:
        """安全执行 exFAT 修复（卸载 → fsck.exfat -y → 重新挂载）。

        仅在 Edit Mode 且分区未被使用时执行。fsck 前记录磁盘
        使用情况，修复后重新挂载并记录释放空间。

        Args:
            name: 分区名称（如 "cam"）

        Returns:
            {
                "device": str,
                "fs_type": str,
                "repaired": bool,
                "remounted": bool,
                "ghost_before": Optional[Dict],
                "ghost_after": Optional[Dict],
                "freed_bytes": int,
                "fsck_output": str,
                "errors": [...],
            }
        """
        result = {
            "device": None,
            "fs_type": None,
            "mounted": False,           # 修复后是否重新挂载成功（兼容旧字段名 remounted）
            "repaired": False,
            "remounted": False,         # 保留旧字段名向后兼容
            "fsck_result": {            # 标准的 fsck 执行结果
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
            },
            "action": "skipped",        # "repaired" | "skipped" | "failed"
            "reason": "",               # 人类可读的原因说明
            "ghost_before": None,
            "ghost_after": None,
            "freed_bytes": 0,
            "fsck_output": "",
            "errors": [],
        }

        path = PARTITIONS.get(name)
        if not path:
            result["errors"].append(f"未知分区: {name}")
            return result

        # ── 安全检查：必须是 Edit Mode ──
        try:
            with open("/tmp/teslausb_mode", "r") as f:
                mode = f.read().strip()
        except Exception:
            mode = "unknown"
        if mode != "edit":
            result["errors"].append(
                f"当前模式为 '{mode}'（需要 Edit Mode 才能离线 fsck）"
            )
            result["action"] = "skipped"
            result["reason"] = f"当前模式为 '{mode}'，需要 Edit Mode"
            return result

        # ── 获取挂载和设备信息 ──
        mount = self.get_mount_info(path)
        if not mount or not mount.get("mounted"):
            result["errors"].append(f"分区 {name} 未挂载")
            result["action"] = "skipped"
            result["reason"] = f"分区 {name} 未挂载"
            return result
        if mount.get("fs_type") != "exfat":
            result["errors"].append(
                f"分区 {name} 文件系统类型为 {mount.get('fs_type')}（需要 exFAT）"
            )
            result["action"] = "skipped"
            result["reason"] = f"分区 {name} 不是 exFAT（{mount.get('fs_type')}）"
            return result

        device = mount["device"]
        fs_type = mount["fs_type"]
        mount_options = mount.get("options", "")
        result["device"] = device
        result["fs_type"] = fs_type

        # ── 修复前记录幽灵空间 ──
        result["ghost_before"] = self.detect_du_df_discrepancy(path)

        logger.info(f"开始离线 fsck: {device} ({name}) | 挂载点: {path}")

        # ── 步骤 1: 卸载分区 ──
        try:
            subprocess.run(
                ["sudo", "umount", device],
                capture_output=True, text=True, timeout=30, check=True
            )
            logger.info(f"已卸载 {device}")
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip()
            if "busy" in err_msg.lower() or "target is busy" in err_msg:
                logger.warning(f"常规卸载被拒绝: {err_msg}，尝试 lazy umount...")
                try:
                    subprocess.run(
                        ["sudo", "umount", "-l", device],
                        capture_output=True, text=True, timeout=30, check=True
                    )
                    logger.info(f"lazy umount 成功: {device}")
                    time.sleep(3)  # 等待内核完成延迟分离
                except subprocess.CalledProcessError as e2:
                    result["errors"].append(
                        f"卸载失败: {err_msg} | lazy umount 也失败: {e2.stderr.strip()}"
                    )
                    result["action"] = "failed"
                    result["reason"] = f"无法卸载 {device}（busy + lazy 均失败）"
                    return result
            else:
                result["errors"].append(f"卸载失败: {err_msg}")
                result["action"] = "failed"
                result["reason"] = f"卸载 {device} 失败: {err_msg}"
                return result

        # ── 步骤 2: 执行 fsck.exfat ──
        fsck_start = time.time()
        try:
            fsck_result = subprocess.run(
                ["sudo", "fsck.exfat", "-p", device],
                capture_output=True, text=True, timeout=600
            )
            exit_code = fsck_result.returncode
            fsck_stdout = fsck_result.stdout.strip()
            fsck_stderr = fsck_result.stderr.strip()
            result["fsck_output"] = (
                (fsck_result.stdout + "\n" + fsck_result.stderr).strip()
            )
            result["fsck_result"] = {
                "exit_code": exit_code,
                "stdout": fsck_stdout,
                "stderr": fsck_stderr,
            }
            elapsed = time.time() - fsck_start

            # ── 解析 fsck 退出码 ──
            # 0 = clean, 1 = errors corrected, 2 = reboot needed,
            # 4 = errors left uncorrected, 8 = operational error
            if exit_code == 0:
                logger.info(f"fsck.exfat 完成: 文件系统干净 (exit=0, {elapsed:.1f}s)")
                result["repaired"] = True
            elif exit_code == 1:
                logger.info(f"fsck.exfat 修复成功 (exit=1, {elapsed:.1f}s)")
                result["repaired"] = True
            elif exit_code == 2:
                logger.info(f"fsck.exfat 修复完成，可能需要重启 (exit=2, {elapsed:.1f}s)")
                result["repaired"] = True
            elif exit_code == 4:
                # exit 4 = errors left uncorrected
                # 但需区分：是真的损坏修不了，还是 fsck 版本太老不认识 Tesla 特有格式
                is_unknown_entry = (
                    fsck_stderr
                    and "unknown entry type" in fsck_stderr.lower()
                )
                files_corrupted_zero = (
                    "files corrupted 0" in fsck_stdout.lower()
                )

                if is_unknown_entry and files_corrupted_zero:
                    # fsck 检测到: files corrupted 0 + unknown entry type
                    # 这意味着文件系统没有损坏，只是 fsck 版本太老
                    # 不认识 Tesla 的 exFAT 扩展 entry type
                    # 幽灵空间大概率是 Tesla 正常的预分配策略，非损坏
                    logger.info(
                        f"fsck.exfat: 文件系统无损坏（files corrupted 0），"
                        f"但遇到不认识的 entry type ({fsck_stderr})。"
                        f"幽灵空间可能是 Tesla 正常分配策略，非损坏。"
                    )
                    result["repaired"] = True  # 无损坏 = 无需修复
                    result["_fsck_entry_type_warning"] = fsck_stderr
                else:
                    logger.warning(
                        f"fsck.exfat 无法修复 (exit=4, {elapsed:.1f}s)"
                    )
                    result["repaired"] = False
                    if fsck_stderr:
                        logger.warning(f"fsck stderr: {fsck_stderr}")
                        if is_unknown_entry:
                            result["errors"].append(
                                f"fsck 无法修复: 文件系统包含 fsck 不认识的 entry type"
                                f"（{fsck_stderr}）。"
                                f"建议: 备份数据 → Tesla 车机格式化 → 恢复数据"
                            )
            elif exit_code == 8:
                logger.error(f"fsck.exfat 操作错误 (exit=8, {elapsed:.1f}s)")
                result["repaired"] = False
                result["errors"].append(f"fsck 操作错误 (exit=8): {fsck_stderr}")
            else:
                logger.warning(f"fsck.exfat 未知退出码: {exit_code} ({elapsed:.1f}s)")
                result["repaired"] = False
        except subprocess.TimeoutExpired:
            result["errors"].append("fsck.exfat 超时（>10 分钟）")
            result["action"] = "failed"
            result["reason"] = "fsck.exfat 超时（超过 10 分钟）"
        except Exception as e:
            result["errors"].append(f"fsck.exfat 异常: {e}")
            result["action"] = "failed"
            result["reason"] = f"fsck.exfat 异常: {e}"

        # ── 步骤 3: 重新挂载分区 ──
        mount_cmd = ["sudo", "mount", "-t", fs_type]
        if mount_options:
            mount_cmd.extend(["-o", mount_options])
        mount_cmd.extend([device, path])
        try:
            subprocess.run(
                mount_cmd,
                capture_output=True, text=True, timeout=30, check=True
            )
            result["remounted"] = True
            result["mounted"] = True
            logger.info(f"已重新挂载 {device} → {path}")
        except subprocess.CalledProcessError as e:
            result["errors"].append(f"重新挂载失败: {e.stderr.strip()}")
            result["action"] = "failed"
            result["reason"] = f"fsck 完成但重新挂载失败: {e.stderr.strip()}"
            logger.error(f"重新挂载失败! 设备 {device} 可能未挂载!")
            return result

        # ── 步骤 4: 修复后记录幽灵空间 ──
        result["ghost_after"] = self.detect_du_df_discrepancy(path)

        if result["ghost_before"] and result["ghost_after"]:
            before_ghost = result["ghost_before"].get("ghost_bytes", 0)
            after_ghost = result["ghost_after"].get("ghost_bytes", 0)
            result["freed_bytes"] = max(0, before_ghost - after_ghost)

            # 检查是否 fsck 遇到 unknown entry type 但无实际损坏
            entry_warning = result.get("_fsck_entry_type_warning", "")

            if entry_warning and before_ghost == after_ghost:
                # fsck 说 files corrupted 0 + unknown entry type + 幽灵空间不变
                # → 幽灵空间不是损坏，是 Tesla 的正常分配策略或 exFAT 元数据
                result["action"] = "healthy"
                result["reason"] = (
                    f"文件系统无损坏（fsck 报告 files corrupted 0）。"
                    f"幽灵空间（{self._fmt_bytes(before_ghost)}）"
                    f"可能是 Tesla 正常的预分配或 exFAT 元数据，无需处理。"
                )
            elif result["repaired"] and result["freed_bytes"] > 0:
                result["action"] = "repaired"
                result["reason"] = (
                    f"fsck 修复成功，释放 {self._fmt_bytes(result['freed_bytes'])} 幽灵空间"
                )
            elif result["repaired"] and result["freed_bytes"] == 0:
                result["action"] = "repaired"
                result["reason"] = (
                    "fsck 完成，文件系统已修复（幽灵空间可能源自 exFAT 簇大小开销"
                    "或 Tesla 特有格式，非损坏）"
                )
            elif not result["repaired"]:
                result["action"] = "failed"
                if not result.get("reason"):
                    result["reason"] = (
                        f"fsck 无法修复幽灵空间（{self._fmt_bytes(before_ghost)}）"
                        f" — 建议备份数据后用车机格式化"
                    )
            else:
                result["action"] = "repaired"
                result["reason"] = "fsck 修复完成并重新挂载"
        else:
            if result["repaired"]:
                result["action"] = "repaired"
                result["reason"] = "fsck 修复完成并重新挂载"
            else:
                result["action"] = "failed"
                if not result.get("reason"):
                    result["reason"] = "fsck 失败"

        return result

    @staticmethod
    def _fmt_bytes(size: int) -> str:
        """格式化字节数"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="TeslaUSB Neo 文件系统检查")
    parser.add_argument("--check", action="store_true", help="执行完整检查")
    parser.add_argument("--quick", action="store_true", help="快速检查（仅挂载状态）")
    parser.add_argument("--discrepancy", action="store_true", help="检测幽灵空间（df vs du）")
    parser.add_argument("--repair", type=str, metavar="PARTITION",
                        help="安全修复指定分区（如: cam），需 Edit Mode")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    checker = FileSystemChecker()

    if args.check:
        result = checker.run_full_check()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        exit(0 if result["healthy"] else 1)
    elif args.discrepancy:
        # 检测幽灵空间
        for name, path in PARTITIONS.items():
            if name == "data":
                continue
            mount = checker.get_mount_info(path)
            if not mount.get("mounted"):
                print(f"{name}: 未挂载")
                continue
            if mount.get("fs_type") != "exfat":
                print(f"{name}: 非 exFAT ({mount.get('fs_type')})")
                continue
            ghost = checker.detect_du_df_discrepancy(path)
            if ghost:
                status = "⚠️ 异常" if ghost["suspicious"] else "✅ 正常"
                print(f"{name} ({path}): {status}")
                print(f"  du: {ghost['du_size_fmt']} | "
                      f"df: {ghost['df_used_fmt']} | "
                      f"ghost: {ghost['ghost_fmt']} ({ghost['ghost_percent']}%)")
    elif args.repair:
        # 执行离线 fsck 修复
        result = checker.run_fsck_repair(args.repair)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["errors"]:
            exit(1)
    elif args.quick:
        for name, path in PARTITIONS.items():
            if name == "data":
                continue
            mount = checker.get_mount_info(path)
            status = "已挂载" if mount.get("mounted") else "未挂载"
            print(f"{name}: {status} ({mount.get('fs_type', 'unknown')})")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
