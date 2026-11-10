#!/usr/bin/env python3
"""v0.3.1.86 Release 创建脚本（从 _release82.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.86"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "b17eb1bb37b6484fb70736118aa71476bdaa0a8f"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.86.tar.gz")
# 签名路径**派生自 TARBALL**，绝不另写一遍版本号：
# 2026-11-10 从 84 脚本复制时只替换了 `teslausb-v0.3.1.84.tar.gz"`，
# `.sig` 那行写法不同（带 .sig 后缀）没被命中 → 把 84 的旧签名当成 86 传了上去（M131 复发）
SIG = TARBALL + ".sig"
SHA256 = "ddd13774f3c17d8b9387215ee1e5d6b33bc8336c6b68a0507f42bad1bb16af25"

BODY = """## v0.3.1.86 — 锁车必推（闩锁复位 + 采样自适应）+ 推送文案窄排版

### 问题

「下午出去开车几次，没有每次都推送」。`journalctl` 还原出今天 **6 次真实锁车、2 次无推送**
（16:31 / 19:21）；15:06 那次「无推送」是正确的（同一把锁，`locked` 字段漂移）。

### 根因

锁车报告用「这一把锁已报过」（`_lock_reported`）防重复，但它**只在 `locked=false` 时复位**，
而 TeslaMate 的 `locked` **会滞后**——14:06 锁车后开门上车、开走，它一路仍返回 `true`
（当天三次「`sentry_mode=true` 但 `locked=false`」的 WARNING 就是它漂移的指纹）：

```
14:06 锁车 → 推送，闩锁=True
15:36 开走 → locked 仍报 true → 复位分支走不到 → 闩锁还是 True
16:31 锁车 → 「这一把已报过」→ return，一条都不发
```

真正**铁证的"未锁"信号**（`speed>3` / `shift_state=D,R` / `state=driving`）当时只被用来
"挡住推送"，**不带复位**——看着证据却不解锁。

第二个原因：「立马」物理上做不到——设备上 `states_interval_s = 300`，车态 5 分钟才拉一次。

### 改法

1. **闩锁复位前置**：`moving`（D/R 档 ∨ `speed>3` ∨ `state=driving`）判据提到闩锁判断**之前**，
   命中即 `_lock_reported = False`。这是本次的核心修复。
2. **复位留痕**：打 `锁车闩锁已复位（车在动：档位=D 车速=43）`，以后漏推能一眼看到卡在哪。
3. **采样两档自适应**：新增 `states_fast_s=60`（还没确认锁好时用）/ `states_interval_s=300`
   （已锁并推过后放慢省流量）。锁车检测延迟 ≤60s。
4. **锁车防抖 600 → 120s**：闩锁修准后，长冷却只会误伤「下车拿个东西又回来锁」。
5. 设置页新增「车态快采间隔」（`states_fast_s`，20~300s）。

### 现场时间线重放（修复后）

```
开机首拍（车未锁）
① 锁车 14:06              ★ 推送    闩锁=True
   停着 15:06（locked 漂移）  静默      闩锁=True
② 开走（locked 仍报 true）    静默      闩锁=False   ← 关键：这里松了
③ 锁车 16:31              ★ 推送    闩锁=True    ← 旧版漏的第 1 次
④ 解锁 16:41              静默      闩锁=False
⑤ 锁车 16:51              ★ 推送    闩锁=True
```

同一把锁的重复上报仍然静默（不刷屏）；120s 防抖内的二次锁车被挡住但**不丢闩锁**。

### 同时包含 v0.3.1.85：推送文案窄排版（M140）

企微手机端一行约 26~28 显示列就折行，原模板按桌面宽度写（27/31/33/45 列），
一条 8 行的停车自检被折成 14 行碎块。改为 `_LINE_MAX = 22` + 渲染末端 `_fit()` 统一兜底：

```
【停车自检】✅ 正常
已锁（据哨兵·补报）
停车 03:49 · 电量 61%
网络   正常 C12345
USB    正常 已连接
缩略图 正常 积压1
推送   正常
v0.3.1.86 · 12:41
```

跃迁 / 恢复 / 日报 / B 失联 / B 恢复 五条消息一并改窄（B 端 v1.1.5 同步）。

### 测试

A 端 434 全绿（`_test_guard_lock_report` 103 / 模板 32 / client 139 / api 31 / peer 29 /
off_regression 24 / wecom 41 / upgrade_atomic 14 / queue 21）；B 端 142；前端 24。
锁车核心用例按本次现场构造：`locked` 在开走后仍报 true，验证闩锁被松开、下次锁车重新推送、
且同一把锁不重复。

### 部署后应有的行为

- 开车（60s 采样）→ 停车锁车 → **≤60 秒**收到「停车自检」
- 停一晚照样只推 1 条，不会因 `locked` 漂移刷屏
- 没网锁车：走 `critical` 落盘队列，回网/重启后补发并标注原时刻
- 排查口径：日志里 `锁车闩锁已复位` 与 `停车自检已发出` 应一一对应

SHA256: ddd13774f3c17d8b9387215ee1e5d6b33bc8336c6b68a0507f42bad1bb16af25"""


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
