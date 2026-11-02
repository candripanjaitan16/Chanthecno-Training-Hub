"""Update UX with public release API mocks; no release publication or installation."""
import asyncio
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


async def main():
    server = subprocess.Popen(['python3', '-m', 'http.server', '8765', '--bind', '127.0.0.1', '--directory', str(ROOT/'frontend/dist')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            state = {'upgrade': False, 'seen': False, 'checks': 0, 'status': 'no_release', 'acks': 0, 'notes': '<script>window.untrustedRelease = true</script> Catatan fixture.'}
            errors = []
            acknowledged = asyncio.Event()
            page.on('pageerror', lambda value: errors.append(str(value)))
            changes = ['Pemeriksaan release publik dengan status yang jujur.', 'Data dan token pengguna dipertahankan.']
            async def route_handler(route):
                request = route.request
                url = urlsplit(request.url)
                if url.hostname != '127.0.0.1':
                    await route.abort()
                    return
                path = url.path
                if not path.startswith('/api/'):
                    await route.continue_()
                    return
                if path == '/api/app/status':
                    notice = {'version': '1.1.2', 'previous_version': '1.0.0', 'message': 'Berhasil diperbarui ke versi 1.1.2', 'changes': changes} if state['upgrade'] and not state['seen'] else None
                    value = {'version': '1.1.2', 'installation': 'installed', 'notice': notice, 'changes': changes, 'update': {'status': 'not_checked', 'message': 'Pembaruan belum diperiksa.'}}
                elif path == '/api/app/seen':
                    assert request.post_data_json == {'version': '1.1.2'}
                    state['seen'] = True
                    state['acks'] += 1
                    acknowledged.set()
                    value = {'seen': True, 'version': '1.1.2'}
                elif path == '/api/app/check-updates':
                    state['checks'] += 1
                    if state['status'] == 'available':
                        value = {'status': 'available', 'message': 'Pembaruan 1.2.0 tersedia.', 'release': {'version': '1.2.0', 'notes': state['notes'], 'url': 'https://github.com/candripanjaitan16/Chanthecno-Training-Hub/releases/tag/v1.2.0', 'commands': ['git clone --branch v1.2.0 --single-branch https://github.com/candripanjaitan16/Chanthecno-Training-Hub.git cth-update-v1.2.0', 'cd cth-update-v1.2.0', 'python3 tools/install.py', 'chanthecno-hub']}}
                    else:
                        messages = {'no_release': 'Belum ada GitHub Release stabil; status terbaru belum dapat ditentukan.', 'network_error': 'Jaringan gagal; status terbaru tidak diketahui.', 'rate_limited': 'Batas API GitHub tercapai.'}
                        value = {'status': state['status'], 'message': messages[state['status']]}
                elif path == '/api/local-state':
                    value = {'generation': 'initial'}
                elif path == '/api/projects/':
                    value = []
                elif path == '/api/connections/google-drive':
                    value = {'status': 'disconnected', 'message': 'Fixture offline'}
                else:
                    raise AssertionError('Unexpected API '+path)
                await route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
            await page.route('**/*', route_handler)
            await page.goto('http://127.0.0.1:8765/#deployments')
            await expect(page.locator('.cth-update-footer')).to_contain_text('CTH 1.1.2')
            await expect(page.get_by_role('dialog')).not_to_be_visible()
            assert state['checks'] == 0 and state['acks'] == 0
            await page.get_by_role('button', name='Periksa pembaruan', exact=True).click()
            dialog = page.get_by_role('dialog')
            await expect(dialog).to_contain_text('Belum ada GitHub Release stabil')
            await page.get_by_label('Tutup panel pembaruan').click()
            state['status'] = 'network_error'
            await page.get_by_role('button', name='Periksa pembaruan', exact=True).click()
            await expect(dialog).to_contain_text('status terbaru tidak diketahui')
            await page.get_by_label('Tutup panel pembaruan').click()
            state['status'] = 'available'
            await page.get_by_role('button', name='Periksa pembaruan', exact=True).click()
            await expect(dialog).to_contain_text('Pembaruan ke 1.2.0')
            await expect(dialog.get_by_label('Petunjuk pembaruan')).to_contain_text('git clone --branch v1.2.0')
            assert 'git pull' not in await dialog.get_by_label('Petunjuk pembaruan').inner_text()
            assert not await page.evaluate('Boolean(window.untrustedRelease)')
            state['notes'] = 'Peningkatan stabilitas aplikasi dan tampilan status. Catatan release ini adalah fixture pengujian.'
            await page.get_by_label('Tutup panel pembaruan').click()
            state['upgrade'] = True
            await page.reload()
            await expect(dialog).to_contain_text('Berhasil diperbarui ke versi 1.1.2')
            await expect(dialog.get_by_role('heading', name='Yang baru', exact=True)).to_be_visible()
            await expect(dialog).to_contain_text(changes[0])
            await asyncio.wait_for(acknowledged.wait(), timeout=5)
            await page.screenshot(path=str(ROOT/'docs/screenshots/updates-desktop.png'))
            assert state['acks'] == 1
            await page.reload()
            await expect(page.locator('.cth-update-footer')).to_contain_text('CTH 1.1.2')
            await expect(dialog).not_to_be_visible()
            assert state['acks'] == 1
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.get_by_label('Buka navigasi').click()
            await page.get_by_role('button', name='Periksa pembaruan', exact=True).click()
            await expect(dialog).to_contain_text('Pembaruan ke 1.2.0')
            await page.screenshot(path=str(ROOT/'docs/screenshots/updates-mobile.png'))
            assert await dialog.evaluate('(element) => element.getBoundingClientRect().width <= window.innerWidth')
            await page.get_by_label('Tutup panel pembaruan').click()
            state['status'] = 'rate_limited'
            await page.get_by_role('button', name='Periksa pembaruan', exact=True).click()
            await expect(dialog).to_contain_text('Batas API GitHub tercapai.')
            assert not errors, errors
            print('PASS update sidebar, no-release/network/rate-limit status, target/notes/instructions, first-install suppression, post-start notice/ack/reload, desktop/mobile, escaped release notes (all mocked)')
            await browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
