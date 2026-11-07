#!/usr/bin/env bash
# ============================================================
# guardian-b 一键安装（幂等，可重复执行）
# ------------------------------------------------------------
# 用法：
#   sudo ./install.sh                 # 默认装到 /opt/guard-b
#   sudo ./install.sh /opt/guard-b    # 指定目录
#
# 做的事：
#   1) 建目录，拷代码（不覆盖已存在的 guard-b.env —— token 不能丢）
#   2) 建 venv 并装依赖
#   3) 建 systemd 单元并 daemon-reload
#   4) **不自动 enable/start**：env 里 token 为空时启动会直接报错退出，
#      所以打印「填 env → 再启动」的下一步，由人确认后再拉起。
#
# 幂等性验证：重复执行不会重置 env、不会重建已存在的 venv。
# ============================================================
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-/opt/guard-b}"

[ "$(id -u)" -eq 0 ] || { echo "!! 需要 root：sudo $0" >&2; exit 2; }

# 前置：Ubuntu/Debian 最小镜像常缺 ensurepip，venv 会失败——且带 set -e 时会
# 在「装 systemd 单元」之前就退出，留下一个只有代码、没有服务的半成品目录。
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    cat >&2 <<'EOF'
!! 缺 python3-venv（ensurepip 不可用），venv 无法创建。
   请先执行：
       apt-get update && apt-get install -y python3-venv
   然后重跑本脚本（幂等，已存在的 guard-b.env 不会被覆盖）。
EOF
    exit 3
fi

echo "==> 目标目录: $DEST"
mkdir -p "$DEST/data" "$DEST/tls" "$DEST/deploy"

echo "==> 拷贝代码"
for f in app.py config.py state.py judge.py alerts.py requirements.txt README.md INSTALL-TS.md; do
    [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$DEST/$f"
done
cp -f "$SRC/deploy/guard-b.service" "$DEST/deploy/guard-b.service"
cp -f "$SRC/deploy/guard-b.env.example" "$DEST/deploy/guard-b.env.example"
[ -d "$SRC/tls" ] && cp -f "$SRC/tls/"*.sh "$DEST/tls/" 2>/dev/null || true

echo "==> Python 虚拟环境"
if [ ! -x "$DEST/venv/bin/python" ]; then
    # 上次失败可能留下半成品 venv（python3 是个断掉的符号链接），先清掉再建
    rm -rf "$DEST/venv"
    python3 -m venv "$DEST/venv" || {
        echo "!! venv 创建失败。请先 apt-get install -y python3-venv，再重跑本脚本" >&2
        exit 4
    }
fi
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet -r "$DEST/requirements.txt"

echo "==> 环境文件"
if [ -f "$DEST/guard-b.env" ]; then
    echo "    guard-b.env 已存在 → 保留不动（避免覆盖 token）"
else
    cp "$DEST/deploy/guard-b.env.example" "$DEST/guard-b.env"
    chmod 600 "$DEST/guard-b.env"
    echo "    已生成 $DEST/guard-b.env（占位，待填 token/key）"
fi

echo "==> systemd 单元"
install -m 0644 "$DEST/deploy/guard-b.service" /etc/systemd/system/guard-b.service
systemctl daemon-reload

if grep -qE '^GUARD_TOKEN=.+$' "$DEST/guard-b.env"; then
    echo "==> GUARD_TOKEN 已配置 → 启用并启动"
    systemctl enable --now guard-b
    sleep 2
    systemctl --no-pager --lines=15 status guard-b || true
    echo
    echo "自检：curl -s http://\$(grep GUARD_BIND_HOST $DEST/guard-b.env | cut -d= -f2):\$(grep GUARD_BIND_PORT $DEST/guard-b.env | cut -d= -f2)/healthz"
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
echo "==> 完成"
