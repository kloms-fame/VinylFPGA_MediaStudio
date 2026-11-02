"""Build-time acceptance using the owner's accepted four-song media. No disk writes."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import uuid

TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL/'src'))
from studio.build import build_project
from studio.paths import vendor_module
from studio.project import save_project


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository',type=Path,required=True)
    args=p.parse_args()
    repository=args.repository.resolve()
    old=json.loads((repository/'doc/产品项目输出/final_release_20261022.json').read_text(encoding='utf-8'))
    profile=json.loads((TOOL/'profiles/frozen/profile.json').read_text(encoding='utf-8'))
    key=uuid.uuid4().hex
    project=TOOL/'projects'/key
    (project/'inputs').mkdir(parents=True)
    vp=vendor_module('vinyl_packer')
    tracks=[]
    with vp.Fat32Image(repository/old['media']['image']) as image:
        for slot, metadata in enumerate(profile['tracks']):
            number=slot+1
            cover=f'PIC{number}.BMP'
            audio='AUDIO.PCM' if slot==0 else f'AUDIO{number}.PCM'
            for name in (cover,audio):
                entry=image.find_root_file(name)
                # Existing accepted PCM files have single contiguous physical runs.
                runs=image.cluster_runs(entry.first_cluster)
                assert len(runs)==1
                image.fh.seek(runs[0][0]*512)
                remaining=entry.size
                with (project/'inputs'/name).open('xb') as target:
                    while remaining:
                        data=image.fh.read(min(4<<20,remaining))
                        assert data
                        target.write(data);remaining-=len(data)
            videos=list((repository/f'doc/资源/sd卡/歌曲素材/歌曲{number}').glob('*.mp4'))
            if len(videos)!=1:
                raise ValueError(f'曲目{number}视频来源不唯一')
            video=f'TRACK{number}.mp4'
            shutil.copyfile(videos[0],project/'inputs'/video)
            lyric=f'track{number}.lrc'
            (project/'inputs'/lyric).write_text(''.join(f'[{ms//60000:02d}:{ms//1000%60:02d}.{ms%1000:03d}]{text}\n'
                                                     for ms,text in metadata['lyrics']),encoding='utf-8')
            tracks.append(dict(title=metadata['title'],artist=metadata['artist'],audio='inputs/'+audio,
                               cover='inputs/'+cover,mv='inputs/'+video,lyrics='inputs/'+lyric,
                               crop=[.5,.5],cover_fit='cover',lyric_offset_ms=0))
    save_project(project,dict(schema=1,name='已验收四曲 · Media Studio 重建',mode='frozen',
                              tracks=tracks,image_size_mib=0))
    output_id=uuid.uuid4().hex
    result=build_project(project,project/'outputs'/output_id,
                         progress=lambda stage,fraction,message:print(stage,round(fraction*100),message,flush=True))
    evidence=dict(project_id=key,release_id=output_id,release=result)
    (TOOL/'evidence').mkdir(exist_ok=True)
    (TOOL/'evidence/full_album.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('FULL_ALBUM_PASS',key,output_id,result['image']['sha256'],flush=True)


if __name__=='__main__':
    main()
