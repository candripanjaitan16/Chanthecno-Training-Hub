"""Akses berkas Colab dan cadangan model tanpa membuat kernel Jupyter."""
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

from app.colab_bridge import BridgeError
from app.storage import atomic_json
from app.drive_storage import upload_project_file

from app.paths import DATA_HOME

BACKEND = DATA_HOME
BACKUPS = BACKEND / 'workspace/training-web/backups'
RUN_PATTERN = r'\d{8}T\d{6}Z-[a-f0-9]{8}'
META_NAMES = {'run.json', 'complete.json', 'evaluation.json', 'status.json'}
ADAPTER_NAMES = {
    'adapter_config.json', 'adapter_model.safetensors', 'adapter_model.safetensors.index.json',
    'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json', 'added_tokens.json',
    'vocab.json', 'merges.txt', 'chat_template.jinja', 'generation_config.json', 'config.json',
}


class BackupError(RuntimeError):
    pass


def validate_job(job):
    if not re.fullmatch(r'[a-f0-9]{32}', str(job.get('job_id', ''))):
        raise BackupError('ID pekerjaan tidak valid.')
    if not isinstance(job.get('project_id'), int) or isinstance(job['project_id'], bool) or job['project_id'] <= 0:
        raise BackupError('ID proyek tidak valid.')


def http_get(bridge, path, **kwargs):
    response = bridge.http.get(bridge.url(path), timeout=(15, 90), allow_redirects=False, **kwargs)
    if response.status_code in {401, 403}:
        response.close()
        bridge.refresh_connection()
        response = bridge.http.get(bridge.url(path), timeout=(15, 90), allow_redirects=False, **kwargs)
    return response


def read_json_file(bridge, path, missing_ok=False):
    with http_get(bridge, 'api/contents/' + quote(path, safe='/'),
                  params={'content': 1, 'type': 'file', 'format': 'text'}) as response:
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code != 200:
            raise BackupError(f'Pembacaan status Colab gagal: HTTP {response.status_code}.')
        model = response.json()
    text = model.get('content')
    if model.get('type') != 'file' or not isinstance(text, str) or len(text) > 1024 * 1024:
        raise BackupError('Format berkas status Colab tidak valid.')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise BackupError('Isi status Colab bukan objek JSON.')
    return data


def read_remote_job(bridge, job):
    validate_job(job)
    for prefix in ('', 'content/'):
        path = f"{prefix}cth/projects/{job['project_id']}/jobs/{job['job_id']}/job.json"
        data = read_json_file(bridge, path, missing_ok=True)
        if data is not None:
            if data.get('job_id') != job['job_id'] or data.get('project_id') != job['project_id']:
                raise BackupError('Identitas status remote tidak sesuai pekerjaan.')
            pulse = read_json_file(bridge, path.replace('/job.json', '/heartbeat.json'), missing_ok=True)
            data['worker_running'] = heartbeat_fresh(pulse, job['job_id'])
            return data
    raise BackupError('Berkas status worker belum ditemukan. Tidak meluncurkan worker kedua otomatis.')


def heartbeat_fresh(pulse, job_id):
    if not pulse or pulse.get('job_id') != job_id:
        return False
    try:
        timestamp = datetime.fromisoformat(pulse['updated_at'])
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        return -30 <= age <= 90
    except (ValueError, KeyError, TypeError):
        return False


def write_stop_request(bridge, job):
    validate_job(job)
    for prefix in ('', 'content/'):
        path = f"{prefix}cth/projects/{job['project_id']}/jobs/{job['job_id']}"
        if read_json_file(bridge, path + '/job.json', missing_ok=True) is None:
            continue
        # PUT idempotent ke nama tetap; tidak mengeksekusi kode atau mematikan GPU.
        response = bridge.http.put(
            bridge.url('api/contents/' + quote(path + '/stop.request', safe='/')),
            json={'type': 'file', 'format': 'text', 'content': ''},
            timeout=(15, 90), allow_redirects=False,
        )
        try:
            if response.status_code not in {200, 201}:
                raise BackupError(f'Permintaan stop belum terkonfirmasi: HTTP {response.status_code}.')
        finally:
            response.close()
        return
    raise BackupError('Worker belum ditemukan; stop belum dikirim.')


def directory(bridge, path, missing_ok=False):
    with http_get(bridge, 'api/contents/' + quote(path, safe='/'),
                  params={'content': 1, 'type': 'directory'}) as response:
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code != 200:
            raise BackupError(f'Akses daftar model gagal: HTTP {response.status_code}.')
        model = response.json()
    if model.get('type') != 'directory' or not isinstance(model.get('content'), list):
        raise BackupError('Format daftar berkas model tidak valid.')
    return model['content']


def selected_files(items, adapter=False):
    chosen = []
    for item in items:
        name = item.get('name', '')
        allowed = name in (ADAPTER_NAMES if adapter else META_NAMES)
        if adapter and re.fullmatch(r'adapter_model-\d{5}-of-\d{5}\.safetensors', name):
            allowed = True
        if not allowed or item.get('type') != 'file':
            continue
        size = item.get('size')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupError(f'Ukuran berkas tidak valid: {name}.')
        chosen.append((('adapter/' if adapter else '') + name, item))
    if len({name for name, _ in chosen}) != len(chosen):
        raise BackupError('Ada nama berkas model duplikat.')
    return chosen


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download(bridge, remote, target, metadata):
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    size = 0
    digest = hashlib.sha256()
    with http_get(bridge, 'files/' + quote(remote, safe='/'), stream=True) as response:
        if response.status_code != 200:
            raise BackupError(f'Unduhan {target.name} gagal: HTTP {response.status_code}.')
        with partial.open('wb') as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > metadata['size']:
                    raise BackupError(f'Ukuran sumber berubah: {target.name}.')
                digest.update(chunk)
                handle.write(chunk)
    if size != metadata['size']:
        raise BackupError(f'Unduhan {target.name} belum lengkap. Salinan parsial tetap disimpan.')
    if metadata.get('hash_algorithm') == 'sha256' and metadata.get('hash') != digest.hexdigest():
        raise BackupError(f'Checksum sumber tidak cocok: {target.name}.')
    partial.replace(target)
    return {'path': str(target.name), 'size': size, 'sha256': digest.hexdigest()}


def local_backup(job):
    validate_job(job)
    work = BACKUPS / job['job_id']
    ready = work / 'local-ready.json'
    if not ready.is_file():
        return None
    data = json.loads(ready.read_text(encoding='utf-8'))
    archive = work / 'model.tar'
    if data.get('project_id') != job['project_id'] or data.get('job_id') != job['job_id'] or data.get('run_id') != job.get('run_id'):
        raise BackupError('Identitas cadangan lokal tidak sesuai.')
    if not archive.is_file() or archive.is_symlink() or sha256(archive) != data.get('sha256'):
        raise BackupError('Arsip lokal berubah atau belum lengkap; tidak menghapus salinannya.')
    return archive, data


def prepare_local_backup(job, bridge, progress):
    validate_job(job)
    if not re.fullmatch(RUN_PATTERN, str(job.get('run_id', ''))):
        raise BackupError('Run ID belum tersedia atau tidak valid.')
    work = BACKUPS / job['job_id']
    work.mkdir(parents=True, exist_ok=True)
    base = f"cth/projects/{job['project_id']}/training_results/{job['run_id']}"
    remote = None
    entries = None
    for prefix in ('', 'content/'):
        candidate = directory(bridge, prefix + base, missing_ok=True)
        if candidate is not None:
            remote, entries = prefix + base, candidate
            break
    if remote is None:
        raise BackupError('Folder model tidak ditemukan pada runtime. Tidak membuat runtime pengganti.')
    chosen = selected_files(entries) + selected_files(directory(bridge, remote + '/adapter'), adapter=True)
    names = {name for name, _ in chosen}
    required = {'run.json', 'complete.json', 'status.json', 'adapter/adapter_config.json', 'adapter/tokenizer_config.json'}
    if not required <= names or not any(name.endswith('.safetensors') for name in names):
        raise BackupError('Adapter atau penanda training selesai belum lengkap.')
    if 'adapter/tokenizer.json' not in names and not {'adapter/vocab.json', 'adapter/merges.txt'} <= names:
        raise BackupError('Kosakata tokenizer belum lengkap.')
    if any(name.endswith('.safetensors') and meta['size'] == 0 for name, meta in chosen):
        raise BackupError('Bobot adapter kosong.')
    total = sum(meta['size'] for _, meta in chosen)
    if shutil.disk_usage(work).free < total * 2 + 128 * 1024 * 1024:
        raise BackupError('Disk backend tidak cukup untuk adapter dan arsip. Tidak menghapus berkas lain.')
    records = []
    model = work / 'model'
    for index, (relative, metadata) in enumerate(chosen, 1):
        progress('downloading', f'Training selesai. Menyelamatkan berkas model {index}/{len(chosen)}: {relative}')
        record = download(bridge, remote + '/' + relative, model / relative, metadata)
        record['path'] = relative
        records.append(record)
    state = json.loads((model / 'status.json').read_text(encoding='utf-8'))
    run = json.loads((model / 'run.json').read_text(encoding='utf-8'))
    adapter = json.loads((model / 'adapter/adapter_config.json').read_text(encoding='utf-8'))
    settings = run.get('settings', {})
    if state.get('status') != 'completed' or state.get('run_id') != job['run_id']:
        raise BackupError('Penanda status run tidak sesuai completed.')
    if not settings.get('model_revision') or settings.get('model_id') != adapter.get('base_model_name_or_path'):
        raise BackupError('Referensi model dasar pada adapter belum cocok.')
    latest = selected_files(directory(bridge, remote)) + selected_files(directory(bridge, remote + '/adapter'), adapter=True)
    signature = lambda values: sorted((name, meta.get('size'), meta.get('last_modified')) for name, meta in values)
    if signature(chosen) != signature(latest):
        raise BackupError('Berkas model berubah selama penyalinan. Salinan lokal dipertahankan.')
    manifest = {
        'project_id': job['project_id'], 'job_id': job['job_id'], 'run_id': job['run_id'],
        'base_model': settings.get('model_id'), 'base_revision': settings['model_revision'],
        'files': records, 'source_backups': job.get('source_backups', []),
        'scope': 'Adapter LoRA, tokenizer dan metadata; bukan GGUF, model dasar, dataset.sqlite atau optimizer checkpoint.',
    }
    atomic_json(model / 'CTH-MODEL-BACKUP.json', manifest)
    archive = work / 'model.tar'
    partial = work / 'model.tar.part'
    with tarfile.open(partial, 'w') as target:
        for relative, _ in chosen:
            target.add(model / relative, arcname=relative, recursive=False)
        target.add(model / 'CTH-MODEL-BACKUP.json', arcname='CTH-MODEL-BACKUP.json', recursive=False)
    partial.replace(archive)
    data = {key: job[key] for key in ('project_id', 'job_id', 'run_id')}
    data.update(sha256=sha256(archive), size=archive.stat().st_size)
    atomic_json(work / 'local-ready.json', data)
    progress('local_ready', 'Adapter sudah disalin ke disk backend. Mengirim cadangan ke Drive…')
    return archive, data


def upload_local_backup(job, local, progress):
    archive, data = local
    receipt_file = archive.parent / 'drive-receipt.json'
    if receipt_file.is_file():
        receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
        if receipt.get('verified') is True and receipt.get('sha256') == data['sha256']:
            return receipt
    progress('uploading', 'Training selesai. Mengunggah dan memverifikasi adapter di Drive…')
    receipt = upload_project_file(job['project_id'], archive, 'models')
    if receipt.get('verified') is not True or receipt.get('sha256') != data['sha256'] or receipt.get('size') != data['size']:
        raise BackupError('Upload Drive belum terverifikasi. Arsip lokal tetap dipertahankan.')
    atomic_json(receipt_file, receipt)
    return receipt


def safe_error(error):
    if isinstance(error, (BackupError, BridgeError)):
        return str(error)[:500]
    if isinstance(error, (OSError, ValueError)):
        return 'Pemeriksaan berkas atau disk belum berhasil. Salinan lokal tidak dihapus.'
    return f'Koneksi atau penyimpanan belum berhasil ({type(error).__name__}). Tidak mengulang training otomatis.'
