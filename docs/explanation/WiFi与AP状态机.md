# WiFi 与 AP 状态机

> A7Z 有两种网络角色：连家里/手机热点的 **station**，和给别人配网用的 **AP 热点**。
> 两者互斥（AIC8800 单射频），切换逻辑是 `wifi_service.py` 里最复杂的一块。

---

## 两种模式

| | station 模式 | AP 模式 |
|---|---|---|
| 用途 | 正常联网：推送、云归档、远程访问 | 手机连上来配 WiFi |
| 服务 | NetworkManager 管 wlan0 | hostapd + dnsmasq |
| 触发进入 | 开机、网络恢复 | 检测不到已保存 WiFi 时兜底开启 |

**AP 是兜底手段，不是常态。** 正常情况下 A7Z 应该待在 station 模式。

---

## 检测节奏

两套 timer 驱动（见 [服务清单](../reference/服务清单.md)）：

| 检查 | 周期 | 干什么 |
|------|------|--------|
| `quick_check` | 短 | 连通性探测：通 → 确保 AP 关闭；不通 → 累加失败计数 |
| `full_check` | 长 | 全量扫描 + 最优网络切换 |

连续失败 2 次才触发 `full_check`。

---

## 手动关闭 AP 的抑制窗（`/var/run/teslausb-ap-manual-off`）

**问题**：`quick_check` 每 2 分钟跑一次且会在「AP 开着」时判定「该关 AP / 该探测让出」。用户在页面上点「关闭 AP」后，如果没有留下任何痕迹，下一轮 timer 就会把 AP **又开回来** —— 用户看到的就是「点了没反应」（10-01 事故，[M85](../reference/教训索引.md)）。

**机制**：抑制是**持久状态**，不是一次事件。

| 环节 | 行为 |
|------|------|
| 写入 | `stop_ap()` **先写标记**（`{"ts":…, "ttl":1800}`）**再**执行关闭；关闭失败则**回滚**删除标记（关 AP 最坏 186s > 检测间隔 120s，后写会留被抢占窗口） |
| 读取 | 所有「自动开 AP」入口（`_start_ap_fallback` 等）在动作前读标记，未过期 → 一律跳过 |
| 清除 | 用户手动「开启 AP」/ 改 AP 模式 / 系统兜底重开 → 清除（`_clear_ap_manual_off`） |
| 幂等 | `stop_ap()` 前置守卫：AP 本就没开 → 直接返回，**不碰网络、不写抑制**（否则 station 模式下点「关闭AP」会执行 `nmcli device connect wlan0` 打断在线 WiFi） |
| 过渡闸 | `_ap_transition_active()`（TTL 120s）：AP 起停进行中时 timer 不得抢开 |
| 提醒 | 进入抑制时 `SystemMonitor.check_ap_manual_off()` 推一条企业微信（推送前二次校验 `hostapd` 未运行防标记残留误报）；**按标记去重**（跨进程有效，且不误吞「关→开→再关」的第二次提醒，[M86](../reference/教训索引.md)） |

单射频约束下抑制窗内设备只剩 station 一条路：**若当时无任何可连 WiFi，抑制窗就是一段合法但静默的离线期** —— 这正是那条提醒存在的理由。

### 前端为什么不能等这个请求的响应

`apAction('stop')` 的 `fetch` 走的就是 AP 这条无线链路，而这个请求**本身要拆掉这条链路** → 浏览器必然 `Failed to fetch`，真成功也会被渲染成「请求失败」。因此前端改为「先查状态 → 按 `apOn` 决定文案」+ `AbortController` 3s 超时；`_ap_bring_down()` 返回的「wlan0 没重连上」也不再当失败，而是降级为 `warning`（且该分支**不** reload 页面）。

---

## AP 自愈探测（`_ap_self_heal`）

解决的核心痛点是「AP 开了出不来」。

### 执行顺序

```
0. 手动关闭抑制窗内（`teslausb-ap-manual-off` 未过期）→ 跳过
1. force-on / hostapd 未运行 / 起停进行中 → 跳过
2. 有客户端连着 → 跳过（不打断用户配网）
3. 客户端「有 → 无」断开事件 → 立即让出探测回连（跳过宽限期/退避/冷却）
4. 常规路径：宽限期 → 冷却 → 退避 三重闸门
5. 让出探测：_ap_bring_down() → 扫描 → 匹配已保存网络 → 尝试连接
6. 连上 → AP 保持关闭，退避重置
7. 没连上 → 重启 AP，退避加倍
```

### 三重闸门

| 闸门 | 作用 |
|------|------|
| 宽限期 | AP 刚启动的 3min（手动）/ 15min（fallback）内不让出，避免刚开就关 |
| 切换冷却 | `_can_switch()` 期间不让出，避免被冷却挡住后误判失败 |
| 指数退避 | 5 → 30min 上限，失败越多次等越久 |

### 为什么需要「客户端感知」

手机连着 AP 的时候不能让出探测 —— 用户正在配网，把热点关了直接断人连接。而且有客户端时不记录探测次数，避免「断开后还要等满退避」。

v0.3.1.36 加的「断开事件优先处理」：检测到客户端从有变无，立即让出回连，跳过所有闸门。这是用户配完网的明确信号。

---

## 扫描窗口的坑（v0.3.1.39 修复）

AIC8800 从 AP 模式切回 station 模式后，需要 **10~30 秒** 才能扫到周围的 WiFi。

老代码的做法：

```python
nmcli dev wifi rescan
time.sleep(5)          # ← 只等 5 秒
scanned = 扫描结果      # 大概率是空的
```

扫不到已保存网络 → 误判「附近没网可连」→ 重启 AP + 退避加倍。8 月 31 日 12:44:24 到 12:44:35 的 11 秒日志完整记录了这样一次误判。

现在改成轮询等待：

```python
nmcli dev wifi rescan
time.sleep(3)
scanned = self._wait_scan_results(max_wait=30, interval=3)
if not scanned:
    # 空结果再 rescan 重试一次（再给 30 秒）
    nmcli dev wifi rescan
    time.sleep(3)
    scanned = self._wait_scan_results(max_wait=30, interval=3)
```

日志里会打印 `已保存 N, 扫描到 M 个 SSID` —— 这个数字是区分「真没网」和「扫描没跑完」的关键，排障时先看它。

### 已知的小瑕疵

轮询可能提前拿到切模式前的旧扫描缓存（非空）。旧 SSID 是真实存在过的网络，`_switch_to` 会实际连接验证，连不上就走兜底。最多多一次失败尝试，不影响最终结果。

---

## 必须避开的雷

### 不要全局重启 NetworkManager

```bash
# ❌ 会打断 systemd-resolved 的 D-Bus 连接 → resolved 崩溃
#    → Restart=always + start-limit 限流 → 本机 DNS 永久损坏
systemctl restart NetworkManager

# ✅ 局部重连，NM 进程不重启，D-Bus 保持
nmcli device connect wlan0
```

如果已经踩了：

```bash
sudo systemctl reset-failed systemd-resolved
sudo systemctl restart systemd-resolved
```

### dnsmasq 与 systemd-resolved 争 53 端口

AP 场景下 dnsmasq 配 `port=0`（只做 DHCP，不做 DNS 解析）。关闭 AP 时要删掉 `/etc/dnsmasq.d/ap.conf`。

### 网段推导

按 `.` split 推导，不要假设固定前缀。

---

## 排障

```bash
# AP 自愈决策过程
journalctl -u teslausb-web --no-pager | grep "AP 自愈" | tail -20

# 当前连接与扫描结果
nmcli connection show
nmcli device wifi list
nmcli device status

# AP 状态
systemctl is-active hostapd dnsmasq
cat /etc/dnsmasq.d/ap.conf 2>/dev/null
```

日志里 `已保存 N, 扫描到 M 个 SSID`：

| M | 含义 |
|---|------|
| 0 | 扫描没跑完（或确实周围无 WiFi），v0.3.1.39 前会误判 |
| >0 但 known 空 | 已保存的网络不在扫描范围（换个位置试试） |

---

## 相关

- [排障手册 · AP 不切回 WiFi](../how-to/排障手册.md)
- [服务清单](../reference/服务清单.md)
- [教训索引 M44 / M46 / M53](../reference/教训索引.md)
