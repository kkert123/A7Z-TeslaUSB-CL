# TeslaUSB A7Z 代码审查标准

> 制定日期: 2026-05-27  
> 适用范围: Radxa A7Z TeslaUSB 项目（Python 3.9+, Flask, systemd, shell）

---

## 1. 审查原则

1. **正确性优先** — 能跑比好看重要，但跑不对等于没跑
2. **防御性编程** — 嵌入式设备，任何异常都不能导致静默失败
3. **资源管理严格** — fd、PID 文件、Unix socket，必须成对管理
4. **日志可排障** — 现场出问题只能看日志，日志质量 = 排障速度
5. **最小权限** — 不需要 root 的不要用 root

---

## 2. 审查清单

### 🔴 Blocker（禁止合并）

| 检查项 | 说明 |
|--------|------|
| 语法错误 | 所有 `dict` 字面量逗号、`os.path.join()` 参数逗号 |
| 未处理异常路径 | `open()` / `subprocess.run()` / `os.kill()` 必须 try/except |
| 资源泄漏 | fd / PID 文件 / socket 文件必须有对称的创建和清理 |
| 竞争条件 | PID 文件存在但进程已死，必须 check_pid 再 trust |
| 硬编码路径 | `/opt/radxa_data/...` 以外路径必须走配置 |
| 阻塞操作无超时 | 任何 `subprocess` / `requests` / `socket.accept` 必须设 timeout |
| 特权操作无保护 | `sudo` / `os.kill(0)` 必须验证权限 |

### 🟡 Suggestion（应该修复）

| 检查项 | 说明 |
|--------|------|
| Logger 格式化 | 统一用 `logger.info("msg: %s", var)`，不用 f-string |
| 魔法数字 | `5`, `16`, `30` 等必须提取为常量 |
| 函数复杂度 | 单函数 > 50 行考虑拆分 |
| 注释语言 | 关键逻辑必须有中文注释（项目规范） |
| 类型注解 | Python 3.9 不支持 `dict | None`，必须用 `Optional[dict]` |

### 💭 Nit（可选）

- 命名风格一致性（`snake_case` vs `camelCase`）
- 文档字符串完整性
- 不必要的 `global` 声明

---

## 3. 本项目特殊规则

### 3.1 守护进程（daemonize）规范

- `daemonize()` 必须用 `os.open()` 而不是 `with open()` 保持 fd 打开
- 第二次 fork 后必须重定向 stdout/stderr 到日志文件
- PID 文件必须包含 `os.getpid()` 的真实 PID

### 3.2 硬件看门狗规范

- 看门狗 fd 必须在整个进程生命周期内保持打开
- 触发重启前**禁止**调用 `stop_watchdog()`（magic close 会取消重启）
- `want_reboot=True` 后停止喂狗，让硬件超时复位

### 3.3 systemd 服务规范

- `Restart=on-failure` vs `always` 必须根据服务性质选择
- `After=` 必须列出所有依赖服务
- `StandardOutput=` 推荐用 `journal` 而不是 append 文件

### 3.4 Gadget 模式规范

- `teslausb-gadget` 不是常驻服务，不列入看门狗监控
- `usb_gadget_init.sh` 的 umount 操作不能破坏 `present_usb.sh` 的只读挂载
- UDC 监控必须有退避逻辑，防止重启风暴

---

## 4. 审查流程

```
提交代码
  ↓
L1: 自动扫描（py_compile + pyflakes）
  ↓ 通过
L2: 标准审查（本清单）
  ↓ 通过
L3: 深度审查（架构/性能/边界条件）
  ↓ 通过
L4: 上线前全检（实际部署验证）
```

---

## 5. 审查评论模板

```
🔴 **Blocker: [问题标题]**
Line XX: [具体代码位置]

**Why:** [为什么是问题，不修会怎样]

**Suggestion:**
[具体修复方案，最好附代码]
```

---

## 6. 常见错误速查表

| 错误模式 | 正确写法 |
|---------|-----------|
| `{"a": 1 "b": 2}` | `{"a": 1, "b": 2}` ← 缺逗号 |
| `os.path.join(a b)` | `os.path.join(a, b)` ← 缺逗号 |
| `with open(fd) as f:` in daemonize | `fd = open(...)` keep open |
| `global x` 在局部赋值但无声明 | 在函数开头声明 `global x` |
| `logger.error(f"msg {var}")` | `logger.error("msg: %s", var)` |
| `except:` bare | `except Exception as e:` |
| `os.kill(pid, 0)` 不检查 ProcessLookupError | `try: os.kill(...)\nexcept OSError: ...` |
