"""Verify selected local/Drive versions and freeze the dataset contract for a run."""
import hashlib
import json
import os
import re
import time

from fastapi import HTTPException

from app.drive_storage import file_checksums, storage_connection, verify_project_file, find_subfolder, project_folder_id
from app.paths import WORKSPACE


def selected_rows(project_id, dataset_ids, source_receipts):
    from app.datasets import db, get_row
    if dataset_ids and source_receipts:
        raise HTTPException(400, 'Pilih dataset_ids atau source_receipts, bukan keduanya.')
    if source_receipts:
        conn = db()
        try:
            dataset_ids = []
            for receipt in source_receipts:
                row = conn.execute('SELECT id FROM cth_datasets WHERE project_id=? AND receipt_id=? AND deleted_at IS NULL', (project_id, receipt)).fetchone()
                if row is None:
                    raise HTTPException(409, 'Bukti Drive tidak terikat pada dataset aktif proyek ini.')
                dataset_ids.append(row['id'])
        finally:
            conn.close()
    if not dataset_ids or len(dataset_ids) > 500 or len(set(dataset_ids)) != len(dataset_ids):
        raise HTTPException(400, 'Pilih 1–500 dataset TXT unik untuk training baru.')
    return [get_row(project_id, source_id) for source_id in dataset_ids]


def verify_selection(project_id, dataset_ids=None, source_receipts=None):
    from app.datasets import local_path, update
    from app.colab_stop import receipt_path
    rows = selected_rows(project_id, dataset_ids, source_receipts)
    prepared = []
    for row in rows:
        if row['kind'] != 'txt' or row['drive_status'] != 'saved' or not row.get('receipt_id') or not row.get('drive_file_id'):
            raise HTTPException(409, 'Semua dataset terpilih wajib terverifikasi di Drive sebelum training.')
        path = local_path(row)
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError('Salinan lokal tidak aman atau tidak tersedia.')
            size, md5, sha = file_checksums(path)
            if path.is_symlink() or size != row['size'] or sha != row['sha256']:
                raise ValueError('Versi lokal berubah.')
            receipt_file = receipt_path(row['receipt_id'])
            if receipt_file.is_symlink() or receipt_file.stat().st_size > 65536:
                raise ValueError('Bukti tidak aman.')
            record = json.loads(receipt_file.read_text())
            receipt = record['upload']
            if (record.get('project_id') != project_id or record.get('source_id', row['id']) != row['id']
                    or receipt.get('verified') is not True or receipt.get('category') != 'datasets'
                    or receipt.get('file_id') != row['drive_file_id']
                    or receipt.get('sha256') != sha or receipt.get('size') != size):
                raise ValueError('Bukti tidak cocok dengan versi lokal.')
        except (ValueError, OSError, KeyError, TypeError):
            update(row, drive_status='failed', error='Versi lokal dan bukti Drive tidak cocok. Periksa sumber sebelum retry.', next_retry=time.time()+60)
            raise HTTPException(409, 'Versi lokal atau bukti Drive tidak cocok; worker tidak diluncurkan.') from None
        prepared.append((row, {'size': size, 'md5': md5, 'sha256': sha}))
    try:
        drive = storage_connection()
        folder = find_subfolder(drive, project_folder_id(project_id), 'datasets')
    except Exception:
        raise HTTPException(503, 'Drive belum dapat diperiksa. Periksa otorisasi akun sebelum training.') from None
    manifest = []
    for row, expected in prepared:
        try:
            remote = verify_project_file(drive, project_id, row['drive_file_id'], 'datasets', expected, folder)
        except Exception:
            update(row, drive_status='failed', error='Verifikasi ID/folder/ukuran/checksum Drive gagal. Salinan lokal dipertahankan.', next_retry=time.time()+60)
            raise HTTPException(409, 'Dataset Drive belum cocok/terjangkau. Worker tidak diluncurkan; periksa dan retry sinkronisasi.') from None
        manifest.append({
            'dataset_id': row['id'], 'project_id': project_id, 'source_mode': row.get('source_mode', 'filtered'),
            'file_id': remote['file_id'], 'folder_id': remote['folder_id'],
            'name': row['name'], 'drive_name': remote['name'],
            **expected, 'checksum_method': remote['checksum_method'],
        })
    return {'format': 1, 'project_id': project_id, 'datasets': manifest}


def freeze(job_id, manifest):
    directory = WORKSPACE / 'training-web/manifests'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (job_id + '.json')
    content = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    with path.open('xb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(content).hexdigest()


def load(job):
    if not re.fullmatch(r'[a-f0-9]{32}', str(job.get('job_id', ''))):
        raise ValueError('ID manifest tidak valid.')
    path = WORKSPACE / 'training-web/manifests' / (job['job_id'] + '.json')
    if path.is_symlink():
        raise ValueError('Manifest tidak aman.')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != job['dataset_manifest_sha256']:
        raise ValueError('Manifest dataset run berubah.')
    return json.loads(content)


def revalidate(job):
    manifest = load(job)
    ids = [item['dataset_id'] for item in manifest['datasets']]
    if verify_selection(job['project_id'], ids) != manifest:
        raise HTTPException(409, 'Dataset berubah sejak run diminta. Buat training baru dengan pilihan yang benar.')
    return manifest
