#!/usr/bin/env python3
"""
A7Z TeslaUSB 统一配置文件
===========================
所有模块应从此文件导入路径常量，避免路径碎片化。

之前存在三套路径体系：
  - app.py 使用 /mnt/teslacam 等
  - media_service.py 使用 /media/cnlvan/cam 等
  - clean_deploy 模块使用 /opt/teslausb-web/ 等

现在统一为以下体系（A7Z 实际部署环境）。
"""

import os

# ─── 数据根目录 ───
DATA_ROOT = "/opt/radxa_data"
TESLAUSB_ROOT = os.path.join(DATA_ROOT, "teslausb")

# ─── 分区挂载点（A7Z NVMe 分区布局，默认值） ───
# 可通过 config/paths.json 在安装时按设备实际情况覆盖（不进版本库）。
PARTITIONS = {
    "cam": "/mnt/teslacam",          # TeslaCam 视频（哨兵/最近/保存片段）
    "music": "/mnt/music",            # 音乐文件
    "boombox": "/mnt/boombox",        # boombox 音频
    "lightshow": "/mnt/lightshow",    # lightshow 灯光秀
}

# ─── 可选：从 config/paths.json 覆盖挂载点（安装脚本写入） ───
_PATHS_OVERRIDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "paths.json")
if os.path.isfile(_PATHS_OVERRIDE):
    try:
        import json
        with open(_PATHS_OVERRIDE, "r", encoding="utf-8") as _f:
            _ov = json.load(_f)
        for _k, _v in _ov.items():
            if _k in PARTITIONS and isinstance(_v, str) and _v.strip():
                PARTITIONS[_k] = _v.strip().rstrip("/")
    except Exception as _e:  # noqa: BLE001
        print(f"[config] 读取 {_PATHS_OVERRIDE} 失败，使用默认挂载点：{_e}")

# ─── TeslaCam 子目录（依赖 PARTITIONS，须在覆盖之后定义） ───
SENTRY_CLIPS_PATH = os.path.join(PARTITIONS["cam"], "TeslaCam", "SentryClips")
RECENT_CLIPS_PATH = os.path.join(PARTITIONS["cam"], "TeslaCam", "RecentClips")
SAVED_CLIPS_PATH = os.path.join(PARTITIONS["cam"], "TeslaCam", "SavedClips")

# ─── 应用数据路径 ───
CONFIG_DIR = os.path.join(TESLAUSB_ROOT, "config")
DATA_DIR = os.path.join(TESLAUSB_ROOT, "data")
STATIC_DIR = os.path.join(TESLAUSB_ROOT, "static")
THUMBNAIL_DIR = os.path.join(STATIC_DIR, "thumbnails")

# ─── 关键文件路径 ───
# 哨兵状态文件与 sentry_service.py / config/sentry.json 保持一致
SENTRY_STATE_FILE = os.path.join(DATA_ROOT, "data", "sentry_events.json")
SENTRY_CONFIG_FILE = os.path.join(CONFIG_DIR, "sentry.json")
PUSH_HEALTH_FILE = os.path.join(DATA_DIR, "push_health.json")
APP_CONFIG_FILE = os.path.join(TESLAUSB_ROOT, "config.json")

# ─── 日志路径 ───
LOG_DIR = "/var/log"
WEB_LOG = os.path.join(LOG_DIR, "teslausb.log")
SENTRY_LOG = os.path.join(LOG_DIR, "teslausb-sentry.log")
GADGET_LOG = os.path.join(LOG_DIR, "teslausb-gadgetd.log")
WIFI_LOG = os.path.join(LOG_DIR, "wifi-smart-switch.log")

# ─── USB Gadget 路径 ───
GADGET_SCRIPT = "/opt/radxa_data/usb_gadget_init.sh"
GADGET_SOCKET = "/tmp/teslausb-gadget.sock"
GADGET_PID_FILE = "/var/run/teslausb-gadgetd.pid"

# ─── WiFi SSID 配置 ───
TESLA_SSID_PREFIX = "Tesla"  # Tesla 车机 WiFi SSID 前缀，用于门控启动

# ─── 微信通知器数据路径 ───
# 覆盖 weixin_notifier.py 的硬编码路径
os.environ.setdefault("WECOM_DATA_DIR", DATA_DIR)
