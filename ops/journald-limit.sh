#!/usr/bin/env bash
# journald 容量限制（v0.3.1.74 / M121）
#
# 作用：把 systemd journal 的总占用限制在固定大小（默认 1G），并立即收缩现有日志。
# 为什么需要：A7Z 根分区只有 28G，journald 默认不设上限，曾涨到 2.7G。
#
# 用法：
#   sudo ./journald-limit.sh          # 默认 1G
#   sudo ./journald-limit.sh 512M     # 自定义
#   sudo ./journald-limit.sh --check  # 只查看当前配置与占用
#
# 幂等：重复执行只会把数值改成同一个值，不会重复追加配置。
set -euo pipefail

CONF=/etc/systemd/journald.conf
LIMIT="${1:-1G}"

if [ "${LIMIT}" = "--check" ]; then
    echo "== 当前配置 =="
    grep -E '^\s*SystemMaxUse\s*=' "${CONF}" || echo "(未设置，等于不限制)"
    echo "== 当前占用 =="
    journalctl --disk-usage 2>/dev/null || true
    exit 0
fi

if ! [[ "${LIMIT}" =~ ^[0-9]+[KMG]$ ]]; then
    echo "!! 容量格式不合法：${LIMIT}（示例 512M / 1G）" >&2
    exit 1
fi

# 备份一次（只在首次修改前保留原始文件）
if [ ! -f "${CONF}.orig" ]; then
    cp -a "${CONF}" "${CONF}.orig"
    echo "已备份原配置 → ${CONF}.orig"
fi

if grep -qE '^\s*SystemMaxUse\s*=' "${CONF}"; then
    sed -i -E "s|^\s*SystemMaxUse\s*=.*|SystemMaxUse=${LIMIT}|" "${CONF}"
else
    if grep -qE '^\s*\[Journal\]\s*$' "${CONF}"; then
        sed -i -E "0,/^\s*\[Journal\]\s*$/s||[Journal]\nSystemMaxUse=${LIMIT}|" "${CONF}"
    else
        printf '\n[Journal]\nSystemMaxUse=%s\n' "${LIMIT}" >> "${CONF}"
    fi
fi

systemctl restart systemd-journald
sleep 1
# 立即收缩（不会删除正在写入的当前 journal）
journalctl --vacuum-size="${LIMIT}" >/dev/null

echo "== 结果 =="
grep -E '^\s*SystemMaxUse\s*=' "${CONF}"
journalctl --disk-usage
