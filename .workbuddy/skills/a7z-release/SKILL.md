---
name: a7z-release
description: A7Z TeslaUSB 通用发布流程：修改代码 → 审查 → 模拟运行 → GitHub 同步 → 发布升级包
color: blue
agent_created: true
---

# A7Z TeslaUSB 通用发布流程

## 触发条件

- **默认发布**："发布新版本"、"同步到 GitHub"、"发布升级包" → 走步骤 1-7，不热更新
- **发布并热更新**："发布 A7Z 新版本并热更新"、"部署到 A7Z" → 加步骤 8

## 流程总览

```
修改代码 → 语法检查 → 模拟运行 → 审查确认 → commit+tag → push GitHub → 构建tar.gz+签名 → 创建Release → (可选)热更新A7Z
```

## 前置条件

| 项目 | 值 |
|------|-----|
| GitHub PAT | `D:\teslausb\a7z\.github-pat`（已 `.gitignore`） |
| A7Z Web | `http://100.116.18.42:5000` |
| A7Z SSH | `100.116.18.42:22` `radxa:radxa` |
| GitHub 仓库 | `kkert123/A7Z-TeslaUSB-CL`（公开） |
| 升级私钥 | `D:\teslausb\a7z\upgrade_key` |
| A7Z 部署路径 | `/opt/radxa_data/teslausb` |
| 构建产物目录 | `.workbuddy/artifacts/` |

---

## 步骤 1: 修改代码

- 根据用户需求修改对应文件
- 常见修改范围：`templates/`、`utils/`、`routes/`、`*.py`、`static/`
- 改版本号：`config.py` 中 `APP_VERSION = "X.Y.Z"`

---

## 步骤 2: 语法检查

对所有修改过的 Python 文件做 AST 解析：

```bash
python -c "import ast; ast.parse(open('FILE.py').read()); print('FILE.py: OK')"
```

---

## 步骤 3: 模拟运行

根据修改内容选择验证方式：
- **纯 Python 模块**：`import <module>` 看是否报错
- **Flask 路由**：确认函数签名正确、decorator/blueprint 注册无误
- **模板 HTML**：确认 Jinja2 变量名与后端 ctx 一致
- **关键边界**：空目录、无网络、缺依赖时是否优雅降级

---

## 步骤 4: 审查代码

用 `@skill:easy-code-review` 做需求符合性检查：
- 每个文件改动是否直接服务需求
- 有无无关修改
- 硬编码密钥检查
- try-except 覆盖

---

## 步骤 5: commit + push

**推送走 SSH**（`git remote` 已指向 `git@github.com:kkert123/A7Z-TeslaUSB-CL.git`，Deploy Key `~/.ssh/a7z_github`）。
HTTPS+PAT 推送会挂在 `github.com:443`（M48），仅作兜底。

```bash
cd /d/teslausb/a7z
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy   # M49：先清本机代理

# 只提交本次变更集（工作区常年有历史 drift，勿 git add -A / 勿 git rm，见注）
git add <本次改动的文件...>
git commit -m "vX.Y.Z: 描述"

# push 当前分支 master（无独立 tag 步骤，Release 由 API 建 tag）
git push origin master
```

> ⚠️ **本机 `git rm <file>` 会连父目录一起删**（已受控验证）；清理/新增一律只用 `git add` / `git checkout`。
> Release 的 tag 由步骤 7 的 API 创建，无需本地 `git tag`。

---

## 步骤 6: 构建升级包 + 签名

用仓库内脚本 **`build_release.py`** 打包（从 `deploy_manager.py` 的 `Config.MANAGED_FILES` 白名单生成，
平铺、按白名单顺序；v0.3.1.55 起随仓库留存，勿再手工 `tar czf`）：

```bash
cd /d/teslausb/a7z
python build_release.py            # → .workbuddy/artifacts/teslausb-v<APP_VERSION>.tar.gz

# ── M41 完整性校验（必做，输出必须为空）──
OLD=.workbuddy/artifacts/teslausb-v<上一版>.tar.gz
NEW=.workbuddy/artifacts/teslausb-v<新版>.tar.gz
comm -23 <(tar tzf $OLD | sort) <(tar tzf $NEW | sort)

# 确认 requirements.txt 在包内（M33）
tar tzf $NEW | grep -c '^requirements.txt$'

# Ed25519 签名（OpenSSH 私钥，SSHSIG 格式；openssl dgst 用不了这把钥匙）
ssh-keygen -Y sign -f upgrade_key -n file $NEW
sha256sum $NEW
```

> 本地可复现设备端校验法确认签名有效：
> ```bash
> TMP=$(mktemp); echo "a7z-upgrade ssh-ed25519 <PUBKEY> a7z-upgrade" > "$TMP"
> ssh-keygen -Y verify -f "$TMP" -I a7z-upgrade -n file -s $NEW.sig < $NEW   # → Good "file" signature
> ```

---

## 步骤 7: 创建 GitHub Release + 上传 assets

**从上一版成功脚本复制改造（`_release<N>.py`），不要凭记忆重建**（M56）。关键点：

```python
# 1) 幂等：tag 已存在先 DELETE 再重建（body 修改会被缓存，M28）
# 2) 创建 release —— 必须显式 target_commitish="master"（M61）：
#    不指定时 tag 打在默认分支 HEAD；且 /releases 列表按日期排序，新 release 会"消失"
api("POST", "/releases", {"tag_name": TAG, "name": TAG, "body": BODY,
                          "target_commitish": "master", "draft": False, "prerelease": False})
# 3) 上传资产 —— ⚠️ 必须走 uploads.github.com（api.github.com/releases/{id}/assets 会 404，M56）
api("POST", f"/releases/{rel['id']}/assets?name={name}", binary=blob, host="uploads.github.com")
```

运行前 `unset HTTP_PROXY HTTPS_PROXY`，用 PAT（`cat .github-pat`，勿打印）：`python _release<N>.py`

> **body 的 SHA256 行必须是纯文本 `SHA256: <64hex>`** —— 禁止反引号/引号等任何包裹字符（M76）。

**发布后三方核对**（tag-sha / asset digest / body 声明）：

```bash
# asset 官方 digest（GitHub 对上传字节流算的 sha256，免下载，避开直链被阻断）
curl -s -H "Authorization: token $(cat .github-pat)" \
  "https://api.github.com/repos/kkert123/A7Z-TeslaUSB-CL/releases/assets/<asset_id>" \
  | python -c "import sys,json;print(json.load(sys.stdin)['digest'])"
# 应与本地 sha256sum 一致；tag sha 用 GET /git/ref/tags/{TAG} 核对 == push 的 HEAD
```

---

## 步骤 8（可选）: A7Z 热更新 + 验证

> **触发条件**：仅当用户明确要求"热更新"、"部署到 A7Z"、"发布并热更新"时执行。
> 默认发布流程到此结束（代码已推 GitHub、Release 已发布、设备可通过系统页升级）。

```python
import paramiko, time, urllib.request, json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('100.116.18.42', port=22, username='radxa', password='radxa', timeout=8)

# SFTP 上传修改的文件
sftp = ssh.open_sftp()
for local, remote in <(本地文件, /opt/radxa_data/teslausb/远程路径) 列表>:
    sftp.put(local, remote)
sftp.close()

# 重启服务
ssh.exec_command("echo 'radxa' | sudo -S systemctl restart teslausb-web", timeout=15)
time.sleep(4)

# 验证
r = urllib.request.urlopen('http://100.116.18.42:5000/api/version/check', timeout=5)
d = json.loads(r.read().decode())
print(f"current={d['current']}, latest={d['latest']}, has_update={d['has_update']}")

ssh.close()
```

---

## 关键约束（每次必读）

1. **PAT 永远从文件读取**：`TOKEN=$(cat .github-pat)` — 不硬编码，不打印
2. **MCP GitHub 只有读权限**（403），写操作必须走 curl + PAT
3. **git push 关凭证助手**：`-c credential.helper=` + `GIT_TERMINAL_PROMPT=0`
4. **sudo 无 tty**：用 `echo 'pwd' | sudo -S`
5. **版本检测缓存 6h**（成功结果缓存 6h）；急用可 `systemctl restart teslausb-web` 清缓存
6. **Release body 必须含 `SHA256: <64hex>`，冒号后紧跟 hex、禁止任何包裹符号**（反引号/引号都不行）——设备端 `version_service.py` 正则 `(?:SHA-?256|sha256)[:\s]+([a-fA-F0-9]{64})` 提取，包裹字符会导致解析失败、升级被前端拦下（**M76**）
7. **tag 用 `target_commitish=master`**，且 `GET /git/ref/tags/{TAG}` 的 sha == push 的 HEAD（M61）
8. **资产上传走 `uploads.github.com`**（M56）；发布后核对 asset `digest` == 本地 sha256sum == body 声明（M26/M72）
9. **构建包要包含 deploy 白名单全部文件**：见 `deploy_manager.py:Config.MANAGED_FILES`（用 `build_release.py` 生成）
10. **包-仓库一致性核对**（M84）：包从**工作树**打包，与 git 解耦。构建后逐文件比 `git show HEAD:<name>`（行尾归一化后比 sha256），差异非 0 即说明仓库落后/缺文件 → 先提交再发版，否则 `git clone tag` 无法复现发布包。
    ```python
    # 逐文件核对（行尾归一，避免 CRLF 假阳性）
    import tarfile,hashlib,subprocess
    def norm(b): return b.replace(b"\r\n",b"\n")
    t=tarfile.open(NEW,"r:gz")
    for m in t.getmembers():
        if not m.isfile(): continue
        h=hashlib.sha256(norm(t.extractfile(m).read())).hexdigest()
        r=subprocess.run(["git","show","HEAD:"+m.name],capture_output=True)
        if r.returncode or hashlib.sha256(norm(r.stdout)).hexdigest()!=h:
            print("DIFF/MISSING:",m.name)
    ```
11. **签名私钥绝不能进仓库**（M84）：`upgrade_key`（Ed25519 签名私钥）必须被 `.gitignore` 精确忽略。提交前 `git check-ignore -v upgrade_key` 确认命中；`upgrade_key.pub`（公钥）则需**可跟踪**（要随包分发）。新增文件提交前跑一遍密钥正则扫描。**本机禁用 `git add -A`**（工作树常年有历史 drift，会夹带无关改动与潜在密钥）。
