"""Material-to-image pipeline and read-only image integrity verification."""
import hashlib
import json
import math
import os
import shutil
import struct
import zlib
from pathlib import Path
from .fat32 import Fat32Builder
from .lyrics import parse_lyrics
from .media import check_cancel, convert_audio, convert_cover, convert_video, make_backgrounds
from .paths import resource_root, resolve_input, sha256_file, vendor_module
from .project import load_project, validate_project


def _crc_file(path, *, cancel=None):
    crc = 0
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(4 << 20), b''):
            check_cancel(cancel)
            crc = zlib.crc32(data, crc)
    return crc & 0xFFFFFFFF


def _archive_lyrics(tracks):
    selected = [t for t in tracks if t['lyrics']]
    if not selected: return None
    data = bytearray(b'LYR2' + struct.pack('<HH', 1, len(selected)))
    for track in selected:
        title, artist = track['title'].encode(), track['artist'].encode()
        data += struct.pack('<BBBH', track['slot'], len(title), len(artist), len(track['lyrics']))
        data += title + artist
        for stamp, text in track['lyrics']:
            text = text.encode('utf-8')
            data += struct.pack('<IH', stamp, len(text)) + text
    data += struct.pack('<I', zlib.crc32(data) & 0xFFFFFFFF)
    return bytes(data)


def _write_rom(output, project, tracks):
    frozen = resource_root() / 'profiles/frozen'
    profile = json.loads((frozen / 'profile.json').read_text(encoding='utf-8'))
    for relative, expected in profile['files_sha256'].items():
        if sha256_file(frozen / relative) != expected:
            raise ValueError('冻结配置文件已改变：' + relative)
    rom = output / 'rom'
    if project['mode'] == 'frozen':
        shutil.copytree(frozen, rom)
        differences = []
        for track in tracks:
            current = profile['tracks'][track['slot']]
            for field in ('title', 'artist', 'lyrics'):
                requested = track[field]
                if field == 'lyrics': requested = [list(row) for row in requested]
                if requested != current[field]:
                    differences.append({'slot': track['slot'], 'field': field,
                                        'message': '当前固件仍显示冻结 ROM 中的内容'})
        firmware = dict(status='frozen', compatible=not differences, differences=differences,
                        bit_sha256=profile['bit_sha256'])
    else:
        rom.mkdir()
        generator = vendor_module('gen_lrc_assets')
        font = resource_root() / 'resources/fonts/NotoSansCJKsc-Regular.otf'
        if not font.is_file(): raise ValueError('便携字库缺失：NotoSansCJKsc-Regular.otf')
        generator.FONT_CANDIDATES = [str(font)]
        rows = []
        source_dir = output / 'lyric_assets'; source_dir.mkdir()
        for slot in range(4):
            track = tracks[slot] if slot < len(tracks) else dict(title='', artist='', lyrics=[])
            lyric = None
            if track['lyrics']:
                lyric = f'track{slot + 1}.lrc'
                text = '\n'.join(f'[{stamp // 60000:02d}:{stamp // 1000 % 60:02d}.{stamp % 1000:03d}]{line}'
                                 for stamp, line in track['lyrics']) + '\n'
                (source_dir / lyric).write_text(text, encoding='utf-8')
            rows.append(dict(song_id=slot, title=track['title'], artist=track['artist'], lrc=lyric))
        manifest = source_dir / 'songs_manifest.json'
        manifest.write_text(json.dumps(dict(glyph_banks=True, tracks=rows), ensure_ascii=False), encoding='utf-8')
        try:
            generated = generator.generate_song_text_assets(manifest, output / 'generated')
        except generator.LrcError as exc:
            raise ValueError(str(exc)) from exc
        for relative in ('src/display/song_text_rom.v', 'src/display/song_font_rom.v', 'src/include/song_text_geom.vh'):
            shutil.copyfile(output / 'generated' / relative, rom / Path(relative).name)
        for name in ('ascii_font_rom.v', 'playback_layout.vh'):
            shutil.copyfile(frozen / name, rom / name)
        custom_profile = dict(schema=1, tracks=[dict(slot=i, title=t['title'], artist=t['artist'], lyrics=t['lyrics'])
                                               for i, t in enumerate(tracks)],
                              font_sha256=sha256_file(font), bank_glyphs=list(map(len, generated['bank_chars'])))
        (rom / 'profile.json').write_text(json.dumps(custom_profile, ensure_ascii=False, indent=2), encoding='utf-8')
        (rom / '固件交接说明.txt').write_text(
            '本目录为候选 ROM，不是已烧录固件。\n将 song_text_rom.v、song_font_rom.v、song_text_geom.vh '
            '放入独立 FPGA 工程对应目录后，完成仿真、综合、布局布线、最终时序检查与烧录。\n'
            '素材工具未更改原工程，也未声称候选 ROM 已在板上生效。\n', encoding='utf-8')
        firmware = dict(status='requires_build', compatible=False,
                        differences=[{'message': '自定义歌词/标题需编译并烧录本次配套 ROM'}], bit_sha256=None)
    return dict(path='rom', files_sha256={p.relative_to(rom).as_posix(): sha256_file(p)
                                        for p in rom.rglob('*') if p.is_file()}), firmware


def build_project(project_dir, output_dir, *, progress=None, cancel=None):
    """Create one immutable release. No release.json is exposed before validation."""
    check_cancel(cancel)
    project_dir, output = Path(project_dir).resolve(), Path(output_dir).resolve()
    project = validate_project(load_project(project_dir), for_build=True)
    if output.exists(): raise FileExistsError('输出目录已存在，请使用新版本目录')
    sources, resolved, lyric_rows = {}, [], []
    for track in project['tracks']:
        paths = {}
        for field in ('audio', 'cover', 'mv', 'lyrics'):
            paths[field] = resolve_input(project_dir, track[field], required=field == 'cover')
            if paths[field] is not None:
                source = paths[field]
                stat = source.stat()
                sources[track[field]] = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                            sha256=sha256_file(source, cancel=cancel))
        resolved.append(paths)
        lyric_rows.append(parse_lyrics(paths['lyrics'], track['lyric_offset_ms']) if paths['lyrics'] else [])
    if project['mode'] == 'custom':
        common = set(' -:/TRACK 0123456789ARTISTNO LYRICS')
        for bank in range(2):
            glyphs = set(common)
            for slot in range(bank * 2, min(bank * 2 + 2, len(project['tracks']))):
                glyphs.update(project['tracks'][slot]['title'])
                glyphs.update(project['tracks'][slot]['artist'])
                for _, line in lyric_rows[slot]: glyphs.update(line)
            if len(glyphs) > 256:
                raise ValueError(f'字库组 {bank} 包含 {len(glyphs)} 个字形，超过 256 上限')
    output.mkdir(parents=True)
    (output / 'project_snapshot.json').write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding='utf-8')
    converted = output / 'converted'; converted.mkdir()
    def report(stage, value, message):
        if stage != 'complete': check_cancel(cancel)
        if progress: progress(stage, value, message)
    report('convert', 0, '开始转换素材')
    tracks, covers = [], []
    for slot, (track, paths) in enumerate(zip(project['tracks'], resolved)):
        check_cancel(cancel)
        audio_name = 'AUDIO.PCM' if slot == 0 else f'AUDIO{slot + 1}.PCM'
        cover_name = f'PIC{slot + 1}.BMP'
        samples = convert_audio(paths['audio'] or paths['mv'], converted / audio_name, cancel=cancel)
        cover = convert_cover(paths['cover'], converted / cover_name, fit=track['cover_fit'], crop=track['crop'])
        covers.append(cover)
        mv = None
        if paths['mv']:
            mv_name = f'TRACK{slot + 1}.MV'
            frames = convert_video(paths['mv'], converted / mv_name, cancel=cancel)
            mv = dict(path='converted/' + mv_name, bytes=(converted / mv_name).stat().st_size,
                      frames=frames, fps=10, sha256=sha256_file(converted / mv_name, cancel=cancel))
        lyrics = lyric_rows[slot]
        record = dict(slot=slot, title=track['title'], artist=track['artist'],
                      duration_ms=samples * 1000 // 48000, samples=samples, lyrics=lyrics,
                      audio=dict(path='converted/' + audio_name, bytes=samples * 2,
                                 sha256=sha256_file(converted / audio_name, cancel=cancel)),
                      cover=dict(path='converted/' + cover_name, bytes=(converted / cover_name).stat().st_size,
                                 sha256=sha256_file(converted / cover_name)), mv=mv)
        tracks.append(record)
        report('convert', (slot + 1) / len(project['tracks']), f'曲目 {slot + 1} 转换完成')
    report('rom', 0, '生成配套 ROM 与固件兼容信息')
    rom, firmware = _write_rom(output, project, tracks)
    check_cancel(cancel)
    make_backgrounds(covers, converted / 'BG.BIN', cancel=cancel)
    archive = _archive_lyrics(tracks)
    if archive: (converted / 'LYRICS.BIN').write_bytes(archive)
    media_files = [p for p in converted.iterdir() if p.is_file()]
    expected_files = {p.name: dict(bytes=p.stat().st_size, sha256=sha256_file(p, cancel=cancel)) for p in media_files}
    for track in tracks:
        for field in ('audio', 'cover', 'mv'):
            if track[field] is not None:
                name = Path(track[field]['path']).name
                if expected_files[name]['sha256'] != track[field]['sha256']:
                    raise ValueError('转换素材的 SHA-256 在打包前发生变化：' + name)
    estimate = sum(p.stat().st_size for p in media_files) + 512
    auto_size = max(64, math.ceil((estimate * 1.04 + (2 << 20)) / (16 << 20)) * 16)
    size = project['image_size_mib'] or auto_size
    if size < auto_size:
        raise ValueError(f'镜像容量不足，当前素材建议至少 {auto_size} MiB')
    report('image', 0, f'创建 {size} MiB FAT32 镜像')
    vp = vendor_module('vinyl_packer')
    songs = []
    with Fat32Builder(output / 'card.img', size) as fat:
        fat.add_file('INDEX.BIN', bytes(512))
        for track in tracks:
            saved = {}
            for field in ('cover', 'audio', 'mv'):
                if track[field] is None: continue
                source = output / track[field]['path']
                saved[field] = fat.add_file(source.name, source, cancel=cancel)
                track[field]['lba'] = saved[field]['lba']
                track[field]['cluster'] = saved[field]['cluster']
            flags = vp.FLAG_COVER | vp.FLAG_AUDIO
            if track['lyrics']: flags |= vp.FLAG_LRC
            if track['mv']: flags |= vp.FLAG_MV
            songs.append(vp.IndexSong(title=track['title'], flags=flags,
                         cover_cluster=saved['cover']['cluster'], cover_bytes=saved['cover']['bytes'],
                         cover_crc32=_crc_file(output / track['cover']['path'], cancel=cancel),
                         audio_cluster=saved['audio']['cluster'], audio_bytes=saved['audio']['bytes'],
                         audio_crc32=_crc_file(output / track['audio']['path'], cancel=cancel), audio_lba=saved['audio']['lba'],
                         mv_lba=track['mv']['lba'] if track['mv'] else 0,
                         mv_frames=track['mv']['frames'] if track['mv'] else 0))
            report('image', (track['slot'] + 1) / len(tracks), f'曲目 {track["slot"] + 1} 连续分配完成')
        background = fat.add_file('BG.BIN', converted / 'BG.BIN', cancel=cancel)
        if archive: fat.add_file('LYRICS.BIN', converted / 'LYRICS.BIN')
        index = bytearray(vp.build_index(songs))
        index[32:36] = b'BGR1'
        struct.pack_into('<III', index, 36, background['lba'], 614400, 6)
        fat.replace_file('INDEX.BIN', bytes(index))
        expected_files['INDEX.BIN'] = dict(bytes=len(index), sha256=hashlib.sha256(index).hexdigest())
    report('verify', 0, '回读镜像，检查目录、连续分配、CRC 与边界')
    validation = verify_image(output / 'card.img', cancel=cancel)
    if set(validation['files']) != set(expected_files):
        raise ValueError('镜像回读文件集合与本次打包输入不一致')
    for name, expected in expected_files.items():
        actual = validation['files'][name]
        if actual['bytes'] != expected['bytes'] or actual['sha256'] != expected['sha256']:
            raise ValueError('镜像回读 SHA-256 与转换素材不符：' + name)
    validation['matches_converted'] = True
    validation['expected_files_sha256'] = {name: record['sha256'] for name, record in expected_files.items()}
    for relative, before in sources.items():
        path = resolve_input(project_dir, relative)
        stat = path.stat()
        if stat.st_size != before['bytes'] or stat.st_mtime_ns != before['mtime_ns'] or sha256_file(path, cancel=cancel) != before['sha256']:
            raise ValueError('源素材在构建期间发生变化，请重新生成：' + relative)
    check_cancel(cancel)
    manifest = dict(schema=1, name=project['name'], mode=project['mode'],
                    image=dict(path='card.img', sha256=sha256_file(output / 'card.img', cancel=cancel), bytes=size * (1 << 20)),
                    tracks=tracks, background=dict(lba=background['lba'], frames=6, frame_bytes=614400),
                    rom=rom, firmware=firmware, validation=validation, sources=sources)
    temp = output / '.release.tmp'
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    check_cancel(cancel)
    os.replace(temp, output / 'release.json')
    report('complete', 1, '镜像与回读校验完成')
    return manifest


def verify_image(image_path, *, cancel=None):
    """Verify file ownership, complete chains, media CRCs and physical ranges."""
    vp = vendor_module('vinyl_packer')
    path = Path(image_path)
    try:
        with vp.Fat32Image(path) as volume:
            if path.stat().st_size != (volume.partition_lba + volume.total_sectors) * 512:
                raise ValueError('镜像长度与 BPB 不一致或被截断')
            if volume.num_fats != 2: raise ValueError('镜像必须有两份 FAT')
            volume.fh.seek(volume.fat_start_lba * 512)
            fat = volume.fh.read(volume.fat_size * 512)
            if len(fat) != volume.fat_size * 512 or volume.fh.read(len(fat)) != fat:
                raise ValueError('FAT1/FAT2 不一致或被截断')
            if (volume.max_cluster + 1) * 4 > len(fat): raise ValueError('FAT 容量不足')
            def chain(first):
                result, seen = [], set()
                current = first
                while True:
                    if current < 2 or current >= volume.max_cluster or current in seen:
                        raise ValueError('FAT 链循环、共享或越界')
                    result.append(current); seen.add(current)
                    current = struct.unpack_from('<I', fat, current * 4)[0] & 0x0FFFFFFF
                    if current >= 0x0FFFFFF8: break
                    if current < 2: raise ValueError('FAT 链提前终止')
                return result
            root_chain = chain(volume.root_cluster)
            occupied = set(root_chain)
            entries = list(volume.root_entries())
            files = {}
            for entry in entries:
                check_cancel(cancel)
                if entry.attr & 0x18: raise ValueError('镜像根目录包含未支持的目录/卷标')
                if entry.raw_index >= 16 or entry.name in files:
                    raise ValueError('目录项超出前 16 项或文件名重复')
                links = chain(entry.first_cluster)
                if entry.size <= 0 or len(links) != math.ceil(entry.size / (volume.sectors_per_cluster * 512)):
                    raise ValueError('目录大小与 FAT 链长度不符：' + entry.name)
                if any(c in occupied for c in links): raise ValueError('媒体文件簇范围重叠')
                occupied.update(links)
                runs = []
                for c in links:
                    lba = volume.cluster_lba(c)
                    if runs and runs[-1][0] + runs[-1][1] == lba: runs[-1][1] += volume.sectors_per_cluster
                    else: runs.append([lba, volume.sectors_per_cluster])
                crc, digest, remaining = 0, hashlib.sha256(), entry.size
                for lba, sectors in runs:
                    volume.fh.seek(lba * 512)
                    count = min(remaining, sectors * 512)
                    while count:
                        check_cancel(cancel)
                        data = volume.fh.read(min(count, 4 << 20))
                        if not data: raise ValueError('媒体读取不完整')
                        crc = zlib.crc32(data, crc); digest.update(data)
                        count -= len(data); remaining -= len(data)
                files[entry.name] = dict(cluster=entry.first_cluster, lba=runs[0][0], bytes=entry.size,
                                        runs=runs, crc32=crc & 0xFFFFFFFF, sha256=digest.hexdigest())
            if 'INDEX.BIN' not in files or files['INDEX.BIN']['bytes'] != 512:
                raise ValueError('缺少 512 字节 INDEX.BIN')
            volume.fh.seek(files['INDEX.BIN']['lba'] * 512)
            index_bytes = volume.fh.read(512)
            index = vp.parse_index(index_bytes)
            for number, song in enumerate(index['songs']):
                for field, flag in (('cover', vp.FLAG_COVER), ('audio', vp.FLAG_AUDIO)):
                    if not song['flags'] & flag: raise ValueError('曲目缺少封面或音频标记')
                    matches = [v for v in files.values() if v['cluster'] == song[field + '_cluster']]
                    if len(matches) != 1: raise ValueError('INDEX 引用了缺失文件')
                    record = matches[0]
                    if record['bytes'] != song[field + '_bytes'] or record['crc32'] != song[field + '_crc32']:
                        raise ValueError(f'第 {number + 1} 曲 {field} CRC 或长度不符')
                    if field == 'audio':
                        if len(record['runs']) != 1 or record['lba'] != song['audio_lba'] or record['bytes'] % 2:
                            raise ValueError('PCM 必须连续、偶数字节且 LBA 与索引一致')
                    else:
                        if record['bytes'] != 921654: raise ValueError('封面大小不符合 640×480 BMP')
                        cover = volume.read_file(record['cluster'], record['bytes'])
                        vp._check_cover(cover, 'cover', 640, 480)
                if song['flags'] & vp.FLAG_MV:
                    matches = [v for v in files.values() if v['lba'] == song['mv_lba']]
                    if len(matches) != 1 or len(matches[0]['runs']) != 1 or matches[0]['bytes'] != song['mv_frames'] * 153600:
                        raise ValueError('MV 的物理范围、帧数或连续分配无效')
            background = None
            if index_bytes[32:36] == b'BGR1':
                lba, frame_bytes, count = struct.unpack_from('<III', index_bytes, 36)
                matches = [v for v in files.values() if v['lba'] == lba]
                if (frame_bytes != 614400 or count not in (5, 6) or len(matches) != 1 or
                        matches[0]['bytes'] != frame_bytes * count or len(matches[0]['runs']) != 1):
                    raise ValueError('BGR1 背景范围或帧长错误')
                background = dict(lba=lba, frames=count, frame_bytes=frame_bytes)
            return dict(passed=True, count=index['count'], files=files, background=background,
                        layout='FAT32 superfloppy' if volume.partition_lba == 0 else 'FAT32 MBR',
                        sectors_per_cluster=volume.sectors_per_cluster,
                        entries_crc32=f'{index["entries_crc32"]:08x}')
    except vp.PackError as exc:
        raise ValueError(str(exc)) from exc
