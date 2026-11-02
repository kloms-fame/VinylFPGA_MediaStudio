import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class LauncherContract(unittest.TestCase):
    def test_starts_in_unicode_path_and_creates_ready_parent(self):
        tool=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='唱片 工具 ') as directory:
            root=Path(directory)
            ready=root/'new status'/'ready.json'
            process=subprocess.Popen([sys.executable,str(tool/'src/launcher.py'),
                                      '--workspace',str(root/'素材项目'), '--no-browser',
                                      '--ready-file',str(ready)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+8
                while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertTrue(ready.exists(),'portable launcher failed before publishing ready state')
                state=json.loads(ready.read_text())
                self.assertTrue(state['url'].startswith('http://127.0.0.1:'))
                self.assertEqual(Path(state['workspace']),root/'素材项目')
            finally:
                if process.poll() is None: process.terminate()
                process.communicate(timeout=5)


if __name__=='__main__':
    unittest.main()
