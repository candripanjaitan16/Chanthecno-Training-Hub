"""Dataset selection/status UI with intercepted APIs; no real worker/Drive requests."""
import asyncio
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


async def main():
    server = subprocess.Popen(['python3', '-m', 'http.server', '8765', '--bind', '127.0.0.1', '--directory', str(ROOT/'frontend/dist')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            sources = [
                {'id': 'a'*32, 'type': 'txt', 'name': 'fakta-alpha.txt', 'size': 1024, 'sha256': '1'*64, 'drive_status': 'saved', 'receipt_id': 'c'*32, 'drive_file_id': 'fixture-drive-1', 'local_saved': True},
                {'id': 'b'*32, 'type': 'txt', 'name': 'belum-sinkron.txt', 'size': 2048, 'sha256': '2'*64, 'drive_status': 'pending', 'receipt_id': None, 'drive_file_id': None, 'local_saved': True},
            ]
            requests = []
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def handle(route):
                request = route.request
                url = urlsplit(request.url)
                if url.hostname != '127.0.0.1':
                    await route.abort()
                    return
                path = url.path
                if not path.startswith('/api/'):
                    await route.continue_()
                    return
                requests.append((request.method, path, request.post_data))
                if path == '/api/app/status':
                    data = {'version': '1.1.0', 'installation': 'installed', 'notice': None, 'changes': [], 'update': {'status': 'not_checked', 'message': 'Belum diperiksa'}}
                elif path == '/api/local-state':
                    data = {'generation': 'initial'}
                elif path == '/api/projects/':
                    data = [{'id': 1, 'name': 'Alpha', 'model_name': 'chanthecno-alpha'}]
                elif path == '/api/connections/google-drive':
                    data = {'status': 'connected', 'message': 'Fixture offline'}
                elif path == '/api/datasets/projects/1':
                    data = sources
                elif path.endswith('/sync'):
                    data = sources[1]
                elif path == '/api/colab/runtime':
                    data = {'status': 'allocated', 'project_id': 1}
                elif path == '/api/colab/training/status/1':
                    data = {'status': 'idle'}
                elif path == '/api/colab/training/preflight':
                    data = {'status': 'ready', 'verified': True, 'message': 'Fixture storage ready'}
                elif path == '/api/colab/training/start':
                    assert request.post_data_json['dataset_ids'] == ['a'*32]
                    assert 'source_receipts' not in request.post_data_json
                    data = {'status': 'queued', 'message': 'Fixture only; no worker'}
                else:
                    raise AssertionError('Unexpected API: ' + path)
                await route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
            await page.route('**/*', handle)
            await page.goto('http://127.0.0.1:8765/#training')
            await page.get_by_label('Pilih proyek aktif').select_option('1')
            start = page.get_by_role('button', name='Mulai training baru', exact=True)
            await expect(start).to_be_disabled()
            await page.get_by_role('checkbox', name='Pilih dataset fakta-alpha.txt', exact=True).check()
            await page.get_by_role('button', name='Uji penyimpanan', exact=True).click()
            consent = page.get_by_role('checkbox', name=re.compile('Saya setuju'))
            await consent.check()
            await expect(start).to_be_enabled()
            await page.get_by_role('checkbox', name='Pilih dataset belum-sinkron.txt', exact=True).check()
            await consent.check()
            await expect(start).to_be_disabled()
            await page.get_by_role('checkbox', name='Pilih dataset belum-sinkron.txt', exact=True).uncheck()
            await consent.check()
            await expect(start).to_be_enabled()
            section = page.get_by_role('region', name='Pilihan dataset training')
            await section.get_by_text('Ringkasan versi yang dipilih').click()
            await expect(section).to_contain_text('fixture-drive-1')
            await expect(section).to_contain_text('1 TXT dipilih')
            output = ROOT/'docs/screenshots/dataset-training-selection.png'
            await section.screenshot(path=str(output))
            await start.click()
            assert len([request for request in requests if request[1].endswith('/training/start')]) == 1
            await page.goto('http://127.0.0.1:8765/#datasets')
            for status, label in [('syncing', 'Drive: mengunggah…'), ('verifying', 'Drive: memverifikasi ID, folder, ukuran, checksum…'), ('failed', 'Drive: gagal — coba sinkronkan kembali')]:
                sources[1]['drive_status'] = status
                await page.get_by_role('button', name='Muat ulang daftar', exact=True).click()
                await expect(page.get_by_text(label, exact=True)).to_be_visible()
            await page.get_by_role('button', name='Coba sinkronkan', exact=True).click()
            assert any(path.endswith('/sync') for _, path, _ in requests)
            assert not any(path.endswith('/txt') for _, path, _ in requests)
            assert not errors, errors
            print('PASS dataset selection, pending-file gate, confirmation, manifest summary, upload/verification/failure labels, retry without browser reupload (all mocked APIs)')
            await browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
