import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
LLAMA_COMMIT = "0ec4027a2a0183de87c007069adddc06e665f7de"
REFUSAL = "Informasi tersebut tidak ditemukan dalam data proyek."
SYSTEM = "Jawab dalam bahasa Indonesia berdasarkan SUMBER yang diberikan. Jangan menambahkan fakta di luar sumber. Jika informasi tidak tersedia, jawab: " + REFUSAL


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".cth-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checkpoint_state(directory):
    required = ("optimizer.pt", "scheduler.pt", "trainer_state.json", "rng_state.pth", "adapter_config.json", "adapter_model.safetensors")
    result = {}
    for name in required:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Checkpoint belum memiliki state optimizer/scheduler/RNG dan adapter lengkap.")
        result[name] = file_hash(path)
    return result


def validate_checkpoint(directory):
    marker = read_json(directory / "cth-complete.json")
    files = checkpoint_state(directory)
    if not marker.get("files") or files != marker["files"]:
        raise ValueError("Integritas checkpoint belum terverifikasi; tidak mengklaim resume persis.")
    return directory


def script_hash():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_name(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", name):
        raise ValueError("Nama model harus 1–80 karakter: huruf, angka, _ atau -.")
    return name


def child(directory, name):
    validate_name(name)
    directory = Path(directory).resolve()
    target = (directory / name).resolve()
    if target.parent != directory:
        raise ValueError("Lokasi berkas tidak diizinkan.")
    return target


@contextmanager
def project_lock(project):
    import fcntl
    project = Path(project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    with (project / ".cth-worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Proyek ini sedang dipakai pekerjaan lain.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def require_t4():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA tidak tersedia. Tidak beralih ke CPU.")
    name = torch.cuda.get_device_name(0)
    if "T4" not in name.upper():
        raise RuntimeError(f"GPU yang terdeteksi adalah {name}; pekerjaan ini khusus T4.")
    return {"name": name, "vram_bytes": torch.cuda.get_device_properties(0).total_memory}
