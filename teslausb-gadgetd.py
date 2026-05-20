#!/usr/bin/env python3
"""
teslausb-gadgetd.py - 守护进程（包装器）
调用现有的 usb_gadget_init.sh（保留所有生产验证的功能）
增加：Unix socket 监听 + UDC 状态监控
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

# ─── 配置 ───
SCRIPT_PATH = "/opt/radxa_data/usb_gadget_init.sh"
SOCKET_PATH = "/tmp/teslausb-gadget.sock"
PID_FILE = "/var/run/teslausb-gadgetd.pid"
LOG_FILE = "/var/log/teslausb-gadgetd.log"
GADGET_DIR = "/sys/kernel/config/usb_gadget/tesla_usb"
MONITOR_INTERVAL = 5  # UDC 监控间隔（秒）

# ─── 日志配置 ───
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ─── 全局状态 ───
running = True
current_mode = "present"  # 默认 Present Mode


def run_script(action):
    """调用 usb_gadget_init.sh"""
    try:
        log.info(f"🔧 调用脚本: {SCRIPT_PATH} {action}")
        result = subprocess.run(
            [SCRIPT_PATH, action],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.stdout:
            for line in result.stdout.split('\n'):
                if line.strip():
                    log.info(f"  {line.strip()}")
        
        if result.returncode == 0:
            log.info(f"✅ 脚本 {action} 成功")
            return True
        else:
            log.error(f"❌ 脚本 {action} 失败 (returncode={result.returncode})")
            if result.stderr:
                log.error(f"  stderr: {result.stderr}")
            return False
    
    except subprocess.TimeoutExpired:
        log.error(f"❌ 脚本 {action} 超时")
        return False
    except Exception as e:
        log.error(f"❌ 脚本 {action} 异常: {e}")
        return False


def monitor_udc():
    """监控 UDC 状态（处理物理 USB 插拔）"""
    global running
    log.info(f"👁️ 启动 UDC 监控（间隔 {MONITOR_INTERVAL} 秒）...")
    
    while running:
        try:
            if os.path.exists(GADGET_DIR):
                udc_file = f"{GADGET_DIR}/UDC"
                
                if os.path.exists(udc_file):
                    udc_content = open(udc_file).read().strip()
                    
                    if not udc_content:
                        # UDC 为空（物理断开？）
                        log.warning("⚠️  UDC 为空，尝试重新绑定...")
                        run_script("restart")
                        time.sleep(2)
                    
                    else:
                        # UDC 有内容，检查 USB 连接状态
                        udc_state_file = f"/sys/class/udc/{udc_content}/state"
                        if os.path.exists(udc_state_file):
                            state = open(udc_state_file).read().strip()
                            if state == "not attached":
                                log.warning(f"⚠️  USB 已断开 (state={state})")
                                # 可选：自动重新绑定
                                # run_script("restart")
                
                else:
                    # Gadget 目录存在，但 UDC 文件不存在
                    log.warning("⚠️  UDC 文件不存在，重新初始化...")
                    run_script("restart")
                    time.sleep(2)
            
            else:
                # Gadget 目录不存在，可能需要重启
                if current_mode == "present":
                    log.warning("⚠️  Gadget 目录不存在，重新初始化...")
                    run_script("start")
                    time.sleep(2)
        
        except Exception as e:
            log.error(f"❌ UDC 监控错误: {e}")
        
        # 等待下次检查
        for i in range(MONITOR_INTERVAL):
            if not running:
                break
            time.sleep(1)


def handle_client(client_sock, address):
    """处理客户端请求"""
    try:
        data = client_sock.recv(4096).decode('utf-8').strip()
        if not data:
            return
        
        log.info(f"📨 收到请求: {data}")
        
        try:
            request = json.loads(data)
        except json.JSONDecodeError:
            response = {"success": False, "error": "Invalid JSON"}
            client_sock.sendall(json.dumps(response).encode('utf-8'))
            return
        
        action = request.get("action")
        mode = request.get("mode")
        
        if action == "switch_mode":
            if mode == "present":
                # 切换到 Present Mode → 启动 USB Gadget
                result = run_script("restart")
                if result:
                    global current_mode
                    current_mode = "present"
                    response = {"success": True, "mode": "present"}
                else:
                    response = {"success": False, "error": "启动失败，请检查日志"}
            
            elif mode == "edit":
                # 切换到 Edit Mode → 停止 USB Gadget
                result = run_script("stop")
                if result:
                    current_mode = "edit"
                    response = {"success": True, "mode": "edit"}
                else:
                    response = {"success": False, "error": "停止失败，请检查日志"}
            
            else:
                response = {"success": False, "error": f"无效的模式: {mode}"}
        
        elif action == "get_mode":
            response = {"success": True, "mode": current_mode}
        
        elif action == "get_status":
            udc = ""
            gadget_exists = False
            
            if os.path.exists(GADGET_DIR):
                gadget_exists = True
                if os.path.exists(f"{GADGET_DIR}/UDC"):
                    udc = open(f"{GADGET_DIR}/UDC").read().strip()
            
            response = {
                "success": True,
                "mode": current_mode,
                "udc": udc,
                "gadget_exists": gadget_exists
            }
        
        else:
            response = {"success": False, "error": f"未知操作: {action}"}
        
        client_sock.sendall(json.dumps(response).encode('utf-8'))
        log.info(f"📤 响应: {response}")
    
    except Exception as e:
        log.error(f"❌ 处理请求失败: {e}")
        response = {"success": False, "error": str(e)}
        client_sock.sendall(json.dumps(response).encode('utf-8'))
    
    finally:
        client_sock.close()


def start_socket_server():
    """启动 Unix socket 服务器"""
    # 删除旧的 socket 文件
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)
    
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(5)
    
    log.info(f"🎧 监听 Unix socket: {SOCKET_PATH}")
    
    while running:
        try:
            server.settimeout(1.0)
            client_sock, address = server.accept()
            handle_client(client_sock, address)
        except socket.timeout:
            continue
        except Exception as e:
            if running:
                log.error(f"❌ Socket 服务器错误: {e}")
            break
    
    server.close()
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)
    log.info("Socket 服务器已停止")


def daemonize():
    """守护进程化"""
    # 第一次 fork
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        log.error(f"第一次 fork 失败: {e}")
        sys.exit(1)
    
    # 脱离终端
    os.chdir("/")
    os.setsid()
    os.umask(0)
    
    # 第二次 fork
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        log.error(f"第二次 fork 失败: {e}")
        sys.exit(1)
    
    # 重定向标准输入输出
    sys.stdout.flush()
    sys.stderr.flush()
    with open('/dev/null', 'r') as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    with open(LOG_FILE, 'a+') as logfile:
        os.dup2(logfile.fileno(), sys.stdout.fileno())
        os.dup2(logfile.fileno(), sys.stderr.fileno())
    
    # 写入 PID 文件
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def start():
    """启动守护进程"""
    if os.path.exists(PID_FILE):
        log.error(f"守护进程已在运行: {PID_FILE}")
        sys.exit(1)
    
    log.info("🚀 启动 TeslaUSB Gadget 守护进程...")
    
    # 守护进程化
    daemonize()
    
    # 注册信号处理
    signal.signal(signal.SIGTERM, lambda s, f: stop_handler())
    signal.signal(signal.SIGINT, lambda s, f: stop_handler())
    
    # 启动 UDC 监控线程
    monitor_thread = threading.Thread(target=monitor_udc, daemon=True)
    monitor_thread.start()
    
    # 初始化 USB Gadget（调用现有脚本）
    if not run_script("start"):
        log.error("USB Gadget 初始化失败")
        sys.exit(1)
    
    # 默认切换到 Present Mode
    current_mode = "present"
    
    # 启动 Socket 服务器（主循环）
    start_socket_server()


def stop():
    """停止守护进程"""
    if not os.path.exists(PID_FILE):
        log.warning("守护进程未运行")
        return
    
    with open(PID_FILE, 'r') as f:
        pid = int(f.read().strip())
    
    log.info(f"🛑 停止守护进程 (PID={pid})...")
    
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        
        log.info("✅ 守护进程已停止")
    except Exception as e:
        log.error(f"停止守护进程失败: {e}")


def stop_handler():
    """停止处理函数"""
    global running
    running = False
    
    log.info("🛑 收到停止信号，清理资源...")
    
    # 调用脚本停止 USB Gadget
    run_script("stop")
    
    # 删除 PID 文件
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    
    log.info("✅ 资源清理完成")
    sys.exit(0)


def restart():
    """重启守护进程"""
    stop()
    time.sleep(2)
    start()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} {{start|stop|restart}}")
        sys.exit(1)
    
    action = sys.argv[1].lower()
    
    if action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "restart":
        restart()
    else:
        print(f"Unknown action: {action}")
        print(f"Usage: {sys.argv[0]} {{start|stop|restart}}")
        sys.exit(1)
