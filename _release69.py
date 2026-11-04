#!/usr/bin/env python3
"""v0.3.1.69 Release 创建脚本（从 _release67.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.69"
# M61：tag 必须指向当前远端 master HEAD。若 master 又推进（例如发版后回填 Release ID
# 的 doc commit），重跑前必须把这个常量更新为最新的 master sha，否则预检会拒绝。
EXPECT_HEAD = "2d1cbf1aa184e0f25e33c70983e9e00c47ce683c"
ART = r"D:\teslausb\a7z\.workbuddy\artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.69.tar.gz")
SIG = os.path.join(ART, "teslausb-v0.3.1.69.tar.gz.sig")
SHA256 = "0c63a9edca1b1bcafbc241b8cc3592d5368e313fe7d8037902b4d7834b2deb2f"

BODY = """## v0.3.1.69 — Web 登录保护（可选启用）+ 修复一个会破坏写操作的回归

**升级后默认不启用登录保护，界面和行为与 v0.3.1.67 完全一致。**
想用登录保护，去「设置 → 安全」按顺序配好再手动点「启用」。

### 本版带来什么

新增一套**仅验证码登录**（不需要密码）的访问控制，用于把 5000 端口关起来：

- 登录方式：填邮箱收到的 6 位验证码。验证码有效期 5 分钟、单码最多试 3 次，
  有重发冷却与每日发信上限
- 没网时：连本地网络 + 输入**本地密钥**=完整登录；不输密钥=只读浏览。
  tailnet 远程在未登录时一律拒绝
- 登录成功会踢掉其它已有会话（多设备同时登录时只有最新那个有效）
- 跳转目标不放在网址里（存服务端），避免被伪造成钓鱼链接
- 首次启用要求：只能在本地网络、必须先完成「发测试码 → 回填」闭环，避免配错了把自己关在门外

发信走腾讯企业邮（smtp.exmail.qq.com，465 优先、587 备用，不试 25）。
设置页会把「管理员需先在企业微信管理端开 POP/IMAP+SMTP 权限」这条前置写在页面上。

### 本版修复什么

修掉 v0.3.1.68 里一个**会把管理界面写操作全部打死**的问题（该版未发布，
所以你不会遇到，但记录在此）：

那个版本的访问控制把「防跨站请求伪造」检查放在了「是否启用登录保护」之前，
而 token 全站只在三个地方产生。结果是**即使一次都不启用登录保护**，
切换模式、清理、云上传、重启、保存配置等操作都会返回「CSRF 校验失败」。

现在改成：开关关闭时，除登录接口外的所有请求与之前版本行为一致；
开关打开后，页面会自动带上校验头（同源才加，不会泄露到外部站点）。

### 升级后怎么启用（建议按顺序，别跳步）

1. 「设置 → 安全」填安全邮箱和发送邮箱（两者不能是同一个地址）
2. 点「测试发信」，确认能收到
3. 点「发送测试码到新邮箱」并回填，完成闭环
4. 设置本地密钥（可自定义，10~64 位；留空则自动生成，只显示一次）
5. 点「启用登录保护」
6. **换另一个浏览器验证一次能正常登录**，再关掉旧页面

断网时用本地密钥登录是唯一的完整权限入口，务必先保存好。

### 验证

- 专项回归 24 项：从真实前端源码扫出 53 处写请求路径，按「开关 × 登录态 × 令牌」
  逐条验证未启用时全部放行、启用后按预期拦截
- 前端拦截器 11 项：同源才加、外部域/协议相对不加、已有不覆盖
- 既有回归 178 项全过（认证 50 + 接口 22 + 缓存 17 + 流量 30 + 服务刷新 23 + 清理守卫 36）
- 真机影子验证 59 项：在设备上解压到临时目录跑，全程未触碰正在运行的 v0.3.1.67
- 升级包 119 个文件，与上一版逐文件比对无缺失
- SHA256: 0c63a9edca1b1bcafbc241b8cc3592d5368e313fe7d8037902b4d7834b2deb2f
"""


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

# -1) 验活 PAT（M88：push 走 SSH，成功不代表 PAT 可用）
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
for path, name, ctype in [(TARBALL, "teslausb-v0.3.1.69.tar.gz", "application/gzip"),
                          (SIG, "teslausb-v0.3.1.69.tar.gz.sig", "application/octet-stream")]:
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
st, out = api("GET", f"/releases/assets/{assets['teslausb-v0.3.1.69.tar.gz']}")
digest = json.loads(out).get("digest", "")
print("资产 digest:", digest)
print("期望 sha256:", "sha256:" + SHA256)
print("digest 一致:", digest == "sha256:" + SHA256)

print("TAG:", TAG, "RELEASE_ID:", rel["id"])
