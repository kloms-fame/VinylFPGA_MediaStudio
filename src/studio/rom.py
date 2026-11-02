"""Decode the actual synthesized-ROM source, not replacement system fonts."""
from pathlib import Path
import re


def _array(source, name, size, bits):
    result = [0]*size
    values = re.findall(r'\b'+re.escape(name)+r'\[(\d+)\]\s*=\s*\d+\s*\x27h([0-9a-fA-F]+)', source)
    if not values:
        raise ValueError('ROM 数组缺失：'+name)
    for index, value in values:
        index=int(index)
        if index >= size:
            raise ValueError('ROM 地址超出已支持范围')
        result[index]=int(value,16)&((1<<bits)-1)
    return result


def _scalar(source,name):
    match=re.search(r'\b'+re.escape(name)+r'\s*=\s*\d+\s*\x27([dh])([0-9a-fA-F]+)',source)
    if not match:
        raise ValueError('ROM 元数据缺失：'+name)
    return int(match[2],16 if match[1]=='h' else 10)


def glyphs(value,length=16):
    return [(value>>(8*i))&255 for i in range(length)]


class Rom:
    def __init__(self,root):
        root=Path(root)
        font=(root/'song_font_rom.v').read_text(encoding='utf-8')
        ascii_source=(root/'ascii_font_rom.v').read_text(encoding='utf-8')
        text=(root/'song_text_rom.v').read_text(encoding='utf-8')
        self.font=_array(font,'row_mem',8192,16)
        self.ascii=_array(ascii_source,'row_mem',1024,8)
        depth_match=re.search(r'time_length_mem\s*\[0:(\d+)\]',text)
        if not depth_match:
            raise ValueError('不支持的歌词 ROM 结构')
        depth=int(depth_match[1])+1
        times=_array(text,'time_length_mem',depth,23)
        chunks=[_array(text,f'glyph_mem_{i}',depth,32) for i in range(4)]
        self.no_lyrics=glyphs(_scalar(text,'no_lyrics_glyphs'))
        self.tracks=[]
        for slot in range(4):
            match=re.search(r"2'd"+str(slot)+r':\s*begin(.*?)\bend\b',text,re.S)
            if not match:
                raise ValueError('ROM 缺少固定曲目槽位')
            block=match[1]
            item={name:_scalar(block,name) for name in
                  ('line_count','line_base','title_length','artist_length','title_glyphs','artist_glyphs')}
            item['title']=glyphs(item['title_glyphs'],item['title_length'])
            item['artist']=glyphs(item['artist_glyphs'],item['artist_length'])
            rows=[]
            for index in range(item['line_base'],item['line_base']+item['line_count']):
                if index>=depth:
                    raise ValueError('歌词 ROM 行范围无效')
                packed=sum(chunks[i][index]<<(32*i) for i in range(4))
                rows.append(dict(time_ms=times[index]>>5,length=times[index]&31,glyphs=glyphs(packed,times[index]&31)))
            item['rows']=rows
            self.tracks.append(item)
