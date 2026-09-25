#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A7Z TeslaUSB 持久化内核日志捕获器
=================================

目的
----
在 Radxa Cubie A7Z（Debian 11）上，把内核日志**持久化**写到 SD 卡的
``/opt/radxa_data/teslausb/data/kernel_live.log``，并做 5MB 单文件轮转
（保留 ``kernel_live.log`` 与 ``kernel_live.log.1`` 两份）。

为什么直接用 /dev/kmsg，而不用 journalctl
-----------------------------------------
2026-09-23 的触碰测试期间，事后无法从 journald 复原内核日志——查证发现是
journald 对**那一次启动的 kernel transport 丢了**（该 boot 只有 user journal、
没有 system journal）。journald 在某些启动/缓冲条件下会丢内核 ring buffer 的早期
记录，事后 ``journalctl -k`` 查不到。

``/dev/kmsg`` 是内核直接暴露的字符设备，读取它**绕开 journald**，逐条拿到内核
原本写进 ring buffer 的记录（即便 journald 没接住）。本脚本以 O_RDONLY|O_NONBLOCK
打开并 select 轮询，把内核消息原样落盘，保证任何一次启动的内核证据都能事后查证。

轮转策略
--------
- 写前检查当前 ``kernel_live.log`` 大小，``>= 5MB`` 时把旧文件 ``os.replace`` 成
  ``kernel_live.log.1``（覆盖上一轮的 .1），始终只保留最新两份。
- 内核消息量通常很小，5MB/2 份对 SD 卡写入压力可忽略。

健壮性（常驻服务关键点）
------------------------
- 每条写入后 ``flush()``，避免断电丢缓冲。
- 处理 EAGAIN/EINTR（继续轮询）、EPIPE/ENODATA（kmsg 被清空→重开 fd 继续）。
- 主循环外层 try/except 兜住任何异常，绝不退出（配合 systemd Restart=always 双保险）。

停用方法
--------
``systemctl disable --now teslausb-kernel-log.service`` 即可；本脚本不写自启逻辑，
完全由 systemd 单元管理。也可以 ``systemctl mask`` 彻底禁用。

用法
----
    python3 kernel_log_capture.py            # 正常落盘到 data/kernel_live.log
    python3 kernel_log_capture.py --dry-run   # 只打印到 stdout，不落盘（本地冒烟用）
"""

import argparse
import logging
import os
import select
import sys
import time

# ── 配置常量 ──────────────────────────────────────────────
KMSG = "/dev/kmsg"
DATA_DIR = "/opt/radxa_data/teslausb/data"
LOG_FILE = DATA_DIR + "/kernel_live.log"
MAX_BYTES = 5 * 1024 * 1024      # 单文件上限 5MB
KEEP_FILES = 2                   # 保留 kernel_live.log + .1

# 每次 read 最多取 8KB；/dev/kmsg 一条记录即一次 read 返回。
READ_CHUNK = 8192
POLL_TIMEOUT = 1.0               # select 轮询超时（秒）

# ── 日志（仅 INFO 启动信息，运行时不在每行刷 logging 避免刷屏） ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("kernel_log_capture")


def ensure_data_dir():
    """确保持久化目录存在。"""
    os.makedirs(DATA_DIR, exist_ok=True)


def parse_record(raw):
    """
    解析一条 /dev/kmsg 原始记录。

    kmsg 每条形如::
        <pri>,<seq>,<usec>,<flags>;<message>
        <pri>,<seq>,<usec>,-;<message>     # 有的版本 flags 字段为 '-'

    返回 (timestamp_str, message_bytes)。解析失败时返回 (None, raw)，
    **绝不丢行**——原样交给上层写盘。
    """
    # message 部分在第一个 ';' 之后；';' 之前是 <pri>,<seq>,<usec>,<flags>
    semi = raw.find(b";")
    if semi == -1:
        # 没有 ';' 分隔，视为纯消息，原样返回
        return None, raw

    header = raw[:semi]
    message = raw[semi + 1:]

    # header 形如 b"<pri>,<seq>,<usec>,<flags>" 或带前导 '<' 已包含在 pri 内
    # 例：b"6,12345,1695000000123,-"
    parts = header.split(b",")
    if len(parts) >= 3:
        usec_field = parts[2]
        try:
            usec = int(usec_field)
            sec = usec // 1_000_000
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec))
            return ts, message
        except (ValueError, OverflowError, OSError):
            # 时间戳字段异常，回退到原样
            return None, raw
    return None, raw


def maybe_rotate():
    """写前检查并轮转：>= MAX_BYTES 时把 .log 覆盖成 .log.1。"""
    try:
        if os.path.getsize(LOG_FILE) >= MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
            log.info("轮转：%s -> %s", LOG_FILE, LOG_FILE + ".1")
    except FileNotFoundError:
        # 文件还不存在，无需轮转
        pass
    except OSError as e:
        log.warning("轮转检查失败（继续）: %s", e)


def open_kmsg():
    """以非阻塞只读打开 /dev/kmsg，返回 fd。"""
    return os.open(KMSG, os.O_RDONLY | os.O_NONBLOCK)


def run(dry_run):
    """主循环：从 /dev/kmsg 读并落盘（或 dry-run 打印）。"""
    if not dry_run:
        ensure_data_dir()

    log.info("目标: %s 上限: %d bytes (dry_run=%s)",
             LOG_FILE, MAX_BYTES, dry_run)

    fd = None
    out = None
    try:
        if not dry_run:
            fd = open_kmsg()
            out = open(LOG_FILE, "ab", buffering=1)  # 行缓冲

        while True:
            # 非阻塞模式下，先用 select 等待可读，1s 超时避免空转
            if fd is not None:
                rlist, _, _ = select.select([fd], [], [], POLL_TIMEOUT)
                if not rlist:
                    continue
                try:
                    data = os.read(fd, READ_CHUNK)
                except OSError as e:
                    if e.errno in (11, 4):  # EAGAIN / EINTR
                        continue
                    if e.errno in (32, 61):  # EPIPE / ENODATA：kmsg 被清空
                        log.info("kmsg 重置（EPIPE/ENODATA），重开 fd")
                        try:
                            if fd is not None:
                                os.close(fd)
                        except OSError:
                            pass
                        fd = open_kmsg()
                        continue
                    # 其它 OSError：记日志、稍后重开
                    log.error("读 kmsg 异常: %s，2s 后重开", e)
                    time.sleep(2)
                    try:
                        if fd is not None:
                            os.close(fd)
                    except OSError:
                        pass
                    fd = open_kmsg()
                    continue
            else:
                # dry-run 且无 /dev/kmsg：用 stdin 模拟，便于本地冒烟。
                # 若 stdin 是交互终端（无人喂数据），不阻塞，直接优雅退出。
                if sys.stdin.isatty():
                    log.info("无 %s 且 stdin 为终端，dry-run 无输入源，退出", KMSG)
                    break
                data = sys.stdin.buffer.readline()
                if not data:
                    # Windows 等无 kmsg 环境：管道 EOF，直接优雅退出
                    log.info("无 %s 且 stdin 已结束，退出 dry-run", KMSG)
                    break

            if fd is not None:
                # 真实模式：/dev/kmsg 一次 read 即一条完整记录，整体解析
                records = [data]
            else:
                # dry-run 从 stdin 喂样例：按行切分，每行当作一条记录
                records = [ln for ln in data.split(b"\n") if ln]

            for rec in records:
                ts, message = parse_record(rec)
                if ts is not None:
                    out_line = ts.encode("ascii") + b" " + message + b"\n"
                else:
                    # 解析失败：原样落盘，时间戳用当前本地时间兜底
                    fallback = time.strftime("%Y-%m-%d %H:%M:%S")
                    out_line = fallback.encode("ascii") + b" " + message + b"\n"

                if dry_run:
                    sys.stdout.buffer.write(out_line)
                    sys.stdout.buffer.flush()
                else:
                    out.write(out_line)
                    out.flush()
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号")
    except Exception as e:  # 兜住任何异常，绝不退出循环
        log.exception("主循环未预期异常（不应发生）: %s", e)
        # 异常后也尝试重启主循环逻辑由外层处理；此处仅记录
        raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if out is not None:
            try:
                out.close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(
        description="A7Z 持久化内核日志捕获（直采 /dev/kmsg）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印到 stdout，不落盘（本地冒烟用）",
    )
    args = parser.parse_args()

    # 无 /dev/kmsg 的环境（如 Windows 开发机）给出清晰错误后优雅退出
    if not args.dry_run and not os.path.exists(KMSG):
        log.error("设备 %s 不存在，本脚本只能在 Linux/A7Z 上落盘运行。"
                  "本地冒烟请加 --dry-run。", KMSG)
        sys.exit(2)

    # 主循环外层：任何未预期异常都重启循环（与 systemd Restart=always 双保险）
    while True:
        try:
            run(args.dry_run)
            break  # run 正常返回（如 dry-run 无输入）才退出
        except Exception:
            log.exception("capture 循环异常退出，2s 后重启")
            time.sleep(2)
            continue


if __name__ == "__main__":
    main()
