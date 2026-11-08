#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 升级包校验单测（真实 ssh-keygen 签名链路，不桩）
==============================================================
覆盖 docs/explanation/守护-B端版本同步-设计评审.md §4 / §6 的每一条拒绝理由。

运行：  python3 tests/test_upgrade.py
依赖：  宿主机有 ssh-keygen（OpenSSH 8.0+）；无第三方库

设计原则：**每个负向用例都必须断言"具体错误码"**，不能只断言"失败"——
否则一个"任何包都失败"的实现也能过测试，那比没有测试更危险。
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import upgrade as up            # noqa: E402

RESULTS = []


def case(fn):
    RESULTS.append(fn)
    return fn


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


TMP = tempfile.mkdtemp(prefix="gbupg-")
KEY = os.path.join(TMP, "pushkey")
KEY2 = os.path.join(TMP, "otherkey")
ALLOWED = os.path.join(TMP, "pkg.pub")


def _keygen(path):
    if os.path.exists(path):
        os.remove(path)
    if os.path.exists(path + ".pub"):
        os.remove(path + ".pub")
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", path, "-N", "",
                    "-C", up.SIGNER_IDENTITY, "-q"], check=True,
                   stdin=subprocess.DEVNULL)


_paths = {
    "pubkey": ALLOWED,
    "floor": os.path.join(TMP, "floor"),
    "staging": os.path.join(TMP, "staged"),
    "journal": os.path.join(TMP, "journal.json"),
    "secret": os.path.join(TMP, "secret"),
}

CUR = "1.0.2"          # 当前版本（测试基线）
NEW = "1.1.0"          # 目标版本


def setup_keys():
    _keygen(KEY)
    _keygen(KEY2)
    with open(KEY + ".pub", "r", encoding="utf-8") as f:
        pub = f.read().strip()
    with open(ALLOWED, "w", encoding="utf-8") as f:
        f.write("%s %s\n" % (up.SIGNER_IDENTITY, pub))


def _sha(b):
    return hashlib.sha256(b).hexdigest()


BASE_FILES = {
    "app.py": b"# app v2\n",
    "judge.py": b"# judge v2\n",
}


def _manifest(files, version=NEW, frm=(CUR,), removes=None):
    return {
        "format": 1,
        "kind": "guardian-b",
        "version": version,
        "from": list(frm),
        "files": [{"path": n, "sha256": _sha(c), "mode": "0644"}
                  for n, c in sorted(files.items())],
        "removes": removes or [],
    }


def _sign(path, key, ns=up.SIGN_NAMESPACE):
    """签名。**先删旧 sig**：ssh-keygen 遇到已存在的 sig 会交互式询问覆盖，
    在脚本里表现为永久挂起（2026-11-08 实测）。"""
    sig = path + ".sig"
    if os.path.exists(sig):
        os.remove(sig)
    r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key, "-n", ns, path],
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("sign failed: %r" % (r.stderr[:200],))
    with open(sig, "rb") as f:
        return f.read()


def _tar(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, data, typ, mode, link in members:
            ti = tarfile.TarInfo(name)
            ti.type = typ
            ti.mode = mode
            if typ == tarfile.DIRTYPE:
                tf.addfile(ti)
                continue
            if typ in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                ti.linkname = link
                tf.addfile(ti)
                continue
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def build(name="pkg.tar.gz", files=None, version=NEW, key=KEY, ns=up.SIGN_NAMESPACE,
          manifest=None, extra=(), override=None, drop=()):
    """打一个可控的包（每个负向用例只改一个变量）。"""
    files = BASE_FILES if files is None else files
    man = _manifest(files, version) if manifest is None else manifest
    mb = json.dumps(man, ensure_ascii=False).encode("utf-8")
    mp = os.path.join(TMP, "manifest.json")
    with open(mp, "wb") as f:
        f.write(mb)
    sig = _sign(mp, key, ns)

    members = [("guardian-b/manifest.json", mb, tarfile.REGTYPE, 0o644, ""),
               ("guardian-b/manifest.json.sig", sig, tarfile.REGTYPE, 0o644, "")]
    for n, c in files.items():
        if n in drop:
            continue
        data = (override or {}).get(n, c)
        members.append(("guardian-b/files/" + n, data, tarfile.REGTYPE, 0o644, ""))
    members.extend(extra)

    out = os.path.join(TMP, name)
    if os.path.exists(out):
        os.remove(out)
    _tar(out, members)
    return out


def stage(pkg, cur=CUR):
    """跑一次校验，返回 UpgradeError.code 或 ('ok', manifest)。"""
    try:
        job, man, staged, sha = up.stage_package(pkg, _paths, cur)
        return "ok", man
    except up.UpgradeError as exc:
        return exc.code, exc.detail


def journal():
    j = up.Journal(_paths["journal"])
    j.data = {}
    j.save()
    return j


# ═══════════════════════════════════════════════════════════
# A. 正常路径
# ═══════════════════════════════════════════════════════════
@case
def a1_正常包通过校验():
    code, man = stage(build())
    ok(code == "ok", "期望 ok，实际 %s" % code)
    ok(man["version"] == NEW, man["version"])
    ok(len(man["files"]) == 2, str(len(man["files"])))


@case
def a2_暂存目录只留最新一份():
    stage(build("p2.tar.gz"))
    stage(build("p3.tar.gz"))
    keep = [n for n in os.listdir(_paths["staging"]) if n.startswith("v-")]
    ok(len(keep) <= up.STAGED_KEEP, "残留 %d 份" % len(keep))


# ═══════════════════════════════════════════════════════════
# B. 签名与完整性
# ═══════════════════════════════════════════════════════════
@case
def b1_别人签的包_bad_signature():
    code, _ = stage(build("b1.tar.gz", key=KEY2))
    ok(code == "bad_signature", code)


@case
def b2_签名用错namespace_bad_signature():
    # 拿 A 自身升级包的 namespace 签 → 必须拒（防类型混淆）
    code, _ = stage(build("b2.tar.gz", ns="file"))
    ok(code == "bad_signature", code)


@case
def b3_manifest被改一字节_bad_signature():
    man = _manifest(BASE_FILES)
    pkg = build("b3.tar.gz", manifest=man)
    # 重新打一个 tar，manifest 内容与签名不匹配
    with tarfile.open(pkg, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            data = tf.extractfile(m).read() if m.isfile() else b""
            if m.name.endswith("manifest.json"):
                data = data.replace(b'"format": 1', b'"format":  1')
            members.append((m.name, data, m.type, m.mode, m.linkname))
    pkg2 = os.path.join(TMP, "b3b.tar.gz")
    _tar(pkg2, members)
    code, _ = stage(pkg2)
    ok(code == "bad_signature", code)


@case
def b4_文件内容被换_hash_mismatch():
    pkg = build("b4.tar.gz", override={"judge.py": b"# evil\n"})
    code, _ = stage(pkg)
    ok(code == "hash_mismatch", code)


@case
def b5_缺文件_missing_file():
    pkg = build("b5.tar.gz", drop=("judge.py",))
    code, _ = stage(pkg)
    ok(code == "missing_file", code)


@case
def b6_多塞文件_unexpected_file():
    extra = [("guardian-b/files/backdoor.py", b"x=1\n", tarfile.REGTYPE, 0o644, "")]
    code, _ = stage(build("b6.tar.gz", extra=extra))
    ok(code == "unexpected_file", code)


@case
def b7_无manifest_no_manifest():
    pkg = os.path.join(TMP, "b7.tar.gz")
    _tar(pkg, [("guardian-b/files/app.py", b"x\n", tarfile.REGTYPE, 0o644, "")])
    code, _ = stage(pkg)
    ok(code == "no_manifest", code)


@case
def b8_无签名_no_signature():
    members = [("guardian-b/manifest.json",
                json.dumps(_manifest(BASE_FILES)).encode(), tarfile.REGTYPE, 0o644, "")] + \
              [("guardian-b/files/" + n, c, tarfile.REGTYPE, 0o644, "")
               for n, c in BASE_FILES.items()]
    pkg = os.path.join(TMP, "b8.tar.gz")
    _tar(pkg, members)
    code, _ = stage(pkg)
    ok(code == "no_signature", code)


# ═══════════════════════════════════════════════════════════
# C. 解包攻击面
# ═══════════════════════════════════════════════════════════
@case
def c1_路径穿越_path_traversal():
    extra = [("guardian-b/files/../../etc/passwd", b"root\n", tarfile.REGTYPE, 0o644, "")]
    code, _ = stage(build("c1.tar.gz", extra=extra))
    ok(code in ("path_traversal", "unexpected_entry"), code)


@case
def c2_绝对路径():
    extra = [("/etc/cron.d/x", b"* * * * * root id\n", tarfile.REGTYPE, 0o644, "")]
    code, _ = stage(build("c2.tar.gz", extra=extra))
    ok(code in ("absolute_path", "unexpected_entry"), code)


@case
def c3_符号链接_not_regular_file():
    extra = [("guardian-b/files/link.py", b"", tarfile.SYMTYPE, 0o644, "/etc/passwd")]
    code, _ = stage(build("c3.tar.gz", extra=extra))
    ok(code == "not_regular_file", code)


@case
def c4_setuid位_setuid_bit():
    extra = [("guardian-b/files/su.py", b"x\n", tarfile.REGTYPE, 0o4755, "")]
    code, _ = stage(build("c4.tar.gz", extra=extra))
    ok(code == "setuid_bit", code)


@case
def c5_子目录文件_bad_path_layout():
    man = _manifest(BASE_FILES)
    man["files"].append({"path": "sub/x.py", "sha256": _sha(b"x\n"), "mode": "0644"})
    code, _ = stage(build("c5.tar.gz", manifest=man))
    ok(code in ("bad_path_layout", "bad_path"), code)


# ═══════════════════════════════════════════════════════════
# D. 触碰禁区（R2）
# ═══════════════════════════════════════════════════════════
@case
def d1_想改env_forbidden_path():
    man = _manifest(BASE_FILES)
    man["files"].append({"path": "guard-b.env", "sha256": _sha(b"K=1"), "mode": "0644"})
    code, _ = stage(build("d1.tar.gz", manifest=man))
    ok(code in ("forbidden_path", "file_type_not_allowed"), code)


@case
def d2_想替换验签器_forbidden_path():
    man = _manifest(BASE_FILES)
    man["files"].append({"path": "upgrade.py", "sha256": _sha(b"x"), "mode": "0644"})
    code, _ = stage(build("d2.tar.gz", manifest=man))
    ok(code == "forbidden_path", code)


@case
def d3_想带shell脚本_file_type_not_allowed():
    man = _manifest(BASE_FILES)
    man["files"].append({"path": "install.sh", "sha256": _sha(b"id"), "mode": "0755"})
    code, _ = stage(build("d3.tar.gz", manifest=man))
    ok(code == "file_type_not_allowed", code)


# ═══════════════════════════════════════════════════════════
# E. 版本策略
# ═══════════════════════════════════════════════════════════
@case
def e1_同版本_not_newer():
    code, _ = stage(build("e1.tar.gz", version=CUR))
    ok(code == "not_newer", code)


@case
def e2_降级_not_newer():
    code, _ = stage(build("e2.tar.gz", version="1.0.1"))
    ok(code == "not_newer", code)


@case
def e3_低于地板_below_floor():
    with open(_paths["floor"], "w", encoding="utf-8") as f:
        f.write("1.0.5\n")
    code, _ = stage(build("e3.tar.gz", version="1.0.4"))
    ok(code in ("not_newer", "below_floor"), code)
    os.remove(_paths["floor"])


@case
def e4_from不兼容_incompatible_from():
    man = _manifest(BASE_FILES, frm=("1.0.9",))
    code, _ = stage(build("e4.tar.gz", manifest=man))
    ok(code == "incompatible_from", code)


@case
def e5_版本号非法_bad_version():
    man = _manifest(BASE_FILES)
    man["version"] = "1.1.0/../evil"
    code, _ = stage(build("e5.tar.gz", manifest=man))
    ok(code == "bad_version", code)


@case
def e6_removes自相矛盾():
    man = _manifest(BASE_FILES, removes=["app.py"])
    code, _ = stage(build("e6.tar.gz", manifest=man))
    ok(code == "remove_conflicts_add", code)


@case
def e7_removes非法路径():
    man = _manifest(BASE_FILES, removes=["../etc/x.py"])
    code, _ = stage(build("e7.tar.gz", manifest=man))
    ok(code == "bad_removes_entry", code)


# ═══════════════════════════════════════════════════════════
# F. 体积限制
# ═══════════════════════════════════════════════════════════
@case
def f1_超过体积上限_pkg_too_large():
    big = os.path.join(TMP, "big.tar.gz")
    with open(big, "wb") as f:
        f.write(b"\0" * (up.MAX_PKG_BYTES + 1024))
    code, _ = stage(big)
    ok(code == "pkg_too_large", code)


@case
def f2_解包总量超限_too_large():
    man = _manifest(BASE_FILES)
    man["files"] = [{"path": "app.py",
                     "sha256": _sha(b"\0" * (up.MAX_UNPACKED_BYTES + 1)),
                     "mode": "0644"}]
    extra = [("guardian-b/files/app.py", b"\0" * (up.MAX_UNPACKED_BYTES + 1),
              tarfile.REGTYPE, 0o644, "")]
    # 直接构造（build() 会按 BASE_FILES 再塞一份）
    mb = json.dumps(man).encode()
    mp = os.path.join(TMP, "manifest.json")
    with open(mp, "wb") as f:
        f.write(mb)
    sig = _sign(mp, KEY)
    pkg = os.path.join(TMP, "f2.tar.gz")
    _tar(pkg, [("guardian-b/manifest.json", mb, tarfile.REGTYPE, 0o644, ""),
               ("guardian-b/manifest.json.sig", sig, tarfile.REGTYPE, 0o644, "")] + extra)
    code, _ = stage(pkg)
    ok(code in ("too_large", "hash_mismatch"), code)


# ═══════════════════════════════════════════════════════════
# G. 确认码与状态机
# ═══════════════════════════════════════════════════════════
def _to_verified():
    j = journal()
    code, man = stage(build("g.tar.gz"))
    ok(code == "ok", code)
    job = "job1234abcd"
    _paths["journal"] = _paths["journal"]
    j.set(job=job, state="verified", version=man["version"], from_version=CUR,
          pkg_sha256="a" * 64, code_exp=int(time.time()) + up.CONFIRM_TTL_S,
          confirm_attempts=0)
    return j, job


@case
def g1_正确确认码进入committed():
    j, job = _to_verified()
    sec = up._secret(_paths["secret"])
    good = up.make_code(sec, job, "a" * 64)
    okc, reason = up.confirm(j, good, _paths["secret"])
    ok(okc, reason)
    ok(j.data["state"] == "committed", j.data["state"])


@case
def g2_错误确认码被拒且计数():
    j, job = _to_verified()
    okc, reason = up.confirm(j, "000000", _paths["secret"])
    ok(not okc and reason == "bad_code", reason)
    ok(j.data["confirm_attempts"] == 1, str(j.data.get("confirm_attempts")))


@case
def g3_连续错5次作废():
    j, job = _to_verified()
    for _ in range(5):
        up.confirm(j, "000000", _paths["secret"])
    ok(j.data["state"] == "failed", j.data["state"])
    ok(j.data["error"] == "too_many_attempts", j.data["error"])


@case
def g4_确认码对但job不符被拒():
    j, job = _to_verified()
    sec = up._secret(_paths["secret"])
    other = up.make_code(sec, "otherjob", "a" * 64)
    okc, reason = up.confirm(j, other, _paths["secret"])
    ok(not okc and reason == "bad_code", reason)


@case
def g5_确认码过期():
    j, job = _to_verified()
    j.set(code_exp=int(time.time()) - 1)
    sec = up._secret(_paths["secret"])
    okc, reason = up.confirm(j, up.make_code(sec, job, "a" * 64), _paths["secret"])
    ok(not okc and reason == "confirm_expired", reason)
    ok(j.data["state"] == "failed", j.data["state"])


@case
def g6_没有待确认任务_no_pending():
    j = journal()
    okc, reason = up.confirm(j, "123456", _paths["secret"])
    ok(not okc and reason == "no_pending", reason)


@case
def g7_断电后journal仍可读且能继续确认():
    j, job = _to_verified()
    j2 = up.Journal(_paths["journal"])          # 模拟进程重启：重新从磁盘加载
    ok(j2.data.get("job") == job, str(j2.data.get("job")))
    ok(j2.data.get("state") == "verified", j2.data.get("state"))
    sec = up._secret(_paths["secret"])
    okc, _ = up.confirm(j2, up.make_code(sec, job, "a" * 64), _paths["secret"])
    ok(okc, "重启后应能继续确认")


@case
def g8_状态接口不泄露确认码():
    j, job = _to_verified()
    st = up.public_status(j)
    blob = json.dumps(st)
    sec = up._secret(_paths["secret"])
    code = up.make_code(sec, job, "a" * 64)
    ok(code not in blob, "状态里出现了确认码")
    ok("staged" not in blob, "状态里出现了内部路径")
    ok(set(st.keys()) == {"job", "state", "version", "from_version", "error",
                          "updated_at", "confirm_expires_in"}, str(sorted(st.keys())))


@case
def g9_确认码绑定包哈希_换包即失效():
    j, job = _to_verified()
    sec = up._secret(_paths["secret"])
    code_a = up.make_code(sec, job, "a" * 64)
    code_b = up.make_code(sec, job, "b" * 64)
    ok(code_a != code_b, "确认码必须绑定 pkg_sha256")


# ═══════════════════════════════════════════════════════════
# H. 其他
# ═══════════════════════════════════════════════════════════
@case
def h1_公钥文件被换身份_pubkey_identity_mismatch():
    good = open(ALLOWED, encoding="utf-8").read()
    # 只换行首 principal（保留 comment 里的 a7z-guard）——
    # 这正是 token 式检查会被骗过、行首检查才能挡住的情形
    rest = good.split(None, 1)[1]
    try:
        with open(ALLOWED, "w", encoding="utf-8") as f:
            f.write("someoneelse %s" % rest)
        code, _ = stage(build("h1.tar.gz"))
        ok(code == "pubkey_identity_mismatch", code)
    finally:
        with open(ALLOWED, "w", encoding="utf-8") as f:
            f.write(good)


@case
def h1b_换成别人的公钥_bad_signature():
    # principal 冒充成 a7z-guard、但公钥是攻击者的 → 必须败在签名而不是放行
    other_pub = open(KEY2 + ".pub", encoding="utf-8").read().strip()
    good = open(ALLOWED, encoding="utf-8").read()
    try:
        with open(ALLOWED, "w", encoding="utf-8") as f:
            f.write("%s %s\n" % (up.SIGNER_IDENTITY, other_pub))
        code, _ = stage(build("h1b.tar.gz"))
        ok(code == "bad_signature", code)
    finally:
        with open(ALLOWED, "w", encoding="utf-8") as f:
            f.write(good)


@case
def h2_非gzip字节_bad_tar():
    p = os.path.join(TMP, "h2.tar.gz")
    with open(p, "wb") as f:
        f.write(b"not a tarball")
    code, _ = stage(p)
    ok(code in ("bad_tar", "no_manifest"), code)


@case
def h3_版本比较按数值不按字符串():
    ok(up.vtuple("1.0.10") > up.vtuple("1.0.9"), "字符串比较会把 1.0.10 判小")
    ok(up.vtuple("2.0.0") > up.vtuple("1.99.99"), "2.0.0 应大于 1.99.99")


@case
def h4_原子写不产生残留tmp():
    p = os.path.join(TMP, "at.json")
    up.atomic_write(p, b"{}", 0o600)
    left = [n for n in os.listdir(TMP) if n.startswith(".tmp-")]
    ok(not left, "残留: %s" % left)


# ═══════════════════════════════════════════════════════════
# I. apply.py（root helper）的落地逻辑 —— 平时不跑，跑错就丢数据
# ═══════════════════════════════════════════════════════════
def _load_apply():
    """importlib 装载 deploy/apply.py（它不是包，路径也不在 sys.path）。"""
    import importlib.util
    p = os.path.join(ROOT, "deploy", "apply.py")
    spec = importlib.util.spec_from_file_location("guardb_apply", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_releases():
    """造 releases/{1.0.2,1.0.1,1.0.0,0.9.0}，并尝试建 current 链接。

    Windows 上创建目录符号链接需要开发者模式/管理员权限，建不了就返回 cur=None，
    依赖链接的用例自己跳过（Linux 真机上这些是必须跑的）。
    """
    root = os.path.join(TMP, "rel")
    shutil.rmtree(root, ignore_errors=True)
    for v in ("1.0.2", "1.0.1", "1.0.0", "0.9.0"):
        d = os.path.join(root, v)
        os.makedirs(d)
        for n in ("app.py", "judge.py"):
            with open(os.path.join(d, n), "w", encoding="utf-8") as f:
                f.write("# %s %s\n" % (v, n))
    cur = os.path.join(TMP, "current")
    if os.path.lexists(cur):
        os.unlink(cur)
    try:
        os.symlink(os.path.join(root, "1.0.2"), cur, target_is_directory=True)
    except (OSError, NotImplementedError):
        return root, None
    return root, cur


def _staged(files, version="1.1.0", removes=None):
    d = os.path.join(TMP, "staged", "v-" + version)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    man = {"format": 1, "kind": "guardian-b", "version": version,
           "from": ["1.0.2"], "removes": removes or [],
           "files": [{"path": n, "sha256": _sha(c), "mode": "0644"}
                     for n, c in sorted(files.items())]}
    for n, c in files.items():
        with open(os.path.join(d, n), "wb") as f:
            f.write(c)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f)
    return d, man


@case
def i1_build_release_叠加并删removes():
    ap = _load_apply()
    root, cur = _fake_releases()
    staged, man = _staged({"app.py": b"# new app\n"}, removes=["judge.py"])
    new = ap.build_release(root, staged, "1.1.0", "1.0.2", man)
    ok(os.path.isfile(os.path.join(new, "app.py")), "新文件应在")
    ok(not os.path.isfile(os.path.join(new, "judge.py")), "removes 应生效")
    with open(os.path.join(new, "app.py"), "rb") as f:
        ok(f.read() == b"# new app\n", "内容应为新版")


@case
def i2_build_release_拒绝被篡改的暂存文件():
    """暂存目录对 guardb 可写：验签到落地之间有人换文件，必须在落地前拦住。"""
    ap = _load_apply()
    root, cur = _fake_releases()
    staged, man = _staged({"app.py": b"# good\n"})
    with open(os.path.join(staged, "app.py"), "wb") as f:
        f.write(b"# evil\n")          # 只改文件，不改清单
    try:
        ap.build_release(root, staged, "1.1.0", "1.0.2", man)
        raise AssertionError("被篡改的暂存文件竟然落地了")
    except RuntimeError as exc:
        ok("改动" in str(exc) or "期望" in str(exc), str(exc))
    ok(not os.path.isdir(os.path.join(root, "1.1.0")),
       "失败后不应留下 1.1.0 目录")


@case
def i2b_清单与文件一起改也拦得住():
    """这是审查抓到的真漏洞：只核 hash 而不验签，等于把"清单自称"当事实。

    攻击者已拿到 guardb 权限时，可以同时改 `staged/app.py` 和
    `staged/manifest.json` 里的 sha256 —— 只核 hash 的实现会全绿通过。
    只有对 manifest **重新验签**才能拦住。本用例锁死这一点。
    """
    ap = _load_apply()
    root, cur = _fake_releases()
    staged, man = _staged({"app.py": b"# good\n"})
    # 同时改文件与清单里的 hash（模拟"聪明的攻击者"）
    evil = b"# evil\n"
    with open(os.path.join(staged, "app.py"), "wb") as f:
        f.write(evil)
    man["files"][0]["sha256"] = _sha(evil)
    with open(os.path.join(staged, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f)

    ap.TRUSTED = os.path.join(TMP, "trusted")
    os.makedirs(ap.TRUSTED, exist_ok=True)
    with open(os.path.join(ap.TRUSTED, "pkg.pub"), "w", encoding="utf-8") as f:
        f.write("%s %s\n" % (up.SIGNER_IDENTITY,
                             open(KEY + ".pub", encoding="utf-8").read().strip()))
    # staged 里的 sig 还是**上一版**清单的签名 → 必须被拒
    okc, _man, why = ap.load_and_verify_staged(staged)
    ok(not okc, "清单被改后签名仍通过：%s" % why)


@case
def i2c_正式包的签名能被apply验过():
    """端到端：本仓的打包产物 → stage_package → apply.verify_staged_sig。

    这一段串起了三个进程边界（离线打包 / Flask 校验 / root helper），
    任何一环的名字或路径约定变了都会在这里炸。
    """
    ap = _load_apply()
    pkg = build("i2c.tar.gz")
    paths = {
        "pubkey": os.path.join(TMP, "e2e.pub"),
        "floor": os.path.join(TMP, "e2e.floor"),
        "staging": os.path.join(TMP, "e2e_staged"),
        "journal": os.path.join(TMP, "e2e_journal.json"),
        "secret": os.path.join(TMP, "e2e_secret"),
    }
    os.makedirs(paths["staging"], exist_ok=True)
    with open(paths["pubkey"], "w", encoding="utf-8") as f:
        f.write("%s %s\n" % (up.SIGNER_IDENTITY,
                             open(KEY + ".pub", encoding="utf-8").read().strip()))
    job, man, staged, sha = up.stage_package(pkg, paths, CUR)
    ap.TRUSTED = os.path.dirname(paths["pubkey"])
    okc, man2, why = ap.load_and_verify_staged(staged)
    ok(okc, why)
    ok(man2 and man2.get("version") == "1.1.0", "验签后应能拿到已解析的清单")
    ok(os.path.isfile(os.path.join(staged, "manifest.json.sig")),
       "暂存目录必须带签名，否则 apply 无法复验")


@case
def i2d_清单里的绝对路径被拒():
    """清单是可被篡改的输入：files[].path='/etc/passwd' 不能让 root 去写它。"""
    ap = _load_apply()
    root, cur = _fake_releases()
    staged, man = _staged({"app.py": b"# ok\n"})
    man["files"].append({"path": "/etc/passwd", "sha256": _sha(b"x"), "mode": "0644"})
    try:
        ap.build_release(root, staged, "1.1.0", "1.0.2", man)
        raise AssertionError("绝对路径竟然被接受")
    except RuntimeError as exc:
        ok("非法" in str(exc) or "逃逸" in str(exc), str(exc))


@case
def i2e_removes里的穿越路径被拒():
    """removes 走 os.remove：'../app.py' 等于让 root 删任意文件。"""
    ap = _load_apply()
    root, cur = _fake_releases()
    staged, man = _staged({"app.py": b"# ok\n"}, removes=["../app.py"])
    try:
        ap.build_release(root, staged, "1.1.0", "1.0.2", man)
        raise AssertionError("穿越路径竟然被接受")
    except RuntimeError as exc:
        ok("非法" in str(exc) or "逃逸" in str(exc), str(exc))


@case
def i3_prune_保留current与两代():
    ap = _load_apply()
    root, cur = _fake_releases()
    os.makedirs(os.path.join(root, "1.2.0"))
    ap.prune(root, ("1.2.0", "1.0.2"))
    left = sorted(n for n in os.listdir(root) if not n.startswith("."))
    ok("0.9.0" not in left, "最旧的应被清掉：%s" % left)
    ok("1.2.0" in left, "current 必须保留")


@case
def i4_切换符号链接是原子的():
    ap = _load_apply()
    root, cur = _fake_releases()
    if not cur:
        print("        （跳过：本机不支持创建目录符号链接，需在 Linux 上跑）")
        return
    ap.swap_symlink(cur, os.path.join(root, "1.0.1"))
    ok(os.path.basename(os.readlink(cur)) == "1.0.1", os.readlink(cur))
    ok(not os.path.lexists(cur + ".swap"), "不应留下临时链接")
    ap.swap_symlink(cur, os.path.join(root, "1.0.2"))
    ok(os.path.basename(os.readlink(cur)) == "1.0.2", os.readlink(cur))


@case
def i5_版本号非法时不猜():
    ap = _load_apply()
    ok(ap.ver_tuple("1.2.10") > ap.ver_tuple("1.2.9"), "数值比较")
    ok(ap.VER_RE.match("1.2.3") is not None, "正常版本应匹配")
    ok(ap.VER_RE.match("../etc") is None, "路径形状不应被当成版本")


def main():
    setup_keys()
    passed = 0
    total = len(RESULTS)
    for fn in RESULTS:
        name = fn.__name__
        try:
            fn()
            passed += 1
            print("  PASS  %s" % name)
        except AssertionError as exc:
            print("  FAIL  %s  -> %s" % (name, exc))
        except Exception:
            print("  ERROR %s" % name)
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d/%d passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
