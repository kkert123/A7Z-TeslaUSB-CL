#!/usr/bin/env python3
"""部署 usb_gadget_init.sh 到 A7Z"""
import paramiko
import sys

HOST = '100.116.18.42'
USER = 'radxa'
PASS = 'radxa'
REMOTE_PATH = '/opt/radxa_data/usb_gadget_init.sh'
LOCAL_PATH  = r'D:\teslausb\a7z\usb_gadget_init.sh'

def deploy():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            HOST,
            username=USER,
            password=PASS,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
    except Exception as e:
        print(f"SSH 连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    sftp = client.open_sftp()
    try:
        # 备份原文件
        try:
            sftp.rename(REMOTE_PATH, REMOTE_PATH + '.bak')
            print(f"[OK] 已备份原文件 -> {REMOTE_PATH}.bak")
        except Exception:
            print("[WARN] 备份失败（可能已存在 .bak）")

        # 二进制上传，防止 CRLF 损坏
        with open(LOCAL_PATH, 'rb') as f:
            content = f.read()
        with sftp.open(REMOTE_PATH, 'wb') as f:
            f.write(content)
        print(f"[OK] 已部署: {LOCAL_PATH} -> {REMOTE_PATH} ({len(content)} bytes)")

        # 设置执行权限
        stdin, stdout, stderr = client.exec_command(f'chmod +x {REMOTE_PATH}')
        print(f"[OK] 已设置执行权限")

    except Exception as e:
        print(f"[ERR] 部署失败: {e}", file=sys.stderr)
    finally:
        sftp.close()
        client.close()

if __name__ == '__main__':
    deploy()
