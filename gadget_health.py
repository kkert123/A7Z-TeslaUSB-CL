"""
Gadget Health Monitor — USB Gadget 状态**只读**检测
====================================================

2026-09-19 降级说明（M80）:
  本模块原先承担 UDC 掉线自愈（_try_rebind_udc / _full_restart）与失败告警，
  但它只在 `has_gadget and not udc_val` 分支生效 —— 即只覆盖「UDC 完全未绑定」
  这一种情况。09-19 触碰测试实锤：真正高频的故障是
  「UDC 已绑定、但链路卡在 not attached / default」，本模块全程零日志零动作
  （09-15 / 09-18 / 09-19 四次插拔，Web 侧 gadget 日志均为 0 条）。

  自愈与告警职责已整体移交给独立常驻服务 **teslausb-usb-guard**（usb_guard.py）：
  判据更全（线在位 online + state == 'configured'）、带拔线闸与熔断、5 步处置阶梯。
  本模块**降级为纯只读**：只计算状态供 SSE / 仪表盘展示，不做任何写操作。

保留接口（routes/system_routes.py 的 SSE 首帧依赖 gadget_status 字段）:
  get_gadget_status() -> {
      'udc_bound':        bool,   # UDC 是否已绑定
      'udc_controller':   str,    # 绑定的控制器名（如 6a00000.xhci2-controller）
      'udc_connected':    bool,   # UDC 是否已连接到主机（Tesla）
      'last_check':       str,    # 上次检查时间
      'last_error':       str | None,
      'rebind_attempted': bool,   # 恒为 False（兼容保留，写操作已移出本模块）
  }
"""
import logging
import os
import threading
import time

logger = logging.getLogger("GadgetHealth")

# ── 路径常量 ──
GADGET_DIR = '/sys/kernel/config/usb_gadget/tesla_usb'
UDC_FILE = GADGET_DIR + '/UDC'

# 检测间隔
CHECK_INTERVAL = 60  # 秒

# ── 内部状态 ──
_last_check_time = 0
_last_status = {
    'udc_bound': False,
    'udc_controller': '',
    'udc_connected': False,
    'last_check': '',
    'last_error': None,
    'rebind_attempted': False,
}
_monitor_started = False       # 监控线程单例守卫
_monitor_thread = None
# 检测互斥（SSE 广播线程与常驻监控线程可能并发调用；
# 非阻塞锁保证同一时刻只有一次检测，其余调用直接返回缓存）
_check_lock = threading.Lock()


def _read_udc() -> str:
    """读取 UDC 绑定状态，返回控制器名或空字符串。"""
    if not os.path.exists(UDC_FILE):
        return ''
    try:
        with open(UDC_FILE, 'r') as f:
            val = f.read().strip()
        return val
    except (OSError, IOError):
        return ''


def _check_udc_connection() -> bool:
    """检查当前绑定的 UDC 是否已连接到主机（Tesla 车机）。

    只检查当前绑定的控制器状态，不检查其他未使用的 UDC。
    只读展示用：任何非 'not attached' 的状态都算"已挂到主机"，含枚举瞬态
    （default/addressed）。严格意义上的"链路可用"（== 'configured'）由
    teslausb-usb-guard 判定，此处不影响任何动作。
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


def _get_gadget_status_impl() -> dict:
    """
    获取当前 Gadget 状态（只读），供 SSE 广播使用。
    60s 缓存以避免频繁 check。（请通过 get_gadget_status() 调用以获得并发保护）
    """
    global _last_check_time, _last_status

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
        'rebind_attempted': False,   # 只读模块：恒 False
    }

    try:
        udc_val = _read_udc()
        has_gadget = os.path.isdir(GADGET_DIR)

        if has_gadget and udc_val:
            # UDC 已绑定
            status['udc_bound'] = True
            status['udc_controller'] = udc_val
            status['udc_connected'] = _check_udc_connection()
        elif has_gadget and not udc_val:
            # Gadget 配置存在但 UDC 未绑定 —— 只记录，不再自愈（已移交 usb_guard）
            status['last_error'] = 'UDC 未绑定'
        else:
            # Gadget 目录不存在
            status['last_error'] = 'Gadget 配置目录不存在'
    except Exception as e:
        status['last_error'] = str(e)[:200]
        logger.error("Gadget 状态检测异常: %s", e)

    _last_status = dict(status)
    return dict(status)


def get_gadget_status() -> dict:
    """线程安全入口：同一时刻只允许一个调用执行真正的检测。

    并发调用（SSE 广播线程 + 常驻监控线程）时，非首个调用直接返回缓存快照，
    避免重复检测阻塞 SSE 线程。
    """
    if not _check_lock.acquire(blocking=False):
        return dict(_last_status, rebind_attempted=False)
    try:
        return _get_gadget_status_impl()
    finally:
        _check_lock.release()


# ═══════════════════════════════════════════════════════════
# 常驻只读监控（仅刷新缓存 + 记录状态跃迁；动作已移交 usb_guard）
# ═══════════════════════════════════════════════════════════
_last_logged_state = ''


def _monitor_loop(interval: int = CHECK_INTERVAL) -> None:
    """常驻只读监控循环：保持 SSE 缓存常新，并在 state 跃迁时留痕。"""
    global _last_logged_state
    # 启动后稍等，避开开机阶段 present_usb.sh 的正常初始化窗口
    time.sleep(15)
    logger.info("Gadget 只读监控循环开始（interval=%ds；自愈已移交 teslausb-usb-guard）", interval)
    while True:
        try:
            st = get_gadget_status()
            cur = st.get('udc_controller', '') or '(未绑定)'
            if cur != _last_logged_state:
                logger.info("Gadget 状态变化: UDC %s → %s（connected=%s, err=%s）",
                            _last_logged_state or '(初始)', cur,
                            st.get('udc_connected'), st.get('last_error'))
                _last_logged_state = cur
        except Exception as e:
            logger.error("Gadget 监控循环异常: %s", e)
        time.sleep(interval)


def start_monitor(interval: int = CHECK_INTERVAL) -> bool:
    """启动常驻只读监控线程（幂等）。返回本次是否真正启动。

    仅应由唯一常驻进程调用一次（app.py 的 teslausb-web），避免多进程各自
    启动监控（本函数用 _monitor_started 做单例守卫）。
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
    logger.info("Gadget 只读监控线程已启动（interval=%ds）", interval)
    return True


def stop_monitor() -> None:
    """停止监控标记（线程为 daemon，随进程退出；此函数仅复位单例标记）。"""
    global _monitor_started
    _monitor_started = False
