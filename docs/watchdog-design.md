# 硬件看门狗开发文档

## 概述

硬件看门狗（Hardware Watchdog）是 TeslaUSB 系统的最后一道防线，在系统或关键服务严重故障时自动重启设备，保障系统自动恢复能力。

## 工作原理

```
┌─────────────────┐   喂狗(每5秒)    ┌──────────────────┐
│  hardware_      │ ──────────────────→ │  /dev/watchdog  │
│  watchdog.py    │                      │  硬件定时器        │
│  (用户态守护进程)│ ←────────────────── │  超时 = 16秒     │
└─────────────────┘   不喂狗(>16秒)    └──────────────────┘
        │                                     │
        │ 如果守护进程崩溃，无人喂狗              │ 16秒后硬件自动复位
        ▼                                     ▼
   systemd Restart=always               A7Z 自动重启
   自动重启守护进程
```

**关键机制：**
- 看门狗设备 `/dev/watchdog` 打开后，硬件定时器开始倒计时（16秒）
- 用户态程序必须定期写入数据（喂狗）重置定时器
- 如果超过 16 秒没有喂狗 → 硬件强制复位（无需软件参与）

## 文件结构

```
hardware_watchdog.py       # 看门狗守护进程（主程序）
services/
  teslausb-watchdog.service  # systemd 服务配置
docs/
  watchdog-design.md         # 设计文档（本文档）
```

## 配置

### 健康检查阈值（hardware_watchdog.py 头部常量）

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `CPU_LOAD_THRESHOLD` | 80% | CPU 负载超过此值记录 issue |
| `CPU_LOAD_CRITICAL` | 100% | CPU 负载超过此值标记 unhealthy |
| `MEMORY_THRESHOLD` | 85% | 内存使用超过此值记录 issue |
| `MEMORY_CRITICAL` | 95% | 内存使用超过此值标记 unhealthy |
| `DISK_THRESHOLD` | 95% | 磁盘使用超过此值记录 issue |
| `RESPONSE_TIMEOUT` | 3s | 服务响应超时（秒）|
| `HEALTH_CHECK_INTERVAL` | 5s | 健康检查间隔（秒），必须 < 16s |

### 关键服务列表

```python
CRITICAL_SERVICES = [
    "teslausb-web",      # Web 管理界面
    "teslausb-sentry",    # 哨兵事件监控
    # 注意：teslausb-gadget 不列入！
    # 它只在 USB 连接 Tesla 时运行，非常驻服务
]
```

### systemd 服务配置

```ini
[Unit]
Description=TeslaUSB Hardware Watchdog Daemon
After=network.target teslausb-web.service teslausb-sentry.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 -u /opt/radxa_data/teslausb/hardware_watchdog.py --daemon --interval 5
Restart=always           # 看门狗是关键服务，无论退出码都重启
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 使用说明

### 启用看门狗

```bash
# 1. 启用并启动服务
sudo systemctl enable teslausb-watchdog
sudo systemctl start teslausb-watchdog

# 2. 验证状态
sudo systemctl status teslausb-watchdog

# 3. 查看实时日志
sudo journalctl -u teslausb-watchdog -f
```

### 停用看门狗（维护模式）

```bash
# ⚠️ 正确顺序：先 disable，再 stop
sudo systemctl disable teslausb-watchdog
sudo systemctl stop teslausb-watchdog
```

**为什么先 disable？**
- 如果先 stop，systemd 会立即重启它（因为 `Restart=always`）
- 先 disable 可防止 systemd 自动重启

### 手动触发重启（测试用）

```bash
# 方法1：停止关键服务，等 15 秒（3次 × 5秒间隔）
sudo systemctl stop teslausb-web
# 等待看门狗触发重启

# 方法2：强制杀死看门狗进程（无人喂狗，16秒后硬件复位）
sudo pkill -f hardware_watchdog.py
```

### 查看看门狗状态

```bash
# 查看看门狗设备是否被打开（正在喂狗）
sudo ls -l /proc/$(pgrep -f hardware_watchdog)/fd | grep watchdog

# 查看看门狗守护进程日志
sudo journalctl -u teslausb-watchdog -n 50

# 手动执行一次健康检查
sudo python3 /opt/radxa_data/teslausb/hardware_watchdog.py --check
```

## 设计决策

### 1. 为什么不用软件看门狗（softdog）？

| 方案 | 优点 | 缺点 |
|------|------|------|
| **硬件看门狗** | 真正硬件复位，即使内核崩溃也能恢复 | 需要正确的喂狗逻辑 |
| **软件看门狗** | 实现简单 | 内核崩溃时无效 |

**结论：** A7Z 有硬件看门狗（`/dev/watchdog`），优先使用硬件方案。

### 2. 为什么 `teslausb-gadget` 不列入监控？

- `teslausb-gadget` 是 **USB Gadget 模式切换脚本**，不是常驻服务
- 它只在 Tesla 插入/拔出时运行，平时不运行
- 列入监控会导致误判为"服务失败"，触发不必要的重启

### 3. 重启策略：3 次失败 × 5 秒 = 15 秒

```
第1次检查失败 → consecutive_failures = 1
第2次检查失败 → consecutive_failures = 2
第3次检查失败 → consecutive_failures = 3 → 触发重启
```

**为什么是 3 次？**
- 避免瞬时故障导致误重启
- 15 秒足够让临时故障恢复（如网络抖动）
- 15 秒 < 16 秒（看门狗超时），确保重启前不会硬件复位

### 4. Magic Close 功能

```python
def stop_watchdog(self):
    """安全关闭看门狗（写入 magic char 'V' 后 close）"""
    if self.watchdog_fd is not None:
        os.write(self.watchdog_fd, b"V")  # Magic Close
        os.close(self.watchdog_fd)
        self.watchdog_fd = None
```

**Magic Close 原理：**
- 向 `/dev/watchdog` 写入字符 `'V'`，告诉硬件"我要主动关闭看门狗"
- 硬件收到 `'V'` 后，停止定时器（不再强制复位）
- 这是**安全关闭**看门狗的唯一方法

**⚠️ 注意：**
- 如果守护进程**崩溃**（没有执行 Magic Close），硬件会在 16 秒后强制复位
- 这是预期行为：**守护进程崩溃 → 无人喂狗 → 硬件复位**

## 故障排查

### 问题1：A7Z 反复重启

**症状：** 设备启动后不久自动重启，循环不断。

**原因：**
1. 看门狗代码有 bug，导致误判为"需要重启"
2. 关键服务没有运行，且 `CRITICAL_SERVICES` 列表配置错误

**排查步骤：**

```bash
# 1. 通过串口连接 A7Z（网络可能不通）
screen /dev/ttyUSB0 115200

# 2. 在启动过程中，快速执行（必须在看门狗启动前完成）
sudo systemctl disable teslausb-watchdog

# 3. 重启设备
sudo reboot

# 4. 修复看门狗代码
# 5. 重新启用看门狗
sudo systemctl enable teslausb-watchdog
sudo systemctl start teslausb-watchdog
```

### 问题2：看门狗启动失败

**症状：** `systemctl status teslausb-watchdog` 显示 `failed`。

**原因：**
1. `/dev/watchdog` 设备不存在（内核未加载看门狗驱动）
2. 权限不足（无法打开 `/dev/watchdog`）

**排查步骤：**

```bash
# 1. 检查看门狗设备是否存在
ls -l /dev/watchdog*

# 2. 检查内核模块是否加载
lsmod | grep watchdog

# 3. 手动测试看门狗
sudo python3 /opt/radxa_data/teslausb/hardware_watchdog.py --check
```

### 问题3：健康检查日志看不到

**症状：** `journalctl -u teslausb-watchdog` 只能看到启动日志，看不到健康检查记录。

**原因：** 日志级别设置为 `INFO`，而健康检查通过的日志是 `logger.debug()`（DEBUG 级别）。

**解决方法：**

```bash
# 方法1：修改代码，将健康检查日志改为 INFO 级别
# 在 hardware_watchdog.py 中：
logger.info("健康检查通过，CPU: %.1f%%, 内存: %.1f%%", ...)

# 方法2：修改 systemd 服务，添加 -v 参数
# 在 teslausb-watchdog.service 中：
ExecStart=/usr/bin/python3 -u /opt/radxa_data/teslausb/hardware_watchdog.py --daemon --interval 5 -v
```

## 开发进度

### ✅ 已完成

- [x] 硬件看门狗设备驱动验证（`/dev/watchdog` 可用）
- [x] 看门狗守护进程基本框架（`hardware_watchdog.py`）
- [x] 系统健康检查（CPU、内存、磁盘、温度）
- [x] 关键服务监控（teslausb-web、teslausb-sentry）
- [x] 网络连通性检查
- [x] Web 服务健康检查
- [x] 自动重启机制（连续 3 次失败触发）
- [x] systemd 服务配置（`teslausb-watchdog.service`）
- [x] 日志输出优化（改为 `%s` 风格，避免 f-string 性能问题）
- [x] 代码审查与 bug 修复（异常处理结构、fd 管理、路径错误等）

### 🔵 待完成

- [ ] CPU/内存高负载触发重启的阈值调整（当前阈值过高，实际很难触发）
- [ ] 看门狗服务启用后的集成测试（模拟各种故障场景）
- [ ] 看门狗与系统其他服务的依赖关系优化（确保启动顺序正确）
- [ ] 看门狗日志轮转配置（避免 `/var/log/teslausb-watchdog.log` 过大）

### 📝 已知问题

1. **`teslausb-gadget` 误列入监控导致反复重启**
   - **状态：** ✅ 已修复（从 `CRITICAL_SERVICES` 移除）
   - **原因：** `teslausb-gadget` 不是常驻服务，误判为故障

2. **看门狗 fd 管理错误（每次喂狗都重新开关）**
   - **状态：** ✅ 已修复（改为保持 fd 常开）
   - **原因：** 原代码使用 `with open()` 导致每次喂狗后 fd 被 close

3. **触发重启前调用 `stop_watchdog()`（Magic Close）**
   - **状态：** ✅ 已修复（移除 `stop_watchdog()` 调用）
   - **原因：** Magic Close 会关闭看门狗，无法触发硬件复位

4. **`run_daemon()` 异常处理结构错误**
   - **状态：** ✅ 已修复（重构为 `while True` 内部套 `try/except`）
   - **原因：** 原代码 `try/except/finally` 包裹 `while True`，异常后循环退出

## 参考文档

- [Linux Watchdog 驱动文档](https://www.kernel.org/doc/html/latest/watchdog/)
- [systemd.service 手册](https://www.freedesktop.org/software/systemd/man/systemd.service.html)
- [A7Z 硬件规格](./hardware-spec.md)（待创建）

---

**最后更新：** 2026-05-27
