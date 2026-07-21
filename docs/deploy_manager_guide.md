# A7Z 部署管理器使用指南

> **工具**: `deploy_manager.py` v1.0  
> **更新**: 2026-05-29

---

## 核心概念

```
┌──────────────┐      paramiko SFTP       ┌──────────────────┐
│   本地电脑    │ ◄──────────────────────► │   A7Z 远程        │
│  (Windows)   │                          │ /opt/radxa_data/  │
│              │   备份 → 上传 → 验证     │   teslausb/       │
│  _deploy/    │                          │                    │
│  ├── versions.json   ← 版本索引        │ _backup_deploy/   │
│  └── backups/        ← 历史备份        │   ← 远程备份副本   │
└──────────────┘                          └──────────────────┘
```

**每次部署的 4 步流程**：

```
[1] 备份      →  从远程下载旧文件 → 本地 + 远程双重保存
[2] 上传      →  二进制上传新文件 → SHA256 校验
[3] 记录      →  写入版本号和文件清单
[4] 重启      →  重启 teslausb-web 服务
```

---

## 命令速查

| 命令 | 用途 | 示例 |
|------|------|------|
| `status` | 查看当前状态和远程连接 | `python deploy_manager.py status` |
| `list` | 列出所有历史版本 | `python deploy_manager.py list` |
| `deploy` | 备份+上传+重启 | `python deploy_manager.py deploy -f app.py,config.json -m "修复xxx"` |
| `rollback` | 回滚到历史版本 | `python deploy_manager.py rollback` (回滚到上一个) |
| `rollback v3` | 回滚到指定版本 | `python deploy_manager.py rollback v3` |
| `verify` | 校验远程文件完整性 | `python deploy_manager.py verify` |

---

## 日常场景

### 场景1: 部署两个文件（最常用）

```bash
python deploy_manager.py deploy -f auto_cleanup.py,app.py -m "cleanup v2 升级"
```

**输出示例**：
```
============================================================
📋 部署计划
============================================================
消息: cleanup v2 升级
文件 (2): auto_cleanup.py, app.py
目标: radxa@100.116.18.42:/opt/radxa_data/teslausb
服务重启: teslausb-web
============================================================

⚠️ 确认部署？[y/N]: y

[1/4] 备份远程文件 → 版本 v20260529_003400
📦 远程备份中...
  ✅ auto_cleanup.py (36.1 KB)
  ✅ app.py (141.3 KB)

[2/4] 上传新文件
📤 部署中 (版本 v20260529_003400)...
  ✅ auto_cleanup.py (36.1 KB, 校验通过)
  ✅ app.py (141.3 KB, 校验通过)

[3/4] 记录版本
  ✅ 版本 v20260529_003400 已记录

[4/4] 重启服务
🔄 重启服务...
  ✅ teslausb-web: active

============================================================
✅ 部署完成 - 版本 v20260529_003400
   回滚命令: python deploy_manager.py rollback v1
============================================================
```

### 场景2: 跳过确认（脚本化部署）

```bash
python deploy_manager.py deploy -f config.json -m "更新清理策略" -y
```

### 场景3: 一键回滚

```bash
# 回滚到上一个版本
python deploy_manager.py rollback

# 回滚到指定版本
python deploy_manager.py rollback v3
```

回滚会从本地备份中取出旧文件，上传到 A7Z，校验完整性，然后重启服务。

### 场景4: 部署前查看状态

```bash
python deploy_manager.py status
```

**输出示例**：
```
============================================================
📊 部署状态
============================================================

当前版本: v3 (v20260529_140000)
消息: cleanup v2 + config update
时间: 2026-05-29T14:00:00
文件: auto_cleanup.py, app.py, config.json

远程连接:
  ✅ radxa@100.116.18.42
     14:00:00 up 3 days, 2:15, 1 user, load average: 0.08, 0.12, 0.10
     teslausb-web: active

版本历史: 3 个版本
  v3 v20260529_140000 - cleanup v2 + config update ← 当前
  v2 v20260529_120000 - app.py fix
  v1 v20260529_003400 - cleanup v2 升级
```

### 场景5: 部署后发现 bug，回滚

```bash
# 1. 先看有哪些版本
python deploy_manager.py list

# 2. 回滚到上一个正常版本
python deploy_manager.py rollback
```

### 场景6: 定期校验文件是否被篡改

```bash
python deploy_manager.py verify
```

---

## 故障排查

### A7Z 连不上

```
❌ 无法连接到 A7Z (尝试了 ['100.116.18.42', '192.168.0.102']): timed out
```

**原因**：车休眠了，USB 断电，A7Z 关机。

**处理**：
1. 走近车 → 开门唤醒 → 等 30 秒 A7Z 启动
2. 在车上开 Dog Mode 或 Camp Mode 保持供电
3. 重新执行部署命令

### 部署失败但备份已保存

```
❌ 部署失败 - 连接错误: timed out
   备份已保存到 _deploy/backups/v20260529_XXXXXX/
   可以稍后手动重试或回滚
```

**处理**：等 A7Z 上线后，重新执行部署（备份不会重复消耗时间）。

### 回滚后文件校验不匹配

```
⚠ auto_cleanup.py 校验不匹配
```

**处理**：手动检查远程文件状态，可能需要重新部署。

### 回滚命令未指定版本

```
python deploy_manager.py rollback
```

默认回滚到**当前版本的上一个版本**。如果只有一个版本，会提示"当前已是最早版本"。

---

## 配置说明

### 环境变量（可选）

```bash
# 修改连接信息
export A7Z_HOST="100.116.18.42"
export A7Z_HOST_FALLBACK="192.168.0.102"
export A7Z_USER="radxa"
export A7Z_PASSWORD="radxa"
```

### 管理文件列表

在 `deploy_manager.py` 的 `Config.MANAGED_FILES` 中定义，只有白名单中的文件才能部署：

```python
MANAGED_FILES = [
    "app.py",
    "auto_cleanup.py",
    "config.json",
    "config.py",
    "config_manager.py",
    # ...
]
```

### 部署后重启的服务

```python
RESTART_SERVICES = ["teslausb-web"]
```

---

## 版本库结构

```
_deploy/
├── versions.json              ← 版本索引文件
└── backups/
    ├── v20260529_003400/      ← 版本 1
    │   ├── manifest.json      ← SHA256 文件清单
    │   ├── auto_cleanup.py    ← 备份的旧文件
    │   └── app.py
    ├── v20260529_120000/      ← 版本 2
    │   ├── manifest.json
    │   └── app.py
    └── v20260529_140000/      ← 版本 3（当前）
        ├── manifest.json
        ├── auto_cleanup.py
        ├── app.py
        └── config.json
```

**注意**：`_deploy/` 目录不应加入 Git（文件太大），已在 `.gitignore` 中排除。

---

## 最佳实践

1. **改了什么就部署什么**，不要批量部署不相关的文件
2. **`-m` 消息写清楚原因**，方便回滚时识别版本
3. **部署后观察日志 5 分钟**，确认服务正常再离开
4. **定期 `verify`**，确保远程文件未被意外修改
5. **至少保留最近 3 个版本**，不要手动删除 `_deploy/backups/`
