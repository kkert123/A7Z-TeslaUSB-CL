#!/usr/bin/env python3
"""
A7Z Boombox 服务模块 (移植自 TeslaUSB-main)
=============================================
管理 Tesla 自定义 Boombox 音频文件，存放于 /mnt/boombox/Boombox/

Tesla 规格要求:
- 文件夹: ``Boombox``
- 格式: MP3 / WAV
- 最大文件大小: 1 MB
- 最大文件数: 5
- 文件名: 最多 64 字符
"""

import os
import logging

logger = logging.getLogger(__name__)

BOOMBOX_BASE = "/mnt/boombox"
BOOMBOX_FOLDER = "Boombox"
BOOMBOX_DIR = os.path.join(BOOMBOX_BASE, BOOMBOX_FOLDER)

MAX_FILE_SIZE = 1 * 1024 * 1024   # 1 MB
MAX_FILE_COUNT = 5
MAX_FILENAME_LENGTH = 64
ALLOWED_EXTS = {".mp3", ".wav"}


def list_boombox_files() -> list:
    """列出所有 Boombox 音频文件"""
    if not os.path.isdir(BOOMBOX_DIR):
        return []
    files = []
    for fname in sorted(os.listdir(BOOMBOX_DIR)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_EXTS:
            continue
        fpath = os.path.join(BOOMBOX_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            size = os.path.getsize(fpath)
        except OSError:
            continue
        files.append({'filename': fname, 'size': size, 'ext': ext})
    return files


def upload_boombox(file_obj, original_filename: str) -> tuple:
    """上传 Boombox 音频。返回 (success, message)"""
    name = original_filename.strip()
    if not name:
        return False, "文件名为空"
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        return False, "只允许 MP3 或 WAV 格式"
    if len(os.path.splitext(name)[0]) > MAX_FILENAME_LENGTH:
        return False, f"文件名最多 {MAX_FILENAME_LENGTH} 个字符"

    existing = list_boombox_files()
    if len(existing) >= MAX_FILE_COUNT:
        return False, f"已达到最大数量 ({MAX_FILE_COUNT})，请先删除旧文件"

    os.makedirs(BOOMBOX_DIR, exist_ok=True)
    safe_name = os.path.basename(name)
    target = os.path.join(BOOMBOX_DIR, safe_name)
    try:
        file_obj.save(target)
    except Exception as e:
        return False, f"保存失败: {e}"

    # 检查大小
    try:
        if os.path.getsize(target) > MAX_FILE_SIZE:
            os.remove(target)
            return False, f"文件过大 (超过 1 MB)"
    except OSError:
        pass

    logger.info(f"Boombox 文件已保存: {safe_name}")
    return True, f"音频 {safe_name} 已上传"


def delete_boombox(filename: str) -> tuple:
    """删除 Boombox 音频。返回 (success, message)"""
    safe_name = os.path.basename(filename)
    target = os.path.join(BOOMBOX_DIR, safe_name)
    if not os.path.isfile(target):
        return False, "文件不存在"
    try:
        os.remove(target)
        logger.info(f"Boombox 文件已删除: {safe_name}")
        return True, f"音频 {safe_name} 已删除"
    except OSError as e:
        return False, f"删除失败: {e}"


def get_available() -> bool:
    return os.path.isdir(BOOMBOX_BASE)
