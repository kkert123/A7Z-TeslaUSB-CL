#!/usr/bin/env python3
"""
A7Z LightShow 服务模块 (移植自 TeslaUSB-main)
===============================================
管理 Tesla 自定义灯光秀文件，存放于 /mnt/lightshow/LightShow/

要求:
- 文件夹: ``LightShow``
- 格式: FSEQ + 同名的 MP3/WAV 音频文件
- 支持单文件上传和 ZIP 批量上传
"""

import os
import logging
import zipfile
import shutil
import tempfile

logger = logging.getLogger(__name__)

LIGHTSHOW_BASE = "/mnt/lightshow"
LIGHTSHOW_FOLDER = "LightShow"
LIGHTSHOW_DIR = os.path.join(LIGHTSHOW_BASE, LIGHTSHOW_FOLDER)

ALLOWED_EXTS = {".fseq", ".mp3", ".wav"}


def list_lightshows() -> list:
    """列出所有灯光秀（按基础名分组）"""
    if not os.path.isdir(LIGHTSHOW_DIR):
        return []
    shows = {}
    for fname in sorted(os.listdir(LIGHTSHOW_DIR)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_EXTS:
            continue
        base = os.path.splitext(fname)[0]
        fpath = os.path.join(LIGHTSHOW_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            size = os.path.getsize(fpath)
        except OSError:
            continue
        if base not in shows:
            shows[base] = {'name': base, 'files': [], 'total_size': 0}
        shows[base]['files'].append({'filename': fname, 'size': size, 'ext': ext})
        shows[base]['total_size'] += size
    return sorted(shows.values(), key=lambda x: x['name'].lower())


def upload_lightshow_file(file_obj, original_filename: str) -> tuple:
    """上传单个灯光秀文件。返回 (success, message)"""
    name = original_filename.strip()
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        return False, f"不支持的文件格式: {ext} (允许 .fseq/.mp3/.wav)"
    os.makedirs(LIGHTSHOW_DIR, exist_ok=True)
    safe_name = os.path.basename(name)
    target = os.path.join(LIGHTSHOW_DIR, safe_name)
    try:
        file_obj.save(target)
        logger.info(f"灯光秀文件已保存: {safe_name}")
        return True, f"文件 {safe_name} 已上传"
    except Exception as e:
        return False, f"保存失败: {e}"


def upload_lightshow_zip(file_obj) -> tuple:
    """从 ZIP 中提取灯光秀文件。返回 (success, message, count)"""
    try:
        # 保存临时 ZIP
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        file_obj.save(tmp.name)
        tmp.close()

        os.makedirs(LIGHTSHOW_DIR, exist_ok=True)
        count = 0
        with zipfile.ZipFile(tmp.name, 'r') as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                ext = os.path.splitext(info.filename)[1].lower()
                if ext not in ALLOWED_EXTS:
                    continue
                safe_name = os.path.basename(info.filename)
                target = os.path.join(LIGHTSHOW_DIR, safe_name)
                with zf.open(info) as src:
                    with open(target, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                count += 1
        os.unlink(tmp.name)
        logger.info(f"从 ZIP 中提取了 {count} 个灯光秀文件")
        return True, f"成功导入 {count} 个文件", count
    except zipfile.BadZipFile:
        return False, "无效的 ZIP 文件", 0
    except Exception as e:
        logger.error(f"ZIP 导入失败: {e}")
        return False, f"导入失败: {e}", 0


def delete_lightshow(basename: str) -> tuple:
    """删除同名灯光秀的所有文件。返回 (success, message)"""
    safe = os.path.basename(basename)
    deleted = 0
    for fname in os.listdir(LIGHTSHOW_DIR):
        if os.path.splitext(fname)[0] == safe:
            fpath = os.path.join(LIGHTSHOW_DIR, fname)
            try:
                os.remove(fpath)
                deleted += 1
            except OSError:
                pass
    if deleted == 0:
        return False, "未找到匹配的文件"
    logger.info(f"灯光秀 {safe} 已删除 ({deleted} 个文件)")
    return True, f"灯光秀 {safe} 已删除 ({deleted} 个文件)"


def get_available() -> bool:
    return os.path.isdir(LIGHTSHOW_BASE)
