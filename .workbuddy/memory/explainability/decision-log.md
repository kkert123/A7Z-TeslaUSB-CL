# 可解释性 - Explainability

## 决策透明度

### 决策记录模板
**日期**: YYYY-MM-DD
**决策**: [做了什么决定]
**原因**: [为什么这样做]
**替代方案**: [考虑过但放弃的方案]
**预期结果**: [期望达到什么效果]
**风险评估**: [可能的问题和应对]

---

## 代码可解释性

### 关键函数说明
| 函数名 | 功能 | 输入 | 输出 | 副作用 |
|--------|------|------|------|--------|
| `get_cpu_percent()` | 获取 CPU 使用率 | 无 | float (0-100) | 无 |
| `get_system_stats()` | 获取系统统计 | 无 | JSON | 无 |
| `get_system_uptime()` | 获取运行时间 | 无 | string | 无 |

### 复杂逻辑解释
**自动刷新机制**:
1. 前端 JavaScript 每30秒调用 `/api/system/stats`
2. 后端 Flask 路由执行系统命令获取实时数据
3. 数据通过 JSON 返回前端
4. 前端更新 DOM 元素显示

---

## 错误可解释性

### 常见错误速查
| 错误信息 | 可能原因 | 排查步骤 | 解决方案 |
|----------|----------|----------|----------|
| `404 Not Found` | API 路径错误 | 检查路由定义 + JS 调用路径 | 统一路径格式 |
| `status=203/EXEC` | 脚本格式错误 | `file` 命令检查换行符 | `dos2unix` 转换 |
| `null` 返回值 | 数据路径错误 | 检查 API 返回结构 | 使用正确的嵌套路径 |

---

## 用户可理解性

### 技术术语解释
- **API**: 应用程序接口，前后端通信方式
- **JSON**: 数据交换格式，类似字典/对象
- **systemd**: Linux 服务管理工具
- **Flask**: Python Web 框架

### 操作说明模板
**如何重启服务**:
```bash
# 1. 重启服务
systemctl restart teslausb-web.service

# 2. 检查状态
systemctl status teslausb-web.service

# 3. 查看日志
journalctl -u teslausb-web.service -f
```

---

_本文件由 Memory System Optimizer 自动维护_
