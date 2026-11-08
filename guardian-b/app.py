#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · HTTP 服务（B 端僚机主进程）
========================================
实现设计 §3 接口契约：

  POST /guard/heartbeat   心跳 + 预期离线握手（event=going_offline）
  GET  /guard/ping        A 侧反向心跳探活（判 B 是否活着）
  GET  /guard/status      只读状态页（默认需 token）
  GET  /healthz           无鉴权存活探针（供 OpenResty / 外部监控）

B 只做三件事：**收心跳 → 判死 → 发告警**（§1）。不做远程控制、不回调 A。

运行形态：单进程 Flask（judge / dead-man 以守护线程在进程内跑）。
公网 TLS 由 OpenResty 终结（自签证书 + A 侧 pinning，见 tls/gen_selfsigned.sh），
本服务默认只绑 127.0.0.1。
"""

import hmac
import ipaddress
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

from flask import Flask, jsonify, request

from config import as_bool, load_config
from state import StateStore
from alerts import Alerter
from judge import Judge

logger = logging.getLogger("guard-b")

# 升级失败时给 A 端的 HTTP 状态：其余一律 400（客户端/包的问题）
_ERR_STATUS = {
    "pkg_too_large": 413,
    "ssh_keygen_missing": 503,
    "no_pubkey": 503,
    "verifier_missing": 503,
}

VERSION = "1.1.2"


def _notify_code(alerter, cfg, job, code, version, ttl, src_ip):
    """把确认码推到用户手机（企微），**完全异步**。

    为什么必须异步：webhook 超时上限 10s + 重试 1 次，同步做会把 prepare 的响应
    拖到 20s 以上；A 端那边有自己的超时 —— 用户看到「预置失败」，而 B 其实已经
    staged 成功、码也生成了。这种「操作成功但界面报错」最难排查。

    失败**绝不**影响升级：本地日志（见 prepare 里的 logger.warning）永远是权威渠道，
    推送只是让「带外确认」从"登服务器捞日志"降到"手机看一眼"。

    安全性：码走 B → 企微服务器 → 用户手机，A 端既读不到群消息、也改不了这个开关
    （开关与 key 都在 root:guardb 0440 的 env 里）。所以它不削弱带外模型。
    """
    if not getattr(cfg, "code_notify", True):
        logger.info("确认码推送已关闭（GUARD_UPGRADE_CODE_NOTIFY=0），仅写本地日志")
        return
    try:
        ready = bool((alerter.status() or {}).get("configured"))
    except Exception:                                  # noqa: BLE001
        ready = False
    if not ready:
        logger.info("确认码未推送：企微 key/url 未配置（仅写本地日志）")
        return

    text = "\n".join([
        "【A7Z 守护 · 僚机升级确认】",
        "目标版本：v%s" % version,
        "确认码：%s" % code,
        "有效期：%d 秒（过期请重新预置）" % int(ttl),
        "任务号：%s" % (job or "")[:8],
        "来源：%s" % (src_ip or "?"),
        "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "在 A7Z「系统 → 版本管理 → 🛰 僚机（B 端）版本」输入上面的码才会切换。",
        "不是你发起的，请忽略，并检查 A 端是否被他人操作。",
    ])

    def _run():
        try:
            r = alerter.send_text(text)
            logger.warning("确认码已推送企微：%s job=%s", r, job)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("确认码推送企微异常（不影响升级）: %r", exc)

    try:
        threading.Thread(target=_run, daemon=True, name="guard-code-notify").start()
    except Exception as exc:                           # noqa: BLE001
        logger.warning("确认码推送线程启动失败（不影响升级）: %r", exc)

# 判死线程最近一次 tick 的墙钟（供升级后的健康检查判断"线程真的在跑"）。
# 只判端口通是不够的：新版本可能起来了但判死线程崩了，
# 那样"升级成功"会变成一次静默的守护失效。
_JUDGE_LAST_TICK = 0.0

# ── 验签库装载（**关键：不在升级包可覆盖范围内**）───────────────
# 生产环境从 /opt/guard-b/.tools 装载（root 0444，不随包升级）；
# 开发/测试环境回落到本文件同目录的副本。
_TOOLS_DIR = os.environ.get("GUARD_TOOLS_DIR") or "/opt/guard-b/.tools"
if os.path.isdir(_TOOLS_DIR) and _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
try:
    import upgrade as upgrade_lib
except Exception:                      # pragma: no cover - 缺库时降级为"功能不可用"
    upgrade_lib = None
    logger.error("升级校验库加载失败：远程升级功能将不可用（fail closed）")


# ── 日志 ────────────────────────────────────────────────────
def setup_logging(cfg) -> None:
    handlers = [logging.StreamHandler()]
    try:
        d = os.path.dirname(cfg.log_path)
        if d:
            os.makedirs(d, exist_ok=True)
        handlers.append(logging.FileHandler(cfg.log_path, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


# ── 应用工厂 ────────────────────────────────────────────────
def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask("guard-b")
    store = StateStore(cfg.state_path)
    alerter = Alerter(cfg.wecom_key, cfg.wecom_url, cfg.alert_dry_run)
    judge = Judge(cfg, store, alerter)
    app.config["GUARD"] = {"cfg": cfg, "store": store, "alerter": alerter, "judge": judge}

    # ── 鉴权 ────────────────────────────────────────────────
    def _authorized() -> bool:
        hdr = request.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return False
        got = hdr[7:].strip()
        return bool(cfg.token) and hmac.compare_digest(got, cfg.token)

    def _deny():
        logger.warning("鉴权失败 来自 %s 路径 %s", request.remote_addr, request.path)
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # ── 心跳 ────────────────────────────────────────────────
    @app.route("/guard/heartbeat", methods=["POST"])
    def heartbeat():
        if not _authorized():
            return _deny()
        channel = (request.headers.get("X-Guard-Channel") or "https").strip().lower()
        if channel not in ("ts", "https"):
            channel = "https"
        seq_raw = request.headers.get("X-Guard-Seq", "")
        try:
            seq = int(seq_raw) if seq_raw != "" else None
        except ValueError:
            seq = None
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        res = judge.on_heartbeat(channel, seq, body)
        return jsonify({"ok": True, "channel": channel, "result": res})

    # ── 反向心跳（A 探活 B）──────────────────────────────────
    @app.route("/guard/ping", methods=["GET"])
    def ping():
        if not _authorized():
            return _deny()
        return jsonify({"ok": True, "ts": int(time.time())})

    # ── 存活（无鉴权）────────────────────────────────────────
    @app.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify({"ok": True, "ts": int(time.time())})

    # ── 状态页 ──────────────────────────────────────────────
    @app.route("/guard/status", methods=["GET"])
    def status():
        if not cfg.public_status and not _authorized():
            return _deny()
        snap = store.snapshot()
        # 不把 token 之类敏感物带出（状态里本就没有）
        return jsonify({
            "ok": True,
            "version": VERSION,
            "now": int(time.time()),
            "classify": judge.classify(),
            "warn_s": cfg.warn_s,
            "alarm_s": cfg.alarm_s,
            "alert": alerter.status(),      # 只回 configured/key_tail/dry_run，不回明文
            "upgrade": _upgrade_brief(),    # A 端版本页据此显示「僚机 vX / 待确认」
            "judge_last_tick": _JUDGE_LAST_TICK,
            "state": snap,
        })

    # ── 配置（v1.0.2：供 A 端设置页下发机器人 key）───────────
    @app.route("/guard/config", methods=["GET"])
    def get_config():
        if not _authorized():
            return _deny()
        return jsonify({
            "ok": True,
            "version": VERSION,
            "alert": alerter.status(),
            "warn_s": cfg.warn_s,
            "alarm_s": cfg.alarm_s,
            "dedup_s": cfg.dedup_s,
            "offline_default_s": cfg.offline_default_s,
        })

    @app.route("/guard/config", methods=["POST"])
    def set_config():
        """只接受「可在线修改项」：wecom_key / alert_dry_run。

        刻意不允许改 warn_s / alarm_s / 端口 / token —— 那些在 env 里。
        两边都能改就会出现「谁生效说不清」，出问题只能靠猜。
        """
        if not _authorized():
            return _deny()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "body_must_be_object"}), 400

        rt = {}
        if os.path.exists(cfg.runtime_path):
            try:
                with open(cfg.runtime_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    rt = loaded
            except (OSError, ValueError):
                rt = {}

        changed = []
        key = data.get("wecom_key")
        if isinstance(key, str) and key.strip():
            k = key.strip()
            if len(k) < 20:
                return jsonify({"ok": False, "error": "wecom_key_too_short"}), 400
            rt["wecom_key"] = k
            alerter.set_key(k)
            changed.append("wecom_key")
        if "alert_dry_run" in data:
            v = as_bool(data.get("alert_dry_run"), alerter.dry_run)
            rt["alert_dry_run"] = v
            alerter.dry_run = v
            changed.append("alert_dry_run")

        if not changed:
            return jsonify({"ok": False, "error": "no_editable_field"}), 400

        # 原子写 + 0600：这个文件里有 webhook key
        parent = os.path.dirname(cfg.runtime_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".rt-", suffix=".json", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rt, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, cfg.runtime_path)
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

        logger.info("运行期配置已更新: %s", ",".join(changed))
        return jsonify({"ok": True, "version": VERSION,
                        "changed": changed, "alert": alerter.status()})

    # ── 远程升级（v1.1.0，默认关闭）──────────────────────────
    # 分层：本进程只做「收包 → 验签 → 暂存 → 记账」；
    # 切版本/重启由 root-owned 的 guardb-upgrade.service 执行，
    # 因为本进程被 systemd 沙箱限制为**对代码目录零写权限**。
    def _up_paths():
        d = cfg.upgrade_dir
        return {
            "pubkey": cfg.pubkey_path,
            "floor": cfg.floor_path,
            "staging": os.path.join(d, "staged"),
            "journal": os.path.join(d, "journal.json"),
            "secret": os.path.join(d, "secret"),
        }

    def _request_apply():
        """让 root helper 去切版本：写一个请求文件，由 `guardb-upgrade.path` 触发。

        **不用 sudo**：`guard-b.service` 开着 `NoNewPrivileges=yes`，
        而 NNP 会让 sudo 无法提权（setuid 被禁）——两条要求互斥，
        写 sudo 的结果是 100% 失败、journal 永久卡在 committed，后续每次升级 409。
        走 path unit 既保住 NNP，也不需要给 guardb 任何 sudo 权限。

        `GUARD_UPGRADE_APPLY_CMD` 非空时才走"直接执行命令"的老路径（需自行放弃 NNP）。
        """
        cmd = str(cfg.apply_cmd or "").strip()
        if cmd:
            _spawn_apply(cmd, _mark_apply_failed)
            return
        d = cfg.upgrade_dir
        try:
            os.makedirs(d, exist_ok=True)
            target = os.path.join(d, "request")
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            logger.warning("已投递升级请求（%s），等待 guardb-upgrade.path 触发", target)
        except OSError as exc:
            logger.exception("投递升级请求失败")
            _mark_apply_failed("request_write_failed: %r" % (exc,))

    def _journal():
        return upgrade_lib.Journal(_up_paths()["journal"])

    def _addr_ok():
        """只接受 Tailscale 网段（以及可选的本机回环）。不看任何 XFF 头。"""
        ip = (request.remote_addr or "").strip()
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if a.is_loopback:
            return cfg.allow_loopback
        try:
            return a in ipaddress.ip_network(cfg.ts_cidr, strict=False)
        except ValueError:
            # CIDR 配错就拒绝，不能因为"配置写坏了"而放行
            logger.error("GUARD_TS_CIDR 配置非法：%r", cfg.ts_cidr)
            return False

    def _up_guard():
        if not cfg.upgrade_enable:
            return jsonify({"ok": False, "error": "upgrade_disabled"}), 403
        if upgrade_lib is None:
            return jsonify({"ok": False, "error": "verifier_missing"}), 503
        if not _authorized():
            return _deny()
        if not _addr_ok():
            logger.warning("升级接口拒绝非 TS 来源 %s", request.remote_addr)
            return jsonify({"ok": False, "error": "addr_not_allowed"}), 403
        return None

    def _err(exc):
        st = _ERR_STATUS.get(getattr(exc, "code", ""), 400)
        logger.warning("升级包被拒：%s %s", exc.code, exc.detail)
        return jsonify({"ok": False, "error": exc.code, "detail": exc.detail}), st

    def _mark_apply_failed(reason):
        """把卡在 committed 的任务标成 failed。

        不这么做的话 journal 永远是 committed → 后续每次 prepare 都返回 409，
        用户只能手工清 journal 才能再升级（而且没人知道要去清）。
        """
        try:
            jj = _journal()
            if jj.data.get("state") == "committed":
                jj.set(state="failed", error=str(reason)[:200])
                logger.warning("已将卡住的升级任务标记为失败：%s", reason)
        except Exception:
            logger.exception("标记 journal 失败")

    def _upgrade_brief():
        """给 A 端展示的最小信息（不含确认码，确认码单独按开关给）。"""
        try:
            j = _journal()
        except Exception:
            return {"enabled": bool(cfg.upgrade_enable), "state": "unknown"}
        st = upgrade_lib.public_status(j) if upgrade_lib else {"state": "unavailable"}
        out = {
            "enabled": bool(cfg.upgrade_enable),
            "state": st.get("state"),
            "version": st.get("version"),
            "job": st.get("job"),
            "error": st.get("error"),
            "confirm_expires_in": st.get("confirm_expires_in"),
        }
        # 「已确认但一直没动静」必须能被看见：path unit 没装、helper 起不来、
        # path unit 没装、helper 起不来，都会表现为 journal 永远停在 committed。
        # 不显式标出来，用户只会看到"卡住"然后反复点。
        if st.get("state") == "committed":
            age = int(time.time()) - int((j.data or {}).get("updated_at") or 0)
            out["committed_for_s"] = age
            out["stalled"] = age > 900
        if cfg.code_via_api and st.get("state") == "verified" and upgrade_lib:
            try:
                sec = upgrade_lib._secret(_up_paths()["secret"])
                d = j.data or {}
                out["confirm_code"] = upgrade_lib.make_code(
                    sec, d.get("job", ""), d.get("pkg_sha256", ""))
            except Exception:
                pass
        return out

    @app.route("/guard/upgrade/prepare", methods=["POST"])
    def upgrade_prepare():
        bad = _up_guard()
        if bad:
            return bad
        j = _journal()
        if j.data.get("state") == "committed":
            return jsonify({"ok": False, "error": "upgrade_in_progress"}), 409

        # 长度检查必须在**碰 request.files 之前**：Flask 访问 files 时就会解析
        # 整个 multipart（body 已进内存/临时文件），放到后面等于白占一遍内存。
        if request.content_length and request.content_length > upgrade_lib.MAX_PKG_BYTES + 65536:
            return jsonify({"ok": False, "error": "pkg_too_large"}), 413
        f = request.files.get("pkg")
        if f is None:
            return jsonify({"ok": False, "error": "no_package"}), 400

        incoming = os.path.join(cfg.upgrade_dir, "incoming")
        os.makedirs(incoming, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="in-", suffix=".tar.gz", dir=incoming)
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = f.stream.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > upgrade_lib.MAX_PKG_BYTES:
                        raise upgrade_lib.UpgradeError("pkg_too_large")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())

            job, man, staged, pkg_sha = upgrade_lib.stage_package(tmp, _up_paths(), VERSION)
            exp = int(time.time()) + upgrade_lib.CONFIRM_TTL_S
            j.set(job=job, state="verified", version=man["version"],
                  from_version=VERSION, pkg_sha256=pkg_sha, staged_dir=staged,
                  code_exp=exp, confirm_attempts=0, error="")
            code = upgrade_lib.make_code(upgrade_lib._secret(_up_paths()["secret"]),
                                         job, pkg_sha)
            # 权威渠道 = 本机日志（A 端读不到）。API 是否也回码由 code_via_api 决定。
            logger.warning("升级包已校验并暂存：v%s job=%s 文件 %d 个；"
                           "确认码 %s（%d 秒内有效，A 端输入后才切换）",
                           man["version"], job, len(man["files"]), code,
                           upgrade_lib.CONFIRM_TTL_S)
            # 异步推企微（v1.1.2）：让"带外确认"不用登服务器。失败只记日志。
            _notify_code(alerter, cfg, job, code, man["version"],
                         upgrade_lib.CONFIRM_TTL_S, request.remote_addr)
            return jsonify({
                "ok": True, "job": job, "version": man["version"],
                "files": len(man["files"]), "bytes": size,
                "from_version": VERSION,
                "confirm_expires_in": upgrade_lib.CONFIRM_TTL_S,
                "confirm_code": code if cfg.code_via_api else "",
            }), 202
        except upgrade_lib.UpgradeError as exc:
            return _err(exc)
        except Exception as exc:                       # noqa: BLE001
            logger.exception("升级包处理异常")
            return jsonify({"ok": False, "error": "internal", "detail": repr(exc)[:200]}), 500
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @app.route("/guard/upgrade/confirm", methods=["POST"])
    def upgrade_confirm():
        bad = _up_guard()
        if bad:
            return bad
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "body_must_be_object"}), 400
        job = str(data.get("job") or "").strip()
        code = str(data.get("code") or "").strip()
        j = _journal()
        if not job or j.data.get("job") != job:
            return jsonify({"ok": False, "error": "job_mismatch"}), 409

        okc, reason = upgrade_lib.confirm(j, code, _up_paths()["secret"])
        if not okc:
            logger.warning("升级确认失败：%s（job=%s）", reason, job)
            return jsonify({"ok": False, "error": reason}), 403

        logger.warning("升级已确认，交 root helper 执行：v%s job=%s",
                       j.data.get("version"), job)
        _request_apply()
        return jsonify({"ok": True, "state": "committed", "job": job,
                        "version": j.data.get("version")}), 202

    @app.route("/guard/upgrade/status", methods=["GET"])
    def upgrade_status():
        # 这个接口在"未启用升级"时也要能读：A 端需要区分
        # 「B 没开升级功能」和「B 连不通」，两者处置完全不同。
        if not _authorized():
            return _deny()
        if upgrade_lib is None:
            return jsonify({"ok": False, "error": "verifier_missing"}), 503
        try:
            brief = _upgrade_brief()
        except Exception as exc:                       # noqa: BLE001
            return jsonify({"ok": False, "error": "journal_unreadable",
                            "detail": repr(exc)[:120]}), 500
        return jsonify({"ok": True, "version": VERSION, "upgrade": brief,
                        "ts_cidr": cfg.ts_cidr})

    # ── 后台线程：judge + dead-man ──────────────────────────
    if cfg.judge_enable:
        _start_judge_thread(cfg, judge)
    if cfg.deadman_url:
        _start_deadman_thread(cfg)

    return app


def _start_judge_thread(cfg, judge) -> None:
    def loop():
        global _JUDGE_LAST_TICK
        logger.info("judge 线程启动 interval=%ss T=%ss warn=%ss alarm=%ss",
                    cfg.judge_interval, cfg.heartbeat_interval, cfg.warn_s, cfg.alarm_s)
        while True:
            try:
                events = judge.tick()
                _JUDGE_LAST_TICK = time.time()
                for e in events:
                    if e.get("type") not in ("ok", "silent", "warn"):
                        logger.info("judge 事件: %s", e)
            except Exception:
                logger.exception("judge tick 异常（已吞，线程继续）")
            time.sleep(cfg.judge_interval)

    t = threading.Thread(target=loop, name="guard-judge", daemon=True)
    t.start()


def _start_deadman_thread(cfg) -> None:
    import requests

    def loop():
        logger.info("dead-man 线程启动 interval=%ss", cfg.deadman_interval)
        while True:
            try:
                requests.get(cfg.deadman_url, timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dead-man 打点失败: %r", exc)
            time.sleep(cfg.deadman_interval)

    t = threading.Thread(target=loop, name="guard-deadman", daemon=True)
    t.start()


def _spawn_apply(cmd, mark_failed=None) -> None:
    """触发 root helper（切版本/重启）。不 shell=True：命令写死在 env，不允许拼接。

    **必须检查返回值**：这条支路只在 `GUARD_UPGRADE_APPLY_CMD` 非空时才会走到
    （默认走 path unit，见 `_request_apply`）。命令失败时 journal 会永远停在
    committed，之后每次 prepare 都 409 —— 用户看到"升级卡住"，日志里却什么都没有。
    """
    def run():
        argv = str(cmd or "").split()
        if not argv:
            logger.error("GUARD_UPGRADE_APPLY_CMD 为空，无法执行升级")
            if mark_failed:
                mark_failed("apply_cmd_empty")
            return
        try:
            r = subprocess.run(argv, capture_output=True, timeout=600)
        except Exception as exc:                        # noqa: BLE001
            logger.exception("apply helper 启动失败")
            if mark_failed:
                mark_failed(repr(exc)[:160])
            return
        if r.returncode != 0:
            err = (r.stderr or b"")[:300].decode("utf-8", "replace").strip()
            logger.error("apply helper 失败 rc=%s %s（检查 GUARD_UPGRADE_APPLY_CMD "
                         "与执行权限）", r.returncode, err)
            if mark_failed:
                mark_failed("apply_cmd_failed rc=%s %s" % (r.returncode, err))
            return
        logger.info("apply helper 已提交")

    threading.Thread(target=run, name="guard-apply", daemon=True).start()


def main() -> int:
    cfg = load_config()
    setup_logging(cfg)
    problems = cfg.validate()
    for p in problems:
        logger.warning("配置告警: %s", p)
    logger.info("guardian-b 启动 bind=%s:%s judge=%s",
                cfg.bind_host, cfg.bind_port, cfg.judge_enable)
    if cfg.state_path and os.path.exists(cfg.state_path):
        logger.info("已加载状态文件 %s", cfg.state_path)
    app = create_app(cfg)
    app.config["GUARD"]["store"].mark_boot()
    app.run(host=cfg.bind_host, port=cfg.bind_port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
