"""
Gadget Health Monitor — USB Gadget UDC 绑定状态检测与自动恢复
===============================================================

问题背景（2026-07-11）:
  A7Z 重启后，gadget 初始化脚本绑定 UDC，但 UDC 在运行中可能被意外清空
  （内核事件/时钟跳跃/其他进程冲突），导致 Tesla 无法识别 USB 设备，
  RecentClips 停止更新。

解决方案（v0.3.1.55 起）:
  - 【主动】常驻后台线程每 60s 检测 UDC 绑定状态。
    此前为"被动"——仅当有人打开仪表盘触发 SSE / get_system_stats 时才检测，
    无人看板时掉线永不重绑（D1，2026-09-13 实锤）。
  - 连续 2 次检测确认掉线才重绑，避开开机阶段 present_usb.sh 的正常瞬态（防误判）。
  - 重绑优先 *xhci* 控制器（与 usb_gadget_init.sh get_udc 对齐，修 D2）。
  - 自愈失败时记日志 + 微信告警（30 分钟防抖）。
  - 状态通过 SSE 推送至仪表盘显示。
"""
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("GadgetHealth")

# ── 路径常量 ──
GADGET_DIR = '/sys/kernel/config/usb_gadget/tesla_usb'
UDC_FILE = GADGET_DIR + '/UDC'
GADGET_INIT_SCRIPT = '/opt/radxa_data/usb_gadget_init.sh'

# 检测间隔
CHECK_INTERVAL = 60  # 秒

# D1：连续掉线判定阈值。连续 N 次检测都未绑定才认定"真掉线"并重绑，
# 避免开机阶段 present_usb.sh 正在重绑（UDC 文件瞬时为空）被误判为掉线。
REBIND_MIN_STREAK = 2

# 自愈失败微信告警防抖（秒）
ALERT_COOLDOWN = 1800

# ── 内部状态 ──
_last_check_time = 0
_last_status = {
    'udc_bound': False,
    'udc_controller': '',
    'udc_connected': False,
    'last_check': '',
    'last_error': None,
}
_unbound_streak = 0            # 连续未绑定计数（防瞬态误判）
_monitor_started = False       # 监控线程单例守卫
_monitor_thread = None
_last_alert_time = 0.0
# 检测/重绑互斥（SSE 广播线程与常驻监控线程可能并发调用；
# 用非阻塞锁保证同一时刻只有一次检测+重绑，其余调用直接返回缓存）
_check_lock = threading.Lock()


def _read_udc() -> str:
    """读取 UDC 绑定状态，返回控制器名或空字符串"""
    if not os.path.exists(UDC_FILE):
        return ''
    try:
        with open(UDC_FILE, 'r') as f:
            val = f.read().strip()
        return val
    except (OSError, IOError):
        return ''


def _check_udc_connection() -> bool:
    """检查当前绑定的 UDC 是否已连接到主机（Tesla 车机）
    
    只检查当前绑定的控制器状态，不检查其他未使用的 UDC。
    """
    try:
        current_udc = _read_udc()
        if not current_udc:
            return False
        state_file = '/sys/class/udc/{}/state'.format(current_udc)
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state = f.read().strip()
            return state not in ('not attached', '')
        return False
    except (OSError, IOError):
        return False


def _try_rebind_udc() -> bool:
    """
    尝试重新绑定 UDC。
    
    优先使用轻量级方法：直接写入 UDC 控制器名（不卸载分区，不 fsck）。
    仅在轻量级方法失败时才回退到完整的 gadget init restart。
    
    轻量级方法无副作用：不卸载任何分区，不中断 Tesla 写入。
    """
    gadget_config_ok = os.path.isdir(GADGET_DIR)
    if not gadget_config_ok:
        logger.error("Gadget 配置目录不存在，需要完整初始化: %s", GADGET_DIR)
        return _full_restart()

    # 方案 1: 轻量级 — 找到可用的 UDC 控制器并直接写入
    try:
        udc_list = os.listdir('/sys/class/udc/')
        # D2 修复：优先 *xhci* 控制器（与 usb_gadget_init.sh get_udc 一致）。
        # A7Z 有两个 UDC：6a00000.xhci2-controller（正确，车机连这根）与
        # 4100000.udc-controller（备用）。原 sorted() 按字母序会先选到
        # 4100000 → 把 gadget 绑到另一个物理控制器 → 车机侧那根 USB 立刻失联。
        ordered = ([u for u in udc_list if 'xhci' in u]
                   + [u for u in udc_list if 'xhci' not in u])
        for udc_name in ordered:
            # 检查 UDC 状态
            state_file = '/sys/class/udc/{}/state'.format(udc_name)
            state = 'not attached'
            if os.path.exists(state_file):
                try:
                    with open(state_file, 'r') as f:
                        state = f.read().strip()
                except (OSError, IOError):
                    pass
            
            # 只绑定未连接的 UDC（避免与已绑定的冲突）
            if state != 'not attached':
                # 已是 configured 状态 → 说不定已经被其他 gadget 用了
                continue

            # 直接写入 UDC 文件
            try:
                with open(UDC_FILE, 'w') as f:
                    f.write(udc_name)

                # 验证绑定成功
                with open(UDC_FILE, 'r') as f:
                    bound = f.read().strip()

                if bound == udc_name:
                    logger.info("Gadget 轻量级 UDC 绑定成功: %s", udc_name)
                    return True
                else:
                    logger.warning("UDC 绑定验证失败: 期望 %s, 实际 %s", udc_name, bound)
                    # 清除失败的绑定
                    try:
                        with open(UDC_FILE, 'w') as f:
                            f.write('\n')
                    except (OSError, IOError):
                        pass
                    continue
            except (OSError, IOError) as e:
                logger.warning("UDC 直接写入失败 %s: %s", udc_name, e)
                continue

        # 所有 UDC 都不可用 → 可能已有绑定但状态不对
        current_udc = _read_udc()
        if current_udc:
            logger.info("UDC 已有绑定: %s, 无需恢复", current_udc)
            return True

        logger.warning("轻量级 UDC 绑定失败，回退到完整重启")
        return _full_restart()
    except Exception as e:
        logger.error("轻量级 UDC 绑定异常: %s", e)
        return _full_restart()


def _full_restart() -> bool:
    """
    完整重启 Gadget（stop → start）。
    副作用：卸载所有分区，重置 gadget 配置。
    仅在轻量级方法失败时使用。
    """
    if not os.path.isfile(GADGET_INIT_SCRIPT):
        logger.error("Gadget 初始化脚本不存在: %s", GADGET_INIT_SCRIPT)
        return False

    try:
        result = subprocess.run(
            ['/bin/bash', GADGET_INIT_SCRIPT, 'restart'],
            capture_output=True,
            timeout=45,
            text=True,
        )
        if result.returncode == 0:
            logger.info("Gadget 完整重启成功")
            return True
        else:
            logger.warning("Gadget restart 失败 (rc=%d), 尝试单独 start", result.returncode)
            result2 = subprocess.run(
                ['/bin/bash', GADGET_INIT_SCRIPT, 'start'],
                capture_output=True,
                timeout=45,
                text=True,
            )
            if result2.returncode == 0:
                logger.info("Gadget start (fallback) 成功")
                return True
            logger.error("Gadget 完整重启失败: %s", result2.stderr[:200] if result2.stderr else 'unknown')
            return False
    except subprocess.TimeoutExpired:
        logger.error("Gadget 完整重启超时 (>45s)")
        return False
    except Exception as e:
        logger.error("Gadget 完整重启异常: %s", e)
        return False


def _get_gadget_status_impl() -> dict:
    """
    获取当前 Gadget 状态，供 SSE 广播使用。
    60s 缓存以避免频繁 check。（请通过 get_gadget_status() 调用以获得并发保护）

    返回:
      {
        'udc_bound': bool,          # UDC 是否已绑定
        'udc_controller': str,      # 绑定的控制器名（如 6a00000.xhci2-controller）
        'udc_connected': bool,      # UDC 是否已连接到主机（Tesla）
        'last_check': str,           # 上次检查时间
        'last_error': str | None,   # 最近错误消息
        'rebind_attempted': bool,    # 本次是否尝试了重新绑定
      }
    """
    global _last_check_time, _last_status, _unbound_streak

    now = time.time()
    if now - _last_check_time < CHECK_INTERVAL:
        return dict(_last_status, rebind_attempted=False)

    _last_check_time = now

    status = {
        'udc_bound': False,
        'udc_controller': '',
        'udc_connected': False,
        'last_check': time.strftime('%H:%M:%S'),
        'last_error': None,
        'rebind_attempted': False,
    }

    try:
        udc_val = _read_udc()
        has_gadget = os.path.isdir(GADGET_DIR)

        if has_gadget and udc_val:
            # UDC 已绑定
            _unbound_streak = 0
            status['udc_bound'] = True
            status['udc_controller'] = udc_val
            status['udc_connected'] = _check_udc_connection()
        elif has_gadget and not udc_val:
            # Gadget 配置存在但 UDC 未绑定 → 先观察，连续 N 次才重绑（防瞬态误判）
            _unbound_streak += 1
            if _unbound_streak < REBIND_MIN_STREAK:
                logger.info("检测到 UDC 未绑定（第 %d/%d 次），观察中（防开机瞬态误判）",
                            _unbound_streak, REBIND_MIN_STREAK)
                status['last_error'] = 'UDC 未绑定（观察中）'
            else:
                logger.warning("检测到 UDC 持续未绑定（连续 %d 次），尝试自动恢复...", _unbound_streak)
                status['rebind_attempted'] = True

                if _try_rebind_udc():
                    _unbound_streak = 0
                    status['udc_bound'] = True
                    new_udc = _read_udc()
                    status['udc_controller'] = new_udc
                    status['udc_connected'] = _check_udc_connection()
                    status['last_error'] = None
                    logger.info("UDC 自动恢复成功: %s", new_udc)
                else:
                    status['last_error'] = 'UDC 未绑定且自动恢复失败'
                    logger.error("UDC 自动恢复失败")
        else:
            # Gadget 目录不存在
            _unbound_streak = 0
            status['last_error'] = 'Gadget 配置目录不存在'
            logger.warning("Gadget 配置目录不存在: %s", GADGET_DIR)

    except Exception as e:
        status['last_error'] = str(e)[:200]
        logger.error("Gadget 状态检测异常: %s", e)

    _last_status = dict(status)
    return dict(status)


def get_gadget_status() -> dict:
    """线程安全入口：同一时刻只允许一个调用执行真正的检测/重绑。

    并发调用（SSE 广播线程 + 常驻监控线程）时，非首个调用直接返回缓存快照，
    避免：① 双重绑 UDC；② 重绑耗时长（_full_restart 最长 45s）阻塞 SSE 线程。
    """
    if not _check_lock.acquire(blocking=False):
        return dict(_last_status, rebind_attempted=False)
    try:
        return _get_gadget_status_impl()
    finally:
        _check_lock.release()


# ═══════════════════════════════════════════════════════════
# 常驻主动监控（v0.3.1.55 / D1）
# ═══════════════════════════════════════════════════════════
def _maybe_alert(msg: str) -> None:
    """自愈失败微信告警（ALERT_COOLDOWN 防抖）。推送失败静默，不影响主流程。"""
    global _last_alert_time
    now = time.time()
    if now - _last_alert_time < ALERT_COOLDOWN:
        return
    _last_alert_time = now
    try:
        from weixin_notifier import WeixinNotifier
        WeixinNotifier(bot_name="系统通知").send_text(msg)
        logger.info("Gadget 异常告警已推送")
    except Exception as e:
        logger.warning("Gadget 告警推送失败: %s", e)


def _monitor_loop(interval: int = CHECK_INTERVAL) -> None:
    """常驻监控循环：无人看仪表盘时也能主动检测并自愈（D1）。"""
    # 启动后稍等，避开开机阶段 present_usb.sh 的正常初始化窗口
    time.sleep(15)
    logger.info("Gadget 监控循环开始（interval=%ds）", interval)
    while True:
        try:
            st = get_gadget_status()
            if st.get('rebind_attempted'):
                if st.get('udc_bound'):
                    logger.info("Gadget 监控：UDC 掉线已自动恢复 (%s)", st.get('udc_controller'))
                else:
                    _maybe_alert(
                        "【TeslaUSB】USB 存储设备掉线且自动恢复失败，车机可能读不到 U 盘。"
                        "原因：%s" % st.get('last_error'))
        except Exception as e:
            logger.error("Gadget 监控循环异常: %s", e)
        time.sleep(interval)


def start_monitor(interval: int = CHECK_INTERVAL) -> bool:
    """启动常驻监控线程（幂等）。返回本次是否真正启动。

    仅应由唯一常驻进程调用一次（app.py 的 teslausb-web），避免多进程各自
    启动监控导致重复重绑（本函数用 _monitor_started 做单例守卫）。
    """
    global _monitor_started, _monitor_thread
    if _monitor_started:
        logger.info("Gadget 监控线程已在运行，跳过重复启动")
        return False
    _monitor_started = True
    _monitor_thread = threading.Thread(
        target=_monitor_loop, kwargs={'interval': interval},
        daemon=True, name="gadget-health")
    _monitor_thread.start()
    logger.info("Gadget 健康监控线程已启动（主动模式，interval=%ds）", interval)
    return True


def stop_monitor() -> None:
    """停止监控标记（线程为 daemon，随进程退出；此函数仅复位单例标记）。"""
    global _monitor_started
    _monitor_started = False
