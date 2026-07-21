#!/usr/bin/env python3
"""Deploy fixed files to A7Z - v12 (ASCII only, no encoding issues)"""

import sys
import os

# A7Z connection
HOST = '100.116.18.42'
PORT = 22
USER = 'radxa'
PASS = 'radxa'

# Files to deploy: (local_path, remote_path)
FILES = [
    (r'D:\teslausb\a7z\app.py', '/opt/radxa_data/teslausb/app.py'),
    (r'D:\teslausb\a7z\media_service.py', '/opt/radxa_data/teslausb/media_service.py'),
    (r'D:\teslausb\a7z\usb_gadget_init.sh', '/opt/radxa_data/usb_gadget_init.sh'),
]

def main():
    print("[1/2] Connecting to A7Z...")
    
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15)
    print(f"[1/2] Connected: {HOST}")
    
    sftp = ssh.open_sftp()
    
    print("\n[2/2] Deploying files...")
    for local, remote in FILES:
        fname = os.path.basename(local)
        print(f"  Uploading {fname} -> {remote}...")
        try:
            sftp.put(local, remote)
            print(f"    OK")
        except Exception as e:
            print(f"    FAILED: {e}")
    
    sftp.close()
    
    print("\n  Restarting teslausb-web...")
    stdin, stdout, stderr = ssh.exec_command('sudo -n systemctl restart teslausb-web', timeout=10)
    err = stderr.read().decode('utf-8', errors='ignore').strip()
    if err:
        print(f"    Warning: {err}")
    else:
        print(f"    OK")
    
    ssh.close()
    print("\nDeploy complete!")
    print("\nVerify:")
    print("  1. http://100.116.18.42:5000/media - music files visible?")
    print("  2. Web UI mode switch works?")
    print("  3. Present mode: /mnt/music mounted read-only?")

if __name__ == '__main__':
    main()
