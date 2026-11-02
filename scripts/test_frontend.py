"""Real Edge UI regressions. Uses isolated projects; never writes physical disks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from playwright.sync_api import sync_playwright
from PIL import Image

TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL/'src'))
from studio.server import StudioServer


class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='界面 回归 ')
        cls.root=Path(cls.temp.name)
        Image.new('RGB',(200,200),(100,30,20)).save(cls.root/'cover.png')
        cls.server=StudioServer(cls.root/'projects')
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join(3);cls.temp.cleanup()

    def setUp(self):
        self.page=self.browser.new_page(viewport={'width':1440,'height':1080})
        self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.goto(self.server.base_url)
        self.page.locator('#firstProject').wait_for()
        self.page.locator('#firstProject').click()
        self.page.locator('#newName').fill('我的界面测试')
        self.page.locator('#newDialog button[value=create]').click()
        self.page.locator('#trackTitle').wait_for(state='visible')

    def tearDown(self):
        self.page.close()

    def test_lyrics_draft_survives_unrelated_cover_upload(self):
        page=self.page
        page.locator('.lyrics-details summary').click()
        page.locator('#lyricsText').fill('[00:01.000]不可丢失的歌词草稿')
        with page.expect_file_chooser() as chooser:
            page.locator('#assetRows [data-action=import]').nth(1).click()
        chooser.value.set_files(str(self.root/'cover.png'))
        page.wait_for_function("document.querySelectorAll('#assetRows [data-action=clear]').length === 1")
        self.assertEqual(page.locator('#lyricsText').input_value(),'[00:01.000]不可丢失的歌词草稿')
        self.assertFalse(self.errors,self.errors)

    def test_track_order_buttons_reorder_actual_project(self):
        page=self.page
        page.locator('#trackTitle').fill('第一首')
        page.locator('#addTrack').click()
        page.locator('#trackTitle').fill('第二首')
        self.assertEqual(page.locator('#moveTrackUp').count(),1,'曲序编辑需要实际上移/下移操作')
        page.locator('#moveTrackUp').click()
        titles=page.locator('.track-copy strong').all_text_contents()
        self.assertEqual(titles,['第二首','第一首'])
        page.locator('#saveProject').click()
        page.wait_for_function("document.querySelector('#saveState').textContent === '已保存'")
        project=page.evaluate('S.project')
        self.assertEqual([x['title'] for x in project['tracks']],titles)
        self.assertFalse(self.errors,self.errors)

    def test_lyric_preflight_shows_actual_split_rows_and_bank_usage(self):
        page=self.page;page.locator('.lyrics-details summary').click()
        page.locator('#lyricsText').fill('[00:01.000]abcdefghijklmnopqrstuvwx')
        page.locator('#saveLyrics').click()
        page.wait_for_function('S.uploads === 0 && S.project.tracks[0].lyric_draft === undefined')
        page.locator('#analyzeLyrics').click()
        page.wait_for_function("document.querySelector('#lyricAnalysis').textContent.includes('本曲校准后 2 行')")
        text=page.locator('#lyricAnalysis').text_content()
        self.assertIn('字库组 1',text);self.assertIn('字库组 2',text)
        self.assertIn('abcdefghijklmnop',text);self.assertIn('qrstuvwx',text)
        self.assertFalse(self.errors,self.errors)

    def test_pending_upload_cannot_switch_or_contaminate_another_project(self):
        page=self.page
        original=page.evaluate('S.projectId')
        other=page.evaluate("async()=>{const x=await api('/api/projects','POST',{name:'另一个项目'});await refreshState();return x.id;}")
        held=[]
        page.route('**/upload?*',lambda route:held.append(route))
        with page.expect_file_chooser() as chooser:
            page.locator('#assetRows [data-action=import]').nth(1).click()
        chooser.value.set_files(str(self.root/'cover.png'))
        page.wait_for_function('S.uploads === 1')
        page.locator('#projectList button').filter(has_text='另一个项目').click()
        page.wait_for_timeout(250)
        self.assertEqual(page.evaluate('S.projectId'),original,'上传期间不能将旧素材挂到新项目')
        self.assertIn('导入',page.locator('#toast').text_content())
        held[0].continue_()
        page.wait_for_function('S.uploads === 0')
        page.wait_for_function("Boolean(S.project.tracks[0].cover)")
        current=page.evaluate("async id=>(await api('/api/projects/'+id)).project",other)
        self.assertFalse(current['tracks'][0]['cover'])
        self.assertFalse(self.errors,self.errors)


if __name__=='__main__':
    unittest.main()
