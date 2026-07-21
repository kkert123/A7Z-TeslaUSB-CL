#!/usr/bin/env python3
"""
Upload clean TeslaUSB-Web files to A7Z via SFTP
Usage: python3 upload_to_a7z.py
"""

import os
import sys
import paramiko

# A7Z connection info
A7Z_IP = "100.64.0.10"
USERNAME = "radxa"
PASSWORD = os.environ.get("A7Z_PASSWORD", "CHANGE_ME_SSH_PASSWORD")
PORT = 22

# Local paths
SOURCE_DIR = r"D:\teslausb\a2\20260418161423\teslausb-web"
CLEAN_DIR = r"D:\teslausb\a7z\clean_deploy\teslausb-web"
MODIFIED_BASE = r"D:\teslausb\a7z\modified_base.html"
ADD_CSS = r"D:\teslausb\a7z\add_to_style.css"
ADD_APP = r"D:\teslausb\a7z\add_to_app.py"
DEPLOY_SCRIPT = r"D:\teslausb\a7z\full_deploy_fixed.sh"

# Core files to upload
CORE_PY_FILES = [
    "app.py", "config.py", "auth.py", "config_manager.py",
    "sentry_service.py", "sentry_watchdog.py", "video_preview.py",
    "preview_generator.py", "weixin_notifier.py", "upload_scheduler.py",
    "location_detector.py", "wifi_service.py", "system_monitor.py",
    "hardware_watchdog.py", "boot_notify.py", "auto_cleanup.py",
    "fsck_check.py", "media_service.py"
]

TEMPLATE_FILES = [
    "base.html", "dashboard.html", "sentry.html", "videos.html",
    "upload_progress.html", "wifi.html", "media.html", "logs.html",
    "analytics.html", "system.html", "boombox.html", "lightshow.html",
    "wraps.html"
]

STATIC_FILES = ["style.css", "app.js"]

def create_clean_dir():
    """Create clean directory structure"""
    print("\n[1/4] Creating clean directory structure...")
    os.makedirs(f"{CLEAN_DIR}/templates", exist_ok=True)
    os.makedirs(f"{CLEAN_DIR}/static", exist_ok=True)
    os.makedirs(f"{CLEAN_DIR}/config", exist_ok=True)
    print(f"  OK: {CLEAN_DIR}")
    return True

def copy_clean_files():
    """Copy only necessary files to clean directory"""
    print("\n[2/4] Copying core files...")
    
    # Copy Python files
    for f in CORE_PY_FILES:
        src = os.path.join(SOURCE_DIR, f)
        if os.path.exists(src):
            import shutil
            shutil.copy(src, CLEAN_DIR)
            print(f"  OK: {f}")
        else:
            print(f"  SKIP: {f} (not found)")
    
    # Copy template files
    for f in TEMPLATE_FILES:
        src = os.path.join(SOURCE_DIR, "templates", f)
        if os.path.exists(src):
            import shutil
            shutil.copy(src, os.path.join(CLEAN_DIR, "templates"))
            print(f"  OK: {f}")
        else:
            print(f"  SKIP: {f} (not found)")
    
    # Copy static files
    for f in STATIC_FILES:
        src = os.path.join(SOURCE_DIR, "static", f)
        if os.path.exists(src):
            import shutil
            shutil.copy(src, os.path.join(CLEAN_DIR, "static"))
            print(f"  OK: {f}")
        else:
            print(f"  SKIP: {f} (not found)")
    
    # Copy config
    config_src = os.path.join(SOURCE_DIR, "config", "sentry.json")
    if os.path.exists(config_src):
        import shutil
        shutil.copy(config_src, os.path.join(CLEAN_DIR, "config"))
        print(f"  OK: sentry.json")
    
    # Copy service file
    service_src = os.path.join(SOURCE_DIR, "teslausb-web.service")
    if os.path.exists(service_src):
        import shutil
        shutil.copy(service_src, CLEAN_DIR)
        print(f"  OK: teslausb-web.service")
    
    return True

def apply_task14():
    """Apply Task #14 modifications"""
    print("\n[3/4] Applying Task #14 modifications...")
    
    # Replace base.html
    if os.path.exists(MODIFIED_BASE):
        import shutil
        shutil.copy(MODIFIED_BASE, os.path.join(CLEAN_DIR, "templates", "base.html"))
        print("  OK: base.html replaced with modified version")
    else:
        print("  SKIP: modified_base.html not found")
    
    # Append CSS
    if os.path.exists(ADD_CSS):
        with open(ADD_CSS, 'r', encoding='utf-8') as f:
            css_content = f.read()
        style_css = os.path.join(CLEAN_DIR, "static", "style.css")
        with open(style_css, 'a', encoding='utf-8') as f:
            f.write("\n\n" + css_content)
        print("  OK: CSS styles appended to style.css")
    else:
        print("  SKIP: add_to_style.css not found")
    
    # Copy add_to_app.py
    if os.path.exists(ADD_APP):
        import shutil
        shutil.copy(ADD_APP, CLEAN_DIR)
        print("  OK: add_to_app.py copied (apply to app.py later)")
    else:
        print("  SKIP: add_to_app.py not found")
    
    return True

def upload_via_sftp():
    """Upload all files to A7Z via SFTP"""
    print("\n[4/4] Uploading to A7Z via SFTP...")
    print(f"  Host: {A7Z_IP}")
    print(f"  User: {USERNAME}")
    print("")
    
    try:
        # Connect to A7Z
        print("  Connecting to A7Z...")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(A7Z_IP, port=PORT, username=USERNAME, password=PASSWORD, timeout=10)
        print("  OK: SSH connected")
        
        # Create SFTP client
        sftp = ssh.open_sftp()
        print("  OK: SFTP channel opened")
        
        # Create remote directory
        remote_base = "/tmp/teslausb-web"
        try:
            sftp.mkdir(remote_base)
        except:
            pass
        
        for subdir in ["templates", "static", "config"]:
            try:
                sftp.mkdir(f"{remote_base}/{subdir}")
            except:
                pass
        
        # Upload all files from clean directory
        print("")
        print("  Uploading files...")
        
        # Upload Python files
        for f in os.listdir(CLEAN_DIR):
            if f.endswith('.py') or f.endswith('.service'):
                local_path = os.path.join(CLEAN_DIR, f)
                remote_path = f"{remote_base}/{f}"
                sftp.put(local_path, remote_path)
                print(f"    OK: {f}")
        
        # Upload templates
        template_dir = os.path.join(CLEAN_DIR, "templates")
        for f in os.listdir(template_dir):
            local_path = os.path.join(template_dir, f)
            remote_path = f"{remote_base}/templates/{f}"
            sftp.put(local_path, remote_path)
            print(f"    OK: templates/{f}")
        
        # Upload static
        static_dir = os.path.join(CLEAN_DIR, "static")
        for f in os.listdir(static_dir):
            local_path = os.path.join(static_dir, f)
            remote_path = f"{remote_base}/static/{f}"
            sftp.put(local_path, remote_path)
            print(f"    OK: static/{f}")
        
        # Upload config
        config_dir = os.path.join(CLEAN_DIR, "config")
        for f in os.listdir(config_dir):
            local_path = os.path.join(config_dir, f)
            remote_path = f"{remote_base}/config/{f}"
            sftp.put(local_path, remote_path)
            print(f"    OK: config/{f}")
        
        # Upload deploy script
        if os.path.exists(DEPLOY_SCRIPT):
            sftp.put(DEPLOY_SCRIPT, "/tmp/full_deploy_fixed.sh")
            print(f"    OK: full_deploy_fixed.sh")
        
        sftp.close()
        ssh.close()
        
        print("")
        print("=" * 50)
        print("UPLOAD SUCCESS!")
        print("=" * 50)
        print("")
        print("Next steps:")
        print("  1. SSH to A7Z: ssh <user>@<your-a7z-host>")
        print("  2. Run deploy script: sudo bash /tmp/full_deploy_fixed.sh")
        print("")
        
        return True
        
    except Exception as e:
        print(f"UPLOAD FAILED: {str(e)}")
        print("")
        print("Please check:")
        print("  1. Network connection to A7Z")
        print("  2. SSH service is running on A7Z")
        print("  3. Credentials are correct (set via A7Z_USER / A7Z_PASSWORD env)")
        return False

def main():
    print("=" * 50)
    print("TeslaUSB-CL Clean Deploy to A7Z")
    print("=" * 50)
    
    # Step 1: Create clean directory
    if not create_clean_dir():
        return 1
    
    # Step 2: Copy clean files
    if not copy_clean_files():
        return 1
    
    # Step 3: Apply Task #14
    if not apply_task14():
        return 1
    
    # Step 4: Upload via SFTP
    if not upload_via_sftp():
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
