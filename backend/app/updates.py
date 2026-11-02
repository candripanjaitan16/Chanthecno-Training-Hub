"""Public GitHub release checks and per-user, post-start version notices."""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import fcntl
import json
import os
from pathlib import Path
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as HTTPRequest, build_opener, ProxyHandler, HTTPSHandler, HTTPRedirectHandler

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.paths import DATA_HOME
from app.storage import atomic_json
from app.version import VERSION, REPOSITORY, stable_version

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = DATA_HOME / 'updates'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
CHECK_INTERVAL = 15 * 60
ERROR_INTERVAL = 60
TIMEOUT = 8
MAX_RESPONSE = 256 * 1024
router = APIRouter(prefix='/api/app', tags=['Application updates'])


@contextmanager
def locked(name, blocking=True):
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DIRECTORY / (name + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def read_json(path):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_RESPONSE:
        raise ValueError('Metadata pembaruan tidak aman atau terlalu besar.')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Metadata pembaruan tidak valid.')
    return value


def local_notes():
    notes = read_json(ROOT / 'release-notes.json')
    if not notes or notes.get('version') != VERSION or not isinstance(notes.get('changes'), list):
        raise ValueError('Catatan versi lokal tidak cocok.')
    if len(notes['changes']) > 30 or any(not isinstance(item, str) or len(item) > 1000 for item in notes['changes']):
        raise ValueError('Catatan versi lokal tidak valid.')
    return notes['changes']


def installation():
    value = read_json(ROOT / '.cth-release.json')
    if value and value.get('version') != VERSION:
        raise ValueError('Metadata instalasi tidak cocok dengan versi yang berjalan.')
    return value


def running_status():
    # Installer probes use disposable data; only the real UI acknowledges the user version.
    installed = installation()
    notes = local_notes()
    with locked('seen'):
        path = DIRECTORY / 'seen.json'
        seen = read_json(path)
        if seen is None:
            previous = (installed or {}).get('previous_version')
            baseline = VERSION
            if previous:
                try:
                    if stable_version(previous) < stable_version(VERSION):
                        baseline = previous
                except ValueError:
                    pass
            seen = {'last_seen_version': baseline}
            atomic_json(path, seen)
        notice = None
        previous = seen.get('last_seen_version')
        if not isinstance(previous, str):
            raise ValueError('Versi terakhir yang dilihat tidak valid.')
        if installed and stable_version(previous) < stable_version(VERSION):
            notice = {'version': VERSION, 'previous_version': previous, 'changes': notes,
                      'message': f'Berhasil diperbarui ke versi {VERSION}'}
        elif installed and stable_version(previous) > stable_version(VERSION):
            atomic_json(path, {'last_seen_version': VERSION})
    try:
        cached = read_json(DIRECTORY / 'check.json')
        check = public_check(cached, cached=True)
    except (ValueError, KeyError, TypeError):
        check = public_check(None)
    return {'version': VERSION, 'installation': 'installed' if installed else 'checkout',
            'notice': notice, 'changes': notes, 'update': check}


class SeenVersion(BaseModel):
    version: str = Field(max_length=80)


@router.get('/status')
def status():
    try:
        return running_status()
    except (ValueError, OSError):
        raise HTTPException(503, 'Versi berjalan atau catatan pembaruan lokal belum dapat diperiksa.') from None


@router.post('/seen')
def acknowledge(body: SeenVersion):
    if body.version != VERSION:
        raise HTTPException(409, 'Versi UI berbeda. Muat ulang sebelum menandai pembaruan dilihat.')
    with locked('seen'):
        atomic_json(DIRECTORY / 'seen.json', {'last_seen_version': VERSION})
    return {'version': VERSION, 'seen': True}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        raise HTTPError(request.full_url, code, 'Redirect tidak diikuti', headers, fp)


def fetch_release():
    # urllib does not read .netrc or reuse Google sessions/authentication headers.
    ca_file = os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('SSL_CERT_FILE')
    context = ssl.create_default_context(cafile=ca_file)
    opener = build_opener(ProxyHandler(), HTTPSHandler(context=context), NoRedirect())
    request = HTTPRequest(API_URL, headers={'Accept': 'application/vnd.github+json',
        'User-Agent': f'Chanthecno-Training-Hub/{VERSION}', 'X-GitHub-Api-Version': '2022-11-28'})
    deadline = time.monotonic() + TIMEOUT
    with opener.open(request, timeout=TIMEOUT) as response:
        body = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('Pemeriksaan melewati batas waktu.')
            chunk = response.read(8192)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise ValueError('Respons GitHub terlalu besar.')
    return json.loads(body.decode('utf-8'))


def release_info(data):
    if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
        raise ValueError('Respons bukan release stabil.')
    tag = data.get('tag_name')
    stable_version(tag)
    notes = data.get('body') or ''
    if not isinstance(notes, str):
        raise ValueError('Catatan release tidak valid.')
    return {'tag': tag, 'version': tag.removeprefix('v'), 'notes': notes[:24000],
            'notes_truncated': len(notes) > 24000,
            'url': f'https://github.com/{REPOSITORY}/releases/tag/{quote(tag, safe="")}',
            'commands': [f'git clone --branch {tag} --single-branch https://github.com/{REPOSITORY}.git cth-update-{tag}',
                         f'cd cth-update-{tag}', 'python3 tools/install.py', 'chanthecno-hub']}


def rate_limit_time(headers, now):
    retry = now + CHECK_INTERVAL
    try:
        raw = headers.get('Retry-After')
        if raw:
            retry = now + int(raw) if raw.isdigit() else parsedate_to_datetime(raw).timestamp()
        elif headers.get('X-RateLimit-Reset'):
            retry = float(headers['X-RateLimit-Reset'])
    except (ValueError, TypeError, OverflowError):
        pass
    return max(now + ERROR_INTERVAL, min(retry, now + 86400))


def public_check(record, cached=False):
    if not record:
        return {'status': 'not_checked', 'message': 'Pembaruan belum diperiksa.', 'installed_version': VERSION}
    result = {**record, 'installed_version': VERSION, 'cached': cached}
    release = record.get('release')
    if record['status'] == 'ok' and release:
        target = stable_version(release['version'])
        current = stable_version(VERSION)
        if target > current:
            result.update(status='available', message=f"Pembaruan {release['version']} tersedia.")
        elif target == current:
            result.update(status='up_to_date', message='Versi terpasang sama dengan release stabil terbaru yang diperiksa.')
        else:
            result.update(status='ahead', message='Versi berjalan lebih baru daripada release stabil yang diperiksa.')
    return result


def check_updates():
    try:
        with locked('check', blocking=False):
            now = time.time()
            try:
                saved = read_json(DIRECTORY / 'check.json')
            except ValueError:
                saved = None
            if saved and now < saved.get('next_check_at', 0):
                return public_check(saved, cached=True)
            record = {'checked_at': now, 'next_check_at': now + CHECK_INTERVAL, 'status': 'ok'}
            try:
                record['release'] = release_info(fetch_release())
            except HTTPError as error:
                headers = error.headers or {}
                if error.code == 404:
                    record.update(status='no_release', message='Belum ada GitHub Release stabil yang tersedia; status terbaru belum dapat ditentukan.')
                elif error.code == 429 or (error.code == 403 and (headers.get('X-RateLimit-Remaining') == '0' or headers.get('Retry-After'))):
                    record.update(status='rate_limited', message='Batas API GitHub tercapai. Tunggu sebelum memeriksa lagi.', next_check_at=rate_limit_time(headers, now))
                else:
                    record.update(status='http_error', message=f'GitHub belum dapat diperiksa (HTTP {error.code}). Status terbaru tidak diketahui.', next_check_at=now + ERROR_INTERVAL)
                error.close()
            except (TimeoutError, URLError, OSError):
                record.update(status='network_error', message='Jaringan atau timeout menghalangi pemeriksaan. Status terbaru tidak diketahui.', next_check_at=now + ERROR_INTERVAL)
            except (ValueError, TypeError, UnicodeError):
                record.update(status='invalid_release', message='Respons atau nomor release stabil belum dapat dibandingkan.', next_check_at=now + ERROR_INTERVAL)
            atomic_json(DIRECTORY / 'check.json', record)
            return public_check(record)
    except BlockingIOError:
        return {'status': 'checking', 'message': 'Pemeriksaan lain masih berjalan.', 'installed_version': VERSION}


@router.post('/check-updates')
def check():
    try:
        installation()
        return check_updates()
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, 'Status pembaruan belum dapat diperiksa atau disimpan.') from None
