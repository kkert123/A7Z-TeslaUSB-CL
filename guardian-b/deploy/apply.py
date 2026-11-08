#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardb-upgrade · 应用已验签的升级包（root，oneshot）
=====================================================
由 `guardb-upgrade.service` 以 **root** 身份执行，是整条链路上**唯一**
能写 `/opt/guard-b/releases` 的进程。

为什么要有这个独立进程
----------------------
Flask 进程（`guardb` 用户）被 systemd 沙箱限制为对代码目录**零写权限**。
哪怕它被 RCE，也只能改 `data/`，改不了自己下一轮要执行的代码。
这就是"本地锁死"的落地方式——不是靠约定，是靠内核权限。

信任链（每一环都必须成立）
--------------------------
  `data/upgrade/staged/v-<ver>-<job>/manifest.json`（离线 Ed25519 签名）
    → **本文件重新验签**（不信任"上游验过了"）
      → 逐文件核 sha256（暂存目录对 guardb 可写，验签到落地有时间窗口）
        → 写 releases/<ver>/ → 原子切 symlink → 重启 → 语义健康检查
          → 失败则切回旧版本（§6.3）

路径全部来自**推导**而不是账本
------------------------------
`journal.json` 在 `data/` 下，guardb 用户可写。若直接拿它里面的
`staged_dir` / `files[].path` 去 join 再删/写，就等于给了一个
"让 root 读写任意路径"的接口（2026-11-08 审查实证）。
所以：staged 目录由 `version + job` 推导并做 realpath 前缀校验；
文件名必须匹配 `^[A-Za-z0-9_.-]+\\.py$` 且 basename 与全名相同。

断电安全
--------
任意一步断电后重跑本脚本即可：
  · journal 停在 committed → 走正常流程（幂等：release 目录已存在就重建）
  · journal 停在 committed 且 current 已指向新版本 → 只跑健康检查，通过即 healthy
  · releases/<ver> 存在但 current 未切换 → 重新切
旧版本目录**永不提前删除**（保留 current + 2 代 + 本次的 from_version）。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

DEST = os.environ.get("GUARD_HOME", "/opt/guard-b")
ENV_FILE = os.path.join(DEST, "guard-b.env")
TRUSTED = os.path.join(DEST, ".trusted")

VER_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
JOB_RE = re.compile(r"^[0-9a-f]{8,64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.py$")
KEEP_GENERATIONS = 3
SIGNER_IDENTITY = "a7z-guard"
SIGN_NAMESPACE = "a7z-guard-pkg"


def log(msg, *args):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " [guardb-upgrade] " + (msg % args if args else msg)
    print(line, flush=True)


def env_get(key, default=""):
    """从 guard-b.env 读一个键（不 source，避免执行任意代码）。"""
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data, mode=0o600):
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def swap_symlink(cur, target):
    """原子切换：先建临时链接再 replace（同一目录内 rename 是原子的）。"""
    tmp = cur + ".swap"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, cur)


def ver_tuple(s):
    try:
        return tuple(int(x) for x in str(s).split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)


# ── 校验 ─────────────────────────────────────────────────────
def load_and_verify_staged(staged):
    """**一次读取** manifest 字节 → 用这份字节验签 → 用同一份字节解析。

    不能"验签时读一次、解析时再读一次"：staged 目录对 guardb 可写，
    两次读之间它可以把文件在「合法清单」和「改过 sha 的清单」之间来回换，
    多试几次就能命中"验的是好的、用的是坏的"（TOCTOU，审查实证）。
    返回 (ok, manifest_dict, reason)。
    """
    man_p = os.path.join(staged, "manifest.json")
    sig_p = os.path.join(staged, "manifest.json.sig")
    pub_p = os.path.join(TRUSTED, "pkg.pub")
    for p, what in ((man_p, "manifest"), (sig_p, "signature"), (pub_p, "公钥")):
        if not os.path.isfile(p):
            return False, None, "缺 %s（%s）" % (what, p)

    if shutil.which("ssh-keygen") is None:
        return False, None, "宿主机缺 ssh-keygen"

    try:
        fd = os.open(man_p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            man_raw = f.read(256 * 1024)
    except OSError as exc:
        return False, None, "读 manifest 失败：%r" % exc

    try:
        with open(pub_p, "r", encoding="utf-8") as f:
            allowed_txt = f.read().strip()
        lines = [l.strip() for l in allowed_txt.splitlines() if l.strip()]
        if not lines or lines[0].split(None, 1)[0].strip() != SIGNER_IDENTITY:
            return False, None, "公钥 principal 不是 %s" % SIGNER_IDENTITY
        fd2, tmp = tempfile.mkstemp(prefix=".al-")
        try:
            with os.fdopen(fd2, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(tmp, 0o600)
            r = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", tmp, "-I", SIGNER_IDENTITY,
                 "-n", SIGN_NAMESPACE, "-s", sig_p],
                input=man_raw, capture_output=True, timeout=30)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, "验签执行失败：%r" % exc
    if r.returncode != 0:
        return False, None, "签名不符：" + (r.stderr or b"").decode("utf-8", "replace")[:120]

    try:
        man = json.loads(man_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return False, None, "manifest 解析失败：%r" % exc
    if not isinstance(man, dict):
        return False, None, "manifest 不是对象"
    return True, man, ""


def safe_target(root, name):
    """把清单里的文件名安全地变成绝对路径。

    清单是**可被篡改的输入**（staged 对 guardb 可写）。`os.path.join(new, "/etc/x")`
    会直接返回 `/etc/x`；`removes` 里写 `../../x.py` 就能让 root 删任意文件。
    所以：形状校验 → basename 校验 → 父目录 realpath 必须是 root 本身 → 叶子不能是链接。
    """
    s = str(name)
    if not NAME_RE.match(s) or os.path.basename(s) != s:
        raise RuntimeError("非法文件名：%r" % s)
    root_real = os.path.realpath(root)
    p = os.path.join(root_real, s)
    if os.path.realpath(os.path.dirname(p)) != root_real:
        raise RuntimeError("路径逃逸：%r" % s)
    if os.path.islink(p):
        # 不让 root 顺着一个符号链接去覆盖别处的文件（例如 releases/x/app.py -> /etc/shadow）
        raise RuntimeError("目标是符号链接：%r" % s)
    return p


def read_verified_file(src_p, want, limit=16 * 1024 * 1024):
    """**一次读取**：用读到的这坨字节算 hash，也用这坨字节落盘。

    先 hash 再从路径复制一遍，中间有窗口可换（TOCTOU）。
    这里把"验过的字节"和"写下的字节"变成同一份内存数据。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(src_p, flags)
    except OSError as exc:
        raise RuntimeError("打不开暂存文件 %s：%r" % (os.path.basename(src_p), exc))
    with os.fdopen(fd, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("文件过大：%s" % os.path.basename(src_p))
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise RuntimeError("暂存文件被改动：%s 期望 %s 实际 %s"
                           % (os.path.basename(src_p), want[:12], got[:12]))
    return data


# ── 健康检查（验语义，不验端口）────────────────────────────────
def http_get(path, host, port, token, timeout=5):
    url = "http://%s:%s%s" % (host, port, path)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(65536).decode("utf-8"))


def health_check(want_version, host, port, token, budget_s=60):
    """返回 (ok, reason)。必须同时满足：活着 + 版本对 + 状态可读 + 判死线程在跑。

    只等端口通是不够的 —— 新版本可能起来了但判死线程崩了，
    那种情况"健康"会让一次坏升级变成静默故障（守护没了就是没了）。
    """
    deadline = time.time() + budget_s
    last = "no_attempt"
    while time.time() < deadline:
        try:
            hz = http_get("/healthz", host, port, "")
            if not hz.get("ok"):
                last = "healthz_not_ok"
            else:
                st = http_get("/guard/status", host, port, token)
                tick = st.get("judge_last_tick")
                if st.get("version") != want_version:
                    last = "version=%s want=%s" % (st.get("version"), want_version)
                elif not st.get("ok"):
                    last = "status_not_ok"
                elif st.get("classify") is None:
                    last = "classify_missing"
                elif isinstance(tick, (int, float)) and 0 < tick and time.time() - tick > 180:
                    # 判死线程停摆比"端口不通"更隐蔽：服务活着但不再告警。
                    # tick==0 表示这个版本没有该字段或判死被关掉（judge_enable=0），不据此判死。
                    last = "judge_stalled(%ds)" % int(time.time() - tick)
                else:
                    return True, "ok"
        except Exception as exc:                       # noqa: BLE001
            last = repr(exc)[:120]
        time.sleep(3)
    return False, last


def restart_unit(unit):
    """restart 前先 reset-failed。

    新版本若连续崩 3 次会撞上 StartLimitBurst → unit 进 failed；
    此时 `systemctl restart` 会**失败**，于是"自动回滚"也起不来，
    服务彻底停摆。必须先清掉失败计数（审查实证）。
    """
    subprocess.run(["systemctl", "reset-failed", unit], check=False, timeout=30)
    r = subprocess.run(["systemctl", "restart", unit], capture_output=True, timeout=180)
    if r.returncode != 0:
        return False, (r.stderr or b"").decode("utf-8", "replace")[:160]
    return True, ""


# ── 落地 ─────────────────────────────────────────────────────
def build_release(releases, staged, version, from_version, manifest):
    """new_release = copy(current) + overlay(staged) - removes。

    增量式：只覆盖包里带的文件，其余沿用当前版本（那些文件已经可信）。
    失败时**清理半成品**：留一个"构建到一半"的 release 目录会让人误以为升级过。
    """
    src = os.path.join(releases, from_version)
    if not os.path.isdir(src):
        raise RuntimeError("当前版本目录不存在：%s" % src)
    new = os.path.join(releases, version)
    if os.path.isdir(new):
        shutil.rmtree(new)                  # 上次半途失败留下的，重建
    shutil.copytree(src, new, symlinks=False)
    try:
        return _fill_release(new, staged, files_of(manifest), manifest)
    except Exception:
        shutil.rmtree(new, ignore_errors=True)
        raise


def files_of(manifest):
    files = manifest.get("files") or []
    if not isinstance(files, list) or not files:
        raise RuntimeError("清单里没有文件")
    return files


def _fill_release(new, staged, files, manifest):
    # 先全部校验并读进内存（一次性失败，避免"写了一半才发现某个名字不合法"）
    plan = []
    for item in files:
        name = str(item.get("path"))
        target = safe_target(new, name)
        mode = str(item.get("mode") or "0644")
        if mode not in ("0644", "0755"):
            raise RuntimeError("非法文件权限：%s=%s" % (name, mode))
        src_p = safe_target(staged, name)
        if not os.path.isfile(src_p):
            raise RuntimeError("暂存文件丢失：%s" % name)
        data = read_verified_file(src_p, str(item.get("sha256") or ""))
        plan.append((name, target, data, mode, str(item.get("sha256") or "")))

    removes = []
    for name in (manifest.get("removes") or []):
        removes.append(safe_target(new, name))

    for name, target, data, mode, _want in plan:
        with open(target, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(target, int(mode, 8))
    for p in removes:
        if os.path.isfile(p):
            os.remove(p)

    with open(os.path.join(new, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 落地后再核一次（写下的字节必须与清单一致）
    for name, target, _data, _mode, want in plan:
        if sha256_file(target) != want:
            raise RuntimeError("落地后校验失败：%s" % name)
    return new


def prune(releases, keep_versions):
    try:
        names = [n for n in os.listdir(releases)
                 if VER_RE.match(n) and os.path.isdir(os.path.join(releases, n))]
    except OSError:
        return
    names.sort(key=ver_tuple, reverse=True)
    keep = set(names[:KEEP_GENERATIONS]) | set(keep_versions)
    for n in names:
        if n not in keep:
            shutil.rmtree(os.path.join(releases, n), ignore_errors=True)
            log("清理旧版本目录 %s", n)


def resolve_staged(version, job):
    """由 version + job **推导**暂存目录，不采信 journal 里的现成路径。"""
    base = os.path.realpath(os.path.join(DEST, "data", "upgrade", "staged"))
    p = os.path.realpath(os.path.join(base, "v-%s-%s" % (version, job)))
    if not p.startswith(base + os.sep):
        raise RuntimeError("暂存路径越界：%r" % p)
    return p


def fail(journal_path, j, code):
    j.update(state="failed", error=code, updated_at=int(time.time()))
    write_json(journal_path, j)
    log("标记失败：%s", code)
    return 1


def main():
    """正常结束就清掉触发文件；被硬杀（断电）时文件还在 → 开机后自动重试。

    这正是我们想要的两面性：跑完别重复触发，跑一半必须能续上。
    """
    try:
        return _run()
    finally:
        try:
            os.remove(os.path.join(DEST, "data", "upgrade", "request"))
        except OSError:
            pass


def _run():
    journal_path = os.path.join(DEST, "data", "upgrade", "journal.json")
    req_path = os.path.join(DEST, "data", "upgrade", "request")
    j = read_json(journal_path)
    state = j.get("state")
    if state != "committed":
        log("journal 状态为 %r，无需操作", state)
        # 顺手清掉触发文件，否则 path unit 会在下次启动时再跑一遍
        for p in (req_path,):
            try:
                os.remove(p)
            except OSError:
                pass
        return 0

    version = str(j.get("version") or "")
    from_version = str(j.get("from_version") or "")
    job = str(j.get("job") or "")
    if not VER_RE.match(version):
        return fail(journal_path, j, "bad_version_in_journal")
    if not JOB_RE.match(job):
        return fail(journal_path, j, "bad_job_in_journal")

    cur = os.path.join(DEST, "current")
    releases = os.path.join(DEST, "releases")
    if os.path.isdir(cur) and not os.path.islink(cur):
        return fail(journal_path, j, "legacy_layout")   # 需先跑 install.sh 迁移

    # 起跳版本以 **current 实际指向**为准，其次才是账本：
    # 账本在 data/ 下可被 guardb 写，而 current 是 root 维护的。
    cur_from = ""
    if os.path.islink(cur):
        cur_from = os.path.basename(os.readlink(cur))
    if VER_RE.match(cur_from):
        if from_version and from_version != cur_from:
            log("注意：账本 from=%s 与 current=%s 不一致，以 current 为准",
                from_version, cur_from)
        from_version = cur_from
    if not VER_RE.match(from_version):
        return fail(journal_path, j, "bad_from_version")

    unit = env_get("GUARD_UNIT", "guard-b")
    host = env_get("GUARD_BIND_HOST", "100.111.252.121")
    port = env_get("GUARD_BIND_PORT", "8899")
    token = env_get("GUARD_TOKEN", "")

    staged = resolve_staged(version, job)
    target = os.path.join(releases, version)

    # 断点续跑：current 已指向目标版本且目录在 → 只补健康检查
    already = (os.path.islink(cur)
               and os.path.basename(os.readlink(cur)) == version
               and os.path.isdir(target))

    try:
        if not already:
            if not os.path.isdir(staged):
                return fail(journal_path, j, "staged_missing")
            ok, manifest, why = load_and_verify_staged(staged)
            if not ok:
                return fail(journal_path, j, "sig_invalid: " + why)
            if str(manifest.get("version")) != version:
                return fail(journal_path, j, "manifest_version_mismatch")
            allow = [str(x) for x in (manifest.get("from") or [])]
            if allow and from_version not in allow:
                return fail(journal_path, j,
                            "from_not_allowed: %s not in %s" % (from_version, allow))

            log("构建 release %s（from %s）", version, from_version)
            build_release(releases, staged, version, from_version, manifest)

            prev = os.path.realpath(cur) if os.path.islink(cur) else "-"
            log("切换 current → %s（旧：%s）", version, prev)
            swap_symlink(cur, target)
        else:
            log("current 已指向 %s，继续健康检查", version)

        okr, whyr = restart_unit(unit)
        if not okr:
            log("重启失败：%s", whyr)

        ok, why = health_check(version, host, port, token)
        if ok:
            j.update(state="healthy", error="", updated_at=int(time.time()))
            write_json(journal_path, j)
            floor = os.path.join(TRUSTED, "floor")
            try:
                os.makedirs(TRUSTED, exist_ok=True)
                with open(floor, "w", encoding="utf-8") as f:
                    f.write(version + "\n")
                os.chmod(floor, 0o444)
            except OSError as exc:
                log("写版本地板失败（不影响本次升级）：%r", exc)
            prune(releases, (version, from_version))
            if os.path.isdir(staged):
                shutil.rmtree(staged, ignore_errors=True)
            log("升级完成：%s → %s", from_version, version)
            return 0

        # ── 失败：回滚 ──
        log("健康检查失败（%s），回滚到 %s", why, from_version)
        old = os.path.join(releases, from_version)
        if not os.path.isdir(old):
            j.update(state="failed", error="rollback_target_missing: " + why,
                     updated_at=int(time.time()))
            write_json(journal_path, j)
            log("回滚目标不存在：%s（需人工介入）", old)
            return 4
        swap_symlink(cur, old)
        okr2, whyr2 = restart_unit(unit)
        ok2, why2 = health_check(from_version, host, port, token, budget_s=45)
        j.update(state="rolled_back",
                 error="healthcheck_failed: " + why,
                 rollback_ok=bool(ok2 and okr2),
                 rollback_note="" if (ok2 and okr2) else ("restart=%s health=%s" % (whyr2, why2)),
                 updated_at=int(time.time()))
        write_json(journal_path, j)
        log("已回滚到 %s（健康检查 %s：%s）", from_version,
            "通过" if (ok2 and okr2) else "仍失败", why2)
        return 0 if (ok2 and okr2) else 5
    except Exception as exc:                            # noqa: BLE001
        log("升级异常：%r", exc)
        j.update(state="failed", error=repr(exc)[:200], updated_at=int(time.time()))
        write_json(journal_path, j)
        return 6


if __name__ == "__main__":
    sys.exit(main())
