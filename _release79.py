#!/usr/bin/env python3
"""v0.3.1.78 Release 创建脚本（从 _release77.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.79"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "1023533498a1611cb4fa1ee887c34954d93a8c80"
ART = r"D:\teslausb\a7z\.workbuddy\artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.79.tar.gz")
SIG = os.path.join(ART, "teslausb-v0.3.1.79.tar.gz.sig")
SHA256 = "b6a2f024bb954bc577e12bbe3ed5aa14717ad8d193ba11bd1606b4d218952b6c"

BODY = """## v0.3.1.79 — 僚机卡片能看清了，企微自检不再"永远黄"

这一版修的都是"用起来才发现"的问题：僚机状态全是灰色、确认码倒计时不动、
点预置后不刷新；以及自检长期报「注意」，而企微推送其实一直是通的。

### 修复 · 僚机卡片（系统 → 版本管理）

- 状态按语义着色：在线绿 / 待确认橙 / 切换中蓝 / 失败回滚红 / 未配置灰
- 确认码倒计时改为**本地时钟每秒递减**（此前只显示僚机返回的一次性快照，永远停在 600 秒）
- 过期后按钮自动禁用并提示「请重新预置」，不再让人点了才看到 confirm_expired
- 点「预置」后 1.5 秒轮询直到僚机进入待确认态；失败时按钮不会卡在「推送并校验中…」
- 确认失败也刷新状态（否则页面停在 verified，预置按钮会被永久锁死）
- 提示适配僚机 v1.1.2：确认码已推企微，不再教人去翻日志

### 修复 · 企微自检（四项里的 wechat）

根因是判据读错了数据源：它读 `push_health.json`（只由旧的哨兵推送写），
而自检报告/告警走 `guard_client` 自己的发送器，成功后只做内存计数、从不落盘 ——
于是"你收到了推送，自检却说 172 天没成功过"。

现在 `guard_client` 落盘自己的 `data/guard_push_health.json`，判据改为
「最近一次尝试成功」，并且单次失败只在 30 分钟内算可疑、没配机器人判 warn
（配置问题不是通道故障）、「发送测试」也计入健康（点一次即可翻正）。

### 双审查修掉的两个 P0

- 前端审查：确认码过期后僚机返回 `expires_in=0` 而账本仍是 verified，
  原本"无条件恢复按钮可点"会让用户必点必失败 → 改为按有效期决定，并明确提示
- 后端审查：`_note_push` 未做异常保护，而它被 B 侧失联通知调用 ——
  抛异常会让「已通知」标记永久卡住，之后真失联也不再通知 → 整函数 try/except 兜住

### 测试

设备侧 **139 + 29 + 31 + 24 = 223 项全绿**（新增 G14a–G14m：并发 16 线程无丢更新、
脏数据不抛异常、测试路径隔离）；页面内联 JS 全部 `node --check` 通过。

### 僚机侧（需单独导入 guardian-b v1.1.2）

确认码生成后自动推送企微，不用再登服务器捞日志。开关 `GUARD_UPGRADE_CODE_NOTIFY`
（默认 1）；群里有他人时建议改单人群或设 0。

SHA256: b6a2f024bb954bc577e12bbe3ed5aa14717ad8d193ba11bd1606b4d218952b6c"""


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
st, out = api("GET", f"/releases/assets/{assets['teslausb-v0.3.1.78.tar.gz']}")
digest = json.loads(out).get("digest", "")
print("资产 digest:", digest)
print("期望 sha256:", "sha256:" + SHA256)
print("digest 一致:", digest == "sha256:" + SHA256)

print("TAG:", TAG, "RELEASE_ID:", rel["id"])
