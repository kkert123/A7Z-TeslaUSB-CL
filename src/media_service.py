#!/usr/bin/env python3
"""
TeslaUSB Neo - 媒体管理服务
===========================

功能:
1. Boombox 音频文件上传管理
2. Lightshow 灯光秀文件上传管理  
3. Wraps 车辆贴纸图片管理

作者：TeslaUSB-Neo 项目
版本：1.0.0
"""

import os
import re
import shutil
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Tuple, List, Dict, Optional
from PIL import Image
import struct

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('media_service')

# 分区挂载点配置
PARTITIONS = {
    "cam": "/media/cnlvan/cam",
    "music": "/media/cnlvan/music",
    "lightshow": "/media/cnlvan/lightshow",
    "boombox": "/media/cnlvan/boombox",
}

# 允许的音频格式
ALLOWED_AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aac", ".m4a"}

# Lightshow 文件格式
ALLOWED_LIGHTSHOW_EXTS = {".fseq", ".mp3", ".wav"}

# Wraps 限制
MAX_WRAP_SIZE = 1 * 1024 * 1024  # 1 MB
MIN_DIMENSION = 512
MAX_DIMENSION = 1024
MAX_FILENAME_LENGTH = 30
VALID_FILENAME_PATTERN = re.compile(r'^[a-zA-Z0-9_\- ]+$')


# ==============================================================================
# 🎵 Boombox 音频服务
# ==============================================================================

class BoomboxService:
    """Boombox 音频文件管理服务"""
    
    @staticmethod
    def get_music_root() -> Path:
        """获取音乐根目录"""
        return Path(PARTITIONS["music"]) / "Music"
    
    @staticmethod
    def validate_audio_file(filename: str) -> Tuple[bool, Optional[str]]:
        """
        验证音频文件
        
        Args:
            filename: 文件名
            
        Returns:
            (是否有效，错误信息)
        """
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTS:
            return False, f"不支持的音频格式：{ext}，支持的格式：{', '.join(ALLOWED_AUDIO_EXTS)}"
        
        # 检查文件名
        if len(filename) > 100:
            return False, "文件名过长（最多 100 字符）"
        
        # 检查非法字符
        if re.search(r'[/\\:*?"<>|]', filename):
            return False, "文件名包含非法字符"
        
        return True, None
    
    @staticmethod
    def upload_audio_file(file_data, filename: str, folder: str = None) -> Tuple[bool, str]:
        """
        上传音频文件
        
        Args:
            file_data: 文件数据（二进制流）
            filename: 文件名
            folder: 子文件夹（可选）
            
        Returns:
            (是否成功，消息)
        """
        try:
            # 验证文件名
            is_valid, error = BoomboxService.validate_audio_file(filename)
            if not is_valid:
                return False, error
            
            # 确保目录存在
            music_root = BoomboxService.get_music_root()
            if folder:
                # 清理文件夹名
                folder = re.sub(r'[^a-zA-Z0-9_\-\u4e00-\u9fa5 ]', '', folder)
                target_dir = music_root / folder
            else:
                target_dir = music_root
            
            target_dir.mkdir(parents=True, exist_ok=True)
            
            # 保存文件
            file_path = target_dir / filename
            
            # 检查是否已存在
            if file_path.exists():
                base, ext = Path(filename).stem, Path(filename).suffix
                counter = 1
                while (target_dir / f"{base}_{counter}{ext}").exists():
                    counter += 1
                file_path = target_dir / f"{base}_{counter}{ext}"
            
            # 写入文件
            with open(file_path, 'wb') as f:
                if hasattr(file_data, 'read'):
                    shutil.copyfileobj(file_data, f)
                else:
                    f.write(file_data)
            
            logger.info(f"音频文件上传成功：{file_path}")
            return True, f"音频文件已上传：{file_path.name}"
            
        except Exception as e:
            logger.error(f"上传音频文件失败：{e}")
            return False, f"上传失败：{str(e)}"
    
    @staticmethod
    def list_audio_files(folder: str = None) -> List[Dict]:
        """
        列出音频文件
        
        Args:
            folder: 子文件夹（可选）
            
        Returns:
            文件列表
        """
        try:
            music_root = BoomboxService.get_music_root()
            if folder:
                target_dir = music_root / folder
            else:
                target_dir = music_root
            
            if not target_dir.exists():
                return []
            
            files = []
            for item in target_dir.iterdir():
                if item.is_file() and item.suffix.lower() in ALLOWED_AUDIO_EXTS:
                    files.append({
                        'name': item.name,
                        'size': item.stat().st_size,
                        'path': str(item.relative_to(music_root)),
                        'modified': item.stat().st_mtime
                    })
            
            # 按名称排序
            files.sort(key=lambda x: x['name'])
            return files
            
        except Exception as e:
            logger.error(f"列出音频文件失败：{e}")
            return []
    
    @staticmethod
    def delete_audio_file(filename: str, folder: str = None) -> Tuple[bool, str]:
        """
        删除音频文件
        
        Args:
            filename: 文件名
            folder: 子文件夹（可选）
            
        Returns:
            (是否成功，消息)
        """
        try:
            music_root = BoomboxService.get_music_root()
            if folder:
                file_path = music_root / folder / filename
            else:
                file_path = music_root / filename
            
            if not file_path.exists():
                return False, "文件不存在"
            
            file_path.unlink()
            logger.info(f"音频文件已删除：{filename}")
            return True, "文件已删除"
            
        except Exception as e:
            logger.error(f"删除音频文件失败：{e}")
            return False, f"删除失败：{str(e)}"


# ==============================================================================
# 💡 Lightshow 灯光秀服务
# ==============================================================================

class LightshowService:
    """Lightshow 灯光秀文件管理服务"""
    
    @staticmethod
    def get_lightshow_root() -> Path:
        """获取灯光秀根目录"""
        return Path(PARTITIONS["lightshow"]) / "LightShow"
    
    @staticmethod
    def upload_zip_file(file_data) -> Tuple[bool, str, int]:
        """
        上传并解压 ZIP 文件
        
        Args:
            file_data: ZIP 文件数据
            
        Returns:
            (是否成功，消息，文件数量)
        """
        try:
            # 创建临时目录
            temp_dir = tempfile.mkdtemp(prefix='lightshow_')
            extracted_files = []
            
            try:
                # 保存 ZIP 文件
                zip_path = Path(temp_dir) / 'upload.zip'
                with open(zip_path, 'wb') as f:
                    if hasattr(file_data, 'read'):
                        shutil.copyfileobj(file_data, f)
                    else:
                        f.write(file_data)
                
                # 解压 ZIP
                extract_dir = Path(temp_dir) / 'extracted'
                extract_dir.mkdir(parents=True, exist_ok=True)
                
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                
                # 递归查找灯光秀文件
                for root, dirs, files in os.walk(extract_dir):
                    for file in files:
                        if Path(file).suffix.lower() in ALLOWED_LIGHTSHOW_EXTS:
                            source_path = Path(root) / file
                            extracted_files.append((source_path, file))
                
                if not extracted_files:
                    shutil.rmtree(temp_dir)
                    return False, "ZIP 文件中没有找到灯光秀文件 (.fseq, .mp3, .wav)", 0
                
                logger.info(f"找到 {len(extracted_files)} 个灯光秀文件")
                
                # 确保目录存在
                lightshow_root = LightshowService.get_lightshow_root()
                lightshow_root.mkdir(parents=True, exist_ok=True)
                
                # 复制文件
                copied_count = 0
                for source_path, filename in extracted_files:
                    dest_path = lightshow_root / filename
                    
                    # 处理重名
                    if dest_path.exists():
                        base, ext = Path(filename).stem, Path(filename).suffix
                        counter = 1
                        while (lightshow_root / f"{base}_{counter}{ext}").exists():
                            counter += 1
                        dest_path = lightshow_root / f"{base}_{counter}{ext}"
                    
                    shutil.copy2(source_path, dest_path)
                    copied_count += 1
                
                # 清理临时文件
                shutil.rmtree(temp_dir)
                
                return True, f"成功上传 {copied_count} 个灯光秀文件", copied_count
                
            except zipfile.BadZipFile:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return False, "无效的 ZIP 文件", 0
            except Exception as e:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.error(f"处理 ZIP 文件失败：{e}")
                return False, f"处理 ZIP 失败：{str(e)}", 0
                
        except Exception as e:
            logger.error(f"上传灯光秀文件失败：{e}")
            return False, f"上传失败：{str(e)}", 0
    
    @staticmethod
    def list_lightshow_files() -> List[Dict]:
        """
        列出灯光秀文件
        
        Returns:
            文件列表
        """
        try:
            lightshow_root = LightshowService.get_lightshow_root()
            
            if not lightshow_root.exists():
                return []
            
            files = []
            for item in lightshow_root.iterdir():
                if item.is_file() and item.suffix.lower() in ALLOWED_LIGHTSHOW_EXTS:
                    file_type = "序列" if item.suffix.lower() == ".fseq" else "音频"
                    files.append({
                        'name': item.name,
                        'type': file_type,
                        'size': item.stat().st_size,
                        'path': str(item.relative_to(lightshow_root)),
                        'modified': item.stat().st_mtime
                    })
            
            # 先序列后音频，按名称排序
            files.sort(key=lambda x: (x['type'] != "序列", x['name']))
            return files
            
        except Exception as e:
            logger.error(f"列出灯光秀文件失败：{e}")
            return []
    
    @staticmethod
    def delete_lightshow_file(filename: str) -> Tuple[bool, str]:
        """
        删除灯光秀文件
        
        Args:
            filename: 文件名
            
        Returns:
            (是否成功，消息)
        """
        try:
            lightshow_root = LightshowService.get_lightshow_root()
            file_path = lightshow_root / filename
            
            if not file_path.exists():
                return False, "文件不存在"
            
            file_path.unlink()
            logger.info(f"灯光秀文件已删除：{filename}")
            return True, "文件已删除"
            
        except Exception as e:
            logger.error(f"删除灯光秀文件失败：{e}")
            return False, f"删除失败：{str(e)}"


# ==============================================================================
# 🎨 Wraps 车辆贴纸服务
# ==============================================================================

class WrapsService:
    """Wraps 车辆贴纸图片管理服务"""
    
    @staticmethod
    def get_wraps_root() -> Path:
        """获取 Wraps 根目录"""
        return Path(PARTITIONS["lightshow"]) / "Wraps"
    
    @staticmethod
    def get_png_dimensions(file_path: Path) -> Tuple[Optional[int], Optional[int]]:
        """
        读取 PNG 文件尺寸
        
        Args:
            file_path: PNG 文件路径
            
        Returns:
            (宽度，高度)
        """
        try:
            with open(file_path, 'rb') as f:
                # 读取 PNG 签名（8 字节）
                signature = f.read(8)
                if signature != b'\x89PNG\r\n\x1a\n':
                    return None, None
                
                # 读取 IHDR 块长度（4 字节）和类型（4 字节）
                chunk_length = struct.unpack('>I', f.read(4))[0]
                chunk_type = f.read(4)
                
                if chunk_type != b'IHDR':
                    return None, None
                
                # 读取宽度和高度（各 4 字节）
                width = struct.unpack('>I', f.read(4))[0]
                height = struct.unpack('>I', f.read(4))[0]
                
                return width, height
                
        except Exception as e:
            logger.error(f"读取 PNG 尺寸失败：{e}")
            return None, None
    
    @staticmethod
    def validate_wrap_file(file_path: Path, filename: str) -> Tuple[bool, Optional[str]]:
        """
        验证 Wraps 文件
        
        Args:
            file_path: 文件路径
            filename: 文件名
            
        Returns:
            (是否有效，错误信息)
        """
        # 检查扩展名
        if not filename.lower().endswith('.png'):
            return False, "只支持 PNG 格式"
        
        # 检查文件名
        base_name = Path(filename).stem
        if len(base_name) > MAX_FILENAME_LENGTH:
            return False, f"文件名不能超过{MAX_FILENAME_LENGTH}字符"
        
        if not VALID_FILENAME_PATTERN.match(base_name):
            return False, "文件名只能包含字母、数字、下划线、短横线"
        
        # 检查文件大小
        file_size = file_path.stat().st_size
        if file_size > MAX_WRAP_SIZE:
            return False, f"文件大小不能超过 1MB（当前{file_size / 1024 / 1024:.2f}MB）"
        
        # 检查尺寸
        width, height = WrapsService.get_png_dimensions(file_path)
        if width is None or height is None:
            return False, "无法读取图片尺寸，文件可能已损坏"
        
        if width < MIN_DIMENSION or height < MIN_DIMENSION:
            return False, f"图片尺寸不能小于{MIN_DIMENSION}x{MIN_DIMENSION}"
        
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            return False, f"图片尺寸不能大于{MAX_DIMENSION}x{MAX_DIMENSION}"
        
        return True, None
    
    @staticmethod
    def upload_wrap_file(file_data, filename: str) -> Tuple[bool, str]:
        """
        上传 Wraps 文件
        
        Args:
            file_data: 文件数据
            filename: 文件名
            
        Returns:
            (是否成功，消息)
        """
        try:
            # 验证文件名
            is_valid, error = WrapsService.validate_wrap_filename(filename)
            if not is_valid:
                return False, error
            
            # 确保目录存在
            wraps_root = WrapsService.get_wraps_root()
            wraps_root.mkdir(parents=True, exist_ok=True)
            
            # 保存临时文件验证
            temp_path = wraps_root / f".tmp_{filename}"
            with open(temp_path, 'wb') as f:
                if hasattr(file_data, 'read'):
                    shutil.copyfileobj(file_data, f)
                else:
                    f.write(file_data)
            
            # 验证文件
            is_valid, error = WrapsService.validate_wrap_file(temp_path, filename)
            if not is_valid:
                temp_path.unlink()
                return False, error
            
            # 重命名为正式文件
            file_path = wraps_root / filename
            if file_path.exists():
                base, ext = Path(filename).stem, Path(filename).suffix
                counter = 1
                while (wraps_root / f"{base}_{counter}{ext}").exists():
                    counter += 1
                file_path = wraps_root / f"{base}_{counter}{ext}"
            
            temp_path.rename(file_path)
            
            logger.info(f"Wraps 文件上传成功：{file_path}")
            return True, f"Wraps 文件已上传：{file_path.name}"
            
        except Exception as e:
            logger.error(f"上传 Wraps 文件失败：{e}")
            if 'temp_path' in locals() and temp_path.exists():
                temp_path.unlink()
            return False, f"上传失败：{str(e)}"
    
    @staticmethod
    def validate_wrap_filename(filename: str) -> Tuple[bool, Optional[str]]:
        """
        验证 Wraps 文件名
        
        Args:
            filename: 文件名
            
        Returns:
            (是否有效，错误信息)
        """
        if not filename.lower().endswith('.png'):
            return False, "只支持 PNG 格式"
        
        base_name = Path(filename).stem
        if len(base_name) > MAX_FILENAME_LENGTH:
            return False, f"文件名不能超过{MAX_FILENAME_LENGTH}字符"
        
        if not base_name:
            return False, "文件名不能为空"
        
        if not VALID_FILENAME_PATTERN.match(base_name):
            return False, "文件名只能包含字母、数字、下划线、短横线"
        
        return True, None
    
    @staticmethod
    def list_wrap_files() -> List[Dict]:
        """
        列出 Wraps 文件
        
        Returns:
            文件列表
        """
        try:
            wraps_root = WrapsService.get_wraps_root()
            
            if not wraps_root.exists():
                return []
            
            files = []
            for item in wraps_root.iterdir():
                if item.is_file() and item.suffix.lower() == '.png':
                    width, height = WrapsService.get_png_dimensions(item)
                    files.append({
                        'name': item.name,
                        'size': item.stat().st_size,
                        'width': width,
                        'height': height,
                        'path': str(item.relative_to(wraps_root)),
                        'modified': item.stat().st_mtime
                    })
            
            files.sort(key=lambda x: x['name'])
            return files
            
        except Exception as e:
            logger.error(f"列出 Wraps 文件失败：{e}")
            return []
    
    @staticmethod
    def delete_wrap_file(filename: str) -> Tuple[bool, str]:
        """
        删除 Wraps 文件
        
        Args:
            filename: 文件名
            
        Returns:
            (是否成功，消息)
        """
        try:
            wraps_root = WrapsService.get_wraps_root()
            file_path = wraps_root / filename
            
            if not file_path.exists():
                return False, "文件不存在"
            
            file_path.unlink()
            logger.info(f"Wraps 文件已删除：{filename}")
            return True, "文件已删除"
            
        except Exception as e:
            logger.error(f"删除 Wraps 文件失败：{e}")
            return False, f"删除失败：{str(e)}"


# ==============================================================================
# 统一服务接口
# ==============================================================================

class MediaService:
    """媒体管理统一服务接口"""
    
    boombox = BoomboxService
    lightshow = LightshowService
    wraps = WrapsService


# 测试代码
if __name__ == '__main__':
    print("TeslaUSB Neo 媒体管理服务测试")
    print("="*60)
    
    # 测试 Boombox
    print("\n[Boombox 音频服务]")
    print(f"音乐根目录：{BoomboxService.get_music_root()}")
    
    # 测试 Lightshow
    print("\n[Lightshow 灯光秀服务]")
    print(f"灯光秀根目录：{LightshowService.get_lightshow_root()}")
    
    # 测试 Wraps
    print("\n[Wraps 车辆贴纸服务]")
    print(f"Wraps 根目录：{WrapsService.get_wraps_root()}")
    
    print("\n媒体服务初始化完成！")
