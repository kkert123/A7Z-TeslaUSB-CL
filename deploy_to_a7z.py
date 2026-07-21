#!/usr/bin/env python3
"""
Deploy app.py and media_service.py to A7Z via paramiko SFTP
Windows OpenSSH is broken - use paramiko instead (see A7Z连接指南.md)
Binary write mode to avoid CRLF issues.
"""

import paramiko
import os
import sys

A7Z_CONFIG = {
    'host': '100.116.18.42',
    'port': 22,
    'username': 'radxa',
    'password': 'radxa',
    'remote_path': '/opt/radxa_data/teslausb/'
}

def deploy_file(local_path, remote_path):
    """Deploy a single file to A7Z via SFTP binary mode."""
    transport = paramiko.Transport((A7Z_CONFIG['host'], A7Z_CONFIG['port']))
    transport.connect(username=A7Z_CONFIG['username'], password=A7Z_CONFIG['password'])
    sftp = paramiko.SFTPClient.from_transport(transport)

    try:
        with open(local_path, 'rb') as f_local:
            with sftp.open(remote_path, 'w') as f_remote:
                f_remote.write(f_local.read())
        if remote_path.endswith('.py'):
            sftp.chmod(remote_path, 0o644)
        print("[OK] Deployed: " + local_path + " -> " + remote_path)
    except Exception as e:
        print("[ERR] Deploy failed: " + str(e))
        raise
    finally:
        sftp.close()
        transport.close()

def run_command(cmd):
    """Run a command on A7Z via SSH."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(A7Z_CONFIG['host'], username=A7Z_CONFIG['username'],
                   password=A7Z_CONFIG['password'])
    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    client.close()
    if out:
        print("  " + out.replace('\n', '\n  '))
    if err:
        print("  [err] " + err.replace('\n', '\n  '))
    return out, err

if __name__ == '__main__':
    print("=== Deploy to A7Z (" + A7Z_CONFIG['host'] + ") ===")
    print("Remote path: " + A7Z_CONFIG['remote_path'])
    print()

    # Deploy app.py
    print("[1/2] Deploying app.py ...")
    deploy_file('app.py', A7Z_CONFIG['remote_path'] + 'app.py')

    # Deploy media_service.py
    print("[2/2] Deploying media_service.py ...")
    deploy_file('media_service.py', A7Z_CONFIG['remote_path'] + 'media_service.py')

    print()
    print("=== Restarting teslausb-web ===")
    run_command('sudo systemctl restart teslausb-web')
    run_command('sudo systemctl status teslausb-web --no-pager -l')

    print()
    print("=== Verify music directory on A7Z ===")
    run_command('ls -la /mnt/music/ 2>/dev/null || echo "NOT MOUNTED: /mnt/music"')
    run_command('ls -la /mnt/music/Music/ 2>/dev/null || echo "NOT FOUND: /mnt/music/Music"')

    print()
    print("=== Test music API endpoint ===")
    run_command('curl -s http://localhost:5000/api/media/music/list 2>/dev/null | head -c 800')
