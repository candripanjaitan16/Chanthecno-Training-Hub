import fcntl
import json
import re
import shutil
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.storage import atomic_json
from app.colab_stop import local_only, project_row
from app.drive_storage import upload_project_file
from app.training_backup import (
    BackupError,
    directory,
    download,
    sha256,
    validate_job,
)

router = APIRouter(
    prefix="/training/checkpoints",
    tags=["Checkpoint Backups"],
)

from app.paths import WORKSPACE

ROOT = WORKSPACE / "checkpoint-backups"

INTERVAL = 300
RUN_PATTERN = r"\d{8}T\d{6}Z-[a-f0-9]{8}"

CHECKPOINT_NAMES = {
    "adapter_config.json",
    "adapter_model.safetensors",
    "adapter_model.safetensors.index.json",
    "optimizer.pt",
    "scheduler.pt",
    "trainer_state.json",
    "training_args.bin",
    "rng_state.pth",
    "scaler.pt",
    "cth-complete.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
    "chat_template.jinja",
}

CODE_NAMES = {
    "__init__.py",
    "common.py",
    "data.py",
    "train.py",
    "web_worker.py",
    "retrieval.py",
}


@contextmanager
def locked(job):
    validate_job(job)

    folder = ROOT / job["job_id"]
    folder.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    if folder.resolve() != folder:
        raise BackupError(
            "Folder cadangan checkpoint tidak aman."
        )

    with (folder / "backup.lock").open("a") as handle:
        try:
            fcntl.flock(
                handle,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            raise BackupError(
                "Cadangan checkpoint lain masih berjalan."
            ) from None

        yield folder


def files(items, allowed):
    selected = []

    for item in items:
        name = item.get("name", "")

        sharded = re.fullmatch(
            r"adapter_model-\d{5}-of-\d{5}\.safetensors",
            name,
        )

        if name not in allowed and not sharded:
            continue

        size = item.get("size")

        if (
            item.get("type") != "file"
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise BackupError(
                "Metadata berkas checkpoint tidak valid."
            )

        selected.append((name, item))

    if len({name for name, _ in selected}) != len(selected):
        raise BackupError(
            "Nama berkas checkpoint duplikat."
        )

    return selected


def signature(items):
    return sorted(
        (
            name,
            metadata["size"],
            metadata.get("last_modified"),
        )
        for name, metadata in items
    )


def remote_run(bridge, job):
    run_id = str(job.get("run_id", ""))

    if not re.fullmatch(RUN_PATTERN, run_id):
        return None

    base = (
        f"cth/projects/{job['project_id']}"
        f"/training_results/{run_id}"
    )

    for prefix in ("", "content/"):
        entries = directory(
            bridge,
            prefix + base,
            missing_ok=True,
        )

        if entries is not None:
            return prefix, prefix + base, entries

    return None


def pack(job, folder, label, sources, bridge, scope):
    destination = folder / label
    destination.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    total = sum(
        metadata["size"]
        for _, _, metadata in sources
    )

    required_space = total * 2 + 128 * 1024 * 1024

    if shutil.disk_usage(folder).free < required_space:
        raise BackupError(
            "Disk laptop tidak cukup untuk cadangan "
            "checkpoint. Tidak menghapus berkas lain."
        )

    manifest = {
        "version": 1,
        "project_id": job["project_id"],
        "job_id": job["job_id"],
        "run_id": job["run_id"],
        "kind": label,
        "scope": scope,
        "source_backups": job.get("source_backups", []),
        "files": [],
    }

    for relative, remote, metadata in sources:
        target = destination / "files" / relative

        if (
            target.resolve() != target
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            raise BackupError(
                "Lokasi cadangan checkpoint tidak valid."
            )

        record = download(
            bridge,
            remote,
            target,
            metadata,
        )

        record["path"] = relative
        manifest["files"].append(record)

    atomic_json(
        destination / "CTH-CHECKPOINT.json",
        manifest,
    )

    archive = folder / (label + ".tar")
    partial = archive.with_suffix(".tar.part")

    with tarfile.open(partial, "w") as tar:
        for relative, _, _ in sources:
            tar.add(
                destination / "files" / relative,
                arcname=relative,
                recursive=False,
            )

        tar.add(
            destination / "CTH-CHECKPOINT.json",
            arcname="CTH-CHECKPOINT.json",
            recursive=False,
        )

    partial.replace(archive)

    return {
        "project_id": job["project_id"],
        "job_id": job["job_id"],
        "run_id": job["run_id"],
        "label": label,
        "size": archive.stat().st_size,
        "sha256": sha256(archive),
        "status": "local_ready",
        "created_at": time.time(),
    }


def send(folder, record):
    label = record.get("label", "")

    if (
        label != "context"
        and not re.fullmatch(r"checkpoint-\d+", label)
    ):
        raise BackupError(
            "Nama cadangan checkpoint tidak valid."
        )

    if record.get("status") not in {"local_ready", "saved"}:
        raise BackupError(
            "Cadangan belum selesai divalidasi."
        )

    if label != "context":
        valid_context = (
            record.get("context_file_id")
            and re.fullmatch(
                r"[a-f0-9]{64}",
                record.get("context_sha256", ""),
            )
            and record.get("step")
            == int(label.split("-")[-1])
        )

        if not valid_context:
            raise BackupError(
                "Konteks resume checkpoint belum terverifikasi."
            )

    archive = folder / (label + ".tar")

    if (
        archive.is_symlink()
        or not archive.is_file()
        or archive.stat().st_size != record["size"]
        or sha256(archive) != record["sha256"]
    ):
        raise BackupError(
            "Arsip checkpoint lokal berubah atau belum lengkap."
        )

    receipt = record.get("receipt") or {}

    if (
        receipt.get("verified") is True
        and receipt.get("sha256") == record["sha256"]
        and receipt.get("size") == record["size"]
    ):
        return record

    receipt = upload_project_file(
        record["project_id"],
        archive,
        "training_results",
    )

    if (
        receipt.get("verified") is not True
        or receipt.get("sha256") != record["sha256"]
        or receipt.get("size") != record["size"]
    ):
        raise BackupError(
            "Cadangan Drive belum terverifikasi. "
            "Salinan checkpoint lokal dipertahankan."
        )

    record = {
        **record,
        "status": "saved",
        "receipt": receipt,
    }

    atomic_json(
        folder / (label + ".json"),
        record,
    )

    return record


def context_backup(
    bridge,
    job,
    folder,
    prefix,
    remote,
    entries,
):
    record_path = folder / "context.json"

    if record_path.is_file():
        return json.loads(
            record_path.read_text(encoding="utf-8")
        )

    chosen = files(
        entries,
        {
            "run.json",
            "dataset.sqlite",
            "dataset_stats.json",
        },
    )

    names = {name for name, _ in chosen}

    if not {"run.json", "dataset.sqlite"} <= names:
        raise BackupError(
            "Metadata run atau indeks dataset "
            "belum tersedia untuk resume."
        )

    sources = [
        (
            name,
            remote + "/" + name,
            metadata,
        )
        for name, metadata in chosen
    ]

    base = (
        f"{prefix}cth/projects/{job['project_id']}"
        f"/jobs/{job['job_id']}/code"
    )

    code = files(
        directory(bridge, base + "/training"),
        CODE_NAMES,
    )

    if {name for name, _ in code} != CODE_NAMES:
        raise BackupError(
            "Snapshot kode worker belum lengkap."
        )

    sources.extend(
        (
            "code/training/" + name,
            base + "/training/" + name,
            metadata,
        )
        for name, metadata in code
    )

    requirements = files(
        directory(bridge, base),
        {"requirements-training.txt"},
    )

    if not requirements:
        raise BackupError(
            "Snapshot dependensi training belum ditemukan."
        )

    sources.extend(
        (
            "code/" + name,
            base + "/" + name,
            metadata,
        )
        for name, metadata in requirements
    )

    record = pack(
        job,
        folder,
        "context",
        sources,
        bridge,
        (
            "Metadata run, indeks sumber, kode dan "
            "dependensi asli. Dataset mentah mengacu "
            "source_backups di Drive."
        ),
    )

    settings = json.loads(
        (folder / "context/files/run.json").read_text(
            encoding="utf-8"
        )
    ).get("settings", {})

    if (
        not settings.get("model_revision")
        or not settings.get("sources")
    ):
        raise BackupError(
            "Pengaturan run untuk resume belum lengkap."
        )

    atomic_json(record_path, record)

    return record


def sync_checkpoint(job, bridge):
    with locked(job) as folder:
        for path in sorted(folder.glob("checkpoint-*.json")):
            record = json.loads(
                path.read_text(encoding="utf-8")
            )

            if record.get("status") == "saved":
                continue

            context = send(
                folder,
                json.loads(
                    (folder / "context.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )

            record["context_file_id"] = (
                context["receipt"]["file_id"]
            )

            atomic_json(path, record)
            send(folder, record)

        remote = remote_run(bridge, job)

        if remote is None:
            return {}

        prefix, run, entries = remote

        checkpoints = directory(
            bridge,
            run + "/checkpoints",
            missing_ok=True,
        )

        if checkpoints is None:
            return {}

        candidates = sorted(
            [
                item["name"]
                for item in checkpoints
                if (
                    item.get("type") == "directory"
                    and re.fullmatch(
                        r"checkpoint-\d+",
                        item.get("name", ""),
                    )
                )
            ],
            key=lambda name: int(name.split("-")[-1]),
            reverse=True,
        )

        required = {
            "cth-complete.json",
            "adapter_config.json",
            "trainer_state.json",
            "optimizer.pt",
            "scheduler.pt",
            "rng_state.pth",
            "adapter_model.safetensors",
        }

        selected = None

        for name in candidates:
            chosen = files(
                directory(
                    bridge,
                    run + "/checkpoints/" + name,
                ),
                CHECKPOINT_NAMES,
            )

            names = {item[0] for item in chosen}

            if (
                required <= names
                and all(
                    metadata["size"] > 0
                    for _, metadata in chosen
                )
            ):
                selected = name, chosen
                break

        if selected is None:
            return {}

        name, chosen = selected
        step = int(name.split("-")[-1])
        record_path = folder / (name + ".json")

        if record_path.is_file():
            send(
                folder,
                json.loads(
                    record_path.read_text(encoding="utf-8")
                ),
            )

            return {
                "checkpoint_step": step,
                "checkpoint_backup_status": "saved",
                "checkpoint_error": None,
            }

        saved = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in folder.glob("checkpoint-*.json")
        ]

        if saved:
            latest_time = max(
                item.get("created_at", 0)
                for item in saved
            )

            if (
                time.time() - latest_time < INTERVAL
                and job.get("training_status") == "training"
            ):
                return {}

        context = context_backup(
            bridge,
            job,
            folder,
            prefix,
            run,
            entries,
        )

        checkpoint = run + "/checkpoints/" + name

        sources = [
            (
                "checkpoint/" + item,
                checkpoint + "/" + item,
                metadata,
            )
            for item, metadata in chosen
        ]

        record = pack(
            job,
            folder,
            name,
            sources,
            bridge,
            (
                "Checkpoint Trainer: adapter, optimizer, "
                "scheduler, RNG dan posisi langkah; "
                "model dasar tidak disertakan."
            ),
        )

        local = folder / name / "files/checkpoint"

        state = json.loads(
            (local / "trainer_state.json").read_text(
                encoding="utf-8"
            )
        )

        marker = json.loads(
            (local / "cth-complete.json").read_text(
                encoding="utf-8"
            )
        )

        if (
            state.get("global_step") != step
            or marker.get("step") != step
        ):
            raise BackupError(
                "Posisi checkpoint tidak sesuai "
                "penanda selesai."
            )

        latest = files(
            directory(bridge, checkpoint),
            CHECKPOINT_NAMES,
        )

        if signature(chosen) != signature(latest):
            raise BackupError(
                "Checkpoint berubah saat disalin. "
                "Belum diunggah ke Drive."
            )

        record.update(
            context_sha256=context["sha256"],
            context_file_id=(
                context.get("receipt") or {}
            ).get("file_id"),
            step=step,
        )

        atomic_json(record_path, record)

        context = send(folder, context)

        record["context_file_id"] = (
            context["receipt"]["file_id"]
        )

        atomic_json(record_path, record)
        send(folder, record)

        return {
            "checkpoint_step": step,
            "checkpoint_backup_status": "saved",
            "checkpoint_error": None,
        }


@router.get("/{project_id}")
def list_checkpoints(project_id: int, request: Request):
    local_only(request)
    project_row(project_id)

    records = []

    if ROOT.is_dir():
        for path in ROOT.glob("*/checkpoint-*.json"):
            record = json.loads(
                path.read_text(encoding="utf-8")
            )

            if record.get("project_id") != project_id:
                continue

            records.append({
                "job_id": record["job_id"],
                "run_id": record["run_id"],
                "step": record.get("step"),
                "status": record.get("status"),
                "size": record.get("size"),
                "sha256": record.get("sha256"),
                "file_id": (
                    record.get("receipt") or {}
                ).get("file_id"),
                "local_exists": (
                    path.with_suffix(".tar").is_file()
                ),
            })

    return sorted(
        records,
        key=lambda row: row.get("step") or 0,
        reverse=True,
    )


class RetryRequest(BaseModel):
    project_id: int = Field(gt=0)


@router.post("/retry")
def retry_checkpoints(
    body: RetryRequest,
    request: Request,
):
    local_only(request)
    project_row(body.project_id)

    count = 0

    try:
        if ROOT.is_dir():
            for context_path in ROOT.glob("*/context.json"):
                context = json.loads(
                    context_path.read_text(encoding="utf-8")
                )

                if (
                    context.get("project_id")
                    != body.project_id
                ):
                    continue

                with locked(context) as folder:
                    context = send(folder, context)

                    paths = sorted(
                        folder.glob("checkpoint-*.json")
                    )

                    for path in paths:
                        record = json.loads(
                            path.read_text(encoding="utf-8")
                        )

                        if (
                            record.get("project_id")
                            != body.project_id
                            or record.get("job_id")
                            != context["job_id"]
                        ):
                            raise BackupError(
                                "Identitas checkpoint tidak cocok."
                            )

                        record["context_file_id"] = (
                            context["receipt"]["file_id"]
                        )

                        atomic_json(path, record)
                        send(folder, record)
                        count += 1

        return {
            "status": "checked",
            "project_id": body.project_id,
            "checkpoint_count": count,
        }

    except Exception:
        raise HTTPException(
            503,
            (
                "Cadangan checkpoint belum berhasil "
                "diverifikasi di Drive. Salinan lokal "
                "dipertahankan; tidak menjalankan training."
            ),
        ) from None