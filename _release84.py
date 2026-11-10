#!/usr/bin/env python3
"""v0.3.1.84 Release 创建脚本（从 _release82.py 复制改造，M56）

注意：BODY 里 SHA256 行必须是纯文本 `SHA256: <64hex>`，不得加反引号/加粗等包裹，
否则设备端正则 (?:SHA-?256|sha256)[:\\s]+([a-fA-F0-9]{64}) 失配 → 升级被拒（M76）。
"""
import json, os, sys, urllib.request

TOKEN = open(r"D:\teslausb\a7z\.github-pat").read().strip()
REPO = "kkert123/A7Z-TeslaUSB-CL"
TAG = "v0.3.1.84"
# M61：tag 必须指向当前远端 master HEAD。
EXPECT_HEAD = "3de93cf1fb44b1b669c7c42260bea954e0bdf40f"
ART = "D:/teslausb/a7z/.workbuddy/artifacts"
TARBALL = os.path.join(ART, "teslausb-v0.3.1.84.tar.gz")
SIG = os.path.join(ART, "teslausb-v0.3.1.84.tar.gz.sig")
SHA256 = "e769e5b281aa84f78e8cf3022f7d4ee2ee1406eb97d6c60217c8a11aabc64598"

BODY = """## v0.3.1.84 — 升级/回退不再毁配置（原子替换 + 备份自检 + 回退必重启）

### 11-10 那次升级到底发生了什么

升到 v0.3.1.83 后出现三件互相牵连的怪事：页面「僚机安全登录信息」变空、守护线程未运行；
每 30 秒一条 `sqlite3.OperationalError: no such table: delete_queue`；
回退到 82 后页面仍显示「当前 v0.3.1.83」，再点回退却说「当前已是 v0.3.1.82，无需回退」。

根因（journal 有直接证据）：

- `_restore_user_data` 用 `rmtree` + `copytree` 替换 `config/`、`data/`，中间有「目录不存在」的窗口
  —— 09:03 进程报 `No such file: /opt/radxa_data/teslausb/config/sentry.json`
- 在线复制 WAL 模式的 SQLite（`.db` 与 `-wal` 两个文件）→ 只存在于 WAL 的表跟着丢
- `_save_user_data` 整段 `except: pass`，`saved_cfg` 先赋值再 copytree → 失败留半份、恢复照单全收
- 备份只落 `/tmp/upgrade-keep-*`（finally 删除），`_backup_current` 不校验 base 目录
  → 09:08 产出 `teslausb-vteslausb.tar.gz`，**8994 B** 的空包
- `do_rollback` 只切软链**不重启**，前端却承诺「并重启服务」

### 改了什么

**① 原子替换**：`_copy_dir_atomic` —— 先拷到 `config.new-<ts>`，校验文件齐全，旧目录改名
`config.old-<ts>` **保留**，再 `rename` 顶上。同目录 rename 是原子的：读侧要么看到旧的、
要么看到新的，**不存在"目录不存在"的瞬间**。旧目录保留一份可人工回捞。

**② SQLite 一致性**：保存前 `PRAGMA wal_checkpoint(FULL)`；复制后对每个 `.db` 做
`PRAGMA quick_check`，不通过就用 `VACUUM INTO` 从源库重导一致快照，仍不通过则中止升级。

**③ 失败 fail-loud**：保存/恢复失败抛 `UserDataError`，调用方**在切软链之前中止**
（现有版本完全不受影响）；备份失败不再"警告后继续"——没有备份就没有退路，宁可这次不升。

**④ 备份自检**：base 必须是真实版本目录（realpath + 版本号正则）、包内必须含
`app.py` + `config/`、体积 ≥ 200 KB，任一不满足直接拒绝。

**⑤ 回退必重启 + 混合态可见**：回退成功后自动重启并提示「正在重启服务…」；
`/api/version/check` 新增 `running_version`/`disk_version`/`mixed`，页面在版本卡片下方
显示橙色条「运行中 v83，磁盘上是 v82（回退/升级后未重启）」+ 一键重启按钮。

**⑥ 软链切换拒绝"真实目录"布局**：遇到真实目录直接报错（请先手工迁移），不再 rmtree 整个部署。

### 停车自检推送改版（样式 2）

```
【停车自检】✅ 正常 · 车辆已锁（据哨兵推断）
车辆    P 档 · 停车 00:24 · 电量 67%
网络    正常（C12345 · 出网正常 · TS Running）
USB链路 正常（mode=present · 已连接 · 状态2秒前）
缩略图  正常（服务active · 积压0 · 扫描1秒前）
企微推送 正常（无待发消息）
v0.3.1.84 → 100.111.252.121:8899 · 09:40
```

有异常时异常项置顶（带 ⚠ 与证据）、正常项压成一行「其余」、末尾给一句建议；
新增「电量」（`/msg` 的 `battery_level`）；停车时长去掉秒；中文按 2 列宽对齐；
pass 态不再出现「最近无成功记录」这种自相矛盾的证据。

### 测试

- `_test_upgrade_atomic.py`（新增）**19/19**——本机 14 项，设备 Linux 上 19 项全绿（含符号链接用例）
- `_test_lock_template.py`（新增）**21/21**
- 守护相关 21+82+29+31+24+41 全绿；`_test_peer_poll.js` 24/24

### 升级注意

本次安装仍由**旧的 0.3.1.82 代码**执行（新逻辑从下一次升级起生效）。安装前建议手工留备份：

```
sudo cp -a /opt/radxa_data/teslausb/config /opt/radxa_data/config.bak-$(date +%m%d-%H%M)
sudo cp -a /opt/radxa_data/teslausb/data   /opt/radxa_data/data.bak-$(date +%m%d-%H%M)
```

SHA256: e769e5b281aa84f78e8cf3022f7d4ee2ee1406eb97d6c60217c8a11aabc64598"""


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
