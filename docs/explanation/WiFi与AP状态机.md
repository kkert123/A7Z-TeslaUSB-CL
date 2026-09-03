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

## AP 自愈探测（`_ap_self_heal`）

解决的核心痛点是「AP 开了出不来」。

### 执行顺序

```
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
