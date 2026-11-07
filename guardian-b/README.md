# guardian-b — 停车自检守护 · B 端（僚机）

> 部署在**腾讯云轻量 VPS**，与 A7Z 设备（A 端）分属两地。
> 职责只有三件：**收心跳 → 判死 → 发告警**。不做远程控制、不回调 A。
>
> 设计文档：`../docs/explanation/自检守护-B端VPS设计.md`
> ⚠️ 本目录**不随 A7Z 升级包分发**，除非显式加入 `deploy_manager.FILES` 白名单。

---

## 为什么需要它

A7Z 断网 / 崩溃 / 断电时，它自己发不出通知。B 端在云端独立**监听 A 的心跳**，
一旦 A 该在线却不再心跳，用**另一条链路**把告警推给用户——补上"设备自己发不出告警"的盲区。

---

## 目录结构

```
guardian-b/
├── app.py                       HTTP 服务（/guard/heartbeat|ping|status, /healthz）
├── config.py                    环境变量配置装载
├── state.py                     状态持久化（原子写 JSON）
├── judge.py                     判死 / 告警状态机（§2 矩阵 + §4 逻辑）
├── alerts.py                    微信告警投递（精简自包含，零 A 侧耦合）
├── requirements.txt
├── deploy/
│   ├── guard-b.service          systemd 单元
│   ├── guard-b.env.example      环境变量样例
│   └── openresty-guard-b.conf   1Panel OpenResty 反代（TLS 终结 + 限流）
├── tls/
│   └── gen_selfsigned.sh        生成自签 CA+服务端证书，输出 A 侧 pinning 物料
└── tests/
    └── test_judge.py            判死矩阵单测（离线可跑）
```

---

## 快速开始（本机冒烟）

```bash
cd guardian-b
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

export GUARD_TOKEN="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
export GUARD_ALERT_DRY_RUN=1          # 先不外发告警
export GUARD_STATE_PATH="./data/state.json"
export GUARD_LOG_PATH="./data/guard-b.log"
./venv/bin/python app.py
```

另开一个终端：

```bash
# 心跳（HTTPS 通道）
curl -s -X POST http://127.0.0.1:8800/guard/heartbeat \
  -H "Authorization: Bearer $GUARD_TOKEN" \
  -H "X-Guard-Channel: https" -H "X-Guard-Seq: 1" \
  -H "Content-Type: application/json" \
  -d '{"seq":1,"ts":0,"version":"0.3.1.6x","vehicle":{"locked":true,"user_present":false,"sentry_mode":true}}'

# 状态
curl -s http://127.0.0.1:8800/guard/status -H "Authorization: Bearer $GUARD_TOKEN"
```

单测：`python3 tests/test_judge.py`

---

## 部署（腾讯云轻量 + 1Panel）

### 1. 目录与依赖

```bash
sudo mkdir -p /opt/guard-b/{data,tls}
sudo cp -r ./* /opt/guard-b/         # app.py/config.py/state.py/judge.py/alerts.py 等
cd /opt/guard-b
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
```

### 2. 配置（环境文件，不进仓库）

```bash
sudo cp deploy/guard-b.env.example /opt/guard-b/guard-b.env
sudo nano /opt/guard-b/guard-b.env     # 至少填 GUARD_TOKEN / GUARD_WECOM_KEY
sudo chmod 600 /opt/guard-b/guard-b.env
# token 生成：openssl rand -hex 32
```

### 3. 证书（自签 + A 侧 pinning）

```bash
sudo bash /opt/guard-b/tls/gen_selfsigned.sh <VPS公网IP> /opt/guard-b/tls
# 产出 ca.crt / server.crt / server.key / fullchain.crt
# 把 ca.crt 拷到 A 端 → A 侧 requests(verify='.../ca.crt')
```

### 4. 反向代理（OpenResty）

把 `deploy/openresty-guard-b.conf` 的 `server{}` 段落到 1Panel 的反向代理配置，
`ssl_certificate` 指到 `/opt/guard-b/tls/fullchain.crt`。
⚠️ `limit_req_zone` 是 **http 上下文**指令，须加到 nginx.conf 的 `http{}`（1Panel → 全局配置）。

### 5. systemd

```bash
sudo cp deploy/guard-b.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guard-b
sudo systemctl status guard-b
journalctl -u guard-b -f
```

### 6. 安全组 / 防火墙

- 放行 TCP **443**（公网 HTTPS）。
- 本方案**自签，不用 ACME**，无需放行 80。
- TS 通道：让 A 直连 `http://<TS_IP>:8800`（见 openresty 配置末尾注释），
  或把 `GUARD_BIND_HOST` 设成 TS 接口地址。
- **不要**把 8800 暴露到公网。

---

## 接口契约（摘要，详见设计文档 §3）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/guard/heartbeat` | Bearer | 头 `X-Guard-Channel: ts\|https`、`X-Guard-Seq: <int>`；body 见设计 §3.1 |
| POST | `/guard/heartbeat` | Bearer | body `{"event":"going_offline","expected_until":<ts>,"reason":"driving"}` |
| GET | `/guard/ping` | Bearer | 反向心跳：A 探活 B，返回 `{"ok":true,"ts":...}` |
| GET | `/guard/status` | Bearer | 只读状态 + `classify` 结论 |
| GET | `/healthz` | 无 | 存活探针 |

**鉴权**：`Authorization: Bearer <GUARD_TOKEN>`（v1 静态 token；HMAC 迁移见设计 §8）。

---

## 判死逻辑（§2 矩阵）

阈值：`WARN = 3T`、`ALARM = 5T`，`T = GUARD_T`（默认 60s）。

| `last_seen.ts` | `last_seen.https` | 判读 | 动作 |
|---|---|---|---|
| 新鲜 | 任意 | 正常 | 静默 |
| 陈旧 | 新鲜 | **Tailscale 通道故障**（设备在线） | 告警 |
| 陈旧 | 陈旧 | **设备失联** | 告警 |
| 新鲜 | 陈旧 | HTTPS 未启用（正常态） | 静默 |

- 预期离线窗口内 / 车在开（`sentry_mode && locked && !user_present` 之外）→ **静默**，防误报。
- 告警去重：同故障 `GUARD_DEDUP_S`（默认 30min）内只推一次；恢复推一条「已恢复」。
- `last_seen` 一律用 **B 自己的接收时刻**，绝不采信 A 的 `ts`（M51 跨机时钟不可比）。

---

## 运维要点

- **B 死了怎么办**：进程崩溃由 systemd `Restart=always` 拉起；整机故障由**外部 dead-man**
  （`GUARD_DEADMAN_URL`，healthchecks.io / Uptime-Kuma）兜底（§10 R5）。
- **HTTPS 通道是否健康**：A 侧每 10min 的 dual-send 探针会让 `last_seen.https` 保持新鲜；
  若它变陈旧而 `ts` 新鲜，说明 HTTPS 兜底通道有问题（但按矩阵这属"正常态"，仅作诊断）。
- **证书轮换**：重跑 `gen_selfsigned.sh` 换 server 证书即可，**A 无需改动**（A 只 pin CA）。
- **状态文件**：`/opt/guard-b/data/state.json`，可随时查看 `last_seen` / `offline_until` / `alerts`。

---

## 与设计文档的差异说明

1. **告警器**：设计 §7 说「复用 A 侧 `weixin_notifier`」。实现改为自包含 `alerts.py`——
   A 侧模块携带 exit-node 探测（`subprocess tailscale`）、哨兵事件模板、写死
   `/opt/teslausb-web/data` 等 A 专属逻辑，在 B 上无意义或会报错。`send_text`/`send_markdown`
   接口保持同形，日后可无缝换回。
2. **§4 伪代码一处修正**：TS 故障判据由 `age_https < WARN` 改为 `age_https < ALARM`，
   与 §2 矩阵口径一致（覆盖 https 落在 [WARN,ALARM) 的过渡带）。详见 `judge.py` 顶部注释。
