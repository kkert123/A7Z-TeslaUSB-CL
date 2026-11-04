# MEMORY.md — A7Z TeslaUSB 项目长期记忆

> Updated: 2026-11-05。**当前版本 v0.3.1.69（已发布，tag→`2d1cbf1`，Release ID 434583508）——登录保护（仅验证码 + 腾讯企业邮 + 本地密钥/只读）首次上线，**默认未启用**，升级后行为与 v0.3.1.67 完全一致；同时修掉 v68 的 P0 回归（守卫把 CSRF 放在「是否启用」之前，会让 65 处写操作全 403）。设备尚未升级。**
> 上一版 v0.3.1.66（tag→`d888d82`）修 M106/M107：升级后刷新全部本项目服务、看门狗重启不再复位整机。
> **本文件只留「高频 + 指针」。全量细节在 `docs/`，不在此重复。**

---

## 📚 知识在哪（`docs/` Divio 四象限）
- `reference/`：服务清单、路径与配置项、**教训索引 M15~M107（唯一权威清单；⚠ 已知 M97/M100/M101/M102/M103/M104 各有两个条目——10-05 那批与 10-14 那批撞号，待重编号）**、TeslaMate 接口、版本历史、发布说明规范
- `explanation/`：架构、缓存一致性、USB-Gadget、WiFi/AP 状态机、硬件编解码
- `how-to/`：部署回滚、发布新版本、Bug 修复 12 步、排障

**铁律：改码就改文档；新坑追加教训索引（编号递增 + 关联版本号）。**
**追加新编号前先 grep 最大值**——曾因没查最大值导致 6 处撞号（已把 10-05 那批重编号为 M108~M113）。
**文档里的「设备事实」（有哪些服务/单元、路径、状态）一律以设备查询为准，不照文件名写**——曾把设备上并不存在的 `teslausb-gadget.service` 写进服务清单。

> v0.3.1.63~66 的手册回填见 commit `2bd7106`；复盘见 `.workbuddy/memory/reflections/2026-10-15-v63-v66-deep-dive.md`。

## 🚀 发版 8 步（详见 `.workbuddy/skills/a7z-release/SKILL.md`）
改码 bump `config.py` → 回归 → `build_release.py`（白名单，**从工作树**）→ 限定范围 commit（**禁 `git add -A`、禁 `git rm`**）→ push（**SSH**，Deploy Key `~/.ssh/a7z_github`）→ `ssh-keygen -Y sign` → `_releaseNN.py` 建 Release → **三方核对**（tag sha==HEAD｜asset digest==本地==body｜body SHA256 行纯文本）。

**高频坑**：M61 `target_commitish=master`｜M76 SHA256 行禁包裹符｜M88 **先 `GET /user` 验活 PAT**（勿把 SSH push 成功当 API 可用；PAT 有有效期，10-14 刚失效过一次）｜M91 `/user` 是根端点，**禁复用带 `/repos/{REPO}` 前缀的 helper**｜M92 升级耗时可达数分钟（实测约 **6.5min**），客户端超时≠失败（只认 `progress`+symlink），**禁并发升级**｜M93 比对须 `diff --strip-trailing-cr`（CRLF）｜验签须用 allowed_signers 格式｜签名用 `upgrade_key` 私钥（**M84 密钥须 gitignore**）｜**升级到设备优先 Web 一键**（`POST /api/version/upgrade`，不依赖 SSH）；设备 SSH 只能走 `tailscale ssh`｜**M106 升级后必须重启「全部」本项目服务**（`upgrade_service` 只自愈 `usb-guard`/`kernel-log`）——判据用逐个 `readlink /proc/<pid>/cwd`，**只看版本号/symlink 会被"未重启的旧进程"骗过**；多进程共享状态文件出现**数值倒退** = 多写者指纹｜**M107 禁 `systemctl restart teslausb-watchdog`**（无 SIGTERM magic-close + 启动 30s 不喂狗 > 硬件超时 → 必然整机复位）。

## 🖥️ 平台与部署
- Radxa Cubie A7Z（A733，armv8-a，8 核，**内存仅 959MB**），Debian 11 bullseye，**Python 3.9.2**。Tailscale `100.116.18.42`。
- **实测资源（10-05）**：7 个 Python 进程共 ~215MB RSS，合计 ~14% 单核；**load 0.36~0.42 / 8 核，CPU 71% idle，MemAvailable ~390MB**。→ **Web 层不是性能瓶颈**。
- 部署：`/opt/radxa_data/teslausb` 软链 → `teslausb-v0.3.1.NN`，目录 **0700 root** → radxa **不能 SFTP 直写**；须「SFTP→`/tmp`→`sudo install`」。备份须 `mkdir -p` 子目录。
- 7 个 systemd 服务：`teslausb-web` / `-sentry` / `-bgpreview` / `-usb-guard` / `-watchdog` / `-kernel-log` / `-fan`。另有 wifi-quick/full-check timer。
- WiFi：NM(nmcli)+hostapd+dnsmasq+wpa_supplicant。芯片实为 **AIC8800D80 双频**，**AP+STA 可并发**（10-04 实测）：`iw dev wlan0 interface add ap0 type __ap` → **立即** `nmcli device set <if> managed no` → 改名后按实际名引用 → **必须同信道** → `ip addr`+hostapd+dnsmasq(`--interface=<vif> --bind-interfaces`)+iptables MASQUERADE。AP 段 192.168.42.10-100，SSID `TeslaUSB-Setup`。**禁 `systemctl restart NetworkManager`**（M53，毁本机 DNS）→ 用 `nmcli device connect`。
- 硬件编解码：**R5 核 OMX 封装层有缺陷**（`libOmxVdec.so` 声称 DMABuf 但崩）。**10-05 复核更正**：8-08「libvencoder 不可用」是**误诊**——真机跑通（三坑：**高度须 16 对齐**、漏 `AlreadyUsedInputBuffer()`+memops 初始化、**禁链 `-lvenc_h264`**）。但**VE2 连续用约 10min 会卡死，必须重启整机**（上游 README 亦承认）→ **不做硬编集成**（M97）。弱网转码**维持 FFmpeg libx264 软转**。资产：`.workbuddy/artifacts/refs/a733-venc/`。
- 蓝牙：可用（`aic_btusb`，hci0 UP）。OBEX **手机→A7Z 成功 / A7Z→手机不可行**（Android 不跑 OBEX 服务端）。见 skill `a7z-bluetooth-obex`。
- 关键 marker（`/var/run/`）：`teslausb-ap-*-time/backoff/had-clients/client-stuck/manual-off`、`wifi-smart-switch.state`。日志 `teslausb-wifi.log`（**新增日志须登记 `utils/log_rotator.LOG_FILES`**）。
- **可复用模块**：`gadget_health` / `system_monitor.NetworkDetector`+`AlertCooldown` / `weixin_notifier.PushHealthTracker` / `sei_service.extract_telemetry()` / `bg_preview_generator.last_scan_time`。
- TeslaMate `http://100.111.252.121:7777`，仓库只消费 `/msg` + `/api/dashboard`。

## 🔥 高频教训（分组摘要，全量见 `docs/reference/教训索引.md`）
- **并发/缓存**：多进程共享的状态文件**必须原子写**（`tempfile.mkstemp` + `os.replace`，tmp 名不能用 pid——同进程多线程会撞名），**读必须假设可能读到坏内容**并能隔离留证后自愈（M114）｜「重复推送/重复请求」先看**频率**：从每 23h 一次变每 31s 一次 = 某处"本应命中缓存"失效，不是通知模块的 bug｜通知里的字段是发送方自己拼的（`真实公网IP: 127.0.0.1` 其实是登录请求体里硬编码的 `publicIp`）。
- **排查顺序**：用户点名设备/系统时**先在该现场取证**；断言「不是这台」要「代码零命中 + 端点 404 + 日志零命中 + 进程/端口清单」四证齐全（M115）
- **状态机**：被后台 timer 逆转的操作须落成**持久抑制状态**（M85，非内存/时间窗）｜操作自身拆掉传输链路时前端不得用该请求判成败｜一次性提醒按**标记**去重（M86）｜`import logging` ≠ `import logging.handlers`（M87，缺 import 被 `except: pass` 吞 → handler 静默不挂）｜限流应限「频率」而非「动作本身」（M89）｜「防抖冷却」勿用在「无网恢复」路径（M90）。
- **设备/网络**：USB 卡死判据=线在位且 UDC `state=='configured'`（M80）｜控制器级卡死须对 `12.usbc2` unbind/bind（M82，**恢复动作要作用根因层**）｜一脚本两单元并发抢 UDC（M74）｜journald 可能整份丢某 boot journal→直采 `/dev/kmsg`（M83）｜VFS 冻结目录 mtime → 用 listdir 指纹（M52/M58）。
- **工程**：判据要按真实结构写，写前先跑「只打印不断言」探针｜远程长任务**超时≠失败**，看目标系统实际状态，**禁并发**（共用 `/tmp` 路径互踩）｜删除型改动须三证明（逐字节同 / 保留接线零改动 / 被删子图与外界连线数=0）。

## 🚧 进行中 / 待办
- **✅ v0.3.1.67 已完成**（11-03）：企微「Web登录成功」刷屏（M114）。`.teslamate_token.json` 被 4 进程非原子写坏 → 每 31 秒重登 TeslaMate；修原子写 + 隔离自愈；真机 19:27 后重登 0 次、4 服务 cwd 全 v67、未复位。教训：**A7Z Web 完全无认证**（`/login` 404，`/api/logs/*` 与 `/api/system/reboot`、`/api/version/upgrade*` 匿名可调）——三方案评审见 `docs/explanation/Web访问认证方案评审.md`（A 网络收敛 / B 口令+签名 Cookie / C nginx 反代；建议先 A 再 B），待用户选型。
- **⚠️ 待决策 S1（v69 审查发现）**：未启用登录保护的「首次配置窗口」内，局域网任何终端都能调 `/api/security/*` → 用自己邮箱完成闭环再 `enable` → 把主人锁在门外。**这是「未启用=无保护」设计的直接后果，非编码缺陷**。选项：a 安装令牌（推荐）/ b 缩短配置窗口 / c 接受。详见设计文档 §14.9。
- **⚠️ 实施前提 S2（加 nginx 反代前必读）**：`_client_ip()` 现在用 `request.remote_addr` 且不信任 XFF（**当前正确**）；一旦 Flask 退回 `127.0.0.1:5001` 走 nginx，`remote_addr` 恒为 127.0.0.1 → `is_local_network()` 恒真 → 离线登录/改安全邮箱两道「仅本地」限制全部失效。必须同时改为「只信任来自反代 IP 的 XFF 最后一跳」+ 补 XFF 伪造测试。
- **🚧 停车自检模块（进行中：设计 + B 端骨架，未上机）**：目标=规避停车后系统异常导致哨兵录像失效。A 端本地自检四项（网络/USB/缩略图/微信）+ **B 端僚机 `guardian-b/`**（云端收心跳→判死→告警，补"设备自己发不出告警"的盲区，含 per-channel `last_seen` 双通道判别 + going_offline 预期离线握手 + 外部 dead-man）。**证书定「裸 IP + 自签 + A 侧 pinning」**（零续期、免疫 CA/B SC-081v3 的 200→100→47 天政策漂移；服务端证书 SAN=IP，A 用 IP 直连 + `verify=<ca.crt>`）。设计 `docs/explanation/自检守护-B端VPS设计.md`；代码 `guardian-b/`（**不随 A7Z 升级包分发**）；单测 `tests/test_judge.py` 18/18。**待办：B 端上机部署（VPS `/opt/guard-b/`）+ A 端心跳 sender（尚未动码）**。
- **✅ v0.3.1.63/64 已完成**（10-14）：v63 = Present 守卫 + 失败语义 + 历史去重/裁剪 + 判据收敛（`utils/system_info.detect_cam_mode` 为唯一权威实现），真机验：Present → 409 + 零删除 + 未写历史；Edit 判据 → `edit`。**v64 = 真凶修复**：`usb_guard` 无模式感知 → 把 Edit（gadget 故意解绑、线仍插着）当链路卡死 → 30s 后 L2 `restart` → `remount_all_ro` 打回只读 + 重绑 UDC → 手动清理 4080 条 `[Errno 30]`；修法：`usb_guard` 加 **Edit 模式闸**（读 `/tmp/teslausb_mode`，读不到 fail-open）+ `usb_gadget_init.sh` start/stop 写该标志 + 清理侧把「确认可写」挪到**紧邻删除之前**并用**真实写探针**验收。真机验：切 Edit 后 t+75s 仍 `mode=edit`、UDC 仍"(无)"、`phase=edit_mode/stages=[]`；Edit 下 execute 全链路通（零数据删除验证法：临时关 enabled → 跑 → 还原）。教训 **M100/M101/M102/M103/M104**。
- **✅ v0.3.1.65 已完成**（10-15）：网络卡片两栏流量归属反了 → `utils/traffic_monitor._current_category()` 只看「A7Z 自己的 AP 是否激活」，于是「STA 连着车载热点」（主场景）被算成家庭 WiFi。**改为按当前连接的网络分类**；新增 `utils/ssid_match.py` 作 SSID/连接名匹配唯一权威实现（容忍 `WiFi-` 前缀），`traffic_monitor` 与 `location_detector` 共用（顺带修活后者一直静默失效的「连热点=外出」判定）；`other` 桶页面单列。审查加固：分类移出 `_lock`、探测/配置失败保留上份有效值、`_current_category()` 保证不抛。真机：判据 `hotspot`／旧口径 `home_wifi` 一比一复现；最终 `home_wifi` 冻结、`hotspot` 正常增长。测试 `_test_traffic_category.py` 30/30。教训 **M105/M106/M107**。
- **⛔ 设备 SSH 通道已变（重要）**：22 端口现在是 **Tailscale SSH**（banner `SSH-2.0-Tailscale`），**radxa/radxa 密码认证不可用**（paramiko / `ssh` 均 `Authentication timeout`）；**但 `tailscale ssh radxa@100.116.18.42` 可用**（需 tailnet 已授权，偶发浏览器确认），root 操作照旧 `echo radxa | sudo -S`。→ 文档里的 paramiko SFTP 热部署**作废**；部署优先走 Web 一键升级 `/api/version/upgrade`（不依赖 SSH）。
- **⚠️ 升级到设备优先用「本地上传」**：Web 一键升级的 **URL 下载路径在本网络下不可靠**（10-14 实测连续 stall：ghproxy 首源 300s 超时 + 直连极慢；失败还留下 **stale `/var/run/upgrade_progress.json`**，页面永远显示"升级中"）。可靠做法：`curl -F file=@<包> -F sha256=<hash> .../api/version/upgrade-upload`（走 Tailscale，秒级）。
- **✅ v0.3.1.66 已完成**（10-15）：修掉两个让 v65 修复"失效"的放大器。**M106** = `upgrade_service` 新增 `_refresh_project_services()`（注册进 `startup_self_heal`，按版本幂等 `data/services_refreshed.json`，只重启 active 的服务、失败不写标记）把 sentry/bgpreview/fan/watchdog 刷到当前版本 + `app_helpers` 的统计/日志线程改为显式入口 `start_broadcasters()`（由 `app.py` 调用，模块导入不再起线程）。**M107** = `hardware_watchdog` 加 `install_signal_handlers()`（SIGTERM/SIGINT/SIGHUP → magic close；已决定重启时不 close）+ **先 open+喂狗再进宽限期**（宽限期内按 interval 持续喂狗）。真机：4 服务于 00:31:06 被刷新、7 个进程 cwd 全为 v66、**看门狗被重启而 `uptime -s` 仍是 00:01:02（未复位）**。测试 `_test_service_refresh.py` 23/23。教训 **M106/M107**。
- **⚠️ 待决策（10-15 剩余）**：① 旧遗留 **M104**（`remount_all_ro` 收不回 ro，一行改动）、**M102**（Edit 窗口仅 2 分钟，长清理可能被打断）；② 仓库根 100+ `_` 临时脚本 & `_logs/` 待归档。
- **⚠️ 两条未修遗留（10-14 发现，待决策）**：① **M104**：`start_gadget` 的 `remount_all_ro()` 对已被清成 rw 的 cam 收不回 ro（用 `mount -o ro <dev> <mp>` 而非 `mount -o remount,ro`）→ 切回 Present 后 cam 仍 `rw`（违"Present 只读"不变式）；② **M102 实锤**：**Edit 窗口只有 2 分钟**（`auto_present_service` 倒计时），真实清理 4080 文件/112GB 很可能跑不完 → 到点 `usb_gadget_init.sh start` 中途打断（umount+重绑）→ 建议清理期间取消/推迟倒计时。
- **⚠️ v0.3.1.64 未做**：Edit 下**真实**清理未执行（用户选零数据删除验证法）→ 下次清理注意 2 分钟窗口，或先修 M102。
- **v0.3.1.62 待办**：真机一键升级清漂移（设备侧开关已置 `true`）。M96 已修 quick_check 在并发下每 2min 关 AP。
- **⚠️ 已知未修缺陷**：`app.py` 在 `app.run()` **前同步发开机通知** → 断网时 Web 永不监听 5000 → AP fallback 下 Web 不可达。
- **⚠️ 已知未修（M102）**：手动清理与 `auto_present_service` 的 Edit→Present 自动切换**无互斥**，长清理被切换打断会双写同一 exFAT；建议清理期间持 `/var/run/usb_gadget_init.lock` + 让自动切回**重试**而非当成功。
- 用户手机 S20 FE WiFi MAC `4c:fc:aa:d7:42:d9`；测试环境有陌生设备 `da:69:31:b5:b3:e7` 会干扰「断开即关」验证。
- 仓库根 100+ `_` 临时脚本 & `_logs/` 待归档（需先给清单确认）。设备端 `-v.56/58/59/60` 旧目录可清理。
- **语言迁移可行性调研已完成**（10-05）→ `artifacts/Python改Rust-Go可行性分析-20261005.md`，结论：**不建议整体迁移**，瓶颈不在语言层。
