#!/usr/bin/env python3
"""v0.3.1.80 Release 创建脚本（从 _release79.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.80"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "6f6d861338fd5e34a0132ac7e8b8b94b313151d8"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.80.tar.gz")
SIG = os.path.join(ART, "teslausb-v0.3.1.80.tar.gz.sig")
SHA256 = "983619f253ec490c95643cff8c436e447415089e61c5d1c7ea4911c5e8519807"

BODY = """## v0.3.1.80 — 企微机器人开关死锁修复

### 修复 · 「A7Z 守护」勾选启用立刻弹回，永远启用不了

现象：设置页「健康与通知 → 企业微信推送 → A7Z 守护」点「已启用」，勾选框立刻回到禁用。
它从未配过 KEY，而修 key 与开开关互为前提，形成死锁：

- 勾选启用 → 输入框为空时前端直接重载列表，一个请求都不发，勾选被静默回滚
- 填 key 保存 → 保存动作读取勾选框状态，而它刚被回滚成 false，于是 key 被写进
  `wecom_guard_webhook_key_disabled`，主字段仍为空
- 再勾选 → 输入框已被清空，回到第一步

顺带解释了「自检长期显示注意」：守护机器人 key 不在主字段，自检的 wechat 项判
「未配置」，四项里只要一项 warn 整体就是注意。

### 改了什么

前端（系统页）

- 切换开关只提交开关状态，后端用既有 key 兜住 → 「有 key 但被禁用」能正常回到启用
- 三态渲染：已启用 / 已禁用 / 未配置；未配置时勾选框置灰，并在下方显示**可见**的
  橙色指引（气泡提示在触屏和车机上不触发，等于没写）
- 切换失败显示具体原因（缺 KEY 会明确提示去填 key 后再点保存），不再无声回滚
- 成功后提示放在列表重建之后，避免刚显示就被覆盖
- 30 秒自动刷新不再打断操作：正在输入 key、有未保存内容、或刚点了切换时不重建列表
  （这条原本会清空正在粘贴的 key，也可能把刚切换的状态刷回旧值）
- 首次保存默认即启用，符合「配置并启用」的语义

后端（企微配置接口）

- 缺 KEY 时返回可识别的错误码，前端据此给出「去填 key」的指引而不是笼统失败
- key 一律去首尾空格；格式校验只针对**用户新输入**的值
  （沿用历史值时若也校验，用户会出现既开不回来也关不掉的情况）
- enabled 严格按布尔处理（字符串 "false" 曾被当成启用，方向正好相反）
- 写配置后保持原文件权限（此前会从 0644 收窄为 0600，同机其它用户读不到）

### 测试

新增 `_test_wecom_toggle.py` **41 项**，覆盖：无 key 切换的提示码、首次保存、
只发开关的关闭/重新启用（核心回归点）、换 key 同时禁用、字符串布尔被拒、
历史非法 key 只切开关必须成功、首尾空格归一、无效机器人、前端静态断言。

关联套件 139 + 29 + 31 + 24 + 15 全绿，合计 **279 项**；页面内联 JS 通过 node 语法检查。

### 已知未做（不阻塞）

- 两个机器人同时保存时，后写者可能用旧快照覆盖前写者（需文件锁）
- 用户**主动禁用**守护机器人后，自检仍判 warn；应区分「未配置」与「主动关闭」

SHA256: 983619f253ec490c95643cff8c436e447415089e61c5d1c7ea4911c5e8519807"""


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
