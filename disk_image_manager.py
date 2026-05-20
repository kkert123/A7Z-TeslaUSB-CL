#!/usr/bin/env python3
"""
disk_image_manager.py - 磁盘镜像管理工具
用于在 Present Mode 下安全地更新 .img 文件

工作流程：
1. 停止 USB Gadget（Tesla 断开连接）
2. 挂载 .img 文件
3. 复制文件到镜像
4. 卸载 .img 文件
5. 重启 USB Gadget（Tesla 重新连接）

作者: Senior Developer
日期: 2026-05-14
"""

import os
import sys
import time
import subprocess
import logging
from pathlib import Path
from typing import Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[disk_image_manager] %(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════
# 配置部分
# ═════════════════════════════════════════════════════════════════

IMAGE_DIR = "/data/teslausb_images"
MUSIC_IMG = os.path.join(IMAGE_DIR, "music.img")
LIGHTSHOW_IMG = os.path.join(IMAGE_DIR, "lightshow.img")
BOOMBOX_IMG = os.path.join(IMAGE_DIR, "boombox.img")

# 挂载点（临时）
MOUNT_BASE = "/tmp/teslausb_mount"
MUSIC_MOUNT = os.path.join(MOUNT_BASE, "music")
LIGHTSHOW_MOUNT = os.path.join(MOUNT_BASE, "lightshow")
BOOMBOX_MOUNT = os.path.join(MOUNT_BASE, "boombox")

# USB Gadget 配置
GADGET_DIR = "/sys/kernel/config/usb_gadget/teslausb"
UDC_FILE = os.path.join(GADGET_DIR, "UDC")
UDC_VALUE = "6a00000.xhci2-controller"

# ═════════════════════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════════════════════

def run_command(cmd, check: bool = True, use_shell: bool = False) -> subprocess.CompletedProcess:
    """
    运行命令（默认 shell=False，防注入）
    
    Args:
        cmd: 命令（推荐 list 格式，或字符串 + use_shell=False）
        check: 是否检查返回码
        use_shell: 显式启用 shell=True（仅限无用户输入的内部常量）
    
    Returns:
        subprocess.CompletedProcess 对象
    """
    if isinstance(cmd, list):
        use_shell = False
        logger.debug(f"执行命令: {' '.join(cmd)}")
    else:
        logger.debug(f"执行命令: {cmd}")
    try:
        result = subprocess.run(
            cmd,
            shell=use_shell,
            capture_output=True,
            text=True,
            check=check
        )
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"命令执行失败: {cmd}")
        logger.error(f"错误输出: {e.stderr}")
        if check:
            raise
        return e

def check_root() -> bool:
    """
    检查是否以 root 权限运行
    
    Returns:
        bool: 是否 root
    """
    return os.geteuid() == 0

def check_image_exists(image_path: str) -> bool:
    """
    检查镜像文件是否存在
    
    Args:
        image_path: 镜像文件路径
    
    Returns:
        bool: 是否存在
    """
    return os.path.isfile(image_path)

# ═════════════════════════════════════════════════════════════════
# USB Gadget 控制函数
# ═════════════════════════════════════════════════════════════════

def stop_usb_gadget() -> bool:
    """
    停止 USB Gadget（Tesla 会断开连接）
    
    Returns:
        bool: 是否成功
    """
    logger.info("🔴 停止 USB Gadget（Tesla 将断开连接）...")
    
    try:
        # 检查 Gadget 是否存在
        if not os.path.isdir(GADGET_DIR):
            logger.warning("   ⚠️  USB Gadget 未启动，跳过")
            return True
        
        # 解绑 UDC
        if os.path.isfile(UDC_FILE):
            logger.info("   解绑 UDC...")
            try:
                with open(UDC_FILE, 'w') as f:
                    f.write('\n')
            except Exception as e:
                logger.warning(f"   写入 UDC 失败: {e}")
            time.sleep(2)  # 等待 Tesla 断开
        
        logger.info("   ✅ USB Gadget 已停止")
        return True
    
    except Exception as e:
        logger.error(f"   ❌ 停止 USB Gadget 失败: {e}")
        return False

def start_usb_gadget() -> bool:
    """
    启动 USB Gadget（Tesla 会重新连接）
    
    Returns:
        bool: 是否成功
    """
    logger.info("🟢 启动 USB Gadget（Tesla 将重新连接）...")
    
    try:
        # 检查 Gadget 是否存在
        if not os.path.isdir(GADGET_DIR):
            logger.error("   ❌ USB Gadget 配置不存在，请先运行 usb_gadget_init_disk_image.sh")
            return False
        
        # 绑定 UDC
        if os.path.isfile(UDC_FILE):
            logger.info("   绑定 UDC...")
            try:
                with open(UDC_FILE, 'w') as f:
                    f.write(UDC_VALUE)
            except Exception as e:
                logger.warning(f"   写入 UDC 失败: {e}")
            time.sleep(2)  # 等待 Tesla 连接
        
        logger.info("   ✅ USB Gadget 已启动")
        return True
    
    except Exception as e:
        logger.error(f"   ❌ 启动 USB Gadget 失败: {e}")
        return False

# ═════════════════════════════════════════════════════════════════
# 镜像挂载/卸载函数
# ═════════════════════════════════════════════════════════════════

def mount_image(image_path: str, mount_point: str) -> bool:
    """
    挂载镜像文件
    
    Args:
        image_path: 镜像文件路径
        mount_point: 挂载点
    
    Returns:
        bool: 是否成功
    """
    logger.info(f"📂 挂载镜像: {image_path}")
    
    try:
        # 创建挂载点
        os.makedirs(mount_point, exist_ok=True)
        
        # 挂载
        result = run_command(
            ['mount', '-o', 'loop,uid=1000,gid=1000', image_path, mount_point],
            check=False
        )
        
        if result.returncode == 0:
            logger.info(f"   ✅ 镜像已挂载到: {mount_point}")
            return True
        else:
            logger.error(f"   ❌ 挂载失败: {result.stderr}")
            return False
    
    except Exception as e:
        logger.error(f"   ❌ 挂载异常: {e}")
        return False

def unmount_image(mount_point: str) -> bool:
    """
    卸载镜像
    
    Args:
        mount_point: 挂载点
    
    Returns:
        bool: 是否成功
    """
    logger.info(f"📁 卸载镜像: {mount_point}")
    
    try:
        # 检查是否挂载
        result = run_command(['mountpoint', '-q', mount_point], check=False)
        
        if result.returncode == 0:
            # 卸载
            run_command(['umount', mount_point], check=False)
            logger.info(f"   ✅ 镜像已卸载")
            return True
        else:
            logger.warning(f"   ⚠️  镜像未挂载，跳过")
            return True
    
    except Exception as e:
        logger.error(f"   ❌ 卸载异常: {e}")
        return False

# ═════════════════════════════════════════════════════════════════
# 文件操作函数
# ═════════════════════════════════════════════════════════════════

def copy_file_to_image(
    source_file: str,
    image_type: str,
    destination_path: Optional[str] = None
) -> bool:
    """
    复制文件到镜像
    
    Args:
        source_file: 源文件路径
        image_type: 镜像类型（music/lightshow/boombox）
        destination_path: 目标路径（相对于镜像根目录）
    
    Returns:
        bool: 是否成功
    """
    logger.info(f"📝 复制文件到 {image_type} 镜像...")
    logger.info(f"   源文件: {source_file}")
    
    # 确定镜像和挂载点
    if image_type == "music":
        image_path = MUSIC_IMG
        mount_point = MUSIC_MOUNT
        default_dest_dir = os.path.join(mount_point, "Music")
    elif image_type == "lightshow":
        image_path = LIGHTSHOW_IMG
        mount_point = LIGHTSHOW_MOUNT
        default_dest_dir = os.path.join(mount_point, "LightShow")
    elif image_type == "boombox":
        image_path = BOOMBOX_IMG
        mount_point = BOOMBOX_MOUNT
        default_dest_dir = os.path.join(mount_point, "Boombox")
    else:
        logger.error(f"   ❌ 未知的镜像类型: {image_type}")
        return False
    
    # 检查源文件
    if not os.path.isfile(source_file):
        logger.error(f"   ❌ 源文件不存在: {source_file}")
        return False
    
    # 检查镜像
    if not check_image_exists(image_path):
        logger.error(f"   ❌ 镜像不存在: {image_path}")
        return False
    
    # 确定目标目录
    if destination_path:
        dest_dir = os.path.join(mount_point, destination_path)
    else:
        dest_dir = default_dest_dir
    
    try:
        # 创建目标目录
        os.makedirs(dest_dir, exist_ok=True)
        
        # 复制文件
        filename = os.path.basename(source_file)
        dest_file = os.path.join(dest_dir, filename)
        
        logger.info(f"   目标位置: {dest_file}")
        
        import shutil
        shutil.copy2(source_file, dest_file)
        
        logger.info(f"   ✅ 文件已复制: {filename}")
        return True
    
    except Exception as e:
        logger.error(f"   ❌ 复制文件失败: {e}")
        return False

# ═════════════════════════════════════════════════════════════════
# 主流程函数
# ═════════════════════════════════════════════════════════════════

def update_image(
    source_file: str,
    image_type: str,
    destination_path: Optional[str] = None
) -> bool:
    """
    完整的镜像更新流程
    
    Args:
        source_file: 源文件路径
        image_type: 镜像类型（music/lightshow/boombox）
        destination_path: 目标路径（可选）
    
    Returns:
        bool: 是否成功
    """
    logger.info("=" * 60)
    logger.info("开始镜像更新流程")
    logger.info("=" * 60)
    
    # 确定镜像和挂载点
    if image_type == "music":
        image_path = MUSIC_IMG
        mount_point = MUSIC_MOUNT
    elif image_type == "lightshow":
        image_path = LIGHTSHOW_IMG
        mount_point = LIGHTSHOW_MOUNT
    elif image_type == "boombox":
        image_path = BOOMBOX_IMG
        mount_point = BOOMBOX_MOUNT
    else:
        logger.error(f"❌ 未知的镜像类型: {image_type}")
        return False
    
    # 步骤 1: 停止 USB Gadget
    logger.info("\n步骤 1/4: 停止 USB Gadget")
    if not stop_usb_gadget():
        logger.error("❌ 停止 USB Gadget 失败")
        return False
    
    # 步骤 2: 挂载镜像
    logger.info("\n步骤 2/4: 挂载镜像")
    if not mount_image(image_path, mount_point):
        logger.error("❌ 挂载镜像失败")
        start_usb_gadget()  # 尝试恢复
        return False
    
    # 步骤 3: 复制文件
    logger.info("\n步骤 3/4: 复制文件")
    if not copy_file_to_image(source_file, image_type, destination_path):
        logger.error("❌ 复制文件失败")
        unmount_image(mount_point)
        start_usb_gadget()  # 尝试恢复
        return False
    
    # 步骤 4: 卸载镜像并重启 USB Gadget
    logger.info("\n步骤 4/4: 卸载镜像并重启 USB Gadget")
    if not unmount_image(mount_point):
        logger.warning("⚠️  卸载镜像失败，但继续...")
    
    if not start_usb_gadget():
        logger.error("❌ 重启 USB Gadget 失败")
        return False
    
    logger.info("=" * 60)
    logger.info("✅ 镜像更新完成！")
    logger.info("=" * 60)
    
    return True

# ═════════════════════════════════════════════════════════════════
# 命令行接口
# ═════════════════════════════════════════════════════════════════

def print_usage():
    """打印使用说明"""
    print("""
磁盘镜像管理工具 - 使用说明

用法:
    sudo python3 disk_image_manager.py <source_file> <image_type> [destination_path]

参数:
    source_file:      源文件路径（要上传的文件）
    image_type:       镜像类型（music/lightshow/boombox）
    destination_path: 目标路径（可选，相对于镜像根目录）

示例:
    # 上传音乐文件
    sudo python3 disk_image_manager.py /tmp/song.mp3 music Music/

    # 上传灯光秀文件
    sudo python3 disk_image_manager.py /tmp/lightshow.zip lightshow LightShow/

    # 上传车贴文件
    sudo python3 disk_image_manager.py /tmp/wrap.png boombox Boombox/

注意:
    1. 必须以 root 权限运行（sudo）
    2. 运行时会短暂断开 Tesla 连接（约 5 秒）
    3. 确保 USB Gadget 已启动
""")

if __name__ == "__main__":
    # 检查 root 权限
    if not check_root():
        print("❌ 错误: 必须以 root 权限运行（使用 sudo）")
        sys.exit(1)
    
    # 检查参数
    if len(sys.argv) < 3:
        print_usage()
        sys.exit(1)
    
    source_file = sys.argv[1]
    image_type = sys.argv[2].lower()
    destination_path = sys.argv[3] if len(sys.argv) > 3 else None
    
    # 验证 image_type
    if image_type not in ["music", "lightshow", "boombox"]:
        print(f"❌ 错误: 无效的镜像类型: {image_type}")
        print("   有效值: music, lightshow, boombox")
        sys.exit(1)
    
    # 执行更新
    success = update_image(source_file, image_type, destination_path)
    
    if success:
        print("\n✅ 成功！文件已上传，Tesla 可以读取新文件。")
        sys.exit(0)
    else:
        print("\n❌ 失败！请检查日志。")
        sys.exit(1)
