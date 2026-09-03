# USB Gadget 与车机交互

> A7Z 伪装成 U 盘的原理、Gadget 的生命周期、以及那条 udev 规则为什么长这样。

---

## 基本原理

Linux 的 USB Gadget 框架让设备能扮演 USB **device**（从设备）角色。A7Z 通过 ConfigFS 创建一个 gadget，把 NVMe 的 exFAT 分区作为「大容量存储 LUN」暴露出去。

车机看到的就是一个普通的 U 盘，正常往里写行车记录仪视频。

```
车机 (USB Host)
    │
    │  USB 线缆
    ▼
A7Z (USB Device)
    │  ConfigFS: /sys/kernel/config/usb_gadget/tesla_usb/
    │    ├── UDC            ← 写入控制器名 = 激活 gadget
    │    └── functions/mass_storage.0/lun.0/file → /dev/nvme0n1p2
    ▼
NVMe 分区（exFAT）
    │
    ▼
本机只读挂载 /mnt/teslacam
```

**判断 gadget 是否激活**：

```bash
cat /sys/kernel/config/usb_gadget/tesla_usb/UDC
# 非空（如 6a00000.xhci2-controller）= 已激活
# 空 = 未绑定，车机认不出设备
```

---

## 生命周期归谁管

| 环节 | 负责人 |
|------|--------|
| 创建 gadget、绑定 UDC | `usb_gadget_init.sh` |
| Present/Edit 模式切换时的挂载 | `present_usb.sh` |
| 守护与异常恢复 | `teslausb-gadgetd.py` (`teslausb-gadget.service`) |
| udev 事件 | `udev/99-usb-gadget.rules` —— **只在 gadget 未激活时才重绑** |

---

## 那条 udev 规则的前世今生

### 旧规则（2026-05-09 手工部署）

任何 `usb_device` 的 `bind`/`unbind` 事件（包括 AIC8800 WiFi/蓝牙插拔）都会无条件触发：

- **unbind 分支**：`echo xhci2-controller > xhci-hcd/unbind`（失败返回 1，还会干扰其他设备）
- **bind 分支**：无条件重新绑定 UDC，正在跟车机通信时直接被打断

8 月 30 日 23:38 实证：gadget 重建期间出现 `ep1out "can't queue to disabled endpoint"`。

### 新规则（v0.3.1.39）

```udev
SUBSYSTEM=="usb", ACTION=="bind", ENV{DEVTYPE}=="usb_device", \
    RUN+="/bin/sh -c 'if [ -d /sys/kernel/config/usb_gadget/tesla_usb ] \
          && [ ! -s /sys/kernel/config/usb_gadget/tesla_usb/UDC ]; then \
          echo 6a00000.xhci2-controller > /sys/kernel/config/usb_gadget/tesla_usb/UDC; fi'"
```

两处改动：

1. **加 UDC 未激活守卫** —— `[ ! -s UDC ]` 表示 UDC 为空（未绑定）才重绑，运行中绝不打断
2. **删掉 unbind 分支的 xhci-hcd 操作** —— gadget 生命周期由 `present_usb.sh` 全权管理，udev 不该插手

### 部署

```bash
sudo cp udev/99-usb-gadget.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

v0.3.1.39 需要手动执行（规则不进升级包）。**v0.3.1.40 起已随包自动部署**：规则加进了 `deploy_manager` 的 `MANAGED_FILES`，`upgrade_service` 新增 post-install 钩子，升级完成后自动复制 + reload（幂等）。

---

## 端点故障：ep1out disabled

这是车机报 UI_a112 的直接原因。

`dwc3` 是 A733 的 USB 控制器驱动，`ep1out` 是出端点。当它变成 disabled 状态，车机的写请求会返回 "not queued"，车机就报 I/O 错误。

**触发链条**：

```
车机写数据
  ↓
A7Z 执行 drop_caches=2（系统级缓存回收）
  ↓
959MB 内存下，本地读重新从 NVMe 落盘
  ↓
NVMe / USB gadget 写路径抖动
  ↓
dwc3 ep1out 端点禁用
  ↓
车机写请求 not queued → UI_a112
```

**所以解法不是修 USB 驱动，而是别在车机写的时候回收缓存。** 详见 [缓存一致性](缓存一致性.md)。

---

## 排障

```bash
# gadget 激活状态
cat /sys/kernel/config/usb_gadget/tesla_usb/UDC

# 内核 USB 相关日志
dmesg -T | grep -iE "dwc3|ep1out|gadget|usb" | tail -30

# gadget 守护日志
tail -50 /var/log/teslausb-gadgetd.log

# udev 规则是否生效
udevadm test /sys/class/udc/* 2>&1 | grep -i gadget
```

| 现象 | 处理 |
|------|------|
| UDC 为空，车机认不出 | `sudo systemctl restart teslausb-gadget` |
| 反复断开重连 | 检查 udev 规则是不是旧版（无 UDC 守卫） |
| 车机报 I/O 错误 | 见 [排障手册](../how-to/排障手册.md) |

---

## 相关

- [缓存一致性](缓存一致性.md) —— 只读挂载带来的读缓存问题
- [架构总览](架构总览.md)
- [教训索引 M52](../reference/教训索引.md) —— 内核行为必须真机验证
