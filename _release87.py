#!/usr/bin/env python3
"""v0.3.1.87 Release 创建脚本（从 _release82.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.87"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "3b92d6fcb2e6314cf344776eae844a3594297ba6"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.87.tar.gz")
# 签名路径**派生自 TARBALL**，绝不另写一遍版本号：
# 2026-11-10 从 84 脚本复制时只替换了 `teslausb-v0.3.1.84.tar.gz"`，
# `.sig` 那行写法不同（带 .sig 后缀）没被命中 → 把 84 的旧签名当成 86 传了上去（M131 复发）
SIG = TARBALL + ".sig"
SHA256 = "40a9aa8598fdc35f79c5104283a39af3b160535cd5d1deaf781e422a929861b6"

BODY = """## v0.3.1.87 — 推送标明来源（A7Z / 僚机）+ WiFi 名称完整显示

### 问题

> 1. 推送过来的信息没有区分是 A7Z 发的还是僚机发的
> 2. WiFi 名称显示不全

截图里：`网络  正常 YL-MIFI-…`（被截断）、标题 `【停车自检】✅ 正常`（没标来源）。

### 根因

**① 来源分不清——A 和 B 共用同一个企微机器人。**
`wecom_guard_webhook_key` 双持有（A 的 `config/sentry.json` + B 的 `data/runtime.json`），
手机里发送方名字都显示「A7Z守护」。**渠道层面没有区分能力，只能靠正文**，而停车自检原来的标题
是中性的 `【停车自检】`。更严重的是 B 端失联告警写 `【A7Z】设备失联` —— 设备失联时 A 已经死了、
根本发不出来，这条只能是 B 发的，标题却让人以为是设备自己报的。

**② SSID 被截成 9 列。** 原来塞进「网络」那一行再 `_clip(ssid, 9)`：
不截是 `网络   正常 YL-MIFI-000500` 共 31 列必折行，截了又恰好把「家宽还是车载热点」
这个关键信息砍掉。

### 改法：方括号里永远是「发送方」

| 发送方 | 标题 |
|---|---|
| A7Z | `【A7Z】停车自检 ✅正常` |
| A7Z | `【A7Z】正常 → 注意` / `【A7Z】已恢复正常` / `【A7Z】自检日报 正常`（原有） |
| 僚机 | `【僚机】停车自检 代发` |
| 僚机 | `【僚机】A7Z 失联` |
| 僚机 | `【僚机】TS 通道故障` |
| 僚机 | `【僚机】A7Z 已恢复` |

`【A7Z】停车自检 ✅正常` 是 22 列（emoji 与档位之间不留空格，留了就是 23 列会折）。

### SSID 独立一行

```
【A7Z】停车自检 ✅正常
已锁（据哨兵）
停车 00:00 · 电量 89%
网络   正常
USB    正常 已连接
缩略图 正常 积压1
推送   正常
WiFi   YL-MIFI-000500
v0.3.1.87 · 19:05
```

`WiFi` 按 7 列标签宽度对齐其余分项；15 列以内完整显示，再长才截断。

### 顺带修两个连带缺陷

| 问题 | 现象 | 修法 |
|---|---|---|
| 网络异常行尾空白 | Tailscale 掉线但 `internet_ok=True` → 只剩 `❗ 网络`，没说原因 | 非 pass 依次判 `出网不通` / `TS 未运行` / `链路异常`（新增 `_TS_CN` 中文化） |
| 异常行被截断 | `⚠ 缩略图 积压128·15秒前` = 24 列 → `积压128·15秒…` | 去掉「前」正好 22 列 |

### guardian-b v1.1.6

代发 `【停车自检·僚机代发】` → `【僚机】停车自检 代发`；设备失联 `【A7Z】设备失联` →
`【僚机】A7Z 失联`；TS 故障 → `【僚机】TS 通道故障`；恢复 → `【僚机】A7Z 已恢复`。

### 测试

A 端 438 全绿（`_test_lock_template` 32→36，新增 A1b「标题以【A7Z】开头」+
C9「SSID 完整」+ C10「网络行不夹带 SSID」+ C11「无 SSID 不出空行」）；B 端 142；前端 24。

### 影响面

纯文案与排版，无逻辑/配置/数据变更。

SHA256: 40a9aa8598fdc35f79c5104283a39af3b160535cd5d1deaf781e422a929861b6"""


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
