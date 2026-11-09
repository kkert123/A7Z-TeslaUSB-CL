#!/usr/bin/env python3
"""v0.3.1.83 Release 创建脚本（从 _release82.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.83"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "89b8c8bfec9e7e5a693ce9f93edcc6726af62189"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.83.tar.gz")
SIG = os.path.join(ART, "teslausb-v0.3.1.83.tar.gz.sig")
SHA256 = "a01123241a4125d285f320d42f3882f333e46a33f43b0e2c9c69fcbb6133abfd"

BODY = """## v0.3.1.83 — 锁车自检不再丢 + 僚机卡片「切换中」修复

### 为什么锁车后还是没有推送

2026-11-09 16:44 锁车，到 17:15 既没收到「停车自检」，远端也没有任何告警。
两个独立的缺口，各修一半：

**A 端**：报告重试队列**只在内存**，且普通报告只争取 13 分钟（30/120/600s）就丢弃。
而现场是「离家无网锁车」（车载 MIFI 掉线），恢复网络常常几小时后 ——
两条缺陷叠加的实际效果是**离线场景下这条通知从来不存在**。

**B 端**：判死闸把「A 死前最后一帧」当永久现状用。A 15:36 在 43km/h 上断掉，
出车静默窗口 16:29 到期后，这一帧仍说「车在开 + 人在车上」→ 每拍都判静默，
1h38m 零告警，`alerts.device_offline.last_sent` 始终 0.0（**一次都没尝试发**）。

### A 端改了什么

- **锁车报告 critical**：走 30/60/300/900s 退避、900s 封顶后一直重试、裁剪时优先保留，
  不再 13 分钟就丢。
- **补发队列落盘**：`data/guard_report_queue.json`（mkstemp+fsync+replace 原子写），
  入队 / 重试计数 / 成功出队 / 丢弃出队**四处都落盘**。
- **补发标注**：正文追加「（补发：本条生成于 11-09 16:44，当时无网络；现已补发）」——
  不标注的话，几小时后收到「停车自检」会让人以为车刚被锁。
- 单调钟 `at` 不落盘、读回置 `None`（重启后立刻补发一次），避开「`0.0` 当哨兵被判冷却中」。

### 僚机卡片「升级成功后停在切换中」

三层原因：

1. B 的 root helper 写 `journal.json` 用 `mkstemp` + `0600`，属主是 root，
   而读它的是 guardb → 读不到 → 状态恒为 `idle`（**每次升级成功都会**）。
2. A 的轮询只把 `healthy`/`rolled_back`/`failed` 当终态，`idle` 落进 else
   → 一直打印「切换中…（idle）」且再也不刷新卡片。
3. 「刷新」按钮其实生效了，但 3 秒后轮询把提示改回去 → 看起来像点不动。

修法：轮询终态抽成纯函数 `peerPollKind()`（`idle`/`verified`/空 → 收尾），
收尾时停轮询 + 按版本号判定成功并刷新卡片；新增 `peerRefreshNow()`（先停轮询再拉）；
收尾文案加 `holdMsg` 保护；修掉 `· idle` 的 falsy 兜底灰字。

### 需要配套升级 B 端

本次同时发布 **guardian-b v1.1.4**（走设置页「僚机版本」预置签名包）：

- 判死闸：`driving` 车态证据加保质期（只对 driving 生效，`sentry_off`/`unlocked`
  的长期静默保持——车睡了断电是正常现象），过期即判「设备失联」。
- 告警重复间隔改递增退避 30min→2h→6h→24h（原来固定 30min，离线一整晚推 20 条）。
- 失联文案带「最后车态」（在开 43km/h · 已锁 · D 档），不再答非所问。
- **新增车态板**：抓 TeslaMate 首页（LiveView 服务端渲染，免 token）读
  `locked`/`sentry`/`state`；观察到「未锁→已锁」边沿、宽限 300s 后设备仍失联，
  就由僚机**代发一条停车自检**。这样设备没网也能收到锁车通知。
- 新增 `POST /guard/alert/test`：把「没发」和「发了没到」分开验证。

> `apply.py` / `upgrade.py` 属 `.tools/`（不在升级包内），要修 journal 属主需单独 install。

### 测试

- A 端：`_test_guard_report_queue.py` 新增 **21 项**；守护相关合计 **228 项全绿**
  （21+82+31+29+24+41）；新增 `_test_peer_poll.js`（node）**24/24**。
- B 端：**141 项全绿**（board 11 + judge 33 + http 11 + config 23 + upgrade 50 + upgrade_api 13）。
- 升级包 120 文件，M41 完整性校验缺文件数 0，SSHSIG 本地验签通过。

### 验证方式

1. 先升 B 到 v1.1.4（设置页 → 僚机版本 → 预置 → 企微确认码 → 确认），
   `/guard/status` 里应出现 `state.board = {locked, sentry, state, err}`。
2. 再升 A 到 v0.3.1.83。
3. 开车出去、锁车：正常联网时 A 自己推「停车自检」；设备没网时僚机在锁车约 5 分钟后
   代发「停车自检·僚机代发」；设备真失联时收到「设备失联（在开 43km/h · 已锁 · D 档）」。

SHA256: a01123241a4125d285f320d42f3882f333e46a33f43b0e2c9c69fcbb6133abfd"""


def api(method, path, data=None, binary=None, headers=None, host="api.github.com", abs_url=None):
    url = abs_url if abs_url else f"https://{host}/repos/{REPO}{path}"
    h = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json",
         "User-Agent": "a7z-release"}
    if data is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    elif binary is not None:
        h["Content-Type"] = "application/octet-stream"
        body = binary
    else:
        body = None
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# 0) 本地自检：BODY 里 SHA256 行必须能被设备端正则匹配，且不得含包裹符号
import re as _re
_m = _re.search(r'(?:SHA-?256|sha256)[:\s]+([a-fA-F0-9]{64})', BODY)
if not _m or _m.group(1) != SHA256:
    print("!! BODY 的 SHA256 行无法被设备端正则解析（M76）：", _m)
    sys.exit(1)
if '`' + SHA256 in BODY or SHA256 + '`' in BODY:
    print("!! BODY 的 SHA256 被包裹符号包围（M76）")
    sys.exit(1)
print("BODY SHA256 行格式自检通过:", _m.group(1)[:12], "...")

# -1) 验活 PAT（M88）
#     M91：/user 是根端点，绝不能复用带 /repos/{REPO} 前缀的 api()
st, out = api("GET", None, abs_url="https://api.github.com/user")
if st != 200:
    print(f"!! PAT 不可用（GET https://api.github.com/user → HTTP {st}）——检查 token 或网络（M88）")
    sys.exit(1)
print("PAT 验活通过:", json.loads(out).get("login"))

# 0b) 校验远端 master 已包含本次提交（M61）
st, out = api("GET", "/git/ref/heads/master")
if st == 200:
    remote_sha = json.loads(out)["object"]["sha"]
    if remote_sha != EXPECT_HEAD:
        print(f"!! 远端 master = {remote_sha[:8]}，期望 {EXPECT_HEAD[:8]}：请先 push 再发版（M61）")
        sys.exit(1)
    print(f"远端 master 已对齐: {remote_sha[:8]}")
else:
    print("!! 无法读取远端 master:", st, out[:200]); sys.exit(1)

# 1) 检查 tag 是否已有 release（幂等，M28）
st, out = api("GET", f"/releases/tags/{TAG}")
if st == 200:
    print("!! Release 已存在，先删除重建（M28）")
    rid = json.loads(out)["id"]
    st, _ = api("DELETE", f"/releases/{rid}")
    print("删除旧 release:", st)

# 1b) 检查 tag 本身是否已存在（M61）
st, out = api("GET", f"/git/ref/tags/{TAG}")
if st == 200:
    sha = json.loads(out)["object"]["sha"]
    print(f"!! Tag 已存在指向 {sha[:8]}，删除重建以对齐当前 HEAD（M61）")
    st, _ = api("DELETE", f"/git/refs/tags/{TAG}")
    print("删除旧 tag:", st)

# 2) 创建 release（M61：显式 target_commitish）
st, out = api("POST", "/releases", {"tag_name": TAG, "name": TAG,
                                    "body": BODY, "draft": False, "prerelease": False,
                                    "target_commitish": "master"})
if st not in (200, 201):
    print("!! Release 创建失败:", st, out[:300]); sys.exit(1)
rel = json.loads(out)
print("Release 创建成功 ID:", rel["id"])

# 3) 上传资产 —— 必须走 uploads.github.com（M56）
assets = {}
# 资产名一律从路径推导：从上一版脚本复制时，这两行字面量最容易被漏改，
# 结果是「内容正确、文件名错版」——digest 核对全绿却照样让设备 404（M131）
for path, ctype in [(TARBALL, "application/gzip"), (SIG, "application/octet-stream")]:
    name = os.path.basename(path)
    with open(path, "rb") as f:
        blob = f.read()
    st, out = api("POST", f"/releases/{rel['id']}/assets?name={name}",
                  binary=blob, host="uploads.github.com",
                  headers={"Content-Length": str(len(blob))})
    if st not in (200, 201):
        print(f"!! 资产 {name} 上传失败:", st, out[:200]); sys.exit(1)
    a = json.loads(out)
    assets[name] = a["id"]
    print(f"资产上传成功: {name} ({a['size']} bytes, id={a['id']})")

# 4) 校验资产 digest == 本地 == body（M27/M72）
st, out = api("GET", f"/releases/assets/{assets[os.path.basename(TARBALL)]}")
digest = json.loads(out).get("digest", "")
print("资产 digest:", digest)
print("期望 sha256:", "sha256:" + SHA256)
print("digest 一致:", digest == "sha256:" + SHA256)

print("TAG:", TAG, "RELEASE_ID:", rel["id"])
