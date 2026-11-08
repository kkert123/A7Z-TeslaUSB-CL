# MEMORY.md — A7Z TeslaUSB 项目长期记忆

> Updated: 2026-11-08。**当前工作版本 v0.3.1.77（代码+测试+文档完成，双审查已过，`config.py` 已 bump；B 端 v1.0.2 待重新导入）——守护配置 UI + 自检报告推送 + 仪表盘僚机状态。**
> 已发布版本：v0.3.1.76（`ce6f371`，Release 437687920，607933 B / 120 文件）守护 A 端心跳｜v0.3.1.75（`9a3d138`）M122｜v0.3.1.74（`6dbc775`）M121｜v0.3.1.72/73（`5dc358e`）登录空闲注销｜v0.3.1.69（`2d1cbf1`）登录保护｜v0.3.1.70（`a5cd9ec`）M118。
> **设备当前仍跑 v0.3.1.76**（v77 未上机，升级需授权）。
> **本文件只留「高频 + 指针」。全量细节在 `docs/`，不在此重复。**

---

## 📚 知识在哪（`docs/` Divio 四象限）
- `reference/`：服务清单、路径与配置项、**教训索引 M15~M122（唯一权威清单）**、TeslaMate 接口、版本历史
- `explanation/`：架构、缓存一致性、USB-Gadget、WiFi/AP 状态机、硬件编解码、登录鉴权设计 v2
- `how-to/`：部署回滚、发布新版本、Bug 修复 12 步、排障
- `releases/`：每版发布记录（版本/commit/tag/Release ID/sha256/验证/回滚）

**铁律：改码就改文档；新坑追加教训索引（编号递增 + 关联版本号）。追加前先 grep 最大值。**
**文档里的「设备事实」（服务/单元/路径/状态）一律以设备查询为准，不照文件名写。**

## 🚀 发版 8 步（详见 `.workbuddy/skills/a7z-release/SKILL.md`）
bump `config.py` → 回归 → `build_release.py`（白名单，从工作树）→ 限定范围 commit（**禁 `git add -A`、禁 `git rm`**）→ push（SSH，Deploy Key）→ `ssh-keygen -Y sign` → `_releaseNN.py` 建 Release → **三方核对**（tag sha==HEAD｜asset digest==本地==body｜body SHA256 行纯文本）。

**高频坑**：M61 tag 必须指当前 HEAD｜M76 SHA256 行禁包裹符｜M88 **先 `GET /user` 验活 PAT**｜M91 `/user` 是根端点，禁复用带 `/repos/` 前缀的 helper｜M92 升级耗时数分钟，**客户端超时≠失败**（看 symlink/progress），禁并发｜M93 比对用 `diff --strip-trailing-cr`｜M106 升级后**必须重启全部 7 个服务**，判据是逐个 `readlink /proc/<pid>/cwd`（只看版本号会被旧进程骗过）｜M107 watchdog 自 v66 有 magic-close，可安全重启。

## 🖥️ 平台与部署
- Radxa Cubie A7Z（A733，armv8-a，8 核，**内存 959MB**），Debian 11，**Python 3.9.2**，Tailscale `100.116.18.42`。
- 部署 `/opt/radxa_data/teslausb` → `teslausb-v0.3.1.NN`（0700 root）→ radxa 不能直写，须 `sudo`。
- 7 个服务：`teslausb-web/-sentry/-bgpreview/-usb-guard/-watchdog/-kernel-log/-fan`。
- SSH **只能走 `tailscale ssh radxa@100.116.18.42`**（密码认证不可用）；root 用 `echo radxa | sudo -S`。
- **升级到设备**：优先 Web 一键（`/api/version/upgrade`）；但**启用登录保护后该接口会 401**（守卫正常）→ 改走 `base64 -w0 <包> | tailscale ssh ... 'base64 -d > /tmp/x.tar.gz'` + `python3 -c "import upgrade_service as u; u.do_upgrade_from_tarball(...)"` + **手动重启全部服务**（M120）。
- 缩略图统一在 `/opt/radxa_data/shared/thumbnails`，各版本 `static/thumbnails` 是指向它的 symlink（M121）。
- WiFi：AIC8800D80 双频，**AP+STA 可并发**（同信道）；**禁 `systemctl restart NetworkManager`**（M53）。
- 硬件编解码：**不做硬编集成**（VE2 连续约 10min 卡死，须重启整机，M97）；弱网转码用 FFmpeg libx264。
- TeslaMate `http://100.111.252.121:7777`（只用 `/msg` + `/api/dashboard`）。

## 🔥 高频教训（全量见 `docs/reference/教训索引.md`）
- **并发/缓存**：多进程共享的状态文件**必须原子写**（`mkstemp` + `os.replace`，tmp 名不能用 pid），读侧必须假设读到坏内容并能隔离留证后自愈（M114 / **M122 第二次犯**）｜`open(w)+json.dump` 被并发写坏的特征是**断在 8KB 边界**（char 8192/8200）｜**同一次操作的多个入口必须共用同一把锁**（M122）｜前端「执行中」状态会被定时轮询重置 → 必须用跨轮询的标记位而不是只设一次 `disabled`（M122）。
- **性能**：`not os.listdir(dir)` 判断空目录是 **O(n)**；放在「每删一个文件」后面就变 **O(n²)**——用 `next(os.scandir(dir), None) is None`（M122）｜**先量再改**：`listdir(23341 项)=16.91ms`，量出来才敢下结论。
- **排查顺序**：用户点名设备/系统时**先在该现场取证**；断言「不是这台」要四证齐全（M115）｜日志里的**时间戳**往往直接给出答案（本次「开始 10:23:25 / 结束 10:30:00」一步锁定）。
- **状态机**：后台 timer 逆转的操作须落成持久抑制（M85）｜一次性提醒按标记去重（M86）｜`import logging` ≠ `import logging.handlers`（M87）｜限流限「频率」而非「动作」（M89）｜防抖冷却勿用在无网恢复（M90）。
- **设备/网络**：USB 卡死判据=线在位且 UDC `state=='configured'`（M80）｜控制器级卡死对 `12.usbc2` unbind/bind（M82）｜VFS 冻结目录 mtime → 用 listdir 指纹（M52/M58）。
- **工程**：判据按真实结构写，先跑「只打印不断言」探针｜远程长任务超时≠失败，看目标系统状态，禁并发｜删除型改动三证明｜**判定依据本身是待配置项 → 引导死锁**（M118）｜**升级/安装不等于重启**（M120）｜**同类缺陷第二次出现要升级成通用规则**（M114→M122 都是「非原子写」）｜**跨层字段必须断言"最终消费方能拿到"**——后端放 `snapshot.sender`、前端读 `d.sender`，功能静默失效且零报错（M123）｜**清空容器要连带复位所有绑定计数器**，否则重试队列一次耗死后永久停用；混装消息必须分级（M124）。

## 🚧 进行中 / 待办
- **🚧 v0.3.1.77 代码完成、未发版**（11-08）：设置页守护分组（4 bot + 18 参数 + 四项明细）+ 自检报告（跃迁/恢复/每日 20:00 + 预热 300s）+ B 失联通知（401 与超时分开）+ 仪表盘「僚机/自检」两行 + `preview_scan.json` 落盘 + B 端 v1.0.2（`/guard/config` + 告警三态）。测试 536 项全绿。**待办：发版 → 设备升级 → 重新导入 B 包 → 8 条验收。**
- **✅ v0.3.1.75 已完成**（11-07）：M122。清理 6.5 分钟 → 秒级；「清理历史加载失败」修复。新增 `_test_cleanup_m122.py`（本机 56 / 真机 57）。真机：缩略图 23,534→177 个（2.8G→29M），根分区 9.2G→6.5G（**21G 可用**），历史抢救回 49 条。
- **⚠️ 待决策 S1**：未启用登录保护的「首次配置窗口」内，局域网任何终端都能调 `/api/security/*` 接管。选项：a 安装令牌（推荐）/ b 缩短窗口 / c 接受。详见设计文档 §14.9。
- **⚠️ 实施前提 S2**：加 nginx 反代前必读——`_client_ip()` 用 `remote_addr` 且不信任 XFF（当前正确）；一旦走反代，`remote_addr` 恒 127.0.0.1 → 两道「仅本地」限制失效。须改「只信任反代 IP 的 XFF 最后一跳」+ 补伪造测试。
- **🚧 停车自检模块**：A 端（v0.3.1.76 心跳 + v0.3.1.77 报告/配置）与 B 端（v1.0.2，TS-only `100.111.252.121:8899`）均已实现。B 端**只绑 TS、不开公网、不用证书**（v1 简化）。守护 key **双持有**：A `config/sentry.json` 的 `wecom_guard_webhook_key` + B `data/runtime.json`（保存时 A→B 下发，失败只提示不回滚）。设计见 `docs/explanation/自检守护-B端VPS设计.md` 与 `守护配置UI与仪表盘状态-设计建议.md`。
- **⚠️ 未修遗留**：M104（`remount_all_ro` 收不回 ro）｜M102（Edit 窗口仅 2 分钟，长清理会被打断）｜`app.py` 在 `app.run()` 前同步发开机通知 → 断网时 Web 不监听。
- **⚠️ 历史遗留（10-15）**：仓库根 100+ `_` 临时脚本 & `_logs/` 待归档（需先给清单确认）。
- **语言迁移**：结论**不建议整体迁移**（瓶颈不在语言层），见 `artifacts/Python改Rust-Go可行性分析-20261005.md`。
- 用户手机 S20 FE WiFi MAC `4c:fc:aa:d7:42:d9`；A7Z `wlan0` MAC `bc:2a:33:96:a9:be`。
