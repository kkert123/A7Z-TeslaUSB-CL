# A7Z TeslaUSB

> 一块 Radxa Cubie A7Z 伪装成 U 盘插在特斯拉上，车机写进来的行车记录仪视频，自动推送到企业微信、备份到 NAS 和云端。

**硬件**：Radxa Cubie A7Z (Allwinner A733) · 959MB RAM · 256GB NVMe
**软件**：Debian 11 · Python 3.9 · Flask · systemd

---

## 5 秒了解

车机以为自己在往 U 盘写，其实写进了 A7Z 的 NVMe。A7Z 同时连着 WiFi，写完就推送、备份、生成预览。

```
特斯拉车机 ──USB──► A7Z ──► NVMe (exFAT)
                     │
                     ├──► 企业微信推送
                     ├──► Web 管理 (:5000)
                     ├──► NAS 同步
                     └──► 云归档 (rclone)
```

---

## 快速开始

```bash
# 连设备
ssh radxa@<tailscale-ip>

# 打开管理界面
http://<tailscale-ip>:5000
```

| 我要… | 去这里 |
|-------|--------|
| 第一次接触这个项目 | [15 分钟理解 A7Z](docs/tutorials/01-十五分钟理解A7Z.md) |
| 装一台新设备 | [接管一台新设备](docs/tutorials/02-接管一台新设备.md) |
| 改完代码要部署 | [日常部署与回滚](docs/how-to/日常部署与回滚.md) |
| 发一个新版本 | [发布新版本](docs/how-to/发布新版本.md) |
| 设备出问题了 | [排障手册](docs/how-to/排障手册.md) |
| 查服务 / 路径 / 版本 | [参考文档](docs/reference/) |
| 搞懂某个设计为什么这样 | [说明文档](docs/explanation/) |

**文档中心**：[docs/README.md](docs/README.md)

---

## 常用命令

```bash
# 服务状态
systemctl is-active teslausb-web teslausb-sentry teslausb-gadget

# Web 日志
journalctl -u teslausb-web -n 50 --no-pager

# 部署（改完代码）
python deploy_manager.py deploy -f wifi_service.py -m "修复说明" -y

# 回滚
python deploy_manager.py rollback

# 车机认不出 U 盘？看 UDC 是否为空
cat /sys/kernel/config/usb_gadget/tesla_usb/UDC
```

> Web 服务改完 Python 代码，`systemctl restart` 不够，必须完整三步：
> `stop` → 清 `__pycache__` → `start`

---

## 项目结构

```
├── app.py              Flask 入口
├── config.py           ★ 所有路径常量的唯一来源
├── routes/             Flask 蓝图（10 个功能域）
├── utils/              无状态能力模块（缓存/时间戳/SEI/缩略图决策）
├── services/           systemd 单元 + 运维脚本
├── templates/          Jinja2 模板
├── udev/               USB gadget 规则
├── docs/               文档体系
└── config/             运行时配置（不进 git）
```

---

## 开发约定

### 分支

`main`（生产） · `feat/xxx` · `fix/xxx` · `chore/xxx`

### 提交

遵循 Conventional Commits：

```
feat(sentry): 支持空事件过滤
fix(wifi): 修复 NM connection 命名冲突
chore(cleanup): 删除临时调试脚本
```

### 硬性约束

- **Python 3.9**：不支持 `dict | None`，用 `Optional[dict]`
- **路径统一**：从 `config.py` 导入，禁止硬编码（[M19](docs/reference/教训索引.md)）
- **新增文件**必须加进 `deploy_manager.py` 的 `MANAGED_FILES`，否则不进升级包（[M36](docs/reference/教训索引.md)）
- **前端改动**跑一遍 `node --check static/app.js`
- **`utils/` 模块**不得依赖 Flask 上下文（[M31](docs/reference/教训索引.md)）
- **子进程调用**必须加 TTL 缓存，959MB 内存扛不住进程风暴（[M34](docs/reference/教训索引.md)）

详见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [教训索引](docs/reference/教训索引.md)。

---

## 版本

当前：`v0.3.1.40`（`config.py: APP_VERSION`）

变更历史：[版本历史](docs/reference/版本历史.md) · [GitHub Releases](https://github.com/kkert123/A7Z-TeslaUSB-CL/releases)

发布说明写作规范：[docs/reference/发布说明规范.md](docs/reference/发布说明规范.md)

---

## 许可

内部项目
