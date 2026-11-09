#!/usr/bin/env python3
"""v0.3.1.82 Release 创建脚本（从 _release81.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.82"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "a60b06d5482e59324f4513491b2e966ffd333468"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.82.tar.gz")
SIG = os.path.join(ART, "teslausb-v0.3.1.82.tar.gz.sig")
SHA256 = "8d46c151504a2f3502d34e2b281f4e54cfebc017c75149f410249271c4989e0c"

BODY = """## v0.3.1.82 — 锁车推送真正触发（车态交叉校验 + 重启补报）

### v0.3.1.81 上线后为什么还是收不到

三个问题串成一条链：

**① TeslaMate 的 `locked` 与实际相反**

```
/msg 实时：locked=false   sentry_mode=true   state=online   shift_state=P   speed=0
车机实际：已锁定          哨兵开启
```

哨兵模式只在锁车后开启，所以这个组合里 `locked` 必然是错的（TeslaMate 侧陈旧/漂移）。
而三层兜底只在 `locked` 为空时才启用，这个「有值但错」的 false 让兜底永远没机会纠正。

**② 重启后的首拍把「车已锁」当成「这一把锁已报过」**

10:37 锁车 → 10:48 升级后重启 → 10:49 首拍时车已锁 → 内部标记「已通知」
→ 那条锁车事件永久丢失，之后除非再解锁一次否则永不推送。

**③ 同一份漂移的 false 被当成「真解锁」**

日志实录 `预期离线握手已送达 reason=unlocked window=900s`。
每漂移一次就给见证者开 900 秒静默窗口，恰好关掉「车已锁、设备将断电」这个最需要告警的时刻。

### 改了什么

**车态交叉校验**：哨兵开启且车辆确定停着（不在行驶档、车速 ≤3）时，把 `locked` 按已锁处理，
通知里标「据哨兵推断」。行驶中不做这个推断——否则闩锁会一直卡住，反而吞掉真正的锁车。
顺带修好 `speed` 解析（该接口返回的是字符串 `"0"`，原来只认数字，导致车速闸形同虚设）。

**重启后补报**：服务重启时如果车已经锁着，挂起一条，等启动预热（5 分钟）结束后补发，
文案标「重启后补报」。挂起会在三种情况下作废：车辆解锁、车辆在动、用户关掉这个开关。
冷却初值改为「从未报过」，避免冷启动前 10 分钟被误判成冷却中。

**握手防抖**：哨兵还开着时不判「解锁」；同类跃迁 30 分钟内只发一次，但「出车」不冷却
（一天多次出车是正常事件，冷却会把第二趟的静默窗口吞掉）；冷却改为**送达成功后**才记账，
避免「车刚启动、网络重连中」的发送失败把这次跃迁永久吞掉。

设置页无需改动：开关和冷却时长在 v0.3.1.81 已有（「锁车后推送停车自检」「锁车报告冷却」）。

### 双审查修掉的问题

1 个 P0 + 6 个 P1 + 4 个 P2：

- P0：冷却用 `dict.get(key, 0.0)` 当「很久以前」，而 `time.monotonic()` 返回的是**开机秒数**
  ——开机不足 30 分钟时，任何 reason 的第一次握手都会被判「冷却中」直接丢弃。
  这正好命中「每次用车」和「刚升级重启」两个最关键的时刻。
- P1：`driving` 被一起冷却；冷却在发送前落账；归一化缺「停着」前提；`speed` 字符串被丢弃；
  关掉开关时挂起标记泄漏；测试没覆盖预热期接线。
- P2：冷启动冷却误判；挂 D 档不清挂起；文案背靠背括号；测试平凡通过。

### 测试

`_test_guard_lock_report.py` 从 46 项扩到 **82 项**，新增 F~O 十组：
哨兵矛盾归一、首拍挂起与补报、预热期接线、冷却基准（P0 回归）、`driving` 不冷却、
关开关作废、挂 D 档作废、`speed` 字符串解析、行驶中不归一。

关联套件 139 + 29 + 31 + 24 + 15 + 41 全绿，合计 **361 项**。

### 验证方式

升级后开出去再回来锁车：出车时应看到 `预期离线握手已送达 reason=driving`，
锁车后 30~90 秒内应收到 `【停车自检】车辆已锁车`（TeslaMate 数据仍异常时显示「据哨兵推断」）。

SHA256: 8d46c151504a2f3502d34e2b281f4e54cfebc017c75149f410249271c4989e0c"""


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
