# A7Z SSH 连接指南

> **文档版本**: v1.0  
> **创建日期**: 2026-05-16  
> **适用场景**: Windows 环境下连接 Radxa A7Z 开发板  
> **状态**: ✅ 已验证有效

---

## 📋 连接信息（快速参考）

| 项目 | 值 |
|------|-----|
| **IP 地址** | `100.116.18.42` (Tailscale VPN) |
| **SSH 端口** | `22` (默认) |
| **用户名** | `radxa` |
| **密码** | `radxa` |
| **Web 界面** | `http://100.116.18.42:5000` |
| **Samba** | `\\100.116.18.42\TeslaCam` (用户 `teslausb` / 密码 `tesla`) |

---

## ⚠️ 重要发现：Windows OpenSSH 客户端已损坏

### 问题现象
```bash
$ ssh radxa@100.116.18.42
# 所有命令返回 255，无任何输出
$ ssh -V
# 返回 255，无版本信息
```

### 影响范围
- ❌ `ssh` 命令完全不可用
- ❌ `scp` 命令完全不可用
- ❌ `sftp` 命令完全不可用
- ✅ **Python paramiko 库不受影响**

---

## ✅ 推荐方案：Python paramiko + SFTP

### 方案优势
1. **绕过损坏的 OpenSSH 客户端**
2. **自动处理 CRLF 问题**（Windows 换行符）
3. **支持二进制写入**（避免脚本格式错误）
4. **可编程**（适合自动化部署）

---

## 🔧 使用方法

### 1. 安装 paramiko
```bash
pip install paramiko
```

### 2. SSH 执行命令（替代 ssh）
```python
import paramiko

# 连接参数
host = '100.116.18.42'
user = 'radxa'
password = 'radxa'

# 创建 SSH 客户端
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=password)

# 执行命令
stdin, stdout, stderr = client.exec_command('ls -la /opt/radxa_data/')
print(stdout.read().decode('utf-8'))

# 关闭连接
client.close()
```

### 3. SFTP 上传文件（替代 scp）
```python
import paramiko

# 连接参数
host = '100.116.18.42'
user = 'radxa'
password = 'radxa'

# 创建 SFTP 客户端
transport = paramiko.Transport((host, 22))
transport.connect(username=user, password=password)
sftp = paramiko.SFTPClient.from_transport(transport)

# 上传文件（二进制模式，避免 CRLF）
with open('local_file.txt', 'rb') as f_local:
    with sftp.open('/opt/radxa_data/remote_file.txt', 'w') as f_remote:
        f_remote.write(f_local.read())

# 关闭连接
sftp.close()
transport.close()
```

### 4. SFTP 下载文件（替代 scp）
```python
import paramiko

# 连接参数（同上）
# ...

# 下载文件
with sftp.open('/opt/radxa_data/remote_file.txt', 'r') as f_remote:
    with open('local_file.txt', 'wb') as f_local:
        f_local.write(f_remote.read())

# 关闭连接
# ...
```

---

## 📝 完整部署脚本示例

### `deploy_to_a7z.py`（推荐）
```python
#!/usr/bin/env python3
"""
部署文件到 A7Z 的完整脚本
- 使用 paramiko SFTP（避免 Windows OpenSSH 损坏问题）
- 自动处理路径（纯正斜杠）
- 二进制写入（避免 CRLF 问题）
"""

import paramiko
import os

# 配置
A7Z_CONFIG = {
    'host': '100.116.18.42',
    'port': 22,
    'username': 'radxa',
    'password': 'radxa',
    'remote_path': '/opt/radxa_data/teslausb/'
}

def deploy_file(local_path, remote_path):
    """部署单个文件到 A7Z"""
    transport = paramiko.Transport((A7Z_CONFIG['host'], A7Z_CONFIG['port']))
    transport.connect(username=A7Z_CONFIG['username'], password=A7Z_CONFIG['password'])
    sftp = paramiko.SFTPClient.from_transport(transport)
    
    try:
        # 二进制上传（避免 CRLF）
        with open(local_path, 'rb') as f_local:
            with sftp.open(remote_path, 'w') as f_remote:
                f_remote.write(f_local.read())
        print(f"✅ 部署成功: {local_path} → {remote_path}")
    except Exception as e:
        print(f"❌ 部署失败: {e}")
    finally:
        sftp.close()
        transport.close()

def deploy_directory(local_dir, remote_dir):
    """递归部署目录到 A7Z"""
    transport = paramiko.Transport((A7Z_CONFIG['host'], A7Z_CONFIG['port']))
    transport.connect(username=A7Z_CONFIG['username'], password=A7Z_CONFIG['password'])
    sftp = paramiko.SFTPClient.from_transport(transport)
    
    try:
        for root, dirs, files in os.walk(local_dir):
            for file in files:
                local_file = os.path.join(root, file)
                # 关键：远程路径用纯正斜杠
                remote_file = remote_dir + local_file[len(local_dir):].replace('\\', '/')
                
                # 确保远程目录存在
                remote_dir_path = remote_file.rsplit('/', 1)[0]
                try:
                    sftp.makedirs(remote_dir_path)
                except:
                    pass
                
                # 二进制上传
                with open(local_file, 'rb') as f_local:
                    with sftp.open(remote_file, 'w') as f_remote:
                        f_remote.write(f_local.read())
                print(f"✅ {local_file} → {remote_file}")
    except Exception as e:
        print(f"❌ 部署失败: {e}")
    finally:
        sftp.close()
        transport.close()

if __name__ == '__main__':
    # 示例：部署 app.py
    deploy_file('app.py', '/opt/radxa_data/teslausb/app.py')
    
    # 示例：部署整个 templates 目录
    # deploy_directory('templates', '/opt/radxa_data/teslausb/templates')
```

---

## 🚫 常见错误与解决方案

### 错误 1: Windows 换行符（CRLF）
**现象**: Shell 脚本在 A7Z 上执行失败，`systemd` 报 `status=203/EXEC`

**原因**: Windows 文本文件使用 `\r\n`，Linux 只认 `\n`

**解决方案**:
```python
# ✅ 正确：二进制写入
with open(local_file, 'rb') as f_local:
    with sftp.open(remote_file, 'w') as f_remote:
        f_remote.write(f_local.read())

# ❌ 错误：文本模式（会转换换行符）
with open(local_file, 'r') as f_local:  # 文本模式
    with sftp.open(remote_file, 'w') as f_remote:
        f_remote.write(f_local.read())  # 可能损坏
```

### 错误 2: 路径混入反斜杠
**现象**: 文件传到 `teslausb\templates` 而非 `teslausb/templates`

**原因**: Windows `os.path.join` 使用 `\`

**解决方案**:
```python
# ✅ 正确：纯字符串拼接
remote_path = remote_dir + '/' + relative_path

# ❌ 错误：os.path.join（Windows 生成反斜杠）
remote_path = os.path.join(remote_dir, relative_path)  # 生成 \
```

### 错误 3: 文件权限丢失
**现象**: 脚本无法执行，`permission denied`

**解决方案**:
```python
# 上传后设置权限
sftp.chmod(remote_file, 0o755)  # rwxr-xr-x
```

---

## 🔄 替代方案对比

| 方案 | 优点 | 缺点 | 推荐度 |
|------|------|------|--------|
| **Python paramiko** | 稳定、自动 CRLF 处理、可编程 | 需要写 Python 代码 | ⭐⭐⭐⭐⭐ (推荐) |
| **WinSCP GUI** | 图形界面、拖拽上传 | 手动操作、不适合自动化 | ⭐⭐⭐ |
| **修复 OpenSSH** | 原生工具链 | 风险高、可能再次损坏 | ⭐ |
| **WSL ssh** | 原生 Linux 环境 | 需要 WSL、路径转换复杂 | ⭐⭐⭐⭐ |

---

## 📌 关键经验教训（2026-05-15）

1. **Windows OpenSSH 客户端可能损坏**
   - 症状：所有命令返回 255，无输出
   - 解决：改用 Python paramiko

2. **SFTP 二进制写入可避免 CRLF**
   - `sftp.open('w')` 二进制模式
   - 比 `scp` + `dos2unix` 更可靠

3. **远程路径永远用正斜杠**
   - 不要用 `os.path.join`
   - 用纯字符串拼接：`dir + '/' + file`

4. **部署后必须重启服务**
   - Flask 模板缓存：`systemctl restart teslausb-web`
   - Shell 脚本：`chmod +x` + `dos2unix`

---

## 🚀 快速开始（复制粘贴即用）

### 测试连接
```python
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('100.116.18.42', username='radxa', password='radxa')
stdin, stdout, stderr = client.exec_command('uptime')
print(stdout.read().decode())
client.close()
```

### 上传文件
```python
import paramiko

transport = paramiko.Transport(('100.116.18.42', 22))
transport.connect(username='radxa', password='radxa')
sftp = paramiko.SFTPClient.from_transport(transport)

with open('app.py', 'rb') as f:
    with sftp.open('/opt/radxa_data/teslausb/app.py', 'w') as remote:
        remote.write(f.read())

sftp.close()
transport.close()
```

---

## 📞 联系方式

| 角色 | 姓名 | 联系方式 |
|------|------|----------|
| 项目负责人 | cnlvan | - |
| 开发工程师 | 小树（AI） | WorkBuddy |

---

**文档结束**

> 📝 **使用说明**: 后续所有 A7Z 连接操作都使用此文档中的 paramiko 方案，不再尝试修复 Windows OpenSSH 客户端。
