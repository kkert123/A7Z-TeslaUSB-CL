"""视频裁剪纯逻辑层 —— 参数校验与 ffmpeg 命令构建（可单测，无 Flask 依赖）。

裁剪采用流拷贝（-c copy），免重编码、秒级完成；-ss 置于 -i 前做快速定位，
-t 为相对时长（end - start），语义明确无歧义。
"""
import subprocess
from typing import Tuple


def validate_trim_range(start_str, end_str, duration: float) -> Tuple[bool, float, float, str]:
    """校验起止时间，返回 (ok, start, end, error)。

    - start/end 必须为数字；start < end；0 <= start；end <= duration（+0.5s 容差）。
    - duration <= 0 时跳过上界校验（无法探测时长时由调用方决定）。
    """
    try:
        start = float(start_str)
        end = float(end_str)
    except (TypeError, ValueError):
        return False, 0.0, 0.0, "起止时间必须为数字"
    if start < 0:
        return False, start, end, "开始时间不能为负"
    if end <= start:
        return False, start, end, "结束时间必须大于开始时间"
    if duration and duration > 0 and end > duration + 0.5:
        return False, start, end, "结束时间超过视频时长"
    return True, start, end, ""


def probe_duration(video_path: str) -> float:
    """用 ffprobe 探测视频时长（秒）；失败/不可用返回 0.0。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", video_path],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return 0.0
        val = float(result.stdout.strip())
        return val if val > 0 else 0.0
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired, OSError):
        return 0.0


def build_trim_command(in_path: str, out_path: str, start: float, duration: float) -> list:
    """构建 ffmpeg 裁剪命令：流拷贝，start 为起点秒，duration 为裁剪时长（end-start）。"""
    return [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", in_path,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        out_path,
    ]
