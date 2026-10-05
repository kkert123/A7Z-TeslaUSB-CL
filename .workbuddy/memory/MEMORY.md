# MEMORY.md — A7Z TeslaUSB 项目长期记忆

> Updated: 2026-10-03。**v0.3.1.61 已发布（Release ID 402285182，tag→`42a166a`），三方核对 + 端到端验签全绿；已真机一键升级至 0.3.1.61，L2.5 保存往返验证通过。** 版本历史 / 教训全量见 `docs/`。

---

## 📚 知识在哪
仓库 `docs/`（Divio 四象限）：
- `reference/`：服务清单、路径与配置项、**教训索引（M15~M93）**、TeslaMate 接口与状态字段、版本历史、发布说明规范
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

**坑**：M61 `target_commitish=master`｜M76 body SHA256 行禁任何包裹符号｜M75 后置钩子跑在旧进程→关键动作须启动期幂等兜底｜M57 发布说明去 AI 腔、标题用用户可见现象｜**M88 先 `GET /user` 验证 PAT 存活，勿把 push(SSH) 成功当 API 可用**｜**M91 `GET /user` 是根端点，勿复用带 `/repos/{REPO}` 前缀的 `api()` helper（拼出不存在路径 → 404 被误判为「PAT 失效」；GitHub 对坏端点与坏凭据都回 404，须打印实际 URL 才能区分）**｜**M92 一键升级耗时可达数分钟（`ghproxy.net` 镜像 stall），客户端超时/空响应 ≠ 升级失败——只认 `progress` + symlink/APP_VERSION；**禁并发升级**（共用 `/tmp/upgrade-v<ver>.tar.gz` 互踩 → 假性「SHA-256 失败：文件不存在」）**｜**M93 比对设备/打包产物与 git blob 必须忽略 CRLF（用 `diff --strip-trailing-cr`），否则整文件全红被误判为「文件漂移」**｜签名校验须用 allowed_signers 格式（`printf 'a7z-upgrade %s\n' "$(cat upgrade_key.pub)" > tmp`），**不能直接 `-f upgrade_key.pub`**。

## 📦 版本时间线（全景见 `docs/reference/版本历史.md`）
| 版本 | 日期 | 内容 |
|---|---|---|
| .54 | 09-10 | AP 断开 15s 事件驱动回连(M73)+dnsmasq 自愈+timer 心跳 |
| .55 | 09-14 | 遗留 gadget 服务双 owner 抢 UDC 修复(M74) |
| .56 | 09-14 | 后置钩子自举缺口启动期自愈(M75)+SHA256 行去包裹(M76) |
| .57 | 09-19 | USB 链路守护独立化(M80)+升级标记两路都写(M77)+开机推送(M78) |
| .58 | 09-26 | **并入 .57**：USB 守护 L2.5 平台重绑(`12.usbc2` unbind/bind,M82)+L3 90→150s+持久内核日志(M83)+AP 手动切 WiFi 失效(M81) |
| .59 | 10-02 | **已发布**：AP「关闭」被 2min timer 逆转修复(持久抑制标记 M85)+前端假失败+`_ap_bring_down` 判据错配+RTC 回写+WiFi 持久日志/抑制期提醒(M86/M87) |
| .60 | 10-03 | **已发布**：AP 自动开启后不回连 WiFi 修复——「全量扫描降频」与「恢复探测静默」解耦(`_ap_quiet_period_elapsed` 5min/3min，M89)+恢复路径绕切换冷却(`_switch_to(force=True)`，M90)+WiFi 日志接入页面 |
| .61 | 10-03 | **已发布**：USB 链路守护从「推送与报警」迁入「USB 模式」+ 独立保存端点/按钮（修复「启用 L2.5 平台重绑」勾选保存不上）+保存回读校验；已真机一键升级至 0.3.1.61 验证通过(messons M92/M93) |
- 设备版本检查成功缓存 6h，发版后最长 6h 可见（急用 `systemctl restart teslausb-web`）。

## 🔥 高频教训（全量见 `docs/reference/教训索引.md`）
- **发布/仓库**：M41/M36/M40 完整性；M26/M28 版本检查失败不缓存；M33 包须含 requirements.txt；M35 Windows tar 丢执行位→`chmod +x`；M48/M49 github:443 被阻→SSH 兜底；M60/M72 签名与恢复核实；**M84 升级包 ≠ git 仓库（密钥须 gitignore）**；**M88 发版第 0 步先 `GET /user` 验活 PAT（非 200 即停）——勿把 push(SSH) 成功当 API 凭据可用；401 判据=与无认证基线对比；PAT 是发布链路单点**；**M91 守卫/断言自身也是代码：`/user` 为根端点不得复用带 `/repos/{REPO}` 前缀的 helper（否则 404 被误判为 PAT 失效）；排查「凭据失效」必打印命中的绝对 URL**；**M92 一键升级耗时可达数分钟（镜像 stall）——客户端超时/空响应 ≠ 失败，只认 `progress`+symlink/APP_VERSION，且**禁并发升级**（共用 `/tmp` 包路径会互踩）**；**M93 比对设备/打包产物与 git blob 须忽略 CRLF（`diff --strip-trailing-cr`），否则整文件全红误判为漂移**。
- **设备/网络**：**M53 禁 `systemctl restart NetworkManager`**（毁本机 DNS）→`nmcli device connect`；M34 子进程节流；M32 共享态加锁；M43 升级后确认真重启；M45 timer 随包+幂等自愈；M46 dnsmasq/resolved 抢 53→`port=0`；M65 部署器写 root 目录须 sudo；M68 oneshot 硬件脚本可重入；M71 删白名单条目前查引用方；M69 推送断续三因；M73 AP 回连事件驱动；**M74 一脚本两单元并发抢 UDC；停用遗留单元只 disable 不加 `--now`**；**M80 USB 卡死判据=线在位且 UDC `state=='configured'`；守护 `tick()` 只有真动作才计阶梯**；**M82 控制器级卡死须对平台父设备 `12.usbc2` unbind/bind（L1/L2 无效）；恢复动作须作用根因层而非症状层**；**M83 journald 可能整份丢某 boot system journal→关键取证直采 /dev/kmsg**；M51 时钟偏差全局只缓存 1 key；**M89 「全量扫描降频」与「恢复探测静默」不能共用一条一刀切时间窗（v31 的 15min gate 把全量检测与让出探测双双堵死→零回连）；限流应限「频率」而非「动作本身」**；**M90 「防抖冷却」用在「无网恢复」路径会否决唯一可连候选→`_switch_to(force=True)` 仅在恢复路径绕过，前台「切更优」仍守冷却**。
- **状态机/前端/通知**：**M85 被后台 timer 逆转的操作须落成持久抑制状态（非内存/时间窗）；操作自身会拆掉其传输链路时前端不得用该请求判成败；辅助信息不得当主操作判据**；**M86 一次性提醒按「标记」去重而非时间窗（跨进程/重启才有效）**；**M87 `import logging` ≠ `logging.handlers`（缺 import → AttributeError 被 `except: pass` 吞 → handler 静默不挂）；`except: pass` 包裹的初始化须干净进程端到端实证；多进程共享日志禁进程内轮转（plain FileHandler + 外部 log_rotator）**；M47 部分成功显式处理；M62 schema 静默漂移→容错+缺键 warning；M63 配额按设计余量；M67 SSE 新字段同步两个摊平点；M21 上传双轨冲突；M22/29/30/31/54/55/23 前端系列。
- **缓存/缩略图**：M52/M58 目录 mtime 被 VFS 冻结→listdir 指纹（概率性）；M59 AI 单目标优化落地前三问；M50 只读尾 64KB；M64/M66 生成即质检；M70 质检按目录语义。

## 🖥️ 平台
- Radxa Cubie A7Z（Allwinner A733, armv8-a, 内核 5.15.147-21-a733），Debian 11 bullseye。Tailscale `100.116.18.42`（SSH 已被 Tailscale 接管），内存 ~959MB（并发转码 ≤2）。
- **部署形态**：`/opt/radxa_data/teslausb` 是**软链** → `/opt/radxa_data/teslausb-v0.3.1.NN`，目录 **0700 root** → radxa 用户 **SFTP 不能直写/直读**；须「SFTP → `/tmp`（radxa 属主）→ `sudo install`」；备份须 `mkdir -p` 子目录。
- 版本系统：`config.py` APP_VERSION + `version_service` + `upgrade_service`（SHA-256 + Ed25519 验签）。
- **蓝牙（10-04 实测）**：A7Z **有蓝牙且已可用**——AIC8800 组合芯片，BT 走 **USB**（驱动 `aic_btusb`），`hci0` UP RUNNING，BD ADD `BC:2A:33:96:A5:D3`；BlueZ 5.55 已装且 `bluetooth.service` 运行中。**BLE 扫描可用**（`btmgmt find` / `bluetoothctl scan on`；经典 `hcitool lescan` 在 AIC8800 报 IO 错，弃用）。控制器支持 LE+BR/EDR、advertising 16 实例。**OBEX 文件传输**：原缺 `obexd`；装 `bluez-obexd`（bullseye 安全池 EOL→须锁 main 源 `=5.55-3.1+deb11u1`）+ `loginctl enable-linger`（否则 user service 随 SSH 断被杀）+ 起 `obexd -r <dir> -a` → **手机(Android)→A7Z 传文件实测成功**（`bluetoothctl paired-devices` 有 S20 FE；文件落 `~/bt_received`）。**A7Z→手机不可行**（Android 不跑 OBEX 服务端）；iPhone 双向都不支持。方法见 skill `.workbuddy/skills/a7z-bluetooth-obex/`。
- WiFi/AP：NetworkManager(nmcli)+hostapd+dnsmasq+wpa_supplicant；AIC8800 **单射频**，**当前实现**把 AP 与 station 做成互斥（AP 时 wlan0 `managed=no`+停 wpa_supplicant，切回需 10-30s 沉降）。**✅ 10-04 实测确认 AP+STA 可并发**：芯片实为 **AIC8800D80（双频）**，驱动 `aic8800_fdrv` v6.4.3.0；`iw phy` 声明 `#{ managed } <= 1, #{ AP } <= 1, total <= 4, #channels <= 3`。**实测方法**（真机通过）：① `iw dev wlan0 interface add ap0 type __ap`（phy 支持第二 vif，**wlan0 STA 全程不掉**）；② **立刻** `nmcli device set <if> managed no`（否则 NM 抢管、把 vif 打回 managed）③ `iw dev <if> set type __ap`；④ udev 会把 vif 改名为 `wlx<mac>`（须以实际名引用）；⑤ AP 必**同信道**（实测 STA ch1/2412MHz 时 AP 也 ch1）；⑥ `ip addr add 192.168.42.1/24` + hostapd(ctrl_interface) + dnsmasq(`--interface=<vif> --bind-interfaces --conf-file=/dev/null`) + `iptables -t nat -A POSTROUTING -s 192.168.42.0/24 -o wlan0 -j MASQUERADE`。实测 hostapd `AP-ENABLED`、dnsmasq 监听 `192.168.42.1:53/67`、`wlan0` 仍连 C12345、ping 网关 0% 丢包。**注意改动点**：现有 `hostapd.conf`/iptables 规则把 `interface=wlan0`/`-i wlan0` 写死；并发改造须改为 AP vif 名（而非 wlan0）。AP 段 192.168.42.10-100，SSID `TeslaUSB-Setup`，wlan0 hwaddr `bc:2a:33:96:a9:be`。回调 `ap_client_event.sh`。
- 运行时 marker（`/var/run/`）：`teslausb-ap-start-time`、`-ap-backoff`、`-ap-had-clients`、`-ap-client-stuck`、**`-ap-manual-off`（v59，JSON `ts/ttl/notified_at`）**、`wifi-smart-switch.state`、`wifi_check_last_trigger`。
- 日志：`/var/log/teslausb-wifi.log`（v59 新增，plain FileHandler，轮转交 `utils/log_rotator.LOG_FILES`——**新增日志文件必须登记**）。
- TeslaMate 自定义服务 `http://100.111.252.121:7777/`：`/login` 换 JWT；`/states` 196 字段含 `locked`/`sentry_mode`/`state`/`is_user_present`/`ui_park_start_str`；仓库只消费 `/msg`+`/api/dashboard`。
- 可复用：`gadget_health.get_gadget_status()`｜`system_monitor.NetworkDetector`+`AlertCooldown`｜`weixin_notifier.PushHealthTracker`/`_exit_node_active()`｜`sei_service.extract_telemetry()`（含 gear_state/speed/GPS，**无 lock**）｜`bg_preview_generator.last_scan_time`。
- GPU：R5 内核硬件编解码不可用（无 /dev/ion、无 V4L2 M2M），弱网转码 = FFmpeg libx264 软转。

## 🚧 进行中
- **🔧 AP+STA 并发改造 M1+M2+M3+M4+M94/M95 全部完成 + 真机验证通过（10-04/05，未发版，已热部署设备）**：开关 `ap_sta_concurrent`（`config.AP_STA_CONCURRENT_DEFAULT=False`，**设备当前 true**；UI `wifi.html` 可切换）。**代码**：`wifi_service.py`（`AP_VIF` + `_use_concurrent`/`_ap_vif_add·del`/`_sta_link_freq`·`_sta_current_channel`/`_channel_to_freq`/`_ap_vif_current_channel`/`_hostapd_cli_chan_switch`/`_sync_ap_channel`/独立 hostapd·dnsmasq/`_ap_concurrent_iptables`/`_ap_bring_up_concurrent`/`_ap_bring_down_concurrent`/`_ap_started_manually`/`_remote_lock_risk`/`_ap_start_client_monitor_vif`/`_yield_ap` 并发分支；各 AP 函数加并发分支）+ `config.py` + `system_monitor.py` + `app.py`（M95）+ `routes/wifi_routes.py`（force + 开关）+ `templates/wifi.html`（开关）+ `ap_client_event.sh`（ifname 化）。**真机全通过**：M94 拒绝 legacy 自锁开 AP；并发起停 ap0+STA 同在线、STA 零扰动、NAT 下发/清理；M3 信道跟随（ap0 漂 ch6→CSA 回 ch1）；B1（fallback+无客户端→关）/B2（有客户端→不关不踢，断开→立即关）/B3（提醒计时）；**M4 事件回调 ifname 化**（确定性驱动验证：断开→15s→关 ap0）。**回归**：v59 58/58 + v60 21/21 + v61 26/26 + v62 **94/94**。**真机暴露并修**：quick_check「网络正常」分支无条件 `_ap_ensure_down()` 在并发下每2min关AP（**M96**）。**⚠️ 测试隔离坑**：timer 有**两个**自愈源（`wifi_service.ensure_smart_switch_timers` + **`sentry_watchdog.py` 也调它**）→ `systemctl stop` 会被拉回，隔离须临时移走 `services/*.timer`。**遗留**：热部署漂移待发版清除；**未发版**。文档=`artifacts/AP+STA并发改造-{设计方案,M1M2改动清单}-20261004.md`+`…代码审查报告-20261005.md`；skill=`.workbuddy/skills/a7z-apsta-concurrency/`。
- **📱 用户手机（Samsung S20 FE）WiFi MAC `4c:fc:aa:d7:42:d9`**（10-05 连 ap0 实测，主机名 Tesla）。**注意**：10-05 测试环境另有陌生设备 `da:69:31:b5:b3:e7` 也连过 ap0——多客户端会干扰"断开即关"测试。
- **⚠️ 10-04/05 事故 → 教训 M94/M95**：M2 真机测试中**未察觉并发开关未生效**（写 root 属主 `ap_config.json` 静默失败）即起 AP → 走 legacy 释放 wlan0 → **Tailscale 失联 ~45min**（自愈退避循环恢复）。另发现 **`app.py` 在 `app.run()` 前同步发开机通知 → 断网时 Web 永不监听 5000 → AP fallback 下 Web 不可达**（既有缺陷，待修）。**新码已热部署设备（漂移待发版清除，M84）**。
- **✅ v0.3.1.61（10-03 已发布）**：Release **ID 402285182**（tag→`42a166a`）；assets `teslausb-v0.3.1.61.tar.gz`（114 文件 / 524624 B）+ `.sig`（294 B）。SHA256 `5ced25c28c08a40b4180b7f76d8a5f577a30a713b2a19d002e4341ec69887b05`。修「USB 链路守护迁入『USB 模式』+ 独立端点/按钮」（真因 = UI 缺就近保存入口 + `push-config` 无法单独存 `usb_guard`，**非后端缺陷**）。回归：v59 **57/57** + v60 **21/21** + v61 **26/26**。**真机验证**：经设备一键升级升至 0.3.1.61（symlink→`-v0.3.1.61`，`has_update=false`）；`usb-guard-config` **200**、`push-config` 不再含 `usb_guard`、页面块已在 `grp-usb`、保存往返 true→GET true→落盘→还原 false 全通过。**波折**：① Web 一键升级客户端空响应实为**成功**（镜像 stall 耗时 ~7min 超客户端超时，**M92**）；② 设备文件 vs git blob 整文件全红实为 CRLF（**M93**）。报告 = `.workbuddy/artifacts/v0.3.1.61-发布记录.md`。**遗留**：真机 UI 点击级验收（可选）；设备端旧版本目录 `-v.56/58/59/60` 可清理。
- **✅ v0.3.1.60（10-03 已发布）**：Release **ID 402250062**（tag→`405b4b0`）；assets `teslausb-v0.3.1.60.tar.gz`（114 文件 / 523939 B）+ `.sig`（294 B）。SHA256 `1369e3f7369bd256f11f8b18b09d8c142dc79eb53fb545b5f957407f4bd2a3b0`。三方核对全绿 + 远端资产下载重算 sha256 逐字节一致 + 端到端验签通过。修「AP 自动开启后不回连 WiFi」（M89 静默期解耦 5min/3min + M90 恢复路径绕冷却 `force=True`）+ WiFi 日志接入页面。回归：v59 **57/57** 无回归 + v60 **21/21**。**发版波折**：`_release60.py` 首跑报「PAT 失效 404」实为脚本 URL 拼接 bug（`/user` 误加 `/repos/{REPO}` 前缀）→ 同 token 直打 `/user` 为 200，教训 **M91**。**遗留**：① 真机复现验证（设备旁操作，单射频远程切 AP 会自锁）；② 设备已于 10-03 一键升级至 0.3.1.61（原热部署态版本漂移已清除）。报告 = `.workbuddy/artifacts/v0.3.1.60-发布记录.md`。
- **✅ v0.3.1.59（10-02 已发布）**：Release **ID 401885529**（tag→`df49b4a`）；assets `teslausb-v0.3.1.59.tar.gz`（114 文件 / 522712 B）+ `.sig`（294 B）。SHA256 `652cd740edcccc76a64d995ad9a6b30f71ba37ae0b064a95968bc54b23771c18`。三方核对全绿 + 远端资产下载重算 sha256 一致 + 端到端验签通过。代码已热部署设备 + 设备端回归 57/57。**遗留**：① 人工验收 S1–S3（设备旁操作，单射频不能远程切 AP）；② 备份 `_backup_v59_20261002_012304` 用户决定**保留**。报告 = `.workbuddy/artifacts/v0.3.1.59-发布记录.md`。
- **✅ 仓库健康（9-26）**：`3fe5a8a` gitignore 私钥；`c3b5390` 补齐 30 个「已部署未提交」文件。遗留：① tag `v0.3.1.58`→`9a821da` 不含同步（不动）；② B 类 ~15 项大改待拍板；③ `config.json` 复现缺口可用 `config.json.example` 补。
- **USB / 内核取证（9-22~9-25，已归档）**：故障为 USB 控制器级 tear-down/re-probe（Type-C PHY 层），非单纯主机侧重枚举；`ep1out` 是后果签名。报告 = `.workbuddy/artifacts/USB故障根因分析-20260924.md`。**待确认**：9-23 17:17 重启是否人工；是否开始 P1（仅 L1 真动作）。
- **L2.5 真机验证**：开 `allow_platform_rebind` 前须跑通 `.workbuddy/artifacts/validate_l25_platform_rebind.sh`。
- **自检守护**：方案见 `.workbuddy/artifacts/自检守护-可行性与设计评估.md`；待定 VPS 入口 / 鉴权、外部 dead-man。
- 仓库根 100+ `_` 临时脚本 & `_logs/` 待归档（需先给清单确认）。
