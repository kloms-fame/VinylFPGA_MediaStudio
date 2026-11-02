import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from studio import project
from studio.paths import safe_relative


class ProjectTests(unittest.TestCase):
    def test_minimum_valid_project_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = {'schema': 1, 'name': '中文唱片', 'mode': 'custom', 'tracks': [
                {'title': '标题', 'artist': '歌手', 'audio': 'inputs/a.wav', 'cover': 'inputs/a.png'}]}
            project.save_project(root, data)
            self.assertEqual(project.load_project(root)['name'], '中文唱片')

    def test_reject_escape_and_five_songs(self):
        data = {'schema': 1, 'name': 'a', 'mode': 'custom', 'tracks': [
            {'title': 'a', 'artist': '', 'audio': '../a.wav', 'cover': 'a.png'}]}
        with self.assertRaises(ValueError):
            project.validate_project(data)

    def test_reject_bad_crop(self):
        data = {'schema': 1, 'name': 'a', 'mode': 'custom', 'tracks': [
            {'title': 'ok', 'artist': '', 'audio': 'a.wav', 'cover': 'a.png', 'crop': [2, .5]}]}
        with self.assertRaises(ValueError): project.validate_project(data)
        data['tracks'][0]['audio'] = 'inputs/a.wav'
        data['tracks'] *= 5
        with self.assertRaises(ValueError):
            project.validate_project(data)

    def test_reject_bad_crop_and_long_title(self):
        data = {'schema': 1, 'name': 'a', 'mode': 'custom', 'tracks': [
            {'title': 'x' * 17, 'artist': '', 'audio': 'a.wav', 'cover': 'a.png'}]}
        with self.assertRaises(ValueError):
            project.validate_project(data)

    def test_draft_can_be_saved_before_import(self):
        data = {'schema': 1, 'name': '草稿', 'mode': 'frozen',
                'tracks': [{'title': '', 'artist': '', 'audio': '', 'cover': ''}]}
        self.assertEqual(project.validate_project(data)['tracks'][0]['audio'], '')
        with self.assertRaises(ValueError): project.validate_project(data, for_build=True)

    def test_windows_relative_path_boundary(self):
        for path in ('/outside.wav', '\\outside.wav', 'C:relative.wav', '../x', 'a/../../x', '\\\\server\\x'):
            with self.subTest(path=path), self.assertRaises(ValueError): safe_relative(path)


if __name__ == '__main__': unittest.main()
