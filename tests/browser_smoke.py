"""Chromium UI checks with explicitly mocked APIs; no Google/Colab calls."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit, parse_qs

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get('CTH_SCREENSHOT_DIR', ROOT/'docs/screenshots'))
JOB_A='a'*32
JOB_B='b'*32


async def main():
    OUT.mkdir(parents=True,exist_ok=True)
    server=subprocess.Popen(['python3','-m','http.server','8765','--bind','127.0.0.1','--directory',str(ROOT/'frontend/dist')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
            page=await browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
            errors=[]
            page.on('pageerror', lambda error: errors.append(str(error)))
            release=asyncio.Event()
            old_started=asyncio.Event()
            async def route(request_route):
                request=request_route.request
                url=urlsplit(request.url)
                if url.hostname!='127.0.0.1':
                    await request_route.abort()
                    return
                path=url.path
                if not path.startswith('/api/'):
                    await request_route.continue_()
                    return
                data={}
                if path == '/api/app/status':
                    data = {'version': '1.1.0', 'installation': 'installed', 'notice': None, 'changes': [], 'update': {'status': 'not_checked', 'message': 'Belum diperiksa'}}
                elif path == '/api/local-state':
                    data={'generation':'initial'}
                elif path=='/api/projects/':
                    data=[{'id':1,'name':'Alpha','model_name':'chanthecno-alpha'},{'id':2,'name':'Beta','model_name':'chanthecno-beta'}]
                elif path=='/api/connections/google-drive':
                    data={'status':'disconnected','message':'Fixture offline'}
                elif path.startswith('/api/datasets/'):
                    data=[]
                elif '/models/' in path:
                    project=path.rsplit('/',1)[1]
                    data={'models':[{'job_id':JOB_A if project=='1' else JOB_B,'run_id':'20261101T120000Z-1234abcd','model_name':'chanthecno-alpha' if project=='1' else 'chanthecno-beta','backup_verified':True,'local_available':True}]}
                elif '/status/' in path:
                    data={'status':'available','job_id':parse_qs(url.query)['job_id'][0]}
                elif '/restore/' in path:
                    data={'status':'idle'}
                elif path.endswith('/chat'):
                    body=request.post_data_json
                    if body['question']=='Permintaan lama':
                        old_started.set()
                        await release.wait()
                        data={'answer':'Jawaban lama tidak boleh tampil','sources':[],'mode':'qwen'}
                    else:
                        data={'answer':'Dataset proyek Alpha disimpan secara persisten.','sources':['alpha.txt'],'mode':'qwen','basis':'mixed','notice':'Fixture: sumber proyek dan kemungkinan pengetahuan umum.'}
                else:
                    raise AssertionError('Unexpected API '+path)
                await request_route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
            await page.route('**/*',route)
            await page.goto('http://127.0.0.1:8765/#testing')
            await page.get_by_label('Pilih proyek aktif').select_option('1')
            await expect(page.locator('.mt-chat-heading h2')).to_have_text('chanthecno-alpha')
            await expect(page.locator('.mt-runtime')).to_contain_text('Berkas tersedia')
            await page.get_by_label('Pesan untuk model').fill('Permintaan lama')
            await page.get_by_role('button',name='Kirim pertanyaan').click()
            await old_started.wait()
            await page.get_by_label('Pilih proyek aktif').select_option('2')
            await expect(page.locator('.mt-chat-heading h2')).to_have_text('chanthecno-beta')
            release.set()
            await page.get_by_label('Pilih proyek aktif').select_option('1')
            await expect(page.locator('.mt-chat-heading h2')).to_have_text('chanthecno-alpha')
            await expect(page.locator('.mt-transcript')).not_to_contain_text('Jawaban lama tidak boleh tampil')
            await page.get_by_label('Pesan untuk model').fill('Bagaimana dataset disimpan?')
            await page.get_by_role('button',name='Kirim pertanyaan').click()
            await expect(page.locator('.mt-message.assistant')).to_contain_text('Dataset proyek Alpha')
            await expect(page.locator('.mt-toolbar select')).to_contain_text('chanthecno-alpha')
            await page.get_by_label('Salin jawaban').click()
            await page.screenshot(path=str(OUT/'model-testing-desktop.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.get_by_role('button',name='Percakapan baru',exact=True).click()
            await expect(page.locator('.mt-message')).to_have_count(0)
            mobile=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,reduced_motion='reduce')
            await mobile.route('**/*',route)
            mobile.on('pageerror',lambda error:errors.append(str(error)))
            await mobile.goto('http://127.0.0.1:8765/#testing')
            await mobile.get_by_label('Pilih proyek aktif').select_option('1')
            await expect(mobile.locator('.mt-chat-heading h2')).to_have_text('chanthecno-alpha')
            assert not await mobile.locator('.mt-library').evaluate('(element) => element.open')
            await mobile.locator('.mt-library summary').click()
            assert await mobile.locator('.mt-library').evaluate('(element) => element.open')
            await mobile.locator('.mt-library summary').click()
            await mobile.get_by_label('Pesan untuk model').fill('Pertanyaan mobile')
            await mobile.get_by_role('button',name='Kirim pertanyaan').click()
            await expect(mobile.locator('.mt-message.assistant')).to_be_visible()
            await expect(mobile.locator('.mt-title h1')).to_be_in_viewport()
            await expect(mobile.get_by_label('Pesan untuk model')).to_be_in_viewport()
            await mobile.screenshot(path=str(OUT/'model-testing-mobile.png'),full_page=True)
            assert await mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors, errors
            print('PASS Chromium desktop/mobile, dynamic name, chat, copy/new conversation, collapsible library, stale response isolation, no horizontal overflow')
            await browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__=='__main__':
    asyncio.run(main())
