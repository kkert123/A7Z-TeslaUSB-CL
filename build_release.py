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
import io
import json
import os
import re
import sys
import tarfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(ROOT, ".workbuddy", "artifacts")

# ── 打包脱敏（2026-11-16 / M148）──────────────────────────────────
# `config.json` 在 MANAGED_FILES 白名单里，而它是**真实运行配置**：
#   · 含 `auth.users.admin` 的 scrypt 密码哈希
#   · `git ls-files config.json` 为空（.gitignore 挡着，仓库本体干净）
#   · 但包会被上传到**公开** Release —— v0.3.1.89 的公开包内已含该哈希（下载 3 次）
# 所以打包时必须把凭据类字段清空。现网升级不受影响：设备端
# `_save_user_data` → `_restore_user_data` 会保留旧 config，新包里的这份只用于全新安装。
SANITIZE_FILES = {"config.json"}


def sanitize_payload(rel: str, data: bytes) -> bytes:
    """对需要脱敏的文件做字段级替换；其他文件原样返回。

    注意：解析失败要**拒绝打包**而不是原样放行 —— 宁可发不出去，
    也不能把一份"没脱敏成功"的真实配置传上公开仓库。
    """
    if rel not in SANITIZE_FILES:
        return data
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SystemExit(f"!! {rel} 解析失败，拒绝打包（无法确认已脱敏）: {e}")
    if not isinstance(obj, dict):
        raise SystemExit(f"!! {rel} 顶层不是对象，拒绝打包")

    auth = obj.get("auth")
    if isinstance(auth, dict):
        users = auth.get("users")
        if users:
            auth["users"] = {}
            # 只清哈希不够：若 auth_enabled=true 且无账号，全新安装会「有登录开关、
            # 没有任何账号」= 锁死。一并关掉开关，让首次启用时重新设密码。
            if obj.get("auth_enabled") is True:
                obj["auth_enabled"] = False
            auth["_sanitized"] = "打包时已清空 auth.users（真实哈希不入公开包）"
    # 其它可能的凭据位（当前版本没有，留作向前兼容）
    for k in ("teslamate_password", "webhook_key", "wecom_key"):
        if obj.get(k):
            obj[k] = ""
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


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
    #    SANITIZE_FILES 里的文件先脱敏再写入（走内存 + addfile，保留原 mtime/权限）
    with tarfile.open(tarball, "w:gz") as tf:
        for rel in files:
            path = os.path.join(ROOT, rel)
            if rel in SANITIZE_FILES:
                with open(path, "rb") as f:
                    data = sanitize_payload(rel, f.read())
                info = tf.gettarinfo(path, arcname=rel)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                tf.add(path, arcname=rel, recursive=False)

    # 3) 回读校验：文件数 + 脱敏结果
    #    写前自检只证明「内存对象已脱敏」，真正传出去的是写出的字节，
    #    所以必须把产物读回来再验一遍（M114/M122 同一类：写后回读）
    with tarfile.open(tarball, "r:gz") as tf:
        got = tf.getnames()
        leaked = []
        for rel in SANITIZE_FILES:
            if rel not in got:
                continue
            try:
                obj = json.loads(tf.extractfile(rel).read().decode("utf-8"))
            except Exception as e:                       # noqa: BLE001
                leaked.append("%s(不可解析: %s)" % (rel, e))
                continue
            auth = obj.get("auth") if isinstance(obj.get("auth"), dict) else {}
            if auth.get("users"):
                leaked.append("%s(auth.users 仍非空)" % rel)
    if leaked:
        raise SystemExit("!! 脱敏失败，拒绝发布：\n  " + "\n  ".join(leaked))
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
