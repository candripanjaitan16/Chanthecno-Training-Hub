from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.database import get_db
from app.drive_manager import create_project_folder

router = APIRouter(prefix="/api/projects", tags=["Projects"])


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    model_name: str = ""


@router.post("/")
def create_project(project: ProjectCreate):

    if not project.name.strip():
        raise HTTPException(
            status_code=400,
            detail="Nama proyek wajib diisi"
        )

    # Buat folder di Google Drive
    try:
        folder_id = create_project_folder(project.name)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Folder Google Drive belum berhasil dibuat. Periksa otorisasi akun dan koneksi Drive."
        ) from None

    # Simpan proyek ke SQLite
    conn = get_db()

    cursor = conn.execute("""
        INSERT INTO projects
        (name, description, model_name, drive_folder_id)
        VALUES (?, ?, ?, ?)
    """, (
        project.name,
        project.description,
        project.model_name,
        folder_id
    ))

    conn.commit()

    project_id = cursor.lastrowid
    conn.close()

    return {
        "status": "success",
        "message": "Proyek dan folder Google Drive berhasil dibuat",
        "project_id": project_id,
        "drive_folder_id": folder_id
    }


@router.get("/")
def get_projects():

    conn = get_db()

    projects = conn.execute(
        "SELECT * FROM projects ORDER BY id DESC"
    ).fetchall()

    conn.close()

    return [dict(project) for project in projects]
