#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A7Z TeslaUSB RTC 回写器（v0.3.1.59 / 10-01 事故 F1）
==================================================

目的
----
把**已由 NTP 校准的系统时钟**写回硬件 RTC（``hwclock --systohc``），并落一行
持久日志到 SD 卡（``data/rtc_sync.log``）。

为什么需要
----------
A7Z 的 sunxi-rtc 在**掉电后不保时**：冷启动时内核从 RTC 读到的是复位默认值，
`dmesg` 实测：``sunxi-rtc 7090000.rtc: setting system clock to 1970-01-01T00:00:12``。
后果是**开机到 NTP 同步之间系统时钟是错的**，而 journald 的条目/归档文件名按时间戳
排序 → 会被误当成"最旧数据"优先轮转删除。10-01 实测 ``/var/log/journal`` 出现
连续 3 天断层（09-29 / 09-30 各只剩 2 行），正是关键取证窗口丢失的直接原因。

本脚本做的是**软件侧能做的部分**：只要系统时钟有效，立刻把它写回 RTC。
- 若 RTC 只是"没被及时写入"（而非硬件失压）→ 本脚本**直接修复**冷启动时钟错；
- 若 RTC 后备电源失效（硬件）→ 至少保证 RTC 尽快与系统时间一致，缩小下次
  冷启动落在错误时钟上的窗口。

安全约束（宁可不动，绝不写坏）
------------------------------
1. **仅当** ``timedatectl show -p NTPSynchronized`` 为 ``yes`` 时才写 —— 开机早期
   时钟还没校准时执行 ``--systohc`` 会把 1970 写进 RTC，属于主动破坏，必须避免。
2. 年份必须 ``>= 2025``（sanity），否则一律跳过。
3. 任何异常都吞掉并以退出码 0 结束（oneshot 单元不应因环境问题反复失败刷日志）。

停用方法
--------
``systemctl disable --now teslausb-rtc-sync.timer``。

用法
----
    python3 rtc_sync.py              # 正常：需已同步，条件满足则回写 RTC
    python3 rtc_sync.py --dry-run    # 只打印决策，不写 RTC
    python3 rtc_sync.py --force      # 跳过 NTPSynchronized 检查（仅人工排障；仍校验年份）
"""

import os
import shutil
import subprocess
import sys
import time

DATA_DIR = "/opt/radxa_data/teslausb/data"
LOG_FILE = os.path.join(DATA_DIR, "rtc_sync.log")
LOG_KEEP_BYTES = 512 * 1024     # 单文件 512KB 轮转（保留 .log 与 .log.1）
MIN_SANE_YEAR = 2025


def _log(msg: str) -> None:
    """打印到 stdout（journald）并追加一行到 SD 卡持久日志（轮转保护）。"""
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) >= LOG_KEEP_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass    # 落盘失败不影响回写动作


def _ntp_synchronized() -> bool:
    """systemd 是否已判定时钟同步。查询失败一律按「未同步」处理（保守）。"""
    try:
        r = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip().lower() == "yes"
    except Exception:
        return False


def _hwclock_path() -> str:
    return shutil.which("hwclock") or "/sbin/hwclock"


def main(argv) -> int:
    dry = "--dry-run" in argv
    force = "--force" in argv

    now = time.time()
    year = time.localtime(now).tm_year
    if year < MIN_SANE_YEAR:
        _log("系统时钟年份 %d < %d，拒绝回写 RTC（时钟未校准）" % (year, MIN_SANE_YEAR))
        return 0

    if not force and not _ntp_synchronized():
        _log("systemd 判定时钟未同步，跳过 RTC 回写（等下一轮）")
        return 0

    if dry:
        _log("DRY-RUN：条件满足，本应执行 %s --systohc（未执行）" % _hwclock_path())
        return 0

    hw = _hwclock_path()
    if not os.path.exists(hw):
        _log("找不到 hwclock（%s），跳过" % hw)
        return 0
    try:
        r = subprocess.run([hw, "--systohc"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            _log("RTC 已回写：%s --systohc 成功（%s）"
                 % (hw, time.strftime("%Y-%m-%d %H:%M:%S")))
        else:
            _log("RTC 回写失败 rc=%s err=%s"
                 % (r.returncode, (r.stderr or "").strip()[:200]))
    except Exception as e:
        _log("RTC 回写异常: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
