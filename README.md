# A7Z TeslaUSB 哨兵云端备份系统

基于 Raspberry Pi / Radxa 的 TeslaUSB 改造项目：自动抓取特斯拉哨兵模式（Sentry） clips，
生成缩略图预览，通过企业微信推送通知，并可选地上传到 NAS / 云端存储。

> ⚠️ **安全提示**：本项目包含密钥（企业微信 webhook key 等）。真实配置文件 **不会** 进入本仓库
> （已被 `.gitignore` 忽略）。请只提交 `*.example.json` 模板，切勿把 `config/sentry.json`、
> `weixin_config.json`、`config.json` 提交到任何公开仓库。

## 功能

- 哨兵 clips 自动扫描与缩略图预览生成
- 企业微信机器人推送（状态 / 哨兵事件）
- 离家/到家位置识别（WiFi SSID / 热点）
- 可选 NAS / 云存储上传（rclone）
- 可选 TeslaMate 位置联动
- USB Gadget 模式（把设备模拟成 U 盘供车机读取）

## 目录结构

```
app.py                   Flask 主入口
config.py / config_manager.py   配置加载
routes/                  Flask 蓝图（页面与 API）
templates/ static/       前端页面与资源
services/                 systemd 服务单元
utils/                   通用工具
*_service.py             各功能模块（sentry / cloud / weixin / wifi ...）
config/                  配置目录（*.example.json 为模板，*.json 为本地真实配置）
cedar_composer/          灯光秀（lightshow）C 源码
docs/                    使用文档
```

## 环境要求

- Python 3.8+
- （可选）Node.js + npm：用于 Playwright 缩略图预览生成
- （可选）rclone：用于 NAS / 云存储上传

## 快速开始

```bash
git clone <你的仓库地址>
cd A7Z-TeslaUSB-CL-publish
chmod +x install.sh
./install.sh
```

脚本会：安装 Python 依赖、从模板生成 `config/sentry.json` 与 `weixin_config.json`、
（可选）安装 Playwright。

## 配置

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

## 运行

```bash
source venv/bin/activate
python app.py
```

## 安全与发布须知

- 真实配置文件已在 `.gitignore` 中忽略，**不要**手动 `git add` 它们。
- 如果你是从别处接手本项目：发布前务必确认 git 历史里没有真实密钥
  （可用 `git log -p -- config/sentry.json` 自查，必要时轮换密钥）。
- 本仓库为“干净导出”版本，已剔除一次性热修/调试脚本与运行产物。

## 免责声明

本项目仅供个人学习与非商业用途。使用风险由使用者自行承担。
