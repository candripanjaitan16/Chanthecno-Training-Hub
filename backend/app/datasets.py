"""Dataset persisten di disk backend, metadata SQLite, sinkronisasi Drive."""
import codecs
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from googleapiclient.http import MediaIoBaseDownload
from pydantic import BaseModel, Field
from typing import Literal

from app.database import get_db
from app.storage import MAX_UPLOAD_BYTES
from app.storage import atomic_json
from app.colab_stop import local_only, project_row, verify_receipts
from app.drive_storage import storage_connection, upload_project_file, file_checksums

router = APIRouter(prefix='/api/datasets', tags=['Datasets'])
from app.paths import DATA_HOME

BASE = DATA_HOME
DATA = BASE / 'workspace/datasets'
RECEIPTS = BASE / 'workspace/stop-save/receipts'
THREADS = set()
THREAD_GUARD = threading.Lock()
SYNC_SLOT = threading.Semaphore(1)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS cth_datasets (
    id TEXT PRIMARY KEY, project_id INTEGER NOT NULL,
    kind TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
    sha256 TEXT NOT NULL, url TEXT,
    drive_status TEXT NOT NULL DEFAULT 'pending',
    receipt_id TEXT, drive_file_id TEXT, error TEXT,
    next_retry REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, deleted_at TEXT,
    UNIQUE(project_id, kind, sha256)
);
'''


def db():
    conn = get_db()
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute(SCHEMA)
    if 'source_mode' not in {row[1] for row in conn.execute('PRAGMA table_info(cth_datasets)')}:
        conn.execute("ALTER TABLE cth_datasets ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'filtered'")
    conn.commit()
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def valid_name(name):
    if not name or len(name) > 180 or name in {'.', '..'} or any(c in name for c in '/\\') or any(ord(c) < 32 for c in name):
        raise HTTPException(400, 'Nama berkas tidak valid.')
    if not name.lower().endswith('.txt'):
        raise HTTPException(400, 'Pilih berkas TXT.')
    return name


def get_row(project_id, source_id, include_deleted=False):
    if not re.fullmatch(r'[a-f0-9]{32}', source_id):
        raise HTTPException(404, 'Sumber tidak ditemukan.')
    conn = db()
    try:
        row = conn.execute('SELECT * FROM cth_datasets WHERE id=? AND project_id=?' + ('' if include_deleted else ' AND deleted_at IS NULL'), (source_id, project_id)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, 'Sumber tidak ditemukan pada proyek ini.')
    return dict(row)


def local_path(row):
    if not re.fullmatch(r'[a-f0-9]{32}', row['id']):
        raise HTTPException(400, 'ID sumber tidak valid.')
    name = valid_name(row['name']) if row['kind'] == 'txt' else 'cth-url-sources.json'
    path = DATA / str(int(row['project_id'])) / row['id'] / name
    if path.resolve() != path:
        raise HTTPException(400, 'Lokasi penyimpanan tidak aman.')
    return path


def public(row):
    analysis = None
    cached = local_path(row).parent/'analysis.json'
    if cached.is_file() and not cached.is_symlink() and cached.stat().st_size <= 65536:
        try:
            value = json.loads(cached.read_text())
            if isinstance(value, dict) and value.get('sha256') == row['sha256'] and value.get('source_mode') == row.get('source_mode', 'filtered'):
                analysis = value
        except (ValueError, OSError):
            pass
    return {
        'id': row['id'], 'project_id': row['project_id'], 'type': row['kind'],
        'name': row['name'], 'size': row['size'], 'sha256': row['sha256'],
        'persisted': True, 'local_saved': local_path(row).is_file(),
        'drive_status': row['drive_status'], 'receipt_id': row['receipt_id'],
        'drive_file_id': row['drive_file_id'], 'error': row['error'],
        'created_at': row['created_at'], 'source_mode': row.get('source_mode', 'filtered'), 'analysis': analysis,
    }


def update(row, **values):
    allowed = {'drive_status', 'receipt_id', 'drive_file_id', 'error', 'next_retry', 'deleted_at', 'source_mode'}
    if not values.keys() <= allowed:
        raise ValueError('Kolom update tidak diizinkan.')
    conn = db()
    try:
        conn.execute('UPDATE cth_datasets SET ' + ','.join(key+'=?' for key in values) + ' WHERE id=? AND project_id=?', (*values.values(), row['id'], row['project_id']))
        conn.commit()
    finally:
        conn.close()


def import_receipts(project_id):
    # Mengimpor metadata bukti upload lama; tidak mengunggah ulang TXT ke Drive.
    if not RECEIPTS.is_dir():
        return
    paths = sorted(RECEIPTS.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    conn = db()
    try:
        for path in paths:
            if path.is_symlink() or path.stat().st_size > 65536 or not re.fullmatch(r'[a-f0-9]{32}', path.stem):
                continue
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(record, dict) or not isinstance(record.get('upload'), dict):
                    continue
                item = record['upload']
                if record.get('project_id') != project_id or item.get('verified') is not True:
                    continue
                if item.get('category', 'datasets') != 'datasets':
                    continue
                name = re.sub(r'^[a-f0-9]{12}-', '', item['name'])
                valid_name(name)
                if not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '')) or not re.fullmatch(r'[A-Za-z0-9_-]+', item.get('file_id', '')):
                    continue
                size = item.get('size')
                if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                    continue
                conn.execute('''INSERT OR IGNORE INTO cth_datasets
                    (id,project_id,kind,name,size,sha256,drive_status,receipt_id,drive_file_id,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (uuid4().hex, project_id, 'txt', name, size, item['sha256'], 'saved', path.stem, item['file_id'],
                     datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()))
            except (ValueError, KeyError, TypeError, HTTPException):
                continue
        conn.commit()
    finally:
        conn.close()


def store_file(project_id, name, kind, size, digest, staged, url=None):
    conn = db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT * FROM cth_datasets WHERE project_id=? AND kind=? AND sha256=?', (project_id, kind, digest)).fetchone()
        if previous:
            row = dict(previous)
            if kind == 'url' and row.get('deleted_at') and conn.execute("SELECT COUNT(*) FROM cth_datasets WHERE project_id=? AND kind='url' AND deleted_at IS NULL", (project_id,)).fetchone()[0] >= 50:
                raise HTTPException(400, 'Maksimal 50 alamat URL aktif per proyek.')
            target = local_path(row)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not target.is_file():
                staged.replace(target)
            conn.execute('UPDATE cth_datasets SET deleted_at=NULL WHERE id=?', (row['id'],))
            duplicate = True
        else:
            if kind == 'url' and conn.execute("SELECT COUNT(*) FROM cth_datasets WHERE project_id=? AND kind='url' AND deleted_at IS NULL", (project_id,)).fetchone()[0] >= 50:
                raise HTTPException(400, 'Maksimal 50 alamat URL aktif per proyek.')
            row = {'id': uuid4().hex, 'project_id': project_id, 'kind': kind, 'name': name}
            target = local_path(row)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staged.replace(target)
            conn.execute('''INSERT INTO cth_datasets(id,project_id,kind,name,size,sha256,url,created_at)
                            VALUES(?,?,?,?,?,?,?,?)''',
                         (row['id'],project_id,kind,name,size,digest,url,now()))
            duplicate = False
        conn.commit()
        return get_row(project_id,row['id']), duplicate
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_sync(row, force=False):
    if row['drive_status'] == 'saved' and row.get('receipt_id') and not force:
        return
    if not local_path(row).is_file():
        return
    if not force and row.get('next_retry', 0) > time.time():
        return
    with THREAD_GUARD:
        if row['id'] in THREADS:
            return
        THREADS.add(row['id'])
    try:
        threading.Thread(target=sync_one, args=(row['project_id'],row['id'],force), daemon=True).start()
    except Exception:
        with THREAD_GUARD:
            THREADS.discard(row['id'])
        raise


def sync_one(project_id, source_id, force=False):
    try:
        with SYNC_SLOT:
            DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (DATA / f'sync-{source_id}.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                row = get_row(project_id, source_id)
                if row['drive_status'] == 'saved' and row.get('receipt_id') and not force:
                    return
                project_row(project_id)
                update(row, drive_status='syncing', error=None)
                size, md5, sha = file_checksums(local_path(row))
                if size != row['size'] or sha != row['sha256']:
                    raise ValueError('Versi lokal berubah; tidak mengunggah versi yang tidak cocok.')
                receipt = upload_project_file(project_id, local_path(row), 'datasets',
                    on_status=lambda phase: update(row, drive_status=phase, error=None))
                if receipt.get('verified') is not True or receipt.get('sha256') != row['sha256'] or receipt.get('size') != row['size']:
                    raise RuntimeError('Upload belum terverifikasi.')
                receipt_id = uuid4().hex
                atomic_json(RECEIPTS / (receipt_id + '.json'), {
                    'project_id':project_id, 'upload':receipt,
                    'original_name':row['name'], 'source_type':row['kind'], 'source_id':row['id'],
                })
                update(row, drive_status='saved', receipt_id=receipt_id,
                       drive_file_id=receipt['file_id'], error=None, next_retry=0)
    except HTTPException as error:
        if error.status_code != 404:
            try:
                update({'id':source_id,'project_id':project_id}, drive_status='failed', error='Sinkronisasi Drive belum berhasil. Salinan lokal tetap disimpan.', next_retry=time.time()+60)
            except Exception:
                pass
    except Exception:
        try:
            update({'id':source_id,'project_id':project_id}, drive_status='failed', error='Sinkronisasi Drive belum berhasil. Salinan lokal tetap disimpan.', next_retry=time.time()+60)
        except Exception:
            pass
    finally:
        with THREAD_GUARD:
            THREADS.discard(source_id)


def resume_syncs():
    conn = db()
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM cth_datasets WHERE deleted_at IS NULL AND drive_status!='saved'")]
    finally:
        conn.close()
    for row in rows:
        ensure_sync(row)


router.add_event_handler('startup', resume_syncs)


class AnalysisSelection(BaseModel):
    dataset_ids: list[str] = Field(min_length=1, max_length=500)


@router.post('/projects/{project_id}/analysis')
def analyze_sources(project_id: int, body: AnalysisSelection, request: Request):
    local_only(request)
    project_row(project_id)
    from app.source_analysis import analyze_selection
    return analyze_selection(project_id, body.dataset_ids)


class SourceMode(BaseModel):
    source_mode: Literal['filtered', 'training_only', 'facts_only']


@router.patch('/projects/{project_id}/{source_id}/source-mode')
def source_mode(project_id: int, source_id: str, body: SourceMode, request: Request):
    local_only(request)
    project_row(project_id)
    row = get_row(project_id, source_id)
    if row['kind'] != 'txt':
        raise HTTPException(400, 'Peran sumber hanya tersedia untuk TXT.')
    update(row, source_mode=body.source_mode)
    from app.source_analysis import analyze_row
    analyze_row(get_row(project_id, source_id))
    return public(get_row(project_id, source_id))


@router.get('/projects/{project_id}')
def list_sources(project_id: int, request: Request):
    local_only(request)
    project_row(project_id)
    import_receipts(project_id)
    conn = db()
    try:
        rows = [dict(row) for row in conn.execute('SELECT * FROM cth_datasets WHERE project_id=? AND deleted_at IS NULL ORDER BY created_at DESC', (project_id,))]
    finally:
        conn.close()
    for row in rows:
        ensure_sync(row)
    return [public(row) for row in rows]


@router.post('/projects/{project_id}/txt', status_code=201)
async def upload_txt(project_id: int, request: Request):
    local_only(request)
    project_row(project_id)
    name = valid_name(unquote(request.headers.get('x-cth-source-name', 'data.txt')))
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    decoder = codecs.getincrementaldecoder('utf-8')(errors='strict')
    digest = hashlib.sha256()
    size = 0
    has_text = False
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=DATA, prefix='.upload-', suffix='.part', delete=False) as handle:
            staged = Path(handle.name)
            os.chmod(staged,0o600)
            async for chunk in request.stream():
                if not chunk:
                    continue
                if shutil.disk_usage(DATA).free < len(chunk)+64*1024*1024:
                    raise HTTPException(507,'Disk backend tidak cukup untuk menyimpan TXT.')
                text = decoder.decode(chunk)
                if '\x00' in text:
                    raise HTTPException(400,'TXT mengandung NUL biner.')
                has_text = has_text or bool(text.lstrip('\ufeff').strip())
                digest.update(chunk)
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, 'TXT melebihi batas 64 MiB per berkas.')
                handle.write(chunk)
            tail = decoder.decode(b'',final=True)
            has_text = has_text or bool(tail.lstrip('\ufeff').strip())
            handle.flush()
            os.fsync(handle.fileno())
        if not size or not has_text:
            raise HTTPException(400,'TXT kosong atau hanya berisi spasi.')
        row, duplicate = store_file(project_id,name,'txt',size,digest.hexdigest(),staged)
        from app.source_analysis import analyze_row
        analyze_row(row)
        ensure_sync(row)
        return {'dataset':public(get_row(project_id, row['id'])),'duplicate':duplicate,'message':'TXT sudah tersimpan di disk backend. Status Drive diperiksa terpisah.'}
    except UnicodeError:
        raise HTTPException(400,'Berkas harus berupa TXT UTF-8 yang valid.') from None
    finally:
        if staged and staged.exists():
            staged.unlink()


class URLSource(BaseModel):
    url: str = Field(min_length=1,max_length=2048)


@router.post('/projects/{project_id}/url', status_code=201)
def add_url(project_id: int, body: URLSource, request: Request):
    local_only(request)
    project_row(project_id)
    address = body.url.strip()
    if not re.match(r'^https?://',address,re.I):
        address = 'https://' + address
    try:
        parsed = urlsplit(address)
        _ = parsed.port
        if parsed.scheme not in {'http','https'} or not parsed.hostname or '.' not in parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        if any(ord(c)<32 for c in address) or any(c.isspace() for c in address):
            raise ValueError()
        normalized = urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),parsed.path or '/',parsed.query,parsed.fragment))
    except ValueError:
        raise HTTPException(400,'Masukkan domain atau URL HTTP/HTTPS tanpa username/password.') from None
    content = json.dumps([normalized],ensure_ascii=False).encode('utf-8')
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.NamedTemporaryFile(dir=DATA,delete=False) as handle:
        staged = Path(handle.name)
        handle.write(content);handle.flush();os.fsync(handle.fileno())
    try:
        row, duplicate = store_file(project_id,normalized,'url',len(content),hashlib.sha256(content).hexdigest(),staged,normalized)
        ensure_sync(row)
        return {'dataset':public(row),'duplicate':duplicate}
    finally:
        if staged.exists():staged.unlink()


@router.post('/projects/{project_id}/{source_id}/sync')
def retry_sync(project_id: int, source_id: str, request: Request):
    local_only(request);project_row(project_id)
    row = get_row(project_id,source_id)
    update(row,next_retry=0)
    ensure_sync(get_row(project_id,source_id),force=True)
    return public(get_row(project_id,source_id))


@router.delete('/projects/{project_id}/{source_id}')
def remove_source(project_id: int, source_id: str, request: Request):
    local_only(request);project_row(project_id)
    row = get_row(project_id,source_id)
    update(row,deleted_at=now())
    return {'status':'removed','message':'Sumber dihapus dari daftar. Salinan disk dan Drive tidak dihapus.'}


@router.post('/projects/{project_id}/{source_id}/restore')
def restore_source(project_id: int, source_id: str, request: Request):
    local_only(request);project_row(project_id)
    row = get_row(project_id,source_id)
    target = local_path(row)
    if target.is_file():return public(row)
    if row['kind'] == 'url':
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        target.write_text(json.dumps([row['url']],ensure_ascii=False),encoding='utf-8')
        return public(row)
    if row['drive_status'] != 'saved' or not row.get('receipt_id'):
        raise HTTPException(409,'Salinan lokal tidak ada dan bukti Drive belum tersedia.')
    verify_receipts(project_id,[row['receipt_id']])
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if shutil.disk_usage(target.parent).free < row['size']+64*1024*1024:
        raise HTTPException(507,'Disk tidak cukup untuk mengunduh sumber dari Drive.')
    temporary = None
    try:
        drive = storage_connection()
        with tempfile.NamedTemporaryFile(dir=target.parent,delete=False,suffix='.part') as handle:
            temporary=Path(handle.name)
            downloader=MediaIoBaseDownload(handle,drive.files().get_media(fileId=row['drive_file_id']),chunksize=1024*1024)
            done=False
            while not done:
                _,done=downloader.next_chunk(num_retries=2)
                if handle.tell()>row['size']:
                    raise HTTPException(502,'Ukuran sumber Drive berubah.')
            handle.flush();os.fsync(handle.fileno())
        digest=hashlib.sha256()
        with temporary.open('rb') as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b''):digest.update(chunk)
        if temporary.stat().st_size!=row['size'] or digest.hexdigest()!=row['sha256']:
            raise HTTPException(502,'Checksum sumber Drive tidak cocok; berkas tidak dipakai.')
        temporary.replace(target)
        return public(row)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503,'Sumber belum dapat diunduh dari Drive. Jangan mengunggah ulang sebelum memeriksa aksesnya.') from None
    finally:
        if temporary and temporary.exists():temporary.unlink()


@router.get('/projects/{project_id}/{source_id}/preview')
def preview_source(project_id: int, source_id: str, request: Request):
    local_only(request);project_row(project_id)
    row=get_row(project_id,source_id)
    if row['kind']!='txt':raise HTTPException(400,'Pratinjau hanya untuk TXT.')
    path=local_path(row)
    if not path.is_file():raise HTTPException(409,'Unduh salinan lokal dari Drive sebelum pratinjau.')
    try:
        with path.open(encoding='utf-8-sig') as handle:text=handle.read(12001)
    except (OSError,UnicodeError):raise HTTPException(503,'TXT belum dapat dibaca.') from None
    return {'id':source_id,'name':row['name'],'content':text[:12000],'truncated':len(text)>12000}
