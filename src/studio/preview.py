"""Image-backed 640×480 pixel model with ROM glyphs and explicit display state.

The pixel selector is validated against actual RTL. The default waveform is a
PCM timeline model, not a claim about SD/SDRAM service history or HDMI timing.
"""
import bisect
import io
import json
from pathlib import Path
import struct
from PIL import Image, ImageDraw

from .paths import safe_relative, sha256_file, vendor_module
from .rom import Rom

SCOPE = ('预览使用最终镜像与配套 ROM；歌词/波形采用时间轴状态模型。'
         '固定像素状态经 RTL 校对，硬件缓存延迟、启动瞬态和显示器处理以实板为准。')


def adjust_channel(pixel, brightness=3, contrast=2):
    bright=[pixel>>2,pixel>>1,(pixel*3)>>2,pixel,min(255,(pixel*5)>>2)][brightness]
    delta=bright-128
    value=[delta>>1,(delta*3)>>2,delta,(delta*5)>>2,(delta*3)>>1][contrast]+128
    return max(0,min(255,value))


def decode565(data,size):
    if len(data)!=size[0]*size[1]*2:
        raise ValueError('RGB565 帧长度错误')
    out=bytearray(len(data)//2*3)
    for i,(value,) in enumerate(struct.iter_unpack('<H',data)):
        r=(value>>11)&31;g=(value>>5)&63;b=value&31
        out[i*3:i*3+3]=bytes(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)))
    return Image.frombytes('RGB',size,bytes(out))


def _inside(root,value):
    path=(root/safe_relative(value)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('预览路径超出当前输出版本')
    return path


class Preview:
    scope=SCOPE

    def __init__(self,release_path):
        release_path=Path(release_path).resolve()
        self.root=release_path.parent
        self.release=json.loads(release_path.read_text(encoding='utf-8'))
        self.image_path=_inside(self.root,self.release['image']['path'])
        if self.image_path.stat().st_size!=self.release['image']['bytes'] or sha256_file(self.image_path)!=self.release['image']['sha256']:
            raise ValueError('镜像身份/哈希与构建清单不符，请重新校验')
        self._image_signature=self._signature()
        rom_root=_inside(self.root,self.release['rom']['path'])
        for name,digest in self.release['rom']['files_sha256'].items():
            if sha256_file(_inside(rom_root,name))!=digest:
                raise ValueError('ROM 哈希与构建清单不符：'+name)
        self.rom=Rom(rom_root)
        vp=vendor_module('vinyl_packer')
        with vp.Fat32Image(self.image_path) as volume:
            entry=volume.find_root_file('INDEX.BIN')
            index_blob=volume.read_file(entry.first_cluster,entry.size)
            self.index=vp.parse_index(index_blob)
            self.cover=[]
            for song in self.index['songs']:
                raw=volume.read_file(song['cover_cluster'],song['cover_bytes'])
                vp._check_cover(raw,'cover',640,480)
                self.cover.append(Image.open(io.BytesIO(raw)).convert('RGB'))
        self.background=None
        if index_blob[32:36]==b'BGR1':
            lba,frame_bytes,count=struct.unpack_from('<III',index_blob,36)
            if frame_bytes!=614400 or count not in (5,6):
                raise ValueError('不支持的背景帧格式')
            self.background=dict(lba=lba,frame_bytes=frame_bytes,frames=count)
        self._masks={};self._background_cache={};self._mv_cache={}

    def _signature(self):
        value=self.image_path.stat()
        return value.st_size,value.st_mtime_ns,value.st_ino

    def _read(self,lba,size,offset=0):
        if self._signature()!=self._image_signature:
            raise ValueError('镜像在预览期间发生变化，请重新打开并校验')
        start=lba*512+offset
        if start<0 or size<0 or start+size>self._image_signature[0]:
            raise ValueError('媒体读取范围超出镜像')
        with self.image_path.open('rb') as stream:
            stream.seek(start);data=stream.read(size)
        if len(data)!=size:
            raise ValueError('镜像媒体读取不完整')
        return data

    def audio_info(self,track):
        song=self._track(track)
        return dict(lba=song['audio_lba'],bytes=song['audio_bytes'],sample_rate=48000)

    def _track(self,track):
        if not isinstance(track,int) or isinstance(track,bool) or not 0<=track<len(self.index['songs']):
            raise ValueError('无效曲目槽位')
        return self.index['songs'][track]

    def base_frame(self,track=0,time_ms=0,page='play',background=0,visual='mv'):
        song=self._track(track)
        if page=='browse':
            return self.cover[track].copy(),False
        if background and self.background:
            index=track if background==1 else background+2
            index=min(index,self.background['frames']-1)
            if index not in self._background_cache:
                b=self.background
                self._background_cache[index]=decode565(self._read(b['lba'],b['frame_bytes'],index*b['frame_bytes']),(640,480))
            image=self._background_cache[index].copy();active=True
        else:
            # playback_page replaces pixels outside the media rectangle.
            image=self.cover[track].copy();active=False
        if visual=='mv' and song['mv_frames']:
            number=min(max(0,time_ms//100),song['mv_frames']-1)
            key=(track,number)
            if key not in self._mv_cache:
                if len(self._mv_cache)>4:self._mv_cache.clear()
                self._mv_cache[key]=decode565(self._read(song['mv_lba'],153600,number*153600),(320,240))
            media=self._mv_cache[key]
        else:
            # Actual raster keeps even columns of each even source row.
            original=list(self.cover[track].get_flattened_data())
            media=Image.new('RGB',(320,240))
            media.putdata([original[y*640+x] for y in range(0,480,2) for x in range(0,640,2)])
        image.paste(media,(16,48))
        return image,active

    def display_state(self,track,time_ms,**state):
        song=self._track(track);rom=self.rom.tracks[track]
        total=song['audio_bytes']//2
        elapsed=min(total,max(0,time_ms)*48)
        seconds=elapsed//48000;duration=total//48000
        fmt=lambda n:f'{min(n//60,99):02d}:{n%60:02d}'
        target=bisect.bisect_right([row['time_ms'] for row in rom['rows']],max(0,time_ms))-1
        center=state.get('lyric_center',max(0,target))
        if state.get('scroll_active') and 'lyric_center' not in state:center=max(0,target-1)
        rows=[]
        for index in range(center-2,center+4):
            rows.append(rom['rows'][index] if 0<=index<len(rom['rows']) else dict(length=0,glyphs=[]))
        wave_samples=state.get('wave_samples')
        if wave_samples is None:
            # Frame snapshot from preceding 800×525/25MHz frame, one sample in 12.
            sample=max(0,(elapsed//(800*525*48000//25000000)-1)*(800*525*48000//25000000))
            offsets=[sample+i*12 for i in range(32)]
            first=min(total,max(0,sample));last=min(total,max(offsets)+1)
            raw=self._read(song['audio_lba'],max(0,last-first)*2,first*2)
            values=struct.unpack('<'+'h'*(len(raw)//2),raw) if raw else []
            wave_samples=[abs((values[i-first]>>8)) if first<=i<last else 0 for i in offsets]
        if len(wave_samples)!=32 or any(not isinstance(x,int) or not 0<=x<=255 for x in wave_samples):
            raise ValueError('波形状态必须是 32 项 0–255 的幅值')
        return dict(track=track,rom=rom,progress_fill=state.get('progress_fill',elapsed*320//total if total else 0),
                    elapsed_time=state.get('elapsed_time',fmt(seconds)),total_time=state.get('total_time',fmt(duration)),
                    remaining_time=state.get('remaining_time','-'+fmt(max(0,duration-seconds))),
                    lyric_rows=rows,highlight_valid=state.get('highlight_valid',target>=0),
                    scroll_active=bool(state.get('scroll_active',False)),scroll_offset=int(state.get('scroll_offset',0)),
                    end_mode=int(state.get('end_mode',2)),wave_samples=list(wave_samples),
                    wave_peak=int(state.get('wave_peak',max(wave_samples))),wave_enable=bool(state.get('wave_enable',True)))

    def _mask(self,track,glyph,ascii=False,icon=False):
        key=(track>>1,glyph,ascii,icon)
        if key in self._masks:return self._masks[key]
        if ascii:
            data=bytes(self.rom.ascii[(glyph&127)*8:(glyph&127)*8+8])
            image=Image.frombytes('1',(8,8),data)
            if not icon:image=image.resize((8,16),Image.Resampling.NEAREST)
        else:
            rows=self.rom.font[(track>>1)*4096+glyph*16:(track>>1)*4096+glyph*16+16]
            image=Image.frombytes('1',(16,16),b''.join(struct.pack('>H',v) for v in rows))
        self._masks[key]=image
        return image

    def _text(self,image,track,text,x,y,color,*,ascii=False,clip=None,fallback=False):
        width=16 if fallback or not ascii else 8
        layer=Image.new('RGB',image.size);mask=Image.new('1',image.size)
        for i,glyph in enumerate(text):
            glyph=ord(glyph) if isinstance(glyph,str) else glyph
            bitmap=self._mask(track,glyph,ascii)
            if fallback:
                repeated=Image.new('1',(16,16));repeated.paste(bitmap,(0,0));repeated.paste(bitmap,(8,0));bitmap=repeated
            layer.paste(color,(x+i*width,y,x+(i+1)*width,y+16));mask.paste(bitmap,(x+i*width,y))
        if clip:
            clipped=Image.new('1',image.size);clipped.paste(mask.crop(clip),clip);mask=clipped
        image.paste(layer,(0,0),mask)

    def render_layers(self,base,display,page='play',background_active=False):
        image=base.copy();track=display['track'];rom=display['rom'];draw=ImageDraw.Draw(image)
        if page=='browse':
            bar=image.crop((32,416,608,457)).point(lambda p:p>>1)
            image.paste(bar,(32,416));draw.line((32,416,607,416),fill='#3A3528');draw.line((32,456,607,456),fill='#3A3528')
            draw.line((148,422,148,443),fill='#4A4030');draw.line((528,422,528,443),fill='#4A4030')
            for slot in range(4):
                if slot==track:draw.rectangle((288+slot*16,464,303+slot*16,466),fill='#FFD700')
                else:draw.rectangle((294+slot*16,464,297+slot*16,466),fill='#404048')
            self._text(image,track,f'TRACK 0{track+1}/04',48,424,'#00CED1',ascii=True)
            clip=(164,424,524,440)
            self._text(image,track,rom['title'],164,424,'white',clip=clip)
            x=164+rom['title_length']*16
            self._text(image,track,'-',x,424,'#A09880',ascii=True,clip=clip)
            self._text(image,track,rom['artist'],x+8,424,'#A09880',clip=clip)
            self._text(image,track,display['total_time'],544,424,'white',ascii=True)
            return image
        if not background_active:
            pixels=image.load()
            for y in range(480):
                for x in range(640):
                    if not (16<=x<336 and 48<=y<288):
                        shade=128-(abs(x-176)>>5)-(abs(y-168)>>5)
                        pixels[x,y]=(shade,shade,shade)
        draw=ImageDraw.Draw(image)
        draw.rectangle((16,342,335,345),fill='#B8BCC4')
        fill=max(0,min(320,display['progress_fill']))
        if fill:draw.rectangle((16,342,15+fill,345),fill='white')
        self._text(image,track,rom['title'] or f'TRACK 0{track+1}',16,298,'white',ascii=not rom['title'],fallback=not rom['title'])
        self._text(image,track,rom['artist'] or 'ARTIST --',16,318,'#D0D4D8',ascii=not rom['artist'],fallback=not rom['artist'])
        self._text(image,track,display['elapsed_time'],16,350,'#D0D4D8',ascii=True)
        self._text(image,track,display['remaining_time'],288,350,'#D0D4D8',ascii=True)
        bank=display['end_mode']+1
        for tile_y in range(2):
            for tile_x in range(2):
                bitmap=self._mask(track,bank*4+tile_y*2+tile_x,ascii=True,icon=True)
                image.paste('#D0D4D8',(168+tile_x*8,350+tile_y*8,176+tile_x*8,358+tile_y*8),bitmap)
        if not rom['line_count']:
            self._text(image,track,self.rom.no_lyrics[:9],356,224,'white')
        else:
            for slot,row in enumerate(display['lyric_rows']):
                y=128+slot*48-display['scroll_offset']
                color='#FFD700' if display['highlight_valid'] and slot==(3 if display['scroll_active'] else 2) else '#B0B4BC'
                self._text(image,track,row['glyphs'],356,y,color,clip=(356,48,612,360))
        if display['wave_enable']:self._wave(image,display)
        return image

    def _wave(self,image,display):
        draw=ImageDraw.Draw(image)
        colors=['#ff3040','#ff8a20','#ffe040','#38e060','#18d8c0','#20a0ff','#7060ff','#ff50d0']
        draw.rectangle((64,470,575,471),fill='#406060')
        for i,value in enumerate(display['wave_samples']):
            height=max(1,min(95,value));x=66+i*16
            draw.rectangle((x,471-height,x+11,471),fill=colors[i//4]);draw.line((x,471-height,x+11,471-height),fill='#fff8d0')
        height=max(1,min(95,display['wave_peak']))
        for row in range(height+1):
            color='#ffffff' if row>height-(height>>2) else '#ffb020' if row>(height>>1) else '#00d8ff'
            draw.line((584,471-row,595,471-row),fill=color)

    def frame(self,track=0,time_ms=0,page='play',background=0,visual='mv',brightness=3,contrast=2,**state):
        if page not in ('play','browse') or visual not in ('mv','cover') or background not in range(4):
            raise ValueError('无效预览页面/图层状态')
        if brightness not in range(5) or contrast not in range(5):
            raise ValueError('亮度和对比度必须为 0–4')
        if not isinstance(time_ms,int) or time_ms<0:
            raise ValueError('预览时间必须是非负整数毫秒')
        base,active=self.base_frame(track,time_ms,page,background,visual)
        display=self.display_state(track,time_ms,**state)
        image=self.render_layers(base,display,page,active)
        table=[adjust_channel(i,brightness,contrast) for i in range(256)]
        return image.point(table*3)
