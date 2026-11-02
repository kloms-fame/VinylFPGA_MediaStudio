"""Deterministic media conversion using only the bundled tools."""
import array
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from .paths import tool_path


class BuildCancelled(ValueError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set(): raise BuildCancelled('用户已取消生成')


def run_tool(name, arguments, *, cancel=None):
    check_cancel(cancel)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as stdout:
        process = subprocess.Popen([str(tool_path(name)), *map(str, arguments)],
                                   stdin=subprocess.DEVNULL, stdout=stdout,
                                   stderr=errors, creationflags=flags)
        try:
            while process.poll() is None:
                check_cancel(cancel)
                time.sleep(0.1)
            stdout.seek(0)
            output = stdout.read()
            if process.returncode:
                errors.seek(0)
                detail = errors.read().decode('utf-8', 'replace')[-3000:]
                raise ValueError('媒体转换失败：' + detail)
            return output
        finally:
            if process.poll() is None:
                process.kill(); process.wait()


def probe(path, *, cancel=None):
    result = json.loads(run_tool('ffprobe', ['-v', 'error', '-show_streams', '-show_format',
                                            '-of', 'json', path], cancel=cancel))
    return result


def convert_audio(source, target, *, cancel=None):
    options = ['-f', 's16le', '-ar', '48000', '-ac', '1'] if Path(source).suffix.lower() == '.pcm' else []
    run_tool('ffmpeg', ['-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *options,
                       '-i', source, '-map', '0:a:0', '-vn', '-sn', '-dn', '-ar', '48000',
                       '-ac', '1', '-c:a', 'pcm_s16le', '-f', 's16le', target], cancel=cancel)
    size = Path(target).stat().st_size
    if not size or size % 2: raise ValueError('转换后的 PCM 长度必须为非零偶数')
    return size // 2


def convert_cover(source, target, *, fit='cover', crop=(0.5, 0.5)):
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert('RGB')
        if fit == 'cover':
            result = ImageOps.fit(image, (640, 480), Image.Resampling.LANCZOS,
                                  centering=tuple(crop))
        else:
            result = Image.new('RGB', (640, 480), (0, 0, 0))
            contained = ImageOps.contain(image, (640, 480), Image.Resampling.LANCZOS)
            result.paste(contained, ((640 - contained.width) // 2, (480 - contained.height) // 2))
        result.save(target, 'BMP')
        return result


def convert_video(source, target, *, cancel=None):
    result = probe(source, cancel=cancel)
    if not any(s.get('codec_type') == 'video' for s in result.get('streams', [])):
        raise ValueError('MV 文件没有视频流')
    try:
        duration = float(result.get('format', {}).get('duration', 0))
    except (TypeError, ValueError): duration = 0
    if duration > 6553.5: raise ValueError('MV 超过 65535 帧的硬件上限')
    run_tool('ffmpeg', ['-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', source,
                       '-map', '0:v:0', '-an', '-sn', '-dn', '-vf',
                       'fps=10,scale=320:240:force_original_aspect_ratio=increase:flags=lanczos,crop=320:240,setsar=1',
                       '-pix_fmt', 'rgb565le', '-f', 'rawvideo', '-threads', '1', target], cancel=cancel)
    size = Path(target).stat().st_size
    if not size or size % 153600 or size // 153600 > 65535:
        raise ValueError('MV 转换结果不是硬件允许的完整帧集合')
    return size // 153600


def rgb565_bytes(image):
    image = image.convert('RGB')
    pixels = image.get_flattened_data() if hasattr(image, 'get_flattened_data') else image.getdata()
    words = array.array('H', (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                              for r, g, b in pixels))
    if sys.byteorder != 'little': words.byteswap()
    return words.tobytes()


def make_backgrounds(covers, target, *, cancel=None):
    """Six stored frames: four cover backgrounds, then two artwork backgrounds."""
    with Path(target).open('xb') as output:
        for i in range(6):
            check_cancel(cancel)
            if i < 4:
                image = ImageEnhance.Brightness(covers[min(i, len(covers) - 1)].filter(
                    ImageFilter.GaussianBlur(20))).enhance(0.38)
            else:
                image = Image.new('RGB', (640, 480))
                pixels = image.load()
                for y in range(480):
                    for x in range(640):
                        distance = min(180, (abs(x - 176) + abs(y - 168)) // 5)
                        pixels[x, y] = ((32 + distance // 5, 24 + distance // 8, 44 + distance // 3)
                                        if i == 4 else (20 + distance // 6, 44 + distance // 3, 42 + distance // 5))
            output.write(rgb565_bytes(image))
