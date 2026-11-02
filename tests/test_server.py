import http.client
import importlib
import json
import tempfile
import threading
import unittest
import hashlib
import io
import uuid
from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class ServerContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        try:
            module = importlib.import_module('studio.server')
        except ImportError:
            self.fail('studio.server and the protected local application API are not implemented')
        self.server = module.StudioServer(Path(self.temp.name), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, url, value=None, token=True, origin=None, host=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        body = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        headers = {'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'}
        if token:
            headers['X-Studio-Token'] = self.server.token
        if origin:
            headers['Origin'] = origin
        if host:
            headers['Host'] = host
        conn.request(method, url, body, headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def create(self):
        code, data = self.request('POST', '/api/projects', {'name': '我的唱片'})
        self.assertEqual(code, 201, data)
        return json.loads(data)['id']

    def test_state_and_offline_html(self):
        code, data = self.request('GET', '/api/state')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)['projects'], [])
        code, data = self.request('GET', '/')
        self.assertEqual(code, 200)
        self.assertIn(self.server.token.encode(), data)
        self.assertNotIn(b'https://cdn.', data)

    def test_board_png_decodes_raw_rgb_without_color_management(self):
        from PIL import Image
        frame=Image.new('RGB',(640,480),(17,83,249));data=io.BytesIO()
        frame.save(data,'PNG',icc_profile=b'opaque profile metadata must not alter the RGB samples')
        code,raw=self.request('POST','/api/decode-png',raw=data.getvalue())
        self.assertEqual(code,200,raw[:100]);self.assertEqual(raw,bytes((17,83,249,255))*(640*480))
        self.assertEqual(self.request('POST','/api/decode-png',raw=data.getvalue(),token=False)[0],403)
        for bad in (Image.new('RGB',(320,240)),Image.new('RGBA',(640,480),(0,0,0,254))):
            data=io.BytesIO();bad.save(data,'PNG')
            self.assertEqual(self.request('POST','/api/decode-png',raw=data.getvalue())[0],400)
        self.assertEqual(self.request('POST','/api/decode-png',raw=b'not a png')[0],400)

    def test_fixed_display_state_reaches_renderer_and_invalid_states_are_rejected(self):
        from PIL import Image
        from urllib.parse import urlencode
        project=self.create();release=uuid.uuid4().hex
        root=self.server.project_dir(project)/'outputs'/release;root.mkdir(parents=True)
        (root/'release.json').write_text('{}')
        state=dict(scroll_active=True,scroll_offset=24,wave_samples=[0]*32,wave_peak=0,end_mode=1)
        renderer=mock.Mock();renderer.frame.return_value=Image.new('RGB',(640,480))
        url=f'/api/projects/{project}/releases/{release}/frame?'
        with mock.patch.object(self.server,'preview',return_value=renderer):
            code,raw=self.request('GET',url+urlencode({'state':json.dumps(state)}))
            self.assertEqual(code,200,raw)
            self.assertEqual(renderer.frame.call_args.kwargs.get('scroll_offset'),24)
            self.assertEqual(renderer.frame.call_args.kwargs.get('wave_samples'),[0]*32)
            for bad in [dict(scroll_offset=64),dict(wave_samples=[0]),dict(track=3),dict(wave_peak=True),dict(end_mode=4)]:
                self.assertEqual(self.request('GET',url+urlencode({'state':json.dumps(bad)}))[0],400,bad)

    def test_mutation_requires_session_token_and_same_origin(self):
        self.assertEqual(self.request('POST', '/api/projects', {'name': 'x'}, token=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/projects', {'name': 'x'}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('GET', '/api/state', host='evil.example')[0], 400)

    def test_create_upload_save_reopen_unicode_project(self):
        project = self.create()
        code, raw = self.request('POST', f'/api/projects/{project}/upload?name=cover.png', raw=b'fixture-png')
        self.assertEqual(code, 201, raw)
        asset = json.loads(raw)
        self.assertTrue(asset['path'].startswith('inputs/'))
        code, raw = self.request('GET', f'/api/projects/{project}')
        draft = json.loads(raw)['project']
        draft['tracks'][0]['cover'] = asset['path']
        self.assertEqual(self.request('PUT', f'/api/projects/{project}', draft)[0], 200)
        self.assertEqual(self.request('GET', f'/api/projects/{project}/asset?path={asset["path"]}')[1], b'fixture-png')
        code, raw = self.request('GET', f'/api/projects/{project}')
        self.assertEqual(json.loads(raw)['project']['tracks'][0]['cover'], asset['path'])

    def test_path_escape_and_executable_upload_refused(self):
        project = self.create()
        self.assertNotEqual(self.request('GET', f'/api/projects/{project}/asset?path=../../secret.txt')[0], 200)
        self.assertEqual(self.request('POST', f'/api/projects/{project}/upload?name=bad.exe', raw=b'MZ')[0], 400)
        self.assertEqual(self.request('POST', '/api/projects', {'name': '../outside'})[0], 201)
        self.assertFalse((Path(self.temp.name).parent / 'outside').exists())

    def test_lyric_preflight_exposes_split_lines_offsets_and_bank_capacity(self):
        project=self.create();root=self.server.project_dir(project)
        lyric=root/'inputs'/'long.lrc';lyric.write_text('[00:01.000]abcdefghijklmnopqrstuvwx',encoding='utf-8')
        _,raw=self.request('GET',f'/api/projects/{project}')
        draft=json.loads(raw)['project'];draft['tracks'][0].update(title='标题',lyrics='inputs/long.lrc',lyric_offset_ms=50)
        code,raw=self.request('POST',f'/api/projects/{project}/analyze',draft)
        self.assertEqual(code,200,raw);result=json.loads(raw)
        self.assertEqual(result['tracks'][0]['lyrics'][0][0],1050)
        self.assertEqual(len(result['tracks'][0]['lyrics']),2)
        self.assertTrue(all(len(row[1])<=16 for row in result['tracks'][0]['lyrics']))
        common=set(' -:/TRACK 0123456789ARTISTNO LYRICS')
        self.assertEqual(result['banks'][0]['glyphs'],len(common|set('标题abcdefghijklmnopqrstuvwx')))
        self.assertEqual(result['banks'][1]['glyphs'],len(common))
        # No image or media conversion is needed to expose an overfull ROM bank.
        lyric.write_text('\n'.join(f'[00:{i:02d}.000]'+''.join(chr(0x4e00+i*16+j) for j in range(16)) for i in range(20)),encoding='utf-8')
        code,raw=self.request('POST',f'/api/projects/{project}/analyze',draft)
        self.assertEqual(code,200,raw);self.assertFalse(json.loads(raw)['passed'])

    def test_build_errors_are_reported_as_terminal_jobs(self):
        project = self.create()
        code, raw = self.request('POST', f'/api/projects/{project}/build', {})
        self.assertEqual(code, 202, raw)
        job = json.loads(raw)['job']
        state = self.server.jobs.wait(job, timeout=10)
        self.assertEqual(state['status'], 'failed')
        self.assertTrue(state['error'])
        code, raw = self.request('GET', f'/api/jobs/{job}')
        self.assertEqual(json.loads(raw)['status'], 'failed')

    def test_invalid_disk_write_never_starts_helper(self):
        code, data = self.request('POST', '/api/disks/write', {'project': 'bad', 'release': 'bad', 'disk': {}, 'confirm': ''})
        self.assertEqual(code, 400, data)

    def test_explicit_exit_stops_the_local_service(self):
        code,raw=self.request('POST','/api/shutdown',{})
        self.assertEqual(code,202,raw)
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def test_saved_edits_are_distinguished_from_built_snapshot(self):
        project=self.create();root=self.server.project_dir(project)
        draft=json.loads((root/'project.json').read_text(encoding='utf-8'))
        release=uuid.uuid4().hex;output=root/'outputs'/release;output.mkdir(parents=True)
        (output/'release.json').write_text(json.dumps({'schema':1}))
        (output/'project_snapshot.json').write_text(json.dumps(draft))
        code,raw=self.request('GET',f'/api/projects/{project}')
        self.assertTrue(json.loads(raw)['releases'][0].get('project_matches',False))
        draft['name']='已经保存但尚未重新构建'
        self.assertEqual(self.request('PUT',f'/api/projects/{project}',draft)[0],200)
        code,raw=self.request('GET',f'/api/projects/{project}')
        self.assertFalse(json.loads(raw)['releases'][0]['project_matches'])

    def test_file_simulation_writes_and_verifies_without_disk_selection(self):
        from studio.fat32 import Fat32Builder
        project=self.create()
        release=uuid.uuid4().hex
        root=self.server.project_dir(project)/'outputs'/release
        root.mkdir(parents=True)
        image=root/'card.img'
        with Fat32Builder(image,64) as builder:
            builder.add_file('DEMO.TXT',b'portable simulation')
        digest=hashlib.sha256(image.read_bytes()).hexdigest()
        (root/'release.json').write_text(json.dumps({'image':{'path':'card.img','sha256':digest,'bytes':image.stat().st_size}}))
        code,raw=self.request('POST','/api/disks/simulate',{'project':project,'release':release})
        self.assertEqual(code,202,raw)
        job=self.server.jobs.wait(json.loads(raw)['job'],timeout=20)
        self.assertEqual(job['status'],'done',job)
        self.assertEqual(job['result']['readback_sha256'],digest)


if __name__ == '__main__':
    unittest.main()
