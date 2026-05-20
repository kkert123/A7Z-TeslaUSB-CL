#!/usr/bin/env python3
"""
部署修复后的 app.py 到 A7Z 设备
"""

import paramiko
import sys

A7Z_IP = '100.116.18.42'
A7Z_USER = 'root'
A7Z_PORT = 22

LOCAL_FILE = r'D:\teslausb\a7z\app_fully_fixed.py'
REMOTE_FILE = '/opt/radxa_data/teslausb/app.py'

def deploy_to_a7z():
    """部署修复后的 app.py 到 A7Z"""
    try:
        # 创建 SSH 客户端
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        print(f"🔌 连接到 A7Z ({A7Z_IP})...")
        ssh.connect(A7Z_IP, port=A7Z_PORT, username=A7Z_USER)
        
        # 备份原文件
        print("📦 备份原 app.py...")
        stdin, stdout, stderr = ssh.exec_command(
            'cp /opt/radxa_data/teslausb/app.py /opt/radxa_data/teslausb/app.py.backup_$(date +%Y%m%d_%H%M%S)'
        )
        stdout.read()
        
        # 使用 SCP 上传文件
        print(f"🚀 上传修复后的 app.py...")
        sftp = ssh.open_sftp()
        sftp.put(LOCAL_FILE, REMOTE_FILE)
        sftp.close()
        
        # 设置权限
        print("🔐 设置文件权限...")
        stdin, stdout, stderr = ssh.exec_command(f'chmod 644 {REMOTE_FILE}')
        stdout.read()
        
        # 测试 Python 语法
        print("🧪 测试 Python 语法...")
        stdin, stdout, stderr = ssh.exec_command(f'python3 -m py_compile {REMOTE_FILE}')
        stdout.read()
        err = stderr.read().decode()
        
        if err:
            print(f"❌ 语法错误:\n{err}")
            print("⚠️ 请手动检查修复后的文件")
            return False
        else:
            print("✅ 语法检查通过！")
        
        # 重启服务
        print("🔄 重启 teslausb-web.service...")
        stdin, stdout, stderr = ssh.exec_command('systemctl restart teslausb-web.service')
        stdout.read()
        
        # 检查服务状态
        import time
        time.sleep(2)
        
        stdin, stdout, stderr = ssh.exec_command('systemctl is-active teslausb-web.service')
        status = stdout.read().decode().strip()
        
        if status == 'active':
            print("✅ 服务重启成功！")
            print(f"\n🎉 部署完成！请访问 http://{A7Z_IP}:5000 测试")
            return True
        else:
            err = stderr.read().decode()
            print(f"❌ 服务启动失败: {err}")
            print("⚠️ 请手动检查日志: journalctl -u teslausb-web.service -n 50")
            return False
        
    except Exception as e:
        print(f"❌ 部署失败: {e}")
        return False
    finally:
        try:
            ssh.close()
        except:
            pass

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 TeslaUSB A7Z 部署工具")
    print("=" * 60)
    
    if not os.path.exists(LOCAL_FILE):
        print(f"❌ 本地文件不存在: {LOCAL_FILE}")
        sys.exit(1)
    
    success = deploy_to_a7z()
    
    if success:
        print("\n✅ 部署成功！")
    else:
        print("\n❌ 部署失败，请查看上方错误信息")
        sys.exit(1)
