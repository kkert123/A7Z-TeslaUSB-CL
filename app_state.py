"""
app_state.py — Flask 应用全局共享状态

将所有散落在 app.py 各处的模块级变量集中管理，一劳永逸地消灭 global 声明。
用法:
    from app_state import state
    state.nvme_cache['temp'] = 42        # 替代 _nvme_cache
    with state.videos_scan_cache_lock:   # 替代 _videos_scan_cache_lock
        ...
"""
import threading


class AppState:
    """Flask 应用全局状态单例"""

    def __init__(self):
        # ═══════════════════════════════════════════════════════
        # NVMe 缓存（温度 + 健康度共享同一份 smart-log 输出）
        # ═══════════════════════════════════════════════════════
        self.nvme_cache = {
            'raw_output': None,
            'timestamp': 0,
            'temp': None,
            'health': None,
        }
        self.nvme_cache_lock = threading.Lock()
        self.NVME_CACHE_TTL = 5  # 缓存有效期（秒），broadcaster 每 5s 刷新一次
        self.nvme_temp_history = []
        self.nvme_temp_lock = threading.Lock()
        self.NVME_TEMP_MAX_HISTORY = 60

        # ═══════════════════════════════════════════════════════
        # 哨兵事件缓存
        # ═══════════════════════════════════════════════════════
        self.cached_sentry_lock = threading.Lock()
        self.cached_sentry_events = 0   # 整数计数（非列表），避免 [] > 0 的 TypeError
        self.last_sentry_scan_time = 0  # 上次扫描时间戳，60s TTL

        # ═══════════════════════════════════════════════════════
        # 预览生成器状态缓存
        # ═══════════════════════════════════════════════════════
        self.preview_status_cache = {
            'state': 'idle',
            'total': 0,
            'pending': 0,
            'progress_pct': 0,
        }
        self.preview_status_cache_time = 0.0

        # ═══════════════════════════════════════════════════════
        # TeslaCam 文件系统健康缓存（60s TTL）
        # ═══════════════════════════════════════════════════════
        self.teslacam_health_cache = {}
        self.teslacam_health_cache_time = 0.0

        # ═══════════════════════════════════════════════════════
        # 位置状态缓存（30s TTL，避免频繁调 TeslaMate API）
        # ═══════════════════════════════════════════════════════
        self.location_status_cache = {}
        self.location_status_cache_time = 0.0

        # ═══════════════════════════════════════════════════════
        # 视频页面扫描缓存（30s TTL，防止全量扫描 CPU/内存拉满）
        # ═══════════════════════════════════════════════════════
        self.videos_scan_cache = {}  # {folder_type: {'events': [...], 'stats': {...}, 'ts': float}}
        self.videos_scan_cache_lock = threading.Lock()
        self.VIDEOS_SCAN_CACHE_TTL = 30  # 30 秒缓存

        # ═══════════════════════════════════════════════════════
        # 默认文件夹选择缓存（10s TTL）
        # ═══════════════════════════════════════════════════════
        self.best_default_folder_cache = {'folder': 'SentryClips', 'ts': 0}
        self.BEST_FOLDER_CACHE_TTL = 10  # 10 秒

        # ═══════════════════════════════════════════════════════
        # 温度历史（min/avg/max 统计）
        # ═══════════════════════════════════════════════════════
        self.cpu_temp_history = []
        self.gpu_temp_history = []
        self.temp_history_lock = threading.Lock()
        self.TEMP_MAX_HISTORY = 60

        # ═══════════════════════════════════════════════════════
        # 磁盘 I/O 历史（速率计算）
        # ═══════════════════════════════════════════════════════
        self.disk_io_prev = {'read_sectors': 0, 'write_sectors': 0, 'timestamp': 0}
        self.disk_io_cur = {'rate_read': 0, 'rate_write': 0}
        self.disk_io_lock = threading.Lock()

        # ═══════════════════════════════════════════════════════
        # 月度流量追踪
        # ═══════════════════════════════════════════════════════
        self.monthly_traffic_lock = threading.Lock()
        self.MONTHLY_TRAFFIC_FILE = '/opt/radxa_data/teslausb/data/monthly_traffic.json'

        # ═══════════════════════════════════════════════════════
        # SSE 订阅者管理
        # ═══════════════════════════════════════════════════════
        self.stats_subscribers = []
        self.stats_subscribers_lock = threading.Lock()
        self.log_subscribers = []
        self.log_subscribers_lock = threading.Lock()

        # ═══════════════════════════════════════════════════════
        # 温度检测索引（CPU/GPU thermal_zone 编号）
        # ═══════════════════════════════════════════════════════
        self.thermal_cpu_idx = 0
        self.thermal_gpu_idx = None


# 全局单例 — 所有模块通过 from app_state import state 引用
state = AppState()
