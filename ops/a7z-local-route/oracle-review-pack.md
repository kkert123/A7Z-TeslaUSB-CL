你是资深 Linux 网络与系统脚本周边的审查者。请审查下面三个文件，只报「有具体位置且真实会发生」的问题，按严重度排序；不要风格建议、不要假设不存在的代码。

## 部署环境
Radxa Cubie A7Z（全志 A733，arm64，Debian 11 bullseye，Python 3.9.2）
- NetworkManager 管理 wlan0，systemd 为 init
- 已装 Tailscale，本机 tailscale IP 100.116.18.42，开启 accept-routes（RouteAll=true）
- tailnet 内另一台机器宣告了 subnet route `12.127.12.0/24`（一个机房内网，家里 WiFi 也在该网段）
- 设备自己宣告 `192.168.8.0/24`（车载热点网段）给 tailnet，NoSNAT=false
- 无公网 IP 暴露，无 nginx 反代

## 要解决的问题
Tailscale 接受 subnet route 后在 table 52 写入 `12.127.12.0/24 dev tailscale0`，
而 `ip rule 5270: lookup 52` 优先于 `32766: lookup main`，导致设备回包给同网段设备时
走隧道、源 IP 变成 100.116.18.42 → 对方按 `12.127.12.x:5000` 发起的 TCP 握手失败。
（已实测确认：设备同网段 ARP 双向可达、防火墙 ACCEPT、服务 self-curl 200，
但 `ip route get 12.127.12.122` 返回 `dev tailscale0 table 52 src 100.116.18.42`。）

## 双场景需求（必须同时满足）
1. 在家（wlan0 属于 12.127.12.0/24）：同网段设备能直连访问本机
2. 在车里（wlan0 属于 192.168.8.0/24）：本机仍能经 Tailscale 访问家里 12.127.12.0/24

## 当前方案
为 12.127.12.0/24 建高优先级规则（priority 5260，先于 tailscale 的 5270）指向专用表 100；
表 100 只在 wlan0 确实属于该网段时才有条目，且**不放 default**，因此表为空时路由查找
会自然落到下一条 rule（tailscale 的 table 52）。

## 已做过的离线验证（不必重复提这些点）
把 `ip` mock 掉跑了 7 个场景、16 项断言全过：在家写直连 / 在车里清条目 / 幂等不重复加 /
无 wlan0 零操作 / `lookup 1000` 不误判为 `lookup 100` / 有网卡无地址时清条目 /
优先级 5260 被占用时告警跳过。

## 请重点看
- 竞态：NM dispatcher 的 up 与 dhcp4-change 连续触发、开机 oneshot 与 dispatcher 并发
- 开机时序：oneshot 在 wlan0 未连上时执行会怎样；之后靠谁补
- 幂等边界：`ip route replace` / `ip route del` 在表不存在、条目不存在时的返回值与副作用
- systemd 单元：After/Wants 是否正确、TimeoutStartSec、RemainAfterExit 语义、关机路径
- dispatcher 的强制要求（属主/权限/执行时间上限）是否满足
- 是否会让「在车里访问家里内网」失效（这是硬需求）
- 回滚是否完整、有无残留
- 有没有更简单可靠的替代做法（如果有，说明为什么更好）

## 文件

### FILE: a7z-local-route.sh
```bash
#!/bin/bash
# a7z-local-route.sh —— 让「本机所在网段」优先走本地直连，避免被 Tailscale subnet route 抢走
#
# 【背景】
# tailnet 里某台机器宣告了 12.127.12.0/24，A7Z 接受后 Tailscale 会在 table 52 写入
#   12.127.12.0/24 dev tailscale0
# 而 ip rule 5270「lookup 52」优先于 32766「lookup main」，于是：
#   设备回包给同网段设备时走隧道，源 IP 变成 tailscale 地址
#   → 对方按「12.127.12.x:5000」发起的连接收到「100.116.18.42:xxxxx」的回包
#   → 4 元组不匹配，TCP 握手失败（ping 同理）。表现为「同网段却互访不通」。
#
# 【做法】
# 为每个「本地直连且与 tailscale 冲突」的网段建一条高优先级规则，指向专用表 100。
# 表 100 里**只在 wlan0 确实属于该网段时才放条目**，且不放 default，
# 因此表为空时路由查找会自然落到下一条 rule（tailscale 的 table 52），
# 从而同时满足：
#   - 在家（wlan0 就在 12.127.12.0/24）→ 同网段走直连
#   - 在车里（wlan0 在 192.168.8.0/24）→ 该网段仍走 Tailscale，可访问家里内网
#
# 【用法】
#   a7z-local-route.sh                # 同步一次，幂等，可反复执行
#
# 【配置】/etc/a7z-local-route.conf（可选）
#   NETS="12.127.12.0/24 10.10.0.0/16"   # 多个网段用空格分隔
#
# 【约定】路由表 100 由本脚本独占，不要手工往里加条目。
#
# 【局限】
#   仅处理 IFACE（默认 wlan0）上的直连网段。多网卡/AP 模式（ap0）需要另配 IFACE。
#
# 【回滚】
#   sudo rm -f /etc/NetworkManager/dispatcher.d/91-a7z-local-route
#   sudo systemctl disable --now a7z-local-route.service
#   sudo rm -f /etc/systemd/system/a7z-local-route.service \
#              /usr/local/sbin/a7z-local-route.sh /etc/a7z-local-route.conf
#   sudo ip rule del to 12.127.12.0/24 lookup 100 priority 5260 2>/dev/null
#   sudo ip route flush table 100
set -u

CONF=/etc/a7z-local-route.conf
TABLE=100
PRIO=5260
IFACE=wlan0
LOG_TAG=a7z-local-route

log() {
    local msg="$*"
    # 1) 总是写 stderr —— systemd oneshot 的输出会进 journal，比 syslog 更可靠
    printf '%s: %s
' "$LOG_TAG" "$msg" >&2
    # 2) 再尽力写 syslog；logger 可能不存在（最小系统），失败不影响主流程
    if [ "${NO_SYSLOG:-0}" != "1" ] && command -v logger >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "$msg" 2>/dev/null || true
    fi
}

# 精确判断「to <net> lookup <table>」规则是否已存在。
# 不能用 grep -F "to X lookup 100"：那会把 "lookup 1000" 当成命中（子串前缀）。
# 这里按字段逐个比较，避免任何子串误判。
has_rule() {
    ip rule show 2>/dev/null | awk -v n="$1" -v t="$2" '
        {
            for (i = 1; i < NF; i++) {
                if ($i == "to" && $(i + 1) == n) {
                    for (j = i; j < NF; j++) {
                        if ($j == "lookup" && $(j + 1) == t) { found = 1; exit }
                    }
                }
            }
        }
        END { exit(found ? 0 : 1) }'
}

# 网段列表：优先读配置文件（root 拥有），否则用内置默认
NETS="12.127.12.0/24"
if [ -r "$CONF" ]; then
    # shellcheck disable=SC1090
    if ! . "$CONF" 2>/dev/null; then
        log "WARN 配置文件语法错误，使用内置默认网段: $CONF"
    fi
fi

if [ -z "${NETS:-}" ]; then
    log "no NETS configured, nothing to do"
    exit 0
fi

# 没有无线网卡（或未加载驱动）时无事可做
if ! ip link show "$IFACE" >/dev/null 2>&1; then
    log "$IFACE absent, nothing to do"
    exit 0
fi

# wlan0 当前的**所有**直连网段（可能因多地址/别名而有多个）
# 用整表比较而不是 head -1：多地址时只取第一个会漏判
LINK_NETS=$(ip -4 route show dev "$IFACE" scope link 2>/dev/null | awk '{print $1}')

is_link_net() {
    [ -n "$LINK_NETS" ] || return 1
    printf '%s\n' "$LINK_NETS" | grep -qxF "$1"
}

for NET in $NETS; do
    # 1) 规则：幂等；并发下 add 失败不视为错误（已存在即目标达成）
    if ! has_rule "$NET" "$TABLE"; then
        # 优先级可能已被别的规则占用（同优先级的规则会并列存在，行为不确定）——
        # 占用时明确告警并跳过，不做"看起来成功其实没生效"的事
        if ip rule show 2>/dev/null | grep -q "^${PRIO}:"; then
            log "WARN 优先级 ${PRIO} 已被占用，跳过 $NET（请改 PRIO 或清理冲突规则）"
        elif ip rule add to "$NET" lookup "$TABLE" priority "$PRIO" 2>/dev/null; then
            log "rule added: $NET"
        else
            log "rule add failed: $NET"
        fi
    fi

    # 2) 表内容：只在本机确实属于该网段时才放直连条目
    #    不带 src —— 由内核取接口当前地址，DHCP 换租后无需更新
    if is_link_net "$NET"; then
        if ip route replace "$NET" dev "$IFACE" table "$TABLE" 2>/dev/null; then
            log "direct route set: $NET via $IFACE"
        else
            log "direct route FAILED: $NET via $IFACE"
        fi
    else
        # 精确删除该条目，而不是 flush 整表（避免误伤将来可能加入的其它条目）
        ip route del "$NET" dev "$IFACE" table "$TABLE" 2>/dev/null || true
    fi
done

exit 0

```

### FILE: 91-a7z-local-route
```bash
#!/bin/bash
# NetworkManager dispatcher —— wlan0 状态/地址变化时同步「本地直连路由」
#
# 安装位置：/etc/NetworkManager/dispatcher.d/91-a7z-local-route
# 权限要求（NM 强制，不满足会被静默忽略）：
#   root:root 拥有、不可被 group/other 写、可执行 → chmod 0755
#
# 为什么要这个：开机时 wlan0 还没连上，oneshot 服务跑一次只会「清空表 100」；
# 真正连上 WiFi、拿到新地址（DHCP 换租）时，需要再同步一次。
IFACE="$1"
ACTION="$2"

[ "$IFACE" = "wlan0" ] || exit 0

case "$ACTION" in
    up|dhcp4-change|down|pre-down)
        # 绝对路径：dispatcher 环境的 PATH 很窄
        /usr/local/sbin/a7z-local-route.sh
        ;;
esac

exit 0

```

### FILE: a7z-local-route.service
```bash
[Unit]
Description=A7Z local direct route (bypass Tailscale subnet route for local segments)
Documentation=file:///usr/local/sbin/a7z-local-route.sh
# 只要求 NM 已启动，不等 network-online：
# 开机时 wlan0 通常还没连上，脚本会安全退出并把表 100 清空；
# 真正连上后由 NM dispatcher 91-a7z-local-route 再同步一次。
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
# 单次执行 < 100ms，加超时兜底，避免极端情况下卡住 shutdown 流程
TimeoutStartSec=30
ExecStart=/usr/local/sbin/a7z-local-route.sh
ExecReload=/usr/local/sbin/a7z-local-route.sh

[Install]
WantedBy=multi-user.target

```
