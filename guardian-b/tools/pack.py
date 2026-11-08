#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 离线打包与签名（在**开发机**跑，不在 VPS 上跑）
==============================================================
为什么在开发机
--------------
B 端签名私钥**不能放在 A7Z 上**。A7Z 是暴露在局域网/车机环境里的设备，
一旦它被拿下，私钥就跟着泄露 → 攻击者可以伪造任意 B 包。
放离线开发机后，A 沦陷最多让 B 升级到一个**真实发布过**的版本，
配合 B 端 `floor`（版本地板）即可把危害压到"降级到旧版"。

用法
----
    # 1) 一次性生成密钥（在离线机器上，私钥别进仓库）
    ssh-keygen -t ed25519 -C a7z-guard -f guard_push -N ""

    # 2) 打包 + 签名
    python tools/pack.py --version 1.1.0 --from 1.0.0,1.0.1,1.0.2 \\
        --key ~/.ssh/guard_push --out dist/

    # 3) 把 public key 装到 B（一行，principal 必须是 a7z-guard）
    echo "a7z-guard $(cat guard_push.pub)" | ssh root@B 'tee /opt/guard-b/.trusted/pkg.pub'

产物：dist/guardian-b-v<版本>.tar.gz（内含 manifest.json + manifest.json.sig）

包内**不含** upgrade.py / apply.py
----------------------------------
验签器与 root helper 一旦能被升级包替换，"校验"这一层就可以被一次合法签名
顺手换掉。所以它们不进包、也不进 releases/，由 install.sh 单独装到 .tools/。
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile

IDENTITY = "a7z-guard"
NAMESPACE = "a7z-guard-pkg"
KIND = "guardian-b"

# 只打这些模块（新增模块要显式加进来——这也是一道"别把杂项打进包"的闸）
APP_MODULES = ["app.py", "config.py", "state.py", "judge.py", "alerts.py"]

# 永不进包
NEVER = {"upgrade.py", "apply.py", "manifest.json", "manifest.json.sig"}


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def collect(src):
    out = {}
    for name in APP_MODULES:
        p = os.path.join(src, name)
        if not os.path.isfile(p):
            raise SystemExit("缺文件：%s" % p)
        with open(p, "rb") as f:
            out[name] = f.read()
    return out


def sign_manifest(manifest_path, key):
    """签名。**必须先删旧 sig**：ssh-keygen 遇到已存在的 sig 会交互式问覆盖，
    脚本里就是无限挂起（2026-11-08 实测踩过）。"""
    sig = manifest_path + ".sig"
    if os.path.exists(sig):
        os.remove(sig)
    r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key, "-n", NAMESPACE,
                        manifest_path],
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    if r.returncode != 0:
        raise SystemExit("签名失败：%s" % (r.stderr or b"").decode("utf-8", "replace"))
    with open(sig, "rb") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="如 1.1.0")
    ap.add_argument("--from", dest="froms", required=True,
                    help="可从此列表中的版本升级，逗号分隔，如 1.0.0,1.0.1,1.0.2")
    ap.add_argument("--key", required=True, help="Ed25519 私钥路径（离线机器上）")
    ap.add_argument("--out", default="dist", help="输出目录")
    ap.add_argument("--src", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="guardian-b 源码目录")
    ap.add_argument("--removes", default="", help="本版删除的模块，逗号分隔")
    args = ap.parse_args()

    files = collect(args.src)
    if not args.version.count(".") == 2:
        raise SystemExit("版本号必须是 X.Y.Z：%s" % args.version)

    manifest = {
        "format": 1,
        "kind": KIND,
        "version": args.version,
        "from": [x.strip() for x in args.froms.split(",") if x.strip()],
        "files": [{"path": n, "sha256": sha256_bytes(c), "mode": "0644"}
                  for n, c in sorted(files.items())],
        "removes": [x.strip() for x in args.removes.split(",") if x.strip()],
    }
    for n in manifest["removes"]:
        if n in files:
            raise SystemExit("removes 与 files 冲突：%s" % n)
        if n not in NEVER and not n.endswith(".py"):
            raise SystemExit("removes 只允许 .py：%s" % n)

    tmp = tempfile.mkdtemp(prefix="packb-")
    mp = os.path.join(tmp, "manifest.json")
    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    with open(mp, "wb") as f:
        f.write(raw)
    sig = sign_manifest(mp, args.key)

    # 自校验：签完立刻验一次，避免"签了个坏的"到 B 端才发现
    allowed = os.path.join(tmp, "allowed")
    with open(allowed, "w", encoding="utf-8") as f:
        with open(args.key + ".pub", "r", encoding="utf-8") as k:
            f.write("%s %s\n" % (IDENTITY, k.read().strip()))
    os.chmod(allowed, 0o600)
    r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", allowed,
                        "-I", IDENTITY, "-n", NAMESPACE, "-s", mp + ".sig"],
                       input=raw, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit("自校验失败：%s" % (r.stderr or b"").decode("utf-8", "replace"))

    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "guardian-b-v%s.tar.gz" % args.version)
    with tarfile.open(out, "w:gz") as tf:
        def add(name, data, mode=0o644):
            import io
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = int(os.path.getmtime(mp)) if os.path.exists(mp) else 0
            tf.addfile(ti, io.BytesIO(data))
        add("guardian-b/manifest.json", raw)
        add("guardian-b/manifest.json.sig", sig)
        for n, c in sorted(files.items()):
            add("guardian-b/files/" + n, c)

    with open(out, "rb") as f:
        digest = sha256_bytes(f.read())
    print("包:      %s" % out)
    print("大小:    %d B" % os.path.getsize(out))
    print("文件:    %d 个（%s）" % (len(files), ", ".join(sorted(files))))
    print("sha256:  %s" % digest)
    print("namespace: %s ｜ principal: %s" % (NAMESPACE, IDENTITY))
    print("")
    print("A 端上传时用这个 sha256 核对；B 端会再独立验签一遍。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
