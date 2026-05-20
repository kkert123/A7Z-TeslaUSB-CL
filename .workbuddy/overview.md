# Task 4.1: 视频同步与归档 — 部署完成报告

## 完成时间
2026-05-16 10:12

## 部署概览

| # | 文件 | 状态 |
|---|------|------|
| 1 | `sync_service.py` (370行) | ✅ |
| 2 | `tesla_sync.sh` (70行) | ✅ |
| 3 | `config/sync.json` | ✅ |
| 4 | `90-tesla-sync` (NM dispatcher) | ✅ |
| 5 | `teslausb-sync.service` | ✅ |
| 6 | `teslausb-sync.timer` | ✅ |
| 7 | `app.py` (4 API 路由) | ✅ |

## 架构

```
触发方式（三种）:
  NM dispatcher ── wlan0 up + SSID 匹配
  systemd timer  ── 每 30min 兜底
  Web 手动触发   ── POST /api/sync/trigger

同步流程:
  前置检查 → mount.cifs → rsync(3次重试) → 清理 → umount → 微信通知
  保护: Present Mode 跳过 / 5min 冷却期 / NAS 可达性检查
```

## API 端点

| 端点 | 方法 | 状态 |
|------|------|------|
| `/api/sync/status` | GET | 200 ✅ |
| `/api/sync/history` | GET | 200 ✅ |
| `/api/sync/trigger` | POST | 200 ✅ (优雅跳过: "未启用") |
| `/api/sync/config` | GET/POST | 200 ✅ |

## ⚠️ 待用户配置

同步系统已部署但**未激活**。请在 A7Z 上编辑配置后再启用：

```bash
# SSH 到 A7Z，编辑配置
sudo nano /opt/radxa_data/teslausb/config/sync.json
```

修改以下字段：
```json
{
    "enabled": true,
    "nas_ip": "你的NAS_IP",
    "nas_share": "共享名",
    "nas_user": "用户名",
    "_nas_pass": "密码",
    "home_ssid": "家庭WiFi_SSID"
}
```

或通过 Web API：
```bash
curl -X POST http://100.116.18.42:5000/api/sync/config \
  -H "Content-Type: application/json" \
  -d '{"enabled":true, "nas_ip":"x.x.x.x", ...}'
```

配置保存后，系统会自动：
1. 连接家庭 WiFi → NM dispatcher 触发同步
2. 每 30min 定时器兜底检查
3. 同步完成 → 企业微信推送通知
4. 删除 A7Z 上 7 天前的本地视频
