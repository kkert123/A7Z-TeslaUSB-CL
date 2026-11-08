# 守护配置 UI + 仪表盘僚机状态 · 实现建议

> 日期：2026-11-08 ｜ 状态：**已实现（v0.3.1.77）** ｜ 关联：`docs/explanation/自检守护-B端VPS设计.md`、`guard_client.py`、`guardian-b/`
>
> ⚠️ 本文是**建议稿**，落地时被需求改过（§7/§8 为准）。§1~§6 保留原始推演过程，
> 与最终实现的差异逐条列在 **§9 校订表**，别照着 §1/§2 的旧结论去改代码。

---

## 0. 三个需求与现状差距

| # | 需求 | 现状 | 差距 |
|---|---|---|---|
| 1 | 「健康与通知」新增「A7Z 守护」机器人配置 | 机器人配置有 2 个（哨兵事件 / 系统通知），KEY 存 `config/weixin_config.json`；**告警实际由 B 端发**，key 在 B 的 `guard-b.env` | UI 没有第三项；A 与 B 的 key 没有配置通路 |
| 2 | 状态守护配置项 | `config/guard.json`（6 个参数 + endpoints + token），**只能 ssh 手写** | 无 UI、无校验、无「测试连接」、无热重载 |
| 3 | 仪表盘网络卡显示僚机通信状态 | 网络卡只有 SSID/信号/TS IP/LAN IP/流量 | 无守护状态；`/api/guard/status` 已有数据但页面不消费 |

**关键认知（决定需求 1 的实现方式）**：A7Z 守护的告警是 **B 端（云端僚机）发的**——
设备失联时设备自己发不出消息。所以「A7Z 守护机器人」这个 key，最终必须存在于 B 端，
A 端设置页只是它的**配置入口**。这一点会直接改变方案。

---

## 1. 需求 1：A7Z 守护机器人配置

### 1.1 三个方案

| 方案 | 做法 | 优点 | 致命问题 |
|---|---|---|---|
| **A 纯引导** | UI 只显示「未配置」，点开是操作指引（去 B 上改 env） | 零风险、零新接口 | 用户还是得 ssh，等于没做 |
| **B A 存 key，随心跳下发** | A 保存到 `guard.json`，每次心跳带 `alert_key` 给 B | 实现最简 | **明文 key 每 60s 过一次网络**，并会落进 B 的 `state.json`/日志 → 否决 |
| **C 配置下发（推荐）** | A 的 UI → `POST /api/guard/bot` → A 用 Bearer 调 B 的 `POST /guard/config` → B 原子写入 `data/runtime.json`（0600），告警时优先取 runtime | key 只传一次、只走 TS 隧道、B 不落日志、UI 与其它两个机器人操作一致 | 需给 B 增加一个**受保护的写接口**（B 的写面 +1） |

### 1.2 推荐方案 C 的接口与语义

**A 端**
```
GET  /api/guard/bot   → {configured: bool, preview: "***8f12", b_reachable: bool,
                         last_verified_at: 1794..., updated_by: "web"}
POST /api/guard/bot   → body {key: "<32-64 hex/uuid>"}
                        · 校验格式（复用 wecom-config 的 `[0-9a-fA-F-]{20,64}`）
                        · 调 B `POST /guard/config`；**B 返回 200 才落本地标记**
                        · 本地只存 `{bot_configured: true, bot_preview: "***8f12"}`，**不存明文**
```

**B 端新增（guardian-b v1.1.0）**
```
POST /guard/config   （Bearer，仅 TS 接口）
  body {wecom_key: "..."}  → 校验 → 原子写 data/runtime.json(0600)
GET  /guard/status   → 增加 alert: {configured: true, preview: "***8f12", source: "runtime|env"}
                        **永不返回明文**
```
告警器 `alerts.py` 取 key 的顺序：`runtime.json` → `GUARD_WECOM_KEY` 环境变量。

### 1.3 实现要点

1. **前端 `WECOM_BOTS` 有两份**（`templates/system.html:814` 与 `routes/system_routes.py` 里的常量），
   加第三项必须**同时改两处**，否则 UI 画得出来但后端返回 400「无效的机器人」。
2. 三个机器人的 `config_key` 建议：`wecom_sentry_webhook_key` / `wecom_status_webhook_key` /
   **`wecom_guard_webhook_key`**（哨兵/系统通知由 A 自己发，走现有 notifier；守护 key 走 A→B 下发）。
3. **「测试」按钮语义要区分**：
   - 哨兵/系统通知：直接发一条测试消息（现有 `wecom-test`）。
   - A7Z 守护：**不能从 A 发**（A 发得出消息就说明它没失联，测不出这条链路的意义）。
     应改为「让 B 发一条测试告警」→ A 调 B `POST /guard/test-alert`。
4. 机器人列表里第三个应显示 **B 端连通性**（`b_reachable`），否则用户填完不知道有没有生效。

### 1.4 注意事项

- key 属**敏感信息**：GET 只回 `***后 6 位`；日志、异常、响应体一律不得出现明文。
- B 的写接口必须：仅 Bearer + 仅绑 TS 接口 + 限流（如 5 次/分钟）+ 写前校验格式 +
  **不把 body 记进日志**（现有 werkzeug access log 只记路径，安全）。
- A 端必须在 **B 返回成功**后才把 `bot_configured` 置真，否则会出现「UI 显示已配，实际 B 没收到」。
- 若 B 不可达，前端应明确提示「本地未保存，请稍后重试」，而不是静默成功。

---

## 2. 需求 2：状态守护配置项

### 2.1 参数表（含取值考量）

| 分组 | UI 项 | 字段 | 默认 | 范围 | 取值理由 |
|---|---|---|---|---|---|
| 总开关 | 启用状态守护 | `enabled` | false | bool | **默认关**：未配置时零线程零请求（M116） |
| 僚机地址 | Tailscale 地址 | `endpoints.ts` | "" | `http(s)://host:port` | 隧道内允许 `http` |
| | HTTPS 地址 | `endpoints.https` | "" | **必须 https** | 配了就会强制 CA pinning |
| | 共享令牌 | `token` | "" | 32–64 hex | 只写不读；与 B 的 `GUARD_TOKEN` 一致 |
| | CA 证书路径 | `ca_path` | "" | 绝对路径 | 仅 https 通道需要 |
| 心跳 | 心跳间隔(秒) | `interval_s` | 60 | **15–60** | >60 会吃掉 B 的 3T(180s) 余量 |
| | 探针周期(秒) | `probe_every_s` | 150 | **60–150** | **必须 < B 的 ALARM 300s**，否则 TS 抖动误报「设备失联」 |
| 车态 | 状态拉取间隔(秒) | `states_interval_s` | 300 | 120–1800 | TeslaMate 配额克制（M63） |
| 离线窗口 | 出车(秒) | `offline_default_s` | 3600 | 60–21600 | 长途驾驶 |
| | 解锁(秒) | `offline_unlock_s` | 900 | 60–3600 | **新增可配**（当前写死 900） |
| | 上车(秒) | `offline_present_s` | 600 | 60–3600 | **新增可配**（当前写死 600） |
| 队列 | 积压上限(条) | `queue_max` | 200 | 1–1000 | 断网时每条 ~1.5KB |
| 自检阈值(P2) | 缩略图积压阈值 / 推送健康窗口 / usb_guard 陈旧阈值 | `thumb_pending_warn` / `wechat_ok_max_age_s` / `gadget_stale_s` | 50 / 86400 / 300 | | 现在写死在代码常量里 |

> **不做成可配的**：B 端的 `T/WARN/ALARM`、去重窗口。它们在 B 的 env 里，
> A 端只能**展示**（通过 `/guard/status` 回传），写成可编辑会造成「两边都能改、谁生效说不清」。

### 2.2 接口

```
GET  /api/guard/config
  → {enabled, endpoints:{ts,https}, token_set:true, token_preview:"***b1b6",
     ca_path, interval_s, probe_every_s, states_interval_s,
     offline_default_s, offline_unlock_s, offline_present_s, queue_max,
     b:{reachable, alarm_s, warn_s, dedup_s, classify}}
POST /api/guard/config     # 白名单字段；token 留空 = 沿用旧值（禁止"清空即清空"歧义）
  → {success, applied:{...回读落库值...}, need_restart:false}
POST /api/guard/reload     # 不重启 web，停旧线程启新线程（配置改动即时生效）
POST /api/guard/test       # 立即发一条测试心跳，返回 {channel, ms, error}
```

### 2.3 实现要点

1. **落盘仍是 `config/guard.json`**，原子写（`mkstemp` + `os.replace` + 目录 fsync，0600）——
   与 `guard_client.atomic_write_json` 复用同一实现，**不要再写一份 `open(w)+json.dump`**（M114/M122 已犯两次）。
2. **该文件在升级包白名单之外**（现已是），保存后不会被下次升级覆盖。
3. **校验必须在后端**做（前端 max/min 只是提示）：
   越界值 `logger.warning` + 夹取，与 `guard_client._CLAMP` 同一份常量，
   避免「UI 显示 600s、代码里被夹成 150s」这种两套真相。
4. **改配置要能热生效**：现有 sender 在 `app.py` 启动时创建，改完必须 `reload`
   （停线程 → 新建 sender），否则用户点保存后要重启 web 才生效 —— 这是最常见的"配了没反应"投诉源。
5. 保存成功要**回读落库值**再返回（照抄 `usb-guard-config` 的 `return merged` 做法），
   前端拿返回值刷新输入框，防「提示已保存实则未生效」。

### 2.4 注意事项

- **`enabled=false` 必须是完整闭环**：停线程 + 不再写 `guard_status.json` + 仪表盘显示「未启用」。
- **token 输入框 type=password + 占位符显示掩码**，留空表示不修改；另给「清除令牌」显式按钮。
- 保存端点属写操作，现有 `auth_guard` 的 CSRF 与会话校验已覆盖（`/api/` 前缀），无需新开口子。
- 配置里 `endpoints.https` 配成 `http://` 时，前端应直接拒绝保存并说明原因
  （后端也会拒：`https_scheme_required`）。
- 「测试」按钮不要写成"发一次心跳就给绿灯"——必须回报**走的哪条通道**（ts/https）与耗时，
  否则用户无法判断是不是 fallback 生效了。

---

## 3. 需求 3：仪表盘网络卡显示僚机通信状态

### 3.1 展示设计（不新增卡片，塞进现有 Hero 网络卡的 `hero-detail`）

```
📡 网络
● C12345  78%
TS 100.116.18.42 | LAN 12.127.12.115
僚机 ●正常 · 12s前            ← 新增这一行
实时 ↓ 1.2 MB/s ↑ 84 KB/s
本月家庭WiFi ↓ ... ↑ ...
```

四态文案（**判据写死，避免"看起来蓝就是好"**）：

| 条件 | 显示 | 颜色 |
|---|---|---|
| `enabled=false` 或未配置 | `僚机 未启用` | 灰 |
| `available && fail_streak==0 && age ≤ 3×interval` | `僚机 ●正常 · {age}s前` | 绿 |
| `fail_streak in 1..2` 或 `queue>0` | `僚机 ▲重试中 · 失败{n}次 · 队列{q}` | 黄 |
| `fail_streak ≥3` 或 `age > 5×interval` | `僚机 ✕不通 · {提示}` | 红 |

补充 hover 明细（title 或小字第二行）：`通道 ts/https、上次成功时刻、B 端 classify（ok/ts_channel_fault/device_offline）`。

### 3.2 数据与刷新

- 数据源：**已有** `GET /api/guard/status`（返回 `enabled/available/channels/snapshot.sender`）。
- 需要补两个字段给 UI：`sender.fail_streak`（已有）、`sender.last_ok_at`（已有）、
  `sender.queue`（已有）、`sender.ping_fail`（已有）—— **后端零改动即可支撑前三态**；
  若要显示 `B 端 classify`，需 A 把 `/guard/ping` 的响应体里的 `classify` 缓存下来（+8 行）。
- 刷新：**跟随仪表盘现有轮询**（stats 流/定时刷新），**不要新增独立 setInterval**——
  否则每次刷新多一个请求，弱网下与 SSE 抢带宽。
- 网络不通时该行显示「未知」，**不要显示为红色故障**（避免 WiFi 本身断了还怪僚机）。

### 3.3 注意事项

- 该卡片在窄屏会折行，新增一行需确认移动端不挤压（`hero-detail` 已有 5 行，第 6 行建议加 `font-size:0.7rem`）。
- 不要在这个位置放"点击跳转设置"的大按钮；给一个 `cursor:pointer` 的小字链接即可。
- 仪表盘是未登录也会打开的页面（若未启用登录保护），**该字段不得暴露 token / B 地址的完整 port 之外的敏感信息**。

---

## 4. 分期建议

| 期 | 内容 | 风险 |
|---|---|---|
| **P0** | 需求 3（纯展示，后端已有数据）+ 需求 2 的 GET/POST 配置接口与表单（不含 key 下发） | 低 |
| **P1** | 需求 1 方案 C：A `POST /api/guard/bot` + B `POST /guard/config` + 「让 B 发测试告警」 | 中（B 增加写面，需限流 + 只在 TS） |
| **P2** | 自检阈值可配、`offline_unlock/present` 可配、配置历史与「恢复默认」 | 低 |

## 5. 验收判据（每项都要可观测）

1. 保存配置后**不重启 web**，`data/guard_status.json` 的 `interval_s`/`probe_every_s` 变化生效。
2. `enabled` 从 true 改 false：线程停止（`/api/guard/status` 的 `sender.running=false`）、状态文件不再更新、仪表盘变灰。
3. 填错 token：`POST /api/guard/test` 返回 `http_401`；填对返回 `channel=ts, ms<500`。
4. 断网 60s：仪表盘僚机行转黄（`fail_streak≥1`）；恢复后 60s 内转绿并清队列。
5. A7Z 守护机器人：UI 保存 → B 的 `/guard/status` 出现 `alert.configured=true`；点测试 → 群里收到一条来自 B 的消息。
6. `config/guard.json` 权限 0600，且 `GET /api/guard/config` 响应里**不含明文 token**（可用 curl 全局搜索验证）。

---

## 6. 需要你确认的三件事

1. **设备版本**：你说已升到 `0.3.1.96`，但仓库最新是 `0.3.1.76`，`artifacts/` 里也没有 0.3.1.8x/9x 的包。
   若是笔误，我按 0.3.1.76 基线继续；若真有 0.3.1.96，请给我包或分支，否则我会基于错误基线改 UI。
2. **需求 1 是否接受方案 C**（A 代填 → 下发到 B）。若你希望"A 只做展示、key 仍在 B 上手工配"，那就是方案 A，我可以少做一个写接口。
3. **「测试」按钮**：守护机器人这一项，是否接受"由 B 发测试告警"（会在群里出现一条【测试】消息）。

---

## 7. 四项确认后的收敛（2026-11-08）

用户答复：① 自检保持"只展示"但**要把自检结果推成报告**；② 补 `thumb.last_scan_at` 落盘；③ 两处都有（设置页详细 + 仪表盘一行）；④ **B 端失联要单独提示**，A 侧 ping 连续失败 → 仪表盘红 + 一次本地日志 + **A 侧推企微**。

### 7.1 被推翻的前提：key 必须"双持有"

方案 C 的原始理由是"key 只存 B，A 不落地"。但 **④要求 B 挂了由 A 发通知**——B 挂了就不可能由 B 代发，
所以 A 必须能**独立**推企微。结论被迫改为：

| 位置 | 用途 | 承载 |
|---|---|---|
| A `config/sentry.json` → `wecom_guard_webhook_key` | 自检报告、B 失联通知 | 与其它 3 个 bot 同机制（含 `_disabled` 后缀的禁用语义） |
| B `guard-b.env` → `GUARD_WECOM_KEY` | 设备失联 / TS 通道故障 / 已恢复 | B 自己判死时用 |

**存储位置选 `sentry.json` 而不是 `guard.json`**，理由：现成的 `GET/POST /api/system/wecom-config`、
`get_wecom_status()`、`WECOM_BOTS` 校验、禁用机制全部可直接复用，UI 侧只是多一个 bot 条目；
`config/` 整个目录在升级时被 `_save_user_data/_restore_user_data` 保护，不会被覆盖。

同步策略：**保存即双写**。设置页保存 → ① 原子写 `sentry.json`（本地立即生效，不依赖 B）；
② 顺手 `POST {B}/guard/config` 下发同一 key。**两件事分开报告结果**——B 不通不能让本地保存失败，
UI 要显示"A 端已生效 / B 端未同步"，否则用户会以为没保存成功而反复点。

安全边界（新增三条硬约束）：
- `GET /api/guard/config`、`/api/system/wecom-config` **绝不回明文 key**，只回 `key_set` + 末 4 位掩码；
- `wecom_guard_webhook_key` 不进日志、不进心跳 body、不进 `/api/guard/status`；
- `config/guard.json` 与 `config/sentry.json` 均 0600，且都**不在升级包白名单**内（已确认现状成立）。

### 7.2 三处代码现状核对（直接决定工作量，均已验证）

| 事实 | 证据 | 对方案的影响 |
|---|---|---|
| `_ping()` 在双发 gate **之外**，TS-only 部署也会跑 | `guard_client.py:786-795`（`if eps.get("https") and eps.get("ts")` 只包住 probe，`self._ping()` 在其后） | ✅ 好消息：④ 的数据源**现成**，不用新增定时器 |
| 但 401 与超时被算成同一种失败 | `guard_client.py:826-834`：`not 200<=code<300` → `_ping_fail += 1` | ⚠️ 必须拆：401/403 = B 活着但令牌不匹配，文案不能写"B 失联"，否则诊断方向错 |
| 第 3 次失败**只打 warning**，无去重、无恢复推送 | `guard_client.py:839` | 需加 `_b_down_notified` 标记 + 恢复推送，否则 2/3 次之间抖动会重复推送 |
| `last_scan_time` 是主循环**局部变量**，从不落盘 | `bg_preview_generator.py:503,513` | ② 需要在 `scan()` 成功后原子写文件；bgpreview 是独立进程，**不能 import guard_client**（守护不该被别的服务依赖），自己 3 行 `mkstemp+replace` |
| `WECOM_BOTS` 前后端**已经不一致** | 后端 `utils/app_helpers.py:114-121` 有 3 项（sentry/status/**boot**）；前端 `templates/system.html:814-817` 只有 2 项 | 🐛 现存缺陷：`boot` 机器人 UI 里看不到也改不了。加第 4 项 `guard` 时必须同时补 `boot`，否则继续漂移 |
| `_queue_count` 只在失败轮刷新 | `guard_client.py:782` | 仪表盘要显示"队列 N"就得先修它（否则恢复后仍显示旧值） |

### 7.3 自检报告：推送条件必须定义，否则等于刷屏

"保持只展示"与"推自检报告"不冲突，但**必须区分"告警"和"报告"**：

| 类型 | 触发 | 频次上限 | 停用开关 |
|---|---|---|---|
| 跃迁报告 | `selfcheck.result` 跨档（pass→warn / pass→fail / warn→fail） | 同档 30min 内不重复 | 可关（默认开） |
| 恢复报告 | 回到 `pass` | 每次跃迁只推 1 条 | 跟随跃迁开关 |
| 日报摘要 | 每日固定时刻（默认 20:00） | 每天 1 条 | 可关（默认开） |
| **B 失联通知** | `_ping_fail ≥ 3`（≈7.5min，按 150s 探针） | 每次失联 1 条 + 恢复 1 条 | **不可关**（这是安全项） |

明确不做：`fail` 不升级为"每小时重推"（那是 B 端判死的职责，A 端重复推就成了两套告警互相打架）。

### 7.4 新增配置项（在 §2.1 的 12 项之外 +4）

| 键 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `report_transition` | `true` | bool | 自检跃迁/恢复是否推送 |
| `report_daily_at` | `"20:00"` | `HH:MM` 或 `""`（关闭） | 日报时刻；用 A 本地时间 |
| `report_cooldown_s` | `1800` | 300–7200 | 同档去重窗口 |
| `ping_fail_threshold` | `3` | 2–6 | B 失联判据（次）；×`probe_every_s` 即检测时延 |

### 7.5 实施清单（文件级）

**P0（不依赖 B 改造，可独立发版）**

| # | 文件 | 改动 |
|---|---|---|
| 1 | `bg_preview_generator.py` | `scan()` 成功后原子写 `data/preview_scan.json`：`{ts, total, pending}` |
| 2 | `guard_client.py` | `thumb.last_scan_age_s` 改读该文件（现恒为 null）；修 `_queue_count`；`stats()` 补 `last_scan_at/b_down/b_down_reason/result` |
| 3 | `routes/system_routes.py` | 新增 `GET/POST /api/guard/config`（token 掩码、范围校验、热 reload） |
| 4 | `utils/app_helpers.py` | `WECOM_BOTS` 加 `guard`（A7Z 守护），**并补前端缺失的 `boot`** |
| 5 | `templates/system.html` | 「健康与通知」内新增守护分组：机器人 key + 12+4 项参数 + 自检四项明细表（含阈值列）+ 报告开关 |
| 6 | `templates/dashboard.html` | 网络卡 `hero-detail` 加 2 行（僚机 / 自检），跟随现有轮询，不新增 `setInterval` |

**P1（需 B 端升到 v1.0.2）**

| # | 文件 | 改动 |
|---|---|---|
| 7 | `guard_client.py` | 自检报告推送器（跃迁/恢复/日报 + 冷却），独立 `_report_due()` 判定，不占心跳节拍 |
| 8 | `guard_client.py` | B 失联推送：拆 `ping_fail_kind ∈ {timeout, http_401, http_5xx}`，去重 + 恢复 |
| 9 | `guardian-b/app.py` `config.py` | 新增 `GET/POST /guard/config`（Bearer + 只绑 TS + 原子写 `runtime.json` 0600 + 不回明文）；告警三态 `mode: dryrun|sent|failed`（**修已记录的 `delivered:True` 语义缺陷**） |

**P2**：自检阈值可配（现为常量 `THUMB_PENDING_WARN/WECHAT_OK_MAX_AGE/WECHAT_FAIL_STREAK`）、恢复默认、日报静默时段。

### 7.6 必须写明的两个固有盲区

1. **A 完全断网时，A 侧什么都推不出去**（企微也走公网）。此时只能靠 B 判死——而 B 若同时挂了，则**无告警**。
   这不是实现缺陷，是"两条链路同时断"的物理结果，但必须让用户知道，不能让人误以为 A 侧推送是兜底。
2. **自检报告依赖 A 的企微直连**（`qyapi.weixin.qq.com`）。车辆热点/弱网下推送可能失败，
   需要本地排队（复用 `_queue` 同构的最小重试），否则"报告"在真正需要时恰好推不出。

### 7.7 验收增补（对应 ④ 与 ①）

7. `systemctl stop guard-b` → ≤8min 仪表盘僚机行转红 + 日志一条 + 企微收到**一条**「B 侧失联」；
   恢复 `start` → 再收**一条**「B 侧已恢复」，且不重复。
8. 把 B 的 `GUARD_TOKEN` 改错 → 文案必须是「令牌不匹配」**而不是**「B 侧失联」（401 与超时已拆分）。
9. 人为把 `teslausb-bgpreview` 停掉 → 5min 内收到**一条**跃迁报告（`thumb: fail`），
   连续 30min 不再重复；恢复后收到一条恢复报告。
10. 日报到点（20:00）只收 1 条，内容含四项 + 通道 + 队列 + `last_scan_age`。
11. `curl /api/guard/config | grep -c "$TOKEN"` → **0**。

---

## 8. 实现落点（v0.3.1.77 已实现）

> 落地时相对 §7 有三处**主动加强**，都是写代码过程中发现的真问题，不是原设计的遗漏。

### 8.1 文件与改动

| 文件 | 改动 |
|---|---|
| `guard_client.py` | `normalize_config()` / `public_config()` / `apply_config()` / `is_running()`；`_push_wecom()` / `_guard_bot_key()` / `_scrub()`；`GuardSender._ping_ok/_ping_failed`（拆 5 类）、`_maybe_report/_maybe_daily_report/_push_report/_retry_pending_report`、`_fmt_*`；`_thumb_snapshot` 改读 `preview_scan.json`；`send_test_report()` / `push_bot_to_b()` |
| `bg_preview_generator.py` | `write_scan_state()`：`scan()` 末尾原子写 `data/preview_scan.json`（`{ts,total,pending}`，0600→0644，不 import guard_client） |
| `routes/system_routes.py` | `GET/POST /api/guard/config`、`POST /api/guard/test-report`、`POST /api/guard/push-bot`；`/api/system/wecom-config` 保存 `guard` bot 时顺带下发 B |
| `utils/app_helpers.py` | `WECOM_BOTS` 增加 `guard`（A7Z 守护） |
| `templates/system.html` | 守护分组（机器人 key 复用 bot 块 + 9 个数值参数 + 开关 + 日报时刻 + 自检四项明细表 + 三个按钮）；删除未使用的硬编码 `WECOM_BOTS` |
| `templates/dashboard.html` | 网络卡加「僚机 / 自检」两行；`refreshGuardLine()` 搭 SSE 的车、30s 节流 |
| `static/style.css` | 补 `.status-dot`（**此前全项目没有这个类的定义**，模板里写的 `status-dot green` 一直是不可见的空 span） |

### 8.2 三处主动加强

1. **预热期 300s**：开机/升级后前 5 分钟只跟踪 `selfcheck.result`、**不推跃迁报告**。
   原因：升级后 `teslausb-web` 可能先于 `teslausb-bgpreview` 起来，此时 `preview_scan.json`
   还不存在 → thumb 判 `warn`；等 bgpreview 首次扫描完成又变 `pass` → 会推一条
   「自检恢复：注意 → 正常」的纯噪音，一年升级十几次就是十几条假消息。
2. **待发报告用小列表（≤3 条）而不是单槽**：单槽会让「B 侧失联」被随后的一条自检报告覆盖掉。
   B 失联是安全项，必须排队逐条补发（测试 G13 锁死）。
3. **`no_guard_bot_key` 不入重试队列**：没配机器人属于配置问题，重试一万次也不会成功，
   只 `warning` 一次，避免断网之外又多一条永远失败的队列。

### 8.3 「保存即双写」的最终实现

- 设置页保存**机器人 key**（走既有 `/api/system/wecom-config`）→ 先写 `sentry.json`（本地立即生效），
  再 `POST {B}/guard/config` 下发；两件事**分开报告**，B 不通时消息里明说「僚机未同步（原因）」，本地不回滚。
- 设置页另有「🔗 同步到僚机」按钮（`POST /api/guard/push-bot`）可单独重试。
- 保存**守护参数**（`/api/guard/config`）只写本地 + 热生效，不触发 B 调用（参数在 B 侧是只读的，见 §2.1）。

### 8.4 B 端 v1.0.2

- `GET /guard/config`：回 `alert.{configured,key_tail,dry_run}` + `warn_s/alarm_s/dedup_s/offline_default_s`，**不含明文 key**。
- `POST /guard/config`：只接受 `wecom_key` / `alert_dry_run`（其余字段一律 400），原子写 `data/runtime.json`（0600），
  内存热生效；`config.py` 启动时 `runtime > env > 默认`，重启不回退。
- `alerts.py` 告警结果由 `bool` 改**三态** `"sent"|"dryrun"|"failed"`；`judge.py` 事件里同时给
  `mode` 与 `delivered`（`mode in (sent,dryrun)`）。这修掉了 2026-11-07 现场「`delivered: True` 但
  无法确定群里到底收到没有」的缺陷。

### 8.5 验证

| 套件 | 结果 |
|---|---|
| `_test_guard_client.py` | **123 PASS / 0 FAIL**（新增 G 段 32 项） |
| `guardian-b/tests/test_judge.py` | 21/21 |
| `guardian-b/tests/test_config_api.py`（新增） | 23/23 |
| 既有回归 9 套 | 335 项全绿 |
| 路由冒烟（20 项） | 保存/夹取/热启停/掩码/400 分支全过 |

---

## 9. 校订表（§1~§6 的过时说法 → 实际实现）

| 位置 | 原文（已过时） | 实际 |
|---|---|---|
| §1 标题 / §1.2 | 「推荐方案 C」：key 只存 B | **已被 §7.1 推翻**：key 双持有（A `sentry.json` + B `runtime.json`），因为「B 挂了由 A 通知」必须由 A 直连企微 |
| §1.2 接口清单 | `POST /api/guard/bot`、`POST /api/guard/reload` | 实现为 `GET/POST /api/guard/config`（保存即热生效，**没有单独的 reload**）+ `POST /api/guard/push-bot` + `POST /api/guard/test-report`；机器人 key 仍走既有 `POST /api/system/wecom-config` |
| §1.2 | 「测试」由 B 发一条测试告警 | 实现为**由 A 发**自检报告（`/api/guard/test-report`）：要验证的正是「A → 企微」这条独立链路，经 B 转发就测不出来 |
| §2.1 参数表 | 14 项 | **18 项**：另加 `report_transition` / `report_daily_at` / `report_cooldown_s` / `ping_fail_threshold` |
| §2.1 参数表 | 三个离线窗口是 3 个参数 | 实现为 1 个 `offline_default_s` + 代码内分档（driving=满窗口、unlocked=900、user_present=600，均 `min(base, offline_default_s)`） |
| §5 验收 | 6 条 | **8 条**：另加「预热期内不推」与「报告队列不被覆盖」 |
| §3.1 展示 | 僚机 4 态 | **5 态**：多一档「令牌不匹配」（401 与超时已分开） |
| §4 分期 | P0 不含报告推送 | 实际 P0+P1 一起做（用户选择），报告推送已在 v0.3.1.77 |
| §8.5 | 123 PASS | **126 PASS**（新增安全项队列用例）；另加 `_test_guard_api.py` 31 项 |
