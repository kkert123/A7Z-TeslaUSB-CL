# A7Z TeslaUSB 哨兵云端备份系统

> 基于 Raspberry Pi / Radxa 的 TeslaUSB 改造项目：自动抓取特斯拉哨兵模式（Sentry）clips，
> 生成缩略图预览，通过企业微信推送通知，并可选地上传到 NAS / 云端存储。

> ⚠️ **安全提示**：本项目包含密钥（企业微信 webhook key 等）。真实配置文件 **不会** 进入本仓库
> （已被 `.gitignore` 忽略）。请只提交 `*.example.json` 模板，切勿把 `config/sentry.json`、
> `weixin_config.json`、`config.json` 提交到任何公开仓库。

---

## 一、项目简介

**A7Z TeslaUSB** 是一套运行在嵌入式设备（Raspberry Pi / Radxa 等）上的特斯拉哨兵视频
管理与云端备份系统。它脱胎于社区经典的 [TeslaUSB](https://github.com/mphacker/TeslaUSB) 思路，
在其「USB Gadget 让车机自动识别 U 盘」的基础上，叠加了：

- **可视化 Web 管理界面**（Flask + 实时 SSE 仪表盘）
- **缩略图 / GIF 预览**自动生成，无需手动翻看视频
- **企业微信（WeCom）推送**：哨兵事件、系统状态、开机通知
- **NAS / 云存储（rclone）归档**：把哨兵片段备份到 14 类远端（SMB/SFTP/FTP/WebDAV/对象存储等）
- **车载自定义功能**：车牌、车身涂装、Boombox 音效、灯光秀（LightShow）、锁车提示音
- **远程访问**：通过 Tailscale 在外网访问设备管理界面

### 核心价值

| 痛点 | 本项目的解决方式 |
|------|------------------|
| 哨兵视频散落在车机，需手动插拔 U 盘 | USB Gadget 让设备被车机识别为 U 盘，自动同步 |
| 回看哨兵事件要一段段翻视频 | 自动生成缩略图 + GIF + 事件列表，Web 一目了然 |
| 出事才发现没通知 | 哨兵触发即推企业微信，含现场缩略图 |
| 本地盘坏了视频全丢 | 可配置 NAS / 云归档，自动保留副本 |
| 想给车加点个性化又嫌麻烦 | Web 界面上传车牌/涂装/音效/灯光秀，车机开机即生效 |

---

## 二、功能特性

| 功能 | 说明 | 主要模块 |
|------|------|----------|
| 🎥 **哨兵 clips 扫描与预览** | 自动扫描 TeslaCam 分区，生成缩略图、GIF，Web 端事件化展示 | `video_service.py` / `preview_generator.py` / `bg_preview_generator.py` / `gif_service.py` / `video_preview.py` |
| 🔔 **企业微信推送** | 哨兵事件、系统状态、开机自检通知，支持重试队列 | `weixin_notifier.py` / `sentry_service.py` / `boot_notify.py` / `sentry_notify_queue.py` |
| 📍 **离家 / 到家识别** | 通过 WiFi SSID / 热点切换判断车辆位置，触发不同动作 | `wifi_service.py` / `location_detector.py` / `services/wifi_smart_switch.sh` |
| ☁️ **NAS / 云归档** | rclone 驱动的 14 类远端备份（SMB/SFTP/FTP/WebDAV/对象存储…），OAuth 授权 | `cloud_rclone_service.py` / `cloud_archive_service.py` / `cloud_oauth_service.py` / `sync_service.py` / `upload_scheduler.py` |
| 🔌 **USB Gadget 模式** | 设备模拟成 U 盘供车机读取，自动整理哨兵内容 | `services/usb_gadget_init.sh` / `present_usb.sh` / `edit_usb.sh` / `teslausb-gadgetd.py` |
| 🚗 **车载自定义** | 车牌 PNG、车身涂装、Boombox 音效、LightShow 灯光秀、锁车提示音、媒体中心 | `license_plate_service.py` / `wrap_service.py` / `boombox_service.py` / `lightshow_service.py` / `templates/media.html` / `staging_service.py` |
| ▶️ **视频播放器** | 内置 TeslaCam 视频播放 / 下载 / SEI 遥测解析；集成 TDashcam Studio 前端 | `routes/video_routes.py` / `sei_service.py` / `/tdashcam/` 静态服务 |
| 📊 **系统监控仪表盘** | CPU/GPU/温度/磁盘/NVMe 健康/网络流量实时展示（SSE 推送） | `system_monitor.py` / `utils/hardware_stats.py` / `app_state.py` |
| 🧹 **自动清理** | 按文件夹 age/size/count 策略自动回收空间 | `auto_cleanup.py` |
| 🛡️ **硬件看门狗** | 设备异常自恢复、Gadget 健康检查 | `hardware_watchdog.py` / `gadget_health.py` |
| 🌐 **移动端适配** | 响应式 + drawer 导航，手机可管理 | `templates/base.html` / `static/style.css` |
| 🔗 **远程访问** | 安装脚本集成 Tailscale 一键部署 | `install.sh` |

> 注：以上均为本仓库中**已实现**的功能；尚未完成的部分见「六、开发计划与进度」。

---

## 三、技术架构

### 3.1 整体分层

```
                        ┌─────────────────────────┐
                        │   app.py  (Flask 入口)   │  ← 仅 ~95 行：创建实例 + 注册蓝图 + 启后台线程
                        └────────────┬────────────┘
                                     │ register_blueprint()
            ┌────────────────────────┼────────────────────────┐
            │                        │                         │
     ┌──────▼──────┐         ┌───────▼────────┐         ┌──────▼───────┐
     │  routes/    │         │  app_state.py  │         │   utils/     │
     │ (Blueprint  │────────▶│  (全局状态单例) │◀────────│ (工具/解析)  │
     │  路由层)    │         └────────────────┘         └──────┬───────┘
     └──────┬──────┘                                            │
            │ import                                           │ import
            ▼                                                  ▼
     ┌──────────────────────────────────────────────────────────────┐
     │                    已有 service 层（业务逻辑，不改动）           │
     │  video / wifi / sync / cloud / media / sentry / sei / ...      │
     └──────────────────────────────────────────────────────────────┘
```

- **路由层 `routes/`**：11 个 Blueprint，每个功能域一个文件，只做「请求 → 参数 → 调 Service → 响应」
- **状态层 `app_state.py`**：集中管理原本散落的全局变量（缓存、SSE 订阅者、磁盘 I/O 等），单例
- **工具层 `utils/`**：硬件采集、SEI 解析、缩略图、SSE 广播等无 Flask 依赖的纯函数
- **业务层 `*_service.py`**：核心业务逻辑，模块化拆分时内部实现保持不变

> 本次「模块化」将原本 5463 行的 `app.py` 瘦身到约 95 行（见开发计划与进度）。

### 3.2 运行时组件

| 类别 | 说明 |
|------|------|
| **Web 服务** | Flask（开发服务器 `app.run`，自托管够用；生产可加 gunicorn + 反代） |
| **后台任务** | Python 后台线程：上传调度、系统监控、开机通知、预览队列消费 |
| **系统服务** | `services/` 下 21 个 systemd `*.service` / `*.timer` 单元，覆盖 Web / 哨兵 / 同步 / fsck / 看门狗 / 灯光秀 / 日志轮转等 |
| **设备脚本** | Bash 脚本（`services/`）：USB Gadget 初始化、U 盘内容整理、WiFi 切换、I/O 调优、磁盘巡检、远端同步 |
| **硬件加速** | `cedar_composer/`（C 源码）：利用 Radxa Cedar VPU 做 2×2 视频合成（灯光秀 / 画中画） |

### 3.3 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.8+ / Flask / 原生 SSE（Server-Sent Events） |
| 前端 | Jinja2 模板 + 原生 JS + SSE 实时推送（移动端响应式） |
| 媒体处理 | `ffmpeg`（缩略图 / GIF / 视频裁剪）、Playwright（可选，缩略图截图） |
| 远端存储 | `rclone`（14 类远端：SMB/SFTP/FTP/WebDAV/对象存储…） |
| 部署 | systemd + `deploy_manager.py`（SSH 推送部署）+ Tailscale（远程访问） |
| 存储 | M.2 NVMe 分区：`teslacam` / `music` / `boombox` / `lightshow`（路径可配，见下文） |

### 3.4 目录结构

```
app.py                   Flask 主入口（~95 行）
app_state.py             全局共享状态单例
config.py / config_manager.py   配置加载（含 paths.json 覆盖层）
routes/                  Flask 蓝图（页面与 API，11 个）
utils/                   通用工具（硬件采集/SEI解析/SSE广播…）
templates/ static/       前端页面与资源
services/                设备基础脚本 + systemd 服务单元（部署到设备的 /opt/radxa_data）
*_service.py             各功能模块（sentry / cloud / weixin / wifi / media ...）
cedar_composer/          灯光秀 2×2 视频合成（C 源码，Cedar VPU 加速）
config/                  配置目录（*.example.json 为模板，*.json 为本地真实配置，被 gitignore）
docs/                    使用文档
```

---

## 四、安装与使用

### 4.1 环境要求

- Python 3.8+
- **ffmpeg**（必需）：缩略图 / 视频裁剪 / gif 生成依赖，安装脚本会检测并可选自动安装
- （可选）Node.js + npm：用于 Playwright 缩略图预览生成
- （可选）rclone：用于 NAS / 云存储上传
- （可选）systemd：用于把 `services/` 下的单元部署为常驻服务（安装脚本可自动完成）
- （可选）Tailscale：用于外网远程访问

### 4.2 快速开始

```bash
git clone <你的仓库地址>
cd A7Z-TeslaUSB-CL-publish
chmod +x install.sh
./install.sh
```

脚本会依次：

1. 安装 Python 依赖（到仓库内 `venv`）
2. 从模板生成 `config/sentry.json` 与 `weixin_config.json`
3. **交互询问**各类型文件的存储路径（TeslaCam 视频 / music / boombox / lightshow，默认指向 M.2 挂载点），写入 `config/paths.json`
4. 检测系统依赖（**ffmpeg** / **rclone**，缺失可自动安装）
5. （可选）安装 Playwright
6. （可选）安装 Tailscale
7. （可选）**部署并启用 systemd 服务**
8. 末尾跑一次导入冒烟测试（`import config` + 编译 `app.py`）

> ⚠️ 安装脚本面向 **Linux 目标机**（树莓派 / Radxa）运行，需要 root 权限完成挂载、服务部署等动作。
> 非 root 运行时脚本会给出提示。

### 4.3 配置

安装后请编辑两个本地配置文件，填入你自己的值：

- `config/sentry.json`
  - `wecom_status_webhook_key` / `wecom_sentry_webhook_key`：企业微信群机器人 webhook key
  - `teslamate_url` / `teslamate_password`：TeslaMate 地址（可选）
  - `nas_base_path`：NAS 挂载路径（可选）
- `weixin_config.json`（仓库根目录）
  - `weixin.sentry.webhook_url` / `weixin.sentry.secret`：哨兵事件推送机器人
  - `weixin.status.webhook_url` / `weixin.status.secret`：系统状态通知机器人

获取 webhook key：企业微信 → 群聊 → 添加群机器人 → 复制 webhook 地址中的 `key=xxx`。
部分接入方式还需填写 `secret`（见示例文件中的 `YOUR_*_SECRET` 占位）。

> **运行时真正读取的配置文件**：本仓库当前版本中，Web 应用（`app.py`）实际只读取
> `config/sentry.json` 与 `weixin_config.json` 两个文件（`config.py` / `config_manager.py` 中
> 定义的 `config.json` / `/data/teslausb-web.json` 在本导出里**并未被 app 启动加载**，属于
> 休眠配置体系）。因此首次运行**只需**按上面填好这两个文件即可，无需额外准备 `config.json`。
> 若你后续接入了使用 `ConfigManager` 的模块，再按其要求补全对应配置。

### 4.4 存储路径配置（config/paths.json）

安装脚本会交互询问 4 类文件的存储挂载点，并写入 `config/paths.json`（已被 `.gitignore` 忽略，**不进版本库**）：

| 键 | 含义 | 默认值 |
|----|------|--------|
| `cam` | TeslaCam 视频（哨兵 / 最近 / 保存片段） | `/mnt/teslacam` |
| `music` | 音乐文件 | `/mnt/music` |
| `boombox` | boombox 音频 | `/mnt/boombox` |
| `lightshow` | lightshow 灯光秀 | `/mnt/lightshow` |

`config.py` 会读取该文件覆盖默认挂载点；若文件不存在则沿用默认值。手动修改后重启应用即可生效。
若你的 M.2 分区挂载点不同，直接改这里即可，无需改动代码。

### 4.5 运行

```bash
source venv/bin/activate
python app.py
```

### 4.6 设备基础脚本（services/）

这些 shell 脚本运行在 Radxa 设备上的 `/opt/radxa_data/`，由对应的 systemd 单元调用。
它们与 Flask Web 服务配合，完成 U 盘模拟、WiFi 切换、磁盘巡检、同步等"底层"工作：

| 脚本 | 作用 |
|------|------|
| `edit_usb.sh` | 编辑 / 重建 USB Gadget 存储布局 |
| `present_usb.sh` | 将哨兵 clips 整理为车机可读取的 U 盘内容 |
| `usb_gadget_init.sh` | 初始化 USB Gadget 模式（设备模拟成 U 盘） |
| `wifi_smart_switch.sh` | WiFi/热点智能切换（离家/到家识别） |
| `io_tuning.sh` | 磁盘 I/O 性能调优 |
| `fsck_check.sh` | 文件系统巡检（配合 `teslausb-fsck.service`） |
| `tesla_sync.sh` | 哨兵 clips 与远端/NAS 的同步 |

`services/` 内同时包含对应的 `*.service` / `*.timer` 单元文件。两种方式部署：

- **自动（推荐）**：运行 `./install.sh` 时选择"部署并启用 systemd 服务"，脚本会把单元复制到
  `/etc/systemd/system/`、把里面的 `/usr/bin/python3` 替换为仓库内的 `venv/bin/python`、
  把硬编码的 `/opt/radxa_data/teslausb` 替换为实际部署目录，然后 `daemon-reload` 并启用核心服务与定时器。
- **手动**：`cp services/*.service services/*.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now teslausb-web`

> **部署提示**：脚本从仓库拉到设备后需保持 LF 换行并赋予可执行权限，否则 systemd 会报
> `status=203/EXEC`。本仓库已通过 `.gitattributes` 强制 `*.sh` / `*.service` 使用 LF；
> 部署到设备后执行 `chmod +x /opt/radxa_data/*.sh` 即可。
> 设备连接与分发方式请参考你本地的连接文档（**不要**将其中的设备 IP / 密码提交到公开仓库）。

---

## 五、引用代码说明

本项目在以下开源成果的基础上构建，对引用部分说明如下，并致谢原作者：

### 1. [mphacker/TeslaUSB](https://github.com/mphacker/TeslaUSB)

- **许可**：GNU GPL v3.0
- **引用内容**：
  - 整体「USB Gadget 让车机自动识别 U 盘、自动同步哨兵 clips」的核心思路与设计
  - `auto_cleanup.py` 的自动清理策略**改写自**其 `scripts/web/services/cleanup_service.py`
    （文件头明确标注：`基于 mphacker/TeslaUSB 的 per-folder age/size/count 策略重写`）

### 2. [ejaramilla/teslausb-neo](https://github.com/ejaramilla/teslausb-neo)

- **许可**：沿用 TeslaUSB 体系（GPL v3.0）
- **引用内容**：
  - `boot_notify.py`（开机通知服务）——文件头标注「TeslaUSB Neo - 开机通知服务 / 作者: TeslaUSB-Neo 项目」
  - `auto_cleanup.py` 的自动清理模块 v2——文件头标注「TeslaUSB Neo 项目」

### 3. [DeaglePC/TDashcamStudio](https://github.com/DeaglePC/TDashcamStudio)

- **引用内容**：
  - 视频播放器前端。本项目通过 `routes/misc_routes.py` 的 `/tdashcam/` 路由，直接 serve 设备上
    另行 clone 安装的 TDashcam Studio 静态文件（`/opt/radxa_data/tdashcam/src`），作为 TeslaCam
    视频的播放界面，而非将其源码 vendored 进本仓库。

> 除上述三处外，本仓库其余代码（Web 管理界面、企业微信推送、云归档、车载自定义功能、硬件加速合成等）
> 均为本项目（A7Z TeslaUSB）原创或针对 A7Z 硬件的适配实现。

---

## 六、开发计划与进度

> 数据来自项目开发计划书（`deliverables/dev-plan-remaining-modules.md`、`app-modularization-plan.md`）。

### 6.1 已完成

**代码模块化重构（app.py 瘦身）✅**
- `app.py` 从 **5463 行 → 约 95 行**（-98.3%），拆分为 11 个 Blueprint + `app_state.py` + `utils/`
- 消除了 7 处 `global` 声明与路由堆积问题，启动更稳、改动更聚焦

**车载功能模块 ✅（v37~v39）**
| 模块 | 状态 |
|------|------|
| 车牌显示（License Plate） | ✅ v37 |
| 车身涂装（Custom Wrap） | ✅ v37 |
| Boombox 音效 | ✅ v37 |
| LightShow 灯光秀 | ✅ v37 |
| 锁车提示音（LockChime） | ✅ v37 |
| 媒体中心（6 tab 整合） | ✅ v38 |
| 分段上传（Present→staging→Edit） | ✅ v39 |

**系统增强 ✅**
| 模块 | 状态 |
|------|------|
| WiFi 双控整合 | ✅ |
| 云归档（14 providers） | ✅ v41 |
| SMB / NAS 自动备份（rclone v1.74.3） | ✅ v41 |
| 移动端导航适配（mobile-drawer） | ✅ v41 |
| analytics 推送健康统计 | ✅ v41 |
| rclone 升级（v1.53 → v1.74.3） | ✅ v41 |

### 6.2 进行中 / 待办

| 模块 | 状态 | 优先级 |
|------|------|--------|
| 🗺 **地图浏览器**（Leaflet 行程/路线/遥测） | ⬜ 待移植 | 🔴 高 |
| 🛡 硬件看门狗修复 | 🔴 有 bug，待修 | 🟡 中 |
| 🔧 proto 编译优化 | 🟢 低 | 🟢 低 |

### 6.3 近期路线

- 补齐地图浏览器（最高优先级待移植项）
- 修复看门狗已知 bug，提升长时间运行稳定性
- 持续优化缩略图/预览生成效率（CPU 自适应、队列治理）

---

## 七、许可证

本项目派生于 [mphacker/TeslaUSB](https://github.com/mphacker/TeslaUSB)（**GNU GPL v3.0**）。
根据 GPL 条款，**包含 GPL-3.0 代码的衍生作品须以 GPL-3.0 发布**。

因此，本仓库以 **GNU General Public License v3.0** 开源。

- 许可证全文见仓库根目录 [`LICENSE`](./LICENSE) 文件
- 第三方项目的归属与引用范围见「[五、引用代码说明](#五引用代码说明)」

> 若你需要在闭源 / 其他许可方式下使用本项目的**原创部分**，请先与上游 GPL 代码解耦，或联系相关作者。

---

## 安全与发布须知

- 真实配置文件已在 `.gitignore` 中忽略，**不要**手动 `git add` 它们。
- 如果你是从别处接手本项目：发布前务必确认 git 历史里没有真实密钥
  （可用 `git log -p -- config/sentry.json` 自查，必要时轮换密钥）。
- 本仓库为"干净导出"版本，已剔除一次性热修/调试脚本与运行产物。

## 免责声明

本项目仅供个人学习与非商业用途。使用风险由使用者自行承担。特斯拉（Tesla）为相关商标所有人，
本项目与其无隶属或合作关系。
