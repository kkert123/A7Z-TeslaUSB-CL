---
name: a7z-bluetooth-obex
description: A7Z 蓝牙文件传输（OBEX/OPP）配置与排障：装 bluez-obexd、用 obexd 作文件接收端、bluetoothctl 自动配对 agent、可发现配置。用于"手机↔A7Z 蓝牙传文件"的搭建与调试。
color: purple
agent_created: true
---

# A7Z 蓝牙文件传输（OBEX / Object Push）

## 触发条件

- 用户想用蓝牙在手机与 A7Z 之间传文件
- 排查蓝牙配对失败、传文件失败、obexd 不工作
- 关键词：蓝牙传文件、OBEX、OPP、Object Push、obexd、配对卡住

## 结论（实测，2026-10-04）

- **手机(Android) → A7Z：可行**（OPP 传文件，obexd `-a` 自动接收）
- **A7Z → 手机：不可行**（Android 默认不跑 OBEX 服务端）
- **iPhone：完全不支持 OBEX**（Apple 硬限制），双向都不行
- 蓝牙速度慢（~100KB/s–1MB/s）→ 大文件/双向用 **WiFi/HTTP**（见 skill a7z-apsta-concurrency）

## 步骤

### 1. 装 OBEX 组件
```bash
# ⚠️ bullseye 安全池已 EOL，直接装会 404（索引指向已删除的 u2 版本）
apt-cache madison bluez-obexd          # 看有没有 main 源版本(deb11u1)
apt-get install -y bluez-obexd=5.55-3.1+deb11u1 obexftp
# 装完确认：/usr/libexec/bluetooth/obexd 存在
```
> `apt-get update` 无法解决 404——索引本身也指向被删版本；必须**锁定 main 源版本**。

### 2. 启 obexd（headless 场景）
Debian 提供的是 **systemd user service**（`/usr/lib/systemd/user/obex.service`）：
```bash
loginctl enable-linger radxa          # ★ 必做：否则 SSH 断开 → 用户管理器退出 → obexd 被杀
mkdir -p ~/bt_received ~/.config/systemd/user/obex.service.d
cat > ~/.config/systemd/user/obex.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/obexd -r /home/radxa/bt_received -a
EOF
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
systemctl --user daemon-reload && systemctl --user enable --now obex.service
```
验证：`bluetoothctl show | grep OBEX` → 应出现 **OBEX Object Push (1105)** + **File Transfer (1106)**。

### 3. 配对 agent（自动确认，免手动 yes）
```bash
# bluetoothctl 的 stdin 接 FIFO，自动喂 yes（否则 passkey 确认卡住 → "无法连接"）
rm -f /tmp/btctl_fifo; mkfifo /tmp/btctl_fifo
bluetoothctl < /tmp/btctl_fifo > /tmp/bt_agent.log 2>&1 &
{
  echo "power on"; echo "agent NoInputNoOutput"; echo "default-agent"
  echo "system-alias TeslaUSB-A7Z"; echo "pairable on"
  echo "discoverable-timeout 0"; echo "discoverable on"; echo "pairable on"
  sleep 3
  while true; do echo yes; sleep 2; done      # 关键：持续喂 yes
} > /tmp/btctl_fifo &
```
（完整脚本见 `references/bt_agent_start.sh`；须 `setsid` 脱离 SSH 会话）

### 4. 测试
手机：蓝牙搜索 `TeslaUSB-A7Z` → 配对 → 文件管理器分享→蓝牙→发送。
设备：文件落 `~/bt_received`。

## 三个必踩的坑

| 坑 | 现象 | 根因 | 解法 |
|---|---|---|---|
| OBEX 装不上 | apt 404 | bullseye 安全池 EOL，索引指向已删版本 | 锁 main 源版本 `=5.55-3.1+deb11u1` |
| obexd 掉线 | 一会就 inactive | user service + `Linger=no`，SSH 断即杀 | `loginctl enable-linger` |
| 配对卡住 | 手机「无法连接」 | agent 弹 `(yes/no)` 等 stdin，哑进程无人答 → `Request canceled` | stdin 接 FIFO 自动喂 `yes` |

## 安全提示

BT 永久可发现 + obexd `-a` 自动接受 = **附近任何人可向 A7Z 推文件**。
常驻化须加约束：仅已配对设备 / 限时可发现 / 大小与类型限制 / 隔离接收目录。

## 排障命令

```bash
bluetoothctl show            # 看 Powered/Discoverable/Pairable/UUID(1105/1106)
bluetoothctl paired-devices  # 看已配对
cat /tmp/bt_agent.log        # agent 日志（含 Confirm passkey / Request canceled）
export XDG_RUNTIME_DIR=/run/user/1000; systemctl --user status obex.service
journalctl --user -u obex.service --no-pager -n 20
ls -la ~/bt_received         # 收到没
```
