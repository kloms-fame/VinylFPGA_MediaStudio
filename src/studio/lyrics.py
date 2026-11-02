"""LRC/SRT import with explicit splitting and current ROM capacity checks."""
import html
import re
from decimal import Decimal
from pathlib import Path

MAX_TIME_MS = 262143


def _chunks(text):
    result = []
    while len(text) > 16:
        cut = text.rfind(' ', 0, 17)
        if cut < 5: cut = 16
        result.append(text[:cut].strip())
        text = text[cut:].strip()
    if text: result.append(text)
    return result


def _stamp(minutes, seconds):
    seconds = Decimal(seconds.replace(',', '.'))
    if not 0 <= seconds < 60:
        raise ValueError('歌词秒数需小于 60')
    return int((int(minutes) * 60 + seconds) * 1000)


def parse_lyrics(path, offset_ms=0):
    path = Path(path)
    if not isinstance(offset_ms, int): raise ValueError('歌词偏移必须是整数毫秒')
    data = path.read_text(encoding='utf-8-sig')
    cues = []
    if path.suffix.lower() == '.srt' or '-->' in data:
        for block in re.split(r'\n\s*\n', data.replace('\r\n', '\n').strip()):
            match = re.search(r'(\d+):(\d+):(\d+[,.]\d+)\s*-->\s*(\d+):(\d+):(\d+[,.]\d+)', block)
            if not match:
                if block.strip(): raise ValueError('SRT 时间格式无效')
                continue
            h1,m1,s1,h2,m2,s2 = match.groups()
            start = int(h1) * 3600000 + _stamp(m1, s1)
            end = int(h2) * 3600000 + _stamp(m2, s2)
            text = html.unescape(re.sub(r'<[^>]*>', '', block[match.end():])).replace('\n', ' ')
            text = re.sub(r'[♪♫]', '', text).strip()
            if end <= start: raise ValueError('SRT 结束时间必须晚于开始时间')
            if text: cues.append((start, end, text))
    else:
        tags = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]')
        offset_match = re.search(r'\[offset:([+-]?\d+)\]', data, re.I)
        if offset_match: offset_ms += int(offset_match[1])
        raw = []
        for line in data.splitlines():
            matches = list(tags.finditer(line))
            if not matches: continue
            text = tags.sub('', line).strip()
            if not text: continue
            for match in matches: raw.append((_stamp(*match.groups()), text))
        raw.sort(key=lambda x: x[0])
        for i, (start, text) in enumerate(raw):
            end = raw[i + 1][0] if i + 1 < len(raw) else start + max(1000, len(text) * 50)
            cues.append((start, end, text))
    if not cues: raise ValueError('未找到有效歌词行；无歌词曲目请留空歌词文件')
    rows = []
    for start, end, text in sorted(cues):
        chunks = _chunks(text)
        total = max(1, sum(len(t) for t in chunks))
        done = 0
        if end - start < len(chunks): raise ValueError('字幕区间太短，无法拆分到不同毫秒')
        for chunk in chunks:
            stamp = start + (end - start) * done // total + offset_ms
            rows.append((stamp, chunk)); done += len(chunk)
    validate_rows(rows)
    return rows


def validate_rows(rows):
    if len(rows) > 128: raise ValueError('单曲歌词超过 128 行，请合并或删减后重试')
    previous = -1
    for stamp, text in rows:
        if not isinstance(stamp, int) or not 0 <= stamp <= MAX_TIME_MS:
            raise ValueError('歌词时间必须在 0–262143 ms（当前 18-bit ROM 范围）')
        if stamp <= previous: raise ValueError('歌词时间重复或未严格递增')
        if not isinstance(text, str) or not 1 <= len(text) <= 16:
            raise ValueError('每条显示歌词需为 1–16 个字形')
        previous = stamp
    return rows
