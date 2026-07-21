#!/usr/bin/env python3
"""
TeslaUSB A7Z — 日志轮转脚本
============================
由 systemd timer 每日触发，替代 logrotate（A7Z 未安装 logrotate 包）。

规则:
- 每日轮转一次
- 保留最近 7 天
- 超过 10MB 立即轮转
- 轮转后 gzip 压缩（最新一份 delaycompress）
"""

import gzip
import os
import shutil
import sys
from datetime import datetime, timedelta

# ── 配置 ──
LOG_FILES = [
    '/var/log/teslausb-bgpreview.log',
    '/var/log/teslausb-sentry.log',
    '/var/log/teslausb.log',
    '/var/log/teslausb-boot-notify.log',
    '/var/log/teslausb-notify-retry.log',
    '/var/log/wifi-smart-switch.log',
]
ROTATE_KEEP = 7       # 保留最近 7 份
MAX_SIZE = 10 * 1024 * 1024  # 10MB 强制轮转


def needs_rotation(filepath):
    """检查日志是否需要轮转"""
    try:
        return os.path.getsize(filepath) >= MAX_SIZE
    except OSError:
        return False


def rotate_log(filepath):
    """轮转单个日志文件"""
    if not os.path.isfile(filepath):
        return

    cleanup_old(filepath)

    # 复制当前日志 → .1，清空原文件（copytruncate 等效）
    rotated = f"{filepath}.1"
    temp_rotated = rotated + ".tmp"

    try:
        shutil.copy2(filepath, temp_rotated)
        os.replace(temp_rotated, rotated)
        # 清空原文件内容
        with open(filepath, 'w') as f:
            f.truncate(0)
    except OSError as e:
        print(f"  [WARN] {filepath}: 轮转失败 ({e})", file=sys.stderr)
        return

    # 压缩 .1 文件（除非还有 .0 在压缩中）
    # delaycompress: .1 是刚轮转出来的，不压缩；.2+ 压缩
    for i in range(2, ROTATE_KEEP + 10):
        src = f"{filepath}.{i}"
        gz = f"{filepath}.{i}.gz"
        if os.path.isfile(src) and not os.path.isfile(gz):
            try:
                with open(src, 'rb') as f_in:
                    with gzip.open(gz + '.tmp', 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                os.replace(gz + '.tmp', gz)
                os.unlink(src)
            except OSError as e:
                print(f"  [WARN] {filepath}.{i}: 压缩失败 ({e})", file=sys.stderr)


def cleanup_old(filepath):
    """滚动轮转编号 + 删除超过保留数量的旧日志"""
    # .6 → .7, .5 → .6, ... .1 → .2
    for i in range(ROTATE_KEEP, 0, -1):
        src = f"{filepath}.{i}"
        dst = f"{filepath}.{i + 1}"
        if os.path.isfile(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass

    # 删除超过保留数量的旧文件
    for i in range(ROTATE_KEEP + 1, ROTATE_KEEP + 20):
        for suffix in ('', '.gz'):
            old = f"{filepath}.{i}{suffix}"
            try:
                if os.path.isfile(old):
                    os.unlink(old)
            except OSError:
                pass


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始日志轮转...")
    rotated = 0

    for logfile in LOG_FILES:
        if needs_rotation(logfile):
            print(f"  轮转: {logfile} ({os.path.getsize(logfile) // 1024 // 1024}MB)")
            rotate_log(logfile)
            rotated += 1

    print(f"  完成: {rotated} 个文件轮转, {len(LOG_FILES)} 个检查")
    return 0


if __name__ == '__main__':
    sys.exit(main())
