"""Release checks and notices without contacting GitHub or publishing releases."""
import io
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch, MagicMock
import unittest

import test_app
from app import updates
from app.storage import atomic_json
from app.version import VERSION, REPOSITORY


def release(tag='v1.2.0', **fields):
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'body': 'Perbaikan fixture.', **fields}


def http_error(code, headers=None):
    return HTTPError(updates.API_URL, code, 'fixture', headers or {}, io.BytesIO())


class UpdateTests(unittest.TestCase):
    def setUp(self):
        test_app.AppTests.setUp(self)
        self.root = test_app.ROOT/'release-fixture'
        self.root.mkdir(exist_ok=True)
        atomic_json(self.root/'release-notes.json', {'version': VERSION, 'changes': ['Catatan fixture lokal.']})
        (self.root/'.cth-release.json').unlink(missing_ok=True)
        patch.object(updates, 'ROOT', self.root).start()
        self.addCleanup(patch.stopall)

    def installed(self, previous=None):
        atomic_json(self.root/'.cth-release.json', {'version': VERSION, 'previous_version': previous})

    def check(self):
        return self.client.post('/api/app/check-updates').json()

    def test_first_install_never_announces_update_or_requests_github(self):
        self.installed()
        with patch.object(updates, 'fetch_release') as network:
            value = self.client.get('/api/app/status').json()
            network.assert_not_called()
        self.assertEqual(value['version'], VERSION)
        self.assertIsNone(value['notice'])
        self.assertEqual(value['update']['status'], 'not_checked')
        self.assertEqual(json.loads((updates.DIRECTORY/'seen.json').read_text())['last_seen_version'], VERSION)

    def test_installation_metadata_alone_never_marks_update_seen(self):
        self.installed('1.0.0')
        self.assertFalse((updates.DIRECTORY/'seen.json').exists())
        value = self.client.get('/api/app/status').json()
        self.assertEqual(value['notice']['message'], f'Berhasil diperbarui ke versi {VERSION}')
        self.assertEqual(value['notice']['changes'], ['Catatan fixture lokal.'])
        self.assertEqual(json.loads((updates.DIRECTORY/'seen.json').read_text())['last_seen_version'], '1.0.0')

    def test_acknowledged_version_is_persisted_and_not_repeated(self):
        self.installed('1.0.0')
        self.assertTrue(self.client.get('/api/app/status').json()['notice'])
        result = self.client.post('/api/app/seen', json={'version': VERSION})
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(self.client.get('/api/app/status').json()['notice'])
        self.assertEqual(self.client.post('/api/app/seen', json={'version': '9.0.0'}).status_code, 409)

    def test_reinstall_same_version_and_downgrade_do_not_claim_upgrade(self):
        self.installed(VERSION)
        self.assertIsNone(self.client.get('/api/app/status').json()['notice'])
        atomic_json(updates.DIRECTORY/'seen.json', {'last_seen_version': '9.0.0'})
        self.assertIsNone(self.client.get('/api/app/status').json()['notice'])

    def test_checkout_and_inconsistent_installation_metadata_are_honest(self):
        value = self.client.get('/api/app/status').json()
        self.assertEqual(value['installation'], 'checkout')
        self.assertIsNone(value['notice'])
        atomic_json(self.root/'.cth-release.json', {'version': '9.0.0'})
        self.assertEqual(self.client.get('/api/app/status').status_code, 503)
        with patch.object(updates, 'fetch_release') as network:
            self.assertEqual(self.client.post('/api/app/check-updates').status_code, 503)
            network.assert_not_called()

    def test_update_available_contains_target_notes_and_fresh_clone_commands(self):
        self.installed()
        self.client.get("/api/app/status")
        with patch.object(updates, 'fetch_release', return_value=release()):
            value = self.check()
        self.assertEqual(value['status'], 'available')
        self.assertEqual(value['release']['version'], '1.2.0')
        commands = '\n'.join(value['release']['commands'])
        self.assertIn('git clone --branch v1.2.0 --single-branch', commands)
        self.assertNotIn('git pull', commands)
        self.assertEqual(value['release']['notes'], 'Perbaikan fixture.')
        served = self.client.get('/api/app/status').json()
        self.assertEqual(served['version'], VERSION)
        self.assertIsNone(served['notice'])

    def test_comparison_is_numeric_and_only_successful_equal_release_is_latest(self):
        for tag, expected in (('v1.10.0', 'available'), ('v'+VERSION, 'up_to_date'), ('v1.0.0', 'ahead')):
            (updates.DIRECTORY/'check.json').unlink(missing_ok=True)
            with patch.object(updates, 'fetch_release', return_value=release(tag)):
                self.assertEqual(self.check()['status'], expected)

    def test_no_release_network_timeout_forbidden_and_invalid_payload_are_not_latest(self):
        cases = [(http_error(404), 'no_release'), (URLError('offline'), 'network_error'),
                 (TimeoutError(), 'network_error'), (http_error(403), 'http_error')]
        for error, expected in cases:
            (updates.DIRECTORY/'check.json').unlink(missing_ok=True)
            with patch.object(updates, 'fetch_release', side_effect=error):
                self.assertEqual(self.check()['status'], expected)
        for data in (release('v2.0.0-beta', prerelease=True), release(draft=True), release('not-semver'), []):
            (updates.DIRECTORY/'check.json').unlink(missing_ok=True)
            with patch.object(updates, 'fetch_release', return_value=data):
                self.assertEqual(self.check()['status'], 'invalid_release')

    def test_checks_are_cached_and_failure_never_reuses_an_old_latest_claim(self):
        with patch.object(updates.time, 'time', return_value=1000), patch.object(updates, 'fetch_release', return_value=release('v'+VERSION)) as network:
            self.assertEqual(self.check()['status'], 'up_to_date')
            self.assertTrue(self.check()['cached'])
            self.assertEqual(network.call_count, 1)
        with patch.object(updates.time, 'time', return_value=2000), patch.object(updates, 'fetch_release', side_effect=URLError('offline')):
            value = self.check()
            self.assertEqual(value['status'], 'network_error')
            self.assertNotIn('release', value)

    def test_rate_limit_and_retry_after_are_respected(self):
        for code, headers in ((429, {'Retry-After': '120'}), (403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1120'}), (403, {'Retry-After': '120'})):
            (updates.DIRECTORY/'check.json').unlink(missing_ok=True)
            with patch.object(updates.time, 'time', return_value=1000), patch.object(updates, 'fetch_release', side_effect=http_error(code, headers)) as network:
                value = self.check()
                self.assertEqual(value['status'], 'rate_limited')
                self.assertEqual(value['next_check_at'], 1120)
                self.check()
                self.assertEqual(network.call_count, 1)

    def test_concurrent_checks_do_not_overlap(self):
        with updates.locked('check'), patch.object(updates, 'fetch_release') as network:
            self.assertEqual(self.check()['status'], 'checking')
            network.assert_not_called()

    def test_release_notes_are_bounded_and_url_is_constructed_from_trusted_repo(self):
        with patch.object(updates, 'fetch_release', return_value=release(body='x'*50000, html_url='https://attacker.invalid')):
            value = self.check()['release']
        self.assertEqual(len(value['notes']), 24000)
        self.assertTrue(value['notes_truncated'])
        self.assertEqual(value['url'], f'https://github.com/{REPOSITORY}/releases/tag/v1.2.0')

    def test_public_request_has_no_google_or_project_credentials_and_is_bounded(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.side_effect = [json.dumps(release()).encode(), b'']
        with patch.object(updates, 'build_opener', return_value=opener), patch('app.drive_auth.get_colab_credentials') as credentials:
            updates.fetch_release()
            credentials.assert_not_called()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, updates.API_URL)
        self.assertIsNone(request.data)
        self.assertEqual(set(key.lower() for key in request.headers), {'accept', 'user-agent', 'x-github-api-version'})
        self.assertEqual(opener.open.call_args.kwargs['timeout'], updates.TIMEOUT)
        response.read.side_effect = [b'x'*(updates.MAX_RESPONSE+1)]
        with patch.object(updates, 'build_opener', return_value=opener):
            with self.assertRaises(ValueError):
                updates.fetch_release()

    def test_redirects_are_not_followed(self):
        with self.assertRaises(HTTPError):
            updates.NoRedirect().redirect_request(MagicMock(full_url=updates.API_URL), io.BytesIO(), 302, 'redirect', {}, 'https://attacker.invalid')

    def test_version_numbers_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(json.loads((root/'frontend/package.json').read_text())['version'], VERSION)
        self.assertEqual(json.loads((root/'frontend/package-lock.json').read_text())['packages']['']['version'], VERSION)
        self.assertEqual(json.loads((root/'release-notes.json').read_text())['version'], VERSION)
        self.assertIn(f'**{VERSION}**', (root/'README.md').read_text())
        for command, label in ((root/'chanthecno-hub', 'CTH'), (root/'tools/install.py', 'CTH installer')):
            result = subprocess.run([sys.executable, str(command), '--version'], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), f'{label} {VERSION}')
        self.assertEqual(self.client.get('/api/hello').json()['version'], VERSION)
        self.assertEqual(self.client.get('/openapi.json').json()['info']['version'], VERSION)
