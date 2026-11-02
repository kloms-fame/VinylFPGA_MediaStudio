import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from studio.build import build_project
from test_build import fixture


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        cls.release = cls.root / 'out'
        cls.manifest = build_project(cls.root, cls.release)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def module(self):
        try:
            return importlib.import_module('studio.preview')
        except ImportError:
            self.fail('The image-backed ROM pixel preview is not implemented')

    def test_rom_vectors_and_glyph_bits_are_read_from_generated_verilog(self):
        m = self.module()
        rom = m.Rom(self.release / 'rom')
        self.assertEqual(len(rom.tracks), 4)
        self.assertEqual(rom.tracks[0]['line_count'], 1)
        self.assertEqual(rom.tracks[0]['title_length'], 2)
        self.assertEqual(len(rom.font), 8192)
        self.assertEqual(len(rom.ascii), 1024)
        self.assertTrue(any(rom.font))

    def test_exact_integer_brightness_and_contrast_boundaries(self):
        adjust = self.module().adjust_channel
        self.assertEqual(adjust(255, 4, 2), 255)
        self.assertEqual(adjust(1, 3, 0), 64)
        self.assertEqual(adjust(0, 3, 4), 0)
        self.assertEqual(adjust(128, 3, 2), 128)
        for i in range(256): self.assertEqual(adjust(i, 3, 2), i)

    def test_rgb565_expands_by_bit_replication(self):
        image = self.module().decode565(bytes.fromhex('00f8e0071f00'), (3,1))
        self.assertEqual(list(image.get_flattened_data()), [(255,0,0),(0,255,0),(0,0,255)])

    def test_image_source_not_loose_conversion_file(self):
        m = self.module()
        preview = m.Preview(self.release / 'release.json')
        first = preview.frame(page='browse').tobytes()
        target = self.release / self.manifest['tracks'][0]['cover']['path']
        old = target.read_bytes()
        try:
            target.write_bytes(b'not a BMP')
            self.assertEqual(preview.frame(page='browse').tobytes(), first)
        finally:
            target.write_bytes(old)
        self.assertEqual(preview.audio_info(0)['lba'], self.manifest['tracks'][0]['audio']['lba'])
        self.assertEqual(preview.audio_info(0)['bytes'], 9600)
        self.assertEqual(preview.frame().size, (640,480))

    def test_waveform_fullscale_and_explicit_scroll_state(self):
        p = self.module().Preview(self.release / 'release.json')
        frame = p.frame(wave_samples=[128]*32, wave_peak=128)
        self.assertEqual(frame.getpixel((66,376)), (255,248,208))
        self.assertEqual(frame.getpixel((66,377)), (255,48,64))
        self.assertEqual(p.frame(scroll_offset=24,scroll_active=True).size,(640,480))

    def test_bad_rom_hash_and_bad_image_identity_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/'copy'
            shutil.copytree(self.release,target)
            rom = target/'rom/song_font_rom.v'
            rom.write_bytes(rom.read_bytes()+b'\n// changed')
            with self.assertRaisesRegex(ValueError,'ROM|哈希'):
                self.module().Preview(target/'release.json')
            shutil.copyfile(self.release/'rom/song_font_rom.v',rom)
            with (target/'card.img').open('r+b') as image:
                image.seek(self.manifest['tracks'][0]['audio']['lba']*512)
                image.write(b'\xff')
            with self.assertRaisesRegex(ValueError,'镜像|哈希'):
                self.module().Preview(target/'release.json')


if __name__=='__main__': unittest.main()
