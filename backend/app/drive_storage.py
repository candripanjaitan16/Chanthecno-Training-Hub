import fcntl
import json
import hashlib
import re
from pathlib import Path
from uuid import uuid4

from googleapiclient.http import MediaFileUpload

from app.paths import WORKSPACE
from app.storage import atomic_json
from app.database import get_db
from app.drive_auth import connect_drive


def storage_connection():
    return connect_drive()


def project_folder_id(project_id):
    conn = get_db()
    try:
        row = conn.execute('SELECT drive_folder_id FROM projects WHERE id=?', (project_id,)).fetchone()
    finally:
        conn.close()
    if not row or not re.fullmatch(r'[A-Za-z0-9_-]+', row['drive_folder_id'] or ''):
        raise RuntimeError('Folder Drive proyek belum valid.')
    return row['drive_folder_id']


def require_writable_folder(drive, folder_id):
    folder = drive.files().get(fileId=folder_id, fields='mimeType,trashed,capabilities(canAddChildren)').execute()
    if folder.get('trashed') or folder.get('mimeType') != 'application/vnd.google-apps.folder' or not folder.get('capabilities', {}).get('canAddChildren'):
        raise RuntimeError('Folder tidak tersedia atau tidak dapat ditulis.')


def find_subfolder(drive, parent_id, folder_name):
    if folder_name not in {'datasets', 'notebooks', 'training_results', 'models'}:
        raise ValueError('Kategori tidak diizinkan.')
    require_writable_folder(drive, parent_id)
    matches = []
    token = None
    while True:
        response = drive.files().list(q=f"'{parent_id}' in parents and trashed=false and name='{folder_name}' and mimeType='application/vnd.google-apps.folder'",
                                      fields='nextPageToken,files(id)', pageSize=100, pageToken=token).execute()
        matches.extend(response.get('files', []))
        token = response.get('nextPageToken')
        if not token:
            break
    if len(matches) != 1:
        raise RuntimeError('Subfolder tidak ditemukan atau ada duplikat. Periksa folder proyek.')
    folder_id = matches[0]['id']
    require_writable_folder(drive, folder_id)
    return folder_id


def file_checksums(path):
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return size, md5.hexdigest(), sha256.hexdigest()


FILE_FIELDS = 'id,name,size,md5Checksum,sha256Checksum,parents,trashed,appProperties'


def verify_project_file(drive, project_id, file_id, category, expected, folder=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(file_id)):
        raise ValueError('ID file Drive tidak valid.')
    folder = folder or find_subfolder(drive, project_folder_id(project_id), category)
    remote = drive.files().get(fileId=file_id, fields=FILE_FIELDS).execute()
    if (remote.get('id') != file_id or remote.get('trashed')
            or folder not in remote.get('parents', [])
            or int(remote.get('size', -1)) != expected['size']):
        raise ValueError('ID, folder proyek, atau ukuran file Drive tidak cocok.')
    sha = remote.get('sha256Checksum')
    md5 = remote.get('md5Checksum')
    if sha and sha != expected['sha256']:
        raise ValueError('Checksum SHA-256 Drive tidak cocok.')
    if md5 and expected.get('md5') and md5 != expected['md5']:
        raise ValueError('Checksum MD5 Drive tidak cocok.')
    if not sha and not (md5 and expected.get('md5')):
        raise ValueError('Drive tidak menyediakan checksum konten yang dapat diverifikasi.')
    properties = remote.get('appProperties', {})
    if properties.get('cth_project_id', str(project_id)) != str(project_id):
        raise ValueError('Identitas proyek file Drive tidak cocok.')
    return {
        'status': 'saved', 'verified': True, 'project_id': project_id,
        'category': category, 'file_id': file_id, 'folder_id': folder,
        'name': remote['name'], 'size': expected['size'],
        'sha256': expected['sha256'], 'md5': expected.get('md5'),
        'checksum_method': 'sha256Checksum' if sha else 'md5Checksum',
    }


def existing_dataset(drive, folder, project_id, expected):
    query = (
        f"'{folder}' in parents and trashed=false and "
        f"appProperties has {{ key='cth_sha256' and value='{expected['sha256']}' }} and "
        f"appProperties has {{ key='cth_project_id' and value='{project_id}' }}"
    )
    page = None
    while True:
        response = drive.files().list(q=query, fields='nextPageToken,files(id)', pageSize=100, pageToken=page).execute()
        for item in response.get('files', []):
            # Metadata tags locate candidates; only server content checksums prove a match.
            try:
                return verify_project_file(drive, project_id, item['id'], 'datasets', expected, folder)
            except ValueError:
                continue
        page = response.get('nextPageToken')
        if not page:
            return None


def send_file(drive, project_id, path, category, expected, folder, file_id=None, on_status=None):
    body = {'name': path.name if category == 'datasets' else uuid4().hex[:12] + '-' + path.name,
            'parents': [folder], 'appProperties': {'cth_sha256': expected['sha256'], 'cth_project_id': str(project_id)}}
    if file_id:
        body['id'] = file_id
    if on_status:
        on_status('syncing')
    media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024)
    try:
        request = drive.files().create(body=body, media_body=media, fields='id')
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=2)
    finally:
        media.stream().close()
    if file_id and response.get('id') != file_id:
        raise ValueError('ID upload berbeda dari ID yang dicadangkan.')
    if on_status:
        on_status('verifying')
    return verify_project_file(drive, project_id, response['id'], category, expected, folder)


def upload_project_file(project_id, local_path, category, on_status=None):
    path = Path(local_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Berkas upload tidak valid.')
    before = file_checksums(path)
    if not before[0]:
        raise ValueError('Berkas kosong.')
    expected = {'size': before[0], 'md5': before[1], 'sha256': before[2]}
    if on_status:
        on_status('verifying')
    drive = storage_connection()
    folder = find_subfolder(drive, project_folder_id(project_id), category)
    if category != 'datasets':
        receipt = send_file(drive, project_id, path, category, expected, folder, on_status=on_status)
    else:
        journal = WORKSPACE / 'drive-uploads'
        journal.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = f'{project_id}-{before[2]}'
        with (journal / (key + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = journal / (key + '.json')
            if record.is_symlink():
                raise ValueError('Catatan upload tidak aman.')
            saved = json.loads(record.read_text()) if record.is_file() else {}
            file_id = saved.get('file_id')
            receipt = None
            if file_id:
                try:
                    receipt = verify_project_file(drive, project_id, file_id, category, expected, folder)
                except Exception as error:
                    if getattr(getattr(error, 'resp', None), 'status', None) != 404:
                        raise
            if receipt is None:
                receipt = existing_dataset(drive, folder, project_id, expected)
            if receipt is None:
                if not file_id:
                    file_id = drive.files().generateIds(count=1, space='drive', type='files').execute()['ids'][0]
                    atomic_json(record, {'file_id': file_id, 'project_id': project_id, **expected})
                try:
                    receipt = send_file(drive, project_id, path, category, expected, folder, file_id, on_status)
                except Exception as error:
                    if getattr(getattr(error, 'resp', None), 'status', None) != 409:
                        raise
                    receipt = verify_project_file(drive, project_id, file_id, category, expected, folder)
            atomic_json(record, receipt)
    if file_checksums(path) != before:
        raise ValueError('Berkas lokal berubah selama sinkronisasi. Upload belum dianggap terverifikasi.')
    return receipt
