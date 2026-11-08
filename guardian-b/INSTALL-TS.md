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

---

## 9. 升级到 v1.0.2（从 v1.0.0 起）

v1.0.2 新增 `GET/POST /guard/config`（A 端设置页下发机器人 key）、告警三态
（`mode: sent|dryrun|failed`）、**测试心跳不计入在线判定**、告警文案重写。
第 3 条是必须升级的原因：v1.0.0 会把手工联调心跳（`version=manual-test`）
当成真实设备在线，5 分钟后开始报「设备失联」，并每 30 分钟重复一次。

```bash
# 1) 传包并校验
scp guardian-b-v1.0.2.tar.gz root@100.111.252.121:/root/
ssh root@100.111.252.121
sha256sum /root/guardian-b-v1.0.2.tar.gz     # 必须等于发布说明里的值

# 2) 解压到**新目录**（包里顶层有 guardian-b/，别再 -C 到同名目录）
rm -rf /root/gb2 && mkdir -p /root/gb2
tar xzf /root/guardian-b-v1.0.2.tar.gz -C /root/gb2
ls /root/gb2/guardian-b                    # 应看到 app.py judge.py install.sh ...

# 3) 装（幂等：不会覆盖已有的 guard-b.env，token/key 都不丢）
cd /root/gb2/guardian-b && bash install.sh

# 4) 清掉被污染的状态 —— 这一步不能省
#    state.json 里存着那条 manual-test 的 last_seen / last_body，
#    不清掉的话升级后仍会继续报「设备失联」。
systemctl stop guard-b
ts=$(date +%s)
mv /opt/guard-b/data/state.json /opt/guard-b/data/state.json.bak-$ts 2>/dev/null || true
systemctl start guard-b

# 5) 自检
curl -s http://100.111.252.121:8899/healthz; echo
curl -s http://100.111.252.121:8899/guard/status \
  -H "Authorization: Bearer $(grep -E '^GUARD_TOKEN=' /opt/guard-b/guard-b.env | cut -d= -f2-)"
# 期望：{"ok":true,"version":"1.0.2",... "alert":{...,"configured":true,...},"state":{...}}
#       state.last_seen 两项都是 0、last_body 为 null、classify = no_heartbeat_yet
```

> **升级后到 A 端真正发心跳之间，B 不会报任何失联**（`no_heartbeat_yet`）——
> 这是预期，不是故障。A 端配好 `guard.json` 并发心跳后才开始判死。

### 验证「测试心跳不会被误判」（可选，30 秒）

```bash
TOKEN=$(grep -E '^GUARD_TOKEN=' /opt/guard-b/guard-b.env | cut -d= -f2-)
curl -s -X POST http://100.111.252.121:8899/guard/heartbeat \
  -H "Authorization: Bearer $TOKEN" -H "X-Guard-Channel: ts" -H "X-Guard-Seq: 1" \
  -H "Content-Type: application/json" \
  -d '{"seq":1,"version":"manual-test","vehicle":{"locked":true,"sentry_mode":true,"user_present":false}}'
# 期望：{"ok":true,"channel":"ts","result":{"event":"test","ignored":true}}
# 再查 status：last_seen.ts 仍是 0，last_test.version = "manual-test"，且 5 分钟后不告警
```

---

## 10. 升级到 v1.1.0（目录布局变更 + 远程升级能力）

> **这一步不是"覆盖文件"**：v1.1.0 把代码改成 `releases/<版本>/ + current 符号链接`，
> 并新增两个 systemd 单元。`install.sh` 会自动迁移旧布局，但**请按顺序做**。

v1.1.0 带来的变化：

| 变化 | 为什么 |
|---|---|
| 代码进 `releases/<ver>/`，`current` 指向它 | 升级只切链接（原子），旧版本保留可回滚 |
| 进程改以 `guardb` 用户运行，代码目录 root 只读 | "本地锁死"：即便 Flask 被 RCE 也改不了自己的代码 |
| 新增 `.tools/`（验签库 + root helper） | 验签器**不能**被升级包替换，否则校验形同虚设 |
| 新增 `guardb-upgrade.service` + `guardb-upgrade.path` | 只有它能写代码目录；guardb 只能**写一个请求文件**，跑什么由 root 侧定义（不用 sudo——`NoNewPrivileges` 会让 sudo 失效） |
| `guard-b.env` 变成 `root:guardb 0440` | 进程能读 key（要发告警）但不能改 key（防改告警目的地） |

### 10.1 步骤

```bash
# 1) 传包并校验
scp guardian-b-deploy-v1.1.0.tar.gz root@100.111.252.121:/root/
ssh root@100.111.252.121
sha256sum /root/guardian-b-deploy-v1.1.0.tar.gz    # 必须等于发布说明里的值

# 2) 停服务再迁移（避免 mv 掉 *.py 的瞬间被 restart 撞上）
systemctl stop guard-b

# 3) 解压到新目录再跑 install.sh
rm -rf /root/gb3 && mkdir -p /root/gb3
tar xzf /root/guardian-b-deploy-v1.1.0.tar.gz -C /root/gb3
cd /root/gb3/guardian-b && bash install.sh
# 期望输出里有：迁移旧平铺布局 → releases/1.0.2 / 安装 releases/1.1.0 / 切换 current

# 4) 装**升级包**签名公钥（一行；principal 必须是 a7z-guard）
echo 'a7z-guard ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOGgjJGFoLcfRpflu14lFkf37M7wxefXyBOZs/BPey/E a7z-guard' \
  | tee /opt/guard-b/.trusted/pkg.pub
chmod 0444 /opt/guard-b/.trusted/pkg.pub

# 5) 清掉可能被污染的判死状态（见 §9 / M125；没做过手工测试可跳过）
mv /opt/guard-b/data/state.json /opt/guard-b/data/state.json.bak-$(date +%s) 2>/dev/null || true

# 6) 起服务 + 自检
systemctl start guard-b
sleep 3
systemctl --no-pager --lines=20 status guard-b
ls -l /opt/guard-b/current                     # 应指向 releases/1.1.0
ps -o user=,cmd= -p $(systemctl show -p MainPID --value guard-b)   # 应是 guardb
```

### 10.2 启用远程升级（可选，默认关闭）

```bash
# 只改这一行，然后重启
# ★ 先确认这一行**存不存在**：从 v1.0.x 升上来的 env 里没有 GUARD_UPGRADE_ENABLE
#   （那是 v1.1.0 才加的键），而 install.sh 按设计保留旧 env 不覆盖。
#   此时 `sed -i 's/^KEY=.*/KEY=1/'` 匹配不到任何行 → **静默无操作、退出码 0**，
#   表现是"改完了但 upgrade.enabled 还是 False"，极易误判成权限或缓存问题。
#   所以一律用「有则替换、无则追加」，并在改完立刻 grep 回读。
cp -a /opt/guard-b/guard-b.env /root/guard-b.env.bak-$(date +%s)
if grep -q '^GUARD_UPGRADE_ENABLE=' /opt/guard-b/guard-b.env; then
    sed -i 's/^GUARD_UPGRADE_ENABLE=.*/GUARD_UPGRADE_ENABLE=1/' /opt/guard-b/guard-b.env
else
    echo 'GUARD_UPGRADE_ENABLE=1' >> /opt/guard-b/guard-b.env
fi
# ★ 必须补这两行：sed -i 是「新建临时文件 + rename」，会把属主从 root:guardb 变成
#   root:root；env 是 0440，guardb 随即读不到 → 进程拿不到 token → 所有接口 401。
#   这个故障看起来像「token 错了」，实际是权限被 sed 改坏了。
chown root:guardb /opt/guard-b/guard-b.env && chmod 0440 /opt/guard-b/guard-b.env
grep -n '^GUARD_UPGRADE' /opt/guard-b/guard-b.env          # 回读：必须看到 =1
systemctl restart guard-b
sudo -u guardb cat /opt/guard-b/guard-b.env >/dev/null && echo "guardb 可读 OK"

# 验证（三项缺一都会 403，这是设计）
curl -s http://<TS_IP>:8899/guard/upgrade/status \
  -H "Authorization: Bearer $(grep -E '^GUARD_TOKEN=' /opt/guard-b/guard-b.env | cut -d= -f2-)" \
  | python3 -m json.tool
# 期望：{"ok":true,"version":"1.1.0","upgrade":{"enabled":true,"state":"idle",...}}
```

**确认码在哪看**（这是防"A 被拿下换掉见证者"的关键一环）：

> **v1.1.2 起默认推企微**：码生成后会立刻发一条「【A7Z 守护 · 僚机升级确认】」
> 到 `GUARD_WECOM_KEY` 对应的群，手机直接收，不用登服务器。
> 想关掉设 `GUARD_UPGRADE_CODE_NOTIFY=0`；群里有别人时建议改单人群。
>
> 推送是**真带外**：码走 B → 企微 → 你手机，A 端读不到群消息，也改不了这个开关
> （开关和 key 都在 `root:guardb 0440` 的 env 里）。推送失败不影响升级。

日志兜底（推送失败或开关关闭时，这一路**永远**在）：

```bash
journalctl -u guard-b --no-pager | grep 确认码 | tail -1
# 或
grep 确认码 /opt/guard-b/data/guard-b.log | tail -1
```

> `GUARD_UPGRADE_CODE_VIA_API=0`（**默认**）时确认码不会被 A 端读到
> —— 这才是真正的带外确认。改成 `1` 后 A 端能自动取到码，操作更省事，
> 但"拿到 A 的攻击者"也能全自动完成整个升级，等于把人工确认降级成"人看一眼"。

### 10.3 回滚

```bash
ls /opt/guard-b/releases            # 保留 current + 2 代
# 方式一：手工切回（立刻生效）
ln -sfn releases/<旧版本> /opt/guard-b/current.swap && mv -Tf /opt/guard-b/current.swap /opt/guard-b/current
systemctl restart guard-b

# 方式二：让账本回滚（下次 apply 时执行）
#   把 data/upgrade/journal.json 的 state 改成 committed、version 改成旧版本 —— 不推荐，
#   正常升级失败时 apply.py 会自动回滚并在 status 里显示 rolled_back。
```

### 10.4 三个必须知道的边界

1. **升级期间判死告警会被抑制**：A 端在发起升级时开一个 900 秒的抑制窗口，避免"B 重启"被报成"B 失联"。窗口内若设备真的掉线，要等窗口结束才会告警。
2. **回滚不保证数据新**：`state.json` 若因新版本 schema 变了，回滚只切代码。升级前如需保底，先 `cp -a data data.bak-$(date +%s)`。
3. **包不能带脚本、不能改 env/状态**：`file_type_not_allowed` / `forbidden_path` 是硬拒绝，不做"跳过"。要改这些只能人工登录操作——这正是锁死的含义。
