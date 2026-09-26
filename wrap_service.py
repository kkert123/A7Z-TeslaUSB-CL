#!/usr/bin/env python3
"""
A7Z Custom Wrap 服务模块 (移植自 TeslaUSB-main)
================================================
管理 Tesla 自定义贴膜背景 PNG，存放于 /mnt/lightshow/Wraps/

Tesla 规格要求:
- 文件夹: ``Wraps``
- 格式: 仅 PNG
- 尺寸: 512×512 ~ 1024×1024 px
- 最大文件大小: 1 MB
- 文件名: 字母数字/下划线/破折号/空格，<= 30 字符
- 最大文件数: 无限制
"""

import os
import re
import struct
import logging

logger = logging.getLogger(__name__)

# ── 路径常量 ──
WRAP_BASE_PATH = "/mnt/lightshow"
WRAPS_FOLDER = "Wraps"
WRAP_DIR = os.path.join(WRAP_BASE_PATH, WRAPS_FOLDER)

# ── Tesla 规格常量 ──
MAX_WRAP_SIZE = 1 * 1024 * 1024     # 1 MB
MIN_DIMENSION = 512
MAX_DIMENSION = 1024
MAX_FILENAME_LENGTH = 30

VALID_FILENAME_PATTERN = re.compile(r'^[a-zA-Z0-9_\- ]+$')


def get_png_dimensions(file_path: str):
    """返回 PNG 文件的 (宽度, 高度)，或 (None, None)。"""
    try:
        with open(file_path, 'rb') as f:
            if f.read(8) != b'\x89PNG\r\n\x1a\n':
                return None, None
            f.read(4)  # chunk length
            if f.read(4) != b'IHDR':
                return None, None
            width = struct.unpack('>I', f.read(4))[0]
            height = struct.unpack('>I', f.read(4))[0]
            return width, height
    except Exception as e:
        logger.error(f"读取 PNG 尺寸失败: {e}")
        return None, None


def validate_filename(filename: str) -> tuple:
    """验证文件名。返回 (is_valid, error_message)"""
    name_no_ext = os.path.splitext(filename)[0]
    if not filename.lower().endswith('.png'):
        return False, "只允许 PNG 格式"
    if not VALID_FILENAME_PATTERN.match(name_no_ext):
        return False, "文件名只能包含字母数字、下划线、破折号和空格"
    if len(name_no_ext) > MAX_FILENAME_LENGTH:
        return False, f"文件名最多 {MAX_FILENAME_LENGTH} 个字符"
    return True, ""


def validate_wrap_file(file_path: str) -> tuple:
    """验证贴膜 PNG 文件。返回 (is_valid, error_message, dimensions_tuple)"""
    if not os.path.isfile(file_path):
        return False, "文件不存在", None
    size = os.path.getsize(file_path)
    if size > MAX_WRAP_SIZE:
        return False, f"文件过大 ({_fmt_size(size)} > {_fmt_size(MAX_WRAP_SIZE)})", None
    width, height = get_png_dimensions(file_path)
    if width is None or height is None:
        return False, "无法读取 PNG 尺寸", None
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        return False, f"图片太小 ({width}x{height}，最小 {MIN_DIMENSION}x{MIN_DIMENSION})", (width, height)
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        return False, f"图片太大 ({width}x{height}，最大 {MAX_DIMENSION}x{MAX_DIMENSION})", (width, height)
    return True, "OK", (width, height)


def list_wrap_files() -> list:
    """列出所有贴膜文件。"""
    if not os.path.isdir(WRAP_DIR):
        return []
    wraps = []
    for fname in sorted(os.listdir(WRAP_DIR)):
        if not fname.lower().endswith('.png'):
            continue
        fpath = os.path.join(WRAP_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            size = os.path.getsize(fpath)
            width, height = get_png_dimensions(fpath)
        except OSError:
            continue
        wraps.append({'filename': fname, 'size': size, 'width': width, 'height': height})
    return wraps


def upload_wrap(file_obj, original_filename: str) -> tuple:
    """上传贴膜文件。返回 (success, message, dimensions_tuple)"""
    is_valid, err = validate_filename(original_filename)
    if not is_valid:
        return False, err, None
    os.makedirs(WRAP_DIR, exist_ok=True)
    safe_name = os.path.basename(original_filename)
    target_path = os.path.join(WRAP_DIR, safe_name)
    try:
        file_obj.save(target_path)
    except Exception as e:
        return False, f"保存失败: {e}", None
    is_valid, err, dims = validate_wrap_file(target_path)
    if not is_valid:
        try:
            os.remove(target_path)
        except OSError:
            pass
        return False, err, dims
    logger.info(f"贴膜文件已保存: {safe_name} ({dims})")
    return True, f"贴膜 {safe_name} 已上传", dims


def delete_wrap(filename: str) -> tuple:
    """删除贴膜文件。返回 (success, message)"""
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith('.png'):
        return False, "只允许删除 PNG 文件"
    target_path = os.path.join(WRAP_DIR, safe_name)
    if not os.path.isfile(target_path):
        return False, "文件不存在"
    try:
        os.remove(target_path)
        logger.info(f"贴膜文件已删除: {safe_name}")
        return True, f"贴膜 {safe_name} 已删除"
    except OSError as e:
        return False, f"删除失败: {e}"


def get_available() -> bool:
    return os.path.isdir(WRAP_BASE_PATH)


def _fmt_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
