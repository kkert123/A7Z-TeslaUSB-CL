#!/usr/bin/env python3
"""
A7Z License Plate 服务模块 (移植自 TeslaUSB-main)
=================================================
管理自定义 Tesla 车牌背景 PNG 图像，存放于 /mnt/lightshow/LicensePlate/

Tesla 规格要求:
- 文件夹: ``LicensePlate`` (区分大小写，无末尾 s)
- 格式: 仅 PNG
- 尺寸 (北美): 420 x 200 px
- 尺寸 (欧盟): 420 x 100 px
- 最大文件大小: 512 KB
- 文件名: 仅字母数字，<= 32 字符
- 最大文件数: 10

A7Z 简化:
- 始终读写挂载 (无 present/edit 模式)
- 无 Samba 刷新
- 无 USB gadget rebind
"""

import os
import re
import struct
import logging

logger = logging.getLogger(__name__)

# ── 路径常量 ──
PLATE_BASE_PATH = "/mnt/lightshow"
LICENSE_PLATE_FOLDER = "LicensePlate"
PLATE_DIR = os.path.join(PLATE_BASE_PATH, LICENSE_PLATE_FOLDER)

# ── Tesla 规格常量 ──
MAX_PLATE_SIZE = 512 * 1024          # 512 KB
MAX_PLATE_COUNT = 10
MAX_FILENAME_LENGTH = 32

PLATE_DIMENSIONS_NA = (420, 200)
PLATE_DIMENSIONS_EU = (420, 100)
ALLOWED_PLATE_DIMENSIONS = (PLATE_DIMENSIONS_NA, PLATE_DIMENSIONS_EU)

VALID_FILENAME_PATTERN = re.compile(r'^[A-Za-z0-9]+$')


def get_png_dimensions(file_path: str):
    """返回 PNG 文件的 (宽度, 高度)，或 (None, None)。"""
    try:
        with open(file_path, 'rb') as f:
            signature = f.read(8)
            if signature != b'\x89PNG\r\n\x1a\n':
                return None, None
            f.read(4)  # length
            chunk_type = f.read(4)
            if chunk_type != b'IHDR':
                return None, None
            width = struct.unpack('>I', f.read(4))[0]
            height = struct.unpack('>I', f.read(4))[0]
            return width, height
    except Exception as e:
        logger.error(f"读取 PNG 尺寸失败: {e}")
        return None, None


def validate_filename(filename: str) -> tuple:
    """验证文件名是否符合 Tesla 车牌规范。
    返回 (is_valid, error_message)
    """
    name_no_ext = os.path.splitext(filename)[0]
    if not filename.lower().endswith('.png'):
        return False, "只允许 PNG 格式"
    if not VALID_FILENAME_PATTERN.match(name_no_ext):
        return False, "文件名只能包含字母和数字（无空格/下划线/破折号）"
    if len(name_no_ext) > MAX_FILENAME_LENGTH:
        return False, f"文件名最多 {MAX_FILENAME_LENGTH} 个字符"
    return True, ""


def validate_plate_file(file_path: str) -> tuple:
    """验证车牌 PNG 文件。
    返回 (is_valid, error_message, dimensions_tuple)
    """
    if not os.path.isfile(file_path):
        return False, "文件不存在", None

    size = os.path.getsize(file_path)
    if size > MAX_PLATE_SIZE:
        return False, f"文件过大 ({_fmt_size(size)} > {_fmt_size(MAX_PLATE_SIZE)})", None

    width, height = get_png_dimensions(file_path)
    if width is None or height is None:
        return False, "无法读取 PNG 尺寸，可能不是有效的 PNG 文件", None

    if (width, height) not in ALLOWED_PLATE_DIMENSIONS:
        return False, f"尺寸 {width}x{height} 不符合规范 (允许 420x200 或 420x100)", (width, height)

    return True, "OK", (width, height)


def list_plate_files() -> list:
    """列出所有车牌文件。返回 [{filename, size, width, height}, ...]"""
    if not os.path.isdir(PLATE_DIR):
        return []
    plates = []
    for fname in sorted(os.listdir(PLATE_DIR)):
        if not fname.lower().endswith('.png'):
            continue
        fpath = os.path.join(PLATE_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            size = os.path.getsize(fpath)
            width, height = get_png_dimensions(fpath)
        except OSError:
            continue
        plates.append({
            'filename': fname,
            'size': size,
            'width': width,
            'height': height,
        })
    return plates


def upload_plate(file_obj, original_filename: str) -> tuple:
    """上传车牌文件。返回 (success, message, dimensions_tuple)"""
    # 验证文件名
    is_valid, err_msg = validate_filename(original_filename)
    if not is_valid:
        return False, err_msg, None

    # 检查数量限制
    existing = list_plate_files()
    if len(existing) >= MAX_PLATE_COUNT:
        return False, f"已达到最大数量 ({MAX_PLATE_COUNT})，请先删除旧文件", None

    # 确保目录存在
    os.makedirs(PLATE_DIR, exist_ok=True)

    # 保存文件
    safe_name = os.path.basename(original_filename)
    target_path = os.path.join(PLATE_DIR, safe_name)

    try:
        file_obj.save(target_path)
    except Exception as e:
        logger.error(f"保存车牌文件失败: {e}")
        return False, f"保存文件失败: {e}", None

    # 验证保存后的文件
    is_valid, err_msg, dims = validate_plate_file(target_path)
    if not is_valid:
        try:
            os.remove(target_path)
        except OSError:
            pass
        return False, err_msg, dims

    logger.info(f"车牌文件已保存: {safe_name} ({dims})")
    return True, f"车牌 {safe_name} 已上传", dims


def delete_plate(filename: str) -> tuple:
    """删除车牌文件。返回 (success, message)"""
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith('.png'):
        return False, "只允许删除 PNG 文件"

    target_path = os.path.join(PLATE_DIR, safe_name)
    if not os.path.isfile(target_path):
        return False, "文件不存在"

    try:
        os.remove(target_path)
        logger.info(f"车牌文件已删除: {safe_name}")
        return True, f"车牌 {safe_name} 已删除"
    except OSError as e:
        logger.error(f"删除车牌文件失败: {e}")
        return False, f"删除失败: {e}"


def get_available() -> bool:
    """检查 LicensePlate 分区是否可用"""
    return os.path.isdir(PLATE_BASE_PATH)


def _fmt_size(size: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
