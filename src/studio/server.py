"""Offline loopback UI. No media or filesystem operation is exposed to the LAN."""
import copy
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import shutil
import struct
import threading
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from .jobs import Jobs
from .devices import bundle_root

VERSION = '1.0.0'
UPLOAD_EXTENSIONS = {'.mp3','.flac','.wav','.aac','.m4a','.ogg','.opus','.pcm',
                     '.mp4','.mkv','.mov','.webm','.avi','.jpg','.jpeg','.png','.bmp',
                     '.webp','.lrc','.srt','.txt'}


def fixed_display_state(raw):
    state=json.loads(raw)
    if not isinstance(state,dict):
        raise ValueError('固定显示状态需要 JSON 对象')
    limits={'lyric_center':(0,127),'scroll_offset':(0,63),'wave_peak':(0,255),
            'progress_fill':(0,320),'end_mode':(0,2)}
    booleans={'scroll_active','highlight_valid','wave_enable'}
    for key,value in state.items():
        if key in limits:
            low,high=limits[key]
            if type(value) is not int or not low<=value<=high:
                raise ValueError(f'{key} 需要 {low}–{high} 的整数')
        elif key in booleans:
            if type(value) is not bool:raise ValueError(f'{key} 需要 true 或 false')
        elif key=='wave_samples':
            if not isinstance(value,list) or len(value)!=32 or any(type(x) is not int or not 0<=x<=255 for x in value):
                raise ValueError('wave_samples 需要 32 项 0–255 的整数')
        else:
            raise ValueError(f'不支持的显示状态字段：{key}')
    return state


def relative_file(root, value):
    root = Path(root).resolve()
    value = str(value)
    if not value or '\x00' in value or Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or ':' in value:
        raise ValueError('无效的项目文件路径')
    candidate = (root/value).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError('文件路径不能超出当前项目目录')
    return candidate


def atomic_json(path, document):
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def empty_track():
    return dict(title='',artist='',audio='',cover='',mv='',lyrics='',
                lyric_offset_ms=0,crop=[.5,.5],cover_fit='cover')


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, workspace, port=0):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.jobs = Jobs()
        self.preview_cache = {}
        self.preview_lock = threading.Lock()
        self.project_lock = threading.Lock()
        super().__init__(('127.0.0.1', port), Handler)

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.server_port}'

    def project_dir(self, key):
        if not re.fullmatch('[a-f0-9]{32}', str(key)):
            raise ValueError('无效项目编号')
        path = self.workspace/key
        if not (path/'project.json').is_file():
            raise ValueError('项目不存在')
        return path

    def release_dir(self, project, key):
        if not re.fullmatch('[a-f0-9]{32}', str(key)):
            raise ValueError('无效输出版本编号')
        path = self.project_dir(project)/'outputs'/key
        if not (path/'release.json').is_file():
            raise ValueError('该输出尚未成功完成')
        return path

    def projects(self):
        result = []
        for path in self.workspace.glob('*/project.json'):
            if re.fullmatch('[a-f0-9]{32}', path.parent.name):
                try:
                    result.append(dict(id=path.parent.name, name=json.loads(path.read_text(encoding='utf-8'))['name']))
                except (ValueError, KeyError, OSError):
                    continue
        return result

    def releases(self, project):
        result = []
        from .project import validate_project
        current=validate_project(json.loads((self.project_dir(project)/'project.json').read_text(encoding='utf-8')))
        for path in sorted((self.project_dir(project)/'outputs').glob('*/release.json'), key=lambda p:p.stat().st_mtime, reverse=True):
            snapshot=path.parent/'project_snapshot.json'
            matches=False
            if snapshot.is_file():
                matches=validate_project(json.loads(snapshot.read_text(encoding='utf-8')))==current
            result.append(dict(id=path.parent.name, release=json.loads(path.read_text(encoding='utf-8')),project_matches=matches))
        return result

    def preview(self, project, release):
        path = self.release_dir(project, release)/'release.json'
        key = str(path)
        with self.preview_lock:
            if key not in self.preview_cache:
                from .preview import Preview
                if len(self.preview_cache) >= 4:
                    self.preview_cache.pop(next(iter(self.preview_cache)))
                self.preview_cache[key] = Preview(path)
            return self.preview_cache[key]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def send_data(self, code, data, content_type='application/json; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def json(self, document, code=200):
        self.send_data(code, json.dumps(document, ensure_ascii=False).encode('utf-8'))

    def read_json(self):
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            raise ValueError('此操作需要 JSON 请求')
        size = int(self.headers.get('Content-Length',0))
        if size < 0 or size > 2*1024*1024:
            raise ValueError('项目配置过大')
        return json.loads(self.rfile.read(size) or b'{}')

    def authorized(self, query):
        host = self.headers.get('Host','')
        if host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.json(dict(error='无效 Host'), 400)
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in (self.server.base_url, f'http://localhost:{self.server.server_port}'):
            self.json(dict(error='拒绝其他网页发起的请求'), 403)
            return False
        if self.path.startswith('/api/'):
            supplied = self.headers.get('X-Studio-Token')
            if self.command == 'GET':
                supplied = supplied or query.get('token',[''])[0]
            if not supplied or not secrets.compare_digest(supplied, self.server.token):
                self.json(dict(error='会话已过期，请从应用重新打开界面'), 403)
                return False
        return True

    def dispatch(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if not self.authorized(query):
            self.close_connection = True
            return
        pieces = [unquote(x) for x in url.path.strip('/').split('/') if x]
        if self.command == 'GET' and (not pieces or pieces[0] != 'api'):
            return self.static(url.path)
        if pieces == ['api','state'] and self.command == 'GET':
            profile = json.loads((bundle_root()/'profiles/frozen/profile.json').read_text(encoding='utf-8'))
            return self.json(dict(version=VERSION,projects=self.server.projects(),jobs=self.server.jobs.all(), profile=profile))
        if pieces==['api','decode-png'] and self.command=='POST':
            from PIL import Image
            size=int(self.headers.get('Content-Length',-1))
            if not 0<size<=20*1024*1024:
                raise ValueError('PNG 文件大小无效或超过 20 MiB')
            if self.headers.get('Content-Type','').split(';')[0]!='application/octet-stream':
                raise ValueError('PNG 对照需要二进制请求')
            raw=self.rfile.read(size)
            if len(raw)!=size or raw[:8]!=b'\x89PNG\r\n\x1a\n' or len(raw)<33 or raw[12:16]!=b'IHDR':
                raise ValueError('请导入有效的原始 PNG')
            if raw[24]!=8 or raw[25] not in (2,6):
                raise ValueError('实板帧需要 8-bit RGB 或 RGBA PNG；不自动转换位深或调色板')
            with Image.open(io.BytesIO(raw)) as frame:
                if frame.size!=(640,480):raise ValueError('实板帧必须是原生 640×480，不会自动缩放')
                # Read stored samples without applying ICC or gamma transforms.
                rgba=frame.convert('RGBA')
                if rgba.getchannel('A').getextrema()!=(255,255):
                    raise ValueError('请使用不透明的原始 HDMI PNG 截图')
                return self.send_data(200,rgba.tobytes(),'application/octet-stream')
        if pieces == ['api','shutdown'] and self.command == 'POST':
            self.read_json()
            if any(job['status']=='running' for job in self.server.jobs.all()):
                raise ValueError('任务仍在运行，请等待完成后退出工具')
            self.json(dict(ok=True),202)
            threading.Thread(target=self.server.shutdown,daemon=True).start()
            return
        if pieces == ['api','projects'] and self.command == 'POST':
            data = self.read_json()
            name = str(data.get('name','我的唱片')).strip()[:100] or '我的唱片'
            key = uuid.uuid4().hex
            root = self.server.workspace/key
            (root/'inputs').mkdir(parents=True)
            project = dict(schema=1,name=name,mode='frozen',tracks=[empty_track()],image_size_mib=0)
            atomic_json(root/'project.json',project)
            return self.json(dict(id=key,project=project),201)
        if len(pieces) >= 3 and pieces[:2] == ['api','projects']:
            return self.project_route(pieces[2],pieces[3:],query)
        if len(pieces) >= 3 and pieces[:2] == ['api','jobs']:
            if len(pieces)==3 and self.command=='GET':
                return self.json(self.server.jobs.get(pieces[2]))
            if pieces[3:]==['cancel'] and self.command=='POST':
                self.read_json(); self.server.jobs.cancel(pieces[2])
                return self.json(dict(ok=True))
        if pieces == ['api','disks'] and self.command=='GET':
            from .devices import writer
            return self.json(dict(disks=writer.enumerate_disks()))
        if len(pieces)==3 and pieces[:2]==['api','disks'] and self.command=='POST':
            return self.disk_route(pieces[2],self.read_json())
        if pieces == ['api','open-output'] and self.command=='POST':
            data = self.read_json()
            root = self.server.release_dir(data['project'],data['release'])
            if os.name=='nt':
                os.startfile(root)
            return self.json(dict(ok=True))
        self.json(dict(error='接口不存在'),404)

    def static(self,path):
        name = 'index.html' if path=='/' else path.lstrip('/')
        target = relative_file(bundle_root()/'web',unquote(name))
        if not target.is_file():
            return self.json(dict(error='页面不存在'),404)
        data = target.read_bytes()
        if name=='index.html':
            data=data.replace(b'__STUDIO_TOKEN__',self.server.token.encode())
        return self.send_data(200,data,mimetypes.guess_type(name)[0] or 'application/octet-stream')

    def project_route(self,key,tail,query):
        root=self.server.project_dir(key)
        if not tail and self.command=='GET':
            return self.json(dict(id=key,project=json.loads((root/'project.json').read_text(encoding='utf-8')),releases=self.server.releases(key)))
        if not tail and self.command=='PUT':
            from .project import save_project
            with self.server.project_lock:
                project=save_project(root,self.read_json())
            return self.json(dict(id=key,project=project))
        if tail==['upload'] and self.command=='POST':
            name=query.get('name',[''])[0]
            extension=Path(name).suffix.lower()
            if extension not in UPLOAD_EXTENSIONS:
                raise ValueError('不支持该素材类型；请选择音频、封面、视频、LRC 或 SRT')
            if self.headers.get('Content-Type','').split(';')[0]!='application/octet-stream':
                raise ValueError('素材上传需要二进制文件')
            size=int(self.headers.get('Content-Length',-1))
            if size<0 or size>8*1024**3:
                raise ValueError('文件大小无效或超过 8 GiB')
            relative='inputs/'+uuid.uuid4().hex+extension
            target=relative_file(root,relative)
            remaining=size
            try:
                with target.open('xb') as stream:
                    while remaining:
                        data=self.rfile.read(min(1024*1024,remaining))
                        if not data:
                            raise ValueError('素材上传中断')
                        stream.write(data); remaining-=len(data)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return self.json(dict(path=relative,name=name,bytes=size),201)
        if tail==['asset'] and self.command=='GET':
            target=relative_file(root,query.get('path',[''])[0])
            if not target.is_relative_to((root/'inputs').resolve()):
                raise ValueError('只允许预览已导入的素材')
            return self.stream_file(target)
        if tail==['build'] and self.command=='POST':
            self.read_json()
            release=uuid.uuid4().hex
            output=root/'outputs'/release
            def build(progress,cancel):
                from .build import build_project
                result=build_project(root,output,progress=progress,cancel=cancel)
                return dict(release=release,manifest=result)
            job=self.server.jobs.start(build,kind='build',project=key)
            return self.json(dict(job=job),202)
        if tail==['analyze'] and self.command=='POST':
            from .project import validate_project
            from .lyrics import parse_lyrics
            project=validate_project(self.read_json())
            banks=[set(' -:/TRACK 0123456789ARTISTNO LYRICS') for _ in range(2)]
            rows=[]
            for slot,track in enumerate(project['tracks']):
                if 'lyric_draft' in track:
                    raise ValueError('请先将歌词草稿保存为素材，再检查拆行和字库')
                lyrics=parse_lyrics(relative_file(root,track['lyrics']),track['lyric_offset_ms']) if track['lyrics'] else []
                bank=banks[slot//2];bank.update(track['title']);bank.update(track['artist'])
                for _,text in lyrics:bank.update(text)
                rows.append(dict(slot=slot,lyrics=lyrics))
            counts=[dict(bank=i,glyphs=len(chars),capacity=256,characters=''.join(sorted(chars))) for i,chars in enumerate(banks)]
            return self.json(dict(passed=all(x['glyphs']<=256 for x in counts),tracks=rows,banks=counts,
                                  scope='候选标题、歌手与歌词；冻结模式的实际字形仍以冻结 ROM 为准'))
        if len(tail)>=2 and tail[0]=='releases':
            release=tail[1]
            output=self.server.release_dir(key,release)
            action=tail[2:]
            manifest=json.loads((output/'release.json').read_text(encoding='utf-8'))
            if action==['verify'] and self.command=='POST':
                self.read_json()
                def verify(progress,cancel):
                    from .build import verify_image
                    from .paths import sha256_file
                    progress('verify',0,'校验 FAT、索引、CRC 与完整镜像 SHA-256')
                    image=relative_file(output,manifest['image']['path'])
                    if sha256_file(image,cancel=cancel)!=manifest['image']['sha256']:
                        raise ValueError('镜像 SHA-256 与构建清单不一致')
                    return verify_image(image,cancel=cancel)
                job=self.server.jobs.start(verify,kind='verify',project=key)
                return self.json(dict(job=job),202)
            if action==['frame'] and self.command=='GET':
                preview=self.server.preview(key,release)
                params={name:int(query.get(name,[default])[0]) for name,default in
                        [('track',0),('time_ms',0),('background',0),('brightness',3),('contrast',2)]}
                params.update(page=query.get('page',['play'])[0],visual=query.get('visual',['mv'])[0])
                params.update(fixed_display_state(query.get('state',['{}'])[0]))
                with self.server.preview_lock:
                    frame=preview.frame(**params)
                data=io.BytesIO(); frame.save(data,format='PNG')
                return self.send_data(200,data.getvalue(),'image/png')
            if action==['audio'] and self.command=='GET':
                track=int(query.get('track',[0])[0])
                preview=self.server.preview(key,release)
                info=preview.audio_info(track)
                image=relative_file(output,manifest['image']['path'])
                size=int(info['bytes'])
                header=struct.pack('<4sI4s4sIHHIIHH4sI',b'RIFF',36+size,b'WAVE',b'fmt ',16,1,1,48000,96000,2,16,b'data',size)
                return self.stream_file(image,prefix=header,offset=int(info['lba'])*512,length=size,content_type='audio/wav')
            if action==['file'] and self.command=='GET':
                return self.stream_file(relative_file(output,manifest['image']['path']),content_type='application/octet-stream')
        raise ValueError('未知项目操作')

    def disk_route(self,action,data):
        output=self.server.release_dir(data.get('project'),data.get('release'))
        manifest=json.loads((output/'release.json').read_text(encoding='utf-8'))
        image=relative_file(output,manifest['image']['path'])
        if action=='simulate':
            def simulate(progress,cancel):
                from .devices import writer
                progress('simulate',0,'写入新的模拟文件，再完整回读；不访问物理磁盘')
                return writer.simulate(image,output/('模拟写卡_'+uuid.uuid4().hex+'.img'),
                                       manifest['image']['sha256'],write=True)
            job=self.server.jobs.start(simulate,kind='simulate',project=data['project'])
            return self.json(dict(job=job),202)
        disk=data.get('disk',{})
        if not isinstance(disk.get('Number'),int):
            raise ValueError('尚未选择磁盘')
        if action=='diagnose':
            def work(progress,cancel):
                from .devices import diagnose
                progress('diagnose',0,'只读检查镜像及目标 SD 卡')
                return diagnose(image,manifest['image']['sha256'],disk)
            kind='diagnose'
        elif action=='write':
            confirm=data.get('confirm','')
            if confirm!=f"ERASE {disk['Number']} {disk.get('Size')}":
                raise ValueError('磁盘确认文字不匹配')
            def work(progress,cancel):
                from .devices import elevated_write
                return elevated_write(image,manifest['image']['sha256'],disk,confirm,
                                      self.server.workspace/'_write_requests',progress)
            kind='physical-write'
        else:
            raise ValueError('未知磁盘操作')
        job=self.server.jobs.start(work,kind=kind,project=data['project'])
        return self.json(dict(job=job),202)

    def stream_file(self,path,prefix=b'',offset=0,length=None,content_type=None):
        path=Path(path)
        length=path.stat().st_size-offset if length is None else length
        total=len(prefix)+length
        start,end=0,total-1
        requested=self.headers.get('Range')
        if requested:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
            if not match or not any(match.groups()):
                return self.send_data(416,b'',extra={'Content-Range':f'bytes */{total}'})
            if match[1]:
                start=int(match[1]); end=min(int(match[2]) if match[2] else total-1,total-1)
            else:
                start=max(0,total-int(match[2]))
            if start>end or start>=total:
                return self.send_data(416,b'',extra={'Content-Range':f'bytes */{total}'})
        self.send_response(206 if requested else 200)
        self.send_header('Content-Type',content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cross-Origin-Resource-Policy','same-origin')
        if requested:
            self.send_header('Content-Range',f'bytes {start}-{end}/{total}')
        self.end_headers()
        if start<len(prefix):
            self.wfile.write(prefix[start:min(end+1,len(prefix))])
        remaining=end-max(start,len(prefix))+1
        if remaining>0:
            with path.open('rb') as stream:
                stream.seek(offset+max(0,start-len(prefix)))
                while remaining:
                    chunk=stream.read(min(1024*1024,remaining))
                    if not chunk:
                        raise OSError('媒体数据意外截断')
                    self.wfile.write(chunk); remaining-=len(chunk)

    def _handle(self):
        try:
            self.dispatch()
        except (BrokenPipeError,ConnectionResetError):
            self.close_connection=True
        except Exception as exc:
            self.close_connection=True
            self.json(dict(error=str(exc)),400)

    do_GET=_handle
    do_POST=_handle
    do_PUT=_handle
