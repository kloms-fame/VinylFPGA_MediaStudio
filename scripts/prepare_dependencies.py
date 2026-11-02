"""Record bundled dependencies and copy their original license notices. No downloads."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]


def prepare():
    notices=ROOT/'resources/licenses';notices.mkdir(parents=True,exist_ok=True)
    python_root=Path(sys.base_prefix)
    python_license=next((p for p in (python_root/'LICENSE_PYTHON.txt',python_root/'LICENSE.txt') if p.is_file()),None)
    if python_license is None:raise FileNotFoundError('Python original license notice missing')
    shutil.copyfile(python_license,notices/'Python-LICENSE.txt')
    from importlib.metadata import distribution
    pillow=distribution('Pillow')
    shutil.copyfile(pillow.locate_file('pillow-13.0.0.dist-info/licenses/LICENSE'),notices/'Pillow-LICENSE.txt')
    components=[('resources/bin/ffmpeg.exe','8.0.1 essentials','https://www.gyan.dev/ffmpeg/builds/'),
                ('resources/bin/ffprobe.exe','8.0.1 essentials','https://www.gyan.dev/ffmpeg/builds/'),
                ('resources/fonts/NotoSansCJKsc-Regular.otf','Noto CJK SC Regular','https://github.com/notofonts/noto-cjk/tree/main/Sans/OTF/SimplifiedChinese')]
    records=[]
    for name,version,source in components:
        path=ROOT/name
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        records.append(dict(path=name,version=version,source=source,bytes=path.stat().st_size,sha256=digest))
    (ROOT/'resources/dependencies.json').write_text(json.dumps(dict(python=sys.version,pillow=pillow.version,files=records),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':prepare()
