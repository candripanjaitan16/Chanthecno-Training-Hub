import fcntl
import json
import re
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession
from pydantic import BaseModel, Field
from requests.exceptions import RequestException

from app.database import get_db
from app.drive_auth import get_colab_credentials


router = APIRouter(
    prefix="/api/colab",
    tags=["Google Colab"],
)

from app.paths import DATA_HOME, prepare

BASE_DIR = DATA_HOME
API_URL = "https://colaboratory.googleapis.com/v1beta"
STATE_FILE = BASE_DIR / "colab_runtime_state.json"
LOCK_FILE = BASE_DIR / ".colab-runtime.lock"

RUNTIME_ID = "cth-training-t4"
RUNTIME_NAME = f"runtimes/{RUNTIME_ID}"

T4_SPEC = {
    "variant": "VARIANT_GPU",
    "accelerator": "T4",
    "shape": "SHAPE_STANDARD",
}


class ConnectRequest(BaseModel):
    project_id: int = Field(gt=0)


def load_state():
    if not STATE_FILE.exists():
        return {}

    data = json.loads(
        STATE_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError("Format catatan runtime tidak sesuai.")

    return data


def save_state(data):
    from app.storage import atomic_json
    atomic_json(STATE_FILE, data)


@contextmanager
def runtime_lock():
    prepare()
    with LOCK_FILE.open("a") as handle:
        try:
            fcntl.flock(
                handle,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )

        except BlockingIOError:
            raise HTTPException(
                409,
                "Pemeriksaan Colab lain masih berjalan. Coba lagi.",
            ) from None

        try:
            yield

        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def google_request(
    session,
    method,
    url,
    allow_missing=False,
    **kwargs,
):
    response = session.request(
        method,
        url,
        timeout=20,
        **kwargs,
    )

    if allow_missing and response.status_code == 404:
        return None

    if not response.ok:
        if response.status_code == 401:
            raise HTTPException(
                401,
                "Otorisasi Google Colab perlu diperbarui.",
            )

        if response.status_code == 429:
            raise HTTPException(
                429,
                "Batas permintaan Google tercapai. Tunggu sebelum mencoba lagi.",
            )

        raise HTTPException(
            502,
            f"Google Colab mengembalikan HTTP {response.status_code}. Tidak beralih ke CPU.",
        )

    data = response.json()

    if not isinstance(data, dict):
        raise ValueError("Respons Google tidak sesuai.")

    return data


def read_api(session, path):
    return google_request(
        session,
        "GET",
        f"{API_URL}/{path}",
    )


def result(status, message, state, **extra):
    return {
        "status": status,
        "message": message,
        "runtime_name": RUNTIME_NAME,
        "project_id": state.get("project_id"),
        "gpu": "T4" if status == "allocated" else None,
        "shape": "Standard" if status == "allocated" else None,
        "gpu_verified": False,
        "training_ready": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def inspect_runtime(session, state):
    runtime = google_request(
        session,
        "GET",
        f"{API_URL}/{RUNTIME_NAME}",
        allow_missing=True,
    )

    if runtime is not None:
        if runtime.get("runtimeSpec") != T4_SPEC:
            return result(
                "mismatch",
                "Runtime CTH bukan T4 Standard. Tidak mengganti atau menghapusnya.",
                state,
            )

        state.update(
            runtime_name=RUNTIME_NAME,
            status="allocated",
        )

        save_state(state)

        return result(
            "allocated",
            "Runtime T4 tersedia melalui API. Uji eksekusi GPU belum dilakukan.",
            state,
        )

    operation_name = state.get("operation_name")

    if operation_name:
        if (
            not isinstance(operation_name, str)
            or not re.fullmatch(
                r"operations/[A-Za-z0-9_/-]+",
                operation_name,
            )
        ):
            raise ValueError("Nama operation tidak valid.")

        operation = google_request(
            session,
            "GET",
            f"https://colaboratory.googleapis.com/v1/{operation_name}",
            allow_missing=True,
        )

        if operation is not None and not operation.get("done", False):
            return result(
                "provisioning",
                "Google sedang menyiapkan T4. Tidak beralih ke CPU.",
                state,
            )

        if operation is not None and operation.get("error"):
            state["status"] = "operation_failed"
            save_state(state)

            return result(
                "error",
                "Google menolak atau gagal menyiapkan T4. Tidak beralih ke CPU.",
                state,
            )

        state["status"] = "disconnected"
        save_state(state)

        return result(
            "disconnected",
            "Runtime CTH tidak terdaftar. Hubungkan kembali untuk meminta T4 baru.",
            state,
        )

    if state.get("status") == "requesting":
        return result(
            "uncertain",
            "Hasil permintaan sebelumnya belum pasti. Tombol akan melanjutkan request ID yang sama.",
            state,
        )

    return result(
        "disconnected",
        "Belum ada runtime CTH. Klik Hubungkan ke Colab.",
        state,
    )


def with_google(action):
    try:
        with runtime_lock():
            with AuthorizedSession(get_colab_credentials()) as session:
                return action(session, load_state())

    except RefreshError:
        raise HTTPException(
            401,
            "Otorisasi Colab perlu diperbarui dari backend.",
        ) from None

    except (TransportError, RequestException):
        raise HTTPException(
            503,
            "Google Colab belum dapat dihubungi. Hasil permintaan belum pasti; periksa ulang dahulu.",
        ) from None

    except (ValueError, KeyError, OSError):
        raise HTTPException(
            503,
            "Token, catatan runtime, atau respons Google belum dapat dibaca. Jangan hapus token.",
        ) from None


def require_project(project_id):
    conn = get_db()

    try:
        project = conn.execute(
            "SELECT id FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()

    finally:
        conn.close()

    if project is None:
        raise HTTPException(
            404,
            "Proyek tidak ditemukan.",
        )


def local_request(request):
    allowed = {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    }

    if request.headers.get("origin") not in allowed | {None}:
        raise HTTPException(
            403,
            "Origin tidak diizinkan.",
        )

    if request.client and request.client.host not in {
        "127.0.0.1",
        "::1",
        "testclient",
    }:
        raise HTTPException(
            403,
            "Endpoint ini hanya untuk aplikasi lokal.",
        )


SWITCH_CHECK = '''
import fcntl
import json
import subprocess
from contextlib import ExitStack

result = {"ok": True, "safe": False}

try:
    with ExitStack() as stack:
        root = Path("/content/cth/projects")

        if root.is_dir():
            for project in root.iterdir():
                if not project.name.isdigit() or not project.is_dir():
                    continue

                if project.resolve() != project:
                    raise RuntimeError()

                for name in (".cth-web.lock", ".cth-worker.lock"):
                    handle = stack.enter_context(
                        (project / name).open("a")
                    )

                    fcntl.flock(
                        handle,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )

        gpu = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )

        result["safe"] = not gpu.stdout.strip()

except Exception:
    pass

print("CTH_CONTROL:" + json.dumps(result))
'''


@contextmanager
def project_switch_guard():
    from app.colab_stop import (
        LOCK_FILE as stop_lock,
        TERMINAL as stop_terminal,
        job_read,
    )
    from app.training_jobs import WORK, load_job

    with ExitStack() as stack:
        for path in (WORK / "launch.lock", stop_lock):
            path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            handle = stack.enter_context(path.open("a"))

            try:
                fcntl.flock(
                    handle,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )

            except BlockingIOError:
                raise HTTPException(
                    409,
                    "Training, testing, atau Stop & Simpan masih berjalan. Pergantian proyek ditunda.",
                ) from None

        training = load_job()
        stopping = job_read()

        if (
            training.get("job_id")
            and training.get("status") not in {
                "idle",
                "completed",
                "stopped",
                "failed",
            }
        ):
            raise HTTPException(
                409,
                "Pekerjaan training atau pencadangan belum selesai. Runtime tetap terkait proyek sebelumnya.",
            )

        if (
            stopping.get("job_id")
            and stopping.get("status") not in stop_terminal
        ):
            raise HTTPException(
                409,
                "Stop & Simpan belum selesai. Runtime tetap terkait proyek sebelumnya.",
            )

        yield


def assign_project(current, state, project_id):
    previous = state.get("project_id")

    if previous == project_id:
        return current

    if current["status"] != "allocated":
        raise HTTPException(
            409,
            "Tunggu runtime T4 tersedia sebelum mengganti proyek yang terkait.",
        )

    from app.colab_bridge import RuntimeBridge

    bridge = None

    with project_switch_guard():
        try:
            bridge = RuntimeBridge(
                RUNTIME_NAME,
                T4_SPEC,
            )

            bridge.require_idle_other_kernels()

            check = bridge.execute(
                SWITCH_CHECK,
                timeout=45,
            )

            if check.get("safe") is not True:
                raise HTTPException(
                    409,
                    "Masih ada worker atau proses GPU aktif, atau pemeriksaan belum berhasil. Runtime tidak dipindahkan.",
                )

            bridge.refresh_connection()

            state.update(
                project_id=project_id,
                selected_at=datetime.now(timezone.utc).isoformat(),
            )

            save_state(state)

            return {
                **current,
                "project_id": project_id,
                "previous_project_id": previous,
                "message": "Runtime T4 mengikuti proyek yang dipilih. Folder proyek sebelumnya tetap disimpan.",
            }

        except HTTPException:
            raise

        except Exception:
            raise HTTPException(
                503,
                "Kesiapan runtime untuk pergantian proyek belum dapat diperiksa. Hubungan proyek sebelumnya dipertahankan.",
            ) from None

        finally:
            if bridge:
                bridge.close()


@router.get("/runtime")
def runtime_status():
    return with_google(inspect_runtime)


@router.post("/select-project")
def select_runtime_project(
    body: ConnectRequest,
    request: Request,
):
    local_request(request)
    require_project(body.project_id)

    def select(session, state):
        current = inspect_runtime(session, state)

        if current["status"] == "allocated":
            return assign_project(
                current,
                state,
                body.project_id,
            )

        return {
            **current,
            "selected_project_id": body.project_id,
        }

    return with_google(select)


@router.post("/connect")
def connect_colab(
    body: ConnectRequest,
    request: Request,
):
    local_request(request)
    require_project(body.project_id)

    def connect(session, state):
        current = inspect_runtime(session, state)

        if current["status"] == "allocated":
            return assign_project(
                current,
                state,
                body.project_id,
            )

        if current["status"] == "provisioning":
            if state.get("project_id") not in (
                None,
                body.project_id,
            ):
                raise HTTPException(
                    409,
                    "Runtime masih disiapkan untuk proyek sebelumnya. Tunggu alokasi selesai; tidak membuat sesi kedua.",
                )

            state["project_id"] = body.project_id
            save_state(state)

            return {
                **current,
                "project_id": body.project_id,
            }

        if current["status"] == "mismatch":
            raise HTTPException(
                409,
                current["message"],
            )

        assigned = read_api(
            session,
            "runtimes",
        ).get("runtimes", [])

        if assigned:
            raise HTTPException(
                409,
                "Ada runtime lain pada akun ini. Tidak membuat atau mengganti runtime otomatis; periksa sesi yang ada.",
            )

        specs = read_api(
            session,
            "runtimespecs",
        ).get("runtimeSpecs", [])

        eligible = any(
            item.get("key") == T4_SPEC
            and item.get("eligible") is True
            for item in specs
        )

        if not eligible:
            raise HTTPException(
                409,
                "T4 Standard belum diizinkan saat ini. Tidak beralih ke CPU.",
            )

        request_id = (
            state.get("request_id")
            if current["status"] == "uncertain"
            else str(uuid4())
        )

        try:
            if UUID(request_id).version != 4:
                raise ValueError()

        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                409,
                "Catatan request ID perlu diperiksa sebelum melanjutkan.",
            ) from None

        state.clear()

        state.update(
            runtime_name=RUNTIME_NAME,
            request_id=request_id,
            project_id=body.project_id,
            status="requesting",
        )

        save_state(state)

        operation = google_request(
            session,
            "POST",
            f"{API_URL}/runtimes",
            params={
                "runtimeId": RUNTIME_ID,
                "requestId": request_id,
            },
            json={
                "runtimeSpec": T4_SPEC,
            },
        )

        name = operation.get("name")

        if (
            not isinstance(name, str)
            or not re.fullmatch(
                r"operations/[A-Za-z0-9_/-]+",
                name,
            )
        ):
            raise HTTPException(
                502,
                "Respons pembuatan runtime belum dapat dipastikan. Periksa ulang status.",
            )

        state.update(
            operation_name=name,
            status="provisioning",
        )

        save_state(state)

        if operation.get("done"):
            return inspect_runtime(session, state)

        return result(
            "provisioning",
            "Permintaan T4 diterima. Status akan diperiksa otomatis.",
            state,
        )

    return with_google(connect)


@router.get("/check")
def colab_check():
    def check(session, state):
        specs = read_api(
            session,
            "runtimespecs",
        ).get("runtimeSpecs", [])

        runtimes = read_api(
            session,
            "runtimes",
        ).get("runtimes", [])

        return {
            "api_status": "connected",
            "t4_eligible": any(
                item.get("key") == T4_SPEC
                and item.get("eligible") is True
                for item in specs
            ),
            "requested_spec": T4_SPEC,
            "runtimes": [
                {
                    "name": item.get("name"),
                    "runtimeSpec": item.get("runtimeSpec"),
                }
                for item in runtimes
            ],
        }

    return with_google(check)


from app.colab_stop import router as stop_router
from app.training_jobs import router as training_router

router.include_router(stop_router)
router.include_router(training_router)


if __name__ == "__main__":
    try:
        print(
            json.dumps(
                colab_check(),
                indent=2,
                ensure_ascii=False,
            )
        )

    except HTTPException as error:
        print(
            f"HTTP {error.status_code}: {error.detail}"
        )