#!/usr/bin/env python3
"""v0.3.1.89 Release 创建脚本（从 _release82.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.89"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "d5a00ffcabcfd5072fa8ff82632fc64b2790fe94"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.89.tar.gz")
# 签名路径**派生自 TARBALL**，绝不另写一遍版本号：
# 2026-11-10 从 84 脚本复制时只替换了 `teslausb-v0.3.1.84.tar.gz"`，
# `.sig` 那行写法不同（带 .sig 后缀）没被命中 → 把 84 的旧签名当成 86 传了上去（M131 复发）
SIG = TARBALL + ".sig"
SHA256 = "5f3c7ec771ddc5350282cb8da0b3e1b4a486a9efbf7f085dcc860cdeb9c53c2b"

BODY = """## v0.3.1.89 — 僚机确认码过期后不再死锁

### 现象

```
僚机版本：v1.1.5 · 已就绪待确认     A 端包库：v1.1.7 (30KB)、v1.1.5 (27KB)
[上传僚机包] [预置 v1.1.7 到僚机(灰)] [确认码 6 位] [确认切换(灰)]
确认码已过期，请重新点「预置到僚机」生成新码（包不用重传）。
```

红字让你去点「预置到僚机」，而那个按钮是灰的 —— **无路可走**。

### 根因

`canUp` 里 `st !== 'verified'` 把所有「有待确认 job」的状态一律禁掉（本意是别把正在
等确认的 job 覆盖掉）；但确认码只有 600 秒，过期后 B 的 `public_status()` 只把
`confirm_expires_in` 归 0，`state` 仍留在 `verified`（不会自己回 idle）——于是
「码已过期」这个新情况落进了「一律禁」的分支，而提示文案还指着那个按钮。

`verified` 写在 journal 里，所以刷新页面、重启 B、等一小时都不会恢复。

### 改法（纯前端，B 不用重新打包）

| # | 改动 |
|---|---|
| 1 | 抽出纯函数 `peerPrepDecision(st, expiresIn, upgradeEnabled, newest, peerVer)` → `{can, label}`：`committed` 禁；`verified` **且码有效** 禁；`verified` **且码已过期** → 放开，标签「重新预置 v1.1.7（换新码）」 |
| 2 | 过期文案改指「换新码」/「重新预置」，不再指向被禁用的按钮 |
| 3 | 新增「换新码」按钮（仅过期时出现），复用 `peerUpgrade()`：重新暂存同一个包并换新 job + 新码 |
| 4 | 倒计时归零那一刻调 `peerOnCodeExpired()` 直接放开按钮，不等 30 秒轮询 |

为什么"过期后允许重新预置"安全：B 的 `stage_package` 只拒 `committed`，且要求
`version > current_version`、`from` 含当前版本 —— 重新预置 v1.1.7（B 跑 1.1.5）合法，
旧 job 被覆盖、旧码自然失效。

### 测试

`_test_peer_poll.js` 24 → **40** 项，核心回归是 `5.4 待确认但码已过期 → 可重新预置`；
另有 `5.6/5.7`（字符串 "0"、负数都算过期）、`5.12`（换新码按钮接线）、
`5.13`（不再硬编码 `st !== 'verified'`）、`5.16`（过期文案不再指向被禁按钮）。
A 端 10 套 + B 端 6 套 + 前端全绿。

### 影响面

纯前端 + 测试。无接口、无配置、无数据结构变更；B 端 v1.1.7 包继续用，不必重打。

SHA256: 5f3c7ec771ddc5350282cb8da0b3e1b4a486a9efbf7f085dcc860cdeb9c53c2b"""


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
