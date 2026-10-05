---
name: a7z-apsta-concurrency
description: A7Z（AIC8800D80）WiFi AP+STA 并发验证与落地：带守护网在真机上建第二 vif、起 hostapd+dnsmasq+NAT，验证 AP 与 STA 同在线。用于"AP 与 WiFi 共存/不再来回切模式"的排查与改造。
color: teal
agent_created: true
---

# A7Z AP+STA 并发验证（带守护网）

## 触发条件

- 需要验证/实现「AP 与 STA 同时工作」（不再 AP↔STA 来回切）
- 排查「AP 开后 WiFi 切不回」「单射频不能共存」类问题
- 在真机上做任何**可能切断 SSH**（Tailscale 走 wlan0）的网络实验
- 关键词：AP+STA 并发、双 vif、hostapd 第二接口、AIC8800 共存

## 核心结论（已验证，2026-10-04）

- 芯片 **AIC8800D80**（双频），驱动 `aic8800_fdrv` v6.4.3.0
- `iw phy` 声明：`#{ managed, mesh point } <= 1, #{ AP } <= 1, ... total <= 4, #channels <= 3`
  → **AP 与 STA 可并发**（各 ≤1）
- **"单射频不能共存"是当前实现的选择，不是硬限制**
- 实机已端到端通过：AP 广播+DHCP+DNS+NAT 可用，`wlan0` STA 全程不掉线，手机连 AP 有网且能访问 A7Z Web

## 铁律

1. **先铺守护网再动网络**。远程只有 Tailscale 走 wlan0，实验一旦断网就失联。
2. 守护网用 **`systemd-run --on-active=<N>` 瞬时定时器**，与 SSH 会话解耦（SSH 断了也照样回滚）。
3. **动手前先做「无害验证」**：跑一条 `systemd-run --on-active=8 ... touch /tmp/x`，确认定时器真能触发，再进行真实实验。
4. **禁止 `systemctl restart NetworkManager`**（M53，会毁本机 DNS）——恢复用 `nmcli device connect` / `nmcli connection up`。
5. 脚本上传后必须 `sed -i 's/\r$//'` 去 CRLF（Windows 工作树）。
6. 工具全用**绝对路径**（非登录 shell 的 PATH 不含 /sbin）：`/sbin/iw`、`/usr/sbin/hostapd`、`/usr/sbin/hostapd_cli`、`/usr/sbin/dnsmasq`、`/usr/bin/nmcli`。

## 步骤

### 0. 只读预检
```bash
nmcli -t -f DEVICE,STATE,CONNECTION device status   # 当前 STA 连的哪个 SSID
/sbin/iw dev wlan0 link                              # 当前信道/频率（决定 AP 信道）
/sbin/iw phy | grep -A3 "valid interface combinations"
systemctl is-active hostapd dnsmasq                  # 应 inactive
cat /proc/sys/net/ipv4/ip_forward                    # 需 =1
```

### 1. 铺守护网（rollback 脚本，幂等）
`rollback.sh` 要点（见 `references/rollback.sh`）：
- 杀测试 `hostapd`（by pid file + `pkill -f <testconf>`）与独立 `dnsmasq`
- 删本测试加的 iptables：`POSTROUTING MASQUERADE`、两条 `FORWARD`
- 删**除 wlan0 外**的一切命名 vif（`for IF in $(iw dev | awk '/Interface /{print $2}')`）
- 恢复：`nmcli device set wlan0 managed yes` → `ip link set wlan0 up` → `nmcli connection up <SSID>`
- 打印恢复后 `iw dev` / `wlan0 link` / `ping 网关` 佐证

武装：`systemd-run --on-active=<N> --unit=a7z-apitest-guard /bin/bash /tmp/rollback.sh`
（先跑无害 touch 验证机制；结束后 `systemctl stop <unit>.timer <unit>.service`）

### 2. 建 AP 第二 vif（**关键**）
```bash
/sbin/iw dev wlan0 interface add ap0 type __ap    # phy 支持第二 vif；wlan0 STA 不受扰
NEWIF=$(/sbin/iw dev | awk '/Interface /{print $2}' | grep -vx wlan0 | head -1)
# ↑ udev 会改名成 wlx<mac>，必须以实际名引用
nmcli device set "$NEWIF" managed no               # ★ 必做：否则 NM 抢管、把 vif 打回 managed
/sbin/iw dev "$NEWIF" set type __ap                # 强制 AP 类型
```

### 3. 起 AP（hostapd + dnsmasq + NAT）
```bash
ip addr add 192.168.42.1/24 dev "$NEWIF"; ip link set "$NEWIF" up
# hostapd.conf：interface=$NEWIF, hw_mode=g, channel=<STA 当前信道>, ctrl_interface=/var/run/<name>
/usr/sbin/hostapd -B -P /tmp/<name>.pid /tmp/<name>.conf
# dnsmasq 独立实例，只绑 AP 接口（不碰系统 dnsmasq / resolved）
/usr/sbin/dnsmasq --conf-file=/dev/null --interface="$NEWIF" --bind-interfaces \
  --dhcp-range=192.168.42.10,192.168.42.100,12h \
  --dhcp-option=3,192.168.42.1 --dhcp-option=6,192.168.42.1 --pid-file=/tmp/<name>.pid
# NAT：AP 网段 → wlan0（STA 上行）
iptables -t nat -A POSTROUTING -s 192.168.42.0/24 -o wlan0 -j MASQUERADE
iptables -A FORWARD -i "$NEWIF" -o wlan0 -j ACCEPT
iptables -A FORWARD -i wlan0 -o "$NEWIF" -m state --state RELATED,ESTABLISHED -j ACCEPT
```

### 4. 验证
```bash
/sbin/iw dev                                    # 期望 AP 与 managed(wlan0) 同在线
/usr/sbin/hostapd_cli -p /var/run/<name> -i "$NEWIF" status   # state=ENABLED
/sbin/iw dev wlan0 link                         # STA 仍连原 SSID
ping -c2 <STA 网关>                              # 0% 丢包
ss -lnup | grep 192.168.42                       # dnsmasq 53/67
```
**端到端**：用手机连 AP（SSID/密码）→ 应能上网、并能打开 A7Z 后台 `http://192.168.42.1:5000`。

### 5. 收尾
手动触发 rollback → 确认仅剩 wlan0 且连回原 SSID → 停/清守护定时器 → 清 `/tmp` 残留。

## 落地改造要点（若要把并发做成常态功能）

- **新建 AP vif**，不要复用 wlan0（复用即"独占"，就是当前痛点）
- 建 vif 后**立刻**置 NM unmanaged + 强制 `type __ap`（顺序不能反）
- AP **同信道**跟随 STA（`hostapd.conf` 的 `channel` 要动态生成，不能用写死的 `6`）
- 现有 `hostapd.conf`(`interface=wlan0`,`channel=6`) 与 iptables(`-i wlan0`)、`wifi_service._write_hostapd_conf`、captive portal 规则都要改成 **AP vif 名**
- 保留 fail-open + 手动/超时出口

## 已知坑

- `iw`/`hostapd`/`wpa_supplicant`/`dnsmasq` 不在非登录 shell 的 PATH → `command -v` 误报 MISSING，须用绝对路径
- udev 把新 vif 改名 `wlx<mac>`（可预测命名），`hostapd.conf` 必须用实际名
- NM 默认会**自动接管**新 vif 并置 managed → 不先 `managed no` 则 hostapd 起不来（`Could not read interface ap0 flags: No such device`）
- 单射频并发：AP 与 STA 必须**同信道**；吞吐分时共享
- `AP_STATIC_IP=192.168.42.1` 与现有 captive portal 方案一致，可复用

## 已在 A7Z 落地（v0.3.1.62 代码，未发版）

开关 `config.AP_STA_CONCURRENT_DEFAULT=False` + 运行时 `config/ap_config.json:ap_sta_concurrent`（UI 实时可切）。实现见 `wifi_service.py`：`_use_concurrent` / `_ap_vif_add·del` / `_ap_bring_up·down_concurrent` / `_sync_ap_channel`(CSA 跟随) / `_remote_lock_risk`(M94 自锁闸) / `_yield_ap` 并发分支；各 AP 函数分流,legacy 零改动。**UI 开关**在 `templates/wifi.html`「AP 配置」区。

## 真机测试新坑（10-05 实证，务必先读）

1. **timer 有「两个」自愈源，`systemctl stop` 会被拉回**：
   - ① `wifi_service.ensure_smart_switch_timers()`（Web 启动 / M45 loop 调）
   - ② **`sentry_watchdog.py` 也调它**
   → 停 timer 后会被自愈重启,**隔离测试须临时 `mv services/*.timer *.timer.testoff`**（自愈 `src-missing` 才跳过），**测完务必移回**。
2. **多客户端 AP 会污染「断开即关」测试**：只要 AP 上还有**任何其它**设备，回调/自愈的"仍有客户端"守卫会（正确地）不关 AP。测前务必确认 station dump 只有被测设备（10-05 现场混入陌生设备 `da:69:31:b5:b3:e7` → 误判"回调没生效"）。
3. **事件回调的确定性验证法（推荐，不依赖现场环境）**：
   ```bash
   echo radxa | sudo -S sh -c '
     rm -f /tmp/ap-client-event.log /var/run/ap-yield-pending
     /opt/radxa_data/teslausb/ap_client_event.sh AP-STA-DISCONNECTED <mac>
     sleep 18
     cat /tmp/ap-client-event.log; iw dev | awk "/Interface /{print \$2}" | grep -v wlan0'
   ```
   期望：回调日志出现 `yield-ap: ...（if=<vif>）` + `AP 事件回调(并发): ... 关闭 ap0`，且 ap0 消失。
4. **CSA 跟随验证**：`hostapd_cli -p /var/run/hostapd-ap0 -i <vif> chan_switch 5 2437` 手动漂到 ch6 → `_sync_ap_channel()` 应 CSA 拉回 STA 信道。
5. **改并发开关配置须用 sudo**：`ap_config.json` 是 **root 属主**，radxa 直写静默失败（`PermissionError`）——写后**必须回读确认**（`_use_concurrent()`）再操作（M94 事故根因）。
6. **远程自锁**：legacy 路径起 AP 会释放 wlan0；Tailscale 走 wlan0 时**会自断链路**。M94 闸门已拦（`start_ap(force=False)` 在"wlan0 为唯一默认路由"时拒绝）。

