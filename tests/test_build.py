import hashlib
import json
import math
import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path
import sys
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from studio.build import build_project, verify_image
from studio.paths import vendor_module, tool_path
import shutil
import subprocess


def fixture(root, mode='custom', mv=False):
    inputs = root / 'inputs'; inputs.mkdir()
    with wave.open(str(inputs / '音频.wav'), 'wb') as out:
        out.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        out.writeframes(b''.join(struct.pack('<h', int(12000 * math.sin(i * .04))) for i in range(4800)))
    Image.new('RGB', (400, 400), (140, 60, 20)).save(inputs / '封面.png')
    (inputs / '歌词.lrc').write_text('[00:00.00]你好', encoding='utf-8')
    project = {'schema': 1, 'name': '测试唱片', 'mode': mode, 'tracks': [
        {'title': '测试', 'artist': '歌手', 'audio': 'inputs/音频.wav', 'cover': 'inputs/封面.png',
         'lyrics': 'inputs/歌词.lrc'}], 'image_size_mib': 64}
    (root / 'project.json').write_text(json.dumps(project, ensure_ascii=False), encoding='utf-8')
    return project


class BuildTests(unittest.TestCase):
    def test_custom_real_roundtrip_and_crc_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); fixture(root)
            out = root / '输出'
            manifest = build_project(root, out)
            self.assertEqual(manifest['firmware']['status'], 'requires_build')
            self.assertFalse(manifest['firmware']['compatible'])
            self.assertTrue((out / 'rom/song_text_rom.v').is_file())
            self.assertTrue(verify_image(out / 'card.img')['passed'])
            track = manifest['tracks'][0]
            self.assertEqual(track['samples'], 4800)
            self.assertEqual(track['audio']['bytes'], 9600)
            with (out / 'card.img').open('r+b') as image:
                image.seek(track['audio']['lba'] * 512); old = image.read(1)
                image.seek(-1, 1); image.write(bytes([old[0] ^ 1]))
            with self.assertRaises(ValueError): verify_image(out / 'card.img')

    def test_frozen_uses_exact_rom_and_reports_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); fixture(root, 'frozen')
            out = root / 'out'; manifest = build_project(root, out)
            self.assertEqual((out / 'rom/song_text_rom.v').read_bytes(),
                             (ROOT / 'profiles/frozen/song_text_rom.v').read_bytes())
            self.assertFalse(manifest['firmware']['compatible'])
            self.assertTrue(manifest['firmware']['differences'])

    def test_cancel_does_not_publish_release(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); fixture(root)
            event = threading.Event(); event.set()
            with self.assertRaises(ValueError): build_project(root, root / 'out', cancel=event)
            self.assertFalse((root / 'out/release.json').exists())

    def test_actual_mv_and_four_slots(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); project = fixture(root)
            subprocess.run([str(tool_path('ffmpeg')), '-nostdin', '-v', 'error', '-f', 'lavfi',
                            '-i', 'color=c=blue:s=64x64:r=10', '-t', '0.2', '-pix_fmt', 'yuv420p',
                            str(root / 'inputs/video.mp4')], check=True, creationflags=0x08000000)
            project['tracks'][0]['mv'] = 'inputs/video.mp4'
            project['tracks'] *= 4
            (root / 'project.json').write_text(json.dumps(project), encoding='utf-8')
            release = build_project(root, root / 'out')
            self.assertEqual(len(release['tracks']), 4)
            self.assertTrue(all(t['mv']['frames'] == 2 for t in release['tracks']))
            self.assertEqual(release['validation']['count'], 4)
            self.assertEqual(len(release['validation']['files']), 15)

    def test_bank_overflow_does_not_publish_release(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); fixture(root)
            (root / 'inputs/歌词.lrc').write_text('\n'.join(
                f'[00:{i:02d}.00]' + ''.join(chr(0x4E00 + i * 16 + j) for j in range(16)) for i in range(17)),
                encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '256'):
                build_project(root, root / 'out')
            self.assertFalse((root / 'out/release.json').exists())

    def test_project_edit_does_not_change_inflight_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); data = fixture(root)
            def edited(stage, fraction, message):
                if stage == 'convert' and fraction == 0:
                    data['name'] = '后来修改'; data['tracks'][0]['title'] = '另一个标题'
                    (root / 'project.json').write_text(json.dumps(data), encoding='utf-8')
            release = build_project(root, root / 'out', progress=edited)
            self.assertEqual(release['name'], '测试唱片')
            self.assertEqual(release['tracks'][0]['title'], '测试')

    def test_mid_build_cancel_does_not_publish_release(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); fixture(root); cancel = threading.Event()
            def callback(stage, fraction, message):
                if stage == 'convert' and fraction == 1: cancel.set()
            with self.assertRaises(ValueError):
                build_project(root, root / 'out', progress=callback, cancel=cancel)
            self.assertFalse((root / 'out/release.json').exists())

    def test_source_change_is_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); fixture(root)
            def mutate(stage, fraction, message):
                if stage == 'rom':
                    p = root / 'inputs/音频.wav'
                    blob = bytearray(p.read_bytes()); blob[-1] ^= 1; p.write_bytes(blob)
            with self.assertRaisesRegex(ValueError, '发生变化'):
                build_project(root, root / 'out', progress=mutate)
            self.assertFalse((root / 'out/release.json').exists())

    def test_packed_files_must_match_expected_conversion_hashes(self):
        for name in ('TRACK1.MV', 'BG.BIN', 'LYRICS.BIN', 'INDEX.BIN'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root = Path(d); data = fixture(root)
                subprocess.run([str(tool_path('ffmpeg')), '-nostdin', '-v', 'error', '-f', 'lavfi',
                                '-i', 'color=c=blue:s=64x64:r=10', '-t', '0.2', '-pix_fmt', 'yuv420p',
                                str(root / 'inputs/video.mp4')], check=True, creationflags=0x08000000)
                data['tracks'][0]['mv'] = 'inputs/video.mp4'
                (root / 'project.json').write_text(json.dumps(data), encoding='utf-8')
                def corrupt(stage, fraction, message):
                    if stage == 'verify':
                        vp = vendor_module('vinyl_packer')
                        with vp.Fat32Image(root / 'out/card.img', writable=True) as image:
                            entry = image.find_root_file(name)
                            # INDEX padding is intentionally outside entries_crc32.
                            offset = image.cluster_lba(entry.first_cluster) * 512 + (511 if name == 'INDEX.BIN' else 0)
                            image.fh.seek(offset); old = image.fh.read(1)
                            image.fh.seek(offset); image.fh.write(bytes([old[0] ^ 1]))
                with self.assertRaisesRegex(ValueError, 'SHA-256|哈希|回读'):
                    build_project(root, root / 'out', progress=corrupt)
                self.assertFalse((root / 'out/release.json').exists())


class ImageRejectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name); fixture(cls.root)
        cls.release = build_project(cls.root, cls.root / 'release')

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory()
        self.image = Path(self.case.name) / 'corrupt.img'
        shutil.copyfile(self.root / 'release/card.img', self.image)
        self.vp = vendor_module('vinyl_packer')

    def tearDown(self): self.case.cleanup()

    def test_truncated_image(self):
        with self.image.open('r+b') as f: f.truncate(10 << 20)
        with self.assertRaises(ValueError): verify_image(self.image)

    def test_fat_copies_must_match(self):
        with self.vp.Fat32Image(self.image, writable=True) as v:
            sector = bytearray(v.read_sector(v.fat_start_lba)); sector[100] ^= 1
            v.write_sector(v.fat_start_lba, bytes(sector))
        with self.assertRaisesRegex(ValueError, 'FAT1/FAT2'): verify_image(self.image)

    def test_shared_file_clusters_refused(self):
        with self.vp.Fat32Image(self.image, writable=True) as v:
            audio, cover = v.find_root_file('AUDIO.PCM'), v.find_root_file('PIC1.BMP')
            raw = bytearray(v.read_sector(audio.lba))
            struct.pack_into('<H', raw, audio.slot_off + 26, cover.first_cluster)
            struct.pack_into('<I', raw, audio.slot_off + 28, cover.size)
            v.write_sector(audio.lba, bytes(raw))
        with self.assertRaisesRegex(ValueError, '重叠'): verify_image(self.image)

    def test_fragmented_audio_refused(self):
        with self.vp.Fat32Image(self.image, writable=True) as v:
            audio = v.find_root_file('AUDIO.PCM')
            chain = list(v.iter_chain(audio.first_cluster)); spare = v.max_cluster - 3
            old = v.cluster_lba(chain[1]); new = v.cluster_lba(spare)
            for i in range(v.sectors_per_cluster): v.write_sector(new + i, v.read_sector(old + i))
            v.fat_set(chain[0], spare); v.fat_set(spare, chain[2]); v.fat_set(chain[1], 0)
        with self.assertRaisesRegex(ValueError, 'PCM 必须连续'): verify_image(self.image)

    def test_index_crc_refused(self):
        with self.vp.Fat32Image(self.image, writable=True) as v:
            entry = v.find_root_file('INDEX.BIN')
            raw = bytearray(v.read_sector(v.cluster_lba(entry.first_cluster))); raw[97] ^= 1
            v.write_sector(v.cluster_lba(entry.first_cluster), bytes(raw))
        with self.assertRaisesRegex(ValueError, 'crc32'): verify_image(self.image)

    def test_index_outside_first_root_sector(self):
        with self.vp.Fat32Image(self.image, writable=True) as v:
            original = bytearray(v.read_sector(v.cluster_lba(v.root_cluster)))
            index_entry = original[:32]
            for i in range(16):
                if i == 0 or original[i * 32] == 0: original[i * 32] = 0xE5
            spare = v.max_cluster - 3
            v.fat_set(v.root_cluster, spare); v.fat_set(spare, 0x0FFFFFFF)
            v.write_sector(v.cluster_lba(v.root_cluster), bytes(original))
            v.write_sector(v.cluster_lba(spare), bytes(index_entry) + bytes(480))
        with self.assertRaisesRegex(ValueError, '16'): verify_image(self.image)


if __name__ == '__main__': unittest.main()
