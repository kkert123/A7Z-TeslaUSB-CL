#!/usr/bin/env bash
# ============================================================
# guardian-b 一键安装 / 升级到 v1.1.0 布局（幂等，可重复执行）
# ------------------------------------------------------------
# 用法：
#   sudo ./install.sh                 # 默认装到 /opt/guard-b
#   sudo ./install.sh /opt/guard-b    # 指定目录
#
# v1.1.0 的目录结构变化（**升级时自动迁移**）：
#
#   /opt/guard-b/
#     current -> releases/1.1.0      ← 原子切换点
#     releases/1.0.2/  releases/1.1.0/
#     .tools/upgrade.py .tools/apply.py   ← root 0444，**不在升级包内**
#     .trusted/pkg.pub .trusted/floor     ← 签名公钥与版本地板
#     data/                              ← 唯一可写区（guardb 属主）
#     venv/  guard-b.env
#
# 做的事：
#   1) 建 guardb 系统用户（无 shell）；data/ 归它所有
#   2) 旧平铺布局 → releases/<旧版本>/ + current 链接（只做一次）
#   3) 装新版本到 releases/<新版本>/，切 current
#   4) 装 .tools（验签库 + root helper），装 3 个 systemd 单元（含 path unit）
#   5) **不覆盖已存在的 guard-b.env**
#
# 幂等性：重复执行不会重置 env、不会重建 venv、不会重复迁移。
# ============================================================
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-/opt/guard-b}"
RUN_USER="guardb"

[ "$(id -u)" -eq 0 ] || { echo "!! 需要 root：sudo $0" >&2; exit 2; }

if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    cat >&2 <<'EOF'
!! 缺 python3-venv（ensurepip 不可用），venv 无法创建。
   请先执行：
       apt-get update && apt-get install -y python3-venv
   然后重跑本脚本（幂等，已存在的 guard-b.env 不会被覆盖）。
EOF
    exit 3
fi

if ! command -v ssh-keygen >/dev/null 2>&1; then
    cat >&2 <<'EOF'
!! 缺 ssh-keygen（openssh-client）：升级包验签依赖它。
   请先执行： apt-get install -y openssh-client
EOF
    exit 3
fi

SRC_VER="$(grep -E '^VERSION = "' "$SRC/app.py" | head -1 | cut -d'"' -f2)"
[ -n "$SRC_VER" ] || { echo "!! 无法从 app.py 读出 VERSION" >&2; exit 4; }
echo "==> 目标目录: $DEST（本包版本 $SRC_VER）"

# ── 1) 运行用户 ─────────────────────────────────────────────
if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "==> 创建系统用户 $RUN_USER（无 shell、无家目录）"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER" 2>/dev/null || \
        useradd --system --no-create-home --shell /bin/false "$RUN_USER"
fi

mkdir -p "$DEST/data" "$DEST/tls" "$DEST/deploy" "$DEST/releases" "$DEST/.trusted" "$DEST/.tools"

# ── 2) 旧平铺布局 → releases/<旧版本>/ ──────────────────────
if [ ! -L "$DEST/current" ] && [ -f "$DEST/app.py" ]; then
    OLD_VER="$(grep -E '^VERSION = "' "$DEST/app.py" | head -1 | cut -d'"' -f2)"
    if [ -n "$OLD_VER" ] && [ -d "$DEST/releases/$OLD_VER" ]; then
        echo "==> 旧布局已迁移过（releases/$OLD_VER 已存在）"
    elif [ -n "$OLD_VER" ]; then
        echo "==> 迁移旧平铺布局 → releases/$OLD_VER"
        mkdir -p "$DEST/releases/$OLD_VER"
        for f in "$DEST"/*.py; do
            # upgrade.py / apply.py 属于「验签器」，不进 releases/
            # （否则升级包就能替换校验自己的那段代码）
            case "$(basename "$f")" in
                upgrade.py|apply.py) echo "    跳过 $(basename "$f")（属 .tools）"; continue ;;
            esac
            [ -f "$f" ] && mv -f "$f" "$DEST/releases/$OLD_VER/"
        done
        cp -f "$DEST/deploy/guard-b.service" "$DEST/releases/$OLD_VER/" 2>/dev/null || true
    else
        echo "!! 旧 app.py 里读不到版本号，跳过迁移（请人工检查）" >&2
    fi
fi

# ── 3) 装新版本 ─────────────────────────────────────────────
echo "==> 安装 releases/$SRC_VER"
mkdir -p "$DEST/releases/$SRC_VER"
for f in app.py config.py state.py judge.py alerts.py requirements.txt README.md INSTALL-TS.md; do
    [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$DEST/releases/$SRC_VER/$f"
done

echo "==> 切换 current → releases/$SRC_VER"
if [ -L "$DEST/current" ]; then
    ln -sfn "releases/$SRC_VER" "$DEST/current.swap"
    mv -Tf "$DEST/current.swap" "$DEST/current"
else
    ln -sfn "releases/$SRC_VER" "$DEST/current"
fi

# ── 4) 信任根与工具（root 只读，不在升级包内）───────────────
echo "==> 安装 .tools（验签库 + root helper）"
install -m 0444 "$SRC/upgrade.py"      "$DEST/.tools/upgrade.py"
install -m 0500 "$SRC/deploy/apply.py" "$DEST/.tools/apply.py"

if [ -f "$DEST/.trusted/pkg.pub" ]; then
    echo "    .trusted/pkg.pub 已存在 → 保留（签名公钥不覆盖）"
else
    cat <<EOF
    !! 尚未安装升级签名公钥。
       把**离线签名机**上的公钥按下面格式写入（一行，principal 必须是 a7z-guard）：
           echo 'a7z-guard $(cat guard_push.pub)' | sudo tee $DEST/.trusted/pkg.pub
           sudo chmod 0444 $DEST/.trusted/pkg.pub
       在此之前远程升级会以 no_pubkey 拒绝（这是预期，不是故障）。
EOF
fi
[ -f "$DEST/.trusted/floor" ] || : > "$DEST/.trusted/floor"
chmod 0755 "$DEST/.trusted"

# ── 5) 权限：可写区只有 data/ ───────────────────────────────
echo "==> 收紧权限"
chown -R root:root "$DEST/releases" "$DEST/.tools" 2>/dev/null || true
chmod -R go-w "$DEST/releases" "$DEST/.tools" 2>/dev/null || true
chown -R "$RUN_USER:$RUN_USER" "$DEST/data"
chmod 0750 "$DEST/data"
# current 必须对 guardb 可读可穿过
chmod 0755 "$DEST" "$DEST/releases"

# ── 6) venv 与依赖 ─────────────────────────────────────────
echo "==> Python 虚拟环境"
if [ ! -x "$DEST/venv/bin/python" ]; then
    rm -rf "$DEST/venv"
    python3 -m venv "$DEST/venv" || {
        echo "!! venv 创建失败。请先 apt-get install -y python3-venv，再重跑本脚本" >&2
        exit 4
    }
fi
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet -r "$DEST/releases/$SRC_VER/requirements.txt"
chown -R root:root "$DEST/venv"

# ── 7) 环境文件 ─────────────────────────────────────────────
echo "==> 环境文件"
if [ -f "$DEST/guard-b.env" ]; then
    echo "    guard-b.env 已存在 → 保留不动（避免覆盖 token）"
else
    cp "$DEST/deploy/guard-b.env.example" "$DEST/guard-b.env" 2>/dev/null || \
        cp "$SRC/deploy/guard-b.env.example" "$DEST/guard-b.env"
    echo "    已生成 $DEST/guard-b.env（占位，待填 token/key）"
fi
# 进程可读不可写：能读 key 才能发告警，不能写才能防"改目的地"（R2）
chown "root:$RUN_USER" "$DEST/guard-b.env"
chmod 0440 "$DEST/guard-b.env"

# ── 8) systemd 单元（guard-b / guardb-upgrade / guardb-upgrade.path）──
echo "==> systemd 单元"
install -m 0644 "$SRC/deploy/guard-b.service"          /etc/systemd/system/guard-b.service
install -m 0644 "$SRC/deploy/guardb-upgrade.service"   /etc/systemd/system/guardb-upgrade.service
install -m 0644 "$SRC/deploy/guardb-upgrade.path"      /etc/systemd/system/guardb-upgrade.path

# 老的 sudoers 文件（v1.1.0 早期草案用过 sudo）如有残留必须删掉：
# guard-b.service 开着 NoNewPrivileges=yes，sudo 根本提不了权，
# 留着只会让人误以为"权限已经配好了"。
rm -f /etc/sudoers.d/guardb-upgrade

systemctl daemon-reload
# path unit 必须 enable：它才是"写请求文件 → 由 root 执行"的那根引线
systemctl enable --now guardb-upgrade.path

if grep -qE '^GUARD_TOKEN=.+$' "$DEST/guard-b.env"; then
    echo "==> GUARD_TOKEN 已配置 → 启用并（重新）启动"
    systemctl enable guard-b
    # 必须 restart 而不是 enable --now：对**已在运行**的 unit 来说，
    # `enable --now` 不会重启它 —— 结果就是 current 已指向新版本、
    # 进程还在跑旧代码，状态页显示的版本号和磁盘上的文件不一致。
    systemctl restart guard-b
    sleep 2
    systemctl --no-pager --lines=15 status guard-b || true
    echo
    echo "自检：sudo -u $RUN_USER readlink -f $DEST/current"
    echo "      ps -o user=,cmd= -p \$(systemctl show -p MainPID --value guard-b)"
else
    cat <<EOF

==> 还差一步：填 $DEST/guard-b.env 里的 GUARD_TOKEN（和 GUARD_WECOM_KEY / GUARD_BIND_HOST）

    sudo nano $DEST/guard-b.env
    # 必改：
    #   GUARD_TOKEN=<32 字节随机串>
    #   GUARD_BIND_HOST=100.111.252.121   # 只绑 Tailscale 接口，不暴露公网
    #   GUARD_BIND_PORT=8899
    #   GUARD_WECOM_KEY=<机器人 key>
    sudo systemctl enable --now guard-b

EOF
fi

cat <<EOF

==> 目录结构
$(ls -la "$DEST" | sed 's/^/    /')

==> 远程升级默认**关闭**（GUARD_UPGRADE_ENABLE=0）。
    要启用，需同时满足：
      1) $DEST/.trusted/pkg.pub 已装（a7z-guard 身份）
      2) $DEST/guard-b.env 里 GUARD_UPGRADE_ENABLE=1
      3) A 端从 Tailscale 网段发起（$DEST 的 GUARD_TS_CIDR，默认 100.64.0.0/10）
    只开 1/2 不开 3 是无效的；三项缺一都会 403，这是设计。

==> 完成
EOF
