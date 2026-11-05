# a7z-local-route

修「**同网段却互访不通**」：Tailscale 的 subnet route 抢走了本地直连网段的优先路由。

## 症状

设备在家里的 WiFi（`12.127.12.0/24`），从同网段的电脑访问 `12.127.12.x:5000`：

- `ping` 超时
- `curl` 连不上
- 但用 Tailscale 地址 `100.116.18.42:5000` 正常

## 根因

tailnet 里另一台机器宣告了 `12.127.12.0/24` 作为 subnet route，设备接受后 Tailscale 在
**table 52** 写入：

```
12.127.12.0/24 dev tailscale0
```

而 `ip rule 5270: lookup 52` 的优先级高于 `32766: lookup main`，于是设备回包给同网段设备时
**走隧道、源 IP 变成 100.116.18.42**：

```
$ ip route get 12.127.12.122
12.127.12.122 dev tailscale0 table 52 src 100.116.18.42    ← 应该是 dev wlan0 src 12.127.12.x
```

对方按 `12.127.12.126:5000` 发起的连接，收到的是来自 `100.116.18.42` 的 SYN-ACK，
4 元组不匹配 → 内核丢弃 → **TCP 握手不上**。ping 同理。

> 排查口诀：**同网段却互 ping 不通，先看 `ip route get <对端IP>` 的回包接口**，
> 比抓包快得多；再 `ip rule show` + `ip route show table 52` 找是谁注入的路由。

## 方案

为冲突网段建一条**更高优先级**的规则，指向专用表 100：

```
5260: from all to 12.127.12.0/24 lookup 100     ← 本套件加的（先查）
5270: from all lookup 52                        ← Tailscale（后查）
32766: from all lookup main
```

表 100 **只在 wlan0 确实属于该网段时**才有条目，且**不放 default**，所以：

| 场景 | 表 100 | 结果 |
|---|---|---|
| 在家（wlan0 = `12.127.12.x`） | 有 `12.127.12.0/24 dev wlan0` | 走本地直连 ✅ |
| 在车里（wlan0 = `192.168.8.x`） | 空 | 路由查找落到下一条 rule → Tailscale ✅ |

**这就是为什么不用「直接关掉 `accept-routes`」**——那会让「在车里访问家里内网」失效。

## 安装

```bash
sudo ./install.sh --dry-run   # 先看要做什么
sudo ./install.sh
```

安装内容（都在升级包之外，不受固件升级影响）：

| 路径 | 作用 |
|---|---|
| `/usr/local/sbin/a7z-local-route.sh` | 同步脚本（幂等） |
| `/etc/NetworkManager/dispatcher.d/91-a7z-local-route` | WiFi 状态/地址变化时触发 |
| `/etc/systemd/system/a7z-local-route.service` | 开机 oneshot 兜底 |
| `/etc/a7z-local-route.conf` | 网段配置（已存在则保留） |

## 配置

`/etc/a7z-local-route.conf`：

```bash
NETS="12.127.12.0/24"        # 多个网段用空格分隔
```

怎么看该填什么：

```bash
ip route show table 52                    # 被 Tailscale 接管的网段
ip -4 route show dev wlan0 scope link     # 本机 WiFi 实际所在网段
```

两者重叠的网段就是需要填的。

## 验证

```bash
# 1 规则在
ip rule show | grep 'lookup 100'

# 2 在家时应走 wlan0（把 <家里电脑IP> 换成实际地址）
ip route get <家里电脑IP>
#   期望：dev wlan0 table 100 src 12.127.12.x

# 3 从家里电脑访问
ping <设备IP> && curl -I http://<设备IP>:5000/

# 4 模拟「在车里」：清空表 100，应回落到 Tailscale
sudo ip route flush table 100
ip route get <家里电脑IP>
#   期望：dev tailscale0 table 52 src 100.116.18.42
# 验证完再跑一次脚本恢复：sudo /usr/local/sbin/a7z-local-route.sh

# 5 日志
journalctl -t a7z-local-route -n 20 --no-pager
```

## 回滚

```bash
sudo rm -f /etc/NetworkManager/dispatcher.d/91-a7z-local-route
sudo systemctl disable --now a7z-local-route.service
sudo rm -f /etc/systemd/system/a7z-local-route.service \
           /usr/local/sbin/a7z-local-route.sh \
           /etc/a7z-local-route.conf
sudo ip rule del to 12.127.12.0/24 lookup 100 priority 5260 2>/dev/null
sudo ip route flush table 100
```

## 局限

- 只处理 `IFACE`（默认 `wlan0`）上的直连网段；AP 模式（`ap0`）需另配。
- 路由表 100 由本脚本独占，**不要手工往里加条目**。
- 优先级 `5260` 若已被别的规则占用，脚本会告警并跳过（不会静默插重复规则）。

## 测试

```bash
bash test-offline.sh     # 用 mock 的 ip/flock 跑 9 个场景、21 项断言，不碰真机
```

覆盖：在家写直连 / 在车里清条目 / 幂等不重复 / 无 wlan0 零操作 /
`lookup 1000` 不误判为 `lookup 100` / 有网卡无地址时清条目 /
优先级被占用时告警跳过 / 持锁时立即退出 / 锁文件不可写时告警继续。
