import codecs
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.colab_bridge import BridgeError, RuntimeBridge
from app.colab_snapshot import FINAL_CHECK_CODE, snapshot_code
from app.storage import atomic_json, MAX_UPLOAD_BYTES
from app.database import get_db
from app.drive_storage import upload_project_file, storage_connection, find_subfolder, project_folder_id, verify_project_file


router = APIRouter()
from app.paths import DATA_HOME

BASE_DIR = DATA_HOME
WORK = BASE_DIR / "workspace" / "stop-save"
JOB_FILE = WORK / "current.json"
LOCK_FILE = WORK / "job.lock"
TERMINAL = {"completed", "failed", "interrupted"}
THREADS = {}
THREAD_LOCK = threading.Lock()


class StopRequest(BaseModel):
    project_id: int = Field(gt=0)
    confirm_name: str = Field(min_length=1, max_length=300)
    acknowledge_scope: bool
    source_receipts: list[str] = Field(default_factory=list, max_length=500)


def local_only(request):
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "Fitur ini hanya untuk aplikasi lokal.")
    origin = request.headers.get("origin")
    if origin and origin not in {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"}:
        raise HTTPException(403, "Origin tidak diizinkan.")


def project_row(project_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT id,name,drive_folder_id FROM projects WHERE id=?", (project_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "Proyek tidak ditemukan.")
    return dict(row)



def job_read():
    return json.loads(JOB_FILE.read_text()) if JOB_FILE.exists() else {"status": "idle"}


def job_write(job, status, message, **extra):
    job.update(status=status, message=message, updated_at=datetime.now(timezone.utc).isoformat(), **extra)
    atomic_json(JOB_FILE, job)
    atomic_json(WORK / "history" / f"{job['job_id']}.json", job)


def receipt_path(receipt_id):
    if len(receipt_id) != 32 or any(char not in "0123456789abcdef" for char in receipt_id):
        raise HTTPException(400, "ID bukti upload tidak valid.")
    return WORK / "receipts" / f"{receipt_id}.json"


@router.post("/save-local-source")
async def save_local_source(request: Request):
    local_only(request)
    try:
        project_id = int(request.headers.get("x-cth-project-id", "0"))
    except ValueError:
        raise HTTPException(400, "ID proyek tidak valid.") from None
    project_row(project_id)
    kind = request.headers.get("x-cth-source-type", "txt")
    name = unquote(request.headers.get("x-cth-source-name", "data.txt"))
    if kind not in {"txt", "urls"} or not name or len(name) > 180 or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(400, "Nama atau jenis sumber tidak valid.")
    if kind == "txt" and not name.lower().endswith(".txt"):
        raise HTTPException(400, "Sumber teks harus berekstensi TXT.")
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    has_text = False
    size = 0
    try:
        with tempfile.TemporaryDirectory(prefix="cth-source-") as temporary:
            path = Path(temporary) / (name if kind == "txt" else "cth-url-sources.json")
            with path.open("wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Sumber melebihi batas 64 MiB per berkas.")
                    if kind == "urls" and size > 200000:
                        raise HTTPException(413, "Daftar URL terlalu besar.")
                    if shutil.disk_usage(temporary).free < len(chunk) + 64 * 1024 * 1024:
                        raise HTTPException(507, "Disk lokal tidak cukup; runtime tidak dihentikan.")
                    text = decoder.decode(chunk)
                    if "\x00" in text:
                        raise HTTPException(400, "Sumber mengandung NUL biner.")
                    has_text = has_text or bool(text.strip())
                    handle.write(chunk)
                tail = decoder.decode(b"", final=True)
                has_text = has_text or bool(tail.strip())
            if not size or not has_text:
                raise HTTPException(400, "Sumber kosong.")
            if kind == "urls":
                data = json.loads(path.read_text())
                if not isinstance(data, list) or len(data) > 50:
                    raise HTTPException(400, "Daftar URL harus maksimal 50 alamat.")
                for item in data:
                    if not isinstance(item, str) or len(item) > 2048:
                        raise HTTPException(400, "URL tidak valid.")
                    parsed = urlsplit(item)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                        raise HTTPException(400, "URL tidak valid.")
            receipt = await run_in_threadpool(upload_project_file, project_id, path, "datasets")
            if receipt.get("verified") is not True:
                raise HTTPException(502, "Upload sumber belum terverifikasi; runtime tidak dihentikan.")
            receipt_id = uuid4().hex
            atomic_json(receipt_path(receipt_id), {"project_id": project_id, "upload": receipt})
            return {"status": "saved", "verified": True, "receipt_id": receipt_id, "file_id": receipt["file_id"]}
    except HTTPException:
        raise
    except (UnicodeError, ValueError):
        raise HTTPException(400, "Sumber harus berisi UTF-8 atau daftar URL yang valid.") from None
    except Exception:
        raise HTTPException(503, "Penyimpanan sumber gagal; runtime tidak dihentikan. Periksa koneksi Drive.") from None


def verify_receipts(project_id, receipt_ids):
    if not receipt_ids:
        return []
    saved_sources = []
    drive = storage_connection()
    target = find_subfolder(drive, project_folder_id(project_id), "datasets")
    for receipt_id in receipt_ids:
        record = json.loads(receipt_path(receipt_id).read_text())
        receipt = record["upload"]
        if record["project_id"] != project_id or receipt.get("verified") is not True:
            raise BridgeError("Bukti upload sumber tidak sesuai proyek.")
        try:
            verify_project_file(drive, project_id, receipt['file_id'], 'datasets', receipt, target)
        except (ValueError, KeyError, TypeError):
            raise BridgeError("Sumber di Drive berubah atau tidak ditemukan. Runtime tidak dihentikan.") from None
        saved_sources.append({key: receipt[key] for key in ("file_id", "name", "size", "sha256")})
    return saved_sources


def download_archive(bridge, metadata, target, project_id, callback):
    backup_id = metadata["backup_id"]
    if len(backup_id) != 32 or any(char not in "0123456789abcdef" for char in backup_id):
        raise BridgeError("ID arsip runtime tidak sesuai.")
    expected_size = metadata["size"]
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise BridgeError("Ukuran arsip tidak valid.")
    if shutil.disk_usage(target.parent).free < expected_size + 128 * 1024 * 1024:
        raise BridgeError("Disk laptop/server tidak cukup untuk transfer cadangan. Runtime tidak dimatikan.")
    relative = f"cth-transfer/{backup_id}/workspace.tar"
    response = None
    for prefix in ("", "content/"):
        candidate = bridge.http.get(bridge.url(f"files/{prefix}{relative}"), stream=True, timeout=60, allow_redirects=False)
        if candidate.status_code == 200:
            response = candidate
            break
        status = candidate.status_code
        candidate.close()
        if status != 404:
            raise BridgeError(f"Unduhan arsip runtime gagal: HTTP {status}.")
    if response is None:
        raise BridgeError("Arsip tidak ditemukan melalui server Jupyter. Runtime tidak dimatikan.")
    digest = hashlib.sha256()
    count = 0
    try:
        with target.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                count += len(chunk)
                if count > expected_size:
                    raise BridgeError("Ukuran unduhan melebihi arsip yang dilaporkan.")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        response.close()
    if count != expected_size or digest.hexdigest() != metadata["sha256"]:
        raise BridgeError("Checksum arsip Colab tidak cocok. Runtime tidak dihentikan.")
    callback()


def perform_stop(job, receipts):
    from app.colab import API_URL, RUNTIME_NAME, T4_SPEC, google_request, load_state, runtime_lock, save_state
    from google.auth.transport.requests import AuthorizedSession
    from app.drive_auth import get_colab_credentials
    bridge = None
    try:
        with runtime_lock():
            state = load_state()
            if state.get("project_id") != job["project_id"]:
                raise BridgeError("Runtime belum dikaitkan ke proyek ini. Klik Gunakan runtime T4 ini dahulu.")
            saved_sources = verify_receipts(job["project_id"], receipts)
            job_write(job, "preparing", "Meminta worker menyimpan checkpoint dan membuat cadangan workspace…", source_backups=saved_sources)
            bridge = RuntimeBridge(RUNTIME_NAME, T4_SPEC)
            metadata = bridge.execute(snapshot_code(job["project_id"], job["job_id"], saved_sources), timeout=900)
            bridge.require_idle_other_kernels()
            job_write(job, "downloading", "Memindahkan arsip dari Colab ke backend…", file_count=metadata["file_count"], checkpoint_count=metadata["checkpoint_count"])
            with tempfile.TemporaryDirectory(prefix="cth-backup-") as temporary:
                archive = Path(temporary) / f"cth-project-{job['project_id']}-{job['job_id']}.tar"
                download_archive(bridge, metadata, archive, job["project_id"], lambda: None)
                job_write(job, "uploading", "Mengunggah dan memverifikasi cadangan di Drive…")
                receipt = upload_project_file(job["project_id"], archive, "training_results")
                if receipt.get("verified") is not True or receipt.get("sha256") != metadata["sha256"]:
                    raise BridgeError("Cadangan Drive belum terverifikasi. Runtime tidak dihentikan.")
                job_write(job, "saved", "Cadangan Drive terverifikasi. Memeriksa workspace sebelum menghentikan runtime…", backup=receipt)
                bridge.refresh_connection()
                bridge.require_idle_other_kernels()
                bridge.execute(FINAL_CHECK_CODE, timeout=600)
                job_write(job, "stopping", "Cadangan sudah terverifikasi. Menghentikan runtime T4…")
                # DELETE hanya setelah seluruh pemeriksaan penyimpanan berhasil.
                with AuthorizedSession(get_colab_credentials()) as session:
                    runtime = google_request(session, "GET", f"{API_URL}/{RUNTIME_NAME}", allow_missing=True)
                    if runtime is not None:
                        if runtime.get("runtimeSpec") != T4_SPEC:
                            raise BridgeError("Identitas runtime berubah sebelum Stop.")
                        info = urlsplit(runtime.get("connectionInfo", {}).get("url", ""))
                        if (info.netloc, info.path) != bridge.identity:
                            raise BridgeError("Sesi runtime berubah sebelum Stop. Penghentian dibatalkan.")
                        google_request(session, "DELETE", f"{API_URL}/{RUNTIME_NAME}")
                    still_exists = google_request(session, "GET", f"{API_URL}/{RUNTIME_NAME}", allow_missing=True)
                    if still_exists is not None:
                        raise BridgeError("Cadangan aman di Drive, tetapi penghentian runtime belum terkonfirmasi. Periksa ulang status.")
                state.update(status="disconnected", stopped_at=datetime.now(timezone.utc).isoformat(), last_backup=receipt)
                state.pop("operation_name", None)
                state.pop("request_id", None)
                save_state(state)
                message = "Cadangan terverifikasi di Drive dan runtime T4 sudah dihentikan."
                if metadata["checkpoint_count"] == 0:
                    message += " Tidak ada checkpoint model; ini cadangan workspace/catatan kosong."
                job_write(job, "completed", message)
    except Exception as error:
        message = str(error) if isinstance(error, BridgeError) else "Proses gagal atau koneksi terputus. Periksa cadangan dan status runtime; jangan anggap GPU sudah berhenti."
        job_write(job, "failed", message)
    finally:
        if bridge:
            bridge.close()


def worker(job, receipts, handle):
    try:
        perform_stop(job, receipts)
    finally:
        with THREAD_LOCK:
            THREADS.pop(job["job_id"], None)
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@router.get("/stop-save")
def stop_save_status(request: Request):
    local_only(request)
    WORK.mkdir(parents=True, exist_ok=True)
    # Lock lintas proses membedakan job aktif dan job yang terputus karena restart.
    with LOCK_FILE.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return job_read()
        try:
            job = job_read()
            if job.get("job_id") and job.get("status") not in TERMINAL:
                job_write(job, "interrupted", "Backend sempat berhenti. Periksa cadangan dan status runtime sebelum mencoba lagi; tidak ada Stop otomatis saat restart.")
            return job
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@router.post("/stop-save", status_code=202)
def start_stop_save(body: StopRequest, request: Request):
    local_only(request)
    project = project_row(body.project_id)
    if not body.acknowledge_scope or body.confirm_name != project["name"]:
        raise HTTPException(400, "Konfirmasi nama proyek dan cakupan penyimpanan diperlukan.")
    for receipt_id in body.source_receipts:
        receipt_path(receipt_id)
    WORK.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise HTTPException(409, "Stop & Simpan masih berjalan. Tunggu hasilnya.") from None
    try:
        job = {"job_id": uuid4().hex, "project_id": body.project_id, "project_name": project["name"]}
        job_write(job, "queued", "Stop & Simpan diterima. Runtime belum dihentikan.")
        thread = threading.Thread(target=worker, args=(job, body.source_receipts, handle), daemon=True)
        with THREAD_LOCK:
            THREADS[job["job_id"]] = thread
        thread.start()
        return job
    except Exception:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        raise
