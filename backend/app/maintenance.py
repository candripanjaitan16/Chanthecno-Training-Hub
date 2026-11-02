"""Offline reset and restore; never connects to Google or removes credentials."""
import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from app.paths import CONFIG_HOME, DATA_HOME, STATE_HOME, prepare
from app.storage import atomic_json, authentication_name

TERMINAL = {'idle', 'completed', 'failed', 'stopped', 'interrupted', 'disconnected', 'not_found'}


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def safe_tree(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Direktori tidak aman atau tidak tersedia.')
    entries = []
    for path in root.rglob('*'):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError('Symlink atau jenis berkas tidak didukung; operasi dibatalkan.')
        if authentication_name(path.name):
            raise ValueError('Berkas autentikasi ditemukan di direktori data; operasi dibatalkan tanpa membacanya.')
        if path.is_file():
            entries.append(path)
    return entries


def validate_roots():
    source = Path(__file__).resolve().parents[2]
    forbidden = [Path.home(), Path('/'), CONFIG_HOME, STATE_HOME, source]
    for other in forbidden:
        if DATA_HOME == other or DATA_HOME in other.parents or (other in DATA_HOME.parents and other != Path.home() and other != Path('/')):
            raise ValueError('Direktori data bertumpang tindih dengan konfigurasi, source, atau cadangan.')


def require_inactive():
    candidates = [DATA_HOME / 'colab_runtime_state.json']
    candidates.extend((DATA_HOME / 'workspace').glob('**/current.json'))
    candidates.extend((DATA_HOME / 'workspace').glob('**/restore-*.json'))
    for path in candidates:
        if path.is_symlink():
            raise ValueError('Status berupa symlink; pemeliharaan ditolak.')
        if not path.exists():
            continue
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('Catatan status terlalu besar; pemeliharaan ditolak.')
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            raise ValueError('Catatan status tidak valid; pemeliharaan ditolak.')
        if record.get('status') not in TERMINAL:
            raise ValueError('Pekerjaan/runtime aktif atau status tidak pasti. Periksa dan selesaikan pekerjaan sebelum pemeliharaan.')
        training = record.get('training_status')
        if training is not None and training not in TERMINAL:
            raise ValueError('Status training belum aman untuk pemeliharaan.')
        if record.get('backup_status') in {'uploading', 'downloading', 'pending', 'preparing', 'uncertain', 'unknown'}:
            raise ValueError('Pencadangan aktif atau belum pasti; pemeliharaan ditolak.')

    database = DATA_HOME / 'cth.db'
    if database.is_file():
        with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cth_datasets'").fetchone()
            if exists and conn.execute("SELECT 1 FROM cth_datasets WHERE deleted_at IS NULL AND drive_status IN ('syncing','verifying') LIMIT 1").fetchone():
                raise ValueError('Sinkronisasi dataset aktif atau belum pasti. Periksa sebelum pemeliharaan.')


def verify_backup(folder):
    root = STATE_HOME / 'backups'
    if folder.is_symlink() or folder.resolve().parent != root.resolve():
        raise ValueError('Pilih direktori cadangan langsung di direktori backups CTH.')
    manifest = folder / 'manifest.json'
    if manifest.is_symlink():
        raise ValueError('Manifest tidak aman.')
    data = json.loads(manifest.read_text())
    if data.get('format') != 1 or not isinstance(data.get('files'), dict):
        raise ValueError('Format cadangan tidak didukung.')
    files = {str(path.relative_to(folder / 'data')): path for path in safe_tree(folder / 'data')}
    if files.keys() != data['files'].keys():
        raise ValueError('Daftar berkas cadangan tidak cocok.')
    for name, path in files.items():
        if digest(path) != data['files'][name]:
            raise ValueError('Checksum cadangan tidak cocok.')
    database = folder / 'data/cth.db'
    if database.is_file():
        with closing(sqlite3.connect(f'file:{database}?mode=ro&immutable=1', uri=True)) as conn:
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Database cadangan rusak.')
    return folder / 'data'


def backup():
    safe_tree(DATA_HOME)
    root = STATE_HOME / 'backups'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = root / uuid4().hex
    folder.mkdir(mode=0o700)
    stage = folder / 'data'
    try:
        shutil.copytree(DATA_HOME, stage, ignore=shutil.ignore_patterns('cth.db', 'cth.db-wal', 'cth.db-shm'))
        database = DATA_HOME / 'cth.db'
        if database.is_file():
            with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as original, closing(sqlite3.connect(stage / 'cth.db')) as target:
                original.backup(target)
                target.execute('PRAGMA journal_mode=DELETE')
        atomic_json(folder / 'manifest.json', {'format': 1, 'files': {str(p.relative_to(stage)): digest(p) for p in safe_tree(stage)}})
        verify_backup(folder)
    except BaseException:
        shutil.rmtree(folder)
        raise
    return folder


def replace_data(source=None):
    staging = DATA_HOME.parent / ('cth-new-' + uuid4().hex)
    previous = DATA_HOME.parent / ('cth-old-' + uuid4().hex)
    try:
        if source:
            shutil.copytree(source, staging)
        else:
            staging.mkdir(mode=0o700)
        os.replace(DATA_HOME, previous)
        try:
            os.replace(staging, DATA_HOME)
            from app.database import init_db
            from app.datasets import db
            init_db()
            db().close()
            (DATA_HOME / 'browser-generation').write_text(uuid4().hex)
        except BaseException:
            if DATA_HOME.exists():
                shutil.rmtree(DATA_HOME)
            os.replace(previous, DATA_HOME)
            raise
        shutil.rmtree(previous)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def perform(command, selected=None):
    validate_roots()
    prepare()
    with ExitStack() as stack:
        for name in ('application.lock', 'api.lock'):
            handle = stack.enter_context((STATE_HOME / name).open('a'))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Tutup aplikasi dan proses pemeliharaan sebelum reset/restore.') from None
        require_inactive()
        source = verify_backup(selected) if command == 'restore-local' and selected else None
        if command == 'restore-local' and source is None:
            raise ValueError('--backup diperlukan untuk restore-local.')
        saved = backup()
        replace_data(source)
        return saved


def main():
    parser = argparse.ArgumentParser(description='Reset/restore data lokal; konfigurasi dan Google Drive dipertahankan')
    parser.add_argument('command', choices=['reset-local', 'restore-local'])
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if not args.confirm:
        parser.error('Tambahkan --confirm setelah membaca cakupan reset di README.')
    try:
        saved = perform(args.command, args.backup)
        print(f'{args.command} selesai. Cadangan sebelum perubahan: {saved}')
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f'Pemeliharaan ditolak/gagal: {error}\n')


if __name__ == '__main__':
    main()
