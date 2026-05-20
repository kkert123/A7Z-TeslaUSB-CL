# A7Z TeslaUSB 上线前全检报告

**日期**：2026-05-20
**场景**：上线前全检 — 代码审查 + 安全审计 + QA测试
**参与成员**：产品评审员 + 安全官 + QA负责人

---

## 📌 TL;DR（执行摘要）

- 整体结论：🔴 **NO-GO — 不可上线**
- 阻断级问题：**10 个**（运行时崩溃/安全暴露/功能完全不可用）
- 严重问题：**12 个**（核心功能异常/认证缺陷/性能瓶颈）
- 中等问题：**11 个**（边界条件/代码质量）
- 建议项：**9 个**（优化/技术债务）
- 总计发现问题：**42 个**
- 下一步：修复全部 10 个阻断项 + 至少 8 个严重项后重新评估

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🔴 **No-Go** （10 个阻断级问题） |
| 严重度分布 | 🔴 10 / 🟠 12 / 🟡 11 / 🟢 9 |
| 关键行动项 | 10 条 P0 |
| 预估修复工时 | 2-3 天（P0+P1） + 1 天集成测试 |
| 自动化测试覆盖率 | < 5% |
| 安全评级 | D（不合格） |
| 代码健康度 | 32/100 |

---

## 1. 各成员核心结论

### 🔍 产品评审员（代码审查）
- 核心判断：**4 个阻断级运行时崩溃**——weixin_notifier.py 版本 API 断裂导致所有调用方崩溃；teslausb-gadgetd.py 缺少 `import threading`；sentry_service.py 依赖缺失模块；构造参数不匹配。
- 关键建议：用 clean_deploy/weixin_notifier.py 替换根目录版本并补齐缺失模块，这是后续所有修复的前提。

### 🛡️ 安全官（OWASP+STRIDE 审计）
- 核心判断：**6 个严重漏洞、安全评级 D**。企业微信 webhook 密钥通过 API 暴露、Flask secret_key 硬编码、多处 `shell=True` 命令注入、ffmpeg 路径注入、明文密码、日志流无认证。
- 关键建议：立即从 `get_wecom_status()` 删除 webhook 密钥字段；用环境变量替代硬编码 secret_key；移除所有 `shell=True`。

### ✅ QA 负责人（功能测试）
- 核心判断：**32/100 健康度、6 个阻断级缺陷**。WeixinNotifier API 全链路断裂、sentry_service 导入链崩溃、player_routes 从未注册、三套路径体系互不兼容、event.json 二进制处理不全。
- 关键建议：修复 WeixinNotifier API → 注册 player blueprint → 统一路径配置 → 修复二进制 event.json → GStreamer→ffmpeg 替换，按此顺序执行。

---

## 2. 综合审查发现（去重合并后按严重度排序）

### 🔴 阻断级（10 项 — 必须在上线前修复）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|--------|------|------|---------|------|------|
| 1 | 🔴 | 功能崩溃 | weixin_notifier.py + sentry_service.py:169 + boot_notify.py:447 + app.py:1311 | **WeixinNotifier API 全链路断裂**。根目录版本（369行，v0.x，接受 `webhook_url`）与所有调用方（使用 `webhook_key`+`bot_name`+高级方法）完全不兼容。send_test_message/send_sentry_detected/send_upload_complete/send_boot_notification 等方法不存在。 | 用 `clean_deploy/teslausb-web/weixin_notifier.py`（577行）替换根目录版本，同时确保 `webhook_key` 参数可自动拼接完整 URL | 产品+QA |
| 2 | 🔴 | 导入崩溃 | sentry_service.py:27-41 | **sentry_service 导入链断裂**。`from sentry_watchdog import ...`、`from location_detector import ...`、`from wifi_switcher import ...` 指向不存在的模块（文件在 clean_deploy 目录，不在 Python import 路径中）。`from weixin_notifier import WeComConfig` 引用了不存在的类。 | 将 sentry_watchdog.py/location_detector.py 部署到运行目录；移除不存在的 import；统一 PYTHONPATH | 产品+QA |
| 3 | 🔴 | 功能缺失 | app.py + player_routes.py | **player_routes blueprint 从未注册**。player_routes.py 提供 `register_player_routes(app)` 但 app.py 1353 行中从未调用。`/player` 及所有子 API 端点（screenshot/telemetry/events/extract_clip）全部返回 404。 | 在 app.py 末尾 `app.run()` 之前添加 `register_player_routes(app)` | QA |
| 4 | 🔴 | 路径断裂 | app.py + media_service.py + boot_notify.py | **三套路径体系互不兼容**。app.py 用 `/mnt/teslacam`，media_service.py 用 `/media/cnlvan/cam`，clean_deploy 模块用 `/opt/teslausb-web/`。同一部署下 media_service 的服务类读写完全不同的目录。 | 创建统一 `config.py`，所有模块从配置导入 PARTITIONS/MOUNT_POINTS 常量 | QA |
| 5 | 🔴 | 运行时崩溃 | teslausb-gadgetd.py:289 | **缺少 `import threading`**。第289行使用 `threading.Thread()` 但文件头部未导入该模块，运行时 `NameError`。 | 在文件头部添加 `import threading` | 产品 |
| 6 | 🔴 | 安全暴露 | app.py:430,438 | **企业微信 Webhook 密钥通过 API 暴露**。`get_wecom_status()` 在 JSON 响应中返回完整 webhook URL（含 secret key），任何可访问 Web 页面的人可见。 | 从 API 响应中移除 `url` 字段，仅在成功/失败时返回状态码 | 安全 |
| 7 | 🔴 | 命令注入 | disk_image_manager.py:56-82, 127, 155, 187, 215 | **`shell=True` 命令注入风险**。`subprocess.run(cmd, shell=True)` 使用字符串拼接（含路径变量），虽仅 root 执行但在 USB gadget 控制中构成注入向量。 | 改用列表参数 `subprocess.run([...], shell=False)` | 安全+产品+QA |
| 8 | 🔴 | 命令注入 | video_preview.py:167, 480, 827 | **ffmpeg 命令注入**。视频文件路径作为 ffmpeg 参数传递时未严格验证，恶意构造的文件名可触发 shell 命令注入。 | 使用 `subprocess.run([...], shell=False)` 列表模式；对路径做 `os.path.abspath` 验证 | 安全 |
| 9 | 🔴 | 认证缺陷 | app.py:657-658 | **日志流端点无认证**。`/api/logs/stream` SSE 端点缺少 `@require_auth` 装饰器，任何人可实时获取系统日志。 | 添加 `@require_auth` 装饰器 | 安全 |
| 10 | 🔴 | 数据崩溃 | player_routes.py:183-185 + video_preview.py:420-421 + sentry_service.py:252 | **event.json 二进制格式处理不完整**。约 5% 的 Tesla event.json 为二进制 protobuf 格式（首字节非 `{`），直接 `json.load(f)` 导致 `JSONDecodeError` 崩溃。检查首字节的方式不够健壮。 | 创建统一 `safe_read_event_json()` 函数：检测首字节→多编码回退→JSON parse→失败返回空 dict | QA |

### 🟠 严重级（12 项 — 上线前强烈建议修复）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|--------|------|------|---------|------|------|
| 11 | 🟠 | 硬件不兼容 | player_routes.py:305-307, 341-344 | **GStreamer OMX 硬件解码器无法在 A7Z 上运行**。`omxh264dec`/`omxh264enc`/`omxmjpegvideoenc` 为 Raspberry Pi 专用 OpenMAX 元素，Allwinner A733 无此硬件。截图和视频剪辑功能完全不可用。 | 替换为 ffmpeg 软解码方案（video_preview.py 已验证可用） | 产品+QA |
| 12 | 🟠 | 安全 | app.py:18 | **Flask secret_key 硬编码**。`'teslausb-secret-key-change-in-production'` 可被任何人用于伪造 session cookie，登录认证形同虚设。 | 从环境变量 `FLASK_SECRET_KEY` 读取；首次运行时生成随机密钥 | 安全+产品+QA |
| 13 | 🟠 | 安全 | app.py:582-584 | **明文密码存储**。admin/teslausb 密码以明文存储在 config.json 中，Fernet 加密密钥存储在可物理访问的 SD 卡上。 | 使用 werkzeug.security 哈希密码；Fernet 密钥改用环境变量 | 安全 |
| 14 | 🟠 | 性能 | app.py:195-197 | **`get_cpu_percent()` 在同步请求中 sleep(0.5)**。每次 Dashboard 渲染阻塞整个 Flask 线程 0.5 秒。10 并发请求排队 5 秒。 | 改用 `psutil.cpu_percent(interval=None)` 非阻塞模式，或后台线程定时缓存 | 产品+QA |
| 15 | 🟠 | 功能冲突 | add_to_app.py (67行) | **add_to_app.py 是已废弃的路由但未标记**。包含与 app.py 重复的 `/api/mode/status` 和 `/api/mode/switch` 路由，逻辑不一致（UDC 检测 vs flag 文件）。若拼接即路由冲突 AssertionError。 | 删除或添加 `# DEPRECATED - DO NOT USE - 已合并到 app.py` 注释 | 产品 |
| 16 | 🟠 | 性能 | app.py:384-391 | **get_folders() 无缓存，大目录严重阻塞**。`os.walk()` 遍历整个 TeslaCam 目录树统计 3000+ 视频文件时需 5-30 秒。Dashboard 页面加载直接卡死。 | 缓存到 JSON 文件，后台 cron 定时更新；或使用 `find` 命令异步执行 | QA |
| 17 | 🟠 | 数据完整性 | app.py:467-472 | **get_queue_status() 是硬编码空壳**。返回空列表，get_queue_counts() 永远返回 0。Dashboard 上传进度 100% 不准确。 | 接入 sync_service.py 或 sentry_watchdog.py 的真实队列状态 | QA |
| 18 | 🟠 | 认证缺陷 | player_routes.py:117,212,264,365,388 | **player API 全部缺少认证**。api_player_events/api_player_event_detail/api_player_telemetry/api_player_screenshot/api_player_extract_clip 无任何认证装饰器。 | 添加 `@require_auth` 或 Blueprint.before_request 认证检查 | QA |
| 19 | 🟠 | 运行时崩溃 | sync_service.py:477,515 | **`import sys` 放在文件末尾**。但 `_send_wechat_notify()` 第477行使用 `sys.path.insert(0, ...)`。若模块导入时触发顶层执行，`NameError`。 | 将 `import sys` 移到文件顶部 | QA |
| 20 | 🟠 | 构造不匹配 | sentry_service.py:169-181 | **WeixinNotifier 构造参数细微不兼容**。`webhook_url=status_url or None` 当 url 为空字符串时传给 WeComConfig 可能导致空 URL 而非从 key 构建。 | 统一所有调用方使用 `WeixinNotifier(webhook_key=key, bot_name="name")` | 产品 |
| 21 | 🟠 | 功能异常 | preview_generator.py:34 + auto_cleanup.py:34 | **`from config import PARTITIONS` 依赖不存在的 config 模块**。config.py 路径不在 PYTHONPATH 中，预览生成和自动清理模块导入即失败。 | 确保 config.py 在正确位置；或改用环境变量 | QA |
| 22 | 🟠 | 功能异常 | video_preview.py:490-499 | **ffmpeg 四宫格预览失败后无降级策略**。仅 log.warning，不生成占位图。用户看到的哨兵事件无预览图。 | ffmpeg 失败时生成文字占位符或使用 PIL 读取单帧 | QA |

### 🟡 中等（11 项）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|--------|------|------|---------|------|------|
| 23 | 🟡 | 代码质量 | app.py:660,1260,1277,1286,1400,1500,1521 | **import subprocess 在函数体内重复 7 次**。subprocess/json/shutil 已在顶部导入，重复导入冗余且可能引发 WSGI 服务器模块重载问题。 | 删除函数体内的重复 import | 产品 |
| 24 | 🟡 | 发送失败 | sentry_watchdog.py:286 | **空事件（file_count=0）未过滤**。哨兵创建仅含 event.json 的空文件夹时仍触发推送通知，用户收到 "0 个视频片段" 的无效通知。 | 添加 `file_count > 1` 过滤（至少一个视频文件） | QA |
| 25 | 🟡 | 功能局限 | app.py:1530 | **cleanup_execute() 每次仅删 10 个文件**。即使有 1000 个过期文件也只处理前 10 个，且不提示剩余数量。 | 增加可配置 batch_size；提示剩余待清理数量 | QA |
| 26 | 🟡 | 兼容性 | app.py:42 | **get_wifi_info() 使用已弃用的 iwconfig**。现代 Debian 上 iwconfig 常不可用（iw/nmcli 为主流）。 | 优先尝试 `iw dev wlan0 info` 或 `nmcli`，回退 iwconfig | QA |
| 27 | 🟡 | 稳定性 | boot_notify.py:466 | **微信推送失败时 exit(1)**。若 systemd 配置了 `Restart=on-failure`，推送失败会反复重启耗尽资源。 | 推送失败记录日志并 `sys.exit(0)`，或 systemd 配置 `Restart=no` | QA |
| 28 | 🟡 | 日志混乱 | video_preview.py + sentry_watchdog.py + sentry_service.py | **多个 basicConfig() 互相覆盖日志配置**。每个模块独立调用 logging 配置导致日志格式和级别不一致。 | 统一在入口点（app.py 启动时）配置日志，子模块仅用 `getLogger(__name__)` | QA |
| 29 | 🟡 | 进程泄漏 | app.py:665-677 | **SSE logs_stream 的 `tail -f` 进程泄漏**。客户端非正常断开 TCP 时 GeneratorExit 不一定触发，`tail -f` 进程可能永久驻留。 | 添加超时机制 + `atexit.register` 清理 | 产品 |
| 30 | 🟡 | 兼容性 | video_preview.py:173,486,833 | **ffmpeg 已废弃的像素格式 `yuvj420p`**。新版 ffmpeg 应使用 `yuv420p`。通常能工作但有 deprecated warning。 | 替换为 `yuv420p` | 产品 |
| 31 | 🟡 | 路径错误 | video_preview.py:779 | **硬编码旧用户路径 `/media/cnlvan/cam/TeslaCam`**。在 A7Z 上应该是 `/mnt/teslacam/TeslaCam`。 | 统一使用 config.py 中的路径常量 | 产品 |
| 32 | 🟡 | 代码残留 | deep_search_decorator.py (123行) | **一次性诊断修复脚本残留在项目目录**。直接修改 `/opt/radxa_data/teslausb/app.py`，含 `__import__('datetime')` 动态导入。不应被误当作模块。 | 移除到 scripts/archive/ 或直接删除 | 产品 |
| 33 | 🟡 | 权限过宽 | teslausb-gadgetd.py:211 | **Unix socket 权限 `0o777`**。任何系统用户可读写 USB gadget 控制 socket。攻击者可发送恶意 gadget 命令。 | 改为 `0o660` 并设置正确的 group | 产品 |

### 🟢 建议（9 项）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|--------|------|------|---------|------|------|
| 34 | 🟢 | 技术债务 | 根目录 | **23 个 app.py 历史备份 + 130+ 临时脚本**。`app_a7z_backup.py`/`_check_*.py`/`_deploy_*.py`/`_fix_*.py` 等污染项目结构。 | 保留 clean_deploy/；删除所有备份 app_*.py 和 `_` 前缀脚本；归档 scripts/ 到独立 repo | 产品 |
| 35 | 🟢 | 测试缺失 | 全部 | **自动化测试覆盖率 < 5%**。无单元测试、无集成测试、无回归测试。仅少量 `if __name__ == "__main__"` 自测代码。 | 至少为 app.py 核心路由添加 pytest 测试 | 产品+QA |
| 36 | 🟢 | 安全 | app.py 全部 POST | **无 CSRF 保护**。所有 POST 端点（登录/模式切换/文件上传/清理）无 CSRF token。 | 添加 Flask-WTF CSRF 保护或自定义 token 中间件 | 产品+安全 |
| 37 | 🟢 | 运维 | app.py 末尾 | **Flask 开发服务器直接暴露 0.0.0.0:5000**。`app.run()` 不适合生产环境。 | 使用 gunicorn + Nginx 反向代理 | 产品 |
| 38 | 🟢 | 运维 | 多处 FileHandler | **日志无轮转机制**。多处直接使用 FileHandler 写入 `/var/log/`，长期运行可能磁盘满。 | 统一使用 RotatingFileHandler（maxBytes + backupCount） | 产品 |
| 39 | 🟢 | 代码质量 | weixin_notifier.py | **`send_file()` 和 `send_sentry_notification()` 图片上传为 TODO 存根**。 | 实现或明确移除 | QA |
| 40 | 🟢 | 错误处理 | 多处 | **多处 `except: pass` 裸异常吞没**。异常信息完全丢失，调试困难。 | 至少添加 `logger.debug()` 记录 | QA |
| 41 | 🟢 | 安全 | config_manager.py | **Fernet 加密密钥存储在未加密分区**。若 SD 卡被物理提取，密钥可被读取。 | 使用 TPM/SE 或至少用环境变量存储密钥 | QA |
| 42 | 🟢 | 可靠性 | sync_service.py:506 | **`send_text()` 吞没所有异常**。同步完成但通知失败时用户无感知。 | 添加日志告警或重试机制 | QA |

---

## ✅ 行动清单（按优先级排序）

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | **替换 weixin_notifier.py** 为 clean_deploy 版本 + 确保 webhook_key 自动拼接 URL | 开发 | P0 | 立即 |
| 2 | **修复 sentry_service 导入链**：部署 sentry_watchdog.py/location_detector.py + 移除不存在的 import | 开发 | P0 | 立即 |
| 3 | **注册 player_routes blueprint** 到 app.py | 开发 | P0 | 立即 |
| 4 | **创建统一 config.py**：定义 PARTITIONS/MOUNT_POINTS 常量，所有模块统一导入 | 开发 | P0 | 立即 |
| 5 | **添加 `import threading`** 到 teslausb-gadgetd.py | 开发 | P0 | 立即 |
| 6 | **从 get_wecom_status() API 响应中删除 webhook 密钥** | 开发 | P0 | 立即 |
| 7 | **移除所有 `shell=True`**：disk_image_manager.py + 其他文件改为列表参数模式 | 开发 | P0 | 立即 |
| 8 | **ffmpeg 命令注入修复**：使用 `shell=False` 列表模式 + 路径验证 | 开发 | P0 | 今日内 |
| 9 | **添加 @require_auth 到 /api/logs/stream** | 开发 | P0 | 今日内 |
| 10 | **统一 safe_read_event_json()**：二进制 event.json 降级处理 | 开发 | P0 | 今日内 |
| 11 | **替换 GStreamer → ffmpeg**：player_routes.py 截图/视频剪辑功能 | 开发 | P1 | 1-2天 |
| 12 | **替换 Flask secret_key** 为环境变量 | 开发 | P1 | 1-2天 |
| 13 | **密码哈希化**：使用 werkzeug.security 替代明文 | 开发 | P1 | 1-2天 |
| 14 | **get_cpu_percent() 改为非阻塞缓存** | 开发 | P1 | 1-2天 |
| 15 | **删除/标记 add_to_app.py 为废弃** | 开发 | P1 | 1-2天 |
| 16 | **get_folders() 添加缓存机制** | 开发 | P1 | 1-2天 |
| 17 | **接入真实队列状态** 替换 get_queue_status() 空壳 | 开发 | P1 | 1-2天 |
| 18 | **player API 添加认证检查** | 开发 | P1 | 1-2天 |
| 19 | **修复 sync_service.py import sys 位置** | 开发 | P1 | 1-2天 |
| 20 | **统一 logging 配置**（入口点统一 basicConfig） | 开发 | P2 | 上线后 |
| 21 | **添加 RotatingFileHandler** 防止磁盘满 | 运维 | P2 | 上线后 |
| 22 | **清理技术债务**：删除 23 个备份 + 130+ 临时脚本 | 开发 | P2 | 上线后 |

---

## ⚠️ 阻塞项清单（上线前必须清零）

| 阻塞项 | 当前状态 | 解除条件 |
|--------|---------|---------|
| WeixinNotifier API 断裂 | ❌ | 所有调用方测试通过 |
| sentry_service 导入崩溃 | ❌ | `python3 -c "import sentry_service"` 成功 |
| player_routes 未注册 | ❌ | `/player` 返回 200 |
| 三套路径并存 | ❌ | 单次部署所有模块访问同一目录 |
| Webhook 密钥泄露 | ❌ | API 响应不含 key |
| shell=True 注入 | ❌ | 全局搜索零 `shell=True` |
| 日志流无认证 | ❌ | 未登录访问返回 401 |
| event.json 二进制崩溃 | ❌ | 5/102 二进制事件不触发 500 |

---

## 📊 回滚预案

| 步骤 | 操作 | 回滚方法 |
|------|------|---------|
| 1 | 部署前备份 | `cp -r /opt/radxa_data/teslausb /opt/radxa_data/teslausb.backup.YYYYMMDD` |
| 2 | 逐步替换文件 | 每替换一个文件后 `systemctl restart teslausb-web` 验证 |
| 3 | 验证失败 | `cp /opt/radxa_data/teslausb.backup.YYYYMMDD/* /opt/radxa_data/teslausb/ && systemctl restart teslausb-web` |

---

## 📚 成员产出索引

- gstack-product-reviewer（产品评审员）原始产出：对话消息 ID `gstack-product-reviewer` — 发现 4 阻断 + 5 严重 + 8 中等 + 6 建议
- gstack-security-officer（安全官）原始产出：`D:\teslausb\a7z\.gstack\security-audit-history\audit-2026-05-14-000000.md` — 发现 6 严重 + 6 高 + 4 中 + 3 低
- gstack-qa-lead（QA负责人）原始产出：对话消息 ID `gstack-qa-lead` — 发现 6 阻断 + 8 严重 + 7 中等 + 5 建议

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
