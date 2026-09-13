#!/usr/bin/env python3
"""A7Z TeslaUSB 升级包构建脚本（可复用）

从 deploy_manager.py 的 Config.MANAGED_FILES 白名单生成升级包，
保持与历史包完全一致的布局：文件名按白名单顺序、平铺在包根、无前缀目录。

用法:
    python build_release.py            # 用 config.py 的 APP_VERSION
    python build_release.py 0.3.1.55   # 指定版本

产物:
    .workbuddy/artifacts/teslausb-v<version>.tar.gz

构建后请继续执行（见 docs/how-to/发布新版本.md 第 3-4 步）:
    ssh-keygen -Y sign -f upgrade_key -n file <tarball>
    comm -23 <(tar tzf <旧包> | sort) <(tar tzf <新包> | sort)   # M41，输出必须为空
"""
import os
import re
import sys
import tarfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(ROOT, ".workbuddy", "artifacts")


def read_managed_files() -> list:
    """从 deploy_manager.py 解析 MANAGED_FILES（保持声明顺序）。"""
    path = os.path.join(ROOT, "deploy_manager.py")
    started = False
    items = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if "MANAGED_FILES = [" in ln:
                started = True
                continue
            if started:
                if ln.strip() == "]":
                    break
                m = re.match(r'\s*"([^"]+)",', ln)
                if m:
                    items.append(m.group(1))
    return items


def read_app_version() -> str:
    path = os.path.join(ROOT, "config.py")
    with open(path, encoding="utf-8") as f:
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', f.read())
    if not m:
        raise SystemExit("!! 无法从 config.py 解析 APP_VERSION")
    return m.group(1)


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else read_app_version()
    files = read_managed_files()
    if not files:
        raise SystemExit("!! MANAGED_FILES 为空，解析失败")

    # 1) 存在性校验（M33：requirements.txt 必须在内）
    missing = [f for f in files if not os.path.exists(os.path.join(ROOT, f))]
    if missing:
        raise SystemExit("!! 白名单文件在磁盘上缺失:\n  " + "\n  ".join(missing))
    if "requirements.txt" not in files:
        raise SystemExit("!! requirements.txt 不在白名单内（M33）")

    os.makedirs(ART, exist_ok=True)
    tarball = os.path.join(ART, f"teslausb-v{version}.tar.gz")

    # 2) 打包：平铺、按白名单顺序、不写 uid/gid（与历史包一致）
    with tarfile.open(tarball, "w:gz") as tf:
        for rel in files:
            tf.add(os.path.join(ROOT, rel), arcname=rel, recursive=False)

    # 3) 回读校验文件数
    with tarfile.open(tarball, "r:gz") as tf:
        got = tf.getnames()
    ok = got == files
    size = os.path.getsize(tarball)
    print(f"✅ 构建完成: {tarball}")
    print(f"   版本: v{version}  文件数: {len(got)}/{len(files)}  大小: {size} bytes")
    print(f"   顺序与白名单一致: {ok}")
    if not ok:
        only_w = sorted(set(files) - set(got))
        only_t = sorted(set(got) - set(files))
        print("   白名单独有:", only_w)
        print("   包内独有:", only_t)
    print("\n下一步:")
    print(f"   ssh-keygen -Y sign -f upgrade_key -n file {tarball}")
    print(f"   comm -23 <(tar tzf <上一版包> | sort) <(tar tzf {tarball} | sort)  # 必须为空")


if __name__ == "__main__":
    main()
