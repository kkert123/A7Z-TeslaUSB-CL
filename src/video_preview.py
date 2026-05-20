#!/usr/bin/env python3
"""
TeslaUSB-Neo 视频预览/水印模块
============================

功能：
1. 从哨兵视频提取预览帧
2. 添加时间/位置水印
3. 生成缩略图
4. 视频信息提取

作者: TeslaUSB-Neo 项目
版本: 1.0.0
"""

import os
import subprocess
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from PIL import Image, ImageDraw, ImageFont
import tempfile

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('video_preview')


class VideoInfo:
    """视频信息数据类"""
    
    def __init__(self, path: Path):
        self.path = path
        self.duration: Optional[float] = None
        self.width: Optional[int] = None
        self.height: Optional[int] = None
        self.fps: Optional[float] = None
        self.codec: Optional[str] = None
        self.bitrate: Optional[str] = None
        self._extract_info()
    
    def _extract_info(self):
        """使用 ffprobe 提取视频信息"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                str(self.path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                data = json.loads(result.stdout)
                
                # 查找视频流
                for stream in data.get('streams', []):
                    if stream.get('codec_type') == 'video':
                        self.width = stream.get('width')
                        self.height = stream.get('height')
                        self.codec = stream.get('codec_name')
                        
                        # 计算 FPS
                        fps_num = stream.get('r_frame_rate', '0/1')
                        if '/' in fps_num:
                            num, den = fps_num.split('/')
                            self.fps = float(num) / float(den) if den != '0' else None
                        
                        break
                
                # 格式信息
                format_info = data.get('format', {})
                self.duration = float(format_info.get('duration', 0))
                self.bitrate = format_info.get('bit_rate')
        
        except Exception as e:
            logger.warning(f"提取视频信息失败: {e}")
    
    def __str__(self) -> str:
        return f"VideoInfo({self.path.name}, {self.width}x{self.height}, {self.duration:.1f}s)"


class VideoPreviewGenerator:
    """
    视频预览生成器
    
    功能：
    - 提取视频帧
    - 添加水印
    - 生成缩略图
    """
    
    def __init__(self, output_dir: Optional[Path] = None,
                 font_path: Optional[Path] = None,
                 watermark_enabled: bool = True):
        """
        初始化预览生成器
        
        Args:
            output_dir: 输出目录
            font_path: 字体文件路径
            watermark_enabled: 是否启用水印
        """
        self.output_dir = output_dir or Path('/opt/teslausb-web/data/previews')
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.font_path = font_path
        self.watermark_enabled = watermark_enabled
        
        # 查找可用字体
        self._font = None
        self._load_font()
        
        logger.info("视频预览生成器初始化完成")
    
    def _load_font(self):
        """加载字体"""
        font_paths = [
            self.font_path,
            Path('/usr/share/fonts/truetype/custom/simhei.ttf'),  # 黑体 (A7Z 中文首选)
            Path('/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'),  # DroidSans (中文)
            Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),  # 文泉驿
            Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),  # DejaVu
            Path('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'),  # Liberation
            Path('/usr/share/fonts/truetype/freefont/FreeSans.ttf'),  # FreeFont
        ]
        
        for font_path in font_paths:
            if font_path and font_path.exists():
                try:
                    self._font = ImageFont.truetype(str(font_path), 24)
                    logger.info(f"加载字体: {font_path}")
                    return
                except Exception as e:
                    logger.debug(f"字体加载失败: {font_path} - {e}")
        
        # 使用默认字体
        self._font = ImageFont.load_default()
        logger.warning("使用默认字体，中文可能显示异常")
    
    def extract_frame(self, video_path: Path, timestamp: str = "00:00:05",
                      output_path: Optional[Path] = None) -> Optional[Path]:
        """
        从视频提取单帧
        
        Args:
            video_path: 视频路径
            timestamp: 时间点 (HH:MM:SS 或 秒数)
            output_path: 输出路径（可选）
            
        Returns:
            输出图片路径
        """
        if not video_path.exists():
            logger.error(f"视频不存在: {video_path}")
            return None
        
        try:
            if output_path is None:
                output_path = self.output_dir / f"{video_path.stem}_frame.jpg"
            
            cmd = [
                'ffmpeg', '-y',
                '-i', str(video_path),
                '-ss', timestamp,
                '-vframes', '1',
                '-q:v', '5',  # 高质量
                '-pix_fmt', 'yuv420p',  # JPEG 兼容格式
                str(output_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0 and output_path.exists():
                logger.info(f"帧提取成功: {output_path}")
                return output_path
            else:
                logger.error(f"帧提取失败: {result.stderr}")
                return None
        
        except Exception as e:
            logger.error(f"帧提取异常: {e}")
            return None
    
    def add_watermark(self, image_path: Path, text_lines: List[str],
                      output_path: Optional[Path] = None,
                      position: str = "bottom-left") -> Optional[Path]:
        """
        添加水印
        
        Args:
            image_path: 图片路径
            text_lines: 水印文字列表
            output_path: 输出路径
            position: 位置 (top-left, top-right, bottom-left, bottom-right)
            
        Returns:
            输出图片路径
        """
        if not image_path.exists():
            logger.error(f"图片不存在: {image_path}")
            return None
        
        try:
            # 打开图片
            img = Image.open(image_path)
            draw = ImageDraw.Draw(img)
            
            # 计算文字尺寸
            if self._font:
                line_height = self._font.getbbox("Ay")[3] - self._font.getbbox("Ay")[1]
            else:
                line_height = 24
            
            # 边距
            margin = 20
            padding = 10
            
            # 计算文字区域
            text_width = 0
            for line in text_lines:
                bbox = draw.textbbox((0, 0), line, font=self._font)
                text_width = max(text_width, bbox[2] - bbox[0])
            
            text_height = len(text_lines) * (line_height + 5)
            box_width = text_width + padding * 2
            box_height = text_height + padding * 2
            
            # 计算位置
            img_width, img_height = img.size
            if position == "bottom-left":
                x, y = margin, img_height - box_height - margin
            elif position == "bottom-right":
                x, y = img_width - box_width - margin, img_height - box_height - margin
            elif position == "top-left":
                x, y = margin, margin
            elif position == "top-right":
                x, y = img_width - box_width - margin, margin
            else:
                x, y = margin, img_height - box_height - margin
            
            # 绘制半透明背景
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle(
                [x, y, x + box_width, y + box_height],
                fill=(0, 0, 0, 128)
            )
            img = Image.alpha_composite(img.convert('RGBA'), overlay)
            draw = ImageDraw.Draw(img)
            
            # 绘制文字
            text_y = y + padding
            for line in text_lines:
                draw.text((x + padding, text_y), line, 
                         fill=(255, 255, 255), font=self._font)
                text_y += line_height + 5
            
            # 保存
            if output_path is None:
                output_path = image_path
            
            img.convert('RGB').save(output_path, 'JPEG', quality=82)
            logger.info(f"水印添加成功: {output_path}")
            return output_path
        
        except Exception as e:
            logger.error(f"添加水印失败: {e}")
            return None
    
    def create_thumbnail(self, image_path: Path, size: Tuple[int, int] = (320, 240),
                        output_path: Optional[Path] = None) -> Optional[Path]:
        """
        创建缩略图
        
        Args:
            image_path: 图片路径
            size: 缩略图尺寸 (宽, 高)
            output_path: 输出路径
            
        Returns:
            缩略图路径
        """
        if not image_path.exists():
            logger.error(f"图片不存在: {image_path}")
            return None
        
        try:
            img = Image.open(image_path)
            img.thumbnail(size, Image.Resampling.LANCZOS)
            
            if output_path is None:
                output_path = self.output_dir / f"{image_path.stem}_thumb.jpg"
            
            img.save(output_path, 'JPEG', quality=85)
            logger.info(f"缩略图创建成功: {output_path}")
            return output_path
        
        except Exception as e:
            logger.error(f"创建缩略图失败: {e}")
            return None
    
    def generate_sentry_preview(self, event_folder: Path, event_id: str,
                                timestamp: Optional[datetime] = None,
                                location: Optional[str] = None,
                                camera_views: int = 4) -> Dict[str, Path]:
        """
        生成哨兵事件预览
        
        Args:
            event_folder: 事件文件夹路径
            event_id: 事件ID
            timestamp: 时间戳
            location: 位置
            camera_views: 提取的视角数量
            
        Returns:
            包含预览图路径的字典
        """
        results = {
            'main_preview': None,
            'thumbnails': [],
            'samples': []
        }
        
        try:
            # 查找所有视频文件
            video_files = sorted(event_folder.rglob('*.mp4'))
            
            if not video_files:
                logger.warning(f"事件 {event_id} 无视频文件")
                return results
            
            # 选择代表性视频
            selected_videos = video_files[:camera_views]
            
            # 主预览（第一个视频的第5秒）
            main_video = selected_videos[0]
            frame_path = self.extract_frame(
                main_video, 
                timestamp="00:00:05",
                output_path=self.output_dir / f"{event_id}_preview.jpg"
            )
            
            if frame_path and self.watermark_enabled:
                # 添加水印
                watermark_text = [
                    f"Tesla Sentry",
                    f"Time: {(timestamp or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')}",
                ]
                if location:
                    watermark_text.append(f"Location: {location}")
                
                frame_path = self.add_watermark(
                    frame_path,
                    watermark_text,
                    output_path=frame_path
                )
            
            results['main_preview'] = frame_path
            
            # 缩略图
            if frame_path:
                thumb_path = self.create_thumbnail(
                    frame_path,
                    size=(640, 480),
                    output_path=self.output_dir / f"{event_id}_thumb.jpg"
                )
                results['thumbnails'].append(thumb_path)
            
            # 采样其他视角
            for i, video in enumerate(selected_videos[1:], 1):
                sample_path = self.extract_frame(
                    video,
                    timestamp="00:00:03",
                    output_path=self.output_dir / f"{event_id}_sample_{i}.jpg"
                )
                if sample_path:
                    results['samples'].append(sample_path)
            
            logger.info(f"哨兵预览生成完成: {event_id}")
        
        except Exception as e:
            logger.error(f"生成哨兵预览失败: {e}")
        
        return results
    

    def generate_sentry_grid_preview(self, event_folder: Path, event_id: str,
                                      timestamp: Optional[datetime] = None,
                                      location: Optional[str] = None) -> Dict[str, Path]:
        """
        生成哨兵事件四宫格预览图 (2x2: front/back/left/right)

        Args:
            event_folder: 事件文件夹路径 (e.g. SentryClips/2026-04-13_17-09-09)
            event_id: 事件ID (不含重复后缀)
            timestamp: event.json 中的关键帧时间戳
            location: 位置信息

        Returns:
            {'grid_preview': Path, 'thumbnails': [Path], 'error': str|None}
        """
        result = {
            'grid_preview': None,
            'thumbnails': [],
            'error': None
        }

        # 1) 读取 event.json 获取关键帧时间戳
        event_json_path = event_folder / 'event.json'
        key_timestamp = None
        if event_json_path.exists():
            try:
                with open(event_json_path, 'r') as f:
                    event_data = json.load(f)
                ts_str = event_data.get('timestamp')
                if ts_str:
                    key_timestamp = datetime.fromisoformat(ts_str)
            except Exception as e:
                logger.warning(f"读取 event.json 失败: {e}")

        if timestamp and not key_timestamp:
            key_timestamp = timestamp
        if not key_timestamp:
            key_timestamp = datetime.now()

        # 2) 解析文件夹名获取视频段起始时间
        folder_name = event_folder.name
        try:
            video_start_str = folder_name.replace('_', '-', 2).replace('_', ' ')
            video_start = datetime.strptime(video_start_str, '%Y-%m-%d-%H-%M-%S')
        except Exception:
            logger.warning(f"无法解析文件夹名: {folder_name}")
            video_start = None

        # 3) 查找四个摄像头视频文件
        camera_map = {
            'front': ('前摄像头', False),
            'back': ('后摄像头', False),
            'left': ('左摄像头', True),
            'right': ('右摄像头', True),
        }

        frames = {}  # camera_key -> PIL Image

        for cam_key, (cam_label, need_flip) in camera_map.items():
            # 搜索该摄像头的视频文件
            # 兼容两种名称: left 和 left_repeater
            video_files = sorted(event_folder.glob(f'*-{cam_key}.mp4'))
            if not video_files and cam_key in ('left', 'right'):
                video_files = sorted(event_folder.glob(f'*-{cam_key}_repeater.mp4'))
            if not video_files:
                logger.warning(f"未找到 {cam_key} 摄像头视频: {event_folder}")
                continue

            video_path = video_files[0]

            # 计算关键帧在视频中的时间偏移
            time_offset = None
            if video_start and key_timestamp:
                delta = (key_timestamp - video_start).total_seconds()
                if delta > 0:
                    time_offset = delta

            # 如果没有有效偏移，默认取第 3 秒
            if time_offset is None or time_offset < 0:
                time_offset = 3.0

            # 使用 ffmpeg 快速提取帧 (-ss 在 -i 前面)
            try:
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                    tmp_path = tmp.name

                cmd = [
                    'ffmpeg', '-y',
                    '-ss', str(time_offset),
                    '-i', str(video_path),
                    '-vframes', '1',
                    '-q:v', '5',
                    '-pix_fmt', 'yuv420p',
                    tmp_path
                ]

                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if proc.returncode == 0:
                    frame = Image.open(tmp_path)
                    # 左右摄像头水平翻转
                    if need_flip:
                        frame = frame.transpose(Image.FLIP_LEFT_RIGHT)
                    frames[cam_key] = (frame, cam_label)
                    logger.info(f"提取 {cam_key} 帧成功 (offset={time_offset:.1f}s)")
                else:
                    logger.warning(f"提取 {cam_key} 帧失败: {proc.stderr[:200]}")

                # 清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            except subprocess.TimeoutExpired:
                logger.warning(f"提取 {cam_key} 帧超时 (60s)")
            except Exception as e:
                logger.warning(f"提取 {cam_key} 帧异常: {e}")

        # 4) 检查是否至少提取到一个帧
        if not frames:
            result['error'] = '所有摄像头帧提取失败'
            return result

        # 5) 构建四宫格
        try:
            # 获取第一帧的尺寸作为基准
            first_frame = list(frames.values())[0][0]
            cell_w, cell_h = first_frame.size
            gap = 4  # 格子间隔

            grid_w = cell_w * 2 + gap
            grid_h = cell_h * 2 + gap

            # 创建画布 (先不绘制水印)
            grid = Image.new('RGB', (grid_w, grid_h), (30, 30, 30))
            draw = ImageDraw.Draw(grid)

            # 布局: 上排 [front, back], 下排 [left, right]
            grid_layout = [
                [('front', 0, 0), ('back', cell_w + gap, 0)],
                [('left', 0, cell_h + gap), ('right', cell_w + gap, cell_h + gap)],
            ]

            for row in grid_layout:
                for cam_key, x, y in row:
                    if cam_key in frames:
                        frame_img, cam_label = frames[cam_key]
                        # 统一尺寸
                        if frame_img.size != (cell_w, cell_h):
                            frame_img = frame_img.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
                        grid.paste(frame_img, (x, y))

                        # 摄像头标签 - 在原始尺寸上绘制
                        label_font_size = max(24, cell_h // 25)
                        try:
                            label_font = ImageFont.truetype(
                                '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
                                label_font_size
                            )
                        except Exception:
                            label_font = self._font or ImageFont.load_default()

                        label_text = cam_label
                        bbox = draw.textbbox((0, 0), label_text, font=label_font)
                        tw = bbox[2] - bbox[0]
                        th = bbox[3] - bbox[1]

                        label_padding = 8
                        label_x = x + 10
                        label_y = y + 10

                        # 半透明标签背景
                        overlay = Image.new('RGBA', grid.size, (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        overlay_draw.rectangle(
                            [label_x, label_y, label_x + tw + label_padding * 2, label_y + th + label_padding * 2],
                            fill=(0, 0, 0, 160)
                        )
                        grid = Image.alpha_composite(grid.convert('RGBA'), overlay)
                        draw = ImageDraw.Draw(grid)
                        draw.text((label_x + label_padding, label_y + label_padding),
                                  label_text, fill=(255, 255, 255), font=label_font)
                    else:
                        # 缺失摄像头 - 绘制占位符
                        draw.rectangle([x, y, x + cell_w, y + cell_h], fill=(50, 50, 50))
                        try:
                            ph_font = ImageFont.truetype(
                                '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf', 28
                            )
                        except Exception:
                            ph_font = self._font or ImageFont.load_default()
                        draw.text((x + cell_w // 2 - 40, y + cell_h // 2 - 14),
                                  '无数据', fill=(120, 120, 120), font=ph_font)

            # 6) 缩放到目标尺寸 (原始约 2572x1996 -> 1600x1241)
            target_w = 1000
            target_h = int(grid_h * target_w / grid_w)
            grid = grid.resize((target_w, target_h), Image.Resampling.LANCZOS)

            # 7) 缩放后绘制右下角时间水印 (确保清晰)
            draw = ImageDraw.Draw(grid)

            # 时间水印文字
            time_str = key_timestamp.strftime('%Y-%m-%dT%H:%M:%S')
            watermark_lines = [time_str]
            if location:
                watermark_lines.append(location)

            # 时间数字用 DejaVu（笔画清晰），中文位置用 DroidSans
            try:
                time_font = ImageFont.truetype(
                    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 36
                )
                cn_font = ImageFont.truetype(
                    '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf', 36
                )
            except Exception:
                time_font = self._font or ImageFont.load_default()
                cn_font = time_font

            wm_margin = 16
            wm_padding = 10
            line_spacing = 6

            # 计算水印区域尺寸
            total_text_height = 0
            max_text_width = 0
            for i, line in enumerate(watermark_lines):
                # 时间行用 DejaVu，位置行用中文字体
                font = time_font if i == 0 else cn_font
                bbox = draw.textbbox((0, 0), line, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                max_text_width = max(max_text_width, tw)
                total_text_height += th + line_spacing
            total_text_height -= line_spacing  # 最后一行不需要额外间距

            box_w = max_text_width + wm_padding * 2
            box_h = total_text_height + wm_padding * 2
            wm_x = target_w - box_w - wm_margin
            wm_y = target_h - box_h - wm_margin

            # 半透明背景
            overlay = Image.new('RGBA', grid.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle(
                [wm_x, wm_y, wm_x + box_w, wm_y + box_h],
                fill=(0, 0, 0, 170)
            )
            grid = Image.alpha_composite(grid.convert('RGBA'), overlay)
            draw = ImageDraw.Draw(grid)

            # 绘制时间水印文字（时间用 DejaVu，位置用中文字体）
            text_y = wm_y + wm_padding
            for i, line in enumerate(watermark_lines):
                font = time_font if i == 0 else cn_font
                bbox = draw.textbbox((0, 0), line, font=font)
                th = bbox[3] - bbox[1]
                draw.text((wm_x + wm_padding, text_y), line,
                          fill=(255, 255, 255), font=font)
                text_y += th + line_spacing

            # 8) 保存最终图片
            grid_rgb = grid.convert('RGB')
            output_path = self.output_dir / f"{event_id}_grid_preview.jpg"
            grid_rgb.save(output_path, 'JPEG', quality=82)

            result['grid_preview'] = output_path

            # 生成缩略图
            thumb_path = self.create_thumbnail(
                output_path, size=(640, 480),
                output_path=self.output_dir / f"{event_id}_grid_thumb.jpg"
            )
            if thumb_path:
                result['thumbnails'].append(thumb_path)

            logger.info(f"四宫格预览生成完成: {output_path} ({output_path.stat().st_size // 1024}KB)")

        except Exception as e:
            result['error'] = f'四宫格生成失败: {e}'
            logger.error(f"四宫格生成失败: {e}")

        return result

    def create_video_preview(self, video_path: Path, 
                            output_path: Optional[Path] = None,
                            duration: int = 3) -> Optional[Path]:
        """
        创建视频预览（短视频片段）
        
        Args:
            video_path: 原始视频路径
            output_path: 输出路径
            duration: 预览时长（秒）
            
        Returns:
            预览视频路径
        """
        if not video_path.exists():
            logger.error(f"视频不存在: {video_path}")
            return None
        
        try:
            if output_path is None:
                output_path = self.output_dir / f"{video_path.stem}_preview.mp4"
            
            cmd = [
                'ffmpeg', '-y',
                '-i', str(video_path),
                '-ss', '00:00:02',  # 从第2秒开始
                '-t', str(duration),  # 截取duration秒
                '-c:v', 'libx264',
                '-c:a', 'aac',
                '-strict', 'experimental',
                '-b:v', '1M',  # 降低码率
                '-vf', 'scale=640:360',  # 降低分辨率
                str(output_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0 and output_path.exists():
                logger.info(f"视频预览创建成功: {output_path}")
                return output_path
            else:
                logger.error(f"视频预览失败: {result.stderr}")
                return None
        
        except Exception as e:
            logger.error(f"创建视频预览异常: {e}")
            return None
    
    def check_dependencies(self) -> Dict[str, bool]:
        """检查依赖是否安装"""
        deps = {
            'ffmpeg': False,
            'ffprobe': False,
            'pillow': False,
        }
        
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
            deps['ffmpeg'] = True
        except:
            pass
        
        try:
            subprocess.run(['ffprobe', '-version'], capture_output=True, timeout=5)
            deps['ffprobe'] = True
        except:
            pass
        
        try:
            import PIL
            deps['pillow'] = True
        except ImportError:
            pass
        
        return deps


# ---------------------------------------------------------------------------
# 单张缩略图生成函数（供 CPU 自适应预览生成系统调用）
# ---------------------------------------------------------------------------

def generate_thumbnail_for_event(folder_type: str, event_id: str) -> dict:
    """
    为单个事件生成单张缩略图（从前摄像头提取）
    用于 CPU 自适应后台补生成，解决 /videos 页面打开时实时生成慢的问题。

    Args:
        folder_type: 'RecentClips' / 'SavedClips' / 'EncryptedClips'
        event_id: 事件ID

    Returns:
        {
            'success': bool,
            'thumbnail_path': str or None,
            'error': str or None
        }
    """
    result = {'success': False, 'thumbnail_path': None, 'error': None}

    try:
        base_cam_path = Path('/media/cnlvan/cam/TeslaCam')
        thumb_dir = Path('/opt/teslausb-web/static/thumbnails')
        thumb_dir.mkdir(parents=True, exist_ok=True)

        # 根据 folder_type 定位视频文件（统一用前摄像头）
        video_path = None

        if folder_type == 'SavedClips':
            # 目录结构: SavedClips/{event_id}/front.mp4
            candidate = base_cam_path / folder_type / event_id / 'front.mp4'
            if candidate.exists():
                video_path = candidate
            else:
                # 兼容可能的命名变体
                videos = sorted((base_cam_path / folder_type / event_id).glob('*-front.mp4'))
                if videos:
                    video_path = videos[0]

        elif folder_type == 'RecentClips':
            # 文件结构: RecentClips/{event_id}-front.mp4（文件平铺，无子目录）
            candidate = base_cam_path / folder_type / f'{event_id}-front.mp4'
            if candidate.exists():
                video_path = candidate
            else:
                # 尝试搜索
                videos = sorted((base_cam_path / folder_type).glob(f'{event_id}-front.mp4'))
                if videos:
                    video_path = videos[0]

        elif folder_type == 'EncryptedClips':
            # 目前结构未知，暂跳过
            result['error'] = 'EncryptedClips 结构待确认，暂不支持'
            logger.warning(f"EncryptedClips 暂不支持: {event_id}")
            return result

        else:
            result['error'] = f'未知 folder_type: {folder_type}'
            return result

        if not video_path or not video_path.exists():
            result['error'] = f'未找到前摄像头视频文件: {folder_type}/{event_id}'
            logger.warning(result['error'])
            return result

        # 输出路径
        output_path = thumb_dir / f'{event_id}.jpg'

        # 用 ffmpeg 提取关键帧（从第3秒，-ss 放 -i 前以加快速度）
        cmd = [
            'ffmpeg', '-y',
            '-ss', '3',
            '-i', str(video_path),
            '-vframes', '1',
            '-q:v', '5',
            '-pix_fmt', 'yuv420p',
            str(output_path)
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if proc.returncode == 0 and output_path.exists():
            result['success'] = True
            result['thumbnail_path'] = str(output_path)
            logger.info(f"缩略图生成成功: {output_path} ({output_path.stat().st_size // 1024}KB)")
        else:
            result['error'] = f'ffmpeg 失败: {proc.stderr[:200]}'
            logger.error(f"缩略图生成失败: {result['error']}")

        return result

    except subprocess.TimeoutExpired:
        result['error'] = 'ffmpeg 超时（60s）'
        logger.warning(f"缩略图生成超时: {folder_type}/{event_id}")
        return result
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"缩略图生成异常: {e}")
        return result


# 测试代码
if __name__ == '__main__':
    import sys
    
    print("="*60)
    print("视频预览生成器测试")
    print("="*60)
    
    # 检查依赖
    generator = VideoPreviewGenerator()
    deps = generator.check_dependencies()
    
    print("\n依赖检查:")
    for name, installed in deps.items():
        status = "✓" if installed else "✗"
        print(f"  {status} {name}")
    
    if not all(deps.values()):
        print("\n错误: 缺少依赖，请安装:")
        print("  - ffmpeg: sudo apt-get install ffmpeg")
        print("  - pillow: pip install Pillow")
        sys.exit(1)
    
    # 检查测试视频
    if len(sys.argv) > 1:
        test_video = Path(sys.argv[1])
    else:
        test_video = Path('/tmp/test_sentry/sample.mp4')
    
    if not test_video.exists():
        print(f"\n创建测试视频: {test_video}")
        test_video.parent.mkdir(parents=True, exist_ok=True)
        
        # 生成测试视频
        cmd = [
            'ffmpeg', '-y',
            '-f', 'lavfi',
            '-i', 'testsrc=duration=10:size=1280x720:rate=30',
            '-pix_fmt', 'yuv420p',
            str(test_video)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"创建测试视频失败: {result.stderr}")
            sys.exit(1)
        print("测试视频创建成功")
    
    print(f"\n测试视频: {test_video}")
    
    # 视频信息
    print("\n1. 提取视频信息...")
    info = VideoInfo(test_video)
    print(f"   分辨率: {info.width}x{info.height}")
    print(f"   时长: {info.duration:.1f}s")
    print(f"   编码: {info.codec}")
    
    # 提取帧
    print("\n2. 提取视频帧...")
    frame_path = generator.extract_frame(test_video)
    if frame_path:
        print(f"   ✓ 帧提取成功: {frame_path}")
        
        # 添加水印
        print("\n3. 添加水印...")
        watermarked = generator.add_watermark(
            frame_path,
            ["Tesla Sentry", "2024-01-15 14:30:00", "Location: Home"],
            output_path=generator.output_dir / "test_watermarked.jpg"
        )
        if watermarked:
            print(f"   ✓ 水印添加成功: {watermarked}")
        
        # 创建缩略图
        print("\n4. 创建缩略图...")
        thumbnail = generator.create_thumbnail(frame_path, size=(320, 240))
        if thumbnail:
            print(f"   ✓ 缩略图创建成功: {thumbnail}")
    else:
        print("   ✗ 帧提取失败")
    
    # 创建视频预览
    print("\n5. 创建视频预览...")
    video_preview = generator.create_video_preview(test_video, duration=3)
    if video_preview:
        print(f"   ✓ 视频预览创建成功: {video_preview}")
    else:
        print("   ✗ 视频预览创建失败")
    
    print("\n" + "="*60)
    print("测试完成")
    print(f"输出目录: {generator.output_dir}")
    print("="*60)
