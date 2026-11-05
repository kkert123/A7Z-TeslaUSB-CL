#!/bin/bash
# 离线行为验证：mock 掉 ip/logger，跑真脚本，检查各场景下的实际动作序列
# 目的：不碰设备就能验证「在家走直连 / 在车里回落 tailscale / 幂等 / 网卡缺失 / 子串陷阱」
#
# 注意两个沙箱约束（都踩过）：
#   1 不能用 mktemp -d：系统 Temp 在 bash 里读不到，mock 创建失败会让脚本回落到真 ip；
#   2 不能出现递归删除目录的命令：会被删除保护拦截，整条命令被杀且无输出。
# 所以工作目录固定为脚本同级的 _work，只创建不清理。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
WORK="$HERE/_work"
MOCK="$WORK/bin"
mkdir -p "$MOCK"

cat > "$MOCK/ip" <<'MOCKIP'
#!/bin/bash
echo "CALL $*" >> "$M_LOG"

if [ "$1" = "link" ] && [ "$2" = "show" ]; then
    [ "${M_LINK_EXISTS:-1}" = "1" ] || exit 1
    echo "3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500"
    exit 0
fi
if [ "$1" = "rule" ] && [ "$2" = "show" ]; then
    echo "0:	from all lookup local"
    for r in ${M_RULES:-}; do
        net=${r%%:*}; rest=${r#*:}; tbl=${rest%%:*}; prio=${rest#*:}
        [ "$prio" = "$tbl" ] && prio=5260
        echo "${prio}:	from all to ${net} lookup ${tbl}"
    done
    echo "5270:	from all lookup 52"
    echo "32766:	from all lookup main"
    exit 0
fi
if [ "$1" = "rule" ] && [ "$2" = "add" ]; then exit 0; fi
if [ "$1" = "-4" ] && [ "$2" = "route" ] && [ "$3" = "show" ]; then
    for n in ${M_LINK_NETS:-}; do echo "$n dev wlan0 proto kernel scope link"; done
    exit 0
fi
if [ "$1" = "route" ]; then exit 0; fi
exit 0
MOCKIP

chmod +x "$MOCK/ip"
cat > "$MOCK/flock" <<'MOCKFLOCK'
#!/bin/bash
[ "${M_FLOCK_BUSY:-0}" = "1" ] && exit 1
exit 0
MOCKFLOCK
chmod +x "$MOCK/flock"


SCRIPT="$HERE/a7z-local-route.sh"
LOG="$WORK/calls.txt"
PASS=0; FAIL=0

ck() {
    if [ "$2" = "1" ]; then PASS=$((PASS+1)); echo "PASS $1";
    else FAIL=$((FAIL+1)); echo "FAIL $1 ${3:-}"; fi
}
has()   { grep -qF "$1" "$LOG" 2>/dev/null && echo 1 || echo 0; }
hasnt() { grep -qF "$1" "$LOG" 2>/dev/null && echo 0 || echo 1; }

run_case() {
    local name="$1"
    : > "$LOG"
    echo "── $name"
    # stderr 里就是 log() 的输出；NO_SYSLOG=1 跳过外部 logger（MSYS 下反复 fork 会卡）
    ( export PATH="$MOCK:/usr/bin:/bin"
      export M_LOG="$LOG"
      export NO_SYSLOG=1
      export A7Z_LOCK_FILE="${A7Z_LOCK_FILE:-$WORK/a7z.lock}"   # 仅未指定时给默认，S9 要靠它注入不可写路径
      export M_FLOCK_BUSY="${M_FLOCK_BUSY:-0}"
      bash "$SCRIPT" >/dev/null 2>>"$LOG" )
    grep -E '^(CALL|a7z-local-route:)' "$LOG" 2>/dev/null | sed 's/^/   /'
}

# ── S1 在家 ──
M_LINK_EXISTS=1 M_LINK_NETS="12.127.12.0/24" M_RULES=""
export M_LINK_EXISTS M_LINK_NETS M_RULES
run_case "S1 在家（期望 add rule + replace 直连）"
ck "S1.1 添加了规则"  "$(has 'rule add to 12.127.12.0/24 lookup 100 priority 5260')"
ck "S1.2 写入直连条目" "$(has 'route replace 12.127.12.0/24 dev wlan0 table 100')"
ck "S1.3 没有误删"     "$(hasnt 'route del 12.127.12.0/24')"

# ── S2 在车里 ──
M_LINK_NETS="192.168.8.0/24"; M_RULES=""; export M_LINK_NETS M_RULES
run_case "S2 在车里（期望规则保留、表条目清空）"
ck "S2.1 规则仍添加"   "$(has 'rule add to 12.127.12.0/24 lookup 100')"
ck "S2.2 不写直连"     "$(hasnt 'route replace 12.127.12.0/24')"
ck "S2.3 删除残留条目" "$(has 'route del 12.127.12.0/24 dev wlan0 table 100')"

# ── S3 幂等 ──
M_LINK_NETS="12.127.12.0/24"; M_RULES="12.127.12.0/24:100"; export M_LINK_NETS M_RULES
run_case "S3 幂等（规则已存在）"
ck "S3.1 未重复添加"   "$(hasnt 'rule add ')"
ck "S3.2 仍同步直连"   "$(has 'route replace 12.127.12.0/24')"

# ── S4 无 wlan0 ──
M_LINK_EXISTS=0; M_LINK_NETS=""; M_RULES=""; export M_LINK_EXISTS M_LINK_NETS M_RULES
run_case "S4 无 wlan0（期望零写操作）"
ck "S4.1 无 rule add"  "$(hasnt 'rule add ')"
ck "S4.2 无 route 写"  "$(hasnt 'route replace')"
ck "S4.3 有退出日志"   "$(grep -q 'absent' "$LOG" && echo 1 || echo 0)"

# ── S5 子串陷阱 ──
M_LINK_EXISTS=1; M_LINK_NETS="12.127.12.0/24"; M_RULES="12.127.12.0/24:1000:5300"
export M_LINK_EXISTS M_LINK_NETS M_RULES
run_case "S5 已有 lookup 1000（优先级 5300，不应误判为 100）"
ck "S5.1 仍会添加 lookup 100" "$(has 'rule add to 12.127.12.0/24 lookup 100 priority')"

# ── S6 有网卡未拿地址 ──
M_LINK_NETS=""; M_RULES="12.127.12.0/24:100"; export M_LINK_NETS M_RULES
run_case "S6 网卡在但无地址"
ck "S6.1 清空直连条目" "$(has 'route del 12.127.12.0/24 dev wlan0 table 100')"

# ── S7 优先级被占用 ──
M_LINK_NETS="12.127.12.0/24"; M_RULES="10.99.0.0/16:200:5260"; export M_LINK_NETS M_RULES
run_case "S7 优先级 5260 已被别的规则占用（应告警并跳过，不硬插）"
ck "S7.1 不强行添加"   "$(hasnt 'rule add to 12.127.12.0/24')"
ck "S7.2 有 WARN 日志"  "$(grep -q 'WARN' "$LOG" && echo 1 || echo 0)"
ck "S7.3 仍同步直连条目" "$(has 'route replace 12.127.12.0/24')"

# ── S8 并发：锁被占用 ──
M_LINK_NETS="12.127.12.0/24"; M_RULES=""; M_FLOCK_BUSY=1; export M_LINK_NETS M_RULES M_FLOCK_BUSY
run_case "S8 另一个实例持锁（期望立即退出、零写操作）"
ck "S8.1 不加规则"    "$(hasnt 'rule add ')"
ck "S8.2 不写路由"    "$(hasnt 'route replace')"
ck "S8.3 有 skip 日志" "$(grep -q 'another instance' "$LOG" && echo 1 || echo 0)"

# ── S9 锁文件不可写：应告警但仍继续 ──
M_FLOCK_BUSY=0; M_LINK_NETS="12.127.12.0/24"; M_RULES=""
A7Z_LOCK_FILE="/proc/nonexistent/a7z.lock"; export M_FLOCK_BUSY M_LINK_NETS M_RULES A7Z_LOCK_FILE
run_case "S9 锁文件不可写（期望 WARN 后继续执行）"
ck "S9.1 仍加规则"   "$(has 'rule add to 12.127.12.0/24')"
ck "S9.2 有 WARN"    "$(grep -q '锁文件不可写' "$LOG" && echo 1 || echo 0)"

echo ""
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
