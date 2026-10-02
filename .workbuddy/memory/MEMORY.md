# MEMORY.md — A7Z TeslaUSB 项目长期记忆

> Updated: 2026-10-02。**v0.3.1.59 已发布（Release ID 401885529，tag→`df49b4a`），三方核对 + 端到端验签全绿。** 版本历史 / 教训全量见 `docs/`。

---

## 📚 知识在哪
仓库 `docs/`（Divio 四象限）：
- `reference/`：服务清单、路径与配置项、**教训索引（M15~M87）**、TeslaMate 接口与状态字段、版本历史、发布说明规范
- `explanation/`：架构、缓存一致性、USB-Gadget、WiFi/AP 状态机、硬件编解码
- `how-to/`：部署回滚、发布新版本、Bug 修复 12 步、排障

**铁律：改码就改文档；新坑追加教训索引（编号递增 + 关联版本号）。** 教训索引是唯一权威清单，本文件只留高频。

## 🚀 发版（完整 8 步见 `.workbuddy/skills/a7z-release/SKILL.md`）
1. 改码 → `config.py` bump APP_VERSION → 语法 / 回归 → 审查
2. `build_release.py`（白名单打包，从**工作树**）→ M41 空、M33 含 requirements.txt
3. 限定范围 `git commit`（**勿 `git add -A`、勿 `git rm`**——本机 `git rm` 会连父目录删）→ `git push origin master`（**SSH**，Deploy Key `~/.ssh/a7z_github`；HTTPS+PAT 兜底）
4. `ssh-keygen -Y sign -f upgrade_key -n file <tar.gz>`；`sha256sum`
5. `_releaseNN.py`（从上一版复制改造）→ 建 Release + 上传资产（API+PAT，资产走 `uploads.github.com`）
6. 三方核对：tag sha == push HEAD（M61）｜asset digest == 本地 == body（M72）｜body SHA256 行纯文本（M76）

**坑**：M61 `target_commitish=master`｜M76 body SHA256 行禁任何包裹符号｜M75 后置钩子跑在旧进程→关键动作须启动期幂等兜底｜M57 发布说明去 AI 腔、标题用用户可见现象｜**M88 先 `GET /user` 验证 PAT 存活，勿把 push(SSH) 成功当 API 可用**｜签名校验须用 allowed_signers 格式（`printf 'a7z-upgrade %s\n' "$(cat upgrade_key.pub)" > tmp`），**不能直接 `-f upgrade_key.pub`**。

## 📦 版本时间线（全景见 `docs/reference/版本历史.md`）
| 版本 | 日期 | 内容 |
|---|---|---|
| .54 | 09-10 | AP 断开 15s 事件驱动回连(M73)+dnsmasq 自愈+timer 心跳 |
| .55 | 09-14 | 遗留 gadget 服务双 owner 抢 UDC 修复(M74) |
| .56 | 09-14 | 后置钩子自举缺口启动期自愈(M75)+SHA256 行去包裹(M76) |
| .57 | 09-19 | USB 链路守护独立化(M80)+升级标记两路都写(M77)+开机推送(M78) |
| .58 | 09-26 | **并入 .57**：USB 守护 L2.5 平台重绑(`12.usbc2` unbind/bind,M82)+L3 90→150s+持久内核日志(M83)+AP 手动切 WiFi 失效(M81) |
| .59 | 10-02 | **已发布**：AP「关闭」被 2min timer 逆转修复(持久抑制标记 M85)+前端假失败+`_ap_bring_down` 判据错配+RTC 回写+WiFi 持久日志/抑制期提醒(M86/M87) |
- 设备版本检查成功缓存 6h，发版后最长 6h 可见（急用 `systemctl restart teslausb-web`）。

## 🔥 高频教训（全量见 `docs/reference/教训索引.md`）
- **发布/仓库**：M41/M36/M40 完整性；M26/M28 版本检查失败不缓存；M33 包须含 requirements.txt；M35 Windows tar 丢执行位→`chmod +x`；M48/M49 github:443 被阻→SSH 兜底；M60/M72 签名与恢复核实；**M84 升级包 ≠ git 仓库（密钥须 gitignore）**；**M88 发版第 0 步先 `GET /user` 验活 PAT（非 200 即停）——勿把 push(SSH) 成功当 API 凭据可用；401 判据=与无认证基线对比；PAT 是发布链路单点**。
- **设备/网络**：**M53 禁 `systemctl restart NetworkManager`**（毁本机 DNS）→`nmcli device connect`；M34 子进程节流；M32 共享态加锁；M43 升级后确认真重启；M45 timer 随包+幂等自愈；M46 dnsmasq/resolved 抢 53→`port=0`；M65 部署器写 root 目录须 sudo；M68 oneshot 硬件脚本可重入；M71 删白名单条目前查引用方；M69 推送断续三因；M73 AP 回连事件驱动；**M74 一脚本两单元并发抢 UDC；停用遗留单元只 disable 不加 `--now`**；**M80 USB 卡死判据=线在位且 UDC `state=='configured'`；守护 `tick()` 只有真动作才计阶梯**；**M82 控制器级卡死须对平台父设备 `12.usbc2` unbind/bind（L1/L2 无效）；恢复动作须作用根因层而非症状层**；**M83 journald 可能整份丢某 boot system journal→关键取证直采 /dev/kmsg**；M51 时钟偏差全局只缓存 1 key。
- **状态机/前端/通知**：**M85 被后台 timer 逆转的操作须落成持久抑制状态（非内存/时间窗）；操作自身会拆掉其传输链路时前端不得用该请求判成败；辅助信息不得当主操作判据**；**M86 一次性提醒按「标记」去重而非时间窗（跨进程/重启才有效）**；**M87 `import logging` ≠ `logging.handlers`（缺 import → AttributeError 被 `except: pass` 吞 → handler 静默不挂）；`except: pass` 包裹的初始化须干净进程端到端实证；多进程共享日志禁进程内轮转（plain FileHandler + 外部 log_rotator）**；M47 部分成功显式处理；M62 schema 静默漂移→容错+缺键 warning；M63 配额按设计余量；M67 SSE 新字段同步两个摊平点；M21 上传双轨冲突；M22/29/30/31/54/55/23 前端系列。
- **缓存/缩略图**：M52/M58 目录 mtime 被 VFS 冻结→listdir 指纹（概率性）；M59 AI 单目标优化落地前三问；M50 只读尾 64KB；M64/M66 生成即质检；M70 质检按目录语义。

## 🖥️ 平台
- Radxa Cubie A7Z（Allwinner A733, armv8-a, 内核 5.15.147-21-a733），Debian 11 bullseye。Tailscale `100.116.18.42`（SSH 已被 Tailscale 接管），内存 ~959MB（并发转码 ≤2）。
- **部署形态**：`/opt/radxa_data/teslausb` 是**软链** → `/opt/radxa_data/teslausb-v0.3.1.NN`，目录 **0700 root** → radxa 用户 **SFTP 不能直写/直读**；须「SFTP → `/tmp`（radxa 属主）→ `sudo install`」；备份须 `mkdir -p` 子目录。
- 版本系统：`config.py` APP_VERSION + `version_service` + `upgrade_service`（SHA-256 + Ed25519 验签）。
- WiFi/AP：NetworkManager(nmcli)+hostapd+dnsmasq+wpa_supplicant；AIC8800 **单射频**，AP 与 station 不共存（AP 时 wlan0 `managed=no`+停 wpa_supplicant，切回需 10-30s 沉降）。AP 段 192.168.42.10-100，SSID `TeslaUSB-Setup`，wlan0 hwaddr `bc:2a:33:96:a9:be`。回调 `ap_client_event.sh`。
- 运行时 marker（`/var/run/`）：`teslausb-ap-start-time`、`-ap-backoff`、`-ap-had-clients`、`-ap-client-stuck`、**`-ap-manual-off`（v59，JSON `ts/ttl/notified_at`）**、`wifi-smart-switch.state`、`wifi_check_last_trigger`。
- 日志：`/var/log/teslausb-wifi.log`（v59 新增，plain FileHandler，轮转交 `utils/log_rotator.LOG_FILES`——**新增日志文件必须登记**）。
- TeslaMate 自定义服务 `http://100.111.252.121:7777/`：`/login` 换 JWT；`/states` 196 字段含 `locked`/`sentry_mode`/`state`/`is_user_present`/`ui_park_start_str`；仓库只消费 `/msg`+`/api/dashboard`。
- 可复用：`gadget_health.get_gadget_status()`｜`system_monitor.NetworkDetector`+`AlertCooldown`｜`weixin_notifier.PushHealthTracker`/`_exit_node_active()`｜`sei_service.extract_telemetry()`（含 gear_state/speed/GPS，**无 lock**）｜`bg_preview_generator.last_scan_time`。
- GPU：R5 内核硬件编解码不可用（无 /dev/ion、无 V4L2 M2M），弱网转码 = FFmpeg libx264 软转。

## 🚧 进行中
- **✅ v0.3.1.59（10-02 已发布）**：Release **ID 401885529**（tag→`df49b4a`）；assets `teslausb-v0.3.1.59.tar.gz`（114 文件 / 522712 B）+ `.sig`（294 B）。SHA256 `652cd740edcccc76a64d995ad9a6b30f71ba37ae0b064a95968bc54b23771c18`。三方核对全绿 + 远端资产下载重算 sha256 一致 + 端到端验签通过。代码已热部署设备 + 设备端回归 57/57。**遗留**：① 人工验收 S1–S3（设备旁操作，单射频不能远程切 AP）；② 备份 `_backup_v59_20261002_012304` 用户决定**保留**。报告 = `.workbuddy/artifacts/v0.3.1.59-发布记录.md`。
- **✅ 仓库健康（9-26）**：`3fe5a8a` gitignore 私钥；`c3b5390` 补齐 30 个「已部署未提交」文件。遗留：① tag `v0.3.1.58`→`9a821da` 不含同步（不动）；② B 类 ~15 项大改待拍板；③ `config.json` 复现缺口可用 `config.json.example` 补。
- **USB / 内核取证（9-22~9-25，已归档）**：故障为 USB 控制器级 tear-down/re-probe（Type-C PHY 层），非单纯主机侧重枚举；`ep1out` 是后果签名。报告 = `.workbuddy/artifacts/USB故障根因分析-20260924.md`。**待确认**：9-23 17:17 重启是否人工；是否开始 P1（仅 L1 真动作）。
- **L2.5 真机验证**：开 `allow_platform_rebind` 前须跑通 `.workbuddy/artifacts/validate_l25_platform_rebind.sh`。
- **自检守护**：方案见 `.workbuddy/artifacts/自检守护-可行性与设计评估.md`；待定 VPS 入口 / 鉴权、外部 dead-man。
- 仓库根 100+ `_` 临时脚本 & `_logs/` 待归档（需先给清单确认）。
