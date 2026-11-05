# 发布记录：a7z-local-route v1.0.1

> 本文档是**运维变更**的发布记录，与 teslausb 主程序的 `v0.3.1.x` 版本线相互独立。
> 回滚步骤可直接照抄执行；验证判据可直接复跑。

## 一、基本信息

| 项 | 值 |
|---|---|
| 组件 | `a7z-local-route`（修 Tailscale subnet route 抢占本地直连网段） |
| 版本 | **1.0.1** |
| 类型 | 首次部署（设备上此前无任何版本） |
| 目标设备 | `radxa-cubie-a7z`（Radxa Cubie A7Z，Debian 11 arm64） |
| 设备地址 | Tailscale `100.116.18.42` / wlan0 `12.127.12.115` |
| 设备主版本 | `teslausb-v0.3.1.69`（09:47:27 启动，本次变更未重启设备） |
| 发布时间 | 2026-11-05 21:56–21:59（CST） |
| 操作人 | OpenClaw（AI 代理，经用户指令执行） |
| 审批 | **用户指令**：「请按正常发布流程将本次变更部署到目标机器上，不要跳过或简化流程」（2026-11-05 21:50） |
| 发布包 | `a7z-local-route-v1.0.1.tar.gz` · 5512 B · SHA256 `7e7a211a66d8c2a1b7249db3e447de174f160e727e2562e2f0e80c035fe18303` |
| 上一版本 | 无（首次部署） |

## 二、变更内容

### 解决的问题

设备在家里的 WiFi（`12.127.12.0/24`）时，从同网段访问 `12.127.12.x:5000` 不通，
但 `100.116.18.42:5000` 正常。

根因：tailnet 内主机 `Docker`（`100.106.205.21`）宣告了 `12.127.12.0/24` 这个 subnet route，
设备接受后 Tailscale 在 **table 52** 写入 `12.127.12.0/24 dev tailscale0`；而
`ip rule 5270: lookup 52` 优先于 `32766: lookup main`，于是设备回包源 IP 变成
`100.116.18.42` → 同网段发起的 TCP 连接因 4 元组不匹配被丢弃。

### 方案

为冲突网段建一条 **priority 5260** 的规则指向专用表 100（先于 tailscale 的 5270）。
表 100 **只在 wlan0 确实属于该网段时**才有条目，且不放 default，因此表为空时
路由查找自然落到 tailscale —— 同时满足两个需求：

- 在家：同网段走本地直连
- 在车里：该网段仍走 Tailscale，可访问家里内网

### 部署的文件

| 路径 | 权限 | 作用 |
|---|---|---|
| `/usr/local/sbin/a7z-local-route.sh` | 0755 root:root | 同步脚本（幂等，带 flock 并发锁） |
| `/etc/NetworkManager/dispatcher.d/91-a7z-local-route` | 0755 root:root | wlan0 状态/DHCP 变化时触发 |
| `/etc/systemd/system/a7z-local-route.service` | 0644 root:root | 开机 oneshot 兜底 |
| `/etc/a7z-local-route.conf` | 0644 root:root | 网段配置，`NETS="12.127.12.0/24"` |

均**不在 teslausb 升级包内**，不受主程序升级影响。

## 三、部署过程（含一次失败与修正）

### 尝试 1：v1.0.0 —— 失败

**现象**：`install.sh` 执行到"立即执行一次同步"时报
`/usr/local/sbin/a7z-local-route.sh: /bin/bash^M: bad interpreter: No such file or directory`

**根因**：Windows 工作区 `core.autocrlf` 使脚本为 CRLF 行尾，shebang 变成 `/bin/bash\r`。
连带影响：`a7z-local-route.conf` 里的 `\r` 会让网段字符串比较永远失败（功能静默失效）。

**处置**：
1. 源文件统一转 LF（5 个部署文件 + 文档）
2. `install.sh` 增加**行尾规范化**步骤（对 4 个落盘文件 `sed -i 's/\r$//'`），
   使无论源码来自哪个平台都能用
3. 追加 `.gitattributes`：`ops/a7z-local-route/* text eol=lf`

**同时发现的第二个问题**：v1.0.0 的 `install.sh` 直接执行脚本而非通过 systemd，
导致 `systemctl is-active` 恒为 `inactive`（oneshot + `RemainAfterExit=yes` 本应
显示 `active (exited)`），运维检查会误判为没装好。改为 `systemctl restart`。

因此**升版 1.0.1** 重新发布。v1.0.0 从未成功运行过，无需回滚。

### 尝试 2：v1.0.1 —— 成功

```
① 发布前检查      工作树仅本次相关改动；HEAD == origin/master（7e02bd1）
② 目标路径检查    4 个安装位置均不存在 → 首次安装，无需覆盖备份
③ 生成发布包      tar czf → 5512 B；包内 5 个文件；确认全部 LF
④ 上传 + 校验     sha256 设备侧计算 == 本地（7e7a211a…）
⑤ 部署前备份      /tmp/pre-deploy-20261105/{ip-rule-before,table100-before,uptime-before,version-before}.txt
⑥ 安装预演        install.sh --dry-run → 计划 4 装 + daemon-reload + enable + 启动
⑦ 正式安装        全部步骤成功，无报错
```

### 部署前设备状态（备份留档）

| 项 | 值 |
|---|---|
| `ip rule` | 无 5260；仅 5210/5230/5250/5270/32766/32767 |
| 表 100 | 空（不存在条目） |
| 4 个安装位置 | 全部不存在 |
| `uptime -s` | 2026-11-05 09:47:27 |
| 主版本 | `teslausb-v0.3.1.69` |

备份位置：设备 `/tmp/pre-deploy-20261105/`（重启自清，需要长期留存请另行复制）。

## 四、验证结果（全部通过）

| # | 判据 | 命令 | 结果 |
|---|---|---|---|
| V1 | 优先级规则存在 | `ip rule show \| grep 'lookup 100'` | `5260: from all to 12.127.12.0/24 lookup 100` ✅ |
| V2 | 回包走本地网卡 | `ip route get 12.127.12.122` | `dev wlan0 table 100 src 12.127.12.115`（修复前是 `dev tailscale0 table 52 src 100.116.18.42`）✅ |
| V3 | 开机自启 | `systemctl is-enabled` | `enabled` ✅ |
| V4 | 服务状态 | `systemctl is-active` | `active` ✅ |
| V5 | dispatcher 权限（NM 强制） | `stat -c '%U:%G %a'` | `root:root 755` ✅ |
| V6 | 版本号 | `grep VERSION` | `VERSION=1.0.1` ✅ |
| V7 | 本机 ping | `ping 12.127.12.115` | 0% 丢包（7–58 ms）✅ |
| V8 | 本机 HTTP | `curl 12.127.12.115:5000` | `200` / 0.78 s ✅ |
| **V9** | **模拟"在车里"**：清空表 100 后应回落 Tailscale | `ip route flush table 100` → `ip route get` | 回到 `dev tailscale0 table 52 src 100.116.18.42`，本机访问中断（证明回落生效）✅ |
| V10 | 恢复后复验 | `systemctl restart` → `curl` | 表 100 恢复 1 条、路由回 wlan0、`200` ✅ |
| V11 | 幂等 | 二次执行日志 | 只写 `direct route set`，不再 `rule added` ✅ |
| V12 | 离线回归 | `bash test-offline.sh` | **9 场景 21 断言全过** ✅ |

**V9 是本次最关键的一条**：它证明「在车里经 Tailscale 访问家里 `12.127.12.0/24`」这个
硬需求没有被本次修复破坏。

**未做的验证（及原因）**：未重启设备。理由：本篇改动不涉及内核/驱动/主程序，重启收益低，
而该设备重启涉及 USB Gadget 与硬件看门狗链路（见 M107 教训），风险不成比例。
`systemctl is-enabled` + 实际 `restart` 已覆盖开机路径。

## 五、回滚方案

### 完整回滚（撤销本次部署）

```bash
# 1 停用并移除 systemd 单元
sudo systemctl disable --now a7z-local-route.service
sudo rm -f /etc/systemd/system/a7z-local-route.service
sudo systemctl daemon-reload

# 2 移除 dispatcher 钩子
sudo rm -f /etc/NetworkManager/dispatcher.d/91-a7z-local-route

# 3 移除脚本与配置
sudo rm -f /usr/local/sbin/a7z-local-route.sh /etc/a7z-local-route.conf

# 4 撤销运行时规则与路由表
sudo ip rule del to 12.127.12.0/24 lookup 100 priority 5260 2>/dev/null
sudo ip route flush table 100

# 5 验证已恢复原状
ip rule show | grep -c 'lookup 100'      # 期望 0
ip route get 12.127.12.122               # 期望 dev tailscale0 table 52
```

**回滚影响**：回到"同网段互访不通"的原始状态，其他功能不受影响。无需重启。

### 紧急临时回滚（只恢复路由，保留文件）

```bash
sudo ip route flush table 100        # 立刻回落到 Tailscale
sudo systemctl stop a7z-local-route.service
```
说明：表 100 清空后规则虽在，但查表无结果 → 自然落到 tailscale，等效于未生效。

## 六、归档位置

| 内容 | 位置 |
|---|---|
| **源码（权威）** | 仓库 `ops/a7z-local-route/`（脚本、dispatcher、service、conf 示例、install.sh） |
| 说明文档 | 同上 `README.md`（原理/安装/验证/回滚/局限） |
| 落地前审查报告 | 同上 `REVIEW.md`（8 处问题与修复、9 场景验证、oracle 不可用记录） |
| 离线测试 | 同上 `test-offline.sh`（可随时复跑） |
| 交叉审查包 | 同上 `oracle-review-pack.md`（prompt + 3 文件全文，可粘贴到任意 LLM） |
| **本发布记录** | `docs/releases/2026-11-05-a7z-local-route-v1.0.1.md` |
| 发布包 | `.workbuddy/artifacts/a7z-local-route-v1.0.1.tar.gz` |
| Git tag | `ops-a7z-local-route-v1.0.1` |
| 设备侧备份 | `/tmp/pre-deploy-20261105/`（临时，重启自清） |
| 运行日志 | 设备 `journalctl -t a7z-local-route` |
| 工作记忆 | `.workbuddy/memory/2026-11-05.md` |

## 七、已知风险与后续

| 项 | 说明 | 处理 |
|---|---|---|
| 网段硬编码 | 默认 `12.127.12.0/24`；换路由器/换网段需改 `/etc/a7z-local-route.conf` | 已在 conf 注释写明如何查该填什么 |
| 单网卡 | 只处理 `wlan0`；AP 模式（`ap0`）未覆盖 | 已在 README「局限」注明 |
| 优先级占用 | 若 5260 被别的规则占用，脚本 WARN 并跳过 | 已有检查与日志 |
| 规则常驻 | 规则长期存在（表为空时无副作用） | 设计即如此，回滚命令可清除 |
| s6 场景 | 离线测试 S6（网卡在但无地址）在 CRLF 源码下曾解析异常 | 源码已强制 LF + 加 `.gitattributes` |
