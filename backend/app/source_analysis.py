"""Local, read-only analysis shared with the runtime parser; no model or network."""
from contextlib import closing
from functools import lru_cache
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import sqlite3
import json

from fastapi import HTTPException

from app.drive_storage import file_checksums
from app.storage import atomic_json


@lru_cache(maxsize=1)
def parser():
    code = Path(__file__).resolve().parents[2] / 'training/retrieval.py'
    spec = spec_from_file_location('cth_source_analysis', code)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def analyze_row(row, force=False):
    from app.datasets import local_path
    path = local_path(row)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(409, 'Pulihkan salinan lokal sebelum analisis sumber.')
    stat = path.stat()
    cache = path.parent/'analysis.json'
    if not force and cache.is_file() and not cache.is_symlink() and cache.stat().st_size <= 65536:
        try:
            saved = json.loads(cache.read_text())
            if (isinstance(saved, dict) and saved.get('sha256') == row['sha256'] and saved.get('source_mode') == row.get('source_mode', 'filtered')
                    and saved.get('mtime_ns') == stat.st_mtime_ns and saved.get('ctime_ns') == stat.st_ctime_ns
                    and saved.get('analysis_version') == parser().ANALYSIS_VERSION):
                return saved
        except (ValueError, OSError):
            pass
    before = file_checksums(path)
    if before[0] != row['size'] or before[2] != row['sha256']:
        raise HTTPException(409, 'Versi lokal berubah; analisis tidak boleh memakai versi yang berbeda.')
    with closing(sqlite3.connect(':memory:')) as conn:
        engine = parser()
        engine.create_index(conn)
        try:
            with path.open(encoding='utf-8', errors='strict') as handle:
                report = engine.index_source(conn, row['name'], handle, row.get('source_mode', 'filtered'))
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Analisis gagal: periksa format TXT UTF-8 dan peran sumber. Berkas asli tidak diubah.') from None
    if file_checksums(path) != before:
        raise HTTPException(409, 'Berkas berubah selama analisis.')
    report.update(dataset_id=row['id'], name=row['name'], sha256=row['sha256'], mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)
    atomic_json(path.parent/'analysis.json', report)
    return report


def analyze_selection(project_id, dataset_ids, force=False):
    from app.datasets import get_row
    if not dataset_ids or len(set(dataset_ids)) != len(dataset_ids) or len(dataset_ids) > 500:
        raise HTTPException(400, 'Pilih 1–500 dataset unik untuk analisis.')
    reports = []
    for dataset_id in dataset_ids:
        row = get_row(project_id, dataset_id)
        if row['kind'] != 'txt':
            raise HTTPException(400, 'Analisis sumber hanya untuk TXT; URL belum diambil.')
        reports.append(analyze_row(row, force))
    return parser().source_summary(reports, details=True)
