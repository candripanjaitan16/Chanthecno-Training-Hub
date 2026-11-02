import base64
import fcntl
import hashlib
import json
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from googleapiclient.http import MediaIoBaseDownload
from pydantic import BaseModel, Field

from app.colab_bridge import BridgeError, RuntimeBridge
from app.storage import atomic_json
from app.colab_stop import local_only, project_row, receipt_path
from app.drive_storage import storage_connection, upload_project_file
from app.training_backup import (
    BackupError, local_backup, prepare_local_backup, read_remote_job,
    safe_error, upload_local_backup, write_stop_request, read_json_file, http_get,
)

from app.checkpoint_backup import router as checkpoint_router, sync_checkpoint

router = APIRouter()
router.include_router(checkpoint_router)
REPO = Path(__file__).resolve().parents[2]
from app import dataset_manifest
from app.paths import WORKSPACE

WORK = WORKSPACE / 'training-web'
CURRENT = WORK / 'current.json'
TERMINAL = {'completed', 'stopped', 'failed'}
CODE_FILES = ['__init__.py', 'common.py', 'data.py', 'train.py', 'web_worker.py', 'retrieval.py']
MONITORS = {}
MONITOR_GUARD = threading.Lock()
POLL_SECONDS = 15
RETRY_SECONDS = 60



def preflight_record(project_id):
    path = WORK / f'preflight-{project_id}.json'
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    age = time.time() - data.get('checked_at_epoch', 0)
    if data.get('project_id') != project_id or not data.get('verified') or not 0 <= age < 21600:
        return None
    return data


class StartTraining(BaseModel):
    project_id: int = Field(gt=0)
    model_name: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
    epochs: float = Field(default=1, gt=0, le=5, allow_inf_nan=False)
    dataset_ids: list[str] = Field(default_factory=list, max_length=500)
    acknowledge_empty_sources: bool = False
    source_receipts: list[str] = Field(default_factory=list, max_length=500)


def load_job():
    return json.loads(CURRENT.read_text()) if CURRENT.exists() else {'status': 'idle'}


def save_job(job, _replace=False, **extra):
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / 'state.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        current = load_job()
        if current.get('job_id') == job['job_id']:
            merged = {**current, **job, **extra}
        else:
            merged = {**job, **extra}
        merged['checked_at'] = datetime.now(timezone.utc).isoformat()
        job.clear()
        job.update(merged)
        atomic_json(WORK / 'history' / (job['job_id'] + '.json'), job)
        if _replace or current.get('job_id') in (None, job['job_id']):
            atomic_json(CURRENT, job)


def launch_busy():
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / 'launch.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def ensure_monitor(job):
    if not job.get('autosave_enabled') or not job.get('job_id'):
        return
    if job.get('backup_status') == 'saved' or job.get('status') in {'failed', 'stopped'}:
        return
    with MONITOR_GUARD:
        previous = MONITORS.get(job['job_id'])
        if previous and previous.is_alive():
            return
        thread = threading.Thread(target=monitor_job, args=(job['job_id'],), daemon=True)
        MONITORS[job['job_id']] = thread
        thread.start()


def resume_monitor():
    # Startup hanya melanjutkan pemantauan/backup, tidak meluncurkan training.
    try:
        ensure_monitor(load_job())
    except Exception:
        pass


router.add_event_handler('startup', resume_monitor)


def monitor_job(job_id):
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / f'monitor-{job_id}.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            try:
                job = load_job()
                if job.get('job_id') != job_id or job.get('backup_status') == 'saved':
                    return
                if job.get('status') in {'failed', 'stopped'}:
                    return
                if launch_busy():
                    time.sleep(2)
                    continue
                if job.get('status') in {'queued', 'uploading'} and not job.get('launched'):
                    save_job(job, status='failed', message='Persiapan lokal terputus sebelum peluncuran terkonfirmasi. Tidak menjalankan ulang otomatis.')
                    return
                wait = float(job.get('next_retry_at', 0)) - time.time()
                if wait > 0:
                    time.sleep(min(wait, 5))
                    continue
                monitor_once(job)
            except Exception as error:
                # Jangan biarkan kegagalan jaringan mematikan monitor tanpa catatan.
                try:
                    current = load_job()
                    if current.get('job_id') != job_id:
                        return
                    if current.get('training_status') == 'completed':
                        save_job(
                            current,
                            status='uncertain',
                            backup_error=safe_error(error),
                            next_retry_at=time.time() + RETRY_SECONDS,
                        )
                    else:
                        uncertain_job(
                            current,
                            'Pemantauan belum berhasil. Status training saat ini belum terverifikasi.',
                            error,
                        )
                except Exception:
                    pass
            time.sleep(POLL_SECONDS)


def uncertain_job(job, message, error):
    previous = job.get('training_status', job.get('status'))
    confirmed = job.get('last_confirmed_training_status')

    if previous in {'preparing', 'training', 'completed', 'stopped', 'failed'}:
        confirmed = previous

    save_job(
        job,
        status='uncertain',
        training_status='unknown',
        worker_running=None,
        last_confirmed_training_status=confirmed,
        message=message,
        connection_error=safe_error(error),
        next_retry_at=time.time() + RETRY_SECONDS,
    )


def inspect_missing_workspace(bridge, job):
    path = remote_path(job)
    project = f"/content/cth/projects/{int(job['project_id'])}"
    run_id = job.get('run_id')
    run = None

    if re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}', str(run_id)):
        run = project + '/training_results/' + run_id

    bridge.require_idle_other_kernels()

    code = f'''import json, subprocess
from pathlib import Path

project = Path({project!r})
job = Path({path!r})
run = {run!r}

gpu = subprocess.run(
    ['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader,nounits'],
    capture_output=True,
    text=True,
    timeout=15,
    check=False,
)

print('CTH_CONTROL:' + json.dumps({{
    'ok': True,
    'project_exists': project.exists(),
    'job_exists': job.exists(),
    'run_exists': Path(run).exists() if run else None,
    'gpu_checked': gpu.returncode == 0,
    'gpu_active': bool(gpu.stdout.strip()) if gpu.returncode == 0 else None,
}}))
'''

    observation = bridge.execute(code, timeout=60)

    missing = (
        observation.get('project_exists') is False
        and observation.get('job_exists') is False
        and observation.get('run_exists') is False
        and observation.get('gpu_checked') is True
        and observation.get('gpu_active') is False
    )

    if missing:
        save_job(
            job,
            status='failed',
            training_status='interrupted',
            last_confirmed_training_status=job.get('last_confirmed_training_status') or job.get('training_status'),
            backup_status='unavailable',
            message='Workspace proyek, pekerjaan dan run tidak ditemukan pada runtime yang diperiksa; tidak ada proses komputasi GPU aktif. Pekerjaan ditandai terputus, bukan masih training. Tidak memulai ulang otomatis.',
            connection_error=None,
            workspace_check=observation,
            next_retry_at=0,
        )
        return True

    return False


def monitor_once(job):
    from app.colab import RUNTIME_NAME, T4_SPEC, load_state
    bridge = None
    try:
        if job.get('training_status') != 'completed':
            if load_state().get('project_id') != job['project_id']:
                raise BackupError('Runtime terkait proyek lain. Tidak memindahkan sesi otomatis.')
            bridge = RuntimeBridge(RUNTIME_NAME, T4_SPEC)
            expected = job.get('runtime_identity')

            if expected is None:
                record = preflight_record(job['project_id'])
                expected = record.get('runtime_identity') if record else None

            if expected is not None and expected != list(bridge.identity):
                error = BackupError('Identitas runtime berbeda dari sesi pekerjaan. Tidak menganggap progres lama masih aktif.')

                if not inspect_missing_workspace(bridge, job):
                    uncertain_job(job, str(error), error)

                return

            try:
                data = read_remote_job(bridge, job)

            except BackupError as error:
                if not str(error).startswith('Berkas status worker belum ditemukan.'):
                    raise

                if not inspect_missing_workspace(bridge, job):
                    uncertain_job(job, 'Berkas status worker belum ditemukan. Aktivitas training belum dapat dipastikan; progres yang tampil adalah catatan terakhir.', error)

                return
            phase = data.get('status')
            if phase not in {'preparing', 'training', 'stopped', 'failed', 'completed'}:
                raise BackupError('Status worker tidak dikenali. Tidak meluncurkan pekerjaan baru.')
            if phase in {'training', 'preparing'} and not data.get('worker_running'):
                uncertain_job(job, 'Heartbeat worker belum terverifikasi. Progres lama adalah riwayat; periksa koneksi tanpa mengulang training.', BackupError('Heartbeat worker tidak tersedia atau kedaluwarsa.'))
                return
            allowed = {'updated_at', 'run_id', 'step', 'max_steps', 'epoch', 'metrics', 'gpu_verified', 'error_type', 'worker_running', 'dataset_stats'}
            fields = {key: value for key, value in data.items() if key in allowed}
            fields.update(
                training_status=phase,
                last_confirmed_training_status=phase,
                last_remote_seen_at=datetime.now(timezone.utc).isoformat(),
                runtime_identity=list(bridge.identity),
                connection_error=None,
                next_retry_at=0,
            )
            if phase == 'completed':
                save_job(job, **fields, status='preparing', backup_status='pending',
                         message='Training selesai. Menyiapkan cadangan adapter otomatis; belum aman menghentikan runtime.')
            else:
                message = data.get('message') if phase in {'failed', 'stopped'} else None
                if not message:
                    if phase == 'training' and data.get('max_steps', 0) > 0 and data.get('step', 0) >= data['max_steps']:
                        message = 'Langkah training selesai. Menunggu evaluasi dan penyimpanan adapter.'
                    elif phase == 'training':
                        message = f"Training berjalan: step {data.get('step', 0)}/{data.get('max_steps', '—')}."
                    else:
                        message = 'Worker sedang menyiapkan dependensi, model atau dataset di Colab.'
                save_job(job, **fields, status=phase, message=message)
                if phase in {'training', 'stopped', 'failed'}:
                    try:
                        checkpoint = sync_checkpoint(job, bridge)
                        if checkpoint:
                            save_job(
                                job, **checkpoint,
                                message=message + f" Cadangan checkpoint step {checkpoint['checkpoint_step']} terverifikasi di Drive.",
                            )
                    except Exception as checkpoint_error:
                        save_job(
                            job,
                            checkpoint_backup_status='failed',
                            checkpoint_error=safe_error(checkpoint_error),
                            message=message + ' Cadangan checkpoint belum berhasil; salinan lokal yang sudah dibuat dipertahankan.',
                        )
                return

        def progress(stage, message):
            save_job(job, status='preparing', backup_status=stage,
                     message=message, connection_error=None, backup_error=None)

        local = local_backup(job)
        if local is None:
            if bridge is None:
                if load_state().get('project_id') != job['project_id']:
                    raise BackupError('Model belum disalin lokal dan runtime terkait proyek berbeda.')
                bridge = RuntimeBridge(RUNTIME_NAME, T4_SPEC)
            local = prepare_local_backup(job, bridge, progress)
        # Setelah arsip lokal siap, upload tidak memerlukan runtime tetap hidup.
        receipt = upload_local_backup(job, local, progress)
        save_job(job, status='completed', backup_status='saved', backup=receipt,
                 message='Training selesai. Adapter, tokenizer dan metadata telah terverifikasi di Drive; salinan lokal dipertahankan.',
                 connection_error=None, backup_error=None, next_retry_at=0)
    except Exception as error:
        if job.get('training_status') == 'completed':
            save_job(job, status='uncertain', backup_status='failed', backup_error=safe_error(error),
                     message='Training sudah selesai, tetapi cadangan belum terverifikasi. Jangan training ulang atau hentikan runtime; backend akan mencoba pencadangan lagi.',
                     connection_error=None, next_retry_at=time.time() + RETRY_SECONDS)
        else:
            uncertain_job(
                job,
                'Status remote belum dapat diverifikasi. Progres terakhir dipertahankan sebagai riwayat; tidak menjalankan ulang otomatis.',
                error,
            )
    finally:
        if bridge:
            bridge.close()


def remote_path(job):
    if not re.fullmatch('[a-f0-9]{32}', job['job_id']):
        raise BridgeError('ID pekerjaan tidak valid.')
    return f"/content/cth/projects/{int(job['project_id'])}/jobs/{job['job_id']}"


def remote_status_code(job, stop=False):
    path = remote_path(job)
    return f'''import json, os
from pathlib import Path
p = Path({path!r})
if {stop!r}:
    (p / "stop.request").touch(exist_ok=True)
data = json.loads((p / "job.json").read_text())
print("CTH_CONTROL:" + json.dumps({{"ok": True, "job": data}}))
'''


def build_bundle(body, target, manifest):
    sources = manifest['datasets']
    drive = storage_connection()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name in CODE_FILES:
            path = REPO / 'training' / name
            if path.is_symlink() or not path.is_file():
                raise BridgeError(f'File worker belum tersedia: training/{name}.')
            archive.write(path, 'code/training/' + name)
        archive.write(REPO / 'requirements-training.txt', 'code/requirements-training.txt')
        for index, meta in enumerate(sources):
            if not meta['name'].lower().endswith('.txt'):
                raise BridgeError('Training web saat ini menerima TXT; URL belum diambil isinya.')
            with tempfile.TemporaryFile() as handle:
                downloader = MediaIoBaseDownload(handle, drive.files().get_media(fileId=meta['file_id']), chunksize=1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk(num_retries=2)
                    if handle.tell() > meta['size']:
                        raise BridgeError('Ukuran dataset Drive berubah.')
                if handle.tell() != meta['size']:
                    raise BridgeError('Ukuran dataset tidak cocok.')
                handle.seek(0)
                digest = hashlib.sha256()
                with archive.open(f'data/{index:04d}.txt', 'w', force_zip64=True) as entry:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                        entry.write(chunk)
                if digest.hexdigest() != meta['sha256']:
                    raise BridgeError('Checksum dataset Drive tidak cocok.')
    return sources


def transfer(bridge, job, archive):
    path = f"/tmp/cth-upload-{job['job_id']}.zip"
    offset = 0
    digest = hashlib.sha256()
    with archive.open('rb') as handle:
        while chunk := handle.read(256 * 1024):
            code = f'''import base64, json
from pathlib import Path
p = Path({path!r})
chunk = base64.b64decode({base64.b64encode(chunk).decode()!r}, validate=True)
with p.open({('xb' if offset == 0 else 'ab')!r}) as f:
    if f.tell() != {offset}:
        raise RuntimeError("Offset transfer berubah")
    f.write(chunk)
print('CTH_CONTROL:' + json.dumps({{"ok": True}}))
'''
            bridge.execute(code, timeout=90)
            offset += len(chunk)
            digest.update(chunk)
    return path, digest.hexdigest()


def launch_code(job, body, archive_path, sha256):
    path = remote_path(job)
    config = {'project_id': body.project_id, 'model_name': body.model_name, 'epochs': body.epochs, 'dataset_manifest': dataset_manifest.load(job)}
    initial = {'status': 'preparing', 'project_id': body.project_id, 'job_id': job['job_id'], 'message': 'Worker diterima runtime.'}
    return f'''import fcntl, hashlib, json, os, subprocess, sys, zipfile
from pathlib import Path
archive = Path({archive_path!r})
h = hashlib.sha256()
with archive.open('rb') as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b''):
        h.update(chunk)
if h.hexdigest() != {sha256!r}:
    raise RuntimeError('Checksum paket tidak cocok')
project = Path('/content/cth/projects/{body.project_id}')
if project.resolve() != project:
    raise RuntimeError('Workspace berupa symlink')
project.mkdir(parents=True, exist_ok=True)
locks = []
try:
    for name in ['.cth-worker.lock', '.cth-web.lock']:
        p = project / name
        if p.is_symlink():
            raise RuntimeError('Lock tidak aman')
        lock = p.open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    job = Path({path!r})
    if job.parent.resolve() != job.parent:
        raise RuntimeError('Folder jobs tidak aman')
    job.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            relative = Path(item.filename)
            target = job / relative
            if relative.is_absolute() or '..' in relative.parts or target.resolve() != target or item.is_dir():
                raise RuntimeError('Isi paket tidak valid')
            allowed = item.filename in {[f'code/training/{n}' for n in CODE_FILES] + ['code/requirements-training.txt']!r} or (len(relative.parts) == 2 and relative.parts[0] == 'data' and relative.suffix == '.txt' and relative.stem.isdigit())
            if not allowed or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise RuntimeError('Jenis berkas tidak diizinkan')
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(item) as source, target.open('xb') as dest:
                import shutil
                shutil.copyfileobj(source, dest, 1024 * 1024)
    (job / 'request.json').write_text(json.dumps({config!r}))
    (job / 'job.json').write_text(json.dumps({initial!r}))
finally:
    for lock in locks:
        lock.close()
with (job / 'worker.log').open('ab') as log:
    process = subprocess.Popen([sys.executable, '-u', '-m', 'training.web_worker', '--job-dir', str(job)],
        cwd=job / 'code', stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
(job / 'pid').write_text(str(process.pid))
archive.unlink()
print('CTH_CONTROL:' + json.dumps({{"ok": True, "worker_started": True}}), flush=True)
process.wait()
'''


def prepare_job(job, body, handle):
    bridge = None
    launched = False
    released = False
    finished = False

    def release_launch():
        nonlocal released

        if not released:
            released = True

            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def worker_started(result):
        if result.get('worker_started') is not True:
            raise BridgeError(
                'Worker belum mengonfirmasi peluncuran.'
            )

        save_job(
            job,
            status='preparing',
            launched=True,
            runtime_identity=list(bridge.identity),
            training_kernel_id=bridge.kernel_id,
            execution_mode='kernel_wait',
            message=(
                'Worker berjalan di kernel training. '
                'Pemantauan dan cadangan checkpoint aktif.'
            ),
        )

        release_launch()
        ensure_monitor(job)

    try:
        from app.colab import (
            RUNTIME_NAME,
            T4_SPEC,
            runtime_lock,
            load_state,
        )

        with runtime_lock():
            if load_state().get('project_id') != body.project_id:
                raise BridgeError(
                    'Pilih proyek yang terikat ke runtime T4 '
                    'pada Colab Connection.'
                )

        save_job(
            job,
            status='uploading',
            message=(
                'Memverifikasi dataset Drive '
                'dan menyiapkan paket worker…'
            ),
        )

        with tempfile.TemporaryDirectory(
            prefix='cth-train-'
        ) as temporary:
            archive = Path(temporary) / 'training.zip'
            manifest = dataset_manifest.revalidate(job)
            sources = build_bundle(body, archive, manifest)
            dataset_manifest.revalidate(job)

            bridge = RuntimeBridge(RUNTIME_NAME, T4_SPEC)
            preflight = preflight_record(body.project_id)

            if (
                not preflight
                or preflight.get('runtime_identity')
                != list(bridge.identity)
            ):
                raise BridgeError(
                    'Runtime berubah atau pemeriksaan cadangan '
                    'kedaluwarsa. Jalankan preflight lagi '
                    'sebelum training.'
                )

            bridge.require_idle_other_kernels()

            remote, checksum = transfer(
                bridge,
                job,
                archive,
            )

            with runtime_lock():
                if (
                    load_state().get('project_id')
                    != body.project_id
                ):
                    raise BridgeError(
                        'Proyek runtime berubah sebelum peluncuran.'
                    )

                bridge.refresh_connection()

                save_job(
                    job,
                    status='launching',
                    source_backups=sources,
                    runtime_identity=list(bridge.identity),
                    message=(
                        'Mengirim permintaan eksekusi. '
                        'Jangan klik Mulai lagi.'
                    ),
                )

            dataset_manifest.revalidate(job)
            launched = True

            bridge.execute(
                launch_code(job, body, remote, checksum),
                timeout=86400,
                on_result=worker_started,
            )

            finished = True

    except Exception as error:
        if launched:
            current = load_job()

            active = (
                current.get('job_id') == job['job_id']
                and current.get('status') not in TERMINAL
            )

            if active:
                current.pop('checked_at', None)

                save_job(
                    current,
                    launched=True,
                    execution_warning=safe_error(error),
                )

        else:
            save_job(
                job,
                status='failed',
                launched=False,
                message=safe_error(error),
                connection_error=None,
            )

    finally:
        release_launch()
        ensure_monitor(job)

        if bridge:
            bridge.close(
                keep_kernel=launched and not finished
            )


@router.post('/training/start', status_code=202)
def start_training(body: StartTraining, request: Request):
    local_only(request)
    project_row(body.project_id)
    if not preflight_record(body.project_id):
        raise HTTPException(409, 'Jalur cadangan belum diuji untuk proyek ini. Jalankan POST /api/colab/training/preflight terlebih dahulu.')
    for receipt_id in body.source_receipts:
        receipt_path(receipt_id)
    WORK.mkdir(parents=True, exist_ok=True)
    handle = (WORK / 'launch.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise HTTPException(409, 'Persiapan training masih berlangsung.') from None
    try:
        existing = load_job()
        if existing.get('status') not in TERMINAL | {'idle'}:
            raise HTTPException(409, 'Ada pekerjaan yang belum terkonfirmasi selesai. Periksa status dahulu.')
        job = {'job_id': uuid4().hex, 'project_id': body.project_id, 'model_name': body.model_name,
               'status': 'queued', 'message': 'Permintaan diterima; training belum mulai.',
               'autosave_enabled': True, 'backup_status': 'waiting'}
        manifest = dataset_manifest.verify_selection(body.project_id, body.dataset_ids, body.source_receipts)
        from app.source_analysis import analyze_selection
        report = analyze_selection(body.project_id, [item['dataset_id'] for item in manifest['datasets']], force=True)
        if report['training_samples'] == 0:
            raise HTTPException(409, 'Pilihan hanya berisi fakta sumber. Pilih berkas latihan untuk training baru.')
        if not report['sources_ready'] and not body.acknowledge_empty_sources:
            raise HTTPException(409, report['warning'] + ' Konfirmasi acknowledge_empty_sources hanya bila memang ingin latihan tanpa sumber.')
        job['source_analysis'] = report
        job['dataset_manifest_sha256'] = dataset_manifest.freeze(job['job_id'], manifest)
        job['dataset_count'] = len(manifest['datasets'])
        save_job(job, _replace=True)
        threading.Thread(target=prepare_job, args=(job, body, handle), daemon=True).start()
        return job
    except Exception:
        handle.close()
        raise


@router.get('/training/status/{project_id}')
def training_status(project_id: int, request: Request):
    local_only(request)
    project_row(project_id)
    job = load_job()
    if job.get('project_id') != project_id:
        return {'status': 'idle', 'project_id': project_id, 'message': 'Belum ada pekerjaan training untuk proyek ini.'}
    ensure_monitor(job)
    if not job.get('autosave_enabled'):
        job = {**job, 'backup_status': job.get('backup_status', 'not_verified'),
               'message': 'Catatan pekerjaan versi lama. Cadangan model belum diverifikasi oleh pemantau otomatis baru.'}
    return job


class StopTraining(BaseModel):
    project_id: int = Field(gt=0)


@router.post('/training/preflight')
def training_preflight(body: StopTraining, request: Request):
    local_only(request)
    project_row(body.project_id)
    if launch_busy():
        raise HTTPException(409, 'Pekerjaan lain masih menyiapkan paket.')
    current = load_job()
    if current.get('autosave_enabled') and current.get('status') not in TERMINAL | {'idle'}:
        raise HTTPException(409, 'Tunggu pekerjaan atau pencadangan aktif selesai sebelum preflight.')
    from app.colab import RUNTIME_NAME, T4_SPEC, load_state, runtime_lock
    bridge = None
    try:
        with runtime_lock():
            if load_state().get('project_id') != body.project_id:
                raise BackupError('Hubungkan T4 dan kaitkan dengan proyek ini terlebih dahulu.')
            bridge = RuntimeBridge(RUNTIME_NAME, T4_SPEC)
            bridge.require_idle_other_kernels()
            nonce = uuid4().hex
            payload = json.dumps({'project_id': body.project_id, 'preflight': nonce}, sort_keys=True)
            relative = f'cth/projects/{body.project_id}/health/preflight-{nonce}.json'
            code = f"""import json
from pathlib import Path
p = Path('/content') / {relative!r}
if p.resolve() != p:
    raise RuntimeError('Path preflight tidak aman')
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text({payload!r}, encoding='utf-8')
print('CTH_CONTROL:' + json.dumps({{'ok': True}}))
"""
            bridge.execute(code, timeout=120)
            actual_path = None
            for prefix in ('', 'content/'):
                data = read_json_file(bridge, prefix + relative, missing_ok=True)
                if data == json.loads(payload):
                    actual_path = prefix + relative
                    break
            if actual_path is None:
                raise BackupError('API berkas belum bisa membaca hasil uji; training belum diizinkan.')
            with http_get(bridge, 'files/' + actual_path, stream=True) as response:
                if response.status_code != 200:
                    raise BackupError(f'Unduhan HTTP belum bekerja: HTTP {response.status_code}.')
                content = bytearray()
                for chunk in response.iter_content(1024):
                    content.extend(chunk)
                    if len(content) > 4096:
                        raise BackupError('Ukuran hasil preflight tidak sesuai.')
            if bytes(content) != payload.encode('utf-8'):
                raise BackupError('Isi unduhan preflight tidak cocok.')
            with tempfile.TemporaryDirectory(prefix='cth-preflight-') as temporary:
                path = Path(temporary) / f'cth-preflight-{nonce}.json'
                path.write_bytes(content)
                receipt = upload_project_file(body.project_id, path, 'training_results')
            if receipt.get('verified') is not True or receipt.get('sha256') != hashlib.sha256(content).hexdigest():
                raise BackupError('Upload uji Drive belum terverifikasi.')
            record = {'project_id': body.project_id, 'verified': True,
                      'runtime_identity': list(bridge.identity), 'checked_at_epoch': time.time(),
                      'drive_file_id': receipt['file_id'], 'sha256': receipt['sha256']}
            atomic_json(WORK / f'preflight-{body.project_id}.json', record)
            return {'status': 'ready', 'verified': True, 'project_id': body.project_id,
                    'message': 'Kontrol kernel, pembacaan status HTTP, unduhan berkas dan upload Drive berhasil diuji. Ini bukan jaminan sesi akan bertahan.',
                    'drive_file_id': receipt['file_id']}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, safe_error(error)) from None
    finally:
        if bridge:
            bridge.close()


@router.post('/training/stop')
def stop_training(body: StopTraining, request: Request):
    local_only(request)
    project_row(body.project_id)
    job = load_job()
    if job.get('project_id') != body.project_id or job['status'] in TERMINAL | {'idle'}:
        raise HTTPException(409, 'Tidak ada pekerjaan aktif pada proyek ini.')
    if job.get('training_status') == 'completed':
        raise HTTPException(409, 'Training selesai; pencadangan sedang ditangani. Jangan meminta training ulang atau stop paksa.')
    if launch_busy():
        raise HTTPException(409, 'Paket masih disiapkan atau dikirim. Tunggu worker terdaftar.')
    from app.colab import RUNTIME_NAME, T4_SPEC, load_state
    bridge = None
    try:
        if load_state().get('project_id') != body.project_id:
            raise HTTPException(409, 'Runtime tidak sesuai proyek.')
        bridge = RuntimeBridge(RUNTIME_NAME, T4_SPEC)
        write_stop_request(bridge, job)
        ensure_monitor(job)
        return {'status': 'stop_requested', 'message': 'Stop kooperatif diminta. Tunggu checkpoint worker; runtime tidak dimatikan. Checkpoint optimizer belum dicadangkan otomatis.'}
    except (BridgeError, BackupError):
        raise HTTPException(503, 'Stop belum terkonfirmasi. Periksa status; tidak mematikan GPU paksa.') from None
    finally:
        if bridge:
            bridge.close()


@router.post('/training/backup/retry', status_code=202)
def retry_backup(body: StopTraining, request: Request):
    local_only(request)
    project_row(body.project_id)
    job = load_job()
    if job.get('project_id') != body.project_id or job.get('training_status', job.get('status')) != 'completed':
        raise HTTPException(409, 'Belum ada run selesai yang dapat dicoba cadangkan.')
    if job.get('backup_status') == 'saved':
        return job
    save_job(job, autosave_enabled=True, training_status='completed', status='preparing',
             backup_status='pending', next_retry_at=0, message='Mencoba cadangan run selesai; tidak melatih ulang.')
    ensure_monitor(job)
    return job
