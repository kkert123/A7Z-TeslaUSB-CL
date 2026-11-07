#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 配置装载
======================
B 端（僚机）全部配置来自**环境变量 / .env**（不进镜像、不入仓库）。

对应设计：docs/explanation/自检守护-B端VPS设计.md §6 / §7。

命名前缀统一 `GUARD_`，避免与系统变量冲突。
"""

import os

try:
    # 可选：本地 .env（部署目录用），缺失不影响
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:  # pragma: no cover - dotenv 可选
    pass


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


class Config:
    """B 端运行配置（进程启动时一次性读取）。"""

    def __init__(self) -> None:
        # ── 鉴权 ─────────────────────────────────────────────
        # v1：静态 Bearer token（≥32 字节随机）。见 §8。
        self.token: str = _env_str("GUARD_TOKEN", "")

        # ── 监听 ─────────────────────────────────────────────
        # 公网由 OpenResty 终结 TLS 后反代到本地址（默认仅 loopback）。
        self.bind_host: str = _env_str("GUARD_BIND_HOST", "127.0.0.1")
        self.bind_port: int = _env_int("GUARD_BIND_PORT", 8800)

        # ── 判死阈值（§4：WARN=3T, ALARM=5T, T=60s）────────────
        self.heartbeat_interval: int = _env_int("GUARD_T", 60)
        self.warn_mult: int = _env_int("GUARD_WARN_MULT", 3)
        self.alarm_mult: int = _env_int("GUARD_ALARM_MULT", 5)
        self.judge_interval: int = _env_int("GUARD_JUDGE_INTERVAL", 30)

        # ── 预期离线（§3.3）────────────────────────────────────
        # going_offline 缺省窗口长度（秒），A 未带 expected_until 时兜底。
        self.offline_default_s: int = _env_int("GUARD_OFFLINE_DEFAULT_S", 3600)

        # ── 告警去重（§4：同 (fault,channel) 30min 内只推一次）──
        self.dedup_s: int = _env_int("GUARD_DEDUP_S", 1800)

        # ── 未知车态策略 ──────────────────────────────────────
        # 最后一次心跳里 vehicle 不足以判定「该在线」时：
        #   judge  = 仍按阈值判死（默认；宁可告警）
        #   silent = 静默（保守；宁可漏报）
        self.unknown_vehicle: str = _env_str("GUARD_UNKNOWN_VEHICLE", "judge").lower()

        # ── 状态与日志 ────────────────────────────────────────
        self.state_path: str = _env_str("GUARD_STATE_PATH", "/opt/guard-b/data/state.json")
        self.log_path: str = _env_str("GUARD_LOG_PATH", "/opt/guard-b/data/guard-b.log")

        # ── 告警通道（企业微信机器人）──────────────────────────
        self.wecom_key: str = _env_str("GUARD_WECOM_KEY", "")
        self.wecom_url: str = _env_str("GUARD_WECOM_URL", "")
        self.alert_dry_run: bool = _env_str("GUARD_ALERT_DRY_RUN", "0") in ("1", "true", "yes")

        # ── 外部 dead-man（§10 R5；healthchecks.io / Uptime-Kuma）──
        self.deadman_url: str = _env_str("GUARD_DEADMAN_URL", "")
        self.deadman_interval: int = _env_int("GUARD_DEADMAN_INTERVAL", 3600)

        # ── 行为开关 ──────────────────────────────────────────
        self.judge_enable: bool = _env_str("GUARD_JUDGE_ENABLE", "1") in ("1", "true", "yes")
        self.public_status: bool = _env_str("GUARD_PUBLIC_STATUS", "0") in ("1", "true", "yes")

    # 便捷派生值
    @property
    def warn_s(self) -> int:
        return self.warn_mult * self.heartbeat_interval

    @property
    def alarm_s(self) -> int:
        return self.alarm_mult * self.heartbeat_interval

    def validate(self) -> list:
        """返回致命配置问题列表（空 = 通过）。"""
        problems = []
        if not self.token or len(self.token) < 16:
            problems.append("GUARD_TOKEN 缺失或过短（建议 ≥32 字节随机）")
        if self.warn_mult >= self.alarm_mult:
            problems.append("GUARD_WARN_MULT 必须 < GUARD_ALARM_MULT")
        if self.unknown_vehicle not in ("judge", "silent"):
            problems.append("GUARD_UNKNOWN_VEHICLE 只能是 judge|silent")
        if not self.wecom_key and not self.wecom_url and not self.alert_dry_run:
            problems.append("未配置微信告警（GUARD_WECOM_KEY/URL）且未开 DRY_RUN——告警将无处投递")
        return problems


def load_config() -> Config:
    return Config()
