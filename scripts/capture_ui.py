"""Exercise a built album through the real browser UI and retain preview PNGs."""
import argparse
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def capture(url,project_id,output):
    output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
        page=browser.new_page(viewport={'width':1600,'height':1280},device_scale_factor=1)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(url)
        page.wait_for_function('S.profile !== null')
        page.evaluate('id=>openProject(id)',project_id)
        page.wait_for_function("document.querySelector('#frame').naturalWidth===640 && !S.frameBusy",timeout=60000)
        previous=page.locator('#frame').get_attribute('src')
        page.evaluate("()=>{const x=document.querySelector('#timeline');x.value=72.5;x.dispatchEvent(new Event('input'));}")
        page.wait_for_function("old=>document.querySelector('#frame').src!==old && !S.frameBusy",arg=previous,timeout=60000)
        page.screenshot(path=str(output/'studio_play.png'),full_page=True)
        raw=page.evaluate("async()=>Array.from(new Uint8Array(await (await fetch(document.querySelector('#frame').src)).arrayBuffer()))")
        (output/'preview_play.png').write_bytes(bytes(raw))
        page.locator('#pageBrowse').click()
        page.wait_for_function('!S.frameBusy',timeout=30000)
        raw=page.evaluate("async()=>Array.from(new Uint8Array(await (await fetch(document.querySelector('#frame').src)).arrayBuffer()))")
        (output/'preview_browse.png').write_bytes(bytes(raw))
        page.screenshot(path=str(output/'studio_browse.png'),full_page=True)
        page.locator('[data-tab=delivery]').click()
        page.locator('#verifyImage').click()
        page.wait_for_function("document.querySelector('#jobTitle').textContent.startsWith('重新校验')",timeout=10000)
        page.wait_for_function('S.job === null',timeout=60000)
        job_text=page.locator('#jobTitle').text_content()
        if '完成' not in job_text:
            raise AssertionError(page.locator('#jobLog').text_content())
        page.screenshot(path=str(output/'studio_delivery.png'),full_page=True)
        page.locator('[data-tab=comparison]').click();page.locator('#snapshotReference').click()
        page.screenshot(path=str(output/'studio_comparison.png'),full_page=True)
        result=dict(url=url,project_id=project_id,verification_job=job_text,
                    console_errors=errors,preview_size=[640,480],board_capture_tested=False,
                    scope='UI and image-readback previews from the final portable EXE; no physical board capture',
                    screenshots=[x.name for x in output.glob('*.png') if not x.name.startswith('hardware_')])
        (output/'browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        browser.close()
        if errors:raise AssertionError(errors)
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();capture(args.url,args.project,args.output)
