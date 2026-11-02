import struct
import tempfile
import unittest
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'vendor'))
from studio.fat32 import Fat32Builder
from studio.paths import sha256_file
import vinyl_packer as vp


class Fat32Tests(unittest.TestCase):
    def test_real_volume_independent_parser(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / '中文卡.img'
            payload = bytes(range(256)) * 5
            with Fat32Builder(path, 64) as fat:
                record = fat.add_file('AUDIO.PCM', payload)
            with vp.Fat32Image(path) as fat:
                self.assertEqual(fat.num_fats, 2)
                self.assertEqual(fat.read_file(record['cluster'], len(payload)), payload)
                self.assertEqual(len(fat.cluster_runs(record['cluster'])), 1)
                self.assertEqual(fat.find_root_file('AUDIO.PCM').size, len(payload))
                self.assertGreaterEqual(fat.max_cluster, 65525)
                fat.fh.seek(fat.fat_start_lba * 512)
                first = fat.fh.read(fat.fat_size * 512)
                self.assertGreaterEqual(len(first), (fat.max_cluster + 1) * 4)
                self.assertEqual(first, fat.fh.read(fat.fat_size * 512))
            with path.open('rb') as f:
                f.seek(512)
                info = f.read(512)
                self.assertEqual(info[:4], b'RRaA')
                self.assertEqual(info[484:488], b'rrAa')

    def test_directory_capacity_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            with Fat32Builder(Path(d) / 'card.img', 64) as fat:
                fat.add_file('SAME.BIN', b'a')
                with self.assertRaises(ValueError): fat.add_file('SAME.BIN', b'b')
                for i in range(15): fat.add_file(f'F{i}.BIN', b'a')
                with self.assertRaises(ValueError): fat.add_file('EXTRA.BIN', b'a')

    def test_refuse_undersize_or_existing_image(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'a.img'
            with self.assertRaises(ValueError): Fat32Builder(path, 4)
            path.write_bytes(b'preserved')
            with self.assertRaises(FileExistsError): Fat32Builder(path, 64)
            self.assertEqual(path.read_bytes(), b'preserved')

    def test_stream_copy_and_digest_observe_cancel_between_chunks(self):
        class CancelAfterChunk:
            def __init__(self): self.calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 1
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = root / 'large.bin'
            with source.open('wb') as stream: stream.truncate(12 << 20)
            with self.assertRaisesRegex(ValueError, '取消'):
                sha256_file(source, cancel=CancelAfterChunk())
            with Fat32Builder(root / 'card.img', 64) as fat:
                with self.assertRaisesRegex(ValueError, '取消'):
                    fat.add_file('LARGE.BIN', source, cancel=CancelAfterChunk())


if __name__ == '__main__': unittest.main()
