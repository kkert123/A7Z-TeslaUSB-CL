#!/usr/bin/env python3
"""v0.3.1.88 Release 创建脚本（从 _release82.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.88"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "a9df8bff6a795abb1357e1fb7808f4051a1e2779"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.88.tar.gz")
# 签名路径**派生自 TARBALL**，绝不另写一遍版本号：
# 2026-11-10 从 84 脚本复制时只替换了 `teslausb-v0.3.1.84.tar.gz"`，
# `.sig` 那行写法不同（带 .sig 后缀）没被命中 → 把 84 的旧签名当成 86 传了上去（M131 复发）
SIG = TARBALL + ".sig"
SHA256 = "5d8016f413c00cb06f92a6a3e47d16f1fdd33e0938cb7f6d7bd8a01d00837e3a"

BODY = """## v0.3.1.88 — 僚机版本回退 + 「检查版本」不再被 6 小时缓存挡住

### 问题

> 1. 在版本管理中僚机也没有版本回退（功能）按键。
> 2. 每次都是在"新版本更新说明"里先看到新版，然后要点击"重启服务"，再按"检查版本"才能检查到新版本。

### 问题 2：6 小时缓存吃掉了「检查版本」

`CHECK_INTERVAL = 21600`（成功缓存 6h）、`MIN_REQUEST_INTERVAL = 600`，
而「检查版本」调的是**不带 force** 的 `/api/version/check` → 6 小时内点多少次都是内存缓存。
「重启服务」把进程里的 `_version_last_check` 清零了，这才是"重启就好"的真相。

**修法**：路由支持 `?force=1` → `check_latest_release(force=True)`（绕过 6h/10min 缓存），
前端按钮带上；同时新增 `FORCE_MIN_INTERVAL = 30`，连点复用缓存，别把 GitHub 匿名配额打光。

### 问题 1：僚机补上版本回退

原先 B 端只有「升级失败自动回滚」，没有用户主动回退。

| 层 | 改动 |
|---|---|
| B `app.py` | `list_releases()`（降序、标 `ok`）+ `GET /guard/releases` + `POST /guard/rollback` |
| B `deploy/apply.py` | `do_rollback()`：切软链 → 重启 → 健康检查，**失败自动切回原版本** |
| A `guard_client.py` | `peer_list_releases()` / `peer_rollback()`（只转发） |
| A 路由 + 页面 | 僚机卡片「回退到 [下拉] [回退]」，二次确认，仅列可运行且非当前版本 |

几个刻意的取舍：

- **回退失败必须切回去** —— 退到一个跑不起来的版本等于守护没了，比不回退严重得多
- **回退后不 `prune`** —— 刚离开的版本留着，可能马上要退回来（本地保留 3 个）
- **不动 `.trusted/floor`** —— 那是「不许降级到有漏洞版本」的安全下限
- **校验全在 B 端** —— A 只转达，避免两套判据打架
- **升级中（committed/prepared/verified）一律 409** —— 两条流程不能同时改 `current`

### 部署必看：先换 `apply.py`

`apply.py` 在 `/opt/guard-b/.tools/`，**不在升级包内**（它能写 `/opt/guard-b`，
被包覆盖等于提权）。只升 B 包不换 helper 的话，回退请求会被接受但什么都不发生。

所以 B 端加了进门检查：`/guard/rollback` 先 grep `.tools/apply.py` 有没有
`def do_rollback(`，没有就返回 **501 `helper_too_old`**，绝不静默成功。

```
sudo install -m 0500 -o root -g root /tmp/apply.py /opt/guard-b/.tools/apply.py
```

### 测试

真机（Radxa Cubie A7Z / Python 3.9.2）：
`test_upgrade_api` **20/20（0 跳过）**、`test_apply_rollback` **12/12**（含"回退失败自动切回"）。
本地：`_test_version_force.py` **15/15**；A 端 10 套 + B 端 6 套 + 前端 24 全绿。

SHA256: 5d8016f413c00cb06f92a6a3e47d16f1fdd33e0938cb7f6d7bd8a01d00837e3a"""


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
