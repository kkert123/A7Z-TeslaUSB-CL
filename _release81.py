#!/usr/bin/env python3
"""v0.3.1.81 Release 创建脚本（从 _release80.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.81"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "664a97875c1df144dc25d69f10e4be4aeef7a1b2"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.81.tar.gz")
SIG = os.path.join(ART, "teslausb-v0.3.1.81.tar.gz.sig")
SHA256 = "4db312fbfab70c903e7ff3e5369d30decf6f34d2a669ef5e1df280ea3b2c0e4b"

BODY = """## v0.3.1.81 — 锁车后必推「停车自检」+ 车态数据源修正

### 为什么锁车后收不到自检（三个问题叠加）

1. **读错了端点**：守护一直在读 TeslaMate `/states`，但那个端点返回的是
   **服务配置状态**（auto_del / buttons / schedule / mqtt_relay / share_config …20 个键），
   车辆字段其实在 `/msg` 的 `raw_data` 里（扁平 95 键，没有 vehicle_state 那一层）。
   于是 `locked` / `state` / `user_present` **永远是 null**。
2. **没有锁车触发点**：原来只在「自检档位变化」和「每日 20:00」时推，锁车本身不算事件。
3. **兜底扫错了目录**：SentryClips / SavedClips 是**事件子目录**结构
   （`SentryClips/<事件时间>/xxx-front.mp4`），按平铺扫顶层一个 mp4 都命中不了。

第 1 条还有个连带影响：「出车 / 解锁」的自动离线握手依赖 `locked`，
所以它自 v0.3.1.76 起**从未真正生效**——之前每次升级都要手工打 `going_offline`，
根源就在这里。这一版修好后，自动握手会开始工作。

### 改了什么

**数据源**：改读 `/msg` 的 `raw_data`，扁平解析 locked / sentry_mode / is_user_present /
state / ui_park_time_str，新增 shift_state / speed；`detect` 字段标记来源。

**三层兜底**（TeslaMate 拿不到车态时）
- L2：解析最新**已写完**的行车记录仪 mp4 的 SEI，读档位与车速
- L3：看文件名时间 —— `SentryClips` 有新文件 ≈ 已锁车（哨兵只在锁车后启动）；
  `RecentClips` 在写 ≈ 车还在活动

**锁车必推**：检测到「未锁 → 已锁」就推一份完整自检，**不看档位**。
三重防抖：这一把锁只报一次（真实解锁才复位）、报告冷却 600 秒、启动后 5 分钟内不推
（但会继续跟踪边沿，不会丢掉这段时间的锁车事件）。

**设置页**（系统 → 设置 → 守护配置）新增：
- 勾选框「锁车后推送停车自检」
- 数字项「锁车报告冷却（秒）」

### 双审查修掉的问题

2 个 P0 + 6 个 P1，全部处理：
- P0 事件子目录未覆盖（导致哨兵判据永久不可达，测试还给了假绿灯）
- P0 兜底推断的「未锁」会触发解锁握手，反复给僚机开静默窗口 ——
  恰好关在「车已锁、设备将断电」这个最需要告警的时刻
- P1 车休眠时 `locked` 在 True→None→True 抖动，会反复推送 → 改用闩锁
- P1 SEI 解析失败不缓存、且跳过的是单个摄像头文件而非整组 → 分组跳过 + 失败缓存
- P1 车机时间超前时 age 算成负数 → 视为不可信
- P1 前端字段缺失时把开关显示成「已勾选」→ 改为 fail-closed
- P1 启动预热期内锁车事件会永久丢失 → 预热期只跟踪不推送

### 测试

新增 46 项（扁平解析 / "nil" 三态 / 三层降级 / 事件子目录 / 锁车边沿 /
抖动不重复 / 冷却 / 档位与车速闸 / 握手不采信推断值）。
关联套件 139 + 29 + 31 + 24 + 15 + 41 全绿，合计 **325 项**。

### 已知限制

`SentryClips` 只在真的发生过哨兵事件时才产生新文件。所以「锁车但没触发哨兵」时，
兜底判不出锁车，只能靠 TeslaMate。也就是说**锁车推送的可靠性以 TeslaMate 可用为前提**。

SHA256: 4db312fbfab70c903e7ff3e5369d30decf6f34d2a669ef5e1df280ea3b2c0e4b"""


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
