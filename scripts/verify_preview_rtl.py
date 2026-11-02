"""Compare the software pixel selector to real RTL for explicit display states.

This verifies fixed states, not the complete HDMI/storage history. Exact input
ROMs, display-state configuration, base pixels and results are retained.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave
from PIL import Image,ImageChops

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from studio.build import build_project
from studio.preview import Preview,decode565
from studio.paths import sha256_file


def fixture(root):
    inputs=root/'inputs';inputs.mkdir(parents=True)
    with wave.open(str(inputs/'song.wav'),'wb') as out:
        out.setparams((1,2,48000,0,'NONE','not compressed'))
        out.writeframes(bytes(48000*2*12))
    cover=Image.new('RGB',(640,480))
    cover.putdata([((x*3+y)&255,(x+y*5)&255,(x*7+y*11)&255) for y in range(480) for x in range(640)])
    cover.save(inputs/'cover.png')
    rows='\n'.join(f'[00:{i:02d}.000]第{i}行歌词画面测试' for i in range(8))
    (inputs/'song.lrc').write_text(rows,encoding='utf-8')
    tracks=[dict(title='测试'+str(i),artist='歌手'+str(i),audio='inputs/song.wav',
                 cover='inputs/cover.png',lyrics='inputs/song.lrc' if i<3 else '') for i in range(4)]
    (root/'project.json').write_text(json.dumps(dict(schema=1,name='RTL像素校对',mode='custom',tracks=tracks,image_size_mib=64),ensure_ascii=False),encoding='utf-8')
    build_project(root,root/'release')
    return root/'release/release.json'


def config(preview,case,active):
    state=preview.display_state(case.get('track',0),case.get('time_ms',0),**{k:v for k,v in case.items() if k not in ('track','time_ms')})
    rom=state['rom']
    rows=state['lyric_rows']
    packed_rows=sum(sum(g<<(i*8) for i,g in enumerate(row['glyphs']))<<(slot*128) for slot,row in enumerate(rows))
    lengths=sum(row['length']<<(slot*5) for slot,row in enumerate(rows))
    def asc(s):return s.encode('ascii').hex()
    assignments=dict(enable="1'b"+str(int(case.get('page','play')=='play')),
        browse_mode="1'b"+str(int(case.get('page','play')=='browse')),
        background_image_active="1'b"+str(int(active)),song_id=f"2'd{state['track']}",
        end_mode=f"2'd{state['end_mode']}",title_length=f"5'd{rom['title_length']}",
        artist_length=f"5'd{rom['artist_length']}",title_glyphs=f"128'h{rom['title_glyphs']:032x}",
        artist_glyphs=f"128'h{rom['artist_glyphs']:032x}",
        no_lyrics_glyphs=f"128'h{sum(g<<(i*8) for i,g in enumerate(preview.rom.no_lyrics)):032x}",
        line_count=f"8'd{rom['line_count']}",lyric_window_valid="1'b1",
        lyric_highlight_valid="1'b"+str(int(state['highlight_valid'])),
        lyric_scroll_active="1'b"+str(int(state['scroll_active'])),lyric_row_base_slot="3'd0",
        lyric_row_lengths=f"30'h{lengths:x}",lyric_row_glyphs=f"768'h{packed_rows:x}",
        lyric_scroll_offset=f"6'd{state['scroll_offset']}",progress_fill=f"9'd{state['progress_fill']}",
        elapsed_time_ascii="40'h"+asc(state['elapsed_time']),total_time_ascii="40'h"+asc(state['total_time']),
        remaining_time_ascii="48'h"+asc(state['remaining_time']),
        brightness=f"3'd{case.get('brightness',3)}",contrast=f"3'd{case.get('contrast',2)}",
        wave_enable="1'b"+str(int(state['wave_enable'] and case.get('page','play')=='play')),
        wave_peak=f"8'd{state['wave_peak']}")
    lines=[f'{name}={value};' for name,value in assignments.items()]
    lines += [f"wave_values[{i}]=8'd{v};" for i,v in enumerate(state['wave_samples'])]
    return '\n'.join(lines)+'\n'


def run(output):
    output=output.resolve();output.mkdir(parents=True,exist_ok=False)
    release=fixture(output/'fixture')
    preview=Preview(release)
    cases=[
      dict(name='browse_first',page='browse',track=0,time_ms=0),
      dict(name='browse_bank1',page='browse',track=2,time_ms=0),
      dict(name='play_gray',track=0,time_ms=2500),
      dict(name='play_background',track=2,time_ms=4500,background=1),
      dict(name='play_art',track=1,time_ms=1000,background=2),
      dict(name='scroll_half',track=0,time_ms=3000,scroll_active=True,scroll_offset=24),
      dict(name='scroll_end',track=2,time_ms=6000,scroll_active=True,scroll_offset=45),
      dict(name='no_lyrics_bank1',track=3,time_ms=0),
      dict(name='wave_fullscale',track=0,time_ms=2500,wave_samples=[128]*32,wave_peak=128),
      dict(name='wave_all_colors',track=1,time_ms=2500,wave_samples=[i*4 for i in range(32)],wave_peak=124),
      dict(name='brightness_low_contrast_high',track=2,time_ms=4500,brightness=0,contrast=4),
      dict(name='brightness_high_contrast_low',page='browse',track=2,brightness=4,contrast=0),
    ]
    rom=release.parent/'rom';rtl=ROOT/'profiles/frozen/rtl'
    source=[rtl/'playback_page.v',rtl/'lrc_five_osd.v',rtl/'playback_background.v',
            rtl/'promax_audio_waveform.v',rtl/'brightness_contrast_steps.v',
            rom/'song_font_rom.v',rom/'ascii_font_rom.v',ROOT/'tests/rtl/tb_preview_frame.v']
    results=[]
    for case in cases:
        case=dict(case);name=case.pop('name');folder=output/name;folder.mkdir()
        base,active=preview.base_frame(case.get('track',0),case.get('time_ms',0),case.get('page','play'),case.get('background',0),case.get('visual','mv'))
        raw=base.tobytes()
        (folder/'base.hex').write_text('\n'.join(raw[i:i+3].hex() for i in range(0,len(raw),3))+'\n',encoding='ascii')
        (folder/'config.vh').write_text(config(preview,case,active),encoding='ascii')
        command=['iverilog','-g2005','-I',str(folder),'-I',str(rom),'-s','tb_preview_frame','-o',str(folder/'sim.vvp'),*[str(p) for p in source]]
        compiled=subprocess.run(command,capture_output=True,text=True)
        log=compiled.stdout+compiled.stderr
        if compiled.returncode:raise RuntimeError(log)
        executed=subprocess.run(['vvp',str(folder/'sim.vvp')],cwd=folder,capture_output=True,text=True,timeout=90)
        log+=executed.stdout+executed.stderr
        (folder/'run.log').write_text(log,encoding='utf-8')
        if executed.returncode or 'PASS' not in log:raise RuntimeError(log)
        actual=Image.frombytes('RGB',(640,480),(folder/'rtl.rgb').read_bytes())
        expected=preview.frame(**case)
        diff=ImageChops.difference(actual,expected)
        count=sum(1 for p in diff.get_flattened_data() if p!=(0,0,0))
        actual.save(folder/'rtl.png');expected.save(folder/'preview.png');diff.save(folder/'difference.png')
        result=dict(case=name,state=case,passed=count==0,different_pixels=count,
                    rtl_rgb_sha256=sha256_file(folder/'rtl.rgb'),preview_rgb_sha256=hashlib.sha256(expected.tobytes()).hexdigest(),
                    input_config_sha256=sha256_file(folder/'config.vh'),compile_command=command)
        results.append(result);print(name,'PASS' if not count else 'FAIL',count,flush=True)
    summary=dict(passed=all(x['passed'] for x in results),cases=results,
                 source_sha256={str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p):sha256_file(p) for p in source},
                 scope='Fixed-state playback_page + lrc_five_osd + promax_audio_waveform + brightness_contrast_steps. No assertion of complete hardware timing/history.')
    folder=output/'rgb565_all';folder.mkdir()
    decoder=ROOT/'tests/rtl/mv_stream_player_reference.v';bench=ROOT/'tests/rtl/tb_rgb565.v'
    command=['iverilog','-g2005','-s','tb_rgb565','-o',str(folder/'sim.vvp'),str(decoder),str(bench)]
    compiled=subprocess.run(command,capture_output=True,text=True)
    if compiled.returncode:raise RuntimeError(compiled.stdout+compiled.stderr)
    executed=subprocess.run(['vvp',str(folder/'sim.vvp')],cwd=folder,capture_output=True,text=True,timeout=30)
    (folder/'run.log').write_text(compiled.stdout+compiled.stderr+executed.stdout+executed.stderr,encoding='utf-8')
    expected=decode565(b''.join(struct.pack('<H',i) for i in range(65536)),(256,256))
    actual=Image.frombytes('RGB',(256,256),(folder/'rgb565.rgb').read_bytes())
    diff=ImageChops.difference(actual,expected)
    count=sum(1 for pixel in diff.get_flattened_data() if pixel!=(0,0,0))
    actual.save(folder/'rtl.png');expected.save(folder/'preview.png');diff.save(folder/'difference.png')
    summary['rgb565_all_values']=dict(passed=executed.returncode==0 and count==0,different_pixels=count,values=65536,
        source_sha256=sha256_file(decoder),compile_command=command)
    summary['passed'] &= summary['rgb565_all_values']['passed']
    print('rgb565_all_values','PASS' if not count else 'FAIL',count,flush=True)
    (output/'results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if not summary['passed']:raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
