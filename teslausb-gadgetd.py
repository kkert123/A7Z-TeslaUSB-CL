#!/usr/bin/env python3
"""
teslausb-gadgetd.py - Gadget 守护进程
监控 UDC 状态，在 Tesla 断开或枚举失败时自动重启 gadget。
通过 Unix socket 提供 mode 切换接口给 Web 前端。
"""

import os
import json
import subprocess
import signal
import socket
import threading
import logging
import time
import sys

# ── 配置 ──
SCRIPT_PATH   = "/opt/radxa_data/usb_gadget_init.sh"
SOCKET_PATH   = "/tmp/teslausb-gadget.sock"
PID_FILE      = "/var/run/teslausb-gadgetd.pid"
LOG_FILE      = "/var/log/teslausb-gadgetd.log"
GADGET_DIR   = "/sys/kernel/config/usb_gadget/tesla_usb"
MONITOR_INTERVAL = 5    # UDC 监控间隔（秒）
RESTART_BACKOFF  = [5, 15, 30]  # 自动重启退避阶梯（秒）


# ── 日志配置 ──
# daemonize() 之前只输出到 stdout；daemonize() 之后切到文件
_log_fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
_root = logging.getLogger()
_handler_stdout = logging.StreamHandler(sys.stdout)
_handler_stdout.setFormatter(_log_fmt)
_root.setLevel(logging.DEBUG)
_root.addHandler(_handler_stdout)
log = logging.getLogger(__name__)


# ── 全局状态 ──
running         = True
current_mode    = "present"   # "present" | "edit"
_restart_failures = 0         # 连续重启失败计数（用于退避）


def _backoff_delay():
    """按当前失败次数计算退避等待时间。"""
    global _restart_failures
    idx = min(_restart_failures, len(RESTART_BACKOFF) - 1)
    return RESTART_BACKOFF[idx]


def _send_resp(sock, resp_dict):
    """向 socket 客户端发送 JSON 响应。"""
    sock.sendall(json.dumps(resp_dict).encode("utf-8"))
    log.info("响应: %s", resp_dict)


def run_script(action):
    """调用 usb_gadget_init.sh，返回 True/False。"""
    try:
        log.info("调用脚本: %s %s", SCRIPT_PATH, action)
        result = subprocess.run(
            [SCRIPT_PATH, action],
            capture_output=True, text=True, timeout=60,
        )
        if result.stdout:
            for line in result.stdout.split("\n"):
                if line.strip():
                    log.info("  %s", line.strip())
        if result.returncode == 0:
            log.info("脚本 %s 成功", action)
            return True
        log.error("脚本 %s 失败 (rc=%d): %s",
                  action, result.returncode, result.stderr)
        return False
    except subprocess.TimeoutExpired:
        log.error("脚本 %s 超时", action)
        return False
    except Exception as e:
        log.error("脚本 %s 异常: %s", action, e)
        return False


def monitor_udc():
    """后台线程：监控 UDC 状态，异常时自动重启 gadget。"""
    global running, _restart_failures
    log.info("启动 UDC 监控（间隔 %d 秒）...", MONITOR_INTERVAL)

    no_udc_count   = 0    # UDC 连续为空的次数
    not_attached_ts = None  # "not attached" 状态首次发现的时间

    while running:
        try:
            if not os.path.exists(GADGET_DIR):
                # Gadget 目录不存在 → 尝试重新初始化
                if current_mode == "present":
                    delay = _backoff_delay()
                    log.warning("Gadget 目录不存在，%d 秒后重试...", delay)
                    time.sleep(delay)
                    if not running:
                        break
                    if run_script("start"):
                        _restart_failures = 0
                    else:
                        _restart_failures += 1
                for _ in range(MONITOR_INTERVAL):
                    if not running:
                        break
                    time.sleep(1)
                continue

            udc_file = os.path.join(GADGET_DIR, "UDC")
            if not os.path.exists(udc_file):
                # Gadget 目录存在但 UDC 文件不存在 → 重新初始化
                log.warning("UDC 文件不存在，尝试重新初始化...")
                delay = _backoff_delay()
                time.sleep(delay)
                if not running:
                    break
                if run_script("restart"):
                    _restart_failures = 0
                else:
                    _restart_failures += 1
                for _ in range(MONITOR_INTERVAL):
                    if not running:
                        break
                    time.sleep(1)
                continue

            udc_content = open(udc_file).read().strip()
            if not udc_content:
                # UDC 为空 → 物理断开或绑定丢失
                no_udc_count += 1
                if no_udc_count >= 3:
                    log.warning("UDC 连续 %d 次为空，尝试重新绑定...", no_udc_count)
                    delay = _backoff_delay()
                    time.sleep(delay)
                    if not running:
                        break
                    if run_script("restart"):
                        _restart_failures = 0
                        no_udc_count = 0
                    else:
                        _restart_failures += 1
                for _ in range(MONITOR_INTERVAL):
                    if not running:
                        break
                    time.sleep(1)
                continue
            else:
                no_udc_count = 0  # 重置

            # UDC 有内容 → 检查 USB 枚举状态
            udc_state_file = os.path.join("/sys/class/udc", udc_content, "state")
            if os.path.exists(udc_state_file):
                state = open(udc_state_file).read().strip()
                if state == "not attached":
                    if not_attached_ts is None:
                        not_attached_ts = time.time()
                        log.warning("USB 已断开 (state=not attached)，开始计时...")
                    else:
                        elapsed = time.time() - not_attached_ts
                        if elapsed >= 30:
                            log.warning("USB 断开超过 30 秒，尝试重新绑定...")
                            delay = _backoff_delay()
                            time.sleep(delay)
                            if not running:
                                break
                            if run_script("restart"):
                                _restart_failures = 0
                                not_attached_ts = None
                            else:
                                _restart_failures += 1
                else:
                    # 已重新连接，重置计时器
                    if not_attached_ts is not None:
                        log.info("USB 已重新连接 (state=%s)", state)
                    not_attached_ts = None
            else:
                not_attached_ts = None

        except Exception as e:
            log.error("UDC 监控异常: %s", e)

        # 间隔等待（可中断）
        for _ in range(MONITOR_INTERVAL):
            if not running:
                break
            time.sleep(1)

    log.info("UDC 监控线程退出")


def handle_client(client_sock, address):
    """处理 Unix socket 客户端请求。"""
    try:
        data = client_sock.recv(4096).decode("utf-8").strip()
        if not data:
            return

        log.info("收到请求: %s", data)
        try:
            request = json.loads(data)
        except json.JSONDecodeError:
            _send_resp(client_sock, {"success": False, "error": "Invalid JSON"})
            return

        action = request.get("action")
        mode   = request.get("mode")

        if action == "switch_mode":
            if mode == "present":
                result = run_script("restart")
                if result:
                    global current_mode
                    current_mode = "present"
                    _send_resp(client_sock, {"success": True, "mode": "present"})
                else:
                    _send_resp(client_sock, {"success": False, "error": "启动失败，请检查日志"})
            elif mode == "edit":
                result = run_script("stop")
                if result:
                    current_mode = "edit"
                    _send_resp(client_sock, {"success": True, "mode": "edit"})
                else:
                    _send_resp(client_sock, {"success": False, "error": "停止失败，请检查日志"})
            else:
                _send_resp(client_sock, {"success": False, "error": "无效的模式: %s" % mode})

        elif action == "get_mode":
            _send_resp(client_sock, {"success": True, "mode": current_mode})

        elif action == "get_status":
            udc = ""
            gadget_exists = os.path.exists(GADGET_DIR)
            if gadget_exists and os.path.exists(os.path.join(GADGET_DIR, "UDC")):
                udc = open(os.path.join(GADGET_DIR, "UDC"), encoding="utf-8").read().strip()
            _send_resp(client_sock, {
                "success": True,
                "mode": current_mode,
                "udc": udc,
                "gadget_exists": gadget_exists,
            })

        else:
            _send_resp(client_sock, {"success": False, "error": "未知操作: %s" % action})

    except Exception as e:
        log.error("处理请求失败: %s", e)
        _send_resp(client_sock, {"success": False, "error": str(e)})
    finally:
        client_sock.close()


def start_socket_server():
    """主循环：Unix socket 服务器。"""
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(5)
    log.info("监听 Unix socket: %s", SOCKET_PATH)

    while running:
        try:
            server.settimeout(1.0)
            client_sock, _ = server.accept()
            handle_client(client_sock, _)
        except socket.timeout:
            continue
        except Exception as e:
            if running:
                log.error("Socket 服务器错误: %s", e)
            break

    server.close()
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)
    log.info("Socket 服务器已停止")


def daemonize():
    """双 fork 守护进程化。"""
    # 第一次 fork
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        log.error("第一次 fork 失败: %s", e)
        sys.exit(1)

    os.chdir("/")
    os.setsid()
    os.umask(0)

    # 第二次 fork
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        log.error("第二次 fork 失败: %s", e)
        sys.exit(1)

    # 重定向标准输入输出（不用 with，保持 fd 打开）
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = open("/dev/null", "r")
    logfile = open(LOG_FILE, "a+")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(logfile.fileno(), sys.stdout.fileno())
    os.dup2(logfile.fileno(), sys.stderr.fileno())

    # 切到文件 handler（关闭 StreamHandler，避免写到 /dev/null）
    _fh = logging.FileHandler(LOG_FILE)
    _fh.setFormatter(_log_fmt)
    _root.handlers.clear()
    _root.setLevel(logging.DEBUG)
    _root.addHandler(_fh)

    # 写入 PID 文件
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def start():
    """启动守护进程（由 systemd 或命令行调用）。"""
    if os.path.exists(PID_FILE):
        log.error("守护进程已在运行: %s", PID_FILE)
        sys.exit(1)

    log.info("启动 TeslaUSB Gadget 守护进程...")
    daemonize()

    # 此时已在子进程，重定向已完成，日志写入 LOG_FILE
    global current_mode
    current_mode = "present"

    signal.signal(signal.SIGTERM, lambda s, f: stop_handler())
    signal.signal(signal.SIGINT,  lambda s, f: stop_handler())

    monitor_thread = threading.Thread(target=monitor_udc, daemon=True)
    monitor_thread.start()

    if not run_script("start"):
        log.error("USB Gadget 初始化失败")
        sys.exit(1)

    log.info("Gadget 守护进程已启动，当前模式: %s", current_mode)
    start_socket_server()


def stop():
    """停止守护进程（容错：进程已死也清理文件）。"""
    if not os.path.exists(PID_FILE):
        log.warning("守护进程未运行")
        return
    with open(PID_FILE, "r", encoding="utf-8") as f:
        pid = int(f.read().strip())
    log.info("停止守护进程 (PID=%d)...", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        log.warning("进程 %d 已不存在，清理残留文件", pid)
    time.sleep(2)
    for _path in (PID_FILE, SOCKET_PATH):
        if os.path.exists(_path):
            os.remove(_path)
    log.info("守护进程已停止")


def stop_handler():
    """SIGTERM/SIGINT 处理函数。"""
    global running
    running = False
    log.info("收到停止信号，清理资源...")
    run_script("stop")
    for _path in (PID_FILE, SOCKET_PATH):
        if os.path.exists(_path):
            os.remove(_path)
    log.info("资源清理完成")
    sys.exit(0)


def restart():
    """重启守护进程。"""
    stop()
    time.sleep(2)
    start()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: %s {start|stop|restart}" % sys.argv[0])
        sys.exit(1)

    action = sys.argv[1].lower()
    if action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "restart":
        restart()
    else:
        print("Unknown action: %s" % action)
        print("Usage: %s {start|stop|restart}" % sys.argv[0])
        sys.exit(1)
