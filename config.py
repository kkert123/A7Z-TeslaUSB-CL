#!/usr/bin/env python3
"""
A7Z TeslaUSB 统一配置文件
===========================
所有模块应从此文件导入路径常量，避免路径碎片化。
"""

import os

# ─── 应用版本号 ───
APP_VERSION = "0.1.8"

# ─── 升级系统 Ed25519 公钥（私钥 upgrade_key 本地保管，不进仓库） ───
UPGRADE_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKeYRpMBX5sn0tsR+IRuwtUbI6qWu+5VTcK4NWL2AOt6 a7z-upgrade"

# ─── 数据根目录 ───
DATA_ROOT = "/opt/radxa_data"
TESLAUSB_ROOT = os.path.join(DATA_ROOT, "teslausb")

# ─── 分区挂载点（A7Z NVMe 分区布局） ───
PARTITIONS = {
    "cam": "/mnt/teslacam",
    "music": "/mnt/music",
    "boombox": "/mnt/boombox",
    "lightshow": "/mnt/lightshow",
}

# ─── TeslaCam 子目录 ───
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
