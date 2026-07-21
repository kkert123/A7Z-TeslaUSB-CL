#!/usr/bin/env python3
"""
Auto Present Service — Edit 模式超时自动切回 Present
=====================================================

管理 Edit 模式倒计时：切换到 Edit 后启动定时器，超时后自动执行
Present 切换。配置存储在 data/auto_present.json。
"""

import os
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("AutoPresent")

# 配置文件路径
CONFIG_FILE = Path(__file__).resolve().parent / "data" / "auto_present.json"

# 默认配置
DEFAULT_CONFIG = {
    "enabled": True,
    "timeout_minutes": 5,
}

# 全局状态
_config_cache: Optional[dict] = None
_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()
_countdown_end: float = 0.0  # Unix timestamp when countdown expires


def _load_config() -> dict:
    """加载配置（含默认值）"""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            config.update(loaded)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"加载 auto_present 配置失败: {e}")
    return config


def _save_config(config: dict) -> bool:
    """保存配置到文件"""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        global _config_cache
        _config_cache = dict(config)
        return True
    except Exception as e:
        logger.error(f"保存 auto_present 配置失败: {e}")
        return False


def get_config() -> dict:
    """获取当前配置（含缓存）"""
    global _config_cache
    if _config_cache is None:
        _config_cache = _load_config()
    return dict(_config_cache)


def update_config(enabled: bool = None, timeout_minutes: int = None) -> dict:
    """更新配置项，返回完整配置"""
    config = _load_config()
    changed = False
    if enabled is not None:
        config["enabled"] = bool(enabled)
        changed = True
    if timeout_minutes is not None:
        tm = int(timeout_minutes)
        tm = max(1, min(tm, 60))  # 钳制 1-60 分钟
        config["timeout_minutes"] = tm
        changed = True
    if changed:
        _save_config(config)
    return config


def get_remaining_seconds() -> int:
    """获取当前倒计时剩余秒数（用于前端显示）"""
    global _countdown_end
    remaining = int(_countdown_end - time.time())
    return max(0, remaining)


def get_status() -> dict:
    """获取完整状态（供 API 返回）"""
    config = get_config()
    remaining = get_remaining_seconds()
    return {
        "enabled": config["enabled"],
        "timeout_minutes": config["timeout_minutes"],
        "remaining_seconds": remaining,
        "active": remaining > 0,
    }


def _do_switch_to_present():
    """实际执行切换到 Present 模式"""
    global _countdown_end
    _countdown_end = 0.0
    logger.info("⏰ Edit 模式超时，自动切换到 Present...")
    try:
        import subprocess
        script = "/opt/radxa_data/teslausb/usb_gadget_init.sh"
        result = subprocess.run(
            ["sudo", "-n", "bash", script, "start"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            # 尝试直接运行
            result = subprocess.run(
                ["bash", script, "start"],
                capture_output=True, text=True, timeout=60,
            )
        if result.returncode == 0:
            with open("/tmp/teslausb_mode", "w") as f:
                f.write("present")
            logger.info("✅ 自动切换到 Present 成功")
        else:
            logger.error(f"❌ 自动切换到 Present 失败: {result.stderr[:200]}")
    except Exception as e:
        logger.error(f"❌ 自动切换异常: {e}")


def start_countdown():
    """启动倒计时（在切换到 Edit 模式后调用）"""
    global _timer, _countdown_end
    config = get_config()
    if not config["enabled"]:
        logger.debug("Auto Present 已禁用，不启动倒计时")
        return

    with _timer_lock:
        # 取消已有定时器
        if _timer is not None:
            _timer.cancel()
            _timer = None

        timeout_sec = config["timeout_minutes"] * 60
        _countdown_end = time.time() + timeout_sec
        _timer = threading.Timer(timeout_sec, _do_switch_to_present)
        _timer.daemon = True
        _timer.start()
        logger.info(f"⏱ Auto Present 倒计时已启动: {config['timeout_minutes']} 分钟")


def cancel_countdown():
    """取消倒计时（在切换到 Present 模式或手动取消时调用）"""
    global _timer, _countdown_end
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None
            logger.info("⏹ Auto Present 倒计时已取消")
        _countdown_end = 0.0
