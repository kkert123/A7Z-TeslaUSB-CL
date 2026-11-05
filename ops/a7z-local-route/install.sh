#!/bin/bash
# 安装 / 升级 a7z-local-route（需 root）
#
# 用法：
#   sudo ./install.sh            # 安装或升级
#   sudo ./install.sh --dry-run  # 只打印将要做什么
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

step() { printf '[%s] %s\n' "$([ "$DRY" = 1 ] && echo dry || echo do)" "$*"; }
run()  { if [ "$DRY" = 1 ]; then step "$*"; else "$@"; fi; }

[ "$(id -u)" = "0" ] || { echo "需要 root 运行" >&2; exit 1; }

# 1) 主脚本
step "安装 /usr/local/sbin/a7z-local-route.sh"
run install -m 0755 "$HERE/a7z-local-route.sh" /usr/local/sbin/a7z-local-route.sh

# 2) NetworkManager dispatcher
#    NM 强制要求：root:root 拥有、group/other 不可写、可执行；不满足会被静默忽略
step "安装 /etc/NetworkManager/dispatcher.d/91-a7z-local-route (root:root 0755)"
run install -m 0755 -o root -g root "$HERE/91-a7z-local-route" /etc/NetworkManager/dispatcher.d/91-a7z-local-route

# 3) systemd 单元
step "安装 /etc/systemd/system/a7z-local-route.service"
run install -m 0644 "$HERE/a7z-local-route.service" /etc/systemd/system/a7z-local-route.service

# 4) 配置文件：已存在则保留（避免覆盖用户改过的网段）
if [ -f /etc/a7z-local-route.conf ]; then
    step "保留已有 /etc/a7z-local-route.conf"
else
    step "创建 /etc/a7z-local-route.conf（来自 example）"
    run install -m 0644 "$HERE/a7z-local-route.conf.example" /etc/a7z-local-route.conf
fi

# 4b) 行尾规范化（必须）
#   Windows 工作区（core.autocrlf）检出的脚本是 CRLF，装到 Linux 后 shebang 会变成
#   /bin/bash\r → "bad interpreter"；conf 里的 \r 还会让网段字符串比较永远失败。
#   这里统一去掉，保证无论源码来自哪个平台都能用。
for f in /usr/local/sbin/a7z-local-route.sh          /etc/NetworkManager/dispatcher.d/91-a7z-local-route          /etc/systemd/system/a7z-local-route.service          /etc/a7z-local-route.conf; do
    step "行尾规范化 $f"
    run sed -i 's/\r$//' "$f"
done

# 5) 生效
step "systemctl daemon-reload"
run systemctl daemon-reload
step "systemctl enable a7z-local-route.service"
run systemctl enable a7z-local-route.service
# 通过 systemd 启动而不是直接跑脚本：oneshot + RemainAfterExit 才会体现为
# active (exited)，否则 is-active 一直是 inactive，运维检查会误判为"没装好"
step "启动服务并执行一次同步"
run systemctl restart a7z-local-route.service

# 6) 自检输出
if [ "$DRY" = 0 ]; then
    echo
    echo "── 自检 ──"
    echo "规则："      ; ip rule show | grep -E 'to .* lookup 100' || echo "  (无，请检查 NETS 配置)"
    echo "表 100："    ; ip route show table 100 2>/dev/null | sed 's/^/  /' || echo "  (空：当前不在该网段，或尚未连接)"
    echo
    echo "完成。验证方法见 README.md「验证」一节。"
fi
