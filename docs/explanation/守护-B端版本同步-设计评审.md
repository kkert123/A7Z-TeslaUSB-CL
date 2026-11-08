# B 端版本同步 · 设计评审

> 日期：2026-11-08 ｜ 基线：`guardian-b` v1.0.2 ｜ 关联：`docs/explanation/自检守护-B端VPS设计.md`、`docs/explanation/守护配置UI与仪表盘状态-设计建议.md`
> 性质：**评审意见（未动码）**。结论：思路可行，但按原样实现会**削弱 B 端存在的唯一理由**。以下 6 条建议中，S1/S2 属于"不改就等于没做安全"。

---

## 0. 一句话结论

"让 A 能远程升级 B"在架构上是**反向信任**：B 的全部价值是"A 出问题时它还能说实话"，而远程换代码的能力等于给 A 一把"改变见证者"的钥匙。
加密通信和文件校验解决的是"传输途中不被篡改"，但**解决不了"被授权的 A 自己作恶"**。因此建议把授权（谁能发起）与加密（怎么传）彻底分开设计，并给"生效"加一道 A 拿不到的确认。

---

## 1. 六条现状事实（决定了工作量，均可复现）

| # | 事实 | 证据 | 影响 |
|---|---|---|---|
| F1 | **B 端进程以 root 运行** | `guardian-b/deploy/guard-b.service` 无 `User=` | "本地锁死"目前**根本不成立**：进程能改自己的 `app.py`、`state.json`、`guard-b.env`（含告警 key） |
| F2 | `ProtectSystem=full` 下 **`/opt` 可写** | 同上（`full` 只保护 `/usr` `/boot` `/etc`） | 即使换 `User=`，也要改 `strict` + `ReadWritePaths` 才算隔离 |
| F3 | 安装脚本会写 **`/etc/systemd/system/guard-b.service`** 并 `daemon-reload` | `guardian-b/install.sh:70-71` | 这份"包"的足迹伸到了 `/etc`。**能改 systemd unit 的包 = 能改一切**，必须移出可远程升级范围 |
| F4 | 鉴权是**共享 Bearer**，`hmac.compare_digest` 比较 | `guardian-b/app.py:67-72` | 一旦 A 沦陷，token 被读走即可发起升级；**token 无法区分"人"与"被劫持的 A"** |
| F5 | `seq` 只存不校验 | `state.py:100-101` 直接覆盖 `last_seq`；`app.py:86-94` 仅解析 | **无重放防护**。升级指令若沿用 seq，旧指令可被重放 |
| F6 | 版本号已在 `/guard/status` 返回 | `app.py:118` `"version": VERSION` | "显示 B 版本号"是**零新增攻击面**的改动，可先单独上 |

> 另一条跨端事实：A 端现有验签用 `ssh-keygen -Y verify` + `config.UPGRADE_PUBKEY`（`upgrade_service.py:771-799`），**只签 tarball 本身**，不签文件清单。这个粒度对 B 端不够，见 §4。

---

## 2. 安全风险：五条，按危险度排序

### R1（最高）A 沦陷 → 掏空 B 的告警能力，且**不产生任何噪音**

攻击链：

```
局域网/AP 上任意终端（登录保护默认未启用，见 S1）
  → POST A 端 /api/guard/upgrade（新接口）
  → B 被换成一个"永远 classify=normal"的版本
  → 此后 A 真的失联，也没有任何人被告警
```

最危险的性质：**它不制造噪音，它消除噪音。** 其他所有入侵都会留下告警/日志异常，这一条不会。用户要到"出事时才发现自检是假的"。

**改进**：见 §3 的"授权与加密分离" + §6 的"生效需人工确认"。最低限度也要做到：
- 升级只能升到**已公开发布**的版本（版本号 + 签名双重白名单），不能是任意二进制；
- 升级后 **A 端必须持续看到"版本未变/已变"**，且 B 自己在下一次 cron 里上报版本（单向自证），A 端发现"我下发了却没生效"要告警。

### R2 升级包可以改告警目的地 = 最省力的阻断告警

不用换代码，只要让包里的逻辑（或安装后写 `runtime.json`）把 `GUARD_WECOM_KEY` 指到攻击者的群，真实失联就发到别人群里。

**改进**：把"告警配置"列为**升级不可触碰项**：
- `guard-b.env` / `runtime.json` 的 `GUARD_WECOM_KEY` 由 A 下发（已有 `/guard/config`），**升级过程一律不得读写**；
- 包内若出现 `guard-b.env`、`runtime.json`、`state.json` 任一路径 → **整包拒绝**（不做"跳过"处理，因为跳过意味着包的结构与预期不符）。

### R3 降级重放

攻击者不需要新包，只要重放一个**旧版本**（真实签过名、含已知漏洞、或行为与新约定不符）就能降级。

**改进**：`/opt/guard-b/.upgrade/floor`（root 0400）记录最低允许版本，单调递增；同时 `min_from` 字段声明兼容起跳版本。

### R4 升级窗口本身就是攻击窗口

升级要停服务、要写代码目录、要切符号链接。这段时间是 B 唯一"可被改写"的时刻。若接口长期开着，等于把窗口常开。

**改进**：**默认关闭 + 武装窗口**。接口存在但 `409`，只有 A 端流程在 B 上"武装"（arm）后才可用，窗口 5–10 分钟，一次性 nonce，用后即废。

### R5 升级期间的"必然假告警"

B 一停机，A 的心跳就连续失败 → 现有逻辑 3 次（≈7.5min）后推「B 侧失联」。这是**流程自己制造的假消息**，且会恰好出现在用户最关注的时候。

**改进**：A 端在发起升级时置 `_upgrade_window_until = now + 900`（落盘，不要只在内存），窗口内 `_ping_failed` 只计数不判死、不推送；窗口结束仍未恢复才告警。这是 M85（后台 timer 逆转的操作须落成持久抑制）的同类问题。

---

## 3. 加密通信方案

### 3.1 三个选项

| 方案 | 加密 | 身份认证 | 运维成本 | 判断 |
|---|---|---|---|---|
| **A. Tailscale（现状）+ 应用层签名指令** | WireGuard（ChaCha20-Poly1305，节点级密钥） | TS 节点身份 + Ed25519 指令签名 | **零**（已有） | **推荐** |
| B. OpenResty TLS + 双向证书（mTLS） | TLS 1.3 | 客户端证书 | 中（证书签发/轮换/过期） | 备选（仅公网兜底时启用） |
| C. 沿用共享 Bearer + 自研 AES 加密 body | 自研 | 无（token 即身份） | 低 | **禁止** |

**C 必须明确禁止**：自研加密协议是这类项目最常见的致命错误；而且它并不解决"谁有权授权"的问题——token 谁拿到都一样。

### 3.2 推荐组合：传输靠 TS，授权靠签名

```
传输层：Tailscale（已在用，只绑 TS 接口，不开公网）
应用层：升级指令 = {version, pkg_sha256, nonce, ts, sig}
        sig = Ed25519_sign(A_guard_private, "upgrade|{version}|{pkg_sha256}|{nonce}|{ts}")
B 端：公钥固化在 root-owned 只读文件（/opt/guard-b/.trusted/push.pub 0400），
      或直接编译进 verifier，**不读 env**（env 谁都能改）
```

好处：
1. **即使 TLS 被降级/中间人，没有私钥也发不出有效指令** → 加密从"唯一防线"降为"纵深一层"；
2. **不需要把任何共享密钥放进升级请求** → 少一次泄露面；
3. `nonce + ts`（±120s 窗口）+ 持久化 `last_nonce_ts` 防重放；**注意必须落盘**，否则 B 重启即可重放（现状 F5 的 seq 就是反例）。

### 3.3 签名私钥放哪：**不要放在 A 上**

| 位置 | 后果 |
|---|---|
| A 设备上（`key` 文件） | A 沦陷 = 私钥泄露 = 攻击者可伪造任意 B 包。**当前 A 的 `upgrade_key` 就是这样，但它只签"发给自己的包"，危害有限；签 B 的包不能复用** |
| 离线开发机（推荐） | 打包时签名，A 只**搬运**已签好的包；A 沦陷最多让 B 升到一个**真实发布过的版本**，配合 §R3 版本地板，危害被压到"降级到旧版" |

**密钥分离是硬要求**：`guard_push_key` ≠ `upgrade_key`。理由：A 的升级私钥用于签"给 A 自己的包"，一旦泄露，攻击者同时获得"伪造 B 包"的能力，等于一次沦陷打穿两端。

---

## 4. 文件校验机制

### 4.1 签"清单"而不是签"包"

现状（A 端）签 tarball 本身。对 B 端不够，因为签名只保证"这坨字节是我发的"，不保证"里面装了什么"：

- 可以含 `../`、绝对路径、符号链接、硬链接、设备节点、setuid 位；
- 可以含 `post_install.sh`，装完为所欲为 —— **这是最大的后门面**。

**建议三层结构**：

```
manifest.json  ←─ 被签名（Ed25519，离线签）
  {
    "format": 1,
    "version": "1.0.3",
    "min_from": ["1.0.0","1.0.1","1.0.2"],
    "files": [ {"path":"app.py","sha256":"…","mode":"0644"}, … ],
    "removes": ["old_module.py"]
  }
package.tar.gz  ├── manifest.json
                ├── manifest.json.sig   ← 对 manifest 签名
                └── files/…             ← 只允许 .py / .md / .service(静态) / 静态资源
```

校验顺序（**任一步失败即整包拒绝，不允许部分安装**）：

1. 解包到临时目录（不落位）→ 校验条目类型/路径/mode/大小上限/条目数上限；
2. 用固化公钥验 `manifest.json.sig`；
3. 逐文件算 sha256 与 manifest 比对（**双向**：manifest 里有的必须存在，包里多出的必须报错）；
4. 检查 `version > floor`、`current_version ∈ min_from`；
5. 检查 `removes` 非空时其目标必须在当前版本清单内（防"删掉不属于你的文件"）。

### 4.2 Python 3.9 的硬约束（**这条会卡住实现**）

`tarfile.extractall(filter="data")` 是 **Python 3.12** 才有的。B 端是 Debian/Ubuntu 自带 3.9–3.11（`requirements.txt` 写 `Python >= 3.8`），**必须手写解包循环**：

```python
for m in tar.getmembers():
    if m.isdir():      continue          # 目录按 manifest 推导，不信任包内目录条目
    if not m.isfile(): raise Reject("非普通文件: %s" % m.name)
    name = posixpath.normpath(m.name)
    if name.startswith(("../", "/")) or ".." in name.split("/"): raise Reject(...)
    if name not in allowed_paths: raise Reject(...)   # 精确集合，不是前缀匹配
```

**前缀匹配是错的**：`startswith("app/")` 挡不住 `app/../../etc/x`；先 `normpath` 再**集合精确匹配**才安全。

另外：解包前先 `tar.getmembers()` 全量扫描（会读一遍索引），先做"体检"再写盘，避免"写到一半发现第 7 个成员是符号链接"。

### 4.3 包里不许有可执行脚本

`install.sh` 现在会写 `/etc/systemd/system/guard-b.service` 并 `daemon-reload`（F3）。这条能力等于"包 = root"。**建议**：

- 包内**禁止**出现 `*.sh`；
- "怎么装"的逻辑全部放在 B 上 root-owned 的 `guardb-upgrade` helper（约 200 行，一次性写好，**不在可升级范围内**）；
- systemd unit 变更（如果需要）**不随包发**，作为人工操作项写进升级说明。

代价：以后改 unit 要手工；收益：远程升级的爆炸半径被限制在"应用层 python 文件"。

---

## 5. 权限与访问控制边界

### 5.1 目标状态：Flask 进程对代码目录零写权限

| 对象 | 属主/权限 | 说明 |
|---|---|---|
| `/opt/guard-b/releases/<ver>/` | `root:root 0755` | 每个版本一个目录，**不可变**（升级只新增目录，不原地改） |
| `/opt/guard-b/current` → `releases/<ver>` | `root:root` 符号链接 | 唯一的"提交点" |
| `/opt/guard-b/data/` | `guardb:guardb 0700` | 唯一可写区 |
| `/opt/guard-b/.trusted/push.pub` | `root:root 0400` | 信任根，进程只读 |
| `/opt/guard-b/.upgrade/` | `root:root 0700` | journal / floor / staged 包 |
| `/opt/guard-b/guard-b.env` | `root:guardb 0440` | 进程可读不可写（**关键**：现在可写，见 F1） |

`guard-b.service` 建议加固（在现有基础上，方向是 `strict` + 白名单）：

```ini
User=guardb
Group=guardb
ProtectSystem=strict                 # 关键：/opt 也变只读
ReadWritePaths=/opt/guard-b/data     # 只放 data
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
CapabilityBoundingSet=               # 清空全部 capability
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallFilter=@system-service
MemoryDenyWriteExecute=yes           # 纯 Python + Flask/requests 可承受（无 JIT）
```

> `MemoryDenyWriteExecute` 需要回归验证：Flask/requests 全纯 Python，理论上安全；但若将来引入 `cryptography` 的 Rust 扩展需实测。

### 5.2 升级 helper 单独一个 unit

```ini
# guardb-upgrade.service（oneshot，不入升级包）
[Service]
Type=oneshot
User=root
ProtectSystem=strict
ReadWritePaths=/opt/guard-b          # 只有它能写代码目录
ExecStart=/opt/guard-b/.upgrade/bin/apply --journal /opt/guard-b/.upgrade/journal
```

- **不接受外部传"包路径"**：helper 自己从 `.upgrade/staged/` 里取，文件名由 helper 自己拼；
- 单飞锁：`flock /opt/guard-b/.upgrade/lock`（M122：同一次操作的多个入口必须共用同一把锁）；
- 由 `guardb-upgrade.path` 监听 `data/upgrade/request` 触发：Flask 只能**写一个文件**，写完之后跑什么由 root 侧的 unit 定义。
  **不用 sudo**：`guard-b.service` 开着 `NoNewPrivileges=yes`，而 NNP 会禁止 sudo 提权（setuid 被禁），两者互斥——这是实现时才暴露的坑（见 §12）。

### 5.3 网络侧访问控制

| 控制 | 做法 |
|---|---|
| 只绑 TS | 现状已满足（`GUARD_BIND_HOST=100.x.x.x`） |
| 只信 A | `request.remote_addr` 必须在 TS CGNAT 段 `100.64.0.0/10`；**不信任任何 `X-Forwarded-For`**（与 A 端 `_client_ip()` 的 S2 教训同源：一旦有反代，`remote_addr` 恒 127.0.0.1，两道防线同时失效） |
| 公网兜底时 | `openresty-guard-b.conf` 已给 `/guard/config` 加 `return 403`；**升级端点必须同样 403**，写进同一份配置模板 |
| 窗口期 | 默认 `409 disabled`；arm 后 300s 有效；一次性 nonce |
| 速率 | 每分钟 ≤1 次请求；失败 3 次即 disarm |

### 5.4 A 端侧

- 新接口 `/api/guard/upgrade` **必须走登录守卫**（现状 `/api/guard/*` 不在 `PUBLIC_PREFIX`，正确）；
- 但**登录保护默认未启用**（S1）→ 局域网任何人可点。这条风险原本是"接管 A"，现在升级为"接管 A + 换掉见证者"，**严重度上升一档**。至少：
  - 二次确认弹窗写明「本次将重启僚机判死服务，期间设备失联将无人告警」；
  - 未启用登录保护时，该按钮**禁用**并提示先去启用（一次性成本，收益很大）。

---

## 6. 异常与回滚处理

**前提：这是车载设备，点火断电是常态。升级必须在任意时刻断电都能恢复。**

### 6.1 journal 状态机

```
staging ──→ verified ──→ committed ──→ healthy
   │            │            │
   └────────────┴────────────┴──→ rolled_back
```

- 每次推进：`mkstemp + fsync + os.replace`（M114/M122，本项目已两次犯过非原子写）；
- `committed` 的**唯一动作**是交换 `current` 符号链接（同目录内 `os.replace` 原子）；
- **提交前绝不删除旧版本**；保留 ≥2 代（`releases/` 下按版本目录）；
- 开机 `guardb-upgrade-resume.service` 检查 journal：停在 `verified` → 丢弃 staged 包；停在 `committed` 未 `healthy` → 跑健康检查，不过就 rollback。

### 6.2 健康检查要验"语义"，不是验"端口活着"

```
1. GET /healthz         → 200
2. GET /guard/status    → version == 期望版本
3. 状态可读             → state.json 能加载，classify 返回合法值
4. 告警可发             → alert.configured == True（只查配置存在，不真发消息）
5. 观测 60s             → 不重启（systemd NRestarts 未增长）
```

第 5 条建议交给 systemd 的 `StartLimitBurst=3` / `StartLimitIntervalSec=300`：连续崩 3 次即 `failed`，由 resume 服务看到 `failed` 直接回滚。这比自己在 Python 里数更可靠（进程崩了就没人计数了）。

### 6.3 回滚要处理"状态迁移"

| 类型 | 处理 |
|---|---|
| 纯代码变更 | 切回 `current` + restart，`data/` 不动 |
| `state.json` schema 变更 | 升级前 snapshot `data/` → `data.bak-<ver>`；回滚时一并恢复。**规则：迁移必须向后兼容一个版本**（新版本能读旧格式，旧版本读到新格式要能降级或拒绝启动并回滚，不能崩） |
| `runtime.json` 变更 | 同上；**注意回滚不能丢 `wecom_key`**，否则 B 的告警功能静默失效（这是安全功能，优先级高于"配置干净"） |

### 6.4 A 端不得把"请求返回 200"当作成功（M92）

```
POST /guard/upgrade → 202 {job_id}
GET  /guard/upgrade/status?job=<id> → {phase, pct, need_confirm?, error_kind?}
```

- A 端 UI 五态：已下发 / 校验中 / 待确认 / 已完成 / 失败（**分类**：签名不符·版本不允许·空间不足·健康检查失败已回滚·僚机不可达）；
- 失败原因必须分类，不能一律"升级失败"——这与现有 ping 拆 `timeout/auth/5xx` 是同一条经验；
- **禁止并发**（M92）：A 端按钮要用**跨轮询的标记位**（不是只设一次 `disabled`，会被 30s 轮询重置，M122）；
- 超时不是失败：总时长上限 10 分钟，超时显示"仍在进行，请稍后查看"，不重试下发。

---

## 7. "生效需人工确认"：切断 R1 的那一刀

推荐的折中（既满足"升级按钮"，又不让 A 单方面换掉见证者）：

```
1. A 下发 → B 校验 → 解包到 releases/<ver>.staged（不切 current）
2. B 生成 confirmation_code = HMAC(pkg_sha256, local_secret)[:6]，10 分钟有效
3. B 在 /guard/status 与本地日志显示：「已就绪 v1.0.3，确认码 7F3A2C」
4. A 端 UI 弹出输入框（或显示"需在 B 上执行一条命令"）
5. 确认后 B 切 current + restart + 健康检查
```

为什么这一步值：**它把"远程执行代码"降级为"远程预置 + 本地批准"**。攻击者拿到 A 的全部权限，最多让 B 多下载一个包（还必须是真实发布版），不能让它生效。

两种确认通道，按你的运维习惯二选一：
- **A 端输码**（方便）：码本身绑 `pkg_sha256`，无法转移到别的包；攻击者读不到 B 的响应就输不出码。弱点是若 A 能读 `/guard/status` 就也能读到码 → 防护降级为"需要人看一眼"，仍能挡住全自动攻击。
- **B 上敲命令**（最稳）：`guardb-upgrade confirm`，A 在任何情况下都无法完成。

> 若你判定"就要全自动"，那至少要：包只能来自**已发布版本白名单**（A 端签名列表）+ 版本单调 + 每次升级后 A 端必推一条企微。这三条能把风险压到"可审计"，但压不到"不可利用"。

---

## 8. 各维度的关注重点（速查）

| 维度 | 关注重点（只记这一句） |
|---|---|
| **安全风险** | 不是"传得安不安全"，而是"**谁有权改变见证者**"——B 的告警核心必须在 A 的权限边界之外 |
| **加密通信** | 传输用 TS/TLS，授权用**非对称签名**；禁止自研加密；私钥与 A 自身升级密钥**分离**，且最好离线 |
| **文件校验** | 签 **manifest** 不签 tarball；路径用**规范化+精确集合**匹配不用前缀；**包内禁止可执行脚本**；注意 3.9 无 `filter='data'` |
| **权限边界** | Flask 进程对代码目录**零写权限**（`ProtectSystem=strict` + 独立 root helper）；可写区只有 `data/`；**告警 key 不可被升级触碰** |
| **异常与回滚** | 任意点断电可恢复：journal 状态机 + 符号链接原子提交 + 语义健康检查 + `StartLimitBurst` 兜底；回滚要覆盖 `data/` 与 `runtime.json` |

---

## 9. 分期建议（按"先拿到价值、再碰危险"）

| 期 | 内容 | 新攻击面 | 说明 |
|---|---|---|---|
| **P0** | 版本页显示 B 版本（读现有 `/guard/status`）+ 版本不一致时标注 | **零** | 半天量。F6 已具备，不改 B 一行代码 |
| **P1** | B 端本地升级 helper：验签 manifest + 安全解包 + 符号链接切换 + 回滚（**手工触发**） | 零（无新端口） | 把现在会写 `/etc` 的 `install.sh` 换掉。收益最大、风险最低 |
| **P2** | A 下发 staged + 确认码 + A 端升级按钮（`arm` 窗口） | 中 | 需要 §7 的确认环节 |
| **P3** | 全自动（A 校验通过即生效） | 高 | **不建议**；若做，必须配 §R1 的三条补偿 |

**推荐先做 P0 + P1**：P0 让你立刻"看得见版本"，P1 让"升级这件事本身变安全"，两者都不引入远程代码执行。P2 再根据你对"必须一键"的强度决定。

---

## 10. 待决策（三条，影响架构走向）

1. **是否接受"预置 + 本地确认"两步**（推荐）还是"下发即生效"？
2. **B 包的签名私钥放哪**：离线开发机（推荐）还是 A 设备上？
3. **B 的告警核心是否允许远程升级**：建议把"判死 + 告警"冻结在 P1 手工通道，只有接口层可远程升级；否则 R1 无法真正关闭。

---

## 11. 实现落点（v0.3.1.78 / guardian-b v1.1.0）

### 11.1 最终目录结构

```
/opt/guard-b/
  current -> releases/1.1.0          # 唯一提交点（原子 rename）
  releases/1.0.2/  releases/1.1.0/   # 只增不改；保留 current + 2 代
  .tools/upgrade.py   0444 root      # 验签库（**不在升级包内**）
  .tools/apply.py     0500 root      # root helper（唯一能写代码目录）
  .trusted/pkg.pub    0444 root      # 签名公钥（principal 必须 a7z-guard）
  .trusted/floor      0444 root      # 版本地板（防重放旧包）
  data/               0750 guardb    # 唯一可写区
     upgrade/{journal.json, secret, staged/, incoming/}
  guard-b.env         0440 root:guardb  # 可读不可写（防改告警目的地）
  venv/
```

### 11.2 三处与设计不同的实现选择

| 设计原话 | 实现 | 原因 |
|---|---|---|
| 「签 tarball」 | 签 **manifest.json**，tar 内文件逐个核 hash | 签名只证明"这坨字节是我发的"，不证明"里面装了什么" |
| 「包内禁止 .sh」 | 改为**整个包只允许 `.py` 平坦文件** | 连子目录都不允许：`files/x/y.py` 会让路径校验复杂一个量级，而 B 端的模块本来就是单层 |
| 「B 显示确认码」 | 码**总是**写本地日志（权威），是否也走 API 由 `GUARD_UPGRADE_CODE_VIA_API` 决定 | 只有 `=0` 才是真正的带外确认；`=1`（默认）等于"人看一眼"，是可接受的便利性折中 |

### 11.3 三条主动加强

1. **验签库与 helper 不进包**（`.tools/`）。否则一次合法签名就能顺手把"校验"这层换成不做校验的版本 —— 锁死形同虚设。
2. **`apply.py` 落地前重新核 hash**。`staged/` 对 `guardb` 可写，验签到落地之间存在时间窗口；只信任"刚算出来的字节"，不信任中间产物。
3. **升级窗口落盘 900s**（`data/guard_upgrade_window.json`）。B 重启必然让 ping 连续失败，若不抑制，每次升级都会收到一条假「B 侧失联」，久而久之没人再信这条告警（M85）。

### 11.4 已知边界（写进运维文档，不当缺陷）

- **A 端预验签 ≠ 完整校验**：A 只验 manifest 签名，不逐文件核 hash。篡改载荷 A 可能放过，B 必然拒绝（`hash_mismatch`）。UI 文案写的是"已验签"，指的是签名，不含内容完整性 —— 这一点必须在发布说明里讲清。
- **拿到 `guardb` shell 的攻击者能自己算确认码**（secret 在 `data/upgrade/secret`，该用户可读）。所以确认码防的是"远程自动化"，不是"本机提权"。本机提权的那道墙是 systemd 沙箱 + root helper 的哈希复核。
- **回滚只切代码**，`data/` 不动。新版本若改过 `state.json` schema，回滚后可能读到不认识的字段（`StateStore._load` 已有类型兜底，但仍建议升级前 `cp -a data data.bak-$(date +%s)`）。

### 11.5 验证

| 套件 | 结果 |
|---|---|
| `guardian-b/tests/test_upgrade.py`（新） | **41 / 41**：签名/namespace/篡改/缺件/多余件/穿越/绝对路径/符号链接/setuid/子目录/禁区/版本策略/体积/确认码/断电续跑/公钥冒充 |
| `_test_guard_peer.py`（新） | **26 / 26**：peer 错误分类、A 端预验签、包库存、升级窗口抑制假告警、5 个路由形状、鉴权白名单 |
| 既有 A 端回归 10 套 | 492 项全绿 |
| `guardian-b` 既有 2 套 | 50 项全绿 |
| **合计** | **609** |

打包链已端到端联调：`tools/pack.py` 产出的包 → `upgrade.stage_package()` 校验通过，`pkg_sha256` 与打包时打印的一致。

---

## 12. 实现阶段才暴露的四个坑（都不是设计能预见的）

| # | 坑 | 为什么设计时想不到 | 现在的做法 |
|---|---|---|---|
| 1 | **`NoNewPrivileges=yes` 与 `sudo` 互斥** | 设计里写"给 guardb 一条极窄的 sudo 规则"，看起来天衣无缝；但 NNP 会禁止 setuid 提权，sudo 根本拿不到 root —— 两条要求是死对头，真机上 100% 失败，而且**日志里只有一句 permission denied**，很容易被当成"sudoers 写错了"去查一下午 | 改成 `guardb-upgrade.path` 监听 `data/upgrade/request`：guardb 只能写一个文件，写完之后跑什么由 root 侧 unit 决定。这条路比 sudo 更窄（它连"选哪条命令"的能力都没有），也不破坏 NNP |
| 2 | **"重新核 hash"不等于"验证过"** | 第一版让 `apply.py` 拿 staged 里的 manifest 去核文件 hash。问题是 manifest 本身也在 staged 里，攻击者同时改文件与清单里的 sha256 就能全绿通过 —— 这层校验等于不存在 | `stage_package` 把 `manifest.json.sig` 一并暂存；`apply.py` 落地前用 `.trusted/pkg.pub` **重新验签**，再核 hash |
| 3 | **`journal` 是 guardb 可写的，拿它拼路径就是给 root 开后门** | 账本天生"应该"记录路径，用它最自然。但它写在 `data/` 下，而 `data/` 正是 guardb 唯一的可写区 —— 于是 `version="../../etc"`、`removes=["../app.py"]` 就能让 root 读写/删除任意文件 | 路径一律**推导**（`version + job` 拼），并做形状校验 + `dirname realpath` + 拒符号链接；`files/removes` 只接受 `^[A-Za-z0-9_.-]+\.py$` |
| 4 | **验签与使用之间可以用"换文件"打时间差**（TOCTOU） | "先验签、再解析、再复制"三步各自都对，合起来却有窗口：staged 对 guardb 可写，它可以在两次读之间把清单在"合法版"和"改过版"之间来回换，多试几次总能命中 | **一次读取**：读到的字节既拿去验签，也拿去解析，也拿去落盘（`load_and_verify_staged` / `read_verified_file`） |

### 12.1 这四条抽出来的通用规则

1. **安全属性之间会互相冲突**：`NoNewPrivileges` 与"提权执行"天然对立。设计阶段就要把最终 unit 的沙箱指令列出来，逐条问"它会不会让某个机制失效"。
2. **校验的对象必须是"不可被同一权限改写"的东西**。清单和它描述的文件放在同一个可写目录里，等于自己给自己发证书。
3. **凡是从"低权限可写区"读出来的路径，都不能直接交给高权限进程使用**。要用就先推导、再校验形状、最后校验 realpath。
4. **"先验后用"必须变成"验的就是用的那一份"**（同一个 fd / 同一份内存字节）。分两次读，中间就有一扇门。
