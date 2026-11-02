"""Capture the accepted ROM/profile. Build-time only; never called by the app."""
import argparse
import hashlib
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path


def capture(repository, tool):
    repository, tool = Path(repository).resolve(), Path(tool).resolve()
    sys.path.insert(0, str(repository / 'tools'))
    import vinyl_packer as vp
    release = json.loads((repository / 'doc/产品项目输出/final_release_20261022.json').read_text(encoding='utf-8'))
    target = tool / 'profiles/frozen'
    target.mkdir(parents=True, exist_ok=True)
    sources = ['src/display/song_text_rom.v', 'src/display/song_font_rom.v',
               'src/display/ascii_font_rom.v', 'src/include/song_text_geom.vh',
               'src/include/playback_layout.vh']
    for name in sources:
        shutil.copyfile(repository / name, target / Path(name).name)
    rtl = target / 'rtl'
    rtl.mkdir(exist_ok=True)
    for name in ['playback_page.v', 'lrc_five_osd.v', 'playback_background.v',
                 'brightness_contrast_steps.v', 'promax_audio_waveform.v',
                 'lrc_scroll_controller.v']:
        shutil.copyfile(repository / 'src/display' / name, rtl / name)
    with vp.Fat32Image(repository / release['media']['image']) as image:
        entry = image.find_root_file('LYRICS.BIN')
        blob = image.read_file(entry.first_cluster, entry.size)
    assert blob[:4] == b'LYR2' and zlib.crc32(blob[:-4]) & 0xffffffff == struct.unpack_from('<I', blob, len(blob)-4)[0]
    count = struct.unpack_from('<H', blob, 6)[0]
    cursor, tracks = 8, []
    for _ in range(count):
        slot, title_len, artist_len, rows = struct.unpack_from('<BBBH', blob, cursor)
        cursor += 5
        title = blob[cursor:cursor+title_len].decode('utf-8'); cursor += title_len
        artist = blob[cursor:cursor+artist_len].decode('utf-8'); cursor += artist_len
        lyrics = []
        for _ in range(rows):
            ms, length = struct.unpack_from('<IH', blob, cursor); cursor += 6
            text = blob[cursor:cursor+length].decode('utf-8'); cursor += length
            lyrics.append([ms, text])
        tracks.append(dict(slot=slot, title=title[:16], artist=artist, lyrics=lyrics))
    assert cursor == len(blob)-4
    files = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in target.rglob('*') if p.is_file() and p.name != 'profile.json'}
    profile = dict(schema=1, name='2026-10-22 已验收冻结固件',
                   hardware_source_commit=release['hardware_source_commit'],
                   bit_sha256=release['frozen_firmware']['sha256'],
                   files_sha256=files, tracks=tracks,
                   defaults=dict(brightness=3, contrast=2, volume=200),
                   screen=[640, 480], mv=[320, 240, 10])
    (target / 'profile.json').write_text(json.dumps(profile, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('Captured', len(tracks), 'tracks and', len(files), 'source artifacts')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tool', default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    capture(args.repository, args.tool)
