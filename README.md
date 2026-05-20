# A7Z TeslaUSB - 特斯拉哨兵云端备份系统

> Radxa Cubie A7Z (Allwinner A733) | Flask + systemd | Python 3.9

## 项目概述

在 Radxa A7Z ARM 单板计算机上运行的特斯拉行车记录仪管理器，实现：
- USB Gadget 模拟特斯拉存储设备
- 哨兵模式实时监控与企业微信推送
- 行车记录仪视频管理与缩略图预览
- WiFi 热点 + 客户端双模管理
- NAS 视频同步备份

## 系统架构

```
特斯拉 ←→ A7Z (USB Gadget) ←→ NVMe 存储 (exFAT)
                ↓
          Flask Web (port 5000)
                ↓
      企业微信推送 / Web 管理界面
```

## 快速开始

### 连接 A7Z

```bash
# Tailscale
ssh radxa@100.116.18.42

# 或本地 WiFi
ssh radxa@192.168.0.102
```

### Web 管理界面

- Tailscale: `http://100.116.18.42:5000`
- 本地: `http://192.168.0.102:5000`

### 部署

```bash
# 一键部署
./deploy.sh

# 预览模式（不实际部署）
./deploy.sh --dry-run
```

## 项目结构

```
.
├── app.py                   # Flask 主应用 (3037 行)
├── boot_notify.py           # 开机微信通知
├── config.py                # 配置管理
├── config_manager.py        # 配置管理器
├── disk_image_manager.py    # 磁盘镜像管理
├── media_service.py         # 媒体服务 (Boombox/Lightshow/Wraps)
├── wifi_service.py          # WiFi 双模管理
├── video_preview.py         # 视频预览/缩略图生成
├── sentry_watchdog.py       # 哨兵监控主进程
├── sentry_service.py        # 哨兵服务
├── sync_service.py          # NAS 同步服务
├── system_monitor.py        # 系统监控
├── weixin_notifier.py       # 企业微信推送 (双机器人)
├── player_routes.py         # 视频播放路由
├── sei_service.py           # SEI 服务
├── teslausb-gadgetd.py      # USB Gadget 守护进程
│
├── templates/               # Jinja2 模板 (16 个页面)
├── static/                  # JS/CSS/占位图
├── config/                  # 配置文件
├── services/                # systemd unit 文件
├── deploy.sh                # 标准化部署脚本
│
├── .gitignore
└── README.md
```

## systemd 服务

| 服务 | 用途 |
|------|------|
| `teslausb-web` | Flask Web 服务 (port 5000) |
| `teslausb-sentry` | 哨兵模式监控 |
| `teslausb-mode` | 开机 Present Mode |
| `teslausb-boot-notify` | 开机微信推送 |
| `teslausb-fsck.timer` | 每周文件系统检查 |
| `teslausb-io-tune` | I/O 性能调优 |

## 硬件信息

- **SoC**: Allwinner A733 (Cortex-A55×6 + A76×2 big.LITTLE)
- **RAM**: LPDDR4
- **存储**: KIOXIA 256GB NVMe (exFAT 分区)
- **OS**: Debian 11 (bullseye) aarch64
- **Python**: 3.9

### 硬件加速

| 加速器 | 状态 |
|--------|------|
| Video Decode (OMX) | ✅ H.264/H.265 |
| G2D 2D 引擎 | ✅ 缩放/旋转/格式转换 |
| GPU (PowerVR) | ✅ OpenGL ES 3.2 |
| ffmpeg OpenCL | ✅ OpenCL 3.0 |

## 开发规范

### 分支策略
- `main` - 生产就绪代码
- `feat/xxx` - 新功能
- `fix/xxx` - Bug 修复
- `chore/xxx` - 杂项

### 提交规范 (Conventional Commits)
```
type(scope): description

feat(sentry): 支持空事件过滤
fix(wifi): 修复 NM connection 命名冲突
chore(cleanup): 删除临时调试脚本
```

### 部署流程
1. 本地开发 → 测试
2. `git commit` 提交
3. `./deploy.sh --dry-run` 预览
4. `./deploy.sh` 部署到 A7Z
5. 验证 `http://100.116.18.42:5000/`

### 注意事项
- Python 3.9 兼容：使用 `Optional[dict]` 代替 `dict | None`
- Flask 模板缓存：部署后必须 `systemctl restart teslausb-web`
- 文件编码：Windows→Linux 用 SFTP 二进制模式，避免 CRLF 问题

## 里程碑

| 阶段 | 内容 | 状态 |
|------|------|------|
| M1 | 基础设施搭建 | ✅ |
| M2 | 核心功能 | ✅ |
| M3 | WiFi+Web+exFAT | ✅ |
| M4 | 高级功能 (缩略图/推送/WiFi) | ✅ |
| M5 | 系统测试 | 🔄 |
| M6 | 标准化交付 | ⬜ |

## 许可

内部项目
