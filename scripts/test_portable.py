"""Relocated EXE end-to-end acceptance with system Python/FFmpeg absent from PATH."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def run(destination):
    source=ROOT/'dist/VinylFPGA_MediaStudio'
    destination=destination.resolve()
    if destination.exists():raise ValueError('便携测试目的目录已存在，请选择新目录')
    shutil.copytree(source,destination)
    ready=destination/'测试状态'/'ready.json'
    env=os.environ.copy()
    env['PATH']=os.pathsep.join([str(Path(env.get('WINDIR','C:/Windows'))/'System32'),env.get('WINDIR','C:/Windows')])
    env.pop('PYTHONHOME',None);env.pop('PYTHONPATH',None)
    process=subprocess.Popen([str(destination/'VinylFPGA_MediaStudio.exe'),'--no-browser','--ready-file',str(ready)],
                             cwd=destination,env=env)
    try:
        deadline=time.monotonic()+25
        while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.1)
        if not ready.exists():raise RuntimeError('移位后的 EXE 未启动；退出码 '+str(process.poll()))
        address=json.loads(ready.read_text())['url']
        html=urllib.request.urlopen(address).read().decode()
        token=re.search(r'window.STUDIO_TOKEN="([^"]+)"',html)[1]
        def api(path,method='GET',data=None,binary=None,headers=None):
            body=binary if binary is not None else json.dumps(data).encode() if data is not None else None
            options={'X-Studio-Token':token,'Content-Type':'application/octet-stream' if binary is not None else 'application/json'}
            options.update(headers or {})
            request=urllib.request.Request(address+path,data=body,method=method,headers=options)
            with urllib.request.urlopen(request,timeout=120) as response:
                payload=response.read()
                if response.headers.get_content_type()=='application/json':return json.loads(payload)
                return response.status,payload,response.headers
        def job(key):
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                item=api('/api/jobs/'+key)
                if item['status']!='running':
                    if item['status']!='done':raise AssertionError(item)
                    return item
                time.sleep(.2)
            raise TimeoutError(key)
        state=api('/api/state');assert state['projects']==[]
        project=api('/api/projects','POST',{'name':'便携 独立路径验收'})
        key=project['id'];draft=project['project'];draft['mode']='custom'
        wave_bytes=io.BytesIO()
        samples=b''.join(struct.pack('<h',int(14000*math.sin(i*math.pi*2*440/48000))) for i in range(48000))
        with wave.open(wave_bytes,'wb') as stream:
            stream.setparams((1,2,48000,0,'NONE','not compressed'));stream.writeframes(samples)
        png=io.BytesIO();Image.new('RGB',(800,600),(143,64,32)).save(png,'PNG')
        assets={
            'audio':('song.wav',wave_bytes.getvalue()),
            'cover':('cover.png',png.getvalue()),
            'lyrics':('song.lrc','[00:00.000]你好\n[00:00.500]便携预览\n'.encode())}
        for field,(name,data) in assets.items():
            record=api(f'/api/projects/{key}/upload?name={name}','POST',binary=data)
            draft['tracks'][0][field]=record['path']
        draft['tracks'][0].update(title='便携测试',artist='原创测试音')
        api('/api/projects/'+key,'PUT',draft)
        built=job(api('/api/projects/'+key+'/build','POST',{})['job'])
        release=built['result']['release'];manifest=built['result']['manifest']
        assert manifest['validation']['passed'] and manifest['validation']['matches_converted']
        assert manifest['firmware']['status']=='requires_build'
        base=f'/api/projects/{key}/releases/{release}'
        status,png,headers=api(base+'/frame?time_ms=500')
        assert status==200 and Image.open(io.BytesIO(png)).size==(640,480)
        status,header,headers=api(base+'/audio?track=0',headers={'Range':'bytes=0-43'})
        assert status==206 and header[:4]==b'RIFF' and len(header)==44
        status,data,headers=api(base+'/audio?track=0',headers={'Range':'bytes=44-63'})
        assert status==206 and data==samples[:20]
        verified=job(api(base+'/verify','POST',{})['job'])
        simulated=job(api('/api/disks/simulate','POST',{'project':key,'release':release})['job'])
        assert simulated['result']['readback_sha256']==manifest['image']['sha256']
        output=ROOT/'evidence/portable';output.mkdir(parents=True,exist_ok=True)
        (output/'portable_preview.png').write_bytes(png)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1100})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(address);page.wait_for_function('S.profile !== null')
            page.evaluate('id=>openProject(id)',key)
            page.wait_for_function("document.querySelector('#frame').naturalWidth===640 && !S.frameBusy",timeout=30000)
            page.locator('#displayState').locator('..').locator('summary').click()
            old=page.locator('#frame').get_attribute('src')
            fixed=dict(wave_samples=[0]*32,wave_peak=0,scroll_active=False,end_mode=1)
            page.locator('#displayState').fill(json.dumps(fixed))
            page.locator('#applyDisplayState').click()
            page.wait_for_function("old=>document.querySelector('#frame').src!==old && !S.frameBusy",arg=old)
            assert page.evaluate('displayedFrameState.state')==fixed
            page.screenshot(path=str(output/'portable_studio.png'),full_page=True)
            # Test the comparator with known identical and one-pixel-changed PNGs.
            # These are synthetic browser fixtures, not board-capture evidence.
            page.locator('[data-tab=comparison]').click();page.locator('#snapshotReference').click()
            data_url=page.locator('#referenceCanvas').evaluate("x=>x.toDataURL('image/png')")
            import base64
            reference=base64.b64decode(data_url.split(',')[1]);same=output/'comparison_fixture.png';same.write_bytes(reference)
            page.locator('#boardFrameInput').set_input_files(str(same))
            page.wait_for_function('comparisonReport !== null')
            assert page.evaluate('comparisonReport.different_pixels')==0
            assert page.evaluate('comparisonReport.reference.state')==fixed
            assert page.evaluate('comparisonReport.reference.release')==release
            profiled=output/'comparison_profile_fixture.png'
            Image.open(io.BytesIO(reference)).convert('RGB').save(profiled,icc_profile=b'profile metadata must not alter original RGB bytes')
            page.locator('#boardFrameInput').set_input_files(str(profiled))
            page.wait_for_function('comparisonReport !== null && comparisonReport.different_pixels === 0')
            changed=Image.open(io.BytesIO(reference)).convert('RGB');r,g,b=changed.getpixel((123,234));changed.putpixel((123,234),((r+1)%256,g,b))
            changed_path=output/'comparison_one_pixel.png';changed.save(changed_path)
            page.locator('#boardFrameInput').set_input_files(str(changed_path))
            page.wait_for_function('comparisonReport !== null && comparisonReport.different_pixels === 1')
            page.screenshot(path=str(output/'pixel_comparison_test.png'),full_page=True)
            assert not errors,errors
            browser.close()
        result=dict(passed=True,destination=str(destination),exe_sha256=hashlib.sha256((destination/'VinylFPGA_MediaStudio.exe').read_bytes()).hexdigest(),
                    stripped_path=env['PATH'],project_id=key,release_id=release,image=manifest['image'],
                    checks=['relocated Unicode path EXE starts','no repository Python/PATH dependency','create/upload/save/reopen project',
                            'custom font/ROM generation','FAT32 build/full media readback','640x480 image-backed preview',
                            'WAV header and PCM byte ranges','file simulation full SHA-256 readback','browser zero console errors',
                            'explicit fixed pixel state reaches preview and comparison report',
                            'PNG stored RGB compared without browser ICC transformation',
                            'pixel comparator identical fixture=0, changed fixture=1'],
                    board_capture_tested=False,physical_card_written=False)
        (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
        api('/api/shutdown','POST',{})
        process.wait(timeout=10)
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,required=True)
    run(parser.parse_args().destination)
