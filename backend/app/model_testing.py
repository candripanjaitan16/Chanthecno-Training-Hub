import base64
import fcntl
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.colab_bridge import RuntimeBridge
from app.colab_stop import local_only, project_row
from app.training_jobs import WORK
from app.model_restore import catalog, select, begin_restore, restore_state

router = APIRouter(prefix="/api/model-testing", tags=["Model Testing"])
REPO = Path(__file__).resolve().parents[2]
RUN_PATTERN = r"\d{8}T\d{6}Z-[a-f0-9]{8}"


class ChatRequest(BaseModel):
    project_id: int = Field(gt=0)
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["qwen", "extractive"] = "qwen"
    job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")



def selected_job(project_id, job_id=None):
    return select(project_id, job_id)


def model_state(project_id, job):
    stats = (job or {}).get('dataset_stats')
    counts = stats.get('counts') if isinstance(stats, dict) else None
    base = {
        "project_id": project_id,
        "model_name": (job or {}).get("model_name"),
        "run_id": (job or {}).get("run_id"),
        "loaded": False,
        "job_id": (job or {}).get("job_id"),
        "training_sample_counts": counts,
    }
    if not job:
        return {**base, "status": "missing", "message": "Belum ada catatan training untuk proyek ini."}
    if job.get("status") != "completed" and job.get("training_status") != "completed":
        return {**base, "status": "not_ready", "message": "Tunggu pekerjaan training dan penyimpanan hasil selesai."}
    if not re.fullmatch(RUN_PATTERN, str(job.get("run_id", ""))):
        return {**base, "status": "missing", "message": "ID run hasil training belum tersedia."}
    return base


def open_bridge(project_id):
    from app.colab import RUNTIME_NAME, T4_SPEC, load_state, runtime_lock

    with runtime_lock():
        if load_state().get("project_id") != project_id:
            raise HTTPException(409, "Hubungkan T4 untuk proyek ini melalui Colab Connection.")
        return RuntimeBridge(RUNTIME_NAME, T4_SPEC)


def contents(bridge, path):
    with bridge.http.get(
        bridge.url("api/contents/" + quote(path, safe="/")),
        params={"content": 1, "type": "directory"},
        timeout=(10, 45), allow_redirects=False,
    ) as response:
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError("Daftar berkas model belum dapat diperiksa.")
        data = response.json()
    if data.get("type") != "directory" or not isinstance(data.get("content"), list):
        raise RuntimeError("Respons daftar berkas model tidak sesuai.")
    return data["content"]


def files_present(items, names):
    files = {
        item.get("name") for item in items
        if item.get("type") == "file" and item.get("size", 0) > 0
    }
    return names <= files


def inspect_model(bridge, project_id, job):
    relative = f"cth/projects/{project_id}/training_results/{job['run_id']}"
    for prefix in ("", "content/"):
        run = contents(bridge, prefix + relative)
        if run is None:
            continue
        if not files_present(run, {"run.json", "complete.json", "dataset.sqlite", "source-index-version.json"}):
            return False
        adapter = contents(bridge, prefix + relative + "/adapter")
        return adapter is not None and files_present(
            adapter, {"adapter_config.json", "adapter_model.safetensors", "tokenizer_config.json"}
        )
    return False


def source_index_state(bridge, project_id, job):
    from app.training_backup import read_json_file
    for prefix in ('', 'content/'):
        try:
            path = f"{prefix}cth/projects/{project_id}/training_results/{job['run_id']}/source-index-version.json"
            marker = read_json_file(bridge, path, missing_ok=True)
            if not isinstance(marker, dict):
                continue
            report = marker.get('source_analysis')
            if isinstance(report, dict) and isinstance(report.get('searchable_quotes'), int):
                count = report['searchable_quotes']
                if count >= 0:
                    return {'sources_ready': count > 0, 'source_analysis': report,
                            'source_warning': None if count else 'Indeks sumber kosong. Model dapat memakai pengetahuan umum; tidak ada kutipan proyek yang siap dicari.'}
        except Exception:
            pass
    return {'sources_ready': None, 'source_analysis': None,
            'source_warning': 'Jumlah kutipan sumber belum dapat diverifikasi. Jangan menganggap indeks siap; periksa/pulihkan sumber.'}


@contextmanager
def inference_lock():
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "launch.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, "Pekerjaan lain masih berjalan. Jangan mengirim pertanyaan berulang.") from None
        yield


def source_bundle():
    files = {}
    for name in ("common.py", "source_answer.py", "retrieval.py"):
        path = REPO / "training" / name
        if path.resolve() != path or not path.is_file() or path.stat().st_size > 262144:
            raise HTTPException(503, f"Kode training/{name} belum tersedia atau tidak valid.")
        files[name] = base64.b64encode(path.read_bytes()).decode("ascii")
    return files


REMOTE = r'''
import base64
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

config = json.loads(base64.b64decode(__PAYLOAD__))
project = Path("/content/cth/projects") / str(config["project_id"])
run = project / "training_results" / config["run_id"]
result = {"ok": True, "error_code": "inference_failed"}

try:
    required = [run / "complete.json", run / "run.json", run / "dataset.sqlite",
                run / "adapter/adapter_config.json", run / "adapter/adapter_model.safetensors"]
    if any(path.resolve() != path or not path.is_file() or path.stat().st_size == 0 for path in required):
        result["error_code"] = "model_missing"
    else:
        python = Path("/tmp/cth-inference-env") / config["job_id"] / "bin/python"
        if not python.is_file():
            python = Path("/tmp/cth-training-env") / config["job_id"] / "bin/python"
        if config["mode"] == "extractive":
            python = Path(sys.executable)
        if not python.is_file():
            result["error_code"] = "environment_missing"
        else:
            with ExitStack() as stack:
                for name in (".cth-web.lock", ".cth-worker.lock"):
                    handle = stack.enter_context((project / name).open("a"))
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with tempfile.TemporaryDirectory(prefix="cth-inference-") as temporary:
                    package = Path(temporary) / "training"
                    package.mkdir()
                    (package / "__init__.py").write_text("", encoding="utf-8")
                    for name, content in config["files"].items():
                        (package / name).write_bytes(base64.b64decode(content))
                    env = os.environ.copy()
                    env.update(PYTHONUNBUFFERED="1", HF_HUB_DISABLE_TELEMETRY="1", WANDB_DISABLED="true")
                    env.pop("PYTHONPATH", None)
                    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                        child = subprocess.run(
                            [str(python), "-u", "-m", "training.source_answer",
                             "--project-dir", str(project), "--run-id", config["run_id"],
                             "--mode", config["mode"], "--question=" + config["question"]],
                            cwd=temporary, env=env, stdout=output, stderr=errors,
                            timeout=300, check=False,
                        )
                        if child.returncode != 0:
                            errors.seek(0, 2)
                            end = errors.tell()
                            errors.seek(max(0, end - 65536))
                            log = project / "jobs" / config["job_id"] / "testing-error.log"
                            log.parent.mkdir(parents=True, exist_ok=True)
                            log.write_bytes(errors.read(65536))
                        if child.returncode == 0 and output.tell() <= 131072:
                            output.seek(0)
                            result = {"ok": True, "reply": json.loads(output.read().decode("utf-8"))}
except BlockingIOError:
    result = {"ok": True, "error_code": "busy"}
except subprocess.TimeoutExpired:
    result = {"ok": True, "error_code": "timeout"}
except Exception:
    result = {"ok": True, "error_code": "inference_failed"}

print("CTH_CONTROL:" + json.dumps(result, ensure_ascii=False))
'''


ERRORS = {
    "model_missing": "Berkas model atau indeks sumber tidak tersedia di runtime ini. Pulihkan model tersimpan melalui tombol Pulihkan & jalankan. Tidak perlu training ulang.",
    "environment_missing": "Lingkungan Python training tidak tersedia di runtime ini. Model belum dapat dijalankan; dependensi tidak dipasang otomatis.",
    "busy": "Proyek sedang dipakai worker lain. Tunggu pekerjaannya selesai.",
    "timeout": "Pengujian melewati batas waktu. Tidak ada training atau pengiriman ulang otomatis.",
    "inference_failed": "Model belum berhasil menjawab. Periksa kecocokan kode, lingkungan Python, T4, dan berkas hasil training.",
}


@router.get("/status/{project_id}")
def testing_status(project_id: int, request: Request, job_id: str | None = None):
    local_only(request)
    project_row(project_id)
    job = selected_job(project_id, job_id)
    state = model_state(project_id, job)
    if "status" in state:
        return state
    bridge = None
    try:
        bridge = open_bridge(project_id)
        present = inspect_model(bridge, project_id, job)
        index = source_index_state(bridge, project_id, job) if present else {}
        return {
            **state, **index, "status": "available" if present else "missing",
            "message": (index.get('source_warning') or "Model tersedia dan kutipan sumber ditemukan. Relevansi jawaban tetap perlu diuji.")
            if present else "Model atau indeks sumber aman belum tersedia di runtime ini. Gunakan Pulihkan & jalankan untuk membangun indeks dari TXT tersimpan; tidak perlu training ulang.",
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Berkas model belum dapat diperiksa. Periksa koneksi Colab; jangan membuat runtime baru otomatis.") from None
    finally:
        if bridge:
            bridge.close()


@router.post("/chat")
def chat(body: ChatRequest, request: Request):
    local_only(request)
    project_row(body.project_id)
    question = body.question.strip()
    if not question or "\x00" in question:
        raise HTTPException(400, "Pertanyaan tidak boleh kosong atau mengandung NUL.")
    bridge = None
    with inference_lock():
        job = selected_job(body.project_id, body.job_id)
        state = model_state(body.project_id, job)
        if "status" in state:
            raise HTTPException(409, state["message"])
        try:
            bundle = source_bundle()
            bridge = open_bridge(body.project_id)
            if not inspect_model(bridge, body.project_id, job):
                raise HTTPException(409, ERRORS["model_missing"])
            bridge.require_idle_other_kernels()
            payload = {
                "project_id": body.project_id, "job_id": job["job_id"],
                "run_id": job["run_id"], "question": question,
                "mode": body.mode, "files": bundle,
            }
            encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
            result = bridge.execute(REMOTE.replace("__PAYLOAD__", repr(encoded)), timeout=330)
            if result.get("error_code"):
                raise HTTPException(409 if result["error_code"] in {"model_missing", "busy", "environment_missing"} else 502,
                                    ERRORS.get(result["error_code"], ERRORS["inference_failed"]))
            reply = result.get("reply")
            if not isinstance(reply, dict) or not isinstance(reply.get("answer"), str) or not reply["answer"].strip() or len(reply["answer"]) > 20000:
                raise HTTPException(502, "Format jawaban model tidak sesuai.")
            sources = reply.get("sources")
            if not isinstance(sources, list) or len(sources) > 50 or any(not isinstance(item, str) or len(item) > 300 for item in sources):
                raise HTTPException(502, "Format sumber jawaban tidak sesuai.")
            if reply.get("mode") != body.mode:
                raise HTTPException(502, "Mode jawaban tidak sesuai permintaan.")
            evidence = reply.get('evidence', [])
            if not isinstance(evidence, list) or len(evidence) > 3:
                raise HTTPException(502, 'Format kutipan sumber tidak sesuai.')
            for item in evidence:
                if (not isinstance(item, dict) or item.get('source') not in sources
                        or not isinstance(item.get('quote'), str) or len(item['quote']) > 800
                        or not isinstance(item.get('start'), int) or not isinstance(item.get('end'), int)
                        or item['start'] < 0 or item['end'] - item['start'] != len(item['quote'])
                        or item.get('offset_scope') != 'passage'):
                    raise HTTPException(502, 'Asal kutipan sumber tidak valid.')
            if reply["mode"] == "extractive":
                basis = "project" if sources else "none"
                notice = "Kutipan pencarian kata; bukan jawaban generatif model." if sources else "Tidak ada kutipan terverifikasi untuk pertanyaan ini; periksa peringatan indeks sumber."
            elif sources:
                basis = "mixed"
                notice = "Sumber proyek tersedia; jawaban generatif juga dapat mencakup pengetahuan umum."
            else:
                basis = "general"
                notice = "Pengetahuan umum model; sumber proyek yang relevan tidak ditemukan."
            return {
                "project_id": body.project_id, "run_id": job["run_id"],
                "model_name": job.get("model_name"), "answer": reply["answer"],
                "sources": sources, "mode": reply["mode"],
                "basis": basis, "notice": notice, "evidence": evidence,
            }
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Pengujian terputus atau Colab belum merespons. Tidak ada pengiriman ulang otomatis dan runtime tidak dihentikan oleh endpoint ini.") from None
        finally:
            if bridge:
                bridge.close()


class RestoreRequest(BaseModel):
    refresh_sources: bool = False
    source_dataset_ids: list[str] = Field(default_factory=list, max_length=500)
    project_id: int = Field(gt=0)
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.get("/models/{project_id}")
def saved_models(project_id: int, request: Request):
    local_only(request)
    project_row(project_id)

    return {
        "project_id": project_id,
        "models": catalog(project_id),
    }


@router.get("/restore/{project_id}")
def restore_progress(project_id: int, request: Request):
    local_only(request)
    project_row(project_id)

    return restore_state(project_id)


@router.post("/restore", status_code=202)
def restore_model(body: RestoreRequest, request: Request):
    local_only(request)
    project_row(body.project_id)

    job = selected_job(body.project_id, body.job_id)
    if body.refresh_sources:
        if not body.source_dataset_ids:
            raise HTTPException(400, 'Pilih berkas fakta proyek untuk memperbarui sumber.')
        from app.dataset_manifest import verify_selection
        sources = verify_selection(body.project_id, body.source_dataset_ids)['datasets']
        if any(item.get('source_mode') != 'facts_only' for item in sources):
            raise HTTPException(409, 'Pembaruan sumber eksplisit hanya menerima berkas berperan fakta proyek, bukan percakapan latihan.')
        job = {**job, 'inference_sources': sources}
        return begin_restore(job, open_bridge)
    if body.source_dataset_ids:
        raise HTTPException(400, 'Konfirmasi refresh_sources diperlukan untuk mengganti sumber inferensi.')
    bridge = None
    try:
        bridge = open_bridge(body.project_id)
        if inspect_model(bridge, body.project_id, job):
            index = source_index_state(bridge, body.project_id, job)
            return {"project_id": body.project_id, "job_id": body.job_id, "status": "completed", **index,
                    "message": "Artefak model tersedia; tidak melatih ulang. " + (index.get('source_warning') or "Kutipan sumber ditemukan.")}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Runtime belum dapat diperiksa. Periksa koneksi sebelum mencoba pemulihan lagi.") from None
    finally:
        if bridge:
            bridge.close()
    return begin_restore(job, open_bridge)
