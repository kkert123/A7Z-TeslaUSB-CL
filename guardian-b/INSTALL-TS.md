# guardian-b 手动导入指南（Tailscale-only，v1.0.0）

> 目标机器：`vm-0-17-ubuntu-sh`（Tailscale `100.111.252.121`，上海腾讯云 Ubuntu）
> 形态：**只监听 Tailscale 接口，不占公网端口、不需要证书**。
> 心跳走 WireGuard 隧道（A 端 `endpoints.ts` 直连 `http://100.111.252.121:8899`）。
>
> ⚠️ 端口用 **8899** 不是 8800：实测 `100.111.252.121:8800` 已有反向代理在听（返回 502），
> 别去抢它。

---

## 0. 前置检查（30 秒）

```bash
# 必须是 root
id -u

# Tailscale 在跑，且本机 IP 就是 100.111.252.121
tailscale ip -4
tailscale status | head -3

# 8899 没被占（有输出说明被占了，换 8898 之类）
ss -lntp | grep -E ':8899\b' || echo "8899 free"

# Python 3.8+ 与 venv 可用
python3 -V
python3 -m venv --help >/dev/null 2>&1 && echo "venv ok" || echo "!! 装 python3-venv"
```

---

## 1. 把包放到 VPS

包：`guardian-b-v1.0.0.tar.gz`（sha256 见交付消息）。

任选一种：

```bash
# A. 你的电脑上 scp（推荐）
scp guardian-b-v1.0.0.tar.gz root@<VPS公网IP>:/root/

# B. 若能直接从 PC 走 Tailscale
scp guardian-b-v1.0.0.tar.gz root@100.111.252.121:/root/

# C. 实在不行：本地 `base64 -w0` 后粘贴到 VPS 的 /root/b64.txt，再 `base64 -d b64.txt > pkg.tar.gz`
```

到 VPS 上解压：

```bash
cd /root
sha256sum guardian-b-v1.0.0.tar.gz          # 与交付消息里的值比对
mkdir -p /root/guardian-b && tar xzf guardian-b-v1.0.0.tar.gz -C /root/guardian-b
cd /root/guardian-b
ls    # 应看到 app.py config.py judge.py state.py alerts.py install.sh INSTALL-TS.md ...
```

---

## 2. 安装（幂等，可重复跑）

```bash
sudo bash install.sh
```

脚本会：拷代码到 `/opt/guard-b` → 建 venv 装依赖 → 生成 `guard-b.env`（**已存在则不覆盖**）
→ 装 systemd 单元。因为 token 还是空的，它**不会**自动启动，会打印下一步。

---

## 3. 填 env（唯一需要手工的环节）

```bash
sudo nano /opt/guard-b/guard-b.env
```

把这三行改成（值在交付消息里，别贴进仓库/ git）：

```ini
GUARD_TOKEN=<32 字节随机串，A/B 必须完全一致>
GUARD_BIND_HOST=100.111.252.121
GUARD_BIND_PORT=8899
GUARD_WECOM_KEY=<「A7Z 守护」机器人 key>
```

其余保持默认即可。**`GUARD_BIND_HOST` 千万不要写 `0.0.0.0`** —— 那等于把带 token 的
HTTP 端口挂到公网。

---

## 4. 启动 + 四条自检

```bash
sudo chmod 600 /opt/guard-b/guard-b.env
sudo systemctl enable --now guard-b
sleep 2
systemctl status guard-b --no-pager --lines=10
```

```bash
# V1 存活（不需要 token）
curl -s -m 5 http://100.111.252.121:8899/healthz; echo

# V2 鉴权：无 token 必须 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://100.111.252.121:8899/guard/heartbeat

# V3 带 token 打一发心跳 → 200，且 /guard/status 能看到 last_seen
TOKEN=$(sudo grep '^GUARD_TOKEN=' /opt/guard-b/guard-b.env | cut -d= -f2)
curl -s -X POST http://100.111.252.121:8899/guard/heartbeat \
  -H "Authorization: Bearer $TOKEN" -H "X-Guard-Channel: ts" -H "X-Guard-Seq: 1" \
  -H "Content-Type: application/json" \
  -d '{"seq":1,"version":"manual-test","net":{"tailscale":"Running"},"vehicle":{"locked":true,"sentry_mode":true,"user_present":false}}'; echo

curl -s http://100.111.252.121:8899/guard/status -H "Authorization: Bearer $TOKEN" | head -c 400; echo

# V4 公网不可达（从 VPS 外面测，比如你自己的电脑）
#   curl -m 5 http://<VPS公网IP>:8899/healthz   → 必须超时/拒绝
```

> V4 是这条部署方式的**安全底线**：只绑 TS，公网连不上。

---

## 5. 接上 A7Z（设备侧）

在 A7Z 上写 `/opt/radxa_data/teslausb/config/guard.json`（注意：`config/` 下的文件不在升级包白名单，
必须手工建，否则升级会覆盖 token）：

```bash
sudo tee /opt/radxa_data/teslausb/config/guard.json >/dev/null <<'JSON'
{
  "enabled": true,
  "token": "<与 GUARD_TOKEN 完全一致>",
  "endpoints": { "ts": "http://100.111.252.121:8899", "https": "" },
  "ca_path": "",
  "interval_s": 60,
  "ts_timeout_s": 8,
  "https_timeout_s": 10,
  "probe_every_s": 150,
  "states_interval_s": 300,
  "queue_max": 200,
  "offline_default_s": 3600
}
JSON
sudo chmod 600 /opt/radxa_data/teslausb/config/guard.json
sudo systemctl restart teslausb-web
```

> 前提：A7Z 上必须已跑 **v0.3.1.76+**（含 `guard_client.py`）。没升级前这个文件不会生效，
> 也不会报错——很容易误以为"配了但没动静"。

验证：

```bash
# 设备端：最近一次快照有没有落盘、发送是否成功
sudo curl -s http://127.0.0.1:5000/api/guard/status | head -c 600; echo
sudo cat /opt/radxa_data/teslausb/data/guard_status.json | head -20
```

B 端应能看到 `last_seen` 在 60s 内滚动：

```bash
curl -s http://100.111.252.121:8899/guard/status -H "Authorization: Bearer $TOKEN"
```

---

## 6. 验收清单（按顺序做，每步判据写死）

| # | 操作 | 判据 |
|---|---|---|
| 1 | 正常态观察 3 分钟 | B 端 `last_seen` 每 ~60s 更新；A 端 `sender.tx_ok` 递增、`fail_streak=0` |
| 2 | `sudo systemctl stop teslausb-web`（A 端） | 3~5 分钟（WARN 180s / ALARM 300s）后收到**企微告警**「设备失联」 |
| 3 | `sudo systemctl start teslausb-web` | 恢复后收到**一条**「已恢复」，且不重复刷 |
| 4 | 车上解锁车门（或 A 端伪造 `locked` 跃迁） | B 端 `offline_until` = now+900s，窗口内**不告警** |
| 5 | 只断 TS（`sudo tailscale down` on A7Z，保留 WiFi） | 告警文案应是「Tailscale 通道故障」，**不是**「设备失联」 |

第 5 条是本次修掉的那个 P0 的直接验收项——如果文案错了，说明探针周期或判据又退化了。

---

## 7. 回滚（30 秒）

```bash
sudo systemctl disable --now guard-b
# A 端同时关闭，避免它继续往一个没人听的端口发心跳
sudo python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/opt/radxa_data/teslausb/config/guard.json')
d = json.loads(p.read_text()); d['enabled'] = False
p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
PY
sudo systemctl restart teslausb-web
```

数据/日志都在 `/opt/guard-b/data/`，删目录不会污染系统其它部分。

---

## 8. 排障速查

| 现象 | 先看这里 |
|---|---|
| 服务起不来，日志 `Address already in use` | 8899 被占：`ss -lntp \| grep 8899` |
| 日志 `Cannot assign requested address` | `GUARD_BIND_HOST` 写错，或 tailscaled 没起来 |
| A 端 `last_error` = `ConnectionError` | 从 A7Z `curl http://100.111.252.121:8899/healthz` 先确认隧道通 |
| A 端 `last_error` = `http_401` | token 两边不一致（注意 env 里有没有多余空格） |
| A 端 `last_error` = `http_403` | B 端 `GUARD_PUBLIC_STATUS=0` 时 `/guard/status` 也要 token，属正常 |
| 收到告警但文案是「设备失联」而 TS 只是抖动 | 探针周期被改回 >300s 了（见设计 §2 注） |
| 企微收不到 | `GUARD_ALERT_DRY_RUN` 是否还是 1；`alerts.py` 日志里有没有 `errcode` |
