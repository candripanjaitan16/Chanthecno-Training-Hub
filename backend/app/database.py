import sqlite3
from app.paths import DATA_HOME, prepare

DATABASE = DATA_HOME / "cth.db"

def get_db():
    prepare()
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            model_name TEXT,
            model_status TEXT DEFAULT 'draft',
            drive_folder_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
