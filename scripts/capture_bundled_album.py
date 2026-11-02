"""Exercise the shipped EXE against the complete existing album, then shut it down."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
ready=ROOT/'test-output'/('capture-'+uuid.uuid4().hex+'.json')
process=subprocess.Popen([str(ROOT/'dist/VinylFPGA_MediaStudio/VinylFPGA_MediaStudio.exe'),
                          '--no-browser','--workspace',str(ROOT/'projects'),'--ready-file',str(ready)],cwd=ROOT)
address=None
try:
    deadline=time.monotonic()+30
    while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.1)
    if not ready.is_file():raise RuntimeError('Portable EXE did not become ready')
    address=json.loads(ready.read_text())['url']
    album=json.loads((ROOT/'evidence/full_album.json').read_text(encoding='utf-8'))
    subprocess.run([sys.executable,str(ROOT/'scripts/capture_ui.py'),'--url',address,'--project',album['project_id'],
                    '--output',str(ROOT/'evidence/screenshots')],check=True,cwd=ROOT)
finally:
    if address and process.poll() is None:
        html=urllib.request.urlopen(address).read().decode()
        token=re.search(r'window.STUDIO_TOKEN="([^"]+)"',html)[1]
        request=urllib.request.Request(address+'/api/shutdown',data=b'{}',method='POST',
                                       headers={'X-Studio-Token':token,'Content-Type':'application/json'})
        urllib.request.urlopen(request).close()
        process.wait(timeout=10)
    if process.poll() is None:process.terminate();process.wait(timeout=5)
