# 知识库 - Knowledge Base

## 项目知识

### TeslaUSB 项目架构
- **硬件架构**: Radxa Cubie A7Z + NVMe SSD
- **软件架构**: Flask Web + systemd 服务 + Shell 脚本
- **数据流**: Tesla 摄像头 → NVMe 存储 → 预览生成 → 企业微信推送

### 关键技术点
1. **USB Gadget**: 模拟 USB 存储设备，让 Tesla 识别
2. **Preview 生成**: 四宫格预览 + 单张缩略图
3. **企业微信推送**: Webhook 双机器人机制
4. **位置检测**: TeslaMate API + WiFi SSID 双重验证
5. **AP 回连（v0.3.1.54 起）**: hostapd 事件驱动——`hostapd.conf` 含 `ctrl_interface=/var/run/hostapd`，`sudo hostapd_cli -a /opt/radxa_data/teslausb/ap_client_event.sh -i wlan0` 常驻监听；`AP-STA-DISCONNECTED` → 写 `/var/run/ap-yield-pending` + 15s 宽限（重连则清标记取消）→ 复查无 Station 后执行 `wifi_service.py --yield-ap`（flock + 共用 `_ap_bring_down`）；`AP-STA-CONNECTED` 清标记。回调参数约定：**`$1`=事件名、`$2`=MAC**
6. **timer 活性可观测（v0.3.1.54）**: `wifi_service.py` 每次 quick/full 运行写 `/var/run/wifi_check_last_trigger`；sentry 常驻线程每 60s 检查 mtime，超 10min → restart 两个 wifi timer + 微信告警（防抖 30min）
7. **版本检测缓存（M63）**: `version_service.check_latest_release()` 成功缓存 6h、失败 30min、距上次请求 <10min 返回缓存、配额余量硬护栏；UI「检查更新」不带 force → 发版后设备最长 6h 才可见新版
8. **USB 链路守护配置（v0.3.1.61 起）**: 配置存 `config/sentry.json` 的 `usb_guard` 键；**独立端点** `GET/POST /api/system/usb-guard-config`（已从 `/api/system/push-config` 解耦），前端配置块位于「USB 模式」组（`grp-usb`）自带「保存」按钮；保存后**回读服务端**比对 `allow_platform_rebind`/`enabled` 一致才提示成功

---

## 故障排除知识

### 常见问题速查

| 问题 | 症状 | 原因 | 解决方案 |
|------|------|------|----------|
| CPU 使用率显示 0% | 仪表盘显示 0% | 缺少 `import time` + 测量间隔太短 | 添加导入 + 改为 0.5秒 |
| API 返回 404 | `/api/system-stats` 404 | JavaScript 调用路径错误 | 改为 `/api/system/stats` |
| 自动刷新不工作 | 数据不更新 | `d.sys` 应该是 `d.sys_stats` | 修复 JavaScript 数据路径 |
| systemd 服务启动失败 | `status=203/EXEC` | Windows 换行符问题 | 使用 `dos2unix` 转换 |
| Samba 配置警告 | 启动时警告 | `unix password sync = yes` | 改为 `no` |
| 升级失败 "No module named 'upgrade_service'" | 手动升级报错 | 升级包漏打 upgrade_service.py（白名单不全）→ 设备目录缺该模块 → 无法自举升级 | 发版前对照历史完整包 diff；补传缺失文件 + 重建完整包 |
| 补发哨兵通知无缩略图 | 网络恢复后补发只有文字 | `send_sentry_detected` 返回 text_success，图片上传失败（网络恢复初期 ConnectionError）被吞掉 → 队列误判成功删除事件 | v0.3.1.29 起图文全成功才成功；`skip_text`/`text_sent` 只补图重试 |
| git push Connection reset / Empty reply | push/ls-remote 失败，curl api.github.com 却正常 | github.com:443 被网络阻断（DNS 默认 IP 不可达） | 生成 Deploy Key 走 SSH（22 端口）；`~/.ssh/config` HostName 用可达 IP 绕开 DNS |
| `ssh-keygen -Y verify` 报 "Could not verify signature" | 签名文件存在、包未改动 | allowed_signers 行格式写反 | 正确格式 `<principal> <keytype> <base64>`（principal 在前，如 `a7z-upgrade ssh-ed25519 AAAA…`） |
| 无法下载 Release 资产核对 SHA256 | 直链 curl 超时/无文件 | github.com:443 被阻断（M48 场景） | 无需下载：读 `GET /releases/assets/{id}` 的 `digest` 字段（GitHub 服务端 sha256），与本地 `sha256sum`、body 声明三方比对 |
| 设备显示版本低于预期 | `/api/version/check` 的 latest 已是新版但 current 落后 | 设备实际部署版本未跟上（不是检测问题） | 以 `current` 字段为准；查 `rollback/options` 有无该版本安装痕迹，无则说明升级从未成功 |
| 「启用 L2.5 平台重绑」勾选保存不上、刷新后又没勾上 | 勾选后刷新还原 | （v0.3.1.61）**非后端缺陷**——`usb_guard` 配置块混在「推送与报警」组、该组唯一「保存」按钮在组顶挨着 TPMS 输入框，用户误以为只管 TPMS；且 `push-config` POST 要求同时带 `tpms_alarm_threshold`+`push_templates`，无法单独存 `usb_guard` | 配置块迁入就近的「USB 模式」组 + 新增**独立保存按钮** + 独立端点 `POST /api/system/usb-guard-config`；保存后回读校验 |
| 一键升级后客户端 curl 收到空响应 / 疑似失败 | 发起升级的请求无返回 | （v0.3.1.61，M92）升级**成功**但从下载到切 symlink 耗时约 7 分钟（`.sig` 优先走 `ghproxy.net` 镜像，镜像 stall 时阻塞 300s 再切直连）→ 超客户端超时 | 判据只看 `GET /api/version/upgrade/progress`（running）+ `readlink /opt/radxa_data/teslausb` + `config.py:APP_VERSION`；**禁并发升级**（共用 `/tmp/upgrade-v<ver>.tar.gz` 互踩 → 假性「SHA-256 校验失败：文件不存在」） |
| 设备文件与 git blob 比对整文件全红 | 逐行全红但行数相同 | （v0.3.1.61，M93）行尾差异——设备/包内文件是 CRLF，`git show` 出的是 LF | 先判行尾 `grep -qU $'\r'`；比对用 `diff --strip-trailing-cr` 或归一化后再哈希（内容其实完全一致） |

### 发布推送通道（2026-08-23 起）
- **常规**：HTTPS + PAT（`.github-pat`），`api.github.com` 与 `github.com` 均可达时使用
- **Fallback**：SSH + Deploy Key（`~/.ssh/a7z_github`，已加写权限到仓库）+ `~/.ssh/config` HostName 140.82.112.3
- Release 创建/资产上传始终走 API + PAT（uploads.github.com）

---

## 代码模式

### Flask API 路由模式
```python
@app.route('/api/system/stats')
def get_system_stats():
    return jsonify({
        'success': True,
        'sys_stats': {...},
        'service': {...},
        'ip': {...}
    })
```

### JavaScript 自动刷新模式
```javascript
setInterval(async () => {
    const r = await fetch('/api/system/stats');
    const d = await r.json();
    if (d.success) {
        const s = d.sys_stats;
        // 更新 DOM
    }
}, 30000);
```

---

## 配置模板

### systemd 服务模板
```ini
[Unit]
Description=TeslaUSB Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/radxa_data/teslausb
ExecStart=/usr/bin/python3 -u app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## 经验教训

### 已验证的经验
1. **Windows 换行符问题**: 从 Windows 上传脚本必须用 `dos2unix`；**比对文本文件须忽略 CRLF/LF**（`diff --strip-trailing-cr`），否则整文件全红被误判为"内容漂移"（M93）
2. **部署脚本化**: 多步骤操作写成脚本，避免手动失误
3. **备份优先**: 修改前必须备份，格式：`filename.backup.YYYYMMDD_HHMMSS`
4. **API 路径一致性**: JavaScript 调用的路径必须和 Flask 路由完全一致
5. **「保存不上」先怀疑 UI 而非后端**（M92 族）：能复现"勾选→刷新还原"时，先实测 `curl` 端点往返；若后端正常，多半是**缺少就近保存入口**或**该请求无法单独携带该字段**（V61 案）
6. **长耗时远程操作判据看目标状态**（M92）：升级/部署类请求可能耗时数分钟，客户端超时/空响应 ≠ 失败；只看进度接口 + 落到实处的产物（symlink/版本号）；**禁并发**（共用临时路径互踩）
7. **守卫/断言自身也是代码**（M91）：预检脚本也会拼错 URL、写错判据；上线前必须用真实请求实证"该亮绿灯时确实绿灯"，不能只在坏输入上验过

---

_本文件由 Memory System Optimizer 自动维护_
