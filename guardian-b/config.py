#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 配置装载
======================
B 端（僚机）全部配置来自**环境变量 / .env**（不进镜像、不入仓库）。

对应设计：docs/explanation/自检守护-B端VPS设计.md §6 / §7。

命名前缀统一 `GUARD_`，避免与系统变量冲突。
"""

import json
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


def as_bool(value, default: bool = False) -> bool:
    """把 runtime.json（手写/转发来的）里的值安全转成 bool。

    `bool("false") == True` 是这类配置最经典的坑：用户写 `"false"` 以为关了，
    实际打开了。这里只认明确的白名单，其余回落默认值。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
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

        # ── 运行期覆盖（由 POST /guard/config 写入，0600）──────
        self.runtime_path: str = _env_str("GUARD_RUNTIME_PATH", "/opt/guard-b/data/runtime.json")
        self._apply_runtime()

        # ── 远程升级（v1.1.0，**默认关闭**）────────────────────
        # 关闭时所有 /guard/upgrade/* 一律 403：这不是"配置项"，是安全默认值。
        # 打开需要改 root-owned 的 env 文件（见 docs §5.2）。
        self.upgrade_enable: bool = _env_str("GUARD_UPGRADE_ENABLE", "0") in ("1", "true", "yes")
        self.upgrade_dir: str = _env_str("GUARD_UPGRADE_DIR", "/opt/guard-b/data/upgrade")
        self.pubkey_path: str = _env_str("GUARD_UPGRADE_PUBKEY", "/opt/guard-b/.trusted/pkg.pub")
        self.floor_path: str = _env_str("GUARD_UPGRADE_FLOOR", "/opt/guard-b/.trusted/floor")
        self.releases_dir: str = _env_str("GUARD_RELEASES_DIR", "/opt/guard-b/releases")
        # 只允许 TS 网段访问升级接口（默认 100.64.0.0/10 = Tailscale CGNAT）
        self.ts_cidr: str = _env_str("GUARD_TS_CIDR", "100.64.0.0/10")
        self.allow_loopback: bool = _env_str("GUARD_UPGRADE_ALLOW_LOOPBACK", "1") in ("1", "true", "yes")
        # 确认码是否也通过 /guard/status 暴露：
        #   0 = 只能看 `journalctl -u guard-b` / data/guard-b.log（**真正的带外确认**，默认）
        #   1 = 便利模式：A 端能自动读到码 → 但这也意味着"拿到 A 的攻击者能全自动完成"，
        #       等于把人工确认降级成"人看一眼"。打开前请确认你接受这个折中。
        self.code_via_api: bool = _env_str("GUARD_UPGRADE_CODE_VIA_API", "0") in ("1", "true", "yes")
        # 触发 root helper 的方式。默认**留空** = 写请求文件，
        # 由 `guardb-upgrade.path` 监听后拉起 `guardb-upgrade.service`。
        #
        # 为什么不用 `sudo systemctl start ...`：`guard-b.service` 开了
        # `NoNewPrivileges=yes`（本地锁死的前提），而 NNP 会禁止 sudo 提权 ——
        # 两者互斥，写 sudo 的结果是 100% 失败（审查实证）。
        # 只有在你明确放弃 NNP 时才把它设成一条命令。
        self.apply_cmd: str = _env_str("GUARD_UPGRADE_APPLY_CMD", "")

    def _apply_runtime(self) -> None:
        """runtime.json 覆盖 env 中的「可在线修改项」。优先级：runtime > env > 默认。

        为什么必须落盘覆盖：设置页保存后若只改内存，B 一重启就变回 env 的旧 key，
        而 UI 上看起来还是"已配置"——两端不一致且没有任何提示，是最难查的一类。
        """
        try:
            with open(self.runtime_path, "r", encoding="utf-8") as f:
                rt = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(rt, dict):
            return
        key = rt.get("wecom_key")
        if isinstance(key, str) and key.strip():
            self.wecom_key = key.strip()
        if "alert_dry_run" in rt:
            self.alert_dry_run = as_bool(rt.get("alert_dry_run"), self.alert_dry_run)

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
