import base64
import fcntl
import json
import re
import shutil
import sqlite3
import tarfile
import threading
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from googleapiclient.http import MediaIoBaseDownload

from app.storage import atomic_json
from app.drive_storage import (
    storage_connection,
    project_folder_id,
    find_subfolder,
)
from app.training_backup import (
    BACKUPS,
    ADAPTER_NAMES,
    META_NAMES,
    sha256,
)
from app.training_jobs import WORK, load_job


ROOT = WORK / "model-library"
RUN = r"\d{8}T\d{6}Z-[a-f0-9]{8}"
JOB = r"[a-f0-9]{32}"
ACTIVE = {
    "queued",
    "downloading",
    "indexing",
    "uploading",
    "installing",
}
THREADS = {}
GUARD = threading.Lock()


def read(path):
    if path.is_symlink():
        raise ValueError("Berkas berupa symlink.")

    return json.loads(path.read_text(encoding="utf-8"))


def identity(job, project_id):
    return (
        job.get("project_id") == project_id
        and isinstance(project_id, int)
        and re.fullmatch(JOB, str(job.get("job_id", "")))
        and re.fullmatch(RUN, str(job.get("run_id", "")))
    )


def records(project_id):
    found = {}

    for path in (WORK / "history").glob("*.json"):
        try:
            job = read(path)

            if (
                identity(job, project_id)
                and (job.get("status") == "completed" or job.get("training_status") == "completed")
            ):
                found[job["job_id"]] = job

        except (ValueError, OSError, TypeError):
            continue

    current = load_job()

    if (
        identity(current, project_id)
        and (current.get("status") == "completed" or current.get("training_status") == "completed")
    ):
        found[current["job_id"]] = current

    for path in BACKUPS.glob("*/local-ready.json"):
        try:
            meta = read(path)

            if (
                not identity(meta, project_id)
                or meta["job_id"] in found
                or path.parent.name != meta["job_id"]
            ):
                continue

            manifest = read(
                path.parent / "model/CTH-MODEL-BACKUP.json"
            )
            run = read(path.parent / "model/run.json")
            status = read(path.parent / "model/status.json")

            if (
                any(
                    manifest.get(key) != meta[key]
                    for key in ("project_id", "job_id", "run_id")
                )
                or status.get("status") != "completed"
            ):
                continue

            receipt_path = path.parent / "drive-receipt.json"

            found[meta["job_id"]] = {
                **meta,
                "status": "completed",
                "model_name": run.get("settings", {}).get(
                    "model_name",
                    "Model tersimpan",
                ),
                "backup": (
                    read(receipt_path)
                    if receipt_path.is_file()
                    else {}
                ),
            }

        except (ValueError, OSError, TypeError):
            continue

    return sorted(
        found.values(),
        key=lambda job: job["run_id"],
        reverse=True,
    )


def select(project_id, job_id=None):
    jobs = records(project_id)

    if job_id:
        if not re.fullmatch(JOB, job_id):
            raise HTTPException(400, "ID model tidak valid.")

        job = next(
            (
                item for item in jobs
                if item["job_id"] == job_id
            ),
            None,
        )
    else:
        job = jobs[0] if jobs else None

    if job_id and job is None:
        raise HTTPException(
            404,
            "Model selesai tidak ditemukan pada proyek ini.",
        )

    return job


def catalog(project_id):
    result = []

    for job in records(project_id):
        receipt = job.get("backup") or {}
        local = BACKUPS / job["job_id"] / "model.tar"
        ready = local.parent / "local-ready.json"
        receipt_path = local.parent / "drive-receipt.json"

        if not receipt and receipt_path.is_file():
            receipt = read(receipt_path)

        result.append({
            "job_id": job["job_id"],
            "run_id": job["run_id"],
            "model_name": (
                job.get("model_name") or "Model tersimpan"
            ),
            "backup_verified": receipt.get("verified") is True,
            "local_available": (
                local.is_file()
                and ready.is_file()
                and not local.is_symlink()
            ),
            "size": receipt.get("size"),
        })

    return result


def progress(project_id, restore_id, **extra):
    ROOT.mkdir(parents=True, exist_ok=True)

    path = ROOT / f"restore-{project_id}.json"
    previous = read(path) if path.is_file() else {}

    if previous.get("restore_id") not in (None, restore_id):
        return

    atomic_json(path, {
        **previous,
        "project_id": project_id,
        "restore_id": restore_id,
        **extra,
    })


def restore_state(project_id):
    path = ROOT / f"restore-{project_id}.json"

    if not path.is_file():
        return {
            "status": "idle",
            "project_id": project_id,
        }

    value = read(path)

    if value.get("status") in ACTIVE:
        thread = THREADS.get(project_id)

        if not thread or not thread.is_alive():
            value.update(
                status="uncertain",
                message=(
                    "Pemantau pemulihan terputus. "
                    "Periksa model sebelum mencoba lagi."
                ),
            )

    return value


def download_drive(
    drive,
    project_id,
    file_id,
    category,
    target,
    size,
    digest,
):
    if (
        not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            str(file_id),
        )
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            str(digest),
        )
    ):
        raise ValueError("Identitas cadangan tidak valid.")

    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size > 2 * 1024**3
    ):
        raise ValueError("Ukuran cadangan tidak valid.")

    folder = find_subfolder(
        drive,
        project_folder_id(project_id),
        category,
    )

    meta = drive.files().get(
        fileId=file_id,
        fields="size,parents,trashed,appProperties",
    ).execute()

    props = meta.get("appProperties") or {}

    if (
        meta.get("trashed")
        or folder not in meta.get("parents", [])
        or int(meta.get("size", -1)) != size
    ):
        raise ValueError(
            "Cadangan Drive tidak berada pada "
            "folder proyek yang sesuai."
        )

    if (
        props.get("cth_project_id")
        not in (None, str(project_id))
        or props.get("cth_sha256") not in (None, digest)
    ):
        raise ValueError(
            "Identitas atau checksum Drive tidak sesuai."
        )

    if (
        shutil.disk_usage(target.parent).free
        < size * 2 + 128 * 1024 * 1024
    ):
        raise ValueError(
            "Disk laptop tidak cukup untuk pemulihan."
        )

    partial = target.with_suffix(
        target.suffix + ".part"
    )

    with partial.open("wb") as handle:
        downloader = MediaIoBaseDownload(
            handle,
            drive.files().get_media(fileId=file_id),
            chunksize=1024 * 1024,
        )

        done = False

        while not done:
            _, done = downloader.next_chunk(num_retries=2)

            if handle.tell() > size:
                raise ValueError(
                    "Ukuran unduhan melebihi cadangan."
                )

    if (
        partial.stat().st_size != size
        or sha256(partial) != digest
    ):
        raise ValueError(
            "Unduhan cadangan belum terverifikasi."
        )

    partial.replace(target)


def archive_for(job, work, drive):
    local = BACKUPS / job["job_id"] / "model.tar"
    ready = local.parent / "local-ready.json"

    if (
        local.is_file()
        and ready.is_file()
        and not local.is_symlink()
    ):
        meta = read(ready)

        if (
            identity(meta, job["project_id"])
            and meta["job_id"] == job["job_id"]
            and meta["run_id"] == job["run_id"]
        ):
            if (
                local.stat().st_size == meta.get("size")
                and sha256(local) == meta.get("sha256")
            ):
                return local

    receipt = job.get("backup") or {}
    receipt_path = local.parent / "drive-receipt.json"

    if not receipt and receipt_path.is_file():
        receipt = read(receipt_path)

    if (
        receipt.get("verified") is not True
        or receipt.get("project_id") != job["project_id"]
    ):
        raise ValueError(
            "Tidak ada arsip lokal atau catatan cadangan "
            "Drive terverifikasi untuk model ini."
        )

    target = work / "model.tar"

    download_drive(
        drive,
        job["project_id"],
        receipt.get("file_id"),
        "models",
        target,
        receipt.get("size"),
        receipt.get("sha256"),
    )

    return target


def unpack(archive, target, job):
    if target.is_symlink() or target.resolve() != target:
        raise ValueError("Direktori ekstraksi tidak aman.")
    with tarfile.open(archive, "r:*") as tar:
        entries = []
        for entry in tar:
            entries.append(entry)
            if len(entries) > 100 or entry.size < 0 or entry.size > 2 * 1024**3 or sum(item.size for item in entries) > 2 * 1024**3:
                raise ValueError("Arsip model melebihi batas ukuran atau jumlah berkas.")
        names = [entry.name for entry in entries]

        if (
            len(names) != len(set(names))
            or len(names) > 100
        ):
            raise ValueError(
                "Arsip memiliki entri duplikat "
                "atau terlalu banyak berkas."
            )

        for entry in entries:
            name = PurePosixPath(entry.name)

            allowed = entry.name in (
                META_NAMES | {"CTH-MODEL-BACKUP.json"}
            )

            allowed = allowed or (
                name.parent == PurePosixPath("adapter")
                and name.name in ADAPTER_NAMES
            )

            if (
                not allowed
                or not entry.isfile()
                or name.is_absolute()
                or ".." in name.parts
            ):
                raise ValueError(
                    "Isi arsip model tidak diizinkan."
                )

        marker = tar.getmember("CTH-MODEL-BACKUP.json")

        if marker.size > 1024 * 1024:
            raise ValueError("Manifest model terlalu besar.")

        with tar.extractfile(marker) as handle:
            manifest = json.load(handle)

        for key in ("project_id", "job_id", "run_id"):
            if manifest.get(key) != job[key]:
                raise ValueError(
                    "Identitas manifest berbeda "
                    "dari model yang dipilih."
                )

        records_list = manifest.get("files", [])

        required = {
            "run.json",
            "complete.json",
            "status.json",
            "adapter/adapter_config.json",
            "adapter/adapter_model.safetensors",
            "adapter/tokenizer_config.json",
        }

        if (
            not isinstance(records_list, list)
            or any(not isinstance(record, dict) for record in records_list)
            or len(records_list) != len(names) - 1
            or {
                record.get("path")
                for record in records_list
            } != set(names) - {"CTH-MODEL-BACKUP.json"}
            or not required <= set(names)
        ):
            raise ValueError(
                "Isi arsip model belum lengkap."
            )

        if (
            sum(entry.size for entry in entries) * 2
            + 128 * 1024 * 1024
            > shutil.disk_usage(target.parent).free
        ):
            raise ValueError(
                "Disk laptop tidak cukup untuk membuka arsip."
            )

        for record in records_list:
            entry = tar.getmember(record["path"])

            if (
                entry.size != record.get("size")
                or (
                    entry.name in required
                    and entry.size <= 0
                )
                or not re.fullmatch(
                    r"[a-f0-9]{64}",
                    str(record.get("sha256", "")),
                )
            ):
                raise ValueError(
                    "Metadata berkas arsip tidak sesuai."
                )

            destination = target / record["path"]
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with (
                tar.extractfile(entry) as source,
                destination.open("xb") as output,
            ):
                shutil.copyfileobj(
                    source,
                    output,
                    1024 * 1024,
                )

            if sha256(destination) != record["sha256"]:
                raise ValueError(
                    "Checksum isi arsip model tidak sesuai."
                )

    run = read(target / "run.json")
    status = read(target / "status.json")
    adapter = read(target / "adapter/adapter_config.json")
    settings = run.get("settings", {})

    if (
        status.get("status") != "completed"
        or status.get("run_id") != job["run_id"]
    ):
        raise ValueError(
            "Status model pada arsip "
            "bukan hasil training selesai."
        )

    if (
        settings.get("model_id")
        != manifest.get("base_model")
        or settings.get("model_revision")
        != manifest.get("base_revision")
        or adapter.get("base_model_name_or_path")
        != manifest.get("base_model")
    ):
        raise ValueError(
            "Referensi model dasar pada arsip tidak cocok."
        )

    return manifest


def rebuild_index(job, manifest, work, run, drive):
    from contextlib import closing
    from importlib.util import spec_from_file_location, module_from_spec
    sources = manifest.get('source_backups') or []
    if not sources or len(sources) > 500:
        raise ValueError('Cadangan TXT untuk indeks pengujian tidak tersedia.')
    code = Path(__file__).resolve().parents[2] / 'training/retrieval.py'
    if code.is_symlink() or not code.is_file():
        raise ValueError('Kode retrieval tidak tersedia.')
    spec = spec_from_file_location('cth_retrieval', code)
    retrieval = module_from_spec(spec)
    spec.loader.exec_module(retrieval)
    with closing(sqlite3.connect(run / 'dataset.sqlite')) as connection:
        retrieval.create_index(connection)
        reports = []
        for index, meta in enumerate(sources):
            text_file = work / f'source-{index}.txt'
            download_drive(drive, job['project_id'], meta.get('file_id'), 'datasets',
                           text_file, meta.get('size'), meta.get('sha256'))
            label = f"{meta.get('name', 'TXT tersimpan')} (Drive: {meta['file_id']})"
            with text_file.open(encoding='utf-8', errors='strict') as handle:
                report = retrieval.index_source(connection, label, handle, meta.get('source_mode', 'filtered'))
                report.update(name=meta.get('name'), file_id=meta['file_id'], sha256=meta.get('sha256'), size=meta.get('size'), dataset_id=meta.get('dataset_id'))
                reports.append(report)
            text_file.unlink()
        connection.commit()
    report = retrieval.source_summary(reports)
    atomic_json(run / 'source-index-version.json', {'version': 2, 'source_analysis': report})
    return report


RESTORE = r'''
import fcntl, hashlib, json, os, shutil, subprocess, sys, tarfile, tempfile
from contextlib import ExitStack
from pathlib import Path

cfg = json.loads(__CONFIG__)
project = Path('/content/cth/projects') / str(cfg['project_id'])

if project.resolve() != project:
    raise RuntimeError('Workspace tidak aman')

project.mkdir(parents=True, exist_ok=True)

with ExitStack() as locks:
    for name in ('.cth-web.lock', '.cth-worker.lock'):
        p = project / name

        if p.is_symlink():
            raise RuntimeError('Lock tidak aman')

        h = locks.enter_context(p.open('a'))
        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)

    archive = Path(cfg['upload'])
    digest = hashlib.sha256()

    with archive.open('rb') as handle:
        for block in iter(
            lambda: handle.read(1024 * 1024),
            b'',
        ):
            digest.update(block)

    if digest.hexdigest() != cfg['sha256']:
        raise RuntimeError('Arsip transfer tidak cocok')

    results = project / 'training_results'
    results.mkdir(exist_ok=True)

    if results.resolve() != results:
        raise RuntimeError('Folder hasil tidak aman')

    run = results / cfg['run_id']

    with tempfile.TemporaryDirectory(
        prefix='.restore-',
        dir=results,
    ) as temporary:
        stage = Path(temporary) / 'run'
        stage.mkdir()

        with tarfile.open(archive, 'r:gz') as tar:
            members = tar.getmembers()

            if (
                {member.name for member in members}
                != set(cfg['files'])
                or len(members) != len(cfg['files'])
            ):
                raise RuntimeError(
                    'Daftar transfer tidak sesuai'
                )

            for member in members:
                relative = Path(member.name)

                if (
                    not member.isfile()
                    or relative.is_absolute()
                    or '..' in relative.parts
                ):
                    raise RuntimeError(
                        'Entri transfer tidak valid'
                    )

                target = stage / relative
                target.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with (
                    tar.extractfile(member) as source,
                    target.open('xb') as destination,
                ):
                    shutil.copyfileobj(
                        source,
                        destination,
                        1024 * 1024,
                    )

                digest = hashlib.sha256()

                with target.open('rb') as source:
                    for block in iter(
                        lambda: source.read(1024 * 1024),
                        b'',
                    ):
                        digest.update(block)

                if (
                    digest.hexdigest()
                    != cfg['files'][member.name]
                ):
                    raise RuntimeError(
                        'Checksum berkas transfer tidak cocok'
                    )

        if run.exists():
            if run.resolve() != run:
                raise RuntimeError('Run berupa symlink')

            for name, expected in cfg['files'].items():
                target = run / name

                if name in {'dataset.sqlite', 'source-index-version.json'}:
                    if target.is_symlink():
                        raise RuntimeError('Indeks sumber berupa symlink')
                    continue

                if (
                    target.resolve() != target
                    or not target.is_file()
                ):
                    raise RuntimeError(
                        'Run lama belum lengkap; '
                        'tidak ditimpa otomatis'
                    )

                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if digest != expected:
                    raise RuntimeError('Run lama berbeda; tidak ditimpa otomatis')
            os.replace(stage / 'dataset.sqlite', run / 'dataset.sqlite')
            os.replace(stage / 'source-index-version.json', run / 'source-index-version.json')
        else:
            stage.rename(run)

    environment = (
        Path('/tmp/cth-inference-env')
        / cfg['job_id']
    )

    environment.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    log = project / (
        'restore-' + cfg['job_id'] + '.log'
    )

    requirements = project / (
        'inference-' + cfg['job_id'] + '.txt'
    )

    requirements.write_text(
        cfg['requirements'],
        encoding='utf-8',
    )

    with log.open('ab') as output:
        subprocess.run(
            [
                sys.executable,
                '-m',
                'venv',
                '--without-pip',
                '--system-site-packages',
                str(environment),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
        )

        subprocess.run(
            [
                sys.executable,
                '-m',
                'pip',
                '--python',
                str(environment),
                'install',
                '--disable-pip-version-check',
                '-r',
                str(requirements),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
        )

        subprocess.run(
            [
                str(environment / 'bin/python'),
                '-c',
                (
                    "import torch, transformers, peft, safetensors; "
                    "assert torch.cuda.is_available(); "
                    "assert 'T4' in torch.cuda.get_device_name(0)"
                ),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
        )

    archive.unlink()

print(
    'CTH_CONTROL:'
    + json.dumps({
        'ok': True,
        'restored': True,
        'run_id': cfg['run_id'],
    })
)
'''


def restore_worker(job, restore_id, handle, open_bridge):
    project_id = job["project_id"]
    bridge = None

    try:
        work = ROOT / restore_id
        work.mkdir(parents=True, mode=0o700)

        progress(
            project_id,
            restore_id,
            status="downloading",
            message="Memeriksa arsip model dan cadangan TXT.",
            job_id=job["job_id"],
        )

        bridge = open_bridge(project_id)
        bridge.require_idle_other_kernels()

        drive = storage_connection()
        archive = archive_for(job, work, drive)

        run = work / "run"
        run.mkdir()

        manifest = unpack(archive, run, job)

        progress(
            project_id,
            restore_id,
            status="indexing",
            message=(
                "Membangun indeks pencarian dari TXT "
                "tersimpan. Tidak melatih model."
            ),
        )

        if job.get('inference_sources') is not None:
            manifest = {**manifest, 'source_backups': job['inference_sources']}
        index_report = rebuild_index(
            job,
            manifest,
            work,
            run,
            drive,
        )

        requirements = (
            Path(__file__).resolve().parents[2]
            / "requirements-training.txt"
        ).read_text(encoding="utf-8")

        names = [
            str(path.relative_to(run))
            for path in sorted(run.rglob("*"))
            if path.is_file()
        ]

        checksums = {
            name: sha256(run / name)
            for name in names
        }

        bundle = work / "restore.tar.gz"

        with tarfile.open(
            bundle,
            "w:gz",
            compresslevel=1,
        ) as tar:
            for name in names:
                tar.add(
                    run / name,
                    arcname=name,
                    recursive=False,
                )

        bridge.refresh_connection()

        upload = (
            "/tmp/cth-restore-"
            + restore_id
            + ".tar.gz"
        )

        progress(
            project_id,
            restore_id,
            status="uploading",
            message="Mengirim model dan indeks ke runtime T4.",
            percent=0,
        )

        offset = 0
        size = bundle.stat().st_size

        with bundle.open("rb") as source:
            while block := source.read(512 * 1024):
                encoded = base64.b64encode(
                    block
                ).decode()

                mode = "xb" if offset == 0 else "ab"

                code = (
                    "import base64,json\n"
                    "from pathlib import Path\n"
                    f"p=Path({upload!r})\n"
                    f"chunk=base64.b64decode({encoded!r})\n"
                    f"with p.open({mode!r}) as f:\n"
                    f"    if f.tell() != {offset}: "
                    'raise RuntimeError("Offset berubah")\n'
                    "    f.write(chunk)\n"
                    'print("CTH_CONTROL:"+'
                    'json.dumps({"ok":True}))\n'
                )

                bridge.execute(code, timeout=120)
                offset += len(block)

                progress(
                    project_id,
                    restore_id,
                    percent=int(offset * 100 / size),
                )

        progress(
            project_id,
            restore_id,
            status="installing",
            message=(
                "Memverifikasi berkas dan menyiapkan "
                "Python untuk inferensi. "
                "Tidak menjalankan training."
            ),
        )

        config = {
            "project_id": project_id,
            "job_id": job["job_id"],
            "run_id": job["run_id"],
            "upload": upload,
            "sha256": sha256(bundle),
            "files": checksums,
            "requirements": requirements,
        }

        code = RESTORE.replace(
            "__CONFIG__",
            repr(json.dumps(config)),
        )

        result = bridge.execute(
            code,
            timeout=1500,
        )

        if (
            not result.get("restored")
            or result.get("run_id") != job["run_id"]
        ):
            raise ValueError(
                "Pemulihan belum terkonfirmasi."
            )

        progress(
            project_id,
            restore_id,
            status="completed",
            message="Model dipulihkan. " + ("Indeks sumber tersedia; kecocokan jawaban tetap perlu diuji." if index_report['sources_ready'] else index_report['warning']),
            source_analysis=index_report,
            percent=100,
            run_id=job["run_id"],
        )

    except Exception as error:
        if isinstance(error, ValueError):
            message = str(error)[:400]
        else:
            message = (
                "Pemulihan belum berhasil. "
                "Cadangan tetap tersedia; periksa koneksi "
                "dan log restore di Colab."
            )

        pending = (
            bridge is not None
            and getattr(
                bridge,
                "execution_pending",
                False,
            )
        )

        progress(
            project_id,
            restore_id,
            status="uncertain" if pending else "failed",
            message=(
                "Koneksi kontrol terputus; pemulihan "
                "mungkin masih berjalan. Periksa model, "
                "jangan langsung mengulang."
                if pending
                else message
            ),
        )

    finally:
        if bridge:
            bridge.close()

        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def begin_restore(job, open_bridge):
    with GUARD:
        current = load_job() or {}

        if current.get("status", "idle") not in {
            "idle",
            "completed",
            "failed",
            "stopped",
        }:
            raise HTTPException(
                409,
                "Ada training atau pekerjaan yang statusnya "
                "belum pasti. Periksa dahulu sebelum pemulihan.",
            )

        thread = THREADS.get(job["project_id"])

        if thread and thread.is_alive():
            return restore_state(job["project_id"])

        WORK.mkdir(parents=True, exist_ok=True)

        handle = (WORK / "launch.lock").open("a")

        try:
            fcntl.flock(
                handle,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            handle.close()

            raise HTTPException(
                409,
                "Pekerjaan lain sedang memakai runtime.",
            ) from None

        restore_id = uuid4().hex

        ROOT.mkdir(parents=True, exist_ok=True)

        atomic_json(
            ROOT / f'restore-{job["project_id"]}.json',
            {
                "project_id": job["project_id"],
                "restore_id": restore_id,
                "job_id": job["job_id"],
                "status": "queued",
                "message": (
                    "Pemulihan diminta. "
                    "Tidak menjalankan training."
                ),
            },
        )

        thread = threading.Thread(
            target=restore_worker,
            args=(
                job,
                restore_id,
                handle,
                open_bridge,
            ),
            daemon=True,
        )

        THREADS[job["project_id"]] = thread

        try:
            thread.start()
        except Exception:
            handle.close()
            raise

        return restore_state(job["project_id"])
