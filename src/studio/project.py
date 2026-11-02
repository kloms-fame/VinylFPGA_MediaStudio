"""Editable project schema; strict media requirements are deferred to build."""
import copy
import json
import math
import os
import uuid
from pathlib import Path
from .paths import safe_relative


def validate_project(data, *, for_build=False):
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ValueError('不支持的素材项目版本')
    result = copy.deepcopy(data)
    name = result.get('name', '')
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('项目名称需为 1–100 个字符')
    result['name'] = name.strip()
    if result.get('mode') not in ('frozen', 'custom'):
        raise ValueError('固件模式需为 frozen 或 custom')
    tracks = result.get('tracks')
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= 4:
        raise ValueError('当前 FPGA 支持 1–4 首曲目')
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            raise ValueError('曲目数据不完整')
        for field in ('title', 'artist'):
            value = track.setdefault(field, '')
            if not isinstance(value, str) or len(value) > 16:
                raise ValueError(f'第 {index + 1} 曲{field}最多 16 个字形')
        for field in ('audio', 'cover', 'mv', 'lyrics'):
            track[field] = safe_relative(track.get(field, '') or '')
        if for_build and (not (track['audio'] or track['mv']) or not track['cover']):
            raise ValueError(f'第 {index + 1} 曲需要音频（或含音轨视频）和专辑封面')
        crop = track.setdefault('crop', [0.5, 0.5])
        if (not isinstance(crop, list) or len(crop) != 2 or
                any(not isinstance(x, (int, float)) or isinstance(x, bool) or
                    not math.isfinite(x) or not 0 <= x <= 1 for x in crop)):
            raise ValueError('封面裁剪中心必须是 0–1 的两个数值')
        if track.setdefault('cover_fit', 'cover') not in ('cover', 'contain'):
            raise ValueError('封面填充模式无效')
        offset = track.setdefault('lyric_offset_ms', 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or abs(offset) > 262143:
            raise ValueError('歌词偏移必须是范围内的整数毫秒')
    size = result.setdefault('image_size_mib', 0)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > 32768:
        raise ValueError('镜像容量必须为 0（自动）或不超过 32768 MiB 的整数')
    return result


def load_project(project_dir):
    path = Path(project_dir) / 'project.json'
    return validate_project(json.loads(path.read_text(encoding='utf-8-sig')))


def save_project(project_dir, data):
    result = validate_project(data)
    root = Path(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    temp = root / ('.project-' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temp, root / 'project.json')
    finally:
        if temp.exists(): temp.unlink()
    return result
