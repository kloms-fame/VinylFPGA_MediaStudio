"""Audit evidence, create portable ZIP, and copy the verified standalone delivery.

Only copies beneath the explicitly selected repository's MediaStudio folder.
Does not delete directories, touch FPGA inputs, or access physical devices.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def package(repository):
    repository=repository.resolve(strict=True)
    target=(repository/'VinylFPGA_MediaStudio').resolve()
    if target.parent!=repository or target.name!='VinylFPGA_MediaStudio' or target==ROOT:
        raise ValueError('Unexpected delivery target')
    checks=read(ROOT/'evidence/checks/results.json')
    rtl=read(ROOT/'evidence/rtl_final/results.json')
    portable=read(ROOT/'evidence/portable/results.json')
    if not all(x['passed'] for x in (checks,rtl,portable)):raise ValueError('Evidence contains a failing result')
    for relative,expected in checks['source_sha256'].items():
        if digest(ROOT/relative)!=expected:raise ValueError('Source changed after verification: '+relative)
    for relative,expected in rtl['source_sha256'].items():
        if digest(ROOT/relative)!=expected:raise ValueError('RTL reference changed: '+relative)
    if digest(ROOT/'tests/rtl/mv_stream_player_reference.v')!=rtl['rgb565_all_values']['source_sha256']:
        raise ValueError('RGB565 RTL reference changed')
    frozen=read(ROOT/'profiles/frozen/profile.json')
    for relative,expected in frozen['files_sha256'].items():
        if digest(ROOT/'profiles/frozen'/relative)!=expected:raise ValueError('Frozen profile changed: '+relative)
    bundle=ROOT/'dist/VinylFPGA_MediaStudio'
    if digest(bundle/'VinylFPGA_MediaStudio.exe')!=portable['exe_sha256']:
        raise ValueError('EXE differs from relocated acceptance test')
    for entry in read(bundle/'bundle.sha256.json'):
        if digest(bundle/entry['path'])!=entry['sha256']:raise ValueError('Bundle hash mismatch: '+entry['path'])
    for folder in ('web','vendor','profiles','resources'):
        for path in (ROOT/folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                if digest(path)!=digest(bundle/'_internal'/path.relative_to(ROOT)):
                    raise ValueError('Bundle data differs from source: '+str(path))
    original=read(repository/'doc/产品项目输出/final_release_20261022.json')
    protected=dict(original['source_and_constraint_sha256'])
    for record in (original['project'],original['frozen_firmware']):protected[record['path']]=record['sha256']
    drift=[]
    for relative,expected in protected.items():
        actual=digest(repository/relative)
        if actual!=expected:drift.append(dict(path=relative,frozen_sha256=expected,current_sha256=actual))
    for record in (original['project'],original['frozen_firmware']):
        if digest(repository/record['path'])!=record['sha256']:
            raise ValueError('Frozen firmware or project identity changed; review before packaging')
    write(ROOT/'evidence/baseline_drift.json',dict(snapshot_utc=datetime.now(timezone.utc).isoformat(),
          current_repository_matches_frozen_baseline=not drift,files=drift,
          scope='Tool preserves the accepted frozen firmware profile. Concurrent KEY6 lyric-focus RTL development is outside that profile; no original FPGA files are written by this packaging script.'))
    album=read(ROOT/'evidence/full_album.json');project=album['project_id']
    release=ROOT/'projects'/project/'outputs'/album['release_id']
    manifest=read(release/'release.json')
    if not manifest['validation']['matches_converted'] or not manifest['firmware']['compatible']:
        raise ValueError('Full album verification or frozen compatibility failed')
    if digest(release/'card.img')!=manifest['image']['sha256']:raise ValueError('Full album image hash mismatch')
    zip_path=ROOT/'release/VinylFPGA_MediaStudio_Windows_x64.zip';zip_path.parent.mkdir(exist_ok=True)
    temporary=zip_path.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():archive.write(path,'VinylFPGA_MediaStudio/'+path.relative_to(bundle).as_posix())
    temporary.replace(zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        if archive.testzip() is not None:raise ValueError('Portable ZIP CRC check failed')
    write(ROOT/'release/checksums.json',dict(zip=dict(path=zip_path.name,bytes=zip_path.stat().st_size,sha256=digest(zip_path)),exe_sha256=portable['exe_sha256']))
    shutil.copyfile(ROOT/'test-output/synthpilot-llms.txt',ROOT/'evidence/SynthPilot_llms_20261022.txt')
    roots=['src','web','vendor','profiles','resources','tests','scripts','packaging','dist','release',
           f'projects/{project}','evidence/rtl_final','evidence/screenshots','evidence/portable','evidence/checks']
    singles=['.gitignore','README.md','requirements.txt','使用说明.md','验收报告.md','设计方案.md','实施计划.md','启动工具.cmd',
             'evidence/full_album.json','evidence/SynthPilot_llms_20261022.txt','evidence/baseline_drift.json']
    files=[ROOT/name for name in singles]
    for name in roots:
        files.extend(p for p in (ROOT/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix!='.pyc' and p.name not in ('studio.log','hardware_play.png','hardware_browse.png'))
    target.mkdir(parents=True,exist_ok=True)
    proposal=target/'设计方案_待确认.md'
    if proposal.is_file():
        archived=target/'历史设计提案_已批准.md'
        if archived.exists():raise ValueError('Historical proposal archive already exists')
        proposal.rename(archived)
        archived.write_text('> 本提案已于 2026-10-22 获用户批准，当前交付见 README.md、设计方案.md 和验收报告.md。\n\n'+archived.read_text(encoding='utf-8-sig'),encoding='utf-8')
    copied=[]
    for path in files:
        relative=path.relative_to(ROOT);destination=target/relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,destination)
        expected=digest(path)
        if digest(destination)!=expected:raise ValueError('Copy hash mismatch: '+str(relative))
        copied.append(dict(path=relative.as_posix(),bytes=destination.stat().st_size,sha256=expected))
    plan=ROOT/'实施计划.md'
    plan.write_text(plan.read_text(encoding='utf-8').replace('- [ ] Copy complete verified tool directory','- [x] Copy complete verified tool directory'),encoding='utf-8')
    shutil.copy2(plan,target/plan.name)
    if digest(plan)!=digest(target/plan.name):raise ValueError('Final plan copy differs')
    for entry in copied:
        if entry['path']==plan.name:entry.update(bytes=plan.stat().st_size,sha256=digest(plan))
    report=dict(passed=True,verified_at_utc=datetime.now(timezone.utc).isoformat(),destination=str(target),
                copied_files=len(copied),copied_bytes=sum(x['bytes'] for x in copied),copy_hashes_match=True,
                original_fpga_files_checked=len(protected),current_repository_matches_frozen_baseline=not drift,
                frozen_project_and_bit_unchanged=True,concurrent_rtl_drift=drift,
                exe_sha256=portable['exe_sha256'],zip_sha256=digest(zip_path),full_album_image_sha256=manifest['image']['sha256'],
                physical_card_written=False,board_capture_tested=False)
    for root in (ROOT,target):
        write(root/'evidence/delivery.json',report)
        write(root/'delivery.sha256.json',dict(schema=1,files=copied))
    print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--repository',type=Path,required=True)
    package(parser.parse_args().repository)
