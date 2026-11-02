"""Save fresh application, browser, and pixel-comparator verification evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
output=ROOT/'evidence/checks';output.mkdir(parents=True,exist_ok=True)
commands=[('unittest',[sys.executable,'-m','unittest','discover','-s','tests','-p','test_*.py','-v']),
          ('frontend',[sys.executable,'scripts/test_frontend.py','-v']),
          ('pixel_compare',['node','scripts/test_pixel_compare.js'])]
results=[]
for name,command in commands:
    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
    log=result.stdout+result.stderr
    (output/(name+'.log')).write_text(log,encoding='utf-8')
    print(name,'PASS' if result.returncode==0 else 'FAIL',log[-800:],flush=True)
    results.append(dict(name=name,command=command,returncode=result.returncode))
files={}
for folder in ('src','web','tests','vendor','profiles'):
    for path in sorted((ROOT/folder).rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
            files[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
(output/'results.json').write_text(json.dumps(dict(passed=all(x['returncode']==0 for x in results),checks=results,source_sha256=files),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if any(x['returncode'] for x in results):raise SystemExit(1)
