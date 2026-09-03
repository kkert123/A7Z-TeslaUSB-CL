# 对话总结 - 2026-05-28

> 会话主题：hardware_watchdog.py / teslausb-gadgetd.py 代码审查、修复与部署

---

## 一、对话背景

用户 cnlvan 正在开发 Radxa A7Z TeslaUSB 项目，本次对话围绕**硬件看门狗**和 **USB Gadget 守护进程** 两个核心模块的 bug 修复、代码审查与部署展开。

---

## 二、硬件看门狗（hardware_watchdog.py）

### 2.1 初始问题
- 用户询问：程序崩溃是否会触发硬件看门狗重启？
- 检查发现：看门狗服务未运行，且无进程喂狗

### 2.2 部署后导致的重启循环（严重事故）

**根因**：`hardware_watchdog.py` 存在多个致命 bug：

| Bug | 影响 |
|-----|------|
| `pet_watchdog()` 用 `with open()` 每次喂狗后关闭 fd | 看门狗未被正确守护，超时即复位 |
| 触发重启前调用 `stop_watchdog()`（写 magic close `'V'`）| 看门狗被安全关闭，无法触发硬件复位 |
| `run_daemon()` 的 `try/except/finally` 包裹 `while True` | 异常后循环退出，`want_reboot` 逻辑失效 |
| 语法错误 `break` outside loop | 脚本无法运行 |

**后果**：A7Z 陷入无限重启循环

**紧急恢复**：
1. 接入串口
2. **先** `sudo systemctl disable teslausb-watchdog`（阻止重启后再次启动）
3. 再 `sudo systemctl stop teslausb-watchdog`（停止当前运行的服务）

### 2.3 代码审查修复清单

| # | 问题 | 修复 |
|---|------|------|
| 1 | `HEALTH_STATUS_FILE` 路径错误 (`/opt/teslausb-web/`) | → `/opt/radxa_data/teslausb/data/` |
| 2 | Logger f-string 格式化（6处）| → 统一 `%s` 风格（logging 推荐，延迟求值）|
| 3 | Docstring 错误提及"树莓派" | → "Linux 标准硬件看门狗" |
| 4 | `cam_path` 硬编码 `/media/cnlvan/cam` | → `/mnt/teslacam` |
| 5 | 每次循环喂狗两次（冗余）| → 简化为一次 |
| 6 | `service_all_ok` 逻辑缺失 | 服务检查失败现在正确设置 `healthy=False` |
| 7 | `Wants=` 多余 | 从 service 文件移除 |
| 8 | `Restart=on-failure` | → `Restart=always`（看门狗是关键服务）|

### 2.4 阈值讨论

| 指标 | 预警阈值 | 重启阈值 | 结论 |
|------|---------|---------|------|
| CPU 负载 | > 80% | > 100% | 当前策略：只预警，不重启 |
| 内存使用 | > 85% | > 95% | 当前策略：只预警，不重启 |

**设计决策**：CPU/内存高只记录，服务不可用才触发重启（避免误重启）

### 2.5 最终状态

- ✅ 代码已修复并通过 `py_compile` 语法检查
- ✅ 已部署到 A7Z (`/opt/radxa_data/teslausb/hardware_watchdog.py`)
- ✅ 服务 `teslausb-watchdog` 已启用并运行
- ⏸️ 用户决定**暂时停用**看门狗，待后期启用
- 📄 设计文档已创建：`docs/watchdog-design.md`

---

## 三、USB Gadget 守护进程（teslausb-gadgetd.py）

### 3.1 初始问题
- 用户问：`teslausb-gadget` 服务是否有自动重启机制？
- 发现：A7Z 上**根本没有 `teslausb-gadget.service`**
- 真相：`teslausb-gadgetd.py` 是**自行管理的守护进程**（不是 systemd 服务），通过 UDC 硬件事件触发

### 3.2 代码审查发现的主要 Bug

| Bug | 严重程度 | 修复 |
|-----|---------|------|
| `daemonize()` 用 `with open()` 导致 stdout/stderr fd 泄漏 | 🔴 Blocker | 改为直接 `open()` 并保持 fd 打开 |
| dict 字面量缺少逗号（多处 `SyntaxError`）| 🔴 Blocker | 全部补上逗号 |
| `state == "not attached"` 重启逻辑被注释掉 | 🔴 Blocker | 取消注释，改为 30s 持续才重启 |
| UDC 为空时每 5s 无脑 restart（重启风暴）| 🔴 Blocker | 加入 `_restart_failures` + 退避阶梯 `[5, 15, 30]s` |
| `start()` 里 `current_mode = "present"` 无 `global` 声明 | 🟡 Major | 补上 `global current_mode` |
| `SCRIPT_PATH` 路径错误 | 🟡 Major | → `/opt/radxa_data/usb_gadget_init.sh` |
| `stop()` 僵尸 PID 文件处理不完整 | 🟡 Major | 进程不存在时清理残留文件 |

### 3.3 部署过程

1. 从 A7Z 拉取 `teslausb-gadgetd.py` 到本地（之前本地版本过时）
2. 完整审查并修复所有 bug
3. 通过 SCP 推送到 A7Z（先传 `/tmp`，再 `sudo mv` 到目标路径）
4. 用自带命令重启守护进程：`sudo python3 teslausb-gadgetd.py restart`
5. 验证：新进程已启动，日志正常输出

### 3.4 与看门狗的冲突

- **问题**：把 `teslausb-gadget` 加入 `CRITICAL_SERVICES` 监控后，导致**反复重启**
- **根因**：`teslausb-gadget` 不是常驻服务（只有接上 Tesla 时才运行），看门狗误判为故障
- **修复**：从 `CRITICAL_SERVICES` 中移除 `teslausb-gadget`

---

## 四、代码审查机制建立

### 4.1 已创建的文档

| 文档 | 路径 | 内容 |
|------|------|------|
| 代码审查标准 | `docs/code-review-standards.md` | L1~L4 审查等级、五维度清单、流程（5阶段）|
| 看门狗设计文档 | `docs/watchdog-design.md` | 工作原理、配置说明、使用指南、故障排查 |

### 4.2 审查流程

```
提交 → L1 自动扫描（语法/格式）→ L2 标准审查 → 报告 → 合并
```

### 4.3 工具推荐

- 语法检查：`py_compile`
- 自动化：`flake8`, `black`, `mypy`
- 安全：`bandit`
- 测试：`pytest`

---

## 五、重要决策记录

| 决策 | 理由 |
|------|------|
| 看门狗暂不启用 | 阈值和触发条件还需要想清楚，避免误重启 |
| `teslausb-gadget` 不加入看门狗监控 | 非常驻服务，会导致误重启 |
| CPU/内存只预警不重启 | 服务可用性优先，资源高消耗不直接重启 |
| gadget 重启加入退避机制 | 避免 UDC 抖动导致重启风暴 |

---

## 六、文件变更清单

### 已修复并部署到 A7Z

| 文件 | A7Z 路径 | 状态 |
|------|---------|------|
| `hardware_watchdog.py` | `/opt/radxa_data/teslausb/hardware_watchdog.py` | ✅ 已部署，暂未启用 |
| `teslausb-watchdog.service` | `/etc/systemd/system/teslausb-watchdog.service` | ✅ 已部署 |
| `teslausb-gadgetd.py` | `/opt/radxa_data/teslausb/teslausb-gadgetd.py` | ✅ 已部署并运行 |

### 本地新建/更新

| 文件 | 状态 |
|------|------|
| `docs/code-review-standards.md` | ✅ 新建 |
| `docs/watchdog-design.md` | ✅ 新建 |
| `.workbuddy/memory/2026-05-27.md` | ✅ 已更新 |
| `.workbuddy/memory/2026-05-28.md` | ✅ 已更新 |
| `.workbuddy/memory/MEMORY.md` | ✅ 已更新（经验教训 #28~#32）|

---

## 七、待办事项

- [ ] 决定看门狗的最终行为（CPU/内存是否触发重启）
- [ ] 启用看门狗时的部署验证测试
- [ ] `usb_gadget_init.sh` 的健壮性审查（错误处理、回滚逻辑）
- [ ] M5 系统集成测试（全功能验证）
- [ ] Git 初始化（Windows 机器安装 Git 后）

---

*总结生成时间：2026-05-28 23:45 CST*
