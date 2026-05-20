# MEMORY.md - 长期记忆

> Updated: 2026-05-20 22:45

## 项目信息 (HOT)
- **项目**: Radxa A7Z TeslaUSB | 路径: `D:\teslausb\a7z\`
- **参考**: TeslaUSB-CL (`D:\teslausb\a2\TeslaUSB-CL`), 完整备份 (`D:\teslausb\a2\20260418161423\teslausb-web`)
- **计划**: `.workbuddy/artifacts/pm-upgrade-plan.md` — SSOT+GitOps 升级方案
- **部署**: `deploy.sh` — 标准化部署脚本 (替代零散 _deploy_*.py)
- **Git**: 待初始化 (当前 Windows 机器未安装 Git)
- **归档**: `project-management-plan.md` 和 `development-guide-and-tracking.md` (2026-05-14)

## 本地工作区 (2026-05-20 清理后)
- 根目录: 20 个核心文件 (从 200+ 精简)
- 临时文件备份: `_backup_20260520/` (275 文件)
- 缺失模块 (待 A7Z 同步): config_manager, media_service, wifi_service, sync_service, system_monitor, auto_cleanup, location_detector, hardware_watchdog, fsck_check, upload_scheduler, preview_generator (共 11 个)
- 缺失模板 (待 A7Z 同步): base, boombox, lightshow, lockchime, login, sentry, upload_progress, wifi, wraps (共 9 个)

## 技术栈
- 硬件: Radxa Cubie A7Z (Allwinner A733) + KIOXIA 256GB NVMe
- Web: Flask + Jinja2 + 纯 CSS | 推送: 企业微信 Webhook (双机器人)
- 存储: exFAT (全 NVMe 分区) | Python: 3.9 (需 Optional[dict], 不支持 dict|None)

## A7Z 连接信息
- Tailscale: `100.116.18.42` | WiFi: `192.168.0.102`
- SSH: `radxa`/`radxa` | Samba: `teslausb`/`tesla`
- Web: `http://100.116.18.42:5000` | 路径: `/opt/radxa_data/teslausb/`
- ⚠️ Tailscale SSH 已启用 (2026-05-20)，需周期性重新认证: https://login.tailscale.com/a/ld411b9c33be89
- Debian 11 (bullseye) aarch64

## A7Z 硬件加速 (2026-05-20 核实)
| 加速器 | 内核驱动 | Userspace | 状态 | 用途 |
|--------|---------|-----------|------|------|
| Video Decode (OMX) | `sunxi_ve` | `libgstreamer-openmax-allwinner` | ✅ H.264/H.265 HW | 截图/裁剪/缩略图 |
| Video Encode (OMX) | `sunxi_ve` | `libgstreamer-openmax-allwinner` | ✅ H.264/MJPEG HW | 片段编码/JPEG截图 |
| G2D 2D引擎 | `g2d_sunxi` | `/dev/g2d` + `sunxi-g2d.h` | ✅ chmod 666 | 缩放/旋转/格式转换 |
| GPU (PowerVR) | `pvrsrvkm` | `libGLESv2_PVR_MESA.so` v24.2 | ✅ symlink 修复 | BXM-4-64, OpenGL ES 3.2 |
| NPU (Vivante) | `vipcore` 已加载 | ❌ 无 /dev/npu* | ❌ | 待 SDK 部署 |
| ffmpeg OpenCL | `pvrsrvkm` | `libPVROCL.so` v24.2, ICD已注册 | ✅ clinfo 正常 | OpenCL 3.0, BXM-4-64, 600MHz (2026-05-20) |

### GPU symlink 修复 (2026-05-20)
- Mesa 备份移至 `libGLESv2_mesa_backup_keep`
- `libGLESv2.so` → `/usr/lib/libGLESv2_PVR_MESA.so` (PowerVR BXM-4-64)
- `libGLESv2.so.2` → 同上

### ffmpeg OpenCL ICD 修复 (2026-05-20)
- 安装 `ocl-icd-libopencl1 opencl-headers clinfo`
- 创建 `/etc/OpenCL/vendors/powervr.icd` → `/usr/lib/libPVROCL.so`
- `clinfo` 正常：Platform PowerVR, OpenCL 3.0, BXM-4-64, 600MHz, 959MiB
- ffmpeg `-init_hw_device opencl` + `avgblur_opencl` 基准测试通过

## NVMe 分区布局
| 分区 | 大小 | FS | 卷标 | 挂载点 | 用途 |
|------|------|----|------|--------|------|
| p1 | 4G | swap | - | [SWAP] | + zram |
| p2 | 200G | exFAT | TESLACAM | /mnt/teslacam | 行车记录 |
| p3 | 8G | exFAT | MUSIC | /mnt/music | 音乐 |
| p4 | 2G | exFAT | LIGHTSHOW | /mnt/lightshow | 灯光秀 |
| p5 | 2G | exFAT | BOOMBOX | /mnt/boombox | Boombox |
- radxa_data: eMMC rootfs `/opt/radxa_data` (无独立分区)

## 里程碑进度
| 阶段 | 进度 | 关键日期 |
|------|------|----------|
| M1 基础设施 | ✅ 100% | 2026-05-14 |
| M2 核心功能 | ✅ 100% | 2026-05-15 |
| M3 WiFi+Web+exFAT | ✅ 100% | 2026-05-16 |
| M4 高级功能 | ✅ 100% | 2026-05-20 上线前全检完成，42问题→27已修复 |
| M5 系统测试 | 🔄 25% | 全页面功能验证通过，哨兵 12h+ 稳定，缩略图 358 张正常 |
| M6 交付 | ⬜ 0% | - |

## 📋 代码审查标准 (2026-05-20)
- 文档: `deliverables/gstack/code-review-standards.md`
- 等级: L1(自动扫描) → L2(标准) → L3(深度) → L4(上线全检)
- 清单: 安全/正确性/可靠性/性能/可维护性 五维度
- 流程: 提交 → L1 扫描 → 审查 → 报告 → 合并

## 关键脚本和模块 (HOT)
- `/opt/radxa_data/`: `present_usb.sh`, `edit_usb.sh`, `usb_gadget_init.sh`, `fsck_check.sh`
- Web 服务: `app.py` (Flask, port 5000)
- 推送: `weixin_notifier.py` (690行, 双机器人)
- 哨兵: `sentry_watchdog.py` (772行), `sentry_service.py` (450行)
- 预览: `video_preview.py` | 同步: `sync_service.py` | WiFi: `wifi_service.py`
- 媒体: `media_service.py` (BoomboxService/LightshowService/WrapsService)
- IoT: `io_tuning.sh` | 系统: `system_monitor.py`, `hardware_watchdog.py`

## systemd 服务
| 服务 | 用途 | 状态 |
|------|------|------|
| teslausb-web | Flask Web | ✅ Active |
| teslausb-mode | 开机 Present Mode | ✅ Active |
| teslausb-fsck.timer | 每周日 03:00 fsck | ✅ Active |
| teslausb-io-tune | I/O 调优开机 | ✅ Active |
| teslausb-boot-notify | 开机微信推送 | ✅ Active |
| teslausb-sync.timer | 30min 同步检查 | ⚠️ 待配 NAS |
| **teslausb-sentry** | **哨兵监控** | **✅ Active** |

## 📝 经验教训 (WARM)
1. **Flask 模板缓存**: 部署后必须 `systemctl restart`，SCP 落盘 ≠ 生效
2. **Windows→Linux 部署**: 用 SFTP 二进制 `sftp.open('w')` 避免 CRLF，路径纯正斜杠
3. **systemd 203/EXEC**: 检查执行权限 + 换行符 (`dos2unix`) + shebang
4. **sudo 非 TTY**: `echo 'password' | sudo -S cmd`
5. **Present Mode 只读挂载**: gadget 脚本 umount 会破坏 present_usb.sh 的 ro mount，必须在 gadget 启动后重新 ro mount
6. **Python 模块级 FileHandler 陷阱**: 模块级 `FileHandler('/var/log/')` 如果用户无写权限 → import 即崩溃。使用 try/except addHandler
7. **WeixinNotifier bot_name**: 只有 `bot_name in ["哨兵事件","系统通知"]` 时才会从 config_manager 读 key，其他名字需直接传 `webhook_key`
8. **LocationDetector API**: `LocationInfo.state` 是 `LocationState` enum，不是 `status`；方法名是 `check_location()` 不是 `detect_location()`
9. **可选模块导入模式**: `try: from x import Y \nexcept ImportError: Y = None` 后，实例化处必须 `if Y is not None: obj = Y()` 防 NoneType callable 崩溃
10. **journalctl 特殊 unit 处理**: kernel→`-k`，systemd→`_PID=1`，cron/smbd 可能未安装 → 返回友好提示
11. **disk_cache 合并模式**: `_stats_broadcaster` 每 3s 覆盖缓存仅写已挂载分区 → 必须先读旧缓存再合并；模板/JS `mounted=False` 时勿丢弃缓存数据
12. **Jinja2 模板变量名一致性**: 字段名必须对齐（key_suffix vs key_preview, python vs python_version）
13. **ARM cpuinfo 解析**: 无 `model name`，需解析 `CPU part` + big.LITTLE `Counter` 统计
14. **RecentClips 缩略图**: serve_thumbnail 只查 isdir 子目录 → 平铺文件需额外搜索 `{event_id}-*.mp4` + 传 video_files
15. **哨兵推送字体**: video_preview.py 字体列表必须包含 A7Z 实际可用中文字体（simhei.ttf），否则回退 DejaVuSans 乱码
16. **哨兵推送位置**: ~~传 `event.location_status`（home/away）而非 `event.json` 的 city/street → 需读取 event.json~~ ✅ 已修复，但 Tesla 5/102 个 event.json 为二进制格式（非 JSON），导致 UTF-8 解码失败回退。需编码容错 + 空事件过滤 (2026-05-19)
17. **media_service 路由**: module 有 class methods 但 app.py 零路由 → 全部 404；需逐一手动连接
18. **Tesla 文件名时区**: RecentClips/SentryClips 文件名已是本地时间（CST），勿加 UTC 偏移
19. **Tesla event.json 二进制问题 (NEW)**: 约 5% 的哨兵事件文件夹中 event.json 为 Tesla 内部二进制格式（非文本），`open(f,'r')` 会 UnicodeDecodeError。需先读 binary 检测首字节：0x7b='{'→JSON, 其他→跳过。同时这些事件通常 file_count=0（无视频），应过滤空事件不推送。
20. **WiFi NM connection 命名冲突 (NEW)**: `switch_wifi()` 硬编码 `con_name=f"WiFi-{ssid}"` 而 NM 自动创建时用纯 SSID 名，导致同一 SSID 两个 profile → 页面重复。修复：切换前扫描所有 profile 的 SSID 复用已有连接名 + `get_wifi_connections()` 按 SSID 去重 (2026-05-19)
21. **哨兵预览帧偏移错误 (NEW)**: `video_preview.py` `generate_sentry_grid_preview()` 始终选 `video_files[0]`(第一个视频段) + 用文件夹名做 time_offset 基准（文件夹名=结束时间，event.json=触发时间，差值为负）。修复：从文件名解析每段视频时间戳→找 key_ts 所在段→选该段 + time_offset=key_ts-视频段开始时间 (2026-05-19)

## 🤔 待办优先级 (HOT)
1. ✅ **M4 收尾**: 日志/缩略图/磁盘缓存/水印/媒体API 全部完成 (2026-05-17)
2. ⬜ **Task 5.1**: M5 系统集成测试
3. ⬜ **Task 5.2**: 用户文档与部署指南
4. ⏸️ **NAS 配置**: 与 WiFi/AP 双模一起做
5. 🔮 **候选**: 地图集成 + 遥测 HUD (v1.0 后评估)

## 2026-05-17 修复汇总
| # | 问题 | 状态 |
|---|------|------|
| 1 | 日志页 Kernel/Systemd/Cron "No entries" | ✅ |
| 2 | 行车记录仪 RecentClips 无缩略图 | ✅ |
| 3 | System/Dashboard 磁盘挂载状态异常 | ✅ |
| 4 | 企业微信机器人字段不匹配 | ✅ |
| 5 | System 页 CPU 型号/Python 版本空白 | ✅ |
| 6 | Dashboard 缓存分区显示"未挂载"无数据 | ✅ |
| 7 | Analytics 清理管理分区缓存缺失 | ✅ |
| 8 | RecentClips 懒生成缩略图 404 | ✅ |
| 9 | 视频时间戳 UTC→CST 转换（Tesla 本地时间无需转换） | ✅ |
| 10 | 哨兵推送水印中文乱码 | ✅ |
| 11 | 哨兵推送位置显示 home/away 非真实地址 | ✅ |
| 12 | /media 页面全部 API 404 | ✅ |

## 2026-05-19 修复汇总
| # | 问题 | 根因 | 修复方案 | 状态 |
|---|------|------|----------|------|
| 1 | 哨兵推送地点显示 "away" | 5/102 个 Tesla event.json 为二进制格式（首字节 0xcb/0xda/0xbe 非 `{`），`_read_event_location()` `open(f,'r')` UTF-8 decode 失败 → 降级用 `event.location_status` | 编码容错：先 binary 读检测首字节→多编码回退(UTF-8→UTF-16→Latin-1)→JSON parse | ✅ |
| 2 | 哨兵推送视频片段显示 "0" | 这 5 个事件文件夹确实含 0 个 mp4 文件（Tesla 创建空事件/立即取消），但哨兵仍推送通知 | `_on_new_event` / `_on_confirm_request` 增加 `file_count<=1` 空事件过滤，仅日志记录不推送 | ✅ |
| 3 | WiFi 页面出现重复条目 "WiFi-189-AP" | `switch_wifi()` 硬编码 `con_name=f"WiFi-{ssid}"`，NM 自动创建时用纯 SSID 名，同一 SSID 两个 NM profile | switch_wifi 扫描所有 profile 复用已有连接名 + get_wifi_connections 按 SSID 去重 + 清理 2 个冗余 NM profile | ✅ |
