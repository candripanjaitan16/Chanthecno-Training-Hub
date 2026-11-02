"""Per-user storage shared by the API, launcher and maintenance commands."""
import os
from pathlib import Path


def location(variable, fallback):
    path = Path(os.environ.get(variable, fallback)).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{variable} must be an absolute path")
    return path.resolve()


DATA_HOME = location("CTH_DATA_DIR", Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "chanthecno-hub/data")
CONFIG_HOME = location("CTH_CONFIG_DIR", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "chanthecno-hub")
STATE_HOME = location("CTH_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "chanthecno-hub")
WORKSPACE = DATA_HOME / "workspace"


def prepare():
    for directory in (DATA_HOME, CONFIG_HOME, STATE_HOME):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
