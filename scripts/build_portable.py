"""Build and hash the Windows folder distribution; Unicode paths stay in Python."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def refresh_docs():
    bundle=ROOT/'dist/VinylFPGA_MediaStudio'
    for relative in ('使用说明.md','验收报告.md','packaging/第三方组件说明.md','packaging/便携版开始使用.md'):
        shutil.copyfile(ROOT/relative,bundle/Path(relative).name)
    files=[]
    for path in sorted(bundle.rglob('*')):
        if path.is_file() and path.name!='bundle.sha256.json':
            with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            files.append(dict(path=path.relative_to(bundle).as_posix(),bytes=path.stat().st_size,sha256=digest))
    (bundle/'bundle.sha256.json').write_text(json.dumps(files,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Portable bundle ready:',bundle,flush=True)


def build():
    for relative in ('resources/bin/ffmpeg.exe','resources/bin/ffprobe.exe',
                     'resources/fonts/NotoSansCJKsc-Regular.otf','resources/fonts/OFL.txt',
                     'resources/licenses/Python-LICENSE.txt','resources/licenses/Pillow-LICENSE.txt'):
        if not (ROOT/relative).is_file():raise FileNotFoundError('Missing bundled dependency: '+relative)
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','packaging/MediaStudio.spec'],cwd=ROOT,check=True)
    refresh_docs()


if __name__=='__main__':
    if sys.argv[1:]==['--docs-only']:refresh_docs()
    else:build()
