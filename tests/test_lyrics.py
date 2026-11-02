import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from studio.lyrics import parse_lyrics


class LyricsTests(unittest.TestCase):
    def parse(self, text, suffix='.lrc', offset=0):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ('歌词' + suffix)
            path.write_text(text, encoding='utf-8')
            return parse_lyrics(path, offset)

    def test_lrc_multi_tags_and_offset(self):
        self.assertEqual(self.parse('[offset:50]\n[00:01.20][00:02.30]你好', offset=20),
                         [(1270, '你好'), (2370, '你好')])

    def test_rom_time_limits(self):
        self.assertEqual(self.parse('[04:22.143]终点'), [(262143, '终点')])
        for text in ('[04:22.144]越界', '[00:00.00]负数'):
            with self.assertRaises(ValueError):
                self.parse(text, offset=-1 if '负数' in text else 0)

    def test_srt_cleanup_and_bounded_splitting(self):
        rows = self.parse('1\n00:00:01,000 --> 00:00:04,000\n<i>你好&amp;世界</i>\n\n2\n00:00:05,000 --> 00:00:08,000\nNever gonna tell a lie and hurt you\n', '.srt')
        self.assertEqual(rows[0], (1000, '你好&世界'))
        self.assertTrue(all(0 < len(t) <= 16 for _, t in rows))
        self.assertEqual([t for t, _ in rows], sorted(set(t for t, _ in rows)))

    def test_refuse_duplicates_and_more_than_128(self):
        with self.assertRaises(ValueError): self.parse('[00:01.00]a\n[00:01.00]b')
        with self.assertRaises(ValueError):
            self.parse('\n'.join(f'[{i // 60:02d}:{i % 60:02d}.00]a' for i in range(129)))


if __name__ == '__main__': unittest.main()
