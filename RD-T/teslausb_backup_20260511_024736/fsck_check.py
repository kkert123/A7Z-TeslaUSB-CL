#!/usr/bin/env python3
"""
TeslaUSB Neo - 文件系统检查模块
================================
功能：
1. 检查 exFAT 分区健康状态
2. 检测文件系统错误
3. 监控分区挂载状态
4. 生成文件系统健康报告

注意：
- exFAT 分区（cam/boombox/music/lightshow）由 Tesla 格式化
- 不能在线 fsck，需要卸载后检查
- 树莓派 Zero 2W 不适合做 fsck（SD卡分区是 ext4，由系统管理）
- 本模块主要做预防性检查和报告

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


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="TeslaUSB Neo 文件系统检查")
    parser.add_argument("--check", action="store_true", help="执行完整检查")
    parser.add_argument("--quick", action="store_true", help="快速检查")
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
