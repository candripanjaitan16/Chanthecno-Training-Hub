from pathlib import Path
from contextlib import asynccontextmanager
import fcntl

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.colab import router as colab_router
from app.database import init_db
from app.datasets import router as datasets_router
from app.drive_auth import check_drive_connection
from app.projects import router as projects_router
from app.model_testing import router as model_testing_router
from app.updates import router as updates_router
from app.version import VERSION


@asynccontextmanager
async def lifespan(app):
    from app.paths import STATE_HOME, prepare
    prepare()
    with (STATE_HOME / "api.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        init_db()
        from app.datasets import resume_syncs
        from app.training_jobs import resume_monitor
        resume_syncs()
        resume_monitor()
        yield


app = FastAPI(
    title="ChanThecno Training Hub",
    version=VERSION,
    lifespan=lifespan,
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])


@app.middleware("http")
async def local_access(request: Request, call_next):
    origin = request.headers.get("origin")
    allowed = {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"}
    if origin and origin not in allowed:
        return JSONResponse({"detail": "Origin tidak diizinkan."}, status_code=403)
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        return JSONResponse({"detail": "Aplikasi hanya tersedia secara lokal."}, status_code=403)
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects_router)
app.include_router(colab_router)
app.include_router(datasets_router)
app.include_router(model_testing_router)
app.include_router(updates_router)


@app.get("/api/hello", tags=["General"])
def hello():
    return {
        "message": "ChanThecno Training Hub Run!",
        "version": VERSION,
    }


@app.get("/api/connections/google-drive", tags=["Connections"])
def google_drive_status():
    return check_drive_connection()


@app.get("/api/local-state", tags=["General"])
def local_state():
    from app.paths import DATA_HOME
    marker = DATA_HOME / "browser-generation"
    return {"generation": marker.read_text().strip() if marker.is_file() else "initial"}


frontend = Path(__file__).resolve().parents[2] / "frontend/dist"
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
