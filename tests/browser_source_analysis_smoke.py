"""Source-format warnings and explicit facts-only reindex; every API is mocked."""
import asyncio
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


async def main():
    server = subprocess.Popen(['python3','-m','http.server','8765','--bind','127.0.0.1','--directory',str(ROOT/'frontend/dist')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
            sources = [
                {'id':'a'*32,'name':'percakapan.txt','type':'txt','size':120,'sha256':'a'*64,'source_mode':'training_only','local_saved':True,'drive_status':'saved','receipt_id':'c'*32,'drive_file_id':'fixture-qa'},
                {'id':'b'*32,'name':'fakta-proyek.txt','type':'txt','size':60,'sha256':'b'*64,'source_mode':'facts_only','local_saved':True,'drive_status':'saved','receipt_id':'d'*32,'drive_file_id':'fixture-facts'}]
            updated = False
            calls = []
            errors = []
            page.on('pageerror',lambda error:errors.append(str(error)))
            def report(ids):
                qa = 'a'*32 in ids
                facts = 'b'*32 in ids
                return {'files':[{'dataset_id':item['id'],'name':item['name'],'format':'conversation_qa' if item['id']=='a'*32 else 'plain_text',
                         'searchable_quotes':0 if item['id']=='a'*32 else 1,'source_mode':item['source_mode'],'skipped_lines':2 if item['id']=='a'*32 else 0,
                         'skipped':[{'line':1,'reason':'percakapan latihan tidak otomatis dipercaya'}] if item['id']=='a'*32 else []} for item in sources if item['id'] in ids],
                        'training_samples':int(qa and sources[0]['source_mode']!='facts_only'),'qa_pairs':int(qa),'searchable_quotes':int(facts),'skipped_lines':2*int(qa),
                        'sources_ready':facts,'warning':None if facts else 'Indeks sumber kosong. Pisahkan berkas fakta proyek dari percakapan latihan.'}
            async def handle(route):
                nonlocal updated
                req=route.request
                url=urlsplit(req.url)
                if url.hostname!='127.0.0.1':
                    await route.abort(); return
                path=url.path
                if not path.startswith('/api/'):
                    await route.continue_(); return
                calls.append((req.method,path,req.post_data_json if req.post_data and 'json' in req.headers.get('content-type','') else None))
                if path=='/api/app/status':
                    data={'version':'1.1.1','installation':'installed','notice':None,'changes':[],'update':{'status':'not_checked','message':'Belum diperiksa'}}
                elif path=='/api/local-state': data={'generation':'initial'}
                elif path=='/api/projects/': data=[{'id':1,'name':'Alpha','model_name':'chanthecno-alpha'}]
                elif path=='/api/connections/google-drive': data={'status':'connected','message':'Fixture offline'}
                elif path=='/api/datasets/projects/1': data=sources
                elif path=='/api/datasets/projects/1/analysis': data=report(req.post_data_json['dataset_ids'])
                elif path.endswith('/source-mode'):
                    source=next(item for item in sources if item['id'] in path)
                    source['source_mode']=req.post_data_json['source_mode']
                    source['analysis']={**report([source['id']]),'format':'conversation_qa','source_mode':source['source_mode']}
                    data=source
                elif path=='/api/colab/runtime': data={'status':'allocated','project_id':1}
                elif path=='/api/colab/training/status/1': data={'status':'idle'}
                elif path=='/api/colab/training/preflight': data={'status':'ready','verified':True,'message':'Fixture penyimpanan siap'}
                elif path=='/api/model-testing/models/1': data={'models':[{'job_id':'a'*32,'run_id':'20261101T120000Z-1234abcd','model_name':'chanthecno-alpha','local_available':True}]}
                elif '/api/model-testing/status/' in path:
                    data={'status':'available','job_id':'a'*32,'sources_ready':updated,'source_analysis':report(['b'*32] if updated else ['a'*32]),'source_warning':None if updated else 'Indeks sumber kosong. Tidak ada kutipan proyek yang siap dicari.'}
                elif path=='/api/model-testing/restore':
                    assert req.post_data_json['refresh_sources'] is True
                    assert req.post_data_json['source_dataset_ids']==['b'*32]
                    updated=True
                    data={'status':'completed','job_id':'a'*32,'source_analysis':report(['b'*32]),'message':'Indeks fakta fixture diperbarui tanpa training'}
                elif path=='/api/model-testing/restore/1': data={'status':'completed' if updated else 'idle','job_id':'a'*32,'source_analysis':report(['b'*32] if updated else ['a'*32])}
                else: raise AssertionError('Unexpected API '+path)
                await route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
            await page.route('**/*',handle)
            await page.goto('http://127.0.0.1:8765/#training')
            await page.get_by_label('Pilih proyek aktif').select_option('1')
            await page.get_by_role('checkbox',name='Pilih dataset percakapan.txt',exact=True).check()
            await expect(page.get_by_text('1 pasangan QA terdeteksi',exact=False)).to_be_visible()
            await page.get_by_role('button',name='Uji penyimpanan',exact=True).click()
            await page.get_by_role('checkbox',name=re.compile('Saya setuju')).check()
            await expect(page.get_by_role('button',name='Mulai training baru',exact=True)).to_be_disabled()
            await expect(page.get_by_role('checkbox',name='Lanjutkan tanpa indeks sumber')).to_be_visible()
            await page.get_by_role('region',name='Pilihan dataset training').screenshot(path=str(ROOT/'docs/screenshots/source-analysis-empty.png'))
            await page.get_by_role('checkbox',name='Pilih dataset fakta-proyek.txt',exact=True).check()
            await expect(page.get_by_text('1 kutipan dapat dicari',exact=False)).to_be_visible()
            await expect(page.get_by_role('checkbox',name='Lanjutkan tanpa indeks sumber')).to_have_count(0)
            await page.goto('http://127.0.0.1:8765/#datasets')
            await page.get_by_label('Peran percakapan.txt').select_option('facts_only')
            await expect(page.get_by_text('0 kutipan dapat dicari',exact=False)).to_be_visible()
            await page.goto('http://127.0.0.1:8765/#testing')
            await expect(page.locator('.mt-state-note[role="alert"]')).to_contain_text('Indeks sumber kosong')
            await page.locator('.mt-details > summary').click()
            await page.get_by_role('checkbox',name=re.compile('fakta-proyek.txt')).check()
            await page.get_by_role('button',name='Perbarui sumber tanpa training',exact=True).click()
            await expect(page.locator('.mt-state-note[role="alert"]')).to_have_count(0)
            assert updated
            assert not any(path.endswith('/training/start') for _,path,_ in calls)
            assert not errors,errors
            print('PASS QA counts/empty warning, explicit facts separation, no implicit assistant trust, facts-only reindex of saved model without training (all API mocks)')
            await browser.close()
    finally:
        server.terminate();server.wait(timeout=5)


if __name__=='__main__': asyncio.run(main())
