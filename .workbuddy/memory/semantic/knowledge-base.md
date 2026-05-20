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
1. **Windows 换行符问题**: 从 Windows 上传脚本必须用 `dos2unix`
2. **部署脚本化**: 多步骤操作写成脚本，避免手动失误
3. **备份优先**: 修改前必须备份，格式：`filename.backup.YYYYMMDD_HHMMSS`
4. **API 路径一致性**: JavaScript 调用的路径必须和 Flask 路由完全一致

---

_本文件由 Memory System Optimizer 自动维护_
