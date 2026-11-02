import json
import os
import tempfile
from pathlib import Path

MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def authentication_name(name):
    name = name.lower()
    return (
        name == "credentials.json"
        or name.startswith("client_secret")
        or (name.startswith("token") and not name.startswith("tokenizer") and name.endswith(".json"))
        or (name.startswith(".env") and name != ".env.example")
        or Path(name).suffix in {".key", ".pem"}
    )


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
