# 自检守护 · B 端 VPS 设计

> 状态：**B 端已上机（2026-11-07，TS-only 8899）/ A 端已实现（v0.3.1.76，默认关闭）** ｜ 初稿：2026-10-15 ｜ 关联：`.workbuddy/artifacts/自检守护-可行性与设计评估.md`（2026-09-12 总评估）、`docs/reference/TeslaMate接口与状态字段.md`
> 本文只覆盖 **B 端（僚机）** 及其与 A 端的接口契约；A 端本地自检四项（网络 / USB / 缩略图 / 微信）的探针细节见上篇总评估。

---

## 0. 本轮决策（2026-10-15）

| # | 决策 | 备注 |
|---|------|------|
| 1 | B 端 = 腾讯云轻量 **2C2G / 50GB SSD / 300GB 月流量 / 4Mbps**，装 1Panel | 资源对心跳服务绰绰有余 |
| 2 | 公网端点用 ~~裸 IP + Let's Encrypt~~ → **裸 IP + 自签证书 + A 侧 pinning**（✅ 2026-10-16 定稿） | 见 §9 / §9.1。裸 IP 免备案；自签+pinning 零续期、免疫 CA/B 政策漂移。服务端证书 SAN=IP，A 用 **IP 直连**并 pin 私有 CA（`verify=<ca.crt>`） |
| 3 | 通道策略 = **failover-only + 低频 dual-send 探针** | TS 优先、HTTPS 兜底；每 **150s** 补一发 HTTPS 探针（必须 < ALARM，见 §2 注） |
| 4 | 鉴权 = **v1 静态 token，稳定后迁 HMAC** | 迁移路径见 §8 |
| 5 | A 端 Tailscale key expiry 暂保持默认，稳定后再改长期 | ⚠️ 默认 ~180 天，见 §10 风险 R1 |

---

## 1. 目标与边界

B 端只做三件事：**收心跳 → 判死 → 发告警**。

明确不做：

- ❌ 不做远程控制 / 命令下发（A 处在不可信网络，反控面越大越危险）
- ❌ 不反向暴露 A（B 不主动连 A 的公网地址）
- ✅ 可选：一个只读状态页供自己查看

---

## 2. 拓扑与通道策略

- A 侧每 60s 心跳；**首选 Tailscale 私网口**，失败则走公网 HTTPS。
- 另每 **150s** 用 HTTPS 补发一发轻探针（dual-send），使 B 在正常态也能掌握 HTTPS 通道健康。
  > **只配 https 通道时**：主心跳本身就每 60s 走一次 https，探针不额外发（`probe_every_s` 只作为
  > 「TS + HTTPS 双通道」与「反向 ping」的节拍）。
  > ⚠️ **原设计写的是 10min，与 §4 的 `ALARM=5T=300s` 自相矛盾**（2026-11-07 复核发现）：
  > 探针 600s 一次意味着 `last_seen.https` 的年龄在 [0,600) 之间随机漂移，
  > TS 一断，`age_ts` 跨过 300s 时约有一半概率 `age_https` 也 ≥300 →
  > 被 `min(age_ts,age_https)>=ALARM` 判成「设备失联」而不是「Tailscale 通道故障」，
  > 随后又来一条「恢复」，再补一条「TS 故障」——一次抖动三条互相矛盾的消息。
  > **契约：探针周期必须 < ALARM**。实现侧已夹取到 `[60,150]`。
- **两个监听分开绑**：TS 监听只绑 B 的 Tailscale 接口；公网监听 443（1Panel OpenResty 反代）。TS 优先时 B 不需要为它开任何公网端口。

### 通道判别矩阵（B 侧）

| last_seen_ts | last_seen_https | B 的判读 | 动作 |
|---|---|---|---|
| 新鲜 | 任意 | 正常 | 静默 |
| 陈旧 | 新鲜 | Tailscale 通道故障（设备在线） | 告警 |
| 陈旧 | 陈旧 | 设备失联（断电 / 崩溃 / 断网，无法区分） | 告警 |
| 新鲜 | 陈旧 | HTTPS 未启用（正常态） | 静默 |

> "设备没网"与"设备死了"在 B 眼里不可区分——这是遥测固有极限。告警文案如实写"设备失联"，不硬猜。

---

## 3. 接口契约

### 3.1 心跳：`POST /guard/heartbeat`

- Headers：`Authorization: Bearer <token>`、`X-Guard-Channel: ts|https`、`X-Guard-Seq: <int>`
- Body（复用 09-12 方案的快照结构）：

```json
{
  "seq": 1024,
  "ts": 1760533000,
  "mono": 1234567,
  "version": "0.3.1.6x",
  "net": {"wifi_ssid": "C12345", "rssi": -52, "tailscale": "Running", "exit_node": null,
          "teslamate_ok": true, "internet_ok": true},
  "gadget": {"udc_bound": true, "udc_connected": true, "state": "configured", "mode": "present"},
  "thumb": {"last_scan_age_s": 42, "queue": 3, "blocked": 0, "service": "active"},
  "wechat": {"last_ok_age_s": 3600, "consecutive_fail": 0},
  "vehicle": {"trigger": "gear=PARK 3.2min", "locked": true, "user_present": false,
              "sentry_mode": true, "park_time": "00:12:31", "source": "local+states"},
  "selfcheck": {"result": "pass", "items": {}}
}
```

- `gadget.mode` 为本次新增（v64 后必须带，否则 B 无法区分 Edit 模式下的正常解绑）。

### 3.2 反向心跳（A 拉）：`GET /guard/ping` → `{"ok":true,"ts":...}`

A 侧按 **150s**（与 dual-send 探针同轮）拉一次；连续失败 → A 知道 B 挂了。
实现见 `guard_client.GuardSender._ping()`：HTTPS 通道同样强制 `https://` + CA pinning，
不允许这条探活路径绕过 §5 的传输约束。

### 3.3 预期离线握手：`POST /guard/heartbeat`（`event=going_offline`）

```json
{"event": "going_offline", "offline_s": 3600, "expected_until": 1760536000, "reason": "driving"}
```

> **`offline_s` 才是权威字段**（相对量，跨机安全）：B 用 `now + offline_s` 计算窗口。
> `expected_until` 是 A 的绝对墙钟，仅作旧版兼容兜底，B 取到时必须夹到 `[now+60, now+6h]`——
> A7Z 冷启动曾 87 分钟才 NTP 同步，坏钟会让窗口变成负数（当场误报）或几年（永不告警）。
> 分档：`driving` 用 `offline_default_s`（默认 3600）、`unlocked` 900、`user_present` 600。

B 在窗口内静默，超窗才告警。

### 3.4 状态页（可选）：`GET /guard/status`（需 token）→ 最近快照 + 通道健康

---

## 4. B 端判别逻辑（伪代码）

```
WARN = 3*T; ALARM = 5*T; T = 60s

on heartbeat(req, channel):
    touch(status.last_seen[channel])
    status.last_seq, last_body = ...
    if body.event == "going_offline":
        status.offline_until = body.expected_until
    persist(status)

tick (every 30s):
    if now < status.offline_until:              # 预期离线窗口 → 静默
        return "silent"
    why = classify_vehicle(status.last_body)    # driving 最先判
    if why in (sentry_off, unlocked, user_present):   # 合法断电 → 长期静默
        return "silent"
    if why == driving and evidence_fresh():     # 车在动 → 只静默 offline_default_s
        return "silent"
    age_ts    = now - status.last_seen.ts
    age_https = now - status.last_seen.https
    if age_ts < WARN:                       return "ok"
    if age_ts >= ALARM and age_https < WARN:   alert("Tailscale 通道故障")
    elif min(age_ts, age_https) >= ALARM:      alert("设备失联")
```

> ⚠️ **实现修正（2026-10-16，`guardian-b/judge.py`）**：TS 故障判据由 `age_https < WARN`
> 改为 `age_https < ALARM`，与 §2 矩阵口径一致——否则当 https 年龄落在 `[WARN, ALARM)`
> 时两个分支都不命中而**静默**，但此时设备显然经 https 可达，应判 **TS 故障**。
> 另：去重判据须加 `last_sent > 0` 前提（首次告警不得被判成"重复"）——此缺陷由单测捕获。
>
> ⚠️ **实现修正（2026-11-07，复核）**：
> ① **只统计曾收到过心跳的通道**。单通道部署（只配 https）时 `last_seen.ts` 恒 0 →
> `age_ts` 是个天文数字 → 旧判据永远报「Tailscale 通道故障」。现在 `ts_seen/https_seen` 各自判定，
> 主通道取「有 TS 看 TS，没有才看 https」。
> ② **预期离线窗口改用相对量**：A 发 `offline_s`，B 用 `now + offline_s`。
> 旧 `expected_until`（A 的绝对墙钟）只在缺失时兜底，且必须夹到 `[now+60, now+6h]`——
> A7Z 冷启动曾 87 分钟才 NTP 同步，坏钟会让窗口变成负数（当场误报）或几年（永不告警）。

`should_be_online(last_body)`：依据最后一次心跳里的 `vehicle`（`sentry_mode && locked && !user_present`），或向 TeslaMate 反查泊车态。**车在开 = 不该在线 = 静默**——这是防误报的核心（M90 无网恢复路径不冷却的对应面：这里是"预期离线不告警"）。

> ⚠️ **实现修正（2026-11-09，M135）**：上面的「车在开 = 静默」**没有保质期**，
> 而 `last_body` 只是「A 死前最后一句话」。11-09 实证：A 在 43km/h 上断掉，
> 出车窗口（1h）到期后这一帧仍说「在开 + 人在车上」→ 每拍都判静默，
> **1h38m 零告警，`alerts.device_offline.last_sent` 始终 0.0**（一次都没尝试发）。
> 现在的口径：
> ① `classify_vehicle` 让 `state=driving`（或 `speed>3`）**最先命中**——否则
>    `user_present` 会先把「在开车」遮成「人在车边」；
> ② 静默分**两类**：`driving` 只静默 `offline_default_s`（默认 1h，与出车窗口同源），
>    过期即判死；`sentry_off` / `unlocked` / `user_present` 保持**长期静默**
>    （车锁了 → 车睡了 → 车机 USB 断电是正常现象，加 TTL 会每晚误报）；
> ③ 重复告警改**递增退避** `GUARD_ALERT_BACKOFF`（默认 `1800,7200,21600,86400`），
>    恢复归零；`GUARD_DEDUP_S` 退化为「第一档间隔」的兜底；
> ④ 失联文案带「最后车态」，行驶中失联不再建议「确认车还停着」。

告警去重 / 限频：同一 `(fault_type, channel)` 30min 内只推一次（M86 标记去重 + M89 限频率）；恢复时推一条"已恢复"。

---

## 4.1 车态板：B 端怎么在「A 没网」时仍然知道车锁了（v1.1.4 / M137）

**问题**：A7Z 离家后没网（车载 MIFI / 热点掉线），B 端拿不到任何车态；
而「锁车后必推一条自检」的前提是**知道车锁了**。

**三条路都试过，只有第三条可行**：

| 方案 | 结论 |
|---|---|
| 读 TeslaMate 的 Postgres | ✗ `5432` 只在容器网络里（没 publish），且 `positions` 表**不存** `locked`/`sentry_mode`/`shift_state`，只有 `speed` |
| 调 TeslaMate 的 `/api/*` | ✗ 全部要 token，token 缓存在 A7Z 上；让 B 也 `/login` 会与 TeslaMate 抢 token（M115：40 分钟 77 次登录） |
| **抓首页 HTML（LiveView SSR）** | ✓ **免鉴权、只读、零副作用**，`http://127.0.0.1:4000/` 直接带 locked / sentry / status |

实测（9.4 KB）：

```html
<span data-tooltip="Sentry Mode"><span class="mdi mdi-shield-check"></span></span>
<span data-tooltip="Locked"><span class="mdi mdi-lock"></span></span>
<td>Status</td><td> online for 13 h, 26 min </td>
```

解析规则（`guardian-b/board.py`）：

- **只认图标 class**：`mdi-lock` = 已锁，`mdi-lock-open-variant` = 未锁；
  `mdi-shield-check` / `mdi-shield-off` = 哨兵开/关。文案会随语言变，class 不会。
- **解析不出就是 `None`，绝不猜**——猜错直接变成一条假告警。
- 抓取/HTTP/解析任一失败 → `err` 字段，**不触发任何告警**（fail-open）。

代发状态机（`judge.on_board` / `_maybe_relay_lock`）：

```
每 60s 采样 → 未锁→已锁 的**边沿** → arm（记 at，持久化）
              → 开锁 → clear（下一把锁重新计）
宽限期 GUARD_LOCK_GRACE_S（默认 300s）后判定：
  · 车态板本身 >900s 过期      → done(board_stale)，不代发
  · 设备心跳仍在 WARN 内        → done(device_alive)，A 会自己推
  · 否则                        → 代发一条「停车自检·僚机代发」+ 把 device_offline 槽一并记账
```

**为什么闩锁要落盘**：重启后若不知道「上一拍是未锁」，第一拍就会被当成刚锁车 →
重复代发一条自检。同一把锁只代发一次，开锁才复位。

代发文案示例：

```
【停车自检·僚机代发】车辆已锁车 · 设备失联
· 车态（TeslaMate）：已锁 · 哨兵开 · 车辆状态 online
· A7Z：最后心跳 1.6 小时前（0.3.1.83）

本条由 B 端代发：设备此刻没有心跳，它自己的停车自检（USB/网络/存储）生成不了。
```

> ⚠️ 已知边界：这个页面是 TeslaMate 的模板输出，**换主题/换 i18n 可能让解析失效**。
> 失效时行为是「读不到车态 → 不代发」，即退化成 v1.1.3（只报设备失联），不会误报。
> `/guard/status` 的 `state.board` 里能看到 `{locked, sentry, state, err}` 与采样时刻。
> 另：`4000` 目前 publish 在 `0.0.0.0`（公网可达且无鉴权），B 自己只用 `127.0.0.1:4000`，
> 建议把端口绑定收窄到回环或加防火墙。

---

## 5. A 端配合逻辑

- **通道 failover 顺序**：先 TS，超时 `ts_timeout`（建议 8s，需实测 TS/DERP 重连时间）失败后走 HTTPS；两者都失败 → 记录本地，下轮重试。
- **低频 dual-send 探针**：每 **150s** 额外走一次 HTTPS（独立计数，不占用主心跳 seq）。
  周期必须 < B 的 `ALARM`（5T=300s），否则 TS 断开时会被误判成「设备失联」——见 §2 注与 §14.6。
- **上报自身通道状态**：`body.net.tailscale` / `exit_node` 必须带，B 才能区分"TS 坏"还是"A 以为 TS 坏"。
- **going_offline 触发**：检测 `gear PARK→DRIVE`（SEI）或 `is_user_present` 转 true 或 UDC 即将解绑时发。
  > **实现偏差（v0.3.1.76 已确认）**：SEI 逐帧解析成本高、且要读正在写入的 mp4，
  > 首版改为从 TeslaMate `/states` 取 `state/locked/is_user_present` 三态判跃迁
  > （`state→driving` / `locked` True→False / `user_present` False→True），
  > 三态字段按字段合并进 prev——缺失字段不得抹掉已知状态，否则真实跃迁会被空窗吃掉。
- **落盘补报**：A 侧写 `guard_status.json`；网络可用时推 B，不可用时留待补报（与 `sentry_notify_queue` 同构，M90）。
- **时钟**：间隔一律用各自单调时钟；body 同时带 `ts`（墙钟，仅展示）与 `mono`（占位，勿跨机比大小）。

---

## 6. 部署方案（腾讯云轻量 + 1Panel）

> **实际部署（2026-11-07 起）与下文原计划不同，以本块为准**：
> 机器是 **`vm-0-17-ubuntu-sh`（Tailscale `100.111.252.121`）**，
> **只绑 TS 接口的 8899 端口**（`GUARD_BIND_HOST=100.111.252.121`），
> **不开公网端口、不走 OpenResty、不用证书**——流量在 WireGuard 内。v1 阶段的 HTTPS 兜底推迟到 C 阶段。
> （`8800` 已被该机一个反向代理占用，所以端口改 8899。）

- **规格**：2C2G / 50GB / 4Mbps —— 心跳服务 CPU / 内存 / 流量都可忽略（300GB/月远超需求）。
- **运行形态**：推荐 **Docker 容器**（1Panel 原生支持），或 systemd 直跑；镜像内单进程。
- **反向代理 + 证书**：用 1Panel 的 OpenResty 反代到本地 B 服务；证书走 acme.sh（1Panel 内置）。
- **Tailscale**：VPS 装 TS 并入同一 tailnet；B 的 TS 监听绑 TS 接口地址。
- **安全组 / 防火墙**：放行 TCP 80（ACME http-01）、TCP 443（HTTPS + 可选 tls-alpn-01）；TS 仅需 UDP 41641 出站，入站不需要。
- **目录**：`/opt/guard-b/`（compose + 配置）、状态 `/opt/guard-b/data/state.json`。
- **配置来源**：token / grace 阈值 / 默认离线窗口走环境变量或 `.env`（不进镜像）。

---

## 7. 状态与持久化

- 单文件 JSON 或 SQLite：`last_seen{ts,https}`、`last_seq`、`last_body`、`offline_until`、`alert_dedup{}`。
- 告警通道：**B 上跑一份 `weixin_notifier`**（复用 A 侧模块 + VPS 自己的 webhook key）。

---

## 8. 安全

| 项 | v1 | v2 |
|---|---|---|
| 鉴权 | 静态 Bearer token（≥32 字节随机） | HMAC(token, ts, body)，防重放 |
| 传输 | HTTPS（LE 证书）+ TS（WireGuard） | 同 |
| 反暴露 | B 不回调 A 公网地址 | 同 |
| 限流 | OpenResty `limit_req` + fail2ban | 同 |
| 秘密 | token 在 A 的 `config/sentry.json`；B 侧环境变量 | 同 |

HMAC 迁移：body 预留 `nav` 语义 + header 带 `X-Guard-Alg`；B 同时接受两种，观察期后关闭 v1。

---

## 9. 证书方案专题（裸 IP + LE）—— ⚠️ 关键

本轮已核实（2026-10-15，来源：letsencrypt.org 2026-01-15 公告 + acme.sh 官方 wiki / 实战帖）：

- LE **IP 证书 2026-01-15 GA**，IPv4 / IPv6 均支持；但 **IP 证书必须是 shortlived profile，有效期 160h（≈6.67 天）**。
- **仅 http-01 / tls-alpn-01 验证**，不支持 dns-01、不支持通配符（RFC 8738）。
- **acme.sh 支持**：

  ```bash
  acme.sh --issue -d <IP> -w <webroot> \
      --server letsencrypt \
      --certificate-profile shortlived \
      --days 3
  ```

- 续期频率：`--days 3`（约 3 天续一次，留 3.66 天缓冲）；**速率限制 5 张 / 168h per 相同标识集**，3 天周期约 2.3 次/周，安全。
- **Caddy 需禁用 `default_sni`** 才能选中 IP 证书（若用 Caddy）；用 1Panel OpenResty 则手动指向证书文件。

**⚠️ 落地风险**：**1Panel 的证书 UI 很可能不暴露 `--certificate-profile shortlived`**（该参数是 acme.sh 较新特性）。预期两条路：

1. 用 1Panel 的"手动签发 / 命令行"或直接在 VPS 上跑 acme.sh 签发 → 把证书路径配给 OpenResty；
2. 若 1Panel 无法承载，退化为 **自签证书 + A 侧证书 pinning**（难看但可行），或**买最便宜域名走 90 天常规证书**（运维成本最低）。

> **域名方案的备案约束**：备案与否**取决于服务器地域，与证书无关**。
> - B 端若为腾讯云**中国大陆地域**：域名走 Web 服务**必须完成 ICP 备案**，否则被接入商监测系统阻断访问（**换非 80/443 端口也不免除**）；纯 **IP 访问不触发**备案义务。
>   - ⚠️ **且域名后缀必须在工信部「顶级域名批复名单」内**，否则接入商备案系统直接提示"该域名后缀不支持备案"、根本无法提交。`.me` 的批复状态各方资料口径不一（部分列表含、部分明确列为不可备），**以工信部「域名行业管理信息公示」或腾讯云备案系统的实测提示为准**。
> - B 端若为**中国香港 / 海外地域**：**无需 ICP 备案**（仅需公安联网备案）。
> - **证书本身不需要备案**——LE 签发只看你对域名/IP 的控制权。
>
> → 这也是 §0 决策 2 选 **裸 IP + LE** 的隐性收益：**完全绕开备案**。

### 9.1 除「LE 短证书」外的替代方案（2026-10-15 补充核实）

> **选型逻辑**：CA/B 论坛 **SC-081v3** 已将公有证书最长有效期做成**阶梯式缩短**（2026-03-15 → 199/200 天、**2027-03-15 → 100 天**、**2029-03-15 → 47 天**）。→ **所有依赖公有 CA 的方案都在"续期跑步机"上**；只有私签（pinning）能免疫该趋势。故"当前多少天"不如"是否依赖公有 CA"重要。

| 方案 | 有效期 | 需域名 | 续期负担 | 外部依赖 | 大陆备案 |
|---|---|---|---|---|---|
| LE IP 证书（现状） | **160h** | 否 | 每 ~3 天 | LE + acme.sh | 无 |
| **ZeroSSL IP 证书** | 90 天 | 否 | 每 ~60 天 | ZeroSSL 账号 | 无 |
| **sslip.io / nip.io + LE 常规证书** | 90 天 | 否（借第三方泛解析 DNS） | 每 ~60 天 | sslip.io 可用性 | ⚠️ 灰色 |
| 商用 IP 证书（DigiCert/Sectigo…） | 199 天 | 否 | 每 ~半年 | **付费 + OV 验证 IP 所有权** | 无 |
| **自签 + A 侧 pinning** | 任意（如 10 年） | 否 | **几乎无** | **无** | 无 |
| 域名 + LE | 90 天 | 是 | 每 ~60 天 | 域名注册 +（大陆）备案 | 大陆需备案 |
| 明文 HTTP + HMAC 签名 | — | 否 | 无 | 无 | 无（不推荐） |

**逐项说明**：

- **自签 + pinning（首选，架构上最优）**：B 与 A 均为自有节点、A 是唯一客户端，这正是**私签 + 证书固定**的标准适用场景（设备集群遥测通行做法），**不是"难看兜底"**。B 上生成自签证书（或轻量私有 CA）设 10 年有效期，把 CA/指纹拷到 A 的 `config/sentry.json`，A 用 `requests(verify=<ca.pem>)` 或固定指纹连接。**收益**：零续期、零 acme.sh/1Panel 依赖、零 CA 策略漂移、零速率限制、**免疫 CA/B 的 47 天趋势**。**唯一代价**：人用浏览器打开会告警（可另开低风险状态页用公有证书解决）。
- **ZeroSSL IP 证书（要"真证书"时的首选）**：少数**免费且支持纯 IP** 的公有 CA，免费版 90 天、支持 ACME 自动续期，1Panel 有 ZeroSSL 集成。比 LE 好 3.75 倍。⚠️ 2026 年 ZeroSSL 是否仍免费签 IP 证书**需实测确认**。
- **sslip.io / nip.io + LE 常规证书**：`<ip-带横线>.sslip.io` 泛解析自动指向该 IP，于是可对**一个"域名"**申请常规 90 天 LE 证书（LE 官方即在 nip.io/sslip.io 页面推荐此法规避 IP 证书速率限制）。**代价**：给兜底通道引入**第三方 DNS 依赖**（对"网络故障"看门狗略讽刺）；大陆节点借第三方域名走 Web 服务存在被接入商监测阻断的灰色风险。
- **商用 IP 证书（基本不可行）**：DigiCert/Sectigo/GlobalSign 均可签 IP 证书，但属 **OV 级**，要求**证明该 IP 段在 RIR 注册在你名下**——**云主机租用的 IP 不属于你**，验证过不了。**排除**。
- **明文 HTTP + HMAC 签名（最后手段）**：仅当 TLS 实在走不通时用。心跳含 token，明文会泄露并可重放，**不推荐**；真要用须 body 全量 HMAC + 单调时间戳 + 服务端去重。

→ **结论**：决策 2 的"IP+LE"方向对，但 **§0 决策 2 的最终落点改为「自签 + pinning」为默认**；若坚持公有证书，选 **ZeroSSL IP 证书（90 天）** 而非 LE 的 160h 短证书。**前置验证优先级**：① 若走公有证书路线 → 先实测 ZeroSSL 能否对裸 IP 出 90 天证书；② 若采纳自签+pinning → **无需任何外部证书验证**，可直接进入骨架开发。

---

## 10. 风险与缓解

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | A 的 TS key 默认 ~180 天过期，离线错过轮换 → 掉出 tailnet | TS 通道永久失效，只剩 HTTPS | 稳定后改长期；B 侧监控 `net.tailscale` 频繁 fallback 即告警 |
| R2 | 公有 CA 证书续期失败/政策漂移（LE IP 证书 160h、CA/B 2027→100 天/2029→47 天） | 兜底通道失效 | **默认走自签+pinning（零续期）**；若用公有证书选 ZeroSSL 90 天 + 到期监控 |
| R3 | 1Panel 可能不支持 shortlived profile；证书自动续期链打断 | 证书方案受阻 | 见 §9.1：自签+pinning 不依赖 1Panel/续期链，为首选 |
| R4 | exit node 挂 → A 出网全断，HTTPS 兜底也废 | 双通道同时失效 | A 侧 exit node 失联自动摘除（M72 可复用） |
| R5 | B 单点 | B 挂了全静默 | 外部 dead-man（healthchecks.io / Uptime-Kuma）1h 打点 |
| R6 | 出车（A 正常断电）误报 | 骚扰用户 | going_offline 握手 + `should_be_online` 抑制 |
| R7 | 静态 token 泄露 | 可伪造心跳 | v1 限流 + 监控异常；尽快迁 HMAC |
| R8 | 心跳只走 TS 无法区分 TS 挂 / 设备挂 | 误判 | per-channel last_seen（§2 矩阵） |
| R9 | 时区 / 时钟跨机比大小 | 误判超时 | 各自单调时钟（M51） |

---

## 11. 分阶段落地

| 阶段 | 内容 | 前置 |
|---|---|---|
| A | A 端本地自检四项 + `guard_status.json`（只检测不修复） | ✅ **已实现** `guard_client.py`（2026-11-07，v0.3.1.76，默认关闭，待真机验证）；vehicle 段复用 TeslaMate `/states` |
| B | B 端骨架 + TS 通道 + 静态 token + 判死 + 微信告警 | ✅ **已上机**（2026-11-07 23:35）：`vm-0-17-ubuntu-sh` / Tailscale `100.111.252.121:8899`，**只绑 TS 接口**、不占公网端口、无需证书；`judge` 单测 21/21 |
| C | 公网 HTTPS 兜底 + IP 证书 + dual-send 探针 + 外部 dead-man | ✅ dual-send 探针（150s）与反向 `/guard/ping` 已随 `guard_client.py` 实现；⏳ 公网 HTTPS 反代待 B 端上机后视情况启用 |
| D | HMAC + 白名单自愈（仅在授权窗口） | B / C 稳定 |

---

## 12. 待办 / 遗留

- [x] ~~验证 acme.sh 对裸 IP 出 shortlived 证书~~ → **已改走自签 + pinning，本项作废**（2026-10-16 决策 2 定稿）
- [x] ~~**A 端实现心跳发送 + 证书 pinning**~~ → **已完成**（2026-11-07，`guard_client.py`，见 §14）
- [ ] **B 端上机部署**：拷 `guardian-b/` 到 VPS `/opt/guard-b/` → 填 `guard-b.env` → 跑 `tls/gen_selfsigned.sh` → 接 OpenResty → 启 systemd（详见 `guardian-b/README.md`）
- [ ] 真机开通：设备上写 `config/guard.json`（**该文件不在升级包白名单**，只能手工建）→ 重启 `teslausb-web` → 看 `data/guard_status.json` 与 B 端 `last_seen`
- [ ] A 的 TS key expiry（暂缓，B 侧监控兜底）
- [ ] exit node 失联自动摘除
- [x] ~~A 侧落 `/guard/heartbeat` 时注意 `app.py` "开机通知在 `app.run()` 前同步发"的已知缺陷（不得在关键路径同步阻塞）~~ → 用 daemon 线程 + 20s 首轮延迟规避（M95）
- [x] ~~若引入新服务 / 文件，进 `deploy_manager.FILES` 白名单 + 升级包（M41）~~ → 只加 `guard_client.py`（120 文件），**不新增 systemd 单元**（M120 风险）

---

## 13. 实现落点（`guardian-b/`，2026-10-16）

B 端骨架已产出，**不随 A7Z 升级包分发**（除非显式加入 `deploy_manager.FILES`）。

| 文件 | 职责 |
|---|---|
| `guardian-b/app.py` | Flask 单进程：`/guard/heartbeat`、`/guard/ping`、`/guard/status`、`/healthz`；Bearer 鉴权；judge/dead-man 守护线程 |
| `guardian-b/config.py` | 全部配置来自环境变量（`GUARD_*`），含自检 `validate()` |
| `guardian-b/state.py` | 状态原子写（tmp+`os.replace`）；`last_seen` 存 **B 侧接收时刻** |
| `guardian-b/judge.py` | §2 矩阵 + §4 逻辑；含上述一处修正 |
| `guardian-b/alerts.py` | 自包含微信推送（**取代** §7 的"复用 A 侧 notifier"，见下） |
| `guardian-b/deploy/` | `guard-b.service`、`guard-b.env.example`、`openresty-guard-b.conf` |
| `guardian-b/tls/gen_selfsigned.sh` | 生成私有 CA + 服务端证书（SAN=IP，10 年），输出 A 侧 pinning 物料 |
| `guardian-b/tests/test_judge.py` | 判死矩阵单测 **21/21 通过**（2026-11-07 增补相对窗口 / 坏钟夹取 / 单通道用例） |

**与本文两处有意偏离（已确认）**：

1. **告警器不复用 A 侧 `weixin_notifier`**（§7 原话）。A 侧模块携带 exit-node 探测（`subprocess tailscale`）、哨兵事件模板、写死 `/opt/teslausb-web/data` 的推送健康文件等 A 专属逻辑，在 B 上无意义或会报错。改为自包含 `alerts.py`，`send_text`/`send_markdown` 接口保持同形，可无缝换回。
2. **§4 伪代码的 TS 判据修正**（见 §4 注）。

---

## 14. A 端实现落点（`guard_client.py`，2026-11-07 v0.3.1.76）

单文件、约 800 行，**不新增 systemd 单元**。由 `app.py` 在 `__main__` 块里以 daemon 线程拉起，
首轮延后 20s（避开开机服务拉起风暴），启动失败只告警、不影响 Web（M95）。

### 14.1 快照字段 → 数据来源（逐项实测确认，不按文档臆测）

| 字段 | 来源 | 开销/说明 |
|---|---|---|
| `net.wifi_ssid` / `net.rssi` | `wifi_service.get_current_wifi()`（nmcli，失败回落 iw） | ~5ms；返回值键是 `signal`，映射成 `rssi` |
| `net.tailscale` / `net.exit_node` | `tailscale status --json`（本模块新封装，30s 缓存） | ~10ms；`BackendState` + `ExitNodeStatus.ExitNodeID` |
| `net.internet_ok` | TCP → `223.5.5.5:53`（1.5s） | 单点探测；三重投票留给 `system_monitor` |
| `net.teslamate_ok` | TCP → `config/sentry.json` 的 `teslamate_url`（60s 缓存） | URL 缺失即 `None`，不猜 |
| `gadget.*` | **`data/usb_guard_status.json`**（usb_guard 原子落盘）+ `/tmp/teslausb_mode` | 零 sysfs 写；`udc_connected = state=='configured'` |
| `thumb.service` | `systemctl is-active teslausb-bgpreview` | 3s 超时 |
| `thumb.queue` / `thumb.blocked` | `data/preview_queue.json`（pending+paused）/ `preview_blocked.json` | 直接读文件，不 import 后台进程 |
| `thumb.last_scan_age_s` | **v1 恒为 null** | bgpreview 未落盘 last_scan；列为下一版增强，不填假值 |
| `wechat.last_ok_age_s` / `consecutive_fail` | `data/push_health.json`（`bots[*].last_success` 是 **ISO 本地时间字符串**） | 年龄为负 → 判不可信返回 None |
| `vehicle.locked` / `sentry_mode` / `user_present` / `state` / `park_time` | `TeslaMateCustomAPI.get_states()`，**300s 一次**，只在子线程拉 | 三态走 `_tri()` 归一化（M62：`"nil"` 字符串） |
| `selfcheck` | 上述四项汇总 pass/warn/fail | 判据见 §14.3 |

### 14.2 发送与容错

- 通道顺序：`endpoints.ts` → `endpoints.https`；HTTPS **必须**配 `ca_path`，否则直接判失败
  （`https_without_ca`）——自签 + pinning 是既定方案，绝不明文信任系统根。
- 双通道都失败 → 快照入 `data/guard_queue.json`；**仅在下一轮发送成功后**才补报（从队头补，每轮最多 3 条），
  避免通道仍不可用时叠加重试把节拍拖死。
- `going_offline` 触发：`state` 转入 `driving` / `locked` True→False / `user_present` False→True，
  且只在**跃迁**时发一次，窗口 `offline_default_s`（默认 3600s）。
- 计时一律 `time.monotonic()`；body 里的 `ts` 与 `mono` 只做展示（M51）。

### 14.3 自检判据（只检测，不修复）

| 项 | pass | warn | fail |
|---|---|---|---|
| net | 已连且能出网 | 已连但出网失败/未知 | WiFi 未连 |
| gadget | `configured` 或 **Edit 模式**（M103） | 线离位（车睡了，不是故障） | 线在位但未 configured |
| thumb | 服务 active 且积压 < 50 | 积压 ≥ 50 | 服务非 active |
| wechat | 24h 内有过成功推送 | 无记录或 > 24h | 连续失败 ≥ 3 次 |

### 14.4 配置

`config/guard.json`（模板 `config/guard.json.example`）。**该文件不在升级包白名单**——
升级不得覆盖用户 token 与端点。`enabled=false` 时 `start_background()` 直接返回，
不建线程、不发请求，**行为与上一版等价**（M116 判据）。

### 14.5 只读接口

`GET /api/guard/status` → `{success, available, enabled, channels, snapshot}`。
只读、不触发网络、走现有登录守卫。

`available=true` 必须同时满足「文件可解析」与「快照新鲜（≤3×interval）」：
心跳线程死了、或文件是历史遗留的，状态页不能拿三天前的快照当现状。

### 14.6 独立复核（2026-11-07）修掉的问题

复核提出 4 个 P0 + 9 个 P1，逐条核对后**全部成立**并已修：

| 级别 | 问题 | 修法 |
|---|---|---|
| P0 | `"enabled": "false"` 被 `bool()` 判成开启（M116 反例） | `_as_bool()`，非法值回落 False |
| P0 | `endpoints.https` 写成 `http://` 时 token 明文过公网 | `channel=https` 强制 `https://`，否则 `https_scheme_required` |
| P0 | `expected_until` 跨机墙钟比较（M51） | 改发相对量 `offline_s`，B 侧优先相对量并夹取旧字段 |
| P0 | 探针 600s > B 的 ALARM 300s → TS 故障误报「设备失联」 | 探针默认 150s 并夹取到 `[60,150]`，设计 §2 同步改 |
| P1 | `queue_max=0` 时 `items[-0:]` = 全表，队列永不淘汰 | 夹取 `[1,1000]`，`limit<=0` 直接不写 |
| P1 | 其余数值参数无上下限（timeout=0 永久入队、states=0 打爆配额） | 逐项 clamp + 越界 warning |
| P1 | 探针最坏耗时叠加 > 心跳周期、每 60s fork 4 个进程 | wifi 60s / systemctl 60s 缓存（tailscale 30s、teslamate 60s 原有） |
| P1 | 探针失效（None）被判 pass，自检假绿 | 四项统一：`False→fail`、`None→warn` |
| P1 | `usb_guard_status.json` 新鲜度算了不用（守护停摆仍 pass） | `age_s > 300` → gadget warn |
| P1 | 状态接口不区分「活着」与「停摆」 | `available` 叠加新鲜度 + `stale`/`age_s` |
| P1 | `last_sent_at` 失败也刷新 | 拆 `last_ok_at` / `last_attempt_at` + `fail_streak` |
| P1 | 发送失败只有 debug 日志（配置错到永远好不了也没人知道） | 错误态变化打 warning，恢复打 info |
| P1 | `going_offline` fire-and-forget，失败即永久丢失 | 保留 pending，最多重试 3 次 |
| P1 | vehicle 缓存无 stale 判定（车开走后仍按旧快照判「该在线」） | 超过 3×states_interval → 三态置 None + `source=stale` |
| P2 | 补报在新鲜心跳之后 → B 的 `last_body` 停在最旧快照 | 先补报（且仅上轮成功时）再发新鲜心跳 |
| P2 | 三种跃迁共用 1h 静默窗口（每天解锁十几次 → 漏报） | driving 用 `offline_default_s`、unlocked 900s、user_present 600s |
| P2 | §3.2 的反向 `GET /guard/ping` 未实现 | 随探针轮询执行，连续 3 次失败告警 |
| P2 | `ExitNodeStatus.ExitNodeID` 字段名错（实际是 `ID`） | 改取 `ID`，兼容旧键，并上报 `exit_online` |
| P2 | `/proc/net/wireless` 有 wlan0 即判「已连接」 | 改为未知（None → warn） |
| P2 | 读 `push_health.json` 会撞 `PushHealthTracker` 的非原子写 | 读失败重试一次（写侧待下一版改原子） |

### 14.7 已知与 §3.1 的偏差（B 当前不消费这些字段，故不阻塞）

- `thumb.service` 发 bool，文档 §3.1 写的是字符串 `"active"`；`thumb.last_scan_age_s` 恒 `null`。
- `vehicle.trigger` 未实现（需要 SEI 逐帧解析，成本与收益不匹配）；`vehicle` 多出 `age_s`/`source`。
- 多出 `wechat.bots`、`gadget.cable`、`gadget.age_s`，均为诊断用。

### 14.8 B 端 v1.0.2（2026-11-08）：配置下发 + 告警三态

| 端点 | 语义 |
|---|---|
| `GET /guard/config` | 回 `alert.{configured,key_tail,dry_run}` + `warn_s/alarm_s/dedup_s/offline_default_s`；**只回末 4 位，不回明文 key** |
| `POST /guard/config` | 只接受 `wecom_key` / `alert_dry_run`，其余字段一律 400（避免"两边都能改、谁生效说不清"）；原子写 `data/runtime.json`（0600）并内存热生效 |

- **优先级 `runtime.json` > env > 默认**：UI 保存的 key 重启后不会回退到 env 旧值
  （`config.py._apply_runtime()`）。`guard-b.env.example` 已加 `GUARD_RUNTIME_PATH`。
- **告警三态**：`alerts.Alerter._post()` 由 `bool` 改 `"sent"|"dryrun"|"failed"`；
  `judge` 事件同时给 `mode` 与 `delivered`（`mode in (sent,dryrun)`）。
  修掉 2026-11-07 现场「`delivered: True` 却无法判断群里收到没有」的缺陷。
- `bool()` 的坑：`runtime.json` 里写 `"false"` 会被 `bool("false")` 判成 True →
  一律走 `config.as_bool()`（白名单 `1/true/yes/on`，其余 False）。
- 公网反代配置（C 阶段用）已给 `/guard/config` 加 `return 403`：能改写告警 key 的接口
  **只允许走 TS 隧道**。

### 14.9 §14.7 的两条偏差已在 v0.3.1.77 修掉

- `thumb.last_scan_age_s` 不再恒 `null`：`bg_preview_generator.scan()` 落盘 `data/preview_scan.json`，
  A 侧读它算年龄（>600s 判 warn）。
- `alerts` 的 bool 语义已换成三态（见 §14.8）。
